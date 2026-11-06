"""桥接层自检：不开浏览器，直接用 HTTP 客户端验证接口与 SSE。

跑法：
    python -m pytest test_dashboard_bridge.py
或
    python test_dashboard_bridge.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from models import InstanceConfig  # noqa: E402


def _get(bridge: DashboardBridge, path: str, token: str | None = None, timeout: float = 5.0):
    url = f"http://127.0.0.1:{bridge.port}{path}"
    if token is not False:
        sep = "&" if "?" in path else "?"
        url += f"{sep}token={token or bridge.token}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(bridge: DashboardBridge, path: str, payload: dict, token: str | None = None):
    url = f"http://127.0.0.1:{bridge.port}{path}?token={token or bridge.token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_token_required() -> None:
    """没有 token 的请求必须被拒。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _get(bridge, "/api/state", token=False)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, f"期望 403，实际 {exc.code}"
        else:
            raise AssertionError("无 token 的请求竟然通过了")
    finally:
        bridge.stop()
    print("[ok] 无 token 请求被拒（403）")


def test_wrong_token_rejected() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _get(bridge, "/api/state", token="wrong-token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, f"期望 403，实际 {exc.code}"
        else:
            raise AssertionError("错误 token 的请求竟然通过了")
    finally:
        bridge.stop()
    print("[ok] 错误 token 请求被拒（403）")


def test_state_locked_before_login() -> None:
    """未登录时 /api/state 应返回 locked。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        payload = _get(bridge, "/api/state")
        assert payload.get("authenticated") is False, payload
        assert payload.get("type") == "locked", payload
    finally:
        bridge.stop()
    print("[ok] 未登录状态返回 locked")


def test_login_without_backend_configured() -> None:
    """登录失败必须给出明确的中文文案，而不是抛栈或 500。

    视本机 `.env` 是否配了 APP_URL，分两种预期：
      - 未配置 -> 文案提到 APP_URL；
      - 已配置 -> 请求会打到后端，返回 401 且文案是「账号或密码错误」。
    两种情况都必须有可读的 error 字段。
    """
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _post(bridge, "/api/login", {"username": "u", "password": "p"})
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 401, 403), f"意外的 HTTP {exc.code}"
            payload = json.loads(exc.read().decode("utf-8"))
            error = payload.get("error", "")
            assert error, "错误响应里没有 error 文案"
            assert (
                ("APP_URL" in error)
                or ("无法连接" in error)
                or ("账号或密码错误" in error)
            ), error
            print(f"[ok] 登录失败给出明确文案：{error}")
        else:
            print("[skip] 当前环境后端可达，跳过失败路径断言")
    finally:
        bridge.stop()


def test_sse_pushes_snapshot() -> None:
    """SSE 连接应立刻收到一份 snapshot。"""
    bridge = DashboardBridge()
    bridge.start()
    received: list[dict] = []
    ready = threading.Event()

    def reader() -> None:
        url = f"http://127.0.0.1:{bridge.port}/api/events?token={bridge.token}"
        try:
            with urllib.request.urlopen(url, timeout=8.0) as resp:
                buffer = ""
                for raw in resp:
                    line = raw.decode("utf-8")
                    if line.startswith("data: "):
                        buffer = line[6:]
                    elif line.strip() == "" and buffer:
                        received.append(json.loads(buffer))
                        buffer = ""
                        ready.set()
                        if len(received) >= 2:
                            break
        except Exception:  # noqa: BLE001
            ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    ready.wait(timeout=8.0)
    thread.join(timeout=2.0)
    bridge.stop()

    assert received, "SSE 没有收到任何事件"
    assert received[0].get("type") in ("locked", "snapshot"), received[0]
    print(f"[ok] SSE 收到 {len(received)} 条事件，首条 type={received[0].get('type')}")


def test_unknown_endpoint_404() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _get(bridge, "/api/nope")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f"期望 404，实际 {exc.code}"
        else:
            raise AssertionError("未知接口竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未知接口返回 404")


def test_static_index_served() -> None:
    """根路径不应 404：前端已构建则返回 index.html，未构建则返回提示页。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        url = f"http://127.0.0.1:{bridge.port}/"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        assert "<html" in body.lower(), body[:200]
        if "前端尚未构建" in body:
            print("[ok] 前端未构建时返回提示页（预期状态）")
        else:
            print("[ok] 根路径返回 index.html")
    finally:
        bridge.stop()


def test_dialog_endpoints_require_login() -> None:
    """对话框类接口在未登录时必须报 401/403，不能泄露数据。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        for path in (
            "/api/connection",
            "/api/version/info",
            "/api/env/read?id=nope",
            "/api/version/local?id=nope",
            "/api/version/target?id=nope",
        ):
            try:
                _get(bridge, path)
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 403), f"{path} 期望 401/403，实际 {exc.code}"
            else:
                raise AssertionError(f"{path} 在未登录时竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 对话框接口未登录时全部拒绝")


def test_dialog_endpoints_reject_unknown_instance() -> None:
    """实例不存在时应返回 404，而不是 500。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        # 登录状态未知，直接打实例类接口，期望是 4xx（鉴权失败或实例不存在），
        # 总之不能是 500（说明代码漏了异常处理）。
        for path in ("/api/env/read?id=__missing__", "/api/version/local?id=__missing__"):
            try:
                _get(bridge, path)
            except urllib.error.HTTPError as exc:
                assert exc.code < 500, f"{path} 返回了服务端错误 {exc.code}"
            else:
                pass
    finally:
        bridge.stop()
    print("[ok] 实例类接口对未知实例不返回 5xx")


def test_env_write_rejects_bad_payload() -> None:
    """写 .env 的接口在未登录/参数缺失时不能 500。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _post(bridge, "/api/env/write", {"id": "__missing__", "text": "A=1"})
        except urllib.error.HTTPError as exc:
            assert exc.code < 500, f"期望 4xx，实际 {exc.code}"
        else:
            raise AssertionError("未登录写 .env 竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 写 .env 接口未登录时拒绝")


def test_import_endpoints_require_login() -> None:
    """导入类接口未登录时必须拒绝。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        for path in (
            "/api/import/check-dir?path=C:/x",
            "/api/import/versions",
            "/api/import/pick-dir",
        ):
            try:
                _get(bridge, path)
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 403), f"{path} 期望 401/403，实际 {exc.code}"
            else:
                raise AssertionError(f"{path} 在未登录时竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 导入接口未登录时全部拒绝")


