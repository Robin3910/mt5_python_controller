"""带单（follow）端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖这条链路的关键保证：
- 能力语义：**跟单默认人人可做，带单需要 follow_leader 角色能力**；
- 带单员列表只出现「有能力 + 有主节点 + 有含主节点的分组」的人，**含自己**（自跟单：自己的非主节点跟自己的分组）；
- 加入跟单：节点必须是自己的、已启用、非主节点；分组必须是带单员名下的带单源分组；
- 带单员被撤销能力后：列表里消失，GET /api/follow/me 仍能自行移出（清理不需要能力）；
- 分组缓存带 followers，且 owner 编辑成员不会误删跟单关系；
- 节点删除 / 归属转移 / 升主节点时跟单关系被清理（防脏数据）。
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
CAP = "follow_leader"


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


def _login(client, username: str) -> dict:
    """登录取 header；管理员走公共 helper（密码是测试库种子里的 admin123）。"""
    if username == "admin":
        return auth_headers(client)
    r = client.post("/api/login", json={"username": username, "password": _PWD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_user(client, h, username: str) -> dict:
    r = client.post(
        "/api/users", json={"username": username, "password": _PWD}, headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_node(client, h, name: str, mt5_login: int, primary: bool = True) -> str:
    r = client.post(
        "/api/nodes", json={"name": name, "mt5_login": mt5_login}, headers=h,
    )
    assert r.status_code == 201, r.text
    nid = r.json()["node_id"]
    if primary:
        p = client.post(
            f"/api/nodes/{nid}/primary", json={"is_primary": True}, headers=h,
        )
        assert p.status_code == 200, p.text
    return nid


def _assign(client, h, user_id: int, node_ids: list[str], confirm: bool = False):
    return client.put(
        f"/api/users/{user_id}/nodes",
        json={"node_ids": node_ids, "confirm": confirm}, headers=h,
    )


def _grant_capability(client, admin_h, role_code: str, caps: list[str]) -> None:
    roles = client.get("/api/roles", headers=admin_h).json()
    role = next(r for r in roles if r["code"] == role_code)
    r = client.put(
        f"/api/roles/{role['id']}/capabilities",
        json={"capabilities": caps}, headers=admin_h,
    )
    assert r.status_code == 200, r.text


def _make_role(client, admin_h, code: str, name: str, menus: list[str], caps: list[str]) -> dict:
    r = client.post(
        "/api/roles",
        json={
            "code": code, "name": name, "menus": menus, "capabilities": caps,
        },
        headers=admin_h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _bind_role(client, admin_h, user_id: int, role_id: int) -> None:
    r = client.put(
        f"/api/users/{user_id}/roles", json={"role_ids": [role_id]}, headers=admin_h,
    )
    assert r.status_code == 200, r.text


def _group_with_primary(client, h, node_ids: list[str], name: str) -> str:
    """建一个分组并把指定节点放进去（节点必须与调用者同一所有者）。

    用**分组所有者自己的** header 建，管理员 header 只能建管理员名下的分组。
    """
    r = client.post(
        "/api/groups", json={"name": name, "node_ids": node_ids}, headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


# ---------------------------------------------------------------------------
# 能力链路
# ---------------------------------------------------------------------------
def test_follow_capability_registered(client):
    """能力码与菜单码都进了注册表，且【带单】菜单对普通用户可分配。"""
    from app import permissions

    assert CAP in permissions.ALL_CAPABILITY_CODES
    assert permissions.CAP_FOLLOW_LEADER == "follow_leader"
    assert permissions.MENU_FOLLOW in permissions.ALL_MENU_CODES
    # assignable=True -> 默认进内置普通用户角色的菜单种子（默认可以跟单）
    assert permissions.MENU_FOLLOW in permissions.DEFAULT_USER_MENUS


def test_user_out_exposes_capabilities(client):
    """用户列表带 capabilities 字段（前端「能力」列用；管理员固定全部能力）。"""
    admin_h = _login(client, "admin")
    users = client.get("/api/users", headers=admin_h).json()
    admin = next(u for u in users if u["username"] == "admin")
    assert CAP in admin["capabilities"], "管理员应固定拥有全部能力"
    assert permissions_all_caps_subset(admin["capabilities"])


def permissions_all_caps_subset(caps: list[str]) -> bool:
    from app import permissions

    return set(caps) == set(permissions.ALL_CAPABILITY_CODES)


def test_admin_sees_all_capabilities_but_role_cannot_be_edited(client):
    """管理员角色固定全部能力，不接受改写（与 ai_strategy 同口径）。"""
    admin_h = _login(client, "admin")
    roles = client.get("/api/roles", headers=admin_h).json()
    admin_role = next(r for r in roles if r["code"] == "admin")
    assert set(admin_role["capabilities"]) >= {CAP}
    r = client.put(
        f"/api/roles/{admin_role['id']}/capabilities",
        json={"capabilities": []}, headers=admin_h,
    )
    assert r.status_code == 400, r.text


def test_unknown_capability_rejected(client):
    """未知能力码拒收，避免前端拼错就静默生效。"""
    admin_h = _login(client, "admin")
    r = client.put(
        f"/api/roles/1/capabilities",
        json={"capabilities": ["follow_xxx"]}, headers=admin_h,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 带单员列表：能力是准入门槛
# ---------------------------------------------------------------------------
def _setup_leader_and_follower(client, admin_h):
    """建 alice（带单员，授能力）/ bob（普通用户，无能力），各配节点与分组。

    返回 (alice, bob, alice_primary, bob_primary, alice_group)
    """
    alice = _mk_user(client, admin_h, "alice")
    bob = _mk_user(client, admin_h, "bob")

    # 自定义角色：带单员持能力，普通用户角色不持
    leader_role = _make_role(
        client, admin_h, "leader", "带单员", ["follow", "trading", "groups"], [CAP],
    )
    plain_role = _make_role(
        client, admin_h, "plain", "普通客户", ["follow", "trading"], [],
    )
    _bind_role(client, admin_h, alice["id"], leader_role["id"])
    _bind_role(client, admin_h, bob["id"], plain_role["id"])

    # 节点：alice 主节点 + 一个备的；bob 主节点 + 一个可跟单的
    a_primary = _mk_node(client, admin_h, "alice-primary", 90001)
    a_extra = _mk_node(client, admin_h, "alice-extra", 90002, primary=False)
    b_primary = _mk_node(client, admin_h, "bob-primary", 90003)
    b_follow = _mk_node(client, admin_h, "bob-follow", 90004, primary=False)
    assert _assign(client, admin_h, alice["id"], [a_primary, a_extra], True).status_code == 200
    assert _assign(client, admin_h, bob["id"], [b_primary, b_follow], True).status_code == 200

    # alice 的分组包含其主节点 -> 带单源分组
    alice_h = _login(client, "alice")
    a_group = _group_with_primary(
        client, alice_h, [a_primary], "alice-带单组",
    )
    return alice, bob, a_primary, a_extra, b_primary, b_follow, a_group


def test_leader_list_requires_capability(client):
    """有主节点 + 有分组，但**没有 follow_leader 能力**的用户不出现在带单列表。"""
    admin_h = _login(client, "admin")
    _setup_leader_and_follower(client, admin_h)

    # 先撤掉 alice 的能力
    roles = client.get("/api/roles", headers=admin_h).json()
    leader_role = next(r for r in roles if r["code"] == "leader")
    client.put(
        f"/api/roles/{leader_role['id']}/capabilities",
        json={"capabilities": []}, headers=admin_h,
    )

    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    names = {x["username"] for x in r.json()["leaders"]}
    assert "alice" not in names, "无能力不应作为带单员出现"


def test_leader_list_shows_capable_user(client):
    """持能力 + 有主节点 + 有含主节点分组 -> 出现在列表，且包含自己（自跟单）。"""
    admin_h = _login(client, "admin")
    _setup_leader_and_follower(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    leaders = r.json()["leaders"]
    assert [x["username"] for x in leaders] == ["alice"], "仅 alice 是带单员"
    alice = leaders[0]
    assert alice["primary_node"]["node_id"], "应带出主节点摘要"
    assert alice["followable_group_count"] >= 1
    assert alice["my_follow_group_count"] == 0

    # alice 自己看：应能看到自己（自跟单场景）
    alice_h = _login(client, "alice")
    r2 = client.get("/api/follow/leaders", headers=alice_h)
    assert r2.status_code == 200, r2.text
    assert "alice" in {x["username"] for x in r2.json()["leaders"]}, "自己有 follow_leader 能力时应看到自己"


def test_leader_list_unknown_query_returns_empty(client):
    """搜索无结果时返回空列表而不是报错。"""
    admin_h = _login(client, "admin")
    _setup_leader_and_follower(client, admin_h)
    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders", params={"q": "不存在的名字"}, headers=bob_h)
    assert r.status_code == 200, r.text
    assert r.json()["leaders"] == []


# ---------------------------------------------------------------------------
# 加入跟单：默认人人可做，不需要 follow_leader
# ---------------------------------------------------------------------------
def test_join_requires_no_leader_capability(client):
    """bob 没有 follow_leader 能力，但**可以正常跟单**（跟单方向默认开放）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [a_group],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["leader_primary_node_id"] == a_primary
    assert data["joined"][0]["node_ids"] == [b_follow], "b_follow 应加入成功"
    assert data["skipped"] == [], "不应有跳过项"


