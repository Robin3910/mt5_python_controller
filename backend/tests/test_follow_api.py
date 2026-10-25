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
import time

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


def test_set_primary_blocked_by_followings(client):
    """跟单节点升为主节点 -> **被拒绝**（主节点不可跟单，跟单关系不能被静默清掉）。"""
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

    # 把仍在跟单的 b_follow 设为主节点 -> 应被拒绝
    p = client.post(
        f"/api/nodes/{b_follow}/primary", json={"is_primary": True}, headers=admin_h,
    )
    assert p.status_code == 409, p.text
    detail = p.json()["detail"]
    assert detail["code"] == "primary_switch_following_blocked", detail
    assert detail["blocking_followings"], "应带跟单明细"
    assert any(
        f["group_id"] == a_group for f in detail["blocking_followings"]
    ), f"明细应含被跟的分组：{detail}"

    # 关键：跟单关系必须**原样保留**，不能被静默清掉
    me2 = client.get("/api/follow/me", headers=bob_h)
    assert me2.json()["summary"]["follow_node_count"] == 1, "被拒绝时跟单关系不能丢"

    # 节点也没有被升为主节点
    nodes = client.get("/api/nodes", headers=admin_h).json()
    assert not any(
        n["node_id"] == b_follow and n["is_primary"] for n in nodes
    ), "被拒绝时不应升为主节点"


