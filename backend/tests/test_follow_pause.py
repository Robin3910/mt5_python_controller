"""「暂停带单 / 暂停 AI 跟单」的端到端用例（fakeredis + SQLite，无需外部服务）。

这条功能的语义（三条，缺一不可）：
1. 新的客户无法再加入该市场名下的跟单分组；
2. 已有跟单关系与在跑任务**不动** —— 等跟单员自行移出；
3. 之后触发的信号**只发主节点**，不再下发给跟单节点。

两个市场**各自独立**：暂停带单（tpl_2 / tpl_3）不影响 AI 分组（tpl_1），
暂停 AI（tpl_1）也不影响带单分组。

这三个维度各有红线用例，任何一个变红都说明实现写错了。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PWD = "pass1234"
CAP_AI = "ai_strategy"
CAP_LEADER = "follow_leader"


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _login(client, username: str) -> dict:
    if username == "admin":
        return auth_headers(client)
    r = client.post("/api/login", json={"username": username, "password": _PWD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_user(client, h, username: str) -> dict:
    r = client.post("/api/users", json={"username": username, "password": _PWD}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_node(client, h, name: str, mt5_login: int, primary: bool = True) -> str:
    r = client.post("/api/nodes", json={"name": name, "mt5_login": mt5_login}, headers=h)
    assert r.status_code == 201, r.text
    nid = r.json()["node_id"]
    if primary:
        p = client.post(f"/api/nodes/{nid}/primary", json={"is_primary": True}, headers=h)
        assert p.status_code == 200, p.text
    return nid


def _assign(client, h, user_id: int, node_ids: list[str]):
    return client.put(
        f"/api/users/{user_id}/nodes", json={"node_ids": node_ids}, headers=h,
    )


def _make_role(client, admin_h, code: str, name: str, menus: list[str], caps: list[str]) -> dict:
    r = client.post(
        "/api/roles",
        json={"code": code, "name": name, "menus": menus, "capabilities": caps},
        headers=admin_h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _bind_role(client, admin_h, user_id: int, role_id: int) -> None:
    r = client.put(f"/api/users/{user_id}/roles", json={"role_ids": [role_id]}, headers=admin_h)
    assert r.status_code == 200, r.text


def _mk_strategy(client, h, name: str, template_id: str, symbol: str = "XAUUSD") -> str:
    payload: dict = {"name": name, "template_id": template_id, "symbol": symbol}
    if template_id == "tpl_3":
        payload["rules"] = [{
            "type": 4, "status": 1, "action": "all",
            "price_lower": 1.0, "price_upper": 1.1,
            "grid_count": 10, "grid_mode": "arithmetic",
            "grid_side": "long", "lot_per_grid": 0.01,
            "total_lot_limit": 0.0, "trigger_price": 0.0,
            "stop_lower": 0.0, "stop_upper": 0.0,
            "close_on_stop": True, "prefill_enabled": True,
            "trailing_up": False, "trailing_max": 0,
        }]
    r = client.post("/api/strategies", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["strategy_id"]


def _mk_group(client, h, node_ids: list[str], name: str, strategy_id: str = "",
              accept_ai_follow: bool = False) -> str:
    payload: dict = {"name": name, "node_ids": node_ids, "accept_ai_follow": accept_ai_follow}
    if strategy_id:
        payload["strategy_id"] = strategy_id
    r = client.post("/api/groups", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


def _setup(client, admin_h, *, caps=None, suffix: str = "") -> dict:
    """建主人（host）+ 跟单员（bob），各配节点。

    caps: 主人持有的能力列表；默认两个能力都持有（同时能做带单与 AI 机器人）。
    """
    caps = caps if caps is not None else [CAP_AI, CAP_LEADER]
    host = _mk_user(client, admin_h, f"host{suffix}")
    bob = _mk_user(client, admin_h, f"bob{suffix}")

    host_role = _make_role(
        client, admin_h, f"hostrole{suffix}", f"主人{suffix}",
        ["ai_trade", "follow", "groups", "strategies", "trading"], caps,
    )
    bob_role = _make_role(
        client, admin_h, f"bobrole{suffix}", f"跟单员{suffix}",
        ["ai_trade", "follow", "groups", "strategies", "trading"], [],
    )
    _bind_role(client, admin_h, host["id"], host_role["id"])
    _bind_role(client, admin_h, bob["id"], bob_role["id"])

    base = 90001 + (int(suffix) * 10 if suffix.isdigit() else 0)
    h_primary = _mk_node(client, admin_h, f"host-primary{suffix}", base + 1)
    h_extra = _mk_node(client, admin_h, f"host-extra{suffix}", base + 2, primary=False)
    b_primary = _mk_node(client, admin_h, f"bob-primary{suffix}", base + 3)
    b_follow = _mk_node(client, admin_h, f"bob-follow{suffix}", base + 4, primary=False)
    assert _assign(client, admin_h, host["id"], [h_primary, h_extra]).status_code == 200
    assert _assign(client, admin_h, bob["id"], [b_primary, b_follow]).status_code == 200

    host_h = _login(client, f"host{suffix}")
    # 带单广场要求「有主节点 + 有含主节点的分组」，所以建一个趋势分组（带单市场）。
    trend_sid = _mk_strategy(client, host_h, f"趋势策略{suffix}", "tpl_2")
    trend_gid = _mk_group(
        client, host_h, [h_primary], f"带单组{suffix}", strategy_id=trend_sid,
    )
    return {
        "host": host, "bob": bob, "host_h": host_h,
        "h_primary": h_primary, "h_extra": h_extra,
        "b_primary": b_primary, "b_follow": b_follow,
        "trend_strategy_id": trend_sid, "trend_group_id": trend_gid,
    }


# ---------------------------------------------------------------------------
# 权限与基本语义
# ---------------------------------------------------------------------------
def test_pause_requires_own_leader(client):
    """不能替别人暂停 —— 主人级的对外接单开关只能自己关（管理员除外）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    bob_h = _login(client, "bob")

    r = client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=bob_h,
    )
    assert r.status_code == 403, r.text


