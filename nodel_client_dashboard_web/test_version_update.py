"""版本更新功能的自检：桥接层接口、版本判定与任务调度。

跑法：
    python -m pytest test_version_update.py
或
    python test_version_update.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from models import InstanceConfig  # noqa: E402


def _get(bridge: DashboardBridge, path: str, timeout: float = 5.0):
    url = f"http://127.0.0.1:{bridge.port}{path}"
    sep = "&" if "?" in path else "?"
    url += f"{sep}token={bridge.token}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(bridge: DashboardBridge, path: str, payload: dict):
    url = f"http://127.0.0.1:{bridge.port}{path}?token={bridge.token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fake_bridge(tmp_path: Path):
    """造一个已登录的桥接层，带一个装着旧客户端的实例。"""
    from e2e_support import FakeApi, FakeSession

    instance_dir = tmp_path / "prod-01"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "node_client.exe").write_bytes(b"stub")
    (instance_dir / "version.txt").write_text("1.0.0-20260101000000\n", encoding="utf-8")

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    bridge.session = session
    bridge.manager.session = session

    cfg = InstanceConfig.create(
        name="mt5-prod-01",
        exe_path=str(instance_dir / "node_client.exe"),
        cwd=str(instance_dir),
    )
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    bridge.manager.add(cfg)
    return bridge, cfg


# ── 接口鉴权 ──


def test_version_update_endpoints_require_login() -> None:
    """版本更新接口未登录时必须拒绝。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        for path in (
            "/api/version-update/instances",
            "/api/version-update/detect",
        ):
            try:
                _get(bridge, path)
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 403), f"{path} 期望 401/403，实际 {exc.code}"
            else:
                raise AssertionError(f"{path} 在未登录时竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 版本更新查询接口未登录时全部拒绝")


