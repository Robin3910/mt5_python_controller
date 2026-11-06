"""个人中心自助维护资料（PATCH /api/me/profile）的端到端用例。

覆盖：字段级「传什么改什么」、空串清空、邮箱格式校验、带单员资料的��力校验、
只改自己（改不到别人）、MeOut 回显新增字段、审计留痕。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

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


def _mk_user(client, h, username: str, **extra) -> dict:
    r = client.post("/api/users", json={"username": username, "password": _PWD, **extra}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _grant_leader_capability(client, admin_h) -> None:
    """给内置普通用户角色授予 follow_leader 能力（改角色能力是管理员端点）。"""
    roles = client.get("/api/roles", headers=admin_h).json()
    role = next(r for r in roles if r["code"] == "user")
    r = client.put(
        f"/api/roles/{role['id']}/capabilities",
        json={"capabilities": ["follow_leader"]}, headers=admin_h,
    )
    assert r.status_code == 200, r.text


# =====================================================================
# 基本读写
# =====================================================================
def test_update_profile_updates_own_fields(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    ah = _login(client, "alice")

    r = client.patch(
        "/api/me/profile",
        json={"display_name": "爱丽丝", "email": "alice@example.com"},
        headers=ah,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "爱丽丝"
    assert body["email"] == "alice@example.com"

    # 再拉一次，确认真的落库了（不是只改了响应体）
    me = client.get("/api/me", headers=ah).json()
    assert me["display_name"] == "爱丽丝"
    assert me["email"] == "alice@example.com"


def test_update_profile_is_partial_not_touched_fields_unchanged(client):
    """字段级语义：没传的字段保持原值，不能被清掉。"""
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    ah = _login(client, "alice")
    client.patch("/api/me/profile", json={"email": "alice@example.com"}, headers=ah)

    # 只改 display_name，email 必须还在
    r = client.patch("/api/me/profile", json={"display_name": "Bob"}, headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Bob"
    assert r.json()["email"] == "alice@example.com"


def test_update_profile_empty_string_clears_field(client):
    """传空串 = 清空（置 NULL），不是「不改」。"""
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    ah = _login(client, "alice")
    client.patch("/api/me/profile", json={"email": "alice@example.com"}, headers=ah)

    r = client.patch("/api/me/profile", json={"email": ""}, headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["email"] is None


# =====================================================================
# 校验
# =====================================================================
def test_update_profile_rejects_bad_email(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    ah = _login(client, "alice")

    for bad in ["not-an-email", "a@b", "a b@c.com", "@example.com", "a@.com"]:
        r = client.patch("/api/me/profile", json={"email": bad}, headers=ah)
        assert r.status_code == 400, (bad, r.status_code, r.text)


def test_update_profile_email_allows_duplicate(client):
    """邮箱不参与登录，故不设唯一约束 —— 允许两个用户填同一个邮箱。"""
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    _mk_user(client, h, "bob")
    ah = _login(client, "alice")
    bh = _login(client, "bob")

    assert client.patch(
        "/api/me/profile", json={"email": "same@example.com"}, headers=ah,
    ).status_code == 200
    assert client.patch(
        "/api/me/profile", json={"email": "same@example.com"}, headers=bh,
    ).status_code == 200


def test_update_profile_requires_login(client):
    assert client.patch("/api/me/profile", json={"display_name": "x"}).status_code == 401


# =====================================================================
# 只改自己
# =====================================================================
def test_update_profile_cannot_touch_other_user(client):
    """接口不带 user_id —— 只能改自己，改不到别人。"""
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    _mk_user(client, h, "bob", display_name="Bob")
    ah = _login(client, "alice")

    client.patch("/api/me/profile", json={"display_name": "Mallory"}, headers=ah)

    # Bob 的资料没被动过
    admin_view = client.get("/api/users", headers=h).json()
    bob = next(u for u in admin_view if u["username"] == "bob")
    assert bob["display_name"] == "Bob"


# =====================================================================
# 带单员资料：能力校验
# =====================================================================
def test_leader_fields_require_capability(client):
    """没有带单能力的用户不能改带单员资料（带单广场是公开页）。"""
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    ah = _login(client, "alice")

    r = client.patch(
        "/api/me/profile",
        json={"leader_name": "韭菜王", "leader_intro": "稳赚不赔"},
        headers=ah,
    )
    assert r.status_code == 403, r.text


def test_leader_fields_allowed_with_capability(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice")
    _grant_leader_capability(client, h)
    ah = _login(client, "alice")

    r = client.patch(
        "/api/me/profile",
        json={"leader_name": "量化领航员", "leader_intro": "专注趋势跟踪"},
        headers=ah,
    )
    assert r.status_code == 200, r.text
    assert r.json()["leader_name"] == "量化领航员"
    assert r.json()["leader_intro"] == "专注趋势跟踪"


def test_admin_can_write_leader_fields_without_capability(client):
    """管理员恒放行（can_capability 内部含 is_admin 分支）。"""
    h = auth_headers(client)
    r = client.patch(
        "/api/me/profile", json={"leader_intro": "管理员自述"}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["leader_intro"] == "管理员自述"


# =====================================================================
# MeOut 回显
# =====================================================================
def test_me_exposes_new_fields(client):
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    _grant_leader_capability(client, h)
    ah = _login(client, "alice")
    client.patch(
        "/api/me/profile",
        json={"email": "alice@example.com", "leader_intro": "介绍"},
        headers=ah,
    )

    me = client.get("/api/me", headers=ah).json()
    assert me["email"] == "alice@example.com"
    assert me["leader_intro"] == "介绍"
    # 未填的带单员名称为 None，前端回退显示 display_name
    assert me["leader_name"] is None
    # 手机号未绑定
    assert me["phone"] is None
    assert me["phone_verified"] is False


def test_leader_fields_rejection_is_atomic(client):
    """没有带单能力时，整个请求被拒 —— 不会「一半生效」。

    即：请求里夹带的 display_name / email 也不该被写进去，
    否则用户会以为保存成功了。
    """
    h = auth_headers(client)
    _mk_user(client, h, "alice", display_name="Alice")
    ah = _login(client, "alice")

    r = client.patch(
        "/api/me/profile",
        json={"display_name": "改了", "leader_intro": "无权限"},
        headers=ah,
    )
    assert r.status_code == 403, r.text

    me = client.get("/api/me", headers=ah).json()
    assert me["display_name"] == "Alice"


def test_me_phone_is_masked(client):
    """手机号只回显脱敏值，完整号码不进接口。"""
    h = auth_headers(client)
    created = _mk_user(client, h, "alice")
    client.patch(f"/api/users/{created['id']}", json={"phone": "13800138000"}, headers=h)
    ah = _login(client, "alice")

    me = client.get("/api/me", headers=ah).json()
    assert me["phone"] == "138****8000"
    assert "13800138000" not in me["phone"]
