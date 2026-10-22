"""AI 交易（ai_follow）端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖这条链路的关键保证：
- **机器人准入 = ai_strategy 且 follow_leader**（两个能力都要）；
- **分组开关 accept_ai_follow 默认关闭**，不开放的分组不进广场、也写不进跟单关系；
- **市场互斥**：带单 tab 加不进 tpl_1 分组，AI tab 加不进 tpl_2/tpl_3 分组；
- 移出跟单默认**不停止在跑任务**（close_tasks 默认 false）；
- 跟单员能力不足 / 分组未开放时，仍可自行移出（清理不需要能力）。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

# 与 conftest 的 MYSQL_DSN 指向同一个库：全部 API 用例共用，必须复用同一个路径
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


def _assign(client, h, user_id: int, node_ids: list[str], confirm: bool = False):
    return client.put(
        f"/api/users/{user_id}/nodes",
        json={"node_ids": node_ids, "confirm": confirm}, headers=h,
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


def _mk_ai_strategy(client, h, name: str, symbol: str = "XAUUSD") -> str:
    """建一个 AI 智能加仓策略（tpl_1）；需要 ai_strategy 能力。"""
    r = client.post(
        "/api/strategies",
        json={"name": name, "template_id": "tpl_1", "symbol": symbol},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["strategy_id"]


def _mk_grid_strategy(client, h, name: str, symbol: str = "EURUSD") -> str:
    """建一个网格策略（tpl_3）；模版默认规则不合法，必须显式给一条可用规则。"""
    r = client.post(
        "/api/strategies",
        json={
            "name": name, "template_id": "tpl_3", "symbol": symbol,
            "rules": [{
                "type": 4, "status": 1, "action": "all",
                "price_lower": 1.0, "price_upper": 1.1,
                "grid_count": 10, "grid_mode": "arithmetic",
                "grid_side": "long", "lot_per_grid": 0.01,
                "total_lot_limit": 0.0, "trigger_price": 0.0,
                "stop_lower": 0.0, "stop_upper": 0.0,
                "close_on_stop": True, "prefill_enabled": True,
                "trailing_up": False, "trailing_max": 0,
            }],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["strategy_id"]


def _mk_group(client, h, node_ids: list[str], name: str, strategy_id: str = "",
              accept_ai_follow: bool = False) -> str:
    payload = {"name": name, "node_ids": node_ids}
    if strategy_id:
        payload["strategy_id"] = strategy_id
    payload["accept_ai_follow"] = accept_ai_follow
    r = client.post("/api/groups", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


def _setup(client, admin_h, *, caps: list[str], ai_group: bool = True,
           open_ai: bool = True, suffix: str = "") -> dict:
    """建主人（host）+ 跟单员（bob），返回关键 id。

    caps: 主人持有的能力列表（决定能否进广场）
    ai_group: 是否给主人建一个 tpl_1 分组
    open_ai: 该 tpl_1 分组是否开放 AI 跟单
    suffix: 用户名后缀（同一次用例里建多组时避免重名）
    """
    host = _mk_user(client, admin_h, f"robot{suffix}")
    bob = _mk_user(client, admin_h, f"bob{suffix}")

    host_role = _make_role(
        client, admin_h, f"hostrole{suffix}", f"机器人主人{suffix}",
        ["ai_trade", "follow", "groups", "strategies"], caps,
    )
    bob_role = _make_role(
        client, admin_h, f"bobrole{suffix}", f"跟单员{suffix}",
        ["ai_trade", "follow", "groups", "strategies"], [],
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

    host_h = _login(client, f"robot{suffix}")
    ai_group_id = ""
    if ai_group:
        sid = _mk_ai_strategy(client, host_h, "host-ai")
        ai_group_id = _mk_group(
            client, host_h, [h_primary], "host-ai组", strategy_id=sid,
            accept_ai_follow=open_ai,
        )
    return {
        "host": host, "bob": bob, "host_h": host_h,
        "h_primary": h_primary, "h_extra": h_extra,
        "b_primary": b_primary, "b_follow": b_follow,
        "ai_group_id": ai_group_id,
    }


# ---------------------------------------------------------------------------
# 机器人准入：两个能力都要
# ---------------------------------------------------------------------------
def test_ai_trade_menu_registered():
    """【AI 交易】菜单码进了注册表，且对普通用户可分配（默认进普通用户角色）。"""
    from app import permissions

    assert permissions.MENU_AI_TRADE == "ai_trade"
    assert permissions.MENU_AI_TRADE in permissions.ALL_MENU_CODES
    assert permissions.MENU_AI_TRADE in permissions.ASSIGNABLE_MENU_CODES
    assert permissions.MENU_AI_TRADE in permissions.DEFAULT_USER_MENUS


def test_can_host_ai_robot_requires_both_capabilities():
    """机器人准入 = ai_strategy **且** follow_leader，两个都要。"""
    from app import permissions

    def p(caps):
        return permissions.Principal(
            user_id=1, username="u", is_admin=False,
            menus=frozenset(), capabilities=frozenset(caps),
        )

    assert permissions.can_host_ai_robot(p([CAP_AI, CAP_LEADER])) is True
    assert permissions.can_host_ai_robot(p([CAP_AI])) is False
    assert permissions.can_host_ai_robot(p([CAP_LEADER])) is False
    assert permissions.can_host_ai_robot(p([])) is False
    # 管理员放行
    admin = permissions.Principal(
        user_id=1, username="u", is_admin=True, menus=frozenset(),
        capabilities=frozenset(),
    )
    assert permissions.can_host_ai_robot(admin) is True


def test_robot_requires_both_capabilities(client):
    """只持一个能力的主人不进广场；两个都有才进。"""
    admin_h = _login(client, "admin")

    # 只持 ai_strategy
    _setup(client, admin_h, caps=[CAP_AI], ai_group=False)
    bob_h = _login(client, "bob")
    r = client.get("/api/ai-follow/robots", headers=bob_h)
    assert r.status_code == 200, r.text
    assert [x["username"] for x in r.json()["robots"]] == [], "只有 ai_strategy 不够"

    # 只持 follow_leader
    _setup(client, admin_h, caps=[CAP_LEADER], ai_group=False, suffix="2")
    r = client.get("/api/ai-follow/robots", headers=bob_h)
    assert r.status_code == 200, r.text
    assert [x["username"] for x in r.json()["robots"]] == [], "只有 follow_leader 不够"


def test_robot_with_both_capabilities_appears(client):
    """两个能力都有 + 有已开放 AI 跟单的 tpl_1 分组 -> 进广场。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.get("/api/ai-follow/robots", headers=bob_h)
    assert r.status_code == 200, r.text
    robots = r.json()["robots"]
    assert [x["username"] for x in robots] == ["robot"]
    assert robots[0]["ai_group_count"] == 1