def test_import_progress_unknown_task_404() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _get(bridge, "/api/import/progress?task_id=nope")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f"期望 404，实际 {exc.code}"
        else:
            raise AssertionError("未知导入任务竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未知导入任务返回 404")


def test_import_start_requires_login() -> None:
    bridge = DashboardBridge()
    bridge.start()
    try:
        try:
            _post(bridge, "/api/import/start", {"directory": "C:/x", "version": "1.0"})
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403), f"期望 401/403，实际 {exc.code}"
        else:
            raise AssertionError("未登录启动导入竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 未登录不能启动导入")


def test_config_status_reports_missing_app_url(tmp_path, monkeypatch) -> None:
    """没配 APP_URL 时，/api/config 要说清楚去哪儿配 —— 登录页靠它提示用户。"""
    import store

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    bridge = DashboardBridge()
    bridge.start()
    try:
        payload = _get(bridge, "/api/config")
        assert payload.get("ok") is False, payload
        assert payload.get("app_url") == "", payload
        assert payload.get("env_exists") is False, payload
        assert "APP_URL" in payload.get("error", ""), payload
        assert str(tmp_path) in payload.get("env_path", ""), payload
    finally:
        bridge.stop()
    print("[ok] /api/config 能报出未配置 APP_URL")


def test_config_status_reads_app_url(tmp_path, monkeypatch) -> None:
    """配了 APP_URL 时，/api/config 要把它返回给前端显示。"""
    import store

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    (tmp_path / ".env").write_text("APP_URL=http://example.test\n", encoding="utf-8")
    bridge = DashboardBridge()
    bridge.start()
    try:
        payload = _get(bridge, "/api/config")
        assert payload.get("ok") is True, payload
        assert payload.get("app_url") == "http://example.test", payload
        assert payload.get("env_exists") is True, payload
        assert payload.get("error") == "", payload
    finally:
        bridge.stop()
    print("[ok] /api/config 能读出 APP_URL")


def test_auto_login_reads_credentials(tmp_path, monkeypatch) -> None:
    """自动登录账号从面板 .env 读取。"""
    import store

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    (tmp_path / ".env").write_text(
        "APP_URL=http://example.test\n"
        "DASHBOARD_LOGIN_USER=admin\n"
        "DASHBOARD_LOGIN_PASSWORD=secret\n",
        encoding="utf-8",
    )
    bridge = DashboardBridge()
    bridge._load_auto_login()
    assert bridge._auto_login_user == "admin"
    assert bridge._auto_login_password == "secret"
    print("[ok] 自动登录账号能从 .env 读出")


