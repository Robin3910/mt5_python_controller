"""客户端版本更新：检测、批量更新、指定版本与本机回滚（UI 无关的编排层）。

与 `import_dialog.py` 是同一套形态：只做编排与进度记录，不碰任何前端细节，
由桥接层把 HTTP 请求翻译成这里的方法。

流程（更新）：
    ① 校验目标版本与实例选择
    ② 下载安装包 → 校验 sha256
    ③ 解压到临时目录
    ④ 逐实例「停 → 版本化备份 → 覆盖 → 按需再启」（复用 ProcessManager）

回滚走本机 `.backups/`，不依赖网络。

耗时动作（下载 / 覆盖）都放后台线程，`start` 立即返回 task_id，
前端轮询 `progress` 拿状态 —— 否则一次更新会把 HTTP 连接占住几分钟。
"""
from __future__ import annotations

import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import version_service as vs
from client_deploy import (
    DOWNGRADE,
    classify_update,
    count_pending_upgrades,
    extract_package,
    list_local_backups,
    read_version_near,
    update_label,
)

# 进度步骤标识
STEP_DOWNLOAD = "download"
STEP_VERIFY = "verify"
STEP_EXTRACT = "extract"
STEP_APPLY = "apply"

STEP_LABELS = {
    STEP_DOWNLOAD: "下载安装包",
    STEP_VERIFY: "校验安装包",
    STEP_EXTRACT: "解压安装包",
    STEP_APPLY: "覆盖并重启",
}

# 覆盖阶段的细粒度文案（与 ProcessManager._PHASES 对齐）
PHASE_LABELS = {
    "stop": "停止实例…",
    "apply": "覆盖文件…",
    "restart": "重新启动…",
}

# 发布版本缓存时长（秒）。面板每 1 秒推一次快照，不做缓存会把一次后端查询
# 放大成每秒一次。
RELEASE_TTL_S = 60.0


class VersionUpdateError(RuntimeError):
    """版本更新过程中的可读错误。"""


@dataclass
class UpdateTask:
    """一次更新/回滚任务的进度状态。"""

    task_id: str
    kind: str = "update"  # update | rollback
    steps: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    done: bool = False
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "steps": list(self.steps),
            "log": list(self.log),
            "done": self.done,
            "error": self.error,
            "result": dict(self.result),
            "elapsed": round(time.time() - self.started_at, 1),
        }