def test_pause_and_resume_roundtrip(client):
    """暂停 -> 恢复，状态与列表回填同步。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")

    # 默认未暂停
    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    leaders = r.json()["leaders"]
    assert leaders and leaders[0]["follow_paused"] is False

    r = client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["follow_paused"] is True
    assert r.json()["paused_at"] is not None

    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.json()["leaders"][0]["follow_paused"] is True

    # 恢复
    r = client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": False},
        headers=host_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["follow_paused"] is False
    assert r.json()["paused_at"] is None

    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.json()["leaders"][0]["follow_paused"] is False


def test_pause_is_idempotent(client):
    """重复暂停不报错（幂等），状态不变。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]

    for _ in range(2):
        r = client.post(
            "/api/follow/pause",
            json={"leader_user_id": s["host"]["id"], "paused": True},
            headers=host_h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["follow_paused"] is True


def test_pause_requires_leader_capability(client):
    """没有带单能力的人不能暂停（暂停一个带不单的人没有意义）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI])  # 只有 ai_strategy，没有 follow_leader
    host_h = s["host_h"]

    r = client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 语义 ①：新客户无法加入
# ---------------------------------------------------------------------------
def test_paused_leader_join_rejected(client):
    """暂停后新客户加入跟单被拒（400）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")
    gid = s["trend_group_id"]

    # 先确认不暂停时能加
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    # 暂停后再加 -> 拒绝
    assert client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    bob2 = _mk_user(client, admin_h, "bob2")
    bob2_role = _make_role(client, admin_h, "bob2role", "跟单员2", ["follow", "groups"], [])
    _bind_role(client, admin_h, bob2["id"], bob2_role["id"])
    b2_primary = _mk_node(client, admin_h, "bob2-primary", 90061)
    b2_follow = _mk_node(client, admin_h, "bob2-follow", 90062, primary=False)
    assert _assign(client, admin_h, bob2["id"], [b2_primary, b2_follow]).status_code == 200
    bob2_h = _login(client, "bob2")

    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [b2_follow],
        },
        headers=bob2_h,
    )
    assert r.status_code == 400, r.text
    assert "暂停" in r.json()["detail"]