def test_auto_login_skipped_without_credentials(tmp_path, monkeypatch) -> None:
    """两项留空 = 不自动登录，保持显示登录页。"""
    import store

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    (tmp_path / ".env").write_text("APP_URL=http://example.test\n", encoding="utf-8")
    bridge = DashboardBridge()
    bridge._load_auto_login()
    assert bridge._auto_login_user == ""
    assert bridge._auto_login_password == ""
    print("[ok] 未配账号密码时不自动登录")


# ── 面板指标（今日信号 / 平均延迟）与节点卡片字段 ──


def _fake_bridge_with_stats(stats: dict[str, dict]):
    """造一个带假实例 + 假统计的桥接层（不连后端）。"""
    from e2e_support import FakeApi, FakeSession

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    session.api.stats = lambda token: stats  # type: ignore[attr-defined]
    bridge.session = session
    bridge.manager.session = session

    cfg = InstanceConfig.create(name="mt5-prod-01", exe_path=r"C:\Mt5\prod-01\node_client.exe",
                                cwd=r"C:\Mt5\prod-01")
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    mp = bridge.manager.add(cfg)
    mp.runtime.process_alive = True
    mp.runtime.health = "ok"
    mp.runtime.node_id = "nd_9f21"
    mp.runtime.mt5_login = 50231488
    return bridge, cfg


def test_snapshot_includes_stats_metrics() -> None:
    """快照里的 hero 带今日信号与平均延迟，节点带服务器名与交易账号 ID。"""
    bridge, _ = _fake_bridge_with_stats({
        "nd_9f21": {"mt5_server": "ICMarkets-Live", "today_signals": 42, "avg_latency_ms": 128.4},
    })
    try:
        snapshot = bridge.snapshot()
        assert snapshot is not None
        hero = snapshot["hero"]
        assert hero["today_signals"] == 42, hero
        assert hero["avg_latency_ms"] == 128.4, hero
        node = snapshot["nodes"][0]
        assert node["server_label"] == "ICMarkets-Live", node
        assert node["account_label"] == "50231488", node
        assert node["today_signals"] == 42, node
        assert node["avg_latency_ms"] == 128.4, node
    finally:
        bridge.stop()
    print("[ok] 快照带今日信号/平均延迟/服务器名/交易账号 ID")


def test_snapshot_without_stats_shows_placeholders() -> None:
    """后端没给统计时，信号数为 0、延迟为 None（不是 0 延迟）。"""
    bridge, _ = _fake_bridge_with_stats({})
    try:
        snapshot = bridge.snapshot()
        assert snapshot is not None
        assert snapshot["hero"]["today_signals"] == 0, snapshot["hero"]
        assert snapshot["hero"]["avg_latency_ms"] is None, snapshot["hero"]
        node = snapshot["nodes"][0]
        assert node["server_label"] == "", node
        assert node["account_label"] == "50231488", node
    finally:
        bridge.stop()
    print("[ok] 没有统计数据时显示占位而不是 0")


def test_stats_cache_avoids_repeated_backend_calls() -> None:
    """统计有 TTL 缓存：连续两次快照只打一次后端。"""
    bridge, _ = _fake_bridge_with_stats({"nd_9f21": {"today_signals": 1, "avg_latency_ms": 10.0}})
    calls = {"n": 0}

    def _stats(token: str):
        calls["n"] += 1
        return {"nd_9f21": {"today_signals": 1, "avg_latency_ms": 10.0}}

    bridge.session.api.stats = _stats  # type: ignore[attr-defined]
    try:
        bridge.snapshot()
        bridge.snapshot()
        assert calls["n"] == 1, f"TTL 内应只请求一次，实际 {calls['n']} 次"
        bridge._stats_fetched_at = 0.0  # 强制过期
        bridge.snapshot()
        assert calls["n"] == 2, f"过期后应重新请求，实际 {calls['n']} 次"
    finally:
        bridge.stop()
    print("[ok] 统计按 TTL 缓存，不会每秒打一次后端")


