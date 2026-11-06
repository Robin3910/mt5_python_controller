"""Web 版导入节点：参数收集 + 异步任务调度。

与 `import_service.py` 的分工：
    - `import_service.py`：纯编排逻辑，不碰 UI，也不碰 HTTP；
    - 本模块：把 HTTP 请求翻译成 service 调用，并维护进度任务表。

导入要下载解压，可能几分钟，不能占住 HTTP 连接。
所以 `start` 立即返回 task_id，前端轮询 `progress` 拿状态。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import env_file as ef
import version_service as vs
from auth_service import DashboardAuthError
from import_service import (
    STEP_LABELS,
    ImportError_,
    ImportRequest,
    ImportResult,
    check_directory,
    resolve_account,
    run_import,
    summarize,
)
from models import InstanceConfig


@dataclass
class ImportTask:
    """一次导入任务的进度状态。"""

    task_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    done: bool = False
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "steps": list(self.steps),
            "log": list(self.log),
            "done": self.done,
            "error": self.error,
            "result": dict(self.result),
            "elapsed": round(time.time() - self.started_at, 1),
        }


class ImportController:
    """导入任务的调度者。持有进度表，供桥接层查询。"""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self._tasks: dict[str, ImportTask] = {}
        self._lock = threading.RLock()
        # 同一时间只允许一个导入（部署会占用 MT5 目录、并提交审计操作）
        self._active: str = ""

    # ── 任务表 ──

    def task(self, task_id: str) -> ImportTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def active_task(self) -> str:
        with self._lock:
            return self._active

    # ── 同步校验 ──

    def check_dir(self, path: str) -> dict[str, Any]:
        """校验目标目录是不是 MT5 安装目录。

        属于导入流程的一环，同样要求已登录（与 pick-dir / versions 一致）。
        """
        self._bridge.session.check()
        return check_directory(path)

    def versions(self) -> dict[str, Any]:
        """后端可下载的客户端版本。"""
        target = self._bridge.version_target()
        if not target.ready:
            return {
                "ok": False,
                "error": "尚未配置后端地址或节点令牌",
                "versions": [],
                "current": "",
            }
        try:
            items, current = vs.fetch_available_versions(target)
        except vs.VersionServiceError as exc:
            return {"ok": False, "error": str(exc), "versions": [], "current": ""}

        versions = [
            {
                "version": str(i.get("version") or ""),
                "size": int(i.get("size") or 0),
                "sha256": str(i.get("sha256") or ""),
                "notes": str(i.get("notes") or ""),
                "is_current": bool(i.get("is_current")),
            }
            for i in items
            if i.get("version")
        ]
        return {
            "ok": True,
            "error": "",
            "versions": versions,
            "current": current,
        }

    def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        """校验参数、解析账号，返回确认摘要。"""
        session = self._bridge.session
        session.check()

        directory = str(body.get("directory") or "").strip()
        check = check_directory(directory)
        if not check["ok"]:
            return {"ok": False, "error": str(check["message"]), "summary": ""}

        account_raw = str(body.get("account") or "").strip()
        try:
            login, server = resolve_account(directory, account_raw)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "summary": ""}

        target = self._bridge.version_target()
        if not target.ready:
            return {"ok": False, "error": "尚未配置后端地址或节点令牌", "summary": ""}

        request = ImportRequest(
            directory=directory,
            name=str(body.get("name") or "").strip(),
            version=str(body.get("version") or "").strip(),
            sha256=str(body.get("sha256") or "").strip(),
            account_raw=account_raw,
            write_env=bool(body.get("write_env", True)),
            ws_url=ef.ws_url_from_backend_base(target.base),
        )
        if not request.version:
            return {"ok": False, "error": "请先选择客户端版本", "summary": ""}

        info = summarize(request, login=login)
        return {
            "ok": True,
            "error": "",
            "summary": info["text"],
            "login": login,
            "server": server,
            "env_exists": info["env_exists"],
        }

    # ── 异步执行 ──

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        """启动导入任务，立即返回 task_id。"""
        session = self._bridge.session
        session.check()

        with self._lock:
            if self._active:
                return {"ok": False, "error": "上一个导入还在进行中", "task_id": self._active}
            task_id = uuid.uuid4().hex[:12]
            task = ImportTask(task_id=task_id)
            self._tasks[task_id] = task
            self._active = task_id

        try:
            request = self._build_request(body)
        except Exception as exc:  # noqa: BLE001
            self._finish(task_id, error=str(exc))
            return {"ok": False, "error": str(exc), "task_id": task_id}

        thread = threading.Thread(
            target=self._run, args=(task_id, request), name=f"import-{task_id}", daemon=True
        )
        thread.start()
        return {"ok": True, "error": "", "task_id": task_id}

    def _build_request(self, body: dict[str, Any]) -> ImportRequest:
        directory = str(body.get("directory") or "").strip()
        target = self._bridge.version_target()
        return ImportRequest(
            directory=directory,
            name=str(body.get("name") or "").strip(),
            version=str(body.get("version") or "").strip(),
            sha256=str(body.get("sha256") or "").strip(),
            account_raw=str(body.get("account") or "").strip(),
            write_env=bool(body.get("write_env", True)),
            ws_url=ef.ws_url_from_backend_base(target.base),
        )

    def _run(self, task_id: str, request: ImportRequest) -> None:
        task = self.task(task_id)
        if task is None:
            return
        bridge = self._bridge
        session = bridge.session

        def progress(key: str, label: str, pct: float | None, note: str) -> None:
            self._record(task_id, key, label, pct, note)

        try:
            target = bridge.version_target()
            if not target.ready:
                raise ImportError_("尚未配置后端地址或节点令牌")

            def enroll(cfg: InstanceConfig, login: int, server: str) -> None:
                node = session.enrolled(login, cfg.name, server)
                session.apply_node(cfg, node)

            def register(cfg: InstanceConfig) -> None:
                bridge.manager.add(cfg)
                bridge._persist()

            result: ImportResult = run_import(
                request,
                target=target,
                perform=session.perform,
                enroll=enroll,
                stop_for_install=bridge.manager.stop_for_install,
                register=register,
                progress=progress,
            )
            self._finish(task_id, result=result)
        except DashboardAuthError as exc:
            self._finish(task_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self._finish(task_id, error=str(exc) or exc.__class__.__name__)

    # ── 进度记录 ──

    def _record(
        self, task_id: str, key: str, label: str, pct: float | None, note: str
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            existing = next((s for s in task.steps if s["key"] == key), None)
            if existing is None:
                existing = {"key": key, "label": label, "pct": 0.0, "status": "pending"}
                task.steps.append(existing)
            existing["label"] = label
            if pct is not None:
                existing["pct"] = round(float(pct), 4)
            existing["status"] = "active"
            if note:
                stamp = time.strftime("%H:%M:%S")
                task.log.append(f"[{stamp}] {note}")
                # 日志别无限涨
                if len(task.log) > 200:
                    del task.log[:-200]

    def _finish(
        self,
        task_id: str,
        *,
        result: ImportResult | None = None,
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
            if result is not None and result.ok and result.config is not None:
                task.result = {
                    "name": result.config.name,
                    "version": result.version,
                    "mt5_login": result.config.mt5_login,
                    "node_id": result.config.node_id,
                    "env_written": result.env_written,
                    "env_overwritten": result.env_overwritten,
                    "algo_trading_disabled": result.algo_trading_disabled,
                    "message": self._success_message(result),
                }
            if self._active == task_id:
                self._active = ""

    @staticmethod
    def _success_message(result: ImportResult) -> str:
        parts = [f"已导入「{result.config.name}」，客户端 v{result.version}"]
        if result.env_overwritten:
            parts.append(".env 已覆盖")
        elif result.env_written:
            parts.append(".env 已生成")
        if result.algo_trading_disabled:
            parts.append("该终端未开启算法交易")
        return "　".join(parts)
