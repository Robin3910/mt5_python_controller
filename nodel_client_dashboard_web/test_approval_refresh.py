"""回归：管理员在后台开通 / 分配节点后，面板不用重启就能启动节点。

背景（用户报的 bug）：
    管理员在后台把节点审核通过并分配给用户，面板里点「启动」却被拒
    （「节点需管理员审核开通后才能启动或开启守护」）。
    关掉面板重开就好了 —— 说明面板拿的是**登录那一刻**的节点快照，
    之后一直没跟后端同步。

根因：
    节点能不能启动由后端说了算，但面板把节点列表快照进 `session.nodes`
    之后就没有再更新过。WebView 版还缺了 TK 版的定时复核。

跑法：
    python -m pytest test_approval_refresh.py
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
    """只实现面板用到的几个接口；`items` 可在测试中途改，模拟后端状态变化。"""

    def __init__(self, base="https://first.example.test", *, user_id=7):
        self.base = base
        self.principal = {"user_id": user_id, "is_admin": False, "menus": ["nodes"]}
        # 初始：节点已绑定但**还没审核开通**（管理员后台还没放行）
        self.items = [{
            "node_id": "nd_one", "mt5_login": 12345, "owner_user_id": user_id,
            "approval_status": "pending", "enabled": False,
            "credential_generation": 0, "has_credential": False,
            "legacy_allowed": False, "status": "offline",
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


def _config(session, root, node_id="nd_one"):
    root.mkdir(parents=True, exist_ok=True)
    cfg = InstanceConfig.create(exe_path=str(root / "node_client.exe"), cwd=str(root))
    session.apply_node(cfg, session.nodes[node_id])
    return cfg


def _approve(api):
    """模拟管理员在后台审核通过并分配给用户。"""
    api.items[0].update({"approval_status": "approved", "enabled": True})


# ─────────────────────────────────────────────── 核心回归

def test_start_succeeds_after_admin_approves_without_restart(login, tmp_path):
    """管理员后台开通后，**不重启面板**直接启动节点就应该成功。

    回归：面板拿的是登录时的旧快照（pending / 未启用），
    于是 perform("start") 在本地就把启动挡下了。
    """
    session, api = login
    cfg = _config(session, tmp_path)
    assert cfg.approval_status == "pending", "前置：初始应为待审核"

    # 管理员在后台放行
    _approve(api)

    started = {"done": False}

    def _do_start():
        started["done"] = True
        return True

    # 启动前会走 require_binding（校验客户端是否支持账户绑定），
    # 这里没有真实客户端，替换掉这一项，专注测开通状态这一层。
    monkey_ok = pytest.MonkeyPatch()
    monkey_ok.setattr(sessions, "require_binding", lambda _exe: None)
    try:
        session.perform("start", cfg, _do_start)
    finally:
        monkey_ok.undo()

    assert started["done"], "开通后应能直接启动，不需要重启面板"
    assert cfg.approval_status == "approved", cfg.approval_status
    assert cfg.enabled is True, cfg.enabled


def test_start_still_blocked_when_node_not_approved(login, tmp_path):
    """还没审核开通时依然要挡住启动 —— 这次刷新不能把闸门放没了。"""
    session, api = login
    cfg = _config(session, tmp_path)

    monkey_ok = pytest.MonkeyPatch()
    monkey_ok.setattr(sessions, "require_binding", lambda _exe: None)
    try:
        with pytest.raises(DashboardAuthError, match="审核开通"):
            session.perform("start", cfg, lambda: True)
    finally:
        monkey_ok.undo()


def test_set_daemon_enabled_succeeds_after_admin_approves(login, tmp_path):
    """开启守护同样受开通状态约束，后台放行后应能开。"""
    session, api = login
    cfg = _config(session, tmp_path)
    _approve(api)

    monkey_ok = pytest.MonkeyPatch()
    monkey_ok.setattr(sessions, "require_binding", lambda _exe: None)
    try:
        session.perform("set_daemon", cfg, lambda: True, params={"enabled": True})
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"后台放行后开启守护不该失败：{exc}")
    finally:
        monkey_ok.undo()


def test_start_succeeds_after_node_reassigned_to_user(login, tmp_path):
    """管理员把节点**重新分配**给当前用户后，启动不该被「无权操作该实例」挡下。

    归属也是后端说了算；旧快照里 owner 不是自己，`owns()` 会先拒。
    """
    session, api = login
    cfg = _config(session, tmp_path)
    # 旧快照：节点归别人（管理员名下 = owner 为空）
    api.items[0].update({"owner_user_id": None})
    session.refresh_node_state(cfg)

    # 后台重新分配给当前用户并开通
    api.items[0].update({"owner_user_id": 7, "approval_status": "approved", "enabled": True})

    started = {"done": False}
    monkey_ok = pytest.MonkeyPatch()
    monkey_ok.setattr(sessions, "require_binding", lambda _exe: None)
    try:
        session.perform("start", cfg, lambda: started.update(done=True) or True)
    except DashboardAuthError as exc:
        pytest.fail(f"重新分配后不该被拒：{exc}")
    finally:
        monkey_ok.undo()
    assert started["done"], "重新分配后应能直接启动"


def test_refresh_node_state_pulls_latest_backend_state(login, tmp_path):
    """`refresh_node_state` 应以后端为准更新本机实例的开通状态。"""
    session, api = login
    cfg = _config(session, tmp_path)
    assert cfg.approval_status == "pending"

    _approve(api)
    session.refresh_node_state(cfg)

    assert cfg.approval_status == "approved", cfg.approval_status
    assert cfg.enabled is True, cfg.enabled


def test_review_keeps_snapshot_when_backend_list_is_empty(login):
    """复核时后端返回空列表（Redis 缓存被清）不能抹掉已有节点快照。

    定时复核每 30 秒跑一次 —— 如果这里把快照清空，本机实例的绑定
    会被反复抹掉，remove_instance 随后就因为缺 node_id 被后端 422 挡下。
    """
    session, api = login
    assert "nd_one" in session.nodes

    api.items = []
    session.review()

    assert "nd_one" in session.nodes, "空列表不该抹掉已有节点快照"


def test_refresh_node_state_keeps_binding_when_backend_list_is_empty(login, tmp_path):
    """后端节点列表为空（Redis 缓存被清）时不能抹掉 node_id。

    绑定一丢，remove_instance 就因为缺 node_id 被后端 422 挡下。
    """
    session, api = login
    cfg = _config(session, tmp_path)
    api.items = []

    session.refresh_node_state(cfg)

    assert cfg.node_id == "nd_one", cfg.node_id
    assert cfg.approval_status == "pending", cfg.approval_status


def test_refresh_node_state_tolerates_backend_outage(login, tmp_path):
    """后端连不上时保持原状，不把面板整个搞挂。"""
    session, api = login
    cfg = _config(session, tmp_path)

    api.nodes = MagicMock(side_effect=OSError("network down"))
    session.refresh_node_state(cfg)

    assert cfg.approval_status == "pending", cfg.approval_status
    assert cfg.node_id == "nd_one", cfg.node_id


def test_prepare_start_picks_up_approval_after_backend_enables(login, tmp_path):
    """prepare_start 也要以后端为准 —— 版本更新后的重启路径同样要能起。"""
    session, api = login
    cfg = _config(session, tmp_path)
    (tmp_path / ".env").write_text(
        "NODE_TOKEN=ndv1.nd_one.synthetic-node-token\n", encoding="utf-8"
    )
    api.items[0].update({"has_credential": True, "legacy_allowed": False})
    _approve(api)

    mp = SimpleNamespace(cfg=cfg, daemon_grant=None, runtime=SimpleNamespace())
    session.prepare_start(mp)

    assert cfg.approval_status == "approved", cfg.approval_status
    assert mp.daemon_grant is not None