def test_join_rejects_primary_node(client):
    """主节点不能用于跟单（主节点是带单源，必须保持独立）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [a_group],
            "node_ids": [b_primary],  # bob 的主节点
        },
        headers=bob_h,
    )
    # 没有可用节点 -> 400
    assert r.status_code == 400, r.text
    assert "主节点" in r.json()["detail"]


def test_join_rejects_foreign_node(client):
    """别人的节点一律拒收（归属校验，不泄露存在性）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [a_group],
            "node_ids": [a_extra],  # alice 的节点
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text
    assert "不属于" in r.json()["detail"] or "没有可用于跟单" in r.json()["detail"]


def test_join_rejects_group_without_leader_primary(client):
    """带单员名下不含其主节点的分组不是带单源分组 -> 404。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    alice_h = _login(client, "alice")
    # alice 建一个只含 a_extra、不含主节点的分组
    other = _group_with_primary(client, alice_h, [a_extra], "alice-手工组")

    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"], "group_ids": [other], "node_ids": [b_follow],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text  # 没有可加入的分组


def test_join_is_idempotent(client):
    """重复加入同一节点进同一分组：幂等（already），不报错。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    body = {
        "leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow],
    }
    r1 = client.post("/api/follow/join", json=body, headers=bob_h)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/follow/join", json=body, headers=bob_h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["joined"][0]["already_node_ids"] == [b_follow], "第二次应为幂等跳过"