# ---------------------------------------------------------------------------
# 分组开关 accept_ai_follow（默认关）
# ---------------------------------------------------------------------------
def test_ai_group_not_open_by_default(client):
    """accept_ai_follow 默认关闭 -> 分组不进广场。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER], open_ai=False)
    bob_h = _login(client, "bob")
    r = client.get("/api/ai-follow/robots", headers=bob_h)
    assert r.status_code == 200, r.text
    assert r.json()["robots"] == [], "未开放 AI 跟单的分组不进广场"


def test_ai_group_default_flag_is_false(client):
    """新建分组的 accept_ai_follow 默认为 false（显式开关，默认关）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER], ai_group=False)
    gid = _mk_group(client, s["host_h"], [s["h_primary"]], "普通分组")
    r = client.get(f"/api/groups/{gid}", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["accept_ai_follow"] is False


def test_accept_ai_follow_toggle_persists(client):
    """打开 accept_ai_follow 后，分组进广场（验证缓存 dict 同步写入）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER], open_ai=False)
    gid = s["ai_group_id"]

    r = client.patch(
        f"/api/groups/{gid}", json={"accept_ai_follow": True}, headers=admin_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["accept_ai_follow"] is True

    bob_h = _login(client, "bob")
    r2 = client.get("/api/ai-follow/robots", headers=bob_h)
    assert r2.status_code == 200, r2.text
    assert [x["username"] for x in r2.json()["robots"]] == ["robot"], "开放后应进广场"


# ---------------------------------------------------------------------------
# 加入 AI 跟单
# ---------------------------------------------------------------------------
def test_join_ai_follow_succeeds(client):
    """正常加入：节点是自己的、非主节点、分组已开放。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["joined"]) == 1

    # 我的 AI 跟单里能看到
    m = client.get("/api/ai-follow/me", headers=bob_h)
    assert m.status_code == 200, m.text
    following = m.json()["following"]
    assert len(following) == 1
    assert following[0]["robot"]["username"] == "robot"


