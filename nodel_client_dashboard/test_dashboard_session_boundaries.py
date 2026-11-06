"""纯会话、凭证与审计边界回归；不访问网络、不启动真实客户端。"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from auth_service import DashboardAuthError
import dashboard_session as sessions
from models import InstanceConfig
from process_manager import ManagedProcess, ProcessManager
from session_ui import requires_session


class FakeAPI:
    def __init__(self, base="https://first.example.test", *, user_id=7):
        self.base = base
        self.principal = {"user_id": user_id, "is_admin": False, "menus": ["nodes"]}
        self.items = [{
            "node_id": "nd_one", "mt5_login": 12345, "owner_user_id": user_id,
            "approval_status": "approved", "enabled": True, "credential_generation": 1,
            "has_credential": False, "legacy_allowed": False,
        }]
        self.authorize = MagicMock(return_value={"operation_id": 101})
        self.result = MagicMock(return_value={"ok": True})
        self.credential = MagicMock(return_value={
            "node_id": "nd_one", "mt5_login": 12345, "generation": 1,
            "valid": True, "token": "ndv1.nd_one.synthetic-node-token",
        })
        self.daemon_grant = MagicMock(return_value=self.grant_data())
        self.check_grant = MagicMock(return_value={
            "ok": True, "node_id": "nd_one", "mt5_login": 12345, "generation": 1,
        })
        self.daemon_event = MagicMock(return_value={"ok": True})
        self.replay_daemon_event = MagicMock(return_value={"ok": True})

    def me(self, _token):
        return deepcopy(self.principal)

    def nodes(self, _token):
        return deepcopy(self.items)

    def grant_data(self):
        return {
            "grant_token": "synthetic-memory-only-daemon-token", "node_id": "nd_one",
            "mt5_login": 12345, "generation": 1, "grant_operation_id": 101,
            "actor_user_id": self.principal["user_id"],
        }


@pytest.fixture
def queues(monkeypatch):
    state = SimpleNamespace(results=[], events=[])
    monkeypatch.setattr(sessions, "load_audit_queue", lambda: deepcopy(state.results))
    monkeypatch.setattr(sessions, "save_audit_queue", lambda records: setattr(state, "results", deepcopy(records)))
    monkeypatch.setattr(sessions, "load_daemon_events", lambda: deepcopy(state.events))
    monkeypatch.setattr(sessions, "save_daemon_events", lambda records: setattr(state, "events", deepcopy(records)))
    return state


@pytest.fixture
def login(queues):
    api = FakeAPI()
    session = sessions.DashboardSession()
    session.establish(api, "synthetic-first-access-token")
    return session, api


def config(session, root, node_id="nd_one"):
    root.mkdir(parents=True, exist_ok=True)
    cfg = InstanceConfig.create(exe_path=str(root / "node_client.exe"), cwd=str(root))
    session.apply_node(cfg, session.nodes[node_id])
    return cfg


@pytest.mark.parametrize("status", [0, 401, 403, 500])
def test_intent_audit_failure_has_no_local_side_effect(login, tmp_path, status):
    session, api = login
    cfg = config(session, tmp_path)
    api.authorize.side_effect = DashboardAuthError("授权审计失败", status=status)
    local = MagicMock()
    with pytest.raises(DashboardAuthError):
        session.perform("edit_env", cfg, local)
    local.assert_not_called()
    api.result.assert_not_called()
    if status in (0, 401, 500):
        assert not session.active


def test_result_audit_failure_persists_and_locks_new_operations(login, queues, tmp_path):
    session, api = login
    cfg = config(session, tmp_path)
    order = []
    api.authorize.side_effect = lambda *_args: order.append("intent") or {"operation_id": 101}
    def fail_result(*_args):
        order.append("result")
        raise DashboardAuthError("结果审计断网")
    api.result.side_effect = fail_result
    assert session.perform("edit_env", cfg, lambda: order.append("local") or "saved") == "saved"
    assert order == ["intent", "local", "result"]
    assert not session.active
    assert len(queues.results) == 1
    assert queues.results[0]["backend_base"] == api.base
    assert queues.results[0]["actor_user_id"] == 7
    assert queues.results[0]["operation_id"] == 101
    assert queues.results[0]["result"] == "ok"
    assert "synthetic-first-access-token" not in json.dumps(queues.results)
    another = MagicMock()
    with pytest.raises(DashboardAuthError):
        session.perform("edit_env", cfg, another)
    another.assert_not_called()
    api.result.side_effect = None
    session.establish(api, "synthetic-relogin-access-token")
    assert session.active and not queues.results and not session.pending_results


def test_action_result_queue_never_replays_to_another_backend_with_same_ids(login, queues, tmp_path):
    session, first = login
    cfg = config(session, tmp_path)
    first.result.side_effect = DashboardAuthError("断网")
    session.perform("edit_env", cfg, lambda: None)
    second = FakeAPI("https://second.example.test")
    try:
        session.establish(second, "synthetic-second-access-token")
    except DashboardAuthError:
        pass
    second.result.assert_not_called()
    assert len(queues.results) == 1
    first.result.side_effect = None
    session.establish(first, "synthetic-relogin-access-token")
    assert not queues.results
    assert first.result.call_args.args[0] == "synthetic-relogin-access-token"


def offline_grant(api):
    api.check_grant.side_effect = DashboardAuthError("离线复核")
    api.daemon_event.side_effect = DashboardAuthError("离线事件")
    return sessions.DaemonGrant(api, api.grant_data())


def test_offline_guard_events_persist_without_tokens_and_flush_on_healthy_check(login, queues):
    _session, api = login
    grant = offline_grant(api)
    restarted = MagicMock()
    assert grant.restart(restarted)
    restarted.assert_called_once()
    assert [record["phase"] for record in queues.events] == ["intent", "result"]
    assert all(record["backend_base"] == api.base for record in queues.events)
    serialized = json.dumps(queues.events)
    assert grant.token not in serialized
    assert "synthetic-first-access-token" not in serialized
    api.check_grant.side_effect = None
    api.daemon_event.side_effect = None
    api.daemon_event.reset_mock()
    assert grant.check()
    assert [call.args[1]["phase"] for call in api.daemon_event.call_args_list] == ["intent", "result"]
    assert not queues.events


def test_daemon_event_queue_never_replays_to_another_backend_with_same_ids(login, queues):
    session, first = login
    grant = offline_grant(first)
    assert grant.restart(lambda: None)
    session.lock("退出登录", clear_token=True)
    second = FakeAPI("https://second.example.test")
    try:
        session.establish(second, "synthetic-second-access-token")
    except DashboardAuthError:
        pass
    second.replay_daemon_event.assert_not_called()
    assert len(queues.events) == 2
    session.establish(first, "synthetic-relogin-access-token")
    assert first.replay_daemon_event.call_count == 2
    assert not queues.events
    for call in first.replay_daemon_event.call_args_list:
        assert "actor_user_id" not in call.args[1] and "backend_base" not in call.args[1]


def test_live_event_flush_preserves_equal_event_ids_from_another_backend(login, queues):
    _session, api = login
    grant = offline_grant(api)
    assert grant.restart(lambda: None)
    foreign = [{**record, "backend_base": "https://second.example.test"} for record in queues.events]
    queues.events.extend(foreign)
    api.check_grant.side_effect = None
    api.daemon_event.side_effect = None
    grant.flush()
    assert queues.events == foreign


@pytest.mark.parametrize("queue_kind", ["results", "events"])
def test_legacy_queue_without_backend_identity_cannot_be_replayed(queues, queue_kind):
    record = {"operation_id": 101, "grant_operation_id": 101, "actor_user_id": 7,
              "request_id": "synthetic-request", "phase": "result", "result": "ok", "params": {}}
    setattr(queues, queue_kind, [record])
    api = FakeAPI()
    session = sessions.DashboardSession()
    try:
        session.establish(api, "synthetic-access-token")
    except DashboardAuthError:
        pass
    api.result.assert_not_called()
    api.replay_daemon_event.assert_not_called()
    assert getattr(queues, queue_kind) == [record]


def test_old_worker_stops_before_next_node_after_logout_and_relogin(login, tmp_path):
    session, api = login
    second = {**api.items[0], "node_id": "nd_two", "mt5_login": 23456}
    api.items.append(second)
    session.review()
    first_cfg, second_cfg = config(session, tmp_path / "one"), config(session, tmp_path / "two", "nd_two")
    manager = ProcessManager(session=session)
    first_mp, second_mp = ManagedProcess(first_cfg), ManagedProcess(second_cfg)
    first_mp.runtime.process_alive = second_mp.runtime.process_alive = True
    manager._items = {first_cfg.id: first_mp, second_cfg.id: second_mp}
    second_mp.stop = MagicMock()
    def first_stop(**_kwargs):
        session.lock("退出登录", clear_token=True)
        session.establish(api, "synthetic-new-session-token")
    first_mp.stop = MagicMock(side_effect=first_stop)
    original = session.generation
    session._local.generation = original
    try:
        results = manager.stop_all()
    finally:
        session._local.generation = None
    first_mp.stop.assert_called_once()
    second_mp.stop.assert_not_called()
    assert results[1][2] is False
    assert session.active and session.generation != original


def test_prepare_start_cannot_write_after_credential_response_changes_session(login, tmp_path):
    session, api = login
    cfg = config(session, tmp_path)
    env = tmp_path / ".env"
    initial = "WATCH_SYMBOLS=EURUSD\n"
    env.write_text(initial, encoding="utf-8")
    issued = api.credential.return_value
    def switch_session(*_args):
        session.lock("退出登录", clear_token=True)
        session.establish(api, "synthetic-new-session-token")
        return issued
    api.credential.side_effect = switch_session
    session._local.generation = session.generation
    try:
        with pytest.raises(DashboardAuthError):
            session.prepare_start(SimpleNamespace(cfg=cfg, daemon_grant=None))
    finally:
        session._local.generation = None
    assert env.read_text(encoding="utf-8") == initial
    api.daemon_grant.assert_not_called()


def test_session_action_pins_generation_for_nested_local_continuations(login, tmp_path):
    session, api = login
    cfg = config(session, tmp_path)
    later = MagicMock()
    def interrupted():
        session.lock("退出登录", clear_token=True)
        session.establish(api, "synthetic-new-session-token")
        session.perform("clear_log", cfg, later)
    with pytest.raises(DashboardAuthError):
        session.perform("edit_env", cfg, interrupted)
    later.assert_not_called()


def test_session_decorator_pins_generation_during_modal_continuation(login, tmp_path, monkeypatch):
    session, api = login
    cfg = config(session, tmp_path)
    owner = SimpleNamespace(session=session)
    later = MagicMock()
    monkeypatch.setattr("session_ui.messagebox.showerror", lambda *_args, **_kwargs: None)
    @requires_session
    def modal_continuation(_self):
        session.lock("退出登录", clear_token=True)
        session.establish(api, "synthetic-new-session-token")
        session.perform("edit_env", cfg, later)
    modal_continuation(owner)
    later.assert_not_called()


def test_pending_node_auto_issues_only_after_approval_and_enable(login, tmp_path):
    session, api = login
    api.items[0].update({"approval_status": "pending", "enabled": False, "owner_user_id": None,
                         "requested_by_user_id": 7})
    session.review()
    cfg = config(session, tmp_path)
    assert not session.sync_credential(cfg)
    api.credential.assert_not_called()
    api.items[0].update({"approval_status": "approved", "enabled": True, "owner_user_id": 7})
    session.review()
    session.apply_node(cfg, session.nodes["nd_one"])
    assert session.sync_credential(cfg)
    api.credential.assert_called_once_with("synthetic-first-access-token", "issue", {"node_id": "nd_one"})
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-node-token" in text
    assert "DASHBOARD_EXPECTED_MT5_LOGIN=12345" in text
    assert cfg.has_credential and session.nodes["nd_one"]["has_credential"]


def test_revoked_scoped_token_is_auto_rotated_without_user_action(login, tmp_path):
    """本机专属令牌被作废 → 面板自己轮换一枚新的写回 .env，不让用户管令牌。"""
    session, api = login
    api.items[0]["has_credential"] = True
    session.review()
    cfg = config(session, tmp_path)
    env = tmp_path / ".env"
    env.write_text("NODE_TOKEN=ndv1.nd_one.synthetic-invalid-node-token\n", encoding="utf-8")
    api.credential.side_effect = [
        {"valid": False, "node_id": "nd_one", "mt5_login": 12345, "generation": 1},
        {"node_id": "nd_one", "mt5_login": 12345, "generation": 2,
         "token": "ndv1.nd_one.synthetic-rotated-token"},
    ]
    assert session.sync_credential(cfg)
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in env.read_text(encoding="utf-8")
    assert [call.args[1] for call in api.credential.call_args_list] == ["verify", "rotate"]
    assert session.active


def test_rotation_is_refused_when_another_machine_holds_the_node(login, tmp_path):
    """别处正连着这个节点时不能轮换：轮换会把对方的节点踢下线。"""
    session, api = login
    api.items[0]["has_credential"] = True
    api.items[0]["status"] = "online"
    session.review()
    cfg = config(session, tmp_path)
    env = tmp_path / ".env"
    env.write_text("NODE_TOKEN=ndv1.nd_one.synthetic-invalid-node-token\n", encoding="utf-8")
    api.credential.side_effect = [
        {"valid": False, "node_id": "nd_one", "mt5_login": 12345, "generation": 1},
    ]
    repair = session.ensure_local_credential(cfg, rotate_allowed=False)
    assert not repair.ok
    assert "其他终端" in repair.reason
    assert env.read_text(encoding="utf-8") == "NODE_TOKEN=ndv1.nd_one.synthetic-invalid-node-token\n"
    assert [call.args[1] for call in api.credential.call_args_list] == ["verify"]


def test_own_online_node_may_still_be_rotated(login, tmp_path):
    """本机自己正在跑这个节点时允许轮换 —— 轮换后由面板重启自己。"""
    session, api = login
    api.items[0]["has_credential"] = True
    api.items[0]["status"] = "online"
    session.review()
    cfg = config(session, tmp_path)
    env = tmp_path / ".env"
    env.write_text("NODE_TOKEN=ndv1.nd_one.synthetic-invalid-node-token\n", encoding="utf-8")
    api.credential.side_effect = [
        {"valid": False, "node_id": "nd_one", "mt5_login": 12345, "generation": 1},
        {"node_id": "nd_one", "mt5_login": 12345, "generation": 2,
         "token": "ndv1.nd_one.synthetic-rotated-token"},
    ]
    mp = SimpleNamespace(cfg=cfg, daemon_grant=None,
                         runtime=SimpleNamespace(ws_state="authenticated", node_id="nd_one"))
    assert session.rotation_allowed(cfg, mp)
    assert session.sync_credential(cfg)
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in env.read_text(encoding="utf-8")


def test_missing_local_token_is_auto_reissued_on_start(login, tmp_path):
    """本机令牌缺失（后端已签发过）→ 启动时自动轮换，不要求用户确认。"""
    session, api = login
    api.items[0]["has_credential"] = True
    session.review()
    cfg = config(session, tmp_path)
    api.credential.side_effect = [
        {"node_id": "nd_one", "mt5_login": 12345, "generation": 2,
         "token": "ndv1.nd_one.synthetic-rotated-token"},
    ]
    mp = SimpleNamespace(cfg=cfg, daemon_grant=None)
    session.prepare_start(mp)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in text
    assert [call.args[1] for call in api.credential.call_args_list] == ["rotate"]
    assert mp.daemon_grant is not None


def test_issue_conflict_falls_back_to_rotate(login, tmp_path):
    """后端报「已签发」时自动改走轮换，而不是让用户去点按钮。"""
    session, api = login
    session.review()
    cfg = config(session, tmp_path)
    api.credential.side_effect = [
        DashboardAuthError("凭证已签发", status=409),
        {"node_id": "nd_one", "mt5_login": 12345, "generation": 2,
         "token": "ndv1.nd_one.synthetic-rotated-token"},
    ]
    assert session.sync_credential(cfg)
    assert [call.args[1] for call in api.credential.call_args_list] == ["issue", "rotate"]
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_legacy_global_node_keeps_original_token_and_does_not_convert(login, tmp_path):
    session, api = login
    api.items[0]["legacy_allowed"] = True
    session.review()
    cfg = config(session, tmp_path)
    env = tmp_path / ".env"
    env.write_text("NODE_TOKEN=synthetic-legacy-global-token\n", encoding="utf-8")
    assert session.sync_credential(cfg)
    mp = SimpleNamespace(cfg=cfg, daemon_grant=None)
    session.prepare_start(mp)
    assert "NODE_TOKEN=synthetic-legacy-global-token" in env.read_text(encoding="utf-8")
    assert cfg.legacy_allowed
    api.credential.assert_not_called()
    assert mp.daemon_grant is not None


@pytest.mark.parametrize("action", ["stop", "edit_env"])
def test_admin_cannot_control_local_instance_bound_to_another_backend(login, tmp_path, action):
    session, api = login
    session.principal["is_admin"] = True
    cfg = config(session, tmp_path)
    cfg.backend_base = "https://second.example.test"
    local = MagicMock()
    with pytest.raises(DashboardAuthError):
        session.perform(action, cfg, local)
    local.assert_not_called()
    api.authorize.assert_not_called()


def test_admin_binding_audits_target_node_from_current_backend(login, tmp_path):
    session, api = login
    session.principal["is_admin"] = True
    cfg = config(session, tmp_path)
    cfg.backend_base, cfg.node_id = "https://second.example.test", "nd_other_backend"
    bound = MagicMock()
    session.perform("bind_instance", cfg, bound, params={"node_id": "nd_one", "mt5_login": 12345})
    bound.assert_called_once()
    payload = api.authorize.call_args.args[1]
    assert payload["node_id"] == "nd_one"
    assert payload["action"] == "bind_instance"


def test_admin_load_does_not_recover_instance_from_another_backend(login, tmp_path, monkeypatch):
    session, api = login
    session.principal["is_admin"] = True
    cfg = config(session, tmp_path)
    cfg.backend_base = "https://second.example.test"
    recovered = MagicMock()
    monkeypatch.setattr(ManagedProcess, "recover", recovered)
    manager = ProcessManager(session=session)
    manager.load([cfg])
    recovered.assert_not_called()
    api.daemon_grant.assert_not_called()


def test_backend_node_record_fills_broker_server_name(login, tmp_path):
    """后台节点记录里带券商服务器名时，实例配置要拿到它。"""
    session, api = login
    api.items[0]["mt5_server"] = "gtcglobal-sa-server5"
    session.review()
    cfg = config(session, tmp_path)
    assert cfg.mt5_server == "gtcglobal-sa-server5"


def test_missing_backend_server_name_keeps_existing_value(login, tmp_path):
    """后台没带服务器名时不能把已知值冲掉（老后端版本就没有这个字段）。"""
    session, api = login
    session.review()
    cfg = config(session, tmp_path)
    cfg.mt5_server = "trademaxglobal"
    session.apply_node(cfg, {**session.nodes["nd_one"], "mt5_server": ""})
    assert cfg.mt5_server == "trademaxglobal"