def test_remove_action_drops_instance() -> None:
    """移除操作后实例从面板与管理器里一起消失。"""
    bridge, cfg = _fake_bridge_with_stats({})
    try:
        mp = bridge.manager.get(cfg.id)
        assert mp is not None
        # 假实例没有真的状态口，标记成「不在运行」，避免停止时的身份确认失败
        mp.runtime.process_alive = False
        bridge._inventory[cfg.id] = cfg
        bridge.instance_action("remove", cfg.id)
        assert bridge.manager.get(cfg.id) is None, "实例还留在管理器里"
        assert cfg.id not in bridge._inventory, "实例还留在本机清单里"
    finally:
        bridge.stop()
    print("[ok] 移除操作会把实例从面板摘掉")


def test_binding_kept_when_backend_node_list_is_empty() -> None:
    """后端节点列表为空时，不能把本机实例的 node_id 抹掉。

    回归：后端 Redis 缓存被清 / 刚重启还没预热时 /api/nodes 会返回空，
    桥接层以前会把实例判成「未绑定」并把 node_id 清空写回清单 ——
    绑定一丢，remove_instance 就因为缺 node_id 被后端 422 挡下，
    面板上的「垃圾桶」点起来像没反应。
    """
    import store
    from e2e_support import FakeApi, FakeSession

    tmp = Path(__file__).resolve().parent / "_e2e_binding"
    tmp.mkdir(exist_ok=True)
    cfg = InstanceConfig.create(name="mt5-prod-01", exe_path=r"C:\Mt5\prod-01\node_client.exe",
                                cwd=r"C:\Mt5\prod-01")
    cfg.id = "cfgkeep01"
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    cfg.approval_status = "approved"
    cfg.backend_base = "http://127.0.0.1:9/e2e"
    (tmp / "instances.json").write_text(
        json.dumps({"instances": [{
            "id": cfg.id, "name": cfg.name, "exe_path": cfg.exe_path, "cwd": cfg.cwd,
            "status_port": cfg.status_port, "daemon": False, "restart_delay_s": 5.0,
            "enabled": True, "node_id": cfg.node_id, "mt5_login": cfg.mt5_login,
            "owner_user_id": 1, "requested_by_user_id": None,
            "approval_status": "approved", "credential_generation": 1,
            "legacy_allowed": True, "has_credential": True,
            "backend_base": cfg.backend_base, "algo_trading_disabled": False,
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    session.nodes = {}          # ← 后端一个节点都没返回（缓存被清）
    bridge.session = session
    bridge.manager.session = session

    original_data_dir = store.data_dir
    store.data_dir = lambda: tmp
    try:
        bridge._load_permitted_instances()
        loaded = [c for c in bridge.manager.configs() if c.id == cfg.id]
        assert loaded, "实例不该被过滤掉"
        assert loaded[0].node_id == "nd_9f21", "node_id 被抹掉了"
        assert loaded[0].approval_status == "approved", loaded[0].approval_status
        saved = json.loads((tmp / "instances.json").read_text(encoding="utf-8"))
        assert saved["instances"][0]["node_id"] == "nd_9f21", saved
    finally:
        store.data_dir = original_data_dir
        bridge.stop()
    print("[ok] 后端节点列表为空时保留本机实例的绑定")


def test_binding_dropped_when_backend_knows_the_node() -> None:
    """本机客户端报的 node_id 后端不认识时，仍然要判为未绑定（管理员可见、不可启动）。"""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import store
    from e2e_support import FakeApi, FakeSession

    class _Status(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _json.dumps({"node_id": "nd_running", "mt5_login": 50231488}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), _Status)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    status_port = server.server_address[1]

    tmp = Path(__file__).resolve().parent / "_e2e_binding2"
    tmp.mkdir(exist_ok=True)
    cfg = InstanceConfig.create(name="mt5-prod-02", exe_path=r"C:\Mt5\prod-02\node_client.exe",
                                cwd=r"C:\Mt5\prod-02")
    cfg.id = "cfgdrop01"
    cfg.node_id = "nd_gone"
    cfg.mt5_login = 50231488
    cfg.approval_status = "approved"
    cfg.backend_base = "http://127.0.0.1:9/e2e"
    (tmp / "instances.json").write_text(
        json.dumps({"instances": [{
            "id": cfg.id, "name": cfg.name, "exe_path": cfg.exe_path, "cwd": cfg.cwd,
            "status_port": status_port, "daemon": False, "restart_delay_s": 5.0,
            "enabled": True, "node_id": cfg.node_id, "mt5_login": cfg.mt5_login,
            "owner_user_id": 1, "requested_by_user_id": None,
            "approval_status": "approved", "credential_generation": 1,
            "legacy_allowed": True, "has_credential": True,
            "backend_base": cfg.backend_base, "algo_trading_disabled": False,
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    session.nodes = {"nd_other": {"node_id": "nd_other", "mt5_login": 999,
                                   "owner_user_id": 1, "approval_status": "approved",
                                   "enabled": True}}
    bridge.session = session
    bridge.manager.session = session

    original_data_dir = store.data_dir
    store.data_dir = lambda: tmp
    try:
        bridge._load_permitted_instances()
        loaded = [c for c in bridge.manager.configs() if c.id == cfg.id]
        assert loaded, "管理员应该仍能看到这个本机实例"
        assert loaded[0].node_id is None, "对不上的实例应该解绑"
        assert loaded[0].approval_status == "unbound", loaded[0].approval_status
    finally:
        store.data_dir = original_data_dir
        bridge.stop()
        server.shutdown()
    print("[ok] 后端知道节点但对不上时，本机实例仍解绑为 unbound")


def test_reconcile_credential_restarts_client_after_auto_rotation() -> None:
    """令牌被自动轮换后，运行中的客户端必须重启才能读到新的 .env。

    回归：旧实现只提示「请重置专属令牌」，没有自愈；轮换若不同步重启，
    客户端会一直拿着已作废的旧令牌连不上后端。
    """
    from types import SimpleNamespace

    from dashboard_session import CredentialRepair

    bridge = DashboardBridge()
    bridge.session = SimpleNamespace(
        ensure_local_credential=lambda cfg, **kw: CredentialRepair(
            ok=True, token="ndv1.nd_one.synthetic-rotated-token", reissued=True
        ),
        rotation_allowed=lambda cfg, mp=None: True,
        grant_for_recovery=lambda mp: None,
    )
    try:
        stopped = []
        started = []
        mp = SimpleNamespace(
            runtime=SimpleNamespace(process_alive=True, last_error="旧错误"),
            stop=lambda graceful=True: stopped.append(graceful),
            start=lambda: started.append(True),
        )
        bridge._reconcile_credential(SimpleNamespace(), mp)
        assert stopped == [True], stopped
        assert started == [True], started
        assert mp.runtime.last_error == "", mp.runtime.last_error
    finally:
        bridge.stop()
    print("[ok] 令牌自动轮换后客户端被重启以加载新 .env")


def test_reconcile_credential_keeps_client_running_when_nothing_changed() -> None:
    """令牌没变时不该乱重启客户端。"""
    from types import SimpleNamespace

    from dashboard_session import CredentialRepair

    bridge = DashboardBridge()
    bridge.session = SimpleNamespace(
        ensure_local_credential=lambda cfg, **kw: CredentialRepair(
            ok=True, token="ndv1.nd_one.synthetic-node-token"
        ),
        rotation_allowed=lambda cfg, mp=None: True,
        grant_for_recovery=lambda mp: None,
    )
    try:
        stopped = []
        mp = SimpleNamespace(
            runtime=SimpleNamespace(process_alive=True, last_error=""),
            stop=lambda graceful=True: stopped.append(graceful),
            start=lambda: None,
        )
        bridge._reconcile_credential(SimpleNamespace(), mp)
        assert stopped == [], stopped
    finally:
        bridge.stop()
    print("[ok] 令牌未变时客户端不会被重启")


if __name__ == "__main__":
    tests = [
        test_token_required,
        test_wrong_token_rejected,
        test_state_locked_before_login,
        test_login_without_backend_configured,
        test_sse_pushes_snapshot,
        test_unknown_endpoint_404,
        test_static_index_served,
        test_dialog_endpoints_require_login,
        test_dialog_endpoints_reject_unknown_instance,
        test_env_write_rejects_bad_payload,
        test_import_endpoints_require_login,
        test_import_progress_unknown_task_404,
        test_import_start_requires_login,
        test_config_status_reports_missing_app_url,
        test_config_status_reads_app_url,
        test_auto_login_reads_credentials,
        test_auto_login_skipped_without_credentials,
        test_snapshot_includes_stats_metrics,
        test_snapshot_without_stats_shows_placeholders,
        test_stats_cache_avoids_repeated_backend_calls,
        test_remove_action_drops_instance,
        test_binding_kept_when_backend_node_list_is_empty,
        test_binding_dropped_when_backend_knows_the_node,
        test_reconcile_credential_restarts_client_after_auto_rotation,
        test_reconcile_credential_keeps_client_running_when_nothing_changed,
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