# ---------------------------------------------------------------------------
# 自跟单：自己的非主节点跟自己的带单源分组
# ---------------------------------------------------------------------------
def test_self_follow_own_group_with_own_backup_node(client):
    """自己的非主节点可以跟自己的分组（带单界面里能看到自己并加入）。"""
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, a_extra, _b_primary, _b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    alice_h = _login(client, "alice")

    # 带单列表里有自己
    lst = client.get("/api/follow/leaders", headers=alice_h)
    assert lst.status_code == 200, lst.text
    assert alice["id"] in {x["leader_user_id"] for x in lst.json()["leaders"]}

    # 详情里能拿到自己名下含主节点的分组
    det = client.get(f"/api/follow/leaders/{alice['id']}", headers=alice_h)
    assert det.status_code == 200, det.text
    assert a_group in {g["group_id"] for g in det.json()["groups"]}
    # 可用于跟单的节点里不含主节点，但含自己的备节点
    my_node_ids = {n["node_id"] for n in det.json()["my_nodes"]}
    assert a_primary not in my_node_ids, "主节点不可用于跟单"
    assert a_extra in my_node_ids, "自己的非主节点应可用于跟单"

    # 加入：自己 = 带单员
    r = client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [a_extra]},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["leader_primary_node_id"] == a_primary
    assert data["joined"][0]["node_ids"] == [a_extra]
    assert data["skipped"] == []

    # 我的跟单里能看到这条自跟单
    me = client.get("/api/follow/me", headers=alice_h)
    assert me.status_code == 200, me.text
    mine = me.json()["following"]
    assert len(mine) == 1, "应有一条指向自己的跟单"
    assert mine[0]["leader"]["leader_user_id"] == alice["id"]
    assert mine[0]["groups"][0]["group_id"] == a_group
    assert {n["node_id"] for n in mine[0]["groups"][0]["my_nodes"]} == {a_extra}