class VersionUpdateController:
    """版本更新任务的调度者。持有进度表，供桥接层查询。"""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self._tasks: dict[str, UpdateTask] = {}
        self._lock = threading.RLock()
        # 同一时间只允许一个版本任务：覆盖会抢占 MT5 目录里的 exe 文件
        self._active: str = ""
        self._published = ""
        self._published_at = 0.0
        self._pending_cache = 0
        self._pending_cache_version = ""
        self._pending_cache_at = 0.0
        # 顶栏徽标的缓存值：快照读它，保证零阻塞
        self._badge: dict[str, Any] = {"published": "", "pending": 0}

    # ── 进度表 ──

    def task(self, task_id: str) -> UpdateTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def active_task(self) -> str:
        with self._lock:
            return self._active

    # ── 只读查询 ──

    def instances(self) -> dict[str, Any]:
        """本机各实例的客户端版本与可回滚备份。"""
        self._bridge.session.check()
        items: list[dict[str, Any]] = []
        for cfg in self._bridge.manager.configs():
            mp = self._bridge.manager.get(cfg.id)
            items.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "version": read_version_near(cfg.exe_path),
                    "backups": list_local_backups(cfg.exe_path),
                    "alive": bool(mp is not None and mp.runtime.process_alive),
                }
            )
        published = self.published()
        return {
            "ok": True,
            "error": "",
            "instances": items,
            "published": published,
            "pending": self.pending_count(published),
        }

    def published(self, *, force: bool = False) -> str:
        """后端当前发布版本号；带 TTL 缓存，未登录或取不到时返回空串。

        这是**阻塞**版本（要走网络）：只在后台刷新线程与用户主动「检测更新」时调用。
        快照里读的是 `badge()` 的缓存值，不能走这里。
        """
        now = time.monotonic()
        with self._lock:
            if not force and self._published and now - self._published_at < RELEASE_TTL_S:
                return self._published
        try:
            target = self._bridge.version_target()
            data = vs.fetch_current_version(target)
            version = str((data or {}).get("version") or "")
        except Exception:  # noqa: BLE001 - 取不到就沿用旧值，不打扰用户
            with self._lock:
                return self._published
        with self._lock:
            self._published = version
            self._published_at = time.monotonic()
        return version

    def pending_count(self, published: str = "") -> int:
        """有多少个实例的版本低于发布版本（顶栏徽标）。

        随发布版本一起缓存：版本徽标每 1 秒随快照推一次，
        每次都去读一遍各实例的 version.txt 纯属浪费。
        """
        version = published or self.published()
        if not version:
            return 0
        now = time.monotonic()
        with self._lock:
            if (
                self._pending_cache_version == version
                and now - self._pending_cache_at < RELEASE_TTL_S
            ):
                return self._pending_cache
        versions = [
            read_version_near(cfg.exe_path) for cfg in self._bridge.manager.configs()
        ]
        count = count_pending_upgrades(versions, version)
        with self._lock:
            self._pending_cache_version = version
            self._pending_cache = count
            self._pending_cache_at = time.monotonic()
        return count

    def badge(self) -> dict[str, Any]:
        """顶栏徽标（只读缓存，**不发起任何网络请求**）。

        快照每 1 秒推一次，这里必须零阻塞：发布版本与待更新数由后台刷新线程
        写进缓存，这里只读。
        """
        with self._lock:
            return dict(self._badge)

    def refresh_badge(self) -> dict[str, Any]:
        """重新算一遍徽标并写进缓存（后台线程调用，可阻塞）。"""
        try:
            published = self.published()
            pending = self.pending_count(published)
        except Exception:  # noqa: BLE001 - 未登录 / 后端不可达时沿用旧值
            with self._lock:
                return dict(self._badge)
        with self._lock:
            self._badge = {"published": published, "pending": pending}
            return dict(self._badge)

    def invalidate_badge(self) -> None:
        """标记徽标需要重算（实例增删 / 更新完成之后调用）。

        清缓存 + 起一个后台线程立刻重算：徽标缓存周期是 60 秒，
        刚更新完就让用户盯着旧数字看 60 秒是不能接受的。
        """
        with self._lock:
            self._pending_cache_version = ""
            self._pending_cache_at = 0.0
            self._published_at = 0.0
        threading.Thread(
            target=self.refresh_badge, name="badge-refresh-now", daemon=True
        ).start()

    def detect(self) -> dict[str, Any]:
        """检测后端发布版本与可下载版本清单（强制刷新缓存）。"""
        self._bridge.session.check()
        target = self._bridge.version_target()
        if not target.ready:
            return {"ok": False, "error": "尚未配置后端地址或节点令牌", "published": "", "versions": [], "notes": ""}
        try:
            items, current = vs.fetch_available_versions(target)
        except vs.VersionServiceError as exc:
            return {"ok": False, "error": str(exc), "published": "", "versions": [], "notes": ""}

        published = current
        notes = ""
        if not published:
            published = self.published(force=True)
        else:
            with self._lock:
                self._published = published
                self._published_at = time.monotonic()
        for item in items:
            if str(item.get("version") or "") == published:
                notes = str(item.get("notes") or "")
                break

        versions = [
            {
                "version": str(i.get("version") or ""),
                "size": int(i.get("size") or 0),
                "sha256": str(i.get("sha256") or ""),
                "notes": str(i.get("notes") or ""),
                "is_current": bool(i.get("is_current")) or str(i.get("version") or "") == published,
            }
            for i in items
            if i.get("version")
        ]
        return {
            "ok": True,
            "error": "",
            "published": published,
            "versions": versions,
            "notes": notes,
        }

    # ── 更新前校验 ──

    def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        """校验目标版本与实例选择，返回逐实例判定与二次确认摘要。"""
        self._bridge.session.check()
        version = str(body.get("version") or "").strip()
        if not version:
            return {"ok": False, "error": "目标版本为空", "summary": "", "rows": [], "downgrades": []}

        ids = [str(i) for i in (body.get("ids") or []) if str(i)]
        rows = self._rows(ids, version)
        if not rows:
            return {
                "ok": False,
                "error": "请先勾选要更新的实例",
                "summary": "",
                "rows": [],
                "downgrades": [],
            }

        downgrades = [r for r in rows if r["kind"] == DOWNGRADE]
        lines = [f"将把以下 {len(rows)} 个实例更新到 v{version}：", ""]
        for r in rows:
            lines.append(
                f"  · {r['name']}  v{r['current'] or '?'} → v{version}  [{r['label']}]"
            )
        lines += [
            "",
            "运行中的实例会先停止 → 备份当前版本 → 覆盖 → 再自动启动。",
            ".env 不会被覆盖。",
            "是否继续？",
        ]
        return {
            "ok": True,
            "error": "",
            "summary": "\n".join(lines),
            "rows": rows,
            "downgrades": [r["name"] for r in downgrades],
        }

    def _rows(self, ids: list[str], version: str) -> list[dict[str, Any]]:
        """实例 × 目标版本 的判定清单（供前端展示与二次确认）。

        `ids` 为空表示「一个都没选」——不能当成「全选」，
        否则用户点「更新」时没勾选也会把全部实例拖下水。
        """
        if not ids:
            return []
        selected = set(ids)
        rows: list[dict[str, Any]] = []
        for cfg in self._bridge.manager.configs():
            if cfg.id not in selected:
                continue
            current = read_version_near(cfg.exe_path)
            kind = classify_update(current, version) if version else "unknown"
            rows.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "current": current,
                    "target": version,
                    "kind": kind,
                    "label": update_label(kind),
                }
            )
        return rows

    # ── 异步执行 ──

    def start_update(self, body: dict[str, Any]) -> dict[str, Any]:
        """启动「更新到指定版本」任务，立即返回 task_id。"""
        self._bridge.session.check()
        version = str(body.get("version") or "").strip()
        sha256 = str(body.get("sha256") or "").strip()
        ids = [str(i) for i in (body.get("ids") or []) if str(i)]
        if not version:
            return {"ok": False, "error": "目标版本为空", "task_id": ""}
        if not ids:
            return {"ok": False, "error": "请先勾选要更新的实例", "task_id": ""}
        return self._start("update", version, sha256, ids)

    def start_rollback(self, body: dict[str, Any]) -> dict[str, Any]:
        """启动「回滚到本机备份」任务，立即返回 task_id。"""
        self._bridge.session.check()
        version = str(body.get("version") or "").strip()
        ids = [str(i) for i in (body.get("ids") or []) if str(i)]
        if not version:
            return {"ok": False, "error": "目标版本为空", "task_id": ""}
        if not ids:
            return {"ok": False, "error": "请先勾选要回滚的实例", "task_id": ""}

        # 只保留本机确实有该版本备份的实例：ProcessManager 会逐个校验绑定能力，
        # 缺备份的实例会让整个批次直接失败，而不是跳过这一个。
        usable = []
        for cfg in self._bridge.manager.configs():
            if cfg.id not in set(ids):
                continue
            if version in list_local_backups(cfg.exe_path):
                usable.append(cfg.id)
        if not usable:
            return {
                "ok": False,
                "error": "所选实例都没有该版本的本机备份",
                "task_id": "",
            }
        return self._start("rollback", version, "", usable)

    def _start(self, kind: str, version: str, sha256: str, ids: list[str]) -> dict[str, Any]:
        with self._lock:
            if self._active:
                return {"ok": False, "error": "上一个版本操作还在进行中", "task_id": self._active}
            task_id = uuid.uuid4().hex[:12]
            self._tasks[task_id] = UpdateTask(task_id=task_id, kind=kind)
            self._active = task_id

        thread = threading.Thread(
            target=self._run,
            args=(task_id, kind, version, sha256, ids),
            name=f"verupdate-{task_id}",
            daemon=True,
        )
        thread.start()
        return {"ok": True, "error": "", "task_id": task_id}

    def _run(self, task_id: str, kind: str, version: str, sha256: str, ids: list[str]) -> None:
        try:
            if kind == "rollback":
                results = self._run_rollback(task_id, version, ids)
            else:
                results = self._run_update(task_id, version, sha256, ids)
            self._finish(task_id, results=results)
        except Exception as exc:  # noqa: BLE001
            self._finish(task_id, error=str(exc) or exc.__class__.__name__)

    def _run_update(self, task_id: str, version: str, sha256: str, ids: list[str]) -> list:
        bridge = self._bridge
        target = bridge.version_target()
        if not target.ready:
            raise VersionUpdateError("尚未配置后端地址或节点令牌")

        with tempfile.TemporaryDirectory(prefix="node_client_update_") as tmp:
            tmpdir = Path(tmp)
            pkg = tmpdir / f"node_client-{version}.zip"

            self._record(task_id, STEP_DOWNLOAD, 0.02, f"正在下载 v{version}…")

            def on_progress(done: int, total: int) -> None:
                if total > 0:
                    pct = 0.02 + 0.48 * (done / total)
                    note = f"{done * 100 // total}%"
                else:
                    pct = None
                    note = f"{done // 1024 // 1024} MB"
                self._record(task_id, STEP_DOWNLOAD, pct, note)

            vs.download_package(target, version, pkg, on_progress=on_progress)

            self._record(task_id, STEP_VERIFY, 0.52, "正在校验安装包 sha256…")
            if sha256 and not vs.verify_sha256(pkg, sha256):
                raise VersionUpdateError("安装包校验和不匹配，已中止更新")

            self._record(task_id, STEP_EXTRACT, 0.56, "正在解压安装包…")
            extract_dir = tmpdir / "extracted"
            ok, msg = extract_package(pkg, extract_dir)
            if not ok:
                raise VersionUpdateError(msg)

            self._record(task_id, STEP_APPLY, 0.60, f"正在更新 {len(ids)} 个实例…")

            def on_instance(index: int, total: int, name: str, phase: str) -> None:
                label = PHASE_LABELS.get(phase, "处理中…")
                pct = 0.60 + 0.38 * (index / max(total, 1))
                self._record(
                    task_id, STEP_APPLY, pct, f"正在更新 ({index}/{total}) {name}：{label}"
                )

            return bridge.manager.batch_update(
                extract_dir,
                instance_ids=ids,
                restart_if_was_running=True,
                on_progress=on_instance,
            )

    def _run_rollback(self, task_id: str, version: str, ids: list[str]) -> list:
        self._record(task_id, STEP_APPLY, 0.10, f"正在回滚 {len(ids)} 个实例到 v{version}…")

        def on_instance(index: int, total: int, name: str, phase: str) -> None:
            label = PHASE_LABELS.get(phase, "处理中…")
            pct = 0.10 + 0.85 * (index / max(total, 1))
            self._record(
                task_id, STEP_APPLY, pct, f"正在回滚 ({index}/{total}) {name}：{label}"
            )

        return self._bridge.manager.batch_rollback(
            version,
            instance_ids=ids,
            restart_if_was_running=True,
            on_progress=on_instance,
        )

    # ── 进度记录 ──

    def _record(
        self, task_id: str, key: str, pct: float | None, note: str
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            existing = next((s for s in task.steps if s["key"] == key), None)
            if existing is None:
                existing = {
                    "key": key,
                    "label": STEP_LABELS.get(key, key),
                    "pct": 0.0,
                    "status": "pending",
                }
                task.steps.append(existing)
            existing["label"] = STEP_LABELS.get(key, key)
            if pct is not None:
                existing["pct"] = round(float(pct), 4)
            existing["status"] = "active"
            if note:
                stamp = time.strftime("%H:%M:%S")
                task.log.append(f"[{stamp}] {note}")
                if len(task.log) > 200:
                    del task.log[:-200]

    def _finish(
        self,
        task_id: str,
        *,
        results: list | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.done = True
            task.error = error
            if error:
                for step in task.steps:
                    if step["status"] == "active":
                        step["status"] = "failed"
            else:
                for step in task.steps:
                    if step["status"] != "failed":
                        step["status"] = "done"
            if results is not None:
                rows = []
                ok_count = 0
                for r in results:
                    ok = bool(getattr(r, "ok", False))
                    if ok:
                        ok_count += 1
                    rows.append(
                        {
                            "id": str(getattr(r, "instance_id", "") or ""),
                            "name": str(getattr(r, "name", "") or ""),
                            "ok": ok,
                            "message": str(getattr(r, "message", "") or ""),
                            "old_version": str(getattr(r, "old_version", "") or ""),
                            "new_version": str(getattr(r, "new_version", "") or ""),
                        }
                    )
                task.result = {"ok_count": ok_count, "total": len(rows), "results": rows}
                # 版本变了，待更新数要重算（否则徽标会一直亮着）。
                self.invalidate_badge()
            if self._active == task_id:
                self._active = ""