def test_version_update_start_requires_login() -> None:
    """未登录不能启动更新。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _post(bridge, "/api/version-update/start", {"version": "1.0.0", "ids": ["x"]})
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403), f"期望 401/403，实际 {exc.code}"
        else:
            raise AssertionError("未登录启动更新竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未登录不能启动版本更新")


def test_version_update_progress_unknown_task_404() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _get(bridge, "/api/version-update/progress?task_id=nope")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f"期望 404，实际 {exc.code}"
        else:
            raise AssertionError("未知版本任务竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未知版本任务返回 404")


# ── 版本判定 ──


def test_prepare_classifies_upgrade_and_downgrade(tmp_path) -> None:
    """更新前的判定要区分「需更新 / 将降级 / 已最新」。"""
    bridge, cfg = _fake_bridge(tmp_path)
    try:
        res = bridge._version_update.prepare({"version": "2.0.0-20260102000000", "ids": [cfg.id]})
        assert res["ok"] is True, res
        assert res["rows"][0]["kind"] == "upgrade", res["rows"]
        assert res["rows"][0]["label"] == "需更新", res["rows"]
        assert res["downgrades"] == [], res
        assert "2.0.0-20260102000000" in res["summary"], res["summary"]

        back = bridge._version_update.prepare({"version": "0.9.0-20260101000000", "ids": [cfg.id]})
        assert back["rows"][0]["kind"] == "downgrade", back["rows"]
        assert back["downgrades"] == [cfg.name], back
    finally:
        bridge.stop()
    print("[ok] 更新前的版本判定区分升级与降级")


def test_prepare_rejects_empty_selection(tmp_path) -> None:
    """没勾实例就点更新必须被拦下。"""
    bridge, _ = _fake_bridge(tmp_path)
    try:
        res = bridge._version_update.prepare({"version": "2.0.0", "ids": []})
        assert res["ok"] is False, res
        assert "勾选" in res["error"], res
    finally:
        bridge.stop()
    print("[ok] 未勾选实例时更新被拦下")


def test_prepare_rejects_empty_version(tmp_path) -> None:
    bridge, cfg = _fake_bridge(tmp_path)
    try:
        res = bridge._version_update.prepare({"version": "", "ids": [cfg.id]})
        assert res["ok"] is False, res
        assert res["error"], res
    finally:
        bridge.stop()
    print("[ok] 目标版本为空时更新被拦下")


def test_instances_report_local_version(tmp_path) -> None:
    """实例清单要报出本机客户端版本与可回滚备份。"""
    bridge, cfg = _fake_bridge(tmp_path)
    try:
        res = bridge._version_update.instances()
        assert res["ok"] is True, res
        assert len(res["instances"]) == 1, res
        item = res["instances"][0]
        assert item["id"] == cfg.id, item
        assert item["version"] == "1.0.0-20260101000000", item
        assert item["backups"] == [], item
    finally:
        bridge.stop()
    print("[ok] 实例清单能报出本机客户端版本")


# ── 回滚 ──


def test_rollback_rejects_instances_without_backup(tmp_path) -> None:
    """没有本机备份的实例不能回滚 —— 否则整个批次必然失败。"""
    bridge, cfg = _fake_bridge(tmp_path)
    try:
        res = bridge._version_update.start_rollback({"version": "0.9.0-20260101000000", "ids": [cfg.id]})
        assert res["ok"] is False, res
        assert "备份" in res["error"], res
    finally:
        bridge.stop()
    print("[ok] 没有本机备份时回滚被拦下")


def test_rollback_start_requires_login() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _post(bridge, "/api/version-update/rollback", {"version": "1.0.0", "ids": ["x"]})
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403), f"期望 401/403，实际 {exc.code}"
        else:
            raise AssertionError("未登录启动回滚竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未登录不能启动回滚")


# ── 徽标 ──


def test_badge_is_zero_blocking(tmp_path) -> None:
    """徽标读缓存，绝不能在快照里发起网络请求。

    快照每 1 秒推一次；徽标若在里面同步打后端，会把 SSE 推送拖慢。
    """
    bridge, cfg = _fake_bridge(tmp_path)
    calls = {"n": 0}

    def _boom(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("徽标不允许发起网络请求")

    import version_service as vs

    original = vs.fetch_current_version
    vs.fetch_current_version = _boom
    try:
        for _ in range(3):
            snapshot = bridge.snapshot()
            assert snapshot is not None
            assert "update" in snapshot, snapshot.keys()
            assert snapshot["update"]["pending"] == 0, snapshot["update"]
        assert calls["n"] == 0, f"徽标发起了 {calls['n']} 次网络请求"
    finally:
        vs.fetch_current_version = original
        bridge.stop()
    print("[ok] 徽标只读缓存，不在快照路径上发网络请求")


def test_badge_pending_counts_outdated_instances(tmp_path) -> None:
    """版本低于发布版本的实例要计入待更新数。"""
    bridge, cfg = _fake_bridge(tmp_path)
    try:
        bridge._version_update._published = "2.0.0-20260102000000"
        bridge._version_update._published_at = 1e12  # 让 TTL 永不过期
        bridge._version_update.invalidate_badge()
        badge = bridge._version_update.refresh_badge()
        assert badge["published"] == "2.0.0-20260102000000", badge
        assert badge["pending"] == 1, badge
    finally:
        bridge.stop()
    print("[ok] 待更新数能统计出落后于发布版本的实例")


# ── 前端源码静态检查 ──


def test_modal_mode_switch_buttons_are_clickable() -> None:
    """「从后端更新 / 本机回滚」两个模式按钮必须始终可点。

    之前用 `:disabled="rollbackMode"` 表达「当前选中」，结果 disabled 的
    那个按钮带 pointer-events:none，用户根本点不到另一个模式 —— 回滚路径
    直接不可达。这里做源码级回归防护。
    """
    modal = (
        Path(__file__).resolve().parent.parent
        / "frontend-console"
        / "src"
        / "components"
        / "VersionUpdateModal.vue"
    )
    assert modal.is_file(), f"找不到 {modal}"
    text = modal.read_text(encoding="utf-8")

    row_start = text.find("vu-mode-row")
    assert row_start != -1, "弹窗里没有 vu-mode-row（模式切换区）"
    block = text[row_start : row_start + 1200]

    assert ":disabled" not in block, "模式切换按钮不能再用 disabled 表达选中态"
    assert 'aria-pressed="!rollbackMode"' in block, "缺少「从后端更新」的 aria-pressed"
    assert 'aria-pressed="rollbackMode"' in block, "缺少「本机回滚」的 aria-pressed"
    print("[ok] 模式切换按钮始终可点，选中态用 aria-pressed 表达")


if __name__ == "__main__":
    tests = [
        test_version_update_endpoints_require_login,
        test_version_update_start_requires_login,
        test_version_update_progress_unknown_task_404,
        test_prepare_classifies_upgrade_and_downgrade,
        test_prepare_rejects_empty_selection,
        test_prepare_rejects_empty_version,
        test_instances_report_local_version,
        test_rollback_rejects_instances_without_backup,
        test_rollback_start_requires_login,
        test_badge_is_zero_blocking,
        test_badge_pending_counts_outdated_instances,
        test_modal_mode_switch_buttons_are_clickable,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
    print("-" * 50)
    print(f"失败 {failures} / {len(tests)}")
    sys.exit(1 if failures else 0)