def test_self_follow_rejects_primary_node(client):
    """自跟单也不能用自己的主节点（主节点是带单源，必须保持独立）。"""
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, _a_extra, _b_primary, _b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    alice_h = _login(client, "alice")
    r = client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [a_primary]},
        headers=alice_h,
    )
    assert r.status_code == 400, r.text
    assert "主节点" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 我的跟单 / 移出
# ---------------------------------------------------------------------------
def test_my_following_and_leave(client):
    """加入后 GET /api/follow/me 能看到；leave 后消失。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.status_code == 200, me.text
    data = me.json()
    assert data["summary"]["following_count"] == 1
    assert data["summary"]["follow_node_count"] == 1
    assert data["following"][0]["leader"]["username"] == "alice"
    assert data["following"][0]["groups"][0]["my_nodes"][0]["node_id"] == b_follow
    assert data["following"][0]["groups"][0]["marketable"] is True

    lv = client.post(
        "/api/follow/leave",
        json={
            "leader_user_id": alice["id"], "group_ids": [a_group],
            "node_ids": [b_follow], "close_tasks": False,
        },
        headers=bob_h,
    )
    assert lv.status_code == 200, lv.text
    me2 = client.get("/api/follow/me", headers=bob_h)
    assert me2.json()["summary"]["follow_node_count"] == 0


def test_leave_works_after_leader_capability_revoked(client):
    """带单员被撤销能力后，跟单员**仍能自行移出**（清理不需要能力）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    # 撤销 alice 的能力
    roles = client.get("/api/roles", headers=admin_h).json()
    leader_role = next(r for r in roles if r["code"] == "leader")
    client.put(
        f"/api/roles/{leader_role['id']}/capabilities",
        json={"capabilities": []}, headers=admin_h,
    )
    # 我的跟单：仍在，但标为不可再收新信号
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["following"][0]["groups"][0]["marketable"] is False
    assert me.json()["following"][0]["groups"][0]["unmarketable_reason"]
    # 仍可自行移出
    lv = client.post(
        "/api/follow/leave",
        json={
            "leader_user_id": alice["id"], "group_ids": [a_group],
            "node_ids": [b_follow], "close_tasks": False,
        },
        headers=bob_h,
    )
    assert lv.status_code == 200, lv.text


# ---------------------------------------------------------------------------
# 详情接口
# ---------------------------------------------------------------------------
def test_leader_detail_returns_groups_and_my_nodes(client):
    """详情：分组含我的已跟标记；我的可跟单节点**排除主节点**。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    d = client.get(f"/api/follow/leaders/{alice['id']}", headers=bob_h)
    assert d.status_code == 200, d.text
    data = d.json()
    assert data["leader"]["username"] == "alice"
    gid = next(g["group_id"] for g in data["groups"] if g["group_id"] == a_group)
    grp = next(g for g in data["groups"] if g["group_id"] == a_group)
    assert grp["my_follow_node_ids"] == [b_follow], "应标出我已跟的节点"
    my_ids = {n["node_id"] for n in data["my_nodes"]}
    assert b_follow in my_ids
    assert b_primary not in my_ids, "我的主节点不应出现在可跟单节点里"


def test_leader_detail_404_for_unknown(client):
    """不存在的带单员 -> 404。"""
    admin_h = _login(client, "admin")
    bob = _mk_user(client, admin_h, "bob")
    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders/999999", headers=bob_h)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 跟单关系不被 owner 编辑成员误删
# ---------------------------------------------------------------------------
def test_owner_edit_members_keeps_followers(client):
    """owner 编辑分组成员（node_ids 整体替换）不应删掉跟单关系。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    # alice 编辑自己的分组成员（替换为 a_extra）
    alice_h = _login(client, "alice")
    upd = client.patch(
        f"/api/groups/{a_group}", json={"node_ids": [a_primary, a_extra]}, headers=alice_h,
    )
    assert upd.status_code == 200, upd.text
    # 跟单关系仍在
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["summary"]["follow_node_count"] == 1, "编辑成员不应删掉跟单关系"