def test_set_primary_allowed_after_leaving_followings(client):
    """移出跟单后 -> 同一节点即可正常设为主节点（限制只拦当下，不永久封禁）。"""
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
    blocked = client.post(
        f"/api/nodes/{b_follow}/primary", json={"is_primary": True}, headers=admin_h,
    )
    assert blocked.status_code == 409, blocked.text

    # 移出跟单
    leave = client.post(
        "/api/follow/leave",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    assert leave.status_code == 200, leave.text

    ok = client.post(
        f"/api/nodes/{b_follow}/primary", json={"is_primary": True}, headers=admin_h,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["is_primary"] is True


def test_set_primary_precheck_endpoint(client):
    """GET /api/nodes/{id}/followings 预查接口：有跟单返回明细，无跟单返回空。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")

    # 还没跟单 -> count 0
    r0 = client.get(f"/api/nodes/{b_follow}/followings", headers=admin_h)
    assert r0.status_code == 200, r0.text
    assert r0.json()["count"] == 0, r0.text

    client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    r1 = client.get(f"/api/nodes/{b_follow}/followings", headers=admin_h)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["count"] == 1, body
    assert body["items"][0]["group_id"] == a_group, body


def test_set_primary_unaffected_node_no_following(client):
    """没有跟单的节点升主 -> 正常放行（限制不能误伤普通节点）。"""
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
    # b_primary 没有跟单 -> 可以升主
    p = client.post(
        f"/api/nodes/{b_primary}/primary", json={"is_primary": True}, headers=admin_h,
    )
    assert p.status_code == 200, p.text
    assert p.json()["is_primary"] is True
    # bob 的跟单关系不受影响
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.json()["summary"]["follow_node_count"] == 1


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


# ---------------------------------------------------------------------------
# 认领跟单节点（带单员新建分组时继承跟过自己的节点）
# ---------------------------------------------------------------------------
def _adopt(client, group_id: str, headers):
    return client.post(f"/api/groups/{group_id}/adopt-followers", headers=headers)


def _followers_of(client, group_id: str, headers) -> list[str]:
    r = client.get(f"/api/groups/{group_id}", headers=headers)
    assert r.status_code == 200, r.text
    return [f["node_id"] for f in (r.json().get("followers") or [])]


def test_adopt_followers_inherits_across_groups(client):
    """新建分组认领继承：跟单员在新分组里自动出现，且 follower_user_id 取自源关系。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text

    # alice 换品种 -> 全新分组（分组名按 策略类型-用户名-MT5号-品种 生成，任一维度变化即新组）
    alice_h = _login(client, "alice")
    new_group = _group_with_primary(client, alice_h, [a_primary], "trend_market-alice-90001-XAUUSD")

    resp = _adopt(client, new_group, alice_h)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [a["node_id"] for a in data["adopted"]] == [b_follow]
    assert data["adopted"][0]["follower_user_id"] == bob["id"], "follower_user_id 必须取自源关系"
    assert data["adopted"][0]["from_group_id"] == a_group
    assert data["scanned_groups"] == 1

    # 新分组真的带上跟单节点
    assert _followers_of(client, new_group, alice_h) == [b_follow]

    # bob 在【我的跟单】里能看到这条新关系（list_followings 按 follower_user_id 过滤）
    me = client.get("/api/follow/me", headers=bob_h)
    assert me.status_code == 200, me.text
    groups = [g for lg in me.json()["following"] for g in lg["groups"]]
    assert new_group in [g["group_id"] for g in groups]


def test_adopt_followers_is_idempotent(client):
    """重复认领幂等：第二次 adopted 为空，不产生重复关系。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    alice_h = _login(client, "alice")
    new_group = _group_with_primary(client, alice_h, [a_primary], "grid-alice-90001-XAUUSD")
    first = _adopt(client, new_group, alice_h).json()
    assert len(first["adopted"]) == 1

    second = _adopt(client, new_group, alice_h).json()
    assert second["adopted"] == [], "第二次不应重复写入"
    assert [s["node_id"] for s in second["skipped"]] == [b_follow]
    assert _followers_of(client, new_group, alice_h) == [b_follow], "关系只有一条"


def test_adopt_followers_ignores_disabled_source_group(client):
    """已确认决策：源分组被禁用时，其中的跟单节点仍应继承。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    # 禁用源分组
    alice_h = _login(client, "alice")
    dis = client.patch(f"/api/groups/{a_group}", json={"enabled": False}, headers=alice_h)
    assert dis.status_code == 200, dis.text

    new_group = _group_with_primary(client, alice_h, [a_primary], "trend_market-alice-90001-EURUSD")
    data = _adopt(client, new_group, alice_h).json()
    assert [a["node_id"] for a in data["adopted"]] == [b_follow], "禁用不影响继承"
    assert _followers_of(client, new_group, alice_h) == [b_follow]


def test_adopt_followers_rejects_foreign_group(client):
    """两道边界都要守住：没有「分组管理」菜单 -> 403；是别人名下的分组 -> 404。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    new_group = _group_with_primary(
        client, _login(client, "alice"), [a_primary], "trend_market-alice-90001-XAUUSD",
    )

    # bob 的角色只有 follow / trading，没有分组管理菜单 —— 菜单权限先挡下来
    assert _adopt(client, new_group, _login(client, "bob")).status_code == 403

    # 给 bob 分组管理菜单后，他仍不能认领 alice 名下的分组 -> 404，不泄露存在性
    groups_role = _make_role(
        client, admin_h, "plain_groups", "普通客户(分组)", ["follow", "trading", "groups"], [],
    )
    _bind_role(client, admin_h, bob["id"], groups_role["id"])
    bob_h = _login(client, "bob")
    resp = _adopt(client, new_group, bob_h)
    assert resp.status_code == 404, resp.text
    # 认领不存在的分组同样 404（不区分「不存在」与「非本人」）
    assert _adopt(client, "grp_missing", bob_h).status_code == 404


def test_adopt_followers_only_uses_existing_followings(client):
    """决策 2：只搬运已存在的跟单关系，不会凭空引入没跟过我的节点。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    # bob 只往 a_extra 建的空分组跟（该组不含 alice 主节点，不是带单源分组）
    alice_h = _login(client, "alice")
    new_group = _group_with_primary(client, alice_h, [a_primary], "trend_market-alice-90001-XAUUSD")

    data = _adopt(client, new_group, alice_h).json()
    assert data["adopted"] == [], "没有任何已存在的跟单关系就不应写入"
    assert _followers_of(client, new_group, alice_h) == []

    # 现在 bob 跟了 a_group，新分组就应继承
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200
    assert [a["node_id"] for a in _adopt(client, new_group, alice_h).json()["adopted"]] == [b_follow]


def test_adopt_followers_dedupes_multi_group_followers(client):
    """同一节点跟了 alice 的多个分组时，只继承一条（uq_follow_group_node 只保证组内唯一）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    alice_h = _login(client, "alice")
    # alice 的第二个带单源分组
    a_group2 = _group_with_primary(client, alice_h, [a_primary], "grid-alice-90001-EURUSD")

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [a_group, a_group2],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    ).status_code == 200

    new_group = _group_with_primary(client, alice_h, [a_primary], "grid-alice-90001-XAUUSD")
    data = _adopt(client, new_group, alice_h).json()
    assert len(data["adopted"]) == 1, "同一节点只应继承一次"
    assert data["adopted"][0]["node_id"] == b_follow
    assert _followers_of(client, new_group, alice_h) == [b_follow], "不得产生重复行"


def test_adopt_followers_skips_group_without_primary(client):
    """目标分组不含带单员主节点时，不做任何继承（不是带单源分组）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, a_extra, b_primary, b_follow, a_group = _setup_leader_and_follower(
        client, admin_h,
    )
    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [a_group], "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    alice_h = _login(client, "alice")
    # 手工分组：只有 a_extra，没有主节点
    manual = _group_with_primary(client, alice_h, [a_extra], "alice-手工组")
    data = _adopt(client, manual, alice_h).json()
    assert data["scanned_groups"] == 1, "来源只含 a_group（手工组不含主节点，不算来源）"
    assert [a["from_group_id"] for a in data["adopted"]] == [a_group]
    assert _followers_of(client, manual, alice_h) == [b_follow]

# ---------------------------------------------------------------------------
# 跟单市场隔离（带单 tab 只收趋势 / 网格，AI 加仓走【AI 交易】tab）
# ---------------------------------------------------------------------------
def _mk_strategy(client, h, name: str, template_id: str, symbol: str = "XAUUSD") -> str:
    """建策略；rules 不传则用模版默认规则。

    网格模版（tpl_3）的默认规则不合法（格距 / 手数为 0），必须显式给一条可用规则。
    """
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


def _mk_group_with_strategy(
    client, h, node_ids: list[str], name: str, strategy_id: str,
    *, accept_ai_follow: bool = False,
) -> str:
    r = client.post(
        "/api/groups",
        json={
            "name": name, "node_ids": node_ids, "strategy_id": strategy_id,
            "accept_ai_follow": accept_ai_follow,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


def _setup_ai_leader(client, admin_h):
    """alice 同时是带单员与 AI 主人：名下有一个 tpl_1（AI）分组 + 一个 tpl_3（网格）分组。

    bob 是普通跟单员（持 follow_leader 无关，有 follow 菜单即可）。
    返回 (alice, bob, a_primary, b_follow, ai_group, grid_group)
    """
    alice = _mk_user(client, admin_h, "alice")
    bob = _mk_user(client, admin_h, "bob")

    leader_role = _make_role(
        client, admin_h, "leader", "带单员",
        ["follow", "trading", "groups", "ai_trade"],
        [CAP, "ai_strategy"],
    )
    plain_role = _make_role(
        client, admin_h, "plain", "普通客户",
        ["follow", "trading", "groups", "ai_trade"], [],
    )
    _bind_role(client, admin_h, alice["id"], leader_role["id"])
    _bind_role(client, admin_h, bob["id"], plain_role["id"])

    a_primary = _mk_node(client, admin_h, "alice-primary", 91001)
    _mk_node(client, admin_h, "alice-extra", 91002, primary=False)
    b_primary = _mk_node(client, admin_h, "bob-primary", 91003)
    b_follow = _mk_node(client, admin_h, "bob-follow", 91004, primary=False)
    assert _assign(client, admin_h, alice["id"], [a_primary], True).status_code == 200
    assert _assign(client, admin_h, bob["id"], [b_primary, b_follow], True).status_code == 200

    alice_h = _login(client, "alice")
    ai_sid = _mk_strategy(client, alice_h, "alice-ai", "tpl_1")
    grid_sid = _mk_strategy(client, alice_h, "alice-grid", "tpl_3", symbol="EURUSD")
    # AI 分组必须开放 AI 跟单（accept_ai_follow 默认关），否则进不了机器人广场
    ai_group = _mk_group_with_strategy(
        client, alice_h, [a_primary], "alice-AI组", ai_sid, accept_ai_follow=True,
    )
    grid_group = _mk_group_with_strategy(
        client, alice_h, [a_primary], "alice-网格组", grid_sid,
    )
    return alice, bob, a_primary, b_follow, ai_group, grid_group


def test_leader_list_excludes_ai_only_leader(client):
    """带单员**只有** tpl_1 分组 -> 不出现在【带单】列表（AI 市场不外露到带单 tab）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    # 把网格分组删掉，只留 AI 分组
    alice_h = _login(client, "alice")
    d = client.delete(f"/api/groups/{grid_group}", headers=alice_h)
    assert d.status_code == 200, d.text

    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    names = {x["username"] for x in r.json()["leaders"]}
    assert "alice" not in names, "只有 AI 分组的带单员不应出现在带单列表"


def test_leader_list_counts_only_follow_market_groups(client):
    """带单员同时有 AI 分组与网格分组 -> 只计入网格分组（AI 分组不计入可跟数）。"""
    admin_h = _login(client, "admin")
    _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    leaders = r.json()["leaders"]
    alice = next(x for x in leaders if x["username"] == "alice")
    assert alice["followable_group_count"] == 1, "只有网格分组计入带单市场"


def test_follow_join_rejects_ai_group(client):
    """POST /api/follow/join 加入 tpl_1 分组 -> 400（市场互斥，写入侧拦截）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [ai_group],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    )
    assert r.status_code == 400, r.text


def test_leader_detail_hides_ai_group_but_shows_hidden(client):
    """详情：AI 分组不进 groups，但进 hidden_groups 且标不可选（存量关系需人工清理）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.get(f"/api/follow/leaders/{alice['id']}", headers=bob_h)
    assert r.status_code == 200, r.text
    body = r.json()
    visible = {g["group_id"] for g in body["groups"]}
    hidden = {g["group_id"] for g in body.get("hidden_groups") or []}
    assert ai_group not in visible, "AI 分组不得出现在可跟列表"
    assert ai_group in hidden, "AI 分组应出现在 hidden_groups"
    assert grid_group in visible
    hid = next(g for g in body["hidden_groups"] if g["group_id"] == ai_group)
    assert hid["selectable"] is False
    assert hid["unselectable_reason"], "不可选必须给人读原因"


def test_leader_detail_groups_expose_symbol(client):
    """详情分组必须带出交易品种（可跟 + 市场外都要），供前端展示「他交易什么」。

    symbol 来自策略快照；市场外分组（hidden_groups）同样要带，
    否则用户在详情页看到的品种会漏掉 AI 那部分。
    """
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.get(f"/api/follow/leaders/{alice['id']}", headers=bob_h)
    assert r.status_code == 200, r.text
    body = r.json()

    vis = next(g for g in body["groups"] if g["group_id"] == grid_group)
    assert vis["symbol"] == "EURUSD", "可跟分组应带出策略品种"

    hid = next(g for g in body["hidden_groups"] if g["group_id"] == ai_group)
    assert hid["symbol"] == "XAUUSD", "市场外分组也要带品种，否则展示会漏"


def test_my_following_marks_ai_group_unmarketable(client):
    """存量关系：AI 分组仍出现在【我的跟单】，但 marketable=false 且原因指向【AI 交易】。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    # 先用 AI tab 加入（带单 tab 加不进去）
    bob_h = _login(client, "bob")
    j = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [ai_group],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    )
    assert j.status_code == 200, j.text

    r = client.get("/api/follow/me", headers=bob_h)
    assert r.status_code == 200, r.text
    groups = [
        g for f in r.json()["following"] for g in f["groups"]
        if g["group_id"] == ai_group
    ]
    assert groups, "存量 AI 跟单关系应仍可见"
    assert groups[0]["marketable"] is False
    assert "AI" in groups[0]["unmarketable_reason"]


# ---------------------------------------------------------------------------
# 「已跟」计数必须按市场隔离（本市场内我已跟的分组/节点数）
# ---------------------------------------------------------------------------
def test_leader_list_my_follow_count_excludes_ai_market(client):
    """【带单广场】只跟了 AI 分组 -> 带单卡片**不得**显示「已跟」。

    与 AI 侧对称：两个市场共用同一张跟单关系表，带单侧的计数也必须
    按市场过滤，否则只跟了 AI 分组的客户会在带单页看到误导性的「已跟」。
    """
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/ai-follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [ai_group],
              "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    r = client.get("/api/follow/leaders", headers=bob_h)
    assert r.status_code == 200, r.text
    leader = next(x for x in r.json()["leaders"] if x["leader_user_id"] == alice["id"])
    assert leader["my_follow_node_count"] == 0, "跟了 AI 分组不代表跟了带单策略"
    assert leader["my_follow_group_count"] == 0


def test_leader_detail_my_follow_count_excludes_ai_market(client):
    """【带单详情】同样按市场隔离：只跟 AI 分组时计数为 0。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/ai-follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [ai_group],
              "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    d = client.get(f"/api/follow/leaders/{alice['id']}", headers=bob_h)
    assert d.status_code == 200, d.text
    leader = d.json()["leader"]
    assert leader["my_follow_node_count"] == 0
    assert leader["my_follow_group_count"] == 0


def test_my_following_counts_only_follow_market(client):
    """【我的跟单】的 leader 计数只算带单市场（两个市场都跟时不被 AI 市场撑大）。

    存量 AI 关系**仍要可见**（便于人工清理），但不能计入带单市场的计数。
    """
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [grid_group],
              "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200
    assert client.post(
        "/api/ai-follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [ai_group],
              "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    r = client.get("/api/follow/me", headers=bob_h)
    assert r.status_code == 200, r.text
    body = r.json()
    leader = body["following"][0]["leader"]
    assert leader["my_follow_group_count"] == 1, "带单侧只有 1 个分组"
    assert leader["my_follow_node_count"] == 1, "带单侧只有 1 个跟单节点"
    assert body["summary"]["follow_node_count"] == 1
    # 存量 AI 关系仍可见（人工清理需要）
    gids = [g["group_id"] for f in body["following"] for g in f["groups"]]
    assert ai_group in gids


def test_leader_list_my_follow_count_reflects_own_market(client):
    """真正跟了带单市场分组时，带单广场计数必须 > 0（别把正常场景一起改没了）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={"leader_user_id": alice["id"], "group_ids": [grid_group],
              "node_ids": [b_follow]},
        headers=bob_h,
    ).status_code == 200

    r = client.get("/api/follow/leaders", headers=bob_h)
    leader = next(x for x in r.json()["leaders"] if x["leader_user_id"] == alice["id"])
    assert leader["my_follow_node_count"] == 1
    assert leader["my_follow_group_count"] == 1

    d = client.get(f"/api/follow/leaders/{alice['id']}", headers=bob_h)
    assert d.json()["leader"]["my_follow_node_count"] == 1


def test_adopt_followers_does_not_cross_market(client):
    """跨市场隔离：AI 分组不得继承网格分组的跟单节点（否则静默拉进 AI 加仓）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    # bob 先跟网格分组（同一市场）
    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [grid_group],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    ).status_code == 200

    alice_h = _login(client, "alice")
    # 认领继承：AI 分组不应拿到网格分组的跟单节点
    data = _adopt(client, ai_group, alice_h).json()
    assert data["adopted"] == [], "AI 分组不得继承网格分组的跟单节点"
    assert _followers_of(client, ai_group, alice_h) == []


def test_adopt_followers_still_works_within_same_market(client):
    """同市场仍继承：网格分组认领继承网格分组的跟单节点（回归）。"""
    admin_h = _login(client, "admin")
    alice, bob, a_primary, b_follow, ai_group, grid_group = _setup_ai_leader(client, admin_h)

    bob_h = _login(client, "bob")
    assert client.post(
        "/api/follow/join",
        json={
            "leader_user_id": alice["id"],
            "group_ids": [grid_group],
            "node_ids": [b_follow],
        },
        headers=bob_h,
    ).status_code == 200

    alice_h = _login(client, "alice")
    # 一个策略只能绑一个分组 -> 另建一个同模版的网格策略来承载新分组
    grid_sid2 = _mk_strategy(client, alice_h, "alice-grid-2", "tpl_3", symbol="EURUSD")
    another = _mk_group_with_strategy(
        client, alice_h, [a_primary], "alice-网格组2", grid_sid2,
    )
    data = _adopt(client, another, alice_h).json()
    assert [a["node_id"] for a in data["adopted"]] == [b_follow], "同市场应继续继承"


# ---------------------------------------------------------------------------
# 绩效口径：**只算主节点**
# ---------------------------------------------------------------------------
# 口径回归测试。带单绩效曾经聚合「名下全部节点」，导致广场上给跟单员看的
# 数字与他实际承担的风险脱节（副节点可能是闲置盘 / 测试盘）。现在口径 =
# 主节点 —— 与带单源、与 AI 机器人页一致。
#
# 造数策略：主节点与副节点**故意给出可区分的盈亏**（主节点亏、副节点赚），
# 这样全节点口径与主节点口径的结果必然不同，断言才能真正卡住口径。
def _seed_deal(node_id: str, deal_id: int, *, profit: float, equity: float,
               position_id: int = 1, at: int | None = None,
               entry_type: str = "OUT") -> None:
    """往 mt5_deal 插一笔成交（统计口径测试用）。

    出场类 entry 才有「已实现盈亏」与胜率分母（见 deal_stats.OUT_ENTRIES）；
    持仓级去重靠 position_id，所以同一持仓的多次出场算一笔。

    `at` 指定成交时间（Unix 秒），默认现在 —— 日序列测试需要把成交
    放到「昨天 / 前天」这类特定自然日里。

    走 `deal_store.save_deals`（节点上报的同一条路径），而不是直接 ORM：
    免得测试绕过归一化（_normalize）造出线上不可能出现的数据。

    本文件的 `client` fixture 是**同步** TestClient，主线程此刻没有 event loop，
    自己 `asyncio.get_event_loop()` 在全量跑时会被前面的用例关掉而炸掉；
    所以借 TestClient 自己那个 portal 把协程丢进去跑（同一线程、同一 loop）。
    """
    from anyio.from_thread import start_blocking_portal

    async def _go():
        from app import deal_store
        await deal_store.save_deals(
            node_id,
            [{
                "deal_id": deal_id,
                "position_id": position_id,
                "symbol": "EURUSD",
                "entry_type": entry_type,
                "direction": "SELL",
                "volume": 0.1,
                "price": 1.1,
                "profit": profit,
                "swap": 0.0,
                "commission": 0.0,
                "time": int(at if at is not None else time.time()),
            }],
            account={"balance": equity, "equity": equity},
        )

    with start_blocking_portal() as portal:
        portal.call(_go)


def test_leader_performance_counts_primary_node_only(client):
    """带单绩效只算主节点：副节点的盈利不得计入（核心口径回归）。"""
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, a_extra, *_ = _setup_leader_and_follower(client, admin_h)

    # 主节点亏 100，副节点赚 500 —— 两口径结果必然不同（-100 vs +400）
    _seed_deal(a_primary, 5001, profit=-100.0, equity=10000.0, position_id=1)
    _seed_deal(a_extra, 5002, profit=500.0, equity=10000.0, position_id=1)

    bob_h = _login(client, "bob")
    r = client.get(
        f"/api/deals/follow/leaders/{alice['id']}/performance",
        params={"window": "all"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    perf = r.json()["performance"]

    assert perf["data_available"] is True, "主节点有成交，应视为有数据"
    assert perf["total_profit"] == pytest.approx(-100.0), (
        f"绩效必须只含主节点的 -100；混入副节点会变成 +400，实际 {perf}"
    )
    assert perf["total_trades"] == 1, "笔数也应只计主节点的持仓"


def test_leader_performance_without_primary_node_is_unavailable(client):
    """无主节点 -> data_available=False（暂无数据），而不是 0。"""
    admin_h = _login(client, "admin")
    # 借 _setup 造出 alice/bob（bob 用来读接口），再补一个「只有副节点」的带单员 carol
    _setup_leader_and_follower(client, admin_h)
    leader_role = _make_role(
        client, admin_h, "leader2", "带单员2", ["follow", "trading", "groups"], [CAP],
    )
    carol = _mk_user(client, admin_h, "carol")
    _bind_role(client, admin_h, carol["id"], leader_role["id"])
    c_extra = _mk_node(client, admin_h, "carol-extra", 90011, primary=False)
    assert _assign(client, admin_h, carol["id"], [c_extra], True).status_code == 200

    bob_h = _login(client, "bob")
    r = client.get(
        f"/api/deals/follow/leaders/{carol['id']}/performance",
        params={"window": "all"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    perf = r.json()["performance"]
    assert perf["data_available"] is False, "无主节点就是无从统计，应显示暂无数据"
    assert perf["total_profit"] == 0.0


def test_leader_detail_fallback_matches_primary_only(client):
    """详情兜底（缓存未命中）与列表口径一致 —— 防「改了预聚合忘了改兜底」。

    详情接口在 `stats:leader:{id}` 缓存未命中时会就地算一次；那条路径必须
    同样只算主节点，否则会出现「列表 -100 / 点进详情 +400」的分裂。
    """
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, a_extra, *_ = _setup_leader_and_follower(client, admin_h)

    _seed_deal(a_primary, 6001, profit=-100.0, equity=10000.0, position_id=1)
    _seed_deal(a_extra, 6002, profit=500.0, equity=10000.0, position_id=1)

    bob_h = _login(client, "bob")
    # 预聚合 worker 在测试里没跑，缓存必然未命中 -> 走兜底路径
    r = client.get(
        f"/api/follow/leaders/{alice['id']}",
        params={"window": "all"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    perf = r.json()["leader"]["performance"]
    assert perf["total_profit"] == pytest.approx(-100.0), (
        "详情兜底也必须只算主节点"
    )


# ---------------------------------------------------------------------------
# 近 7 日收益序列（柱状图数据源）
# ---------------------------------------------------------------------------
# 核心保证：**柱状图与页面头部的「累计收益」必须对得上**。
# 两者一旦不一致，用户会立刻怀疑整个页面的数据可信度 —— 所以下面第一条
# 用例直接把「7 根柱子的和 === 7d 窗口的 total_profit」钉死。


def _day_start_utc8(offset_days: int = 0) -> int:
    """UTC+8 的某个自然日 00:00（Unix 秒）。offset_days=0 表示今天。"""
    day = 86400
    offset = 8 * 3600
    now = int(time.time())
    today = ((now + offset) // day) * day - offset
    return today - offset_days * day


def test_leader_detail_daily_profit_matches_7d_total(client):
    """7 根柱子的和 === 7d 窗口的 total_profit（口径一致红线）。"""
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, a_extra, *_ = _setup_leader_and_follower(client, admin_h)

    # 三笔分布在三天里：今天 +120，昨天 -30，前天 +50
    _seed_deal(a_primary, 7001, profit=120.0, equity=10000.0,
               position_id=1, at=_day_start_utc8(0) + 3600)
    _seed_deal(a_primary, 7002, profit=-30.0, equity=10000.0,
               position_id=2, at=_day_start_utc8(1) + 3600)
    _seed_deal(a_primary, 7003, profit=50.0, equity=10000.0,
               position_id=3, at=_day_start_utc8(2) + 3600)
    # 副节点的盈利不得进柱状图（口径 = 主节点）
    _seed_deal(a_extra, 7004, profit=999.0, equity=10000.0,
               position_id=4, at=_day_start_utc8(0) + 3600)

    bob_h = _login(client, "bob")
    r = client.get(
        f"/api/follow/leaders/{alice['id']}",
        params={"window": "7d"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    daily = data["daily_profit"]

    assert len(daily) == 7, f"必须是 7 个点（没有成交的日子也要占位），实际 {len(daily)}"
    assert daily[0]["date"], "日期不能为空"

    total = sum(p["profit"] for p in daily)
    perf_total = data["leader"]["performance"]["total_profit"]
    assert total == pytest.approx(perf_total), (
        f"柱状图合计 {total} 必须等于 7d 窗口累计收益 {perf_total}"
    )
    assert total == pytest.approx(140.0), (
        f"只应含主节点三笔（120 - 30 + 50），实际 {total}"
    )

    # 分日正确性：今天 +120、昨天 -30、前天 +50（从后往前数）
    assert daily[-1]["profit"] == pytest.approx(120.0)
    assert daily[-2]["profit"] == pytest.approx(-30.0)
    assert daily[-3]["profit"] == pytest.approx(50.0)
    # 更早的 4 天没有成交，但必须占位（X 轴连续）
    assert [p["profit"] for p in daily[:4]] == [0.0, 0.0, 0.0, 0.0]
    assert daily[-1]["deals"] == 1


def test_leader_detail_daily_profit_excludes_open_entries(client):
    """开仓（IN）不计入日收益 —— 与 deal_stats 的出场类口径一致。"""
    admin_h = _login(client, "admin")
    alice, _bob, a_primary, _a_extra, *_ = _setup_leader_and_follower(client, admin_h)

    _seed_deal(a_primary, 7101, profit=88.0, equity=10000.0,
               position_id=1, entry_type="IN")

    bob_h = _login(client, "bob")
    r = client.get(
        f"/api/follow/leaders/{alice['id']}",
        params={"window": "7d"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    daily = r.json()["daily_profit"]
    assert sum(p["profit"] for p in daily) == pytest.approx(0.0), (
        "开仓成交不应计入已实现收益"
    )


def test_leader_detail_daily_profit_without_data_is_placeholder(client):
    """无成交 -> 仍然是 7 个 0 值占位点（前端据此显示空图而不是报错）。"""
    admin_h = _login(client, "admin")
    alice, _bob, *_ = _setup_leader_and_follower(client, admin_h)

    bob_h = _login(client, "bob")
    r = client.get(
        f"/api/follow/leaders/{alice['id']}",
        params={"window": "7d"},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    daily = r.json()["daily_profit"]
    assert len(daily) == 7
    assert all(p["profit"] == 0.0 for p in daily)
    assert all(p["deals"] == 0 for p in daily)