def test_paused_ai_robot_join_rejected(client):
    """AI 市场：暂停后新客户加入 AI 分组被拒（400）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")

    sid = _mk_strategy(client, host_h, "AI策略", "tpl_1")
    gid = _mk_group(
        client, host_h, [s["h_primary"]], "AI组", strategy_id=sid, accept_ai_follow=True,
    )

    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    assert client.post(
        "/api/ai-follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    bob2 = _mk_user(client, admin_h, "bob2")
    bob2_role = _make_role(client, admin_h, "bob2role", "跟单员2", ["ai_trade", "groups"], [])
    _bind_role(client, admin_h, bob2["id"], bob2_role["id"])
    b2_primary = _mk_node(client, admin_h, "bob2-primary", 90061)
    b2_follow = _mk_node(client, admin_h, "bob2-follow", 90062, primary=False)
    assert _assign(client, admin_h, bob2["id"], [b2_primary, b2_follow]).status_code == 200
    bob2_h = _login(client, "bob2")

    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [b2_follow],
        },
        headers=bob2_h,
    )
    assert r.status_code == 400, r.text
    assert "暂停" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 语义 ②：已有跟单关系不动
# ---------------------------------------------------------------------------
def test_pause_keeps_existing_follow_relationship(client):
    """暂停**不**删除已有跟单关系（等跟单员自行移出）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")
    gid = s["trend_group_id"]

    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    assert client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    # 关系还在：跟单员仍能看到这条跟单
    r = client.get("/api/follow/me", headers=bob_h)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["following"], "暂停不应清空我的跟单"
    group = me["following"][0]["groups"][0]
    assert group["group_id"] == gid
    # 但分组被标记为不可再收新信号
    assert group["marketable"] is False
    assert "暂停" in (group.get("unmarketable_reason") or "")


def test_pause_keeps_existing_ai_follow_relationship(client):
    """AI 市场：暂停**不**删除已有跟单关系。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")

    sid = _mk_strategy(client, host_h, "AI策略", "tpl_1")
    gid = _mk_group(
        client, host_h, [s["h_primary"]], "AI组", strategy_id=sid, accept_ai_follow=True,
    )

    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    assert client.post(
        "/api/ai-follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    r = client.get("/api/ai-follow/me", headers=bob_h)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["following"], "暂停不应清空我的 AI 跟单"


# ---------------------------------------------------------------------------
# 两个市场互不串台
# ---------------------------------------------------------------------------
def test_pause_follow_does_not_affect_ai_market(client):
    """暂停带单**不**影响 AI 市场：AI 跟单仍可加入。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")

    ai_sid = _mk_strategy(client, host_h, "AI策略", "tpl_1")
    ai_gid = _mk_group(
        client, host_h, [s["h_primary"]], "AI组", strategy_id=ai_sid, accept_ai_follow=True,
    )

    assert client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    # AI 跟单不受影响
    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [ai_gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    # 两个标志互相独立
    r = client.get("/api/ai-follow/robots", headers=bob_h)
    robots = r.json()["robots"]
    assert robots[0]["ai_follow_paused"] is False


def test_me_exposes_pause_flags(client):
    """/api/me 带上我自己的两个暂停标志（交易页据此提示「只发主节点」）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]

    r = client.get("/api/me", headers=host_h)
    assert r.status_code == 200, r.text
    assert r.json()["follow_paused"] is False
    assert r.json()["ai_follow_paused"] is False

    assert client.post(
        "/api/follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    r = client.get("/api/me", headers=host_h)
    assert r.json()["follow_paused"] is True
    assert r.json()["ai_follow_paused"] is False, "带单暂停不应影响 AI 标志"

    assert client.post(
        "/api/ai-follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    r = client.get("/api/me", headers=host_h)
    assert r.json()["follow_paused"] is True
    assert r.json()["ai_follow_paused"] is True


def test_pause_ai_does_not_affect_follow_market(client):
    """暂停 AI **不**影响带单市场：普通跟单仍可加入。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h)
    host_h = s["host_h"]
    bob_h = _login(client, "bob")
    gid = s["trend_group_id"]

    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": s["host"]["id"], "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    assert client.post(
        "/api/ai-follow/pause",
        json={"leader_user_id": s["host"]["id"], "paused": True},
        headers=host_h,
    ).status_code == 200

    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.json()["leaders"][0]["follow_paused"] is False