# ---------------------------------------------------------------------------
# 生命周期连带清理
# ---------------------------------------------------------------------------
def test_delete_group_cascades_followings(client):
    """分组被删 -> 跟单关系级联清理，不留脏数据。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    alice_h = _login(client, "alice")
    d = client.delete(f"/api/groups/{a_group}", headers=alice_h)
    assert d.status_code == 200, d.text
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["summary"]["follow_node_count"] == 0, "分组删除应级联清理跟单"


def test_delete_node_cascades_followings(client):
    """节点被删 -> 跟单关系清理。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    d = client.delete(f"/api/nodes/{b_follow}", headers=admin_h)
    assert d.status_code == 200, d.text
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["summary"]["follow_node_count"] == 0, "节点删除应清理跟单"


def test_set_primary_cascades_followings(client):
    """跟单节点升为主节点 -> 跟单关系自动清理（主节点不可跟单）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["summary"]["follow_node_count"] == 1
    # 把 b_follow 设为主节点
    p = client.post(
        f"/api/nodes/{b_follow}/primary", json={"is_primary": True}, headers=admin_h,
    )
    assert p.status_code == 200, p.text
    me2 = client.get("/api/follow/me", headers=bob_h)
    assert me2.json()["summary"]["follow_node_count"] == 0, "升主节点应清理跟单"


# ---------------------------------------------------------------------------
# 纯规则（follow_rules）
# ---------------------------------------------------------------------------
def test_follow_rules_group_contains_primary():
    from app import follow_rules

    g = {"members": [{"node_id": "n_a"}, {"node_id": "n_b"}]}
    assert follow_rules.group_contains_primary(g, "n_a") is True
    assert follow_rules.group_contains_primary(g, "n_zzz") is False
    assert follow_rules.group_contains_primary(g, None) is False


def test_follow_rules_participant_ids_order_and_dedup():
    from app import group_rules

    g = {
        "members": [{"node_id": "n_a"}, {"node_id": "n_b"}],
        "followers": [{"node_id": "n_c"}, {"node_id": "n_a"}],  # n_a 重复
    }
    assert group_rules.participant_ids(g) == ["n_a", "n_b", "n_c"], "owner 成员在前，跟单在后，去重"


def test_follow_rules_effective_includes_followers():
    from app import group_rules

    g = {
        "members": [{"node_id": "n_a"}],
        "followers": [{"node_id": "n_c"}],
    }
    nodes = {"n_a": {"enabled": True}, "n_c": {"enabled": True}}
    online = {"n_a", "n_c"}
    assert group_rules.effective_node_ids(g, nodes, online) == ["n_a", "n_c"]
    # 跟单节点离线 -> 不参与
    assert group_rules.effective_node_ids(g, nodes, {"n_a"}) == ["n_a"]
    # owner_only 口径不含跟单
    assert group_rules.owner_effective_node_ids(g, nodes, online) == ["n_a"]


def test_follow_rules_marketable_reason():
    from app import follow_rules

    g = {"members": [{"node_id": "n_a"}], "enabled": True}
    assert follow_rules.marketable_reason(g, True, "n_a") is None
    assert "带单" in follow_rules.marketable_reason(g, False, "n_a")
    assert "主节点" in follow_rules.marketable_reason(g, True, "n_other")


def test_follow_rules_node_join_reject():
    from app import follow_rules

    # 主节点
    assert "主节点" in follow_rules.node_join_reject_reason(
        {"node_id": "n", "is_primary": True}, existing_group=None, group_id="g",
    )
    # 停用
    assert "停用" in follow_rules.node_join_reject_reason(
        {"node_id": "n", "enabled": False}, existing_group=None, group_id="g",
    )
    # 重复
    assert "已在此分组跟单" in follow_rules.node_join_reject_reason(
        {"node_id": "n", "enabled": True},
        existing_group={"followers": [{"node_id": "n"}]}, group_id="g",
    )
    # 正常
    assert follow_rules.node_join_reject_reason(
        {"node_id": "n", "enabled": True}, existing_group=None, group_id="g",
    ) is None