"""用户权限端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖：角色-菜单权限、数据归属（节点 / 分组 / 策略）、普通用户手动触发收窄到本人分组、
节点分配的两类 409、会话吊销、最后一个管理员与自我保护、审计范围、后台 WS 过滤，
以及补列迁移与启动种子。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect, text
from starlette.websockets import WebSocketDisconnect

from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PWD = "pass1234"


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


def _login(client, username: str, password: str = _PWD) -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _token(headers: dict) -> str:
    return headers["Authorization"].split(" ", 1)[1]


def _mk_user(client, h, username: str, **extra) -> dict:
    r = client.post("/api/users", json={"username": username, "password": _PWD, **extra}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_node(client, h, mt5_login: int) -> str:
    r = client.post("/api/nodes", json={"name": f"node-{mt5_login}", "mt5_login": mt5_login}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


def _assign(client, h, user_id: int, node_ids: list[str], confirm: bool = False):
    return client.put(
        f"/api/users/{user_id}/nodes", json={"node_ids": node_ids, "confirm": confirm}, headers=h,
    )


def _grant_ai_strategy(client, admin_h) -> None:
    """给内置普通用户角色授权 AI 智能加仓策略（走公共接口，与权限页勾选同一路径）。

    必须用管理员的 header：改角色能力是 require_admin 端点。
    角色级生效后该角色下所有用户都拿到能力。
    """
    roles = client.get("/api/roles", headers=admin_h).json()
    role = next(r for r in roles if r["code"] == "user")
    r = client.put(
        f"/api/roles/{role['id']}/capabilities",
        json={"capabilities": ["ai_strategy"]}, headers=admin_h,
    )
    assert r.status_code == 200, r.text


def _mk_strategy(client, h, name: str, symbol: str = "XAUUSD", admin_h=None) -> dict:
    """以 AI 智能加仓模版（tpl_1）新建策略。

    tpl_1 需要 ai_strategy 能力；这些用例要验证的是数据归属而非授权本身，
    故先用管理员 header 给普通用户角色授权，再以使用者身份创建。
    """
    from app.strategy_templates import TEMPLATE_1_ID
    if admin_h is not None:
        _grant_ai_strategy(client, admin_h)
    r = client.post(
        "/api/strategies", json={"template_id": TEMPLATE_1_ID, "name": name, "symbol": symbol},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_group(client, h, name: str, node_ids=None, strategy_id=None):
    return client.post(
        "/api/groups",
        json={"name": name, "node_ids": node_ids or [], "strategy_id": strategy_id},
        headers=h,
    )


def _node_token(client, h) -> str:
    r = client.get("/api/config/node-token", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["token"]


# =====================================================================
# 身份与菜单
# =====================================================================
def test_me_admin_and_default_user_menus(client):
    from app import permissions as perm

    h = auth_headers(client)
    me = client.get("/api/me", headers=h).json()
    assert me["is_admin"] is True
    assert me["menus"] == list(perm.ALL_MENU_CODES)
    assert [r["code"] for r in me["roles"]] == ["admin"]

    _mk_user(client, h, "alice", display_name="Alice")
    me = client.get("/api/me", headers=_login(client, "alice")).json()
    assert me["is_admin"] is False
    assert me["display_name"] == "Alice"
    assert me["menus"] == list(perm.DEFAULT_USER_MENUS)
    assert [r["code"] for r in me["roles"]] == ["user"]


def test_admin_only_endpoints_forbidden_for_user(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    ah = _login(client, "alice")
    forbidden = [
        ("post", "/api/nodes", {"mt5_login": 9001}),
        ("post", "/api/nodes/lot", {"node_ids": [], "lot_mode": "fixed", "lot": 0.1}),
        ("post", "/api/close-all", {"target": "all"}),
        ("get", "/api/config/node-token", None),
        ("post", "/api/config/node-token/rotate", None),
        ("get", "/api/config/webhook-auth", None),
        ("put", "/api/config/webhook-auth", {"enabled": False}),
        ("get", "/api/config/filters", None),
        ("put", "/api/config/filters", {}),
        ("put", "/api/config/trend", {}),
        ("get", "/api/events/signals", None),
        ("post", "/api/console/purge-trade-logs", {"confirm": "清空交易记录"}),
        ("get", "/api/client-versions", None),
        ("get", "/api/users", None),
        ("get", "/api/roles", None),
        ("get", "/api/menus", None),
    ]
    for method, url, body in forbidden:
        kwargs = {"headers": ah}
        if body is not None:
            kwargs["json"] = body
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
    # 只需登录的读接口
    assert client.get("/api/config/trend", headers=ah).status_code == 200
    assert client.get("/api/strategies/templates", headers=ah).status_code == 200


def test_menu_gating_and_role_menu_change_takes_effect(client):
    h = auth_headers(client)
    role = client.post(
        "/api/roles", json={"code": "viewer", "name": "只看总览", "menus": ["dashboard"]}, headers=h,
    )
    assert role.status_code == 201, role.text
    role = role.json()
    _mk_user(client, h, "bob", role_ids=[role["id"]])
    bh = _login(client, "bob")

    assert client.get("/api/me", headers=bh).json()["menus"] == ["dashboard"]
    assert _mk_group(client, bh, "b1").status_code == 403
    assert client.get("/api/audits", headers=bh).status_code == 403
    # 读本人数据不绑菜单
    assert client.get("/api/groups", headers=bh).json() == []

    r = client.put(f"/api/roles/{role['id']}/menus", json={"menus": ["dashboard", "groups"]}, headers=h)
    assert r.status_code == 200, r.text
    # 权限缓存已失效，旧 token 立即拿到新菜单
    assert _mk_group(client, bh, "b1").status_code == 201


def test_role_crud_and_menu_validation(client):
    h = auth_headers(client)
    menus = client.get("/api/menus", headers=h).json()
    assert {m["code"]: m["assignable"] for m in menus}["console"] is False

    roles = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    assert roles["admin"]["is_builtin"] and roles["user"]["is_builtin"]

    bad = client.post("/api/roles", json={"code": "ops", "name": "运维", "menus": ["console"]}, headers=h)
    assert bad.status_code == 400
    r = client.post("/api/roles", json={"code": "ops", "name": "运维", "menus": ["groups"]}, headers=h)
    assert r.status_code == 201, r.text
    ops = r.json()
    assert client.post("/api/roles", json={"code": "ops", "name": "x"}, headers=h).status_code == 409
    assert client.put(f"/api/roles/{ops['id']}/menus", json={"menus": ["bogus"]}, headers=h).status_code == 400

    admin_id = roles["admin"]["id"]
    assert client.put(f"/api/roles/{admin_id}/menus", json={"menus": []}, headers=h).status_code == 400
    assert client.patch(f"/api/roles/{admin_id}", json={"name": "x"}, headers=h).status_code == 400
    assert client.delete(f"/api/roles/{roles['user']['id']}", headers=h).status_code == 400

    u = _mk_user(client, h, "carol", role_ids=[ops["id"]])
    assert client.delete(f"/api/roles/{ops['id']}", headers=h).status_code == 409
    r = client.put(f"/api/users/{u['id']}/roles", json={"role_ids": [roles["user"]["id"]]}, headers=h)
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/roles/{ops['id']}", headers=h).status_code == 200


# =====================================================================
# 数据归属
# =====================================================================
def test_node_scope(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7001)
    n2 = _mk_node(client, h, 7002)
    assert _assign(client, h, alice["id"], [n1]).status_code == 200
    ah = _login(client, "alice")

    listed = client.get("/api/nodes", headers=ah).json()
    assert [n["node_id"] for n in listed] == [n1]
    assert listed[0]["owner_username"] == "alice"
    assert [a["node_id"] for a in client.get("/api/accounts", headers=ah).json()] == [n1]
    for url in (f"/api/nodes/{n2}", f"/api/nodes/{n2}/dispatches", f"/api/nodes/{n2}/account"):
        assert client.get(url, headers=ah).status_code == 404, url
    assert client.post(f"/api/nodes/{n2}/close", json={"target": "all"}, headers=ah).status_code == 404

    assert client.patch(f"/api/nodes/{n1}", json={"name": "我的节点"}, headers=ah).status_code == 200
    assert client.patch(f"/api/nodes/{n1}", json={"filters": {}}, headers=ah).status_code == 403
    assert client.patch(f"/api/nodes/{n2}", json={"name": "x"}, headers=ah).status_code == 404
    assert client.delete(f"/api/nodes/{n1}", headers=ah).status_code == 403

    admin_view = {n["node_id"]: n for n in client.get("/api/nodes", headers=h).json()}
    assert admin_view[n1]["owner_user_id"] == alice["id"]
    assert admin_view[n1]["owner_username"] == "alice"
    assert admin_view[n2]["owner_user_id"] is None


def test_group_and_strategy_owner_rules(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    _mk_user(client, h, "bob")
    n1 = _mk_node(client, h, 7101)
    n2 = _mk_node(client, h, 7102)
    _assign(client, h, alice["id"], [n1])
    ah, bh = _login(client, "alice"), _login(client, "bob")

    sa = _mk_strategy(client, ah, "黄金加仓", admin_h=h)
    assert sa["owner_user_id"] == alice["id"]
    admin_sty = _mk_strategy(client, h, "管理员策略")

    r = _mk_group(client, ah, "A组", [n1], sa["strategy_id"])
    assert r.status_code == 201, r.text
    ga = r.json()
    assert ga["owner_user_id"] == alice["id"] and ga["owner_username"] == "alice"
    assert _mk_group(client, ah, "A2", [n2]).status_code == 400
    assert _mk_group(client, ah, "A3", [], admin_sty["strategy_id"]).status_code == 400
    assert _mk_group(client, ah, "A组").status_code == 409

    gid = ga["group_id"]
    assert client.get("/api/groups", headers=bh).json() == []
    for method in ("get", "delete"):
        assert getattr(client, method)(f"/api/groups/{gid}", headers=bh).status_code == 404
    assert client.patch(f"/api/groups/{gid}", json={"name": "x"}, headers=bh).status_code == 404
    assert client.get(f"/api/groups/{gid}/signals", headers=bh).status_code == 404
    assert client.post(f"/api/groups/{gid}/close", headers=bh).status_code == 404
    assert client.get(f"/api/strategies/{sa['strategy_id']}", headers=bh).status_code == 404
    assert client.get("/api/strategies", headers=bh).json() == []
    # 名称只在同一所有者内唯一
    assert _mk_group(client, bh, "A组").status_code == 201
    assert _mk_strategy(client, bh, "黄金加仓", admin_h=h)["owner_user_id"] != alice["id"]

    # 管理员编辑用户的分组：成员仍只能选该用户名下的节点
    r = client.patch(f"/api/groups/{gid}", json={"node_ids": [n1, n2]}, headers=h)
    assert r.status_code == 400
    admin_view = {g["group_id"]: g for g in client.get("/api/groups", headers=h).json()}
    assert admin_view[gid]["owner_username"] == "alice"


def test_manual_signal_scoped_to_own_groups(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7201)
    _assign(client, h, alice["id"], [n1])
    ah = _login(client, "alice")
    sa = _mk_strategy(client, ah, "a-xau", admin_h=h)
    ga = _mk_group(client, ah, "A组", [n1], sa["strategy_id"]).json()
    sb = _mk_strategy(client, h, "admin-xau")
    gb = _mk_group(client, h, "管理员组", [], sb["strategy_id"]).json()

    url = "/api/console/manual-signal"
    buy = {"symbol": "XAUUSD", "action": "BUY", "volume": 0.1}
    assert client.post(url, json=buy, headers=ah).status_code == 403  # 缺省 normal
    r = client.post(url, json={**buy, "model": "strategy", "group_ids": [gb["group_id"]]}, headers=ah)
    assert r.status_code == 404
    r = client.post(url, json={**buy, "model": "strategy"}, headers=ah)
    assert r.status_code == 200, r.text
    assert {t["group_id"] for t in r.json()["tasks"]} == {ga["group_id"]}

    # 管理员不收窄：共享信号语义不变
    r = client.post(url, json={**buy, "action": "SELL", "volume": 0.2, "model": "strategy"}, headers=h)
    assert r.status_code == 200, r.text
    assert {ga["group_id"], gb["group_id"]} <= {t["group_id"] for t in r.json()["tasks"]}


def test_manual_signal_user_without_groups(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "BUY", "volume": 0.1, "model": "strategy"},
        headers=_login(client, "alice"),
    )
    assert r.status_code == 400


# =====================================================================
# 节点分配
# =====================================================================
def test_assign_nodes_requires_confirm_when_in_groups(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7301)
    g = _mk_group(client, h, "管理员组", [n1]).json()

    r = _assign(client, h, alice["id"], [n1])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "confirm_required"
    assert detail["memberships"][0]["group_id"] == g["group_id"]

    r = _assign(client, h, alice["id"], [n1], confirm=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == [n1] and body["removed"] == []
    assert client.get(f"/api/groups/{g['group_id']}", headers=h).json()["nodes"] == []
    assert client.get(f"/api/users/{alice['id']}/nodes", headers=h).json() == {"node_ids": [n1]}

    # 回收：节点回到管理员名下
    r = _assign(client, h, alice["id"], [])
    assert r.status_code == 200 and r.json()["removed"] == [n1]
    assert client.get("/api/nodes", headers=_login(client, "alice")).json() == []


def test_assign_nodes_rejects_node_with_active_tasks(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7401)
    sty = _mk_strategy(client, h, "admin-xau")
    assert _mk_group(client, h, "管理员组", [n1], sty["strategy_id"]).status_code == 201
    token = _node_token(client, h)

    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": 7401}})
        assert ws.receive_json()["type"] == "auth_ok"
        r = client.post(
            "/webhook", json={"action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy"},
        )
        assert r.status_code == 200, r.text
        assert ws.receive_json()["cmd"] == "strategy_start"

        r = _assign(client, h, alice["id"], [n1], confirm=True)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["reason"] == "active_tasks"
        assert detail["nodes"][0]["node_id"] == n1
    assert client.get("/api/nodes", headers=_login(client, "alice")).json() == []


def test_assign_nodes_allows_admin_self_assignment(client):
    """超管可以给自己分配节点：owner 从 NULL 转成自己的 user_id。

    原先这里硬挡 400「超管可见全部节点，无需分配」；现在放开，语义与普通用户一致：
    分配后节点进入本人名下，去掉则退回管理员名下（owner=NULL）。
    """
    h = auth_headers(client)
    uid = client.get("/api/me", headers=h).json()["user_id"]
    n1 = _mk_node(client, h, 7501)

    # 分配前：节点在管理员名下（owner_user_id 为 NULL）
    before = {n["node_id"]: n for n in client.get("/api/nodes", headers=h).json()}
    assert before[n1]["owner_user_id"] is None

    # 自分配 → 200，且 added 命中
    r = _assign(client, h, uid, [n1])
    assert r.status_code == 200, r.text
    assert r.json()["added"] == [n1]

    # owner 已转为超管本人
    after = {n["node_id"]: n for n in client.get("/api/nodes", headers=h).json()}
    assert after[n1]["owner_user_id"] == uid

    # 用户列表里能查到自己的 node_ids
    me = next(u for u in client.get("/api/users", headers=h).json() if u["id"] == uid)
    assert n1 in me["node_ids"]

    # 再整体替换为空 → 退回管理员名下
    r2 = _assign(client, h, uid, [])
    assert r2.status_code == 200, r2.text
    assert r2.json()["removed"] == [n1]
    back = {n["node_id"]: n for n in client.get("/api/nodes", headers=h).json()}
    assert back[n1]["owner_user_id"] is None


# =====================================================================
# 会话吊销与保护规则
# =====================================================================
def test_disable_user_revokes_sessions_and_ws(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    ah = _login(client, "alice")

    with client.websocket_connect(f"/ws/admin?token={_token(ah)}") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        r = client.patch(f"/api/users/{alice['id']}", json={"is_active": False}, headers=h)
        assert r.status_code == 200, r.text
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401

    assert client.get("/api/me", headers=ah).status_code == 401
    assert client.post("/api/login", json={"username": "alice", "password": _PWD}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/admin?token={_token(ah)}") as ws:
            ws.receive_json()

    client.patch(f"/api/users/{alice['id']}", json={"is_active": True}, headers=h)
    assert client.get("/api/me", headers=_login(client, "alice")).status_code == 200
    # 重新启用不会让禁用前签发的 token 复活
    assert client.get("/api/me", headers=ah).status_code == 401


def test_reset_password_revokes_old_token(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    ah = _login(client, "alice")
    r = client.post(f"/api/users/{alice['id']}/reset-password", json={"new_password": "newpass99"}, headers=h)
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=ah).status_code == 401
    assert client.get("/api/me", headers=_login(client, "alice", "newpass99")).status_code == 200


def test_change_password_revokes_old_token(client):
    h = auth_headers(client)
    r = client.post(
        "/api/change-password",
        json={"current_password": "admin123", "new_password": "newpass99"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=h).status_code == 401


def test_self_protection(client):
    h = auth_headers(client)
    uid = client.get("/api/me", headers=h).json()["user_id"]
    assert client.patch(f"/api/users/{uid}", json={"is_active": False}, headers=h).status_code == 400
    assert client.put(f"/api/users/{uid}/roles", json={"role_ids": []}, headers=h).status_code == 400
    assert client.delete(f"/api/users/{uid}", headers=h).status_code == 400


def test_second_admin_can_be_demoted(client):
    h = auth_headers(client)
    roles = {r["code"]: r["id"] for r in client.get("/api/roles", headers=h).json()}
    bob = _mk_user(client, h, "bob", role_ids=[roles["admin"]])
    assert client.get("/api/me", headers=_login(client, "bob")).json()["is_admin"] is True
    r = client.put(f"/api/users/{bob['id']}/roles", json={"role_ids": [roles["user"]]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is False


def test_delete_user_requires_no_resources(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7501)
    _assign(client, h, alice["id"], [n1])
    r = client.delete(f"/api/users/{alice['id']}", headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["nodes"] == 1
    _assign(client, h, alice["id"], [])
    assert client.delete(f"/api/users/{alice['id']}", headers=h).status_code == 200
    assert client.post("/api/login", json={"username": "alice", "password": _PWD}).status_code == 401
    assert client.post("/api/users", json={"username": "alice", "password": _PWD}, headers=h).status_code == 201


def test_duplicate_username_rejected(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    r = client.post("/api/users", json={"username": "alice", "password": _PWD}, headers=h)
    assert r.status_code == 409
    r = client.post("/api/users", json={"username": "a", "password": _PWD}, headers=h)
    assert r.status_code == 422


# =====================================================================
# 审计
# =====================================================================
def test_audits_scoped_for_user_and_auth_category(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7601)
    _assign(client, h, alice["id"], [n1])
    ah = _login(client, "alice")
    _mk_strategy(client, ah, "a-s", admin_h=h)

    items = client.get("/api/audits", params={"category": "all", "page_size": 100}, headers=ah).json()["items"]
    assert items and all(a["operator"] == "alice" for a in items)
    assert {"create_strategy", "login"} <= {a["action"] for a in items}

    items = client.get("/api/audits", params={"category": "auth", "page_size": 100}, headers=h).json()["items"]
    actions = {a["action"] for a in items}
    assert {"create_user", "assign_nodes", "login"} <= actions
    assign = next(a for a in items if a["action"] == "assign_nodes")
    assert assign["after"]["added"] == [n1]

    client.post("/api/login", json={"username": "alice", "password": "wrong"})
    items = client.get("/api/audits", params={"category": "auth", "page_size": 100}, headers=h).json()["items"]
    assert any(a["action"] == "login_failed" and a["result"] == "fail" for a in items)


# =====================================================================
# 后台 WS 过滤
# =====================================================================
def test_admin_ws_snapshot_and_events_scoped(client):
    h = auth_headers(client)
    alice = _mk_user(client, h, "alice")
    n1 = _mk_node(client, h, 7701)
    n2 = _mk_node(client, h, 7702)
    _assign(client, h, alice["id"], [n1])
    ah = _login(client, "alice")
    token = _node_token(client, h)

    with client.websocket_connect(f"/ws/admin?token={_token(h)}") as ws:
        snap = ws.receive_json()
    assert {n["node_id"] for n in snap["data"]["nodes"]} == {n1, n2}

    with client.websocket_connect(f"/ws/admin?token={_token(ah)}") as aws:
        snap = aws.receive_json()
        assert snap["type"] == "snapshot"
        assert [n["node_id"] for n in snap["data"]["nodes"]] == [n1]
        # 先上线管理员的节点、再上线 alice 的：alice 收到的第一条只能是自己节点的事件
        with client.websocket_connect("/ws/node") as w2:
            w2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 7702}})
            assert w2.receive_json()["type"] == "auth_ok"
            with client.websocket_connect("/ws/node") as w1:
                w1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 7701}})
                assert w1.receive_json()["type"] == "auth_ok"
                msg = aws.receive_json()
                assert msg["type"] == "node_status"
                assert msg["data"]["node_id"] == n1


# =====================================================================
# 迁移与启动种子
# =====================================================================
def test_owner_and_rbac_migrations_idempotent(tmp_path):
    from app.db import _migrate_owner_columns, _migrate_user_rbac_columns

    eng = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(text("CREATE TABLE nodes (node_id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE node_group (group_id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE trading_strategy (strategy_id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'admin')"))
    for _ in range(2):
        with eng.begin() as conn:
            _migrate_user_rbac_columns(conn)
            _migrate_owner_columns(conn)
    insp = inspect(eng)
    assert {"token_version", "display_name"} <= {c["name"] for c in insp.get_columns("users")}
    for table in ("nodes", "node_group", "trading_strategy"):
        assert "owner_user_id" in {c["name"] for c in insp.get_columns(table)}
        assert f"ix_{table}_owner_user_id" in {i["name"] for i in insp.get_indexes(table)}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT token_version FROM users")).scalar() == 0
    eng.dispose()


async def test_seed_rbac_binds_legacy_admin_only_on_first_upgrade():
    from app import rbac_service
    from app.db import SessionLocal, engine, init_db
    from app.orm import Role, RoleMenu, User, UserRole

    await init_db()
    async with SessionLocal() as s:
        for table in (UserRole, RoleMenu, Role, User):
            await s.execute(delete(table))
        s.add(User(username="legacy", password_hash="x", role="admin"))
        s.add(User(username="plain", password_hash="x", role="user"))
        await s.commit()
    try:
        await rbac_service.seed_rbac()
        await rbac_service.seed_rbac()
        legacy = await rbac_service.load_principal(None, "legacy")
        plain = await rbac_service.load_principal(None, "plain")
        assert legacy.is_admin and not plain.is_admin
        roles = {r["code"]: r for r in await rbac_service.list_roles()}
        assert set(roles) == {"admin", "user"}

        # 管理员收回其超级管理员角色后再次启动：不会被重新绑定
        await rbac_service.set_user_roles(legacy.user_id, [roles["user"]["id"]])
        await rbac_service.seed_rbac()
        again = await rbac_service.load_principal(None, "legacy")
        assert again is not None and not again.is_admin
    finally:
        await engine.dispose()


# =====================================================================
# AI 智能加仓策略的能力授权（tpl_1 需 ai_strategy）
# =====================================================================
def _mk_strategy_as(client, h, template_id: str, name: str) -> object:
    return client.post(
        "/api/strategies",
        json={"template_id": template_id, "name": name, "symbol": "XAUUSD"},
        headers=h,
    )


def test_ai_strategy_requires_capability_to_create(client):
    """未授权的普通用户建不了 tpl_1，其余模版照常。"""
    from app import permissions as perm
    from app.strategy_templates import TEMPLATE_2_ID

    h = auth_headers(client)
    _mk_user(client, h, "alice")
    ah = _login(client, "alice")

    assert perm.CAP_AI_STRATEGY not in client.get("/api/me", headers=ah).json()["capabilities"]
    denied = _mk_strategy_as(client, ah, perm.AI_STRATEGY_TEMPLATE_ID, "越权加仓")
    assert denied.status_code == 403
    assert "授权" in denied.json()["detail"]
    # 未被拒绝时不该留下任何实例
    assert client.get("/api/strategies", headers=ah).json() == []

    # 其它模版不受 AI 授权影响
    assert _mk_strategy_as(client, ah, TEMPLATE_2_ID, "普通趋势单").status_code == 201


def test_ai_strategy_capability_grant_and_revoke(client):
    """授权后能建 AI 加仓；收回后立刻回到 403。"""
    from app import permissions as perm

    h = auth_headers(client)
    _mk_user(client, h, "alice")
    roles = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    user_role = roles["user"]

    denied = _mk_strategy_as(
        client, _login(client, "alice"), perm.AI_STRATEGY_TEMPLATE_ID, "加仓-授权前",
    )
    assert denied.status_code == 403

    grant = client.put(
        f"/api/roles/{user_role['id']}/capabilities",
        json={"capabilities": [perm.CAP_AI_STRATEGY]}, headers=h,
    )
    assert grant.status_code == 200
    assert grant.json()["capabilities"] == [perm.CAP_AI_STRATEGY]

    ah = _login(client, "alice")
    assert perm.CAP_AI_STRATEGY in client.get("/api/me", headers=ah).json()["capabilities"]
    assert _mk_strategy_as(client, ah, perm.AI_STRATEGY_TEMPLATE_ID, "加仓-授权后").status_code == 201

    revoke = client.put(
        f"/api/roles/{user_role['id']}/capabilities", json={"capabilities": []}, headers=h,
    )
    assert revoke.status_code == 200
    assert _mk_strategy_as(
        client, _login(client, "alice"), perm.AI_STRATEGY_TEMPLATE_ID, "加仓-收回后",
    ).status_code == 403


def test_ai_strategy_write_denied_after_grant_revoked(client):
    """授权被收回后，既有 AI 加仓实例的任何写操作都要 403。"""
    from app import permissions as perm
    from app.strategy_templates import TEMPLATE_2_ID

    h = auth_headers(client)
    _mk_user(client, h, "alice")
    roles = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    user_role = roles["user"]
    client.put(
        f"/api/roles/{user_role['id']}/capabilities",
        json={"capabilities": [perm.CAP_AI_STRATEGY]}, headers=h,
    )

    ah = _login(client, "alice")
    ai = _mk_strategy_as(client, ah, perm.AI_STRATEGY_TEMPLATE_ID, "我的加仓").json()
    plain = _mk_strategy_as(client, ah, TEMPLATE_2_ID, "我的趋势单").json()

    # 收回授权
    client.put(
        f"/api/roles/{user_role['id']}/capabilities", json={"capabilities": []}, headers=h,
    )
    ah = _login(client, "alice")

    assert client.patch(
        f"/api/strategies/{ai['strategy_id']}", json={"name": "改名"}, headers=ah,
    ).status_code == 403
    assert client.delete(f"/api/strategies/{ai['strategy_id']}", headers=ah).status_code == 403
    # 不允许把普通模版的实例改成 AI 加仓来绕过
    assert client.patch(
        f"/api/strategies/{plain['strategy_id']}", json={"name": "改个名"}, headers=ah,
    ).status_code == 200


def test_ai_strategy_readable_without_capability(client):
    """能力只管写：普通用户仍能看到自己名下的 AI 加仓实例（分发中要看得见）。"""
    from app import permissions as perm

    h = auth_headers(client)
    _mk_user(client, h, "alice")
    roles = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    client.put(
        f"/api/roles/{roles['user']['id']}/capabilities",
        json={"capabilities": [perm.CAP_AI_STRATEGY]}, headers=h,
    )
    ah = _login(client, "alice")
    ai = _mk_strategy_as(client, ah, perm.AI_STRATEGY_TEMPLATE_ID, "我的加仓").json()

    client.put(
        f"/api/roles/{roles['user']['id']}/capabilities", json={"capabilities": []}, headers=h,
    )
    ah = _login(client, "alice")

    listed = client.get("/api/strategies", headers=ah).json()
    assert [s["strategy_id"] for s in listed] == [ai["strategy_id"]]
    assert client.get(f"/api/strategies/{ai['strategy_id']}", headers=ah).status_code == 200


def test_role_capabilities_api_rules(client):
    """能力端点本身的管理员边界与入参校验。"""
    from app import permissions as perm

    h = auth_headers(client)
    _mk_user(client, h, "alice")
    ah = _login(client, "alice")

    caps = client.get("/api/capabilities", headers=h)
    assert caps.status_code == 200
    assert {c["code"] for c in caps.json()} == set(perm.ALL_CAPABILITY_CODES)

    roles = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    user_role_id, admin_role_id = roles["user"]["id"], roles["admin"]["id"]

    # 普通用户无权改角色能力
    assert client.put(
        f"/api/roles/{user_role_id}/capabilities",
        json={"capabilities": [perm.CAP_AI_STRATEGY]}, headers=ah,
    ).status_code == 403
    # 未知能力码拒收
    bad = client.put(
        f"/api/roles/{user_role_id}/capabilities",
        json={"capabilities": ["nope"]}, headers=h,
    )
    assert bad.status_code == 400
    # 管理员角色固定全部能力，不可改
    assert client.put(
        f"/api/roles/{admin_role_id}/capabilities",
        json={"capabilities": []}, headers=h,
    ).status_code == 400

    # 管理员角色在角色列表里始终呈现全部能力
    assert client.get("/api/roles", headers=h).json()  # 列表可读
    listed = {r["code"]: r for r in client.get("/api/roles", headers=h).json()}
    assert listed["admin"]["capabilities"] == list(perm.ALL_CAPABILITY_CODES)
    assert listed["user"]["capabilities"] == []


def test_create_role_with_ai_capability(client):
    """新建角色时可直接带上 AI 授权。"""
    from app import permissions as perm

    h = auth_headers(client)
    r = client.post(
        "/api/roles",
        json={
            "code": "ai_trader", "name": "AI 交易员", "enabled": True,
            "menus": [perm.MENU_STRATEGIES], "capabilities": [perm.CAP_AI_STRATEGY],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["capabilities"] == [perm.CAP_AI_STRATEGY]

    # 绑给用户后 /api/me 即带上能力，且能建 AI 加仓
    user = _mk_user(client, h, "bob")
    assert client.put(
        f"/api/users/{user['id']}/roles", json={"role_ids": [r.json()["id"]]}, headers=h,
    ).status_code == 200
    bh = _login(client, "bob")
    assert perm.CAP_AI_STRATEGY in client.get("/api/me", headers=bh).json()["capabilities"]
    assert _mk_strategy_as(client, bh, perm.AI_STRATEGY_TEMPLATE_ID, "授权角色的加仓").status_code == 201


def test_role_capabilities_column_migration_idempotent(tmp_path):
    """补列迁移：缺列时补上，重复执行不报错，存量角色回填为空能力数组。

    MySQL 不允许 TEXT 带 DEFAULT，迁移里只能先加 NULL 列再回填；
    这条用例锁住「存量行拿到的是 '[]' 而不是空串 / NULL」。
    """
    from app.db import _migrate_role_capabilities

    eng = create_engine(f"sqlite:///{(tmp_path / 'legacy_caps.db').as_posix()}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE sys_role (id INTEGER PRIMARY KEY, code VARCHAR(32))"))
        conn.execute(text("INSERT INTO sys_role (id, code) VALUES (1, 'admin'), (2, 'user')"))
    for _ in range(2):
        with eng.begin() as conn:
            _migrate_role_capabilities(conn)
    insp = inspect(eng)
    assert "capabilities" in {c["name"] for c in insp.get_columns("sys_role")}
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT code, capabilities FROM sys_role ORDER BY id")).all()
    # 存量行必须回填成 '[]'（空数组 = 无 AI 授权），不能是 NULL 或空串
    assert rows == [("admin", "[]"), ("user", "[]")], rows
    eng.dispose()
