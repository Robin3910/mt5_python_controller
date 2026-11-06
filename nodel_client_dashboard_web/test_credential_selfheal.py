"""专属令牌自愈的回归：令牌是面板的内部细节，用户只管导入 + 启动。

跑法：
    python -m pytest test_credential_selfheal.py
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_session as sessions  # noqa: E402
from auth_service import DashboardAuthError  # noqa: E402
from models import InstanceConfig  # noqa: E402


class FakeAPI:
    """只实现令牌相关的那几个接口。"""

    def __init__(self, base="https://first.example.test", *, user_id=7):
        self.base = base
        self.principal = {"user_id": user_id, "is_admin": False, "menus": ["nodes"]}
        self.items = [{
            "node_id": "nd_one", "mt5_login": 12345, "owner_user_id": user_id,
            "approval_status": "approved", "enabled": True, "credential_generation": 1,
            "has_credential": False, "legacy_allowed": False, "status": "offline",
        }]
        self.authorize = MagicMock(return_value={"operation_id": 101})
        self.result = MagicMock(return_value={"ok": True})
        self.credential = MagicMock(return_value={
            "node_id": "nd_one", "mt5_login": 12345, "generation": 1,
            "valid": True, "token": "ndv1.nd_one.synthetic-node-token",
        })
        self.daemon_grant = MagicMock(return_value={
            "grant_token": "synthetic-memory-only-daemon-token", "node_id": "nd_one",
            "mt5_login": 12345, "generation": 1, "grant_operation_id": 101,
            "actor_user_id": user_id,
        })
        self.check_grant = MagicMock(return_value={
            "ok": True, "node_id": "nd_one", "mt5_login": 12345, "generation": 1,
        })
        self.daemon_event = MagicMock(return_value={"ok": True})
        self.replay_daemon_event = MagicMock(return_value={"ok": True})

    def me(self, _token):
        return deepcopy(self.principal)

    def nodes(self, _token):
        return deepcopy(self.items)


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setattr(sessions, "load_audit_queue", lambda: [])
    monkeypatch.setattr(sessions, "save_audit_queue", lambda records: None)
    monkeypatch.setattr(sessions, "load_daemon_events", lambda: [])
    monkeypatch.setattr(sessions, "save_daemon_events", lambda records: None)
    api = FakeAPI()
    session = sessions.DashboardSession()
    session.establish(api, "synthetic-first-access-token")
    return session, api


def config(session, root, node_id="nd_one"):
    root.mkdir(parents=True, exist_ok=True)
    cfg = InstanceConfig.create(exe_path=str(root / "node_client.exe"), cwd=str(root))
    session.apply_node(cfg, session.nodes[node_id])
    return cfg


# ─────────────────────────────────────────────── 自动重签

def test_missing_local_token_is_issued_automatically(login, tmp_path):
    """后端没签过 → 面板自己签一枚写回 .env，用户什么都不用做。"""
    session, api = login
    cfg = config(session, tmp_path)
    assert session.sync_credential(cfg)
    assert api.credential.call_args.args[1] == "issue"
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-node-token" in text
    assert "DASHBOARD_EXPECTED_MT5_LOGIN=12345" in text


def test_revoked_local_token_is_rotated_automatically(login, tmp_path):
    """本机那枚被作废 → 自动轮换，不要求用户确认。"""
    session, api = login
    api.items[0]["has_credential"] = True
    session.review()
    cfg = config(session, tmp_path)
    (tmp_path / ".env").write_text(
        "NODE_TOKEN=ndv1.nd_one.synthetic-revoked-token\n", encoding="utf-8"
    )
    api.credential.side_effect = [
        {"valid": False, "node_id": "nd_one", "mt5_login": 12345, "generation": 1},
        {"node_id": "nd_one", "mt5_login": 12345, "generation": 2,
         "token": "ndv1.nd_one.synthetic-rotated-token"},
    ]
    repair = session.ensure_local_credential(cfg)
    assert repair.ok and repair.reissued
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
    assert [call.args[1] for call in api.credential.call_args_list] == ["verify", "rotate"]


def test_issue_conflict_falls_back_to_rotate(login, tmp_path):
    """后端报「已签发」时自动改走轮换，而不是把问题抛给用户。"""
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


def test_missing_local_token_on_start_is_rotated_automatically(login, tmp_path):
    """启动时发现本机令牌缺失 → 自动轮换后照常启动。"""
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
    assert "NODE_TOKEN=ndv1.nd_one.synthetic-rotated-token" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
    assert mp.daemon_grant is not None


# ─────────────────────────────────────────────── 安全边界

def test_rotation_refused_when_another_machine_holds_the_node(login, tmp_path):
    """别处正连着这个节点时不能轮换 —— 轮换会把对方的节点踢下线。"""
    session, api = login
    api.items[0]["has_credential"] = True
    api.items[0]["status"] = "online"
    session.review()
    cfg = config(session, tmp_path)
    (tmp_path / ".env").write_text(
        "NODE_TOKEN=ndv1.nd_one.synthetic-revoked-token\n", encoding="utf-8"
    )
    api.credential.side_effect = [
        {"valid": False, "node_id": "nd_one", "mt5_login": 12345, "generation": 1},
    ]
    repair = session.ensure_local_credential(cfg, rotate_allowed=False)
    assert not repair.ok
    assert "其他终端" in repair.reason
    assert [call.args[1] for call in api.credential.call_args_list] == ["verify"]


def test_own_online_node_may_still_be_rotated(login, tmp_path):
    """本机自己正在跑这个节点时允许轮换 —— 之后由面板重启自己。"""
    session, api = login
    api.items[0]["has_credential"] = True
    api.items[0]["status"] = "online"
    session.review()
    cfg = config(session, tmp_path)
    mp = SimpleNamespace(
        cfg=cfg, daemon_grant=None,
        runtime=SimpleNamespace(ws_state="authenticated", node_id="nd_one"),
    )
    assert session.rotation_allowed(cfg, mp)
    assert not session.node_is_online_elsewhere(cfg, mp)


def test_legacy_global_node_keeps_its_token(login, tmp_path):
    """旧节点沿用全局令牌：面板不碰它的令牌，也不签专属令牌。"""
    session, api = login
    api.items[0]["legacy_allowed"] = True
    session.review()
    cfg = config(session, tmp_path)
    (tmp_path / ".env").write_text(
        "NODE_TOKEN=synthetic-legacy-global-token\n", encoding="utf-8"
    )
    assert session.sync_credential(cfg)
    api.credential.assert_not_called()
    assert "NODE_TOKEN=synthetic-legacy-global-token" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")


def test_pending_node_is_not_issued_credential(login, tmp_path):
    """节点还没审核开通 → 不碰凭证。"""
    session, api = login
    api.items[0].update({"approval_status": "pending", "enabled": False})
    session.review()
    cfg = config(session, tmp_path)
    assert not session.sync_credential(cfg)
    api.credential.assert_not_called()