def test_join_ai_follow_rejects_unopened_group(client):
    """未开放 AI 跟单的分组写不进关系（开关在**写入侧**校验）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER], open_ai=False)
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text


def test_join_ai_follow_rejects_grid_group(client):
    """AI tab 加不进 tpl_3 分组（市场互斥）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER], ai_group=False)
    sid = _mk_grid_strategy(client, s["host_h"], "host-grid")
    gid = _mk_group(client, s["host_h"], [s["h_primary"]], "host-grid组", strategy_id=sid)
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [gid],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text


def test_follow_tab_rejects_ai_group(client):
    """带单 tab 加不进 tpl_1 分组（两个市场互斥）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text


def test_join_ai_follow_rejects_primary_node(client):
    """主节点不可用于跟单。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_primary"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 移出 AI 跟单
# ---------------------------------------------------------------------------
def test_leave_ai_follow_works(client):
    """移出：关系解除。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    r = client.post(
        "/api/ai-follow/leave",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["removed"]) == 1

    m = client.get("/api/ai-follow/me", headers=bob_h)
    assert m.json()["following"] == []


def test_leave_ai_follow_default_keeps_running_tasks(client):
    """close_tasks 默认 false -> 在跑任务不停止（移出后仍继续运行至结束）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    r = client.post(
        "/api/ai-follow/leave",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 没有进行中任务时不会有 closing/forced，也不会误报 warning
    assert body["closing"] == []
    assert body["forced"] == []


def test_leave_ai_follow_works_after_capability_revoked(client):
    """主人被撤销能力后，跟单员仍能自行移出（清理不需要能力）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    # 撤销能力
    roles = client.get("/api/roles", headers=admin_h).json()
    role = next(r for r in roles if r["code"] == "hostrole")
    client.put(
        f"/api/roles/{role['id']}/capabilities",
        json={"capabilities": []}, headers=admin_h,
    )
    r = client.post(
        "/api/ai-follow/leave",
        json={
            "leader_user_id": s["host"]["id"],
            "group_ids": [s["ai_group_id"]],
            "node_ids": [s["b_follow"]],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    m = client.get("/api/ai-follow/me", headers=bob_h)
    assert m.json()["following"] == []


# ---------------------------------------------------------------------------
# 机器人详情
# ---------------------------------------------------------------------------
def test_robot_detail_returns_groups_and_my_nodes(client):
    """详情：可开放的 AI 分组 + 我的可跟单节点。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.get(f"/api/ai-follow/robots/{s['host']['id']}", headers=bob_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["robot"]["username"] == "robot"
    assert len(body["groups"]) == 1
    assert body["groups"][0]["group_id"] == s["ai_group_id"]
    # AI 不支持信号并发
    assert body["groups"][0]["signal_concurrent_enabled"] is False
    assert body["groups"][0]["concurrent_unavailable_reason"]
    # 我的可跟单节点：只有非主节点
    assert [n["node_id"] for n in body["my_nodes"]] == [s["b_follow"]]


def test_robot_detail_404_when_not_host(client):
    """不具备机器人资格的用户，详情 404（不泄露存在性）。"""
    admin_h = _login(client, "admin")
    s = _setup(client, admin_h, caps=[CAP_AI, CAP_LEADER])
    bob_h = _login(client, "bob")
    r = client.get(f"/api/ai-follow/robots/{s['bob']['id']}", headers=bob_h)
    assert r.status_code == 404, r.text
