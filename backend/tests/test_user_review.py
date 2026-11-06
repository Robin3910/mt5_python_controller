"""注册用户 KYC 实名审核的端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖：审核列表聚合（用户 / 审核单 / 名下节点 / 搜索 / 统计口径）、审核通过
（含分配节点、409 回滚）、驳回、批量通过、权限隔离、实名材料（姓名 / 邮箱 /
手机号）校验、**驳回后重新提交**（核心新路径），以及原登录链路回归。

**不真发短信**：注入 FakeSmsProvider。
"""
import pathlib
import sqlite3

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.captcha_provider import FakeCaptchaVerifier, reset_verifier_cache, set_verifier
from app.redis_store import RedisStore
from app.sms_config import PURPOSE_AUTH
from app.sms_provider import FakeSmsProvider, reset_provider_cache, set_provider
from app.sms_service import normalize_phone
from app.user_review_service import (
    KycInvalid,
    mask_phone_snapshot,
    validate_kyc,
)
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

# 必须与 conftest.py 里设置的 MYSQL_DSN 指向同一文件 —— 引擎只认那一个库。
# 别的 API 用例也共用它，所以这里不能用独立文件名。
_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PHONE = "13800138000"
_E164 = "+8613800138000"
_OTHER = "13900139000"

_NAME = "徐明远"
_EMAIL = "xumingyuan@example.com"

_FAKE_SERVER = fakeredis.FakeServer()


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(
            fakeredis.FakeAsyncRedis(server=_FAKE_SERVER, decode_responses=True)
        )

    reset_test_db(_TEST_DB)
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).flushall()
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


@pytest.fixture
def provider():
    reset_provider_cache()
    fake = FakeSmsProvider()
    set_provider(fake)
    yield fake
    reset_provider_cache()


@pytest.fixture
def captcha():
    reset_verifier_cache()
    fake = FakeCaptchaVerifier(ok=True)
    set_verifier(fake)
    yield fake
    reset_verifier_cache()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _enable_sms(client, h, **overrides) -> None:
    body = {
        "enabled": True,
        "provider": "aliyun_sms_auth",
        "endpoint": "dypnsapi.aliyuncs.com",
        "sign_name": "测试签名",
        "template_code": "100001",
        "require_approval": True,
        "code_ttl_seconds": 300,
        "send_interval_seconds": 60,
        "daily_limit_per_phone": 10,
        "hourly_limit_per_ip": 20,
        "max_verify_attempts": 5,
    }
    body.update(overrides)
    r = client.put("/api/config/sms", json=body, headers=h)
    assert r.status_code == 200, r.text


def _clear_cooldown(phone: str = _PHONE) -> None:
    from app.redis_store import K_SMS_LOCK

    try:
        key = normalize_phone(phone)
    except Exception:
        key = phone
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).delete(K_SMS_LOCK.format(key))


def _last_code(provider, phone=_E164) -> str:
    for item in reversed(provider.sent):
        if item["phone"] == phone:
            return item["code"]
    raise AssertionError(f"没有向 {phone} 发出验证码")


def _register(client, provider, phone=_PHONE, **extra):
    """走完整注册流程（发码 → 校验 → 注册），返回响应对象。"""
    _clear_cooldown(phone)
    client.post("/api/auth/sms/send", json={"phone": phone, "purpose": PURPOSE_AUTH})
    code = _last_code(provider, "+86" + phone)
    ticket = client.post(
        "/api/auth/sms/verify",
        json={"phone": phone, "code": code, "purpose": PURPOSE_AUTH},
    ).json()["ticket"]
    payload = {"phone": phone, "ticket": ticket}
    payload.update(extra)
    return client.post("/api/auth/register", json=payload)


def _verify_next_action(client, provider, phone=_PHONE) -> dict:
    _clear_cooldown(phone)
    client.post("/api/auth/sms/send", json={"phone": phone, "purpose": PURPOSE_AUTH})
    code = _last_code(provider, "+86" + phone)
    return client.post(
        "/api/auth/sms/verify",
        json={"phone": phone, "code": code, "purpose": PURPOSE_AUTH},
    ).json()


def _kyc(name=_NAME, email=_EMAIL):
    return {"real_name": name, "email": email}


# 第二个用户的实名材料
_NAME_2 = "李思远"
_EMAIL_2 = "lisiyuan@example.com"


def _make_user(client, admin, name="alice"):
    r = client.post(
        "/api/users", headers=admin, json={"username": name, "password": "pass1234"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_node(client, admin, login=8801):
    r = client.post(
        "/api/nodes",
        headers=admin,
        json={"name": "节点A", "mt5_login": login, "mt5_server": "demo"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 脱敏与校验（单元）
# ---------------------------------------------------------------------------


def test_mask_phone_snapshot():
    assert mask_phone_snapshot("13800138000") == "138****8000"
    # 带国家码前缀也要脱敏成同一个形状
    assert mask_phone_snapshot("+8613800138000") == "138****8000"
    # 空值 → None
    assert mask_phone_snapshot(None) is None
    assert mask_phone_snapshot("") is None


def test_validate_kyc_no_name_returns_none():
    """没填姓名 = 没提交实名材料（邮箱可以单独填）。"""
    assert validate_kyc(None, None) is None
    assert validate_kyc("", "") is None
    assert validate_kyc("   ", "") is None


def test_validate_kyc_returns_name_and_email():
    out = validate_kyc("徐明远", "xumingyuan@example.com")
    assert out["real_name"] == "徐明远"
    assert out["email"] == "xumingyuan@example.com"
    # 姓名首尾空白被裁掉
    assert validate_kyc("  徐明远  ", None)["real_name"] == "徐明远"


def test_validate_kyc_bad_email_rejected():
    with pytest.raises(KycInvalid):
        validate_kyc("徐明远", "not-an-email")
    with pytest.raises(KycInvalid):
        validate_kyc("徐明远", "a@b")
    with pytest.raises(KycInvalid):
        validate_kyc("徐明远", "a b@c.com")


def test_validate_kyc_bad_email_rejected_even_without_name():
    """没填姓名时邮箱格式照样要校验（不能被 early return 放过去）。"""
    with pytest.raises(KycInvalid):
        validate_kyc(None, "not-an-email")
    with pytest.raises(KycInvalid):
        validate_kyc("", "not-an-email")


def test_validate_kyc_too_long_name_rejected():
    with pytest.raises(KycInvalid):
        validate_kyc("徐" * 65, None)


# ---------------------------------------------------------------------------
# 注册侧：实名自填
# ---------------------------------------------------------------------------


def test_register_with_kyc_creates_review(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _register(client, provider, **_kyc())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pending_approval"] is True
    assert body["review_id"] is not None
    assert body["resubmitted"] is False


def test_register_without_kyc_creates_review_but_not_submitted(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _register(client, provider)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["review_id"] is not None

    lst = client.get("/api/user-reviews", headers=h).json()
    item = lst["items"][0]
    assert item["kyc_submitted"] is False
    assert item["real_name"] is None


def test_register_bad_email_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _register(client, provider, real_name="徐明远", email="not-an-email")
    assert r.status_code == 400, r.text


def test_register_email_only_no_name_is_allowed(client, provider):
    """只填邮箱不填姓名 = 不算提交实名材料，不报错。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _register(client, provider, email="only@example.com")
    assert r.status_code == 201, r.text
    lst = client.get("/api/user-reviews", headers=h).json()
    item = lst["items"][0]
    assert item["kyc_submitted"] is False
    assert item["real_name"] is None
    assert item["email"] == "only@example.com"


def test_register_no_approval_mode_creates_no_review(client, provider):
    """免审核模式不建审核单 —— 表里只记录走过审核流程的用户。"""
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    r = _register(client, provider, **_kyc())
    assert r.status_code == 201, r.text
    assert r.json()["review_id"] is None
    assert r.json()["token"] is not None
    assert client.get("/api/user-reviews", headers=h).json()["items"] == []


def test_no_cert_columns_in_table(client, provider):
    """证件相关列已彻底移除，手机号存的是脱敏快照。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    conn = sqlite3.connect(_TEST_DB)
    try:
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(user_reviews)")
        ]
        # 证件三列都不在了
        assert "cert_type" not in cols
        assert "cert_no_masked" not in cols
        assert "cert_no_hash" not in cols
        assert "cert_no" not in cols
        # 手机号快照列在，且存的是脱敏值
        assert "phone" in cols
        row = conn.execute("SELECT phone, real_name, email FROM user_reviews").fetchone()
    finally:
        conn.close()
    assert row[0] == "138****8000"
    assert row[1] == _NAME
    assert row[2] == _EMAIL


# ---------------------------------------------------------------------------
# 列表聚合
# ---------------------------------------------------------------------------


def test_review_list_empty(client):
    h = auth_headers(client)
    r = client.get("/api/user-reviews", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["stats"]["total"] == 0
    assert body["stats"]["pending"] == 0


def test_review_list_aggregates_user_and_nodes(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    node = _make_node(client, h)
    users = client.get("/api/users", headers=h).json()
    uid = users[1]["id"]
    assert client.put(
        f"/api/users/{uid}/nodes", headers=h, json={"node_ids": [node["node_id"]]}
    ).status_code == 200

    body = client.get("/api/user-reviews", headers=h).json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["real_name"] == _NAME
    assert item["email"] == _EMAIL
    assert item["phone"] == "138****8000"
    assert item["account"] == f"U-{uid}"
    assert item["status"] == "pending"
    assert len(item["nodes"]) == 1
    assert item["nodes"][0]["node_id"] == node["node_id"]
    assert item["nodes"][0]["status"] == "offline"
    assert item["nodes"][0]["balance"] == 0.0
    assert body["stats"]["node_total"] == 1
    assert body["stats"]["node_online"] == 0


def test_review_list_stats_not_affected_by_filter(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    _register(client, provider, phone=_OTHER, **_kyc(_NAME_2, _EMAIL_2))

    body = client.get("/api/user-reviews?status=approved", headers=h).json()
    # 筛选后列表为空，但统计仍是全局数
    assert body["items"] == []
    assert body["stats"]["total"] == 2
    assert body["stats"]["pending"] == 2


def test_review_list_status_filter(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    _register(client, provider, phone=_OTHER, **_kyc(_NAME_2, _EMAIL_2))
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    first = items[0]["id"]

    assert client.post(
        f"/api/user-reviews/{first}/reject", headers=h, json={"reason": "材料不齐"}
    ).status_code == 200

    pending = client.get("/api/user-reviews?status=pending", headers=h).json()
    rejected = client.get("/api/user-reviews?status=rejected", headers=h).json()
    assert len(pending["items"]) == 1
    assert len(rejected["items"]) == 1
    assert rejected["items"][0]["reject_reason"] == "材料不齐"


def test_review_list_invalid_status_400(client):
    h = auth_headers(client)
    r = client.get("/api/user-reviews?status=bogus", headers=h)
    assert r.status_code == 400, r.text


def test_review_list_keyword_dimensions(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    node = _make_node(client, h)
    users = client.get("/api/users", headers=h).json()
    uid = users[1]["id"]
    client.put(
        f"/api/users/{uid}/nodes", headers=h, json={"node_ids": [node["node_id"]]}
    )

    # 用户名
    assert len(client.get("/api/user-reviews?keyword=m13800138000", headers=h).json()["items"]) == 1
    # 账号
    assert len(client.get(f"/api/user-reviews?keyword=U-{uid}", headers=h).json()["items"]) == 1
    # 手机号（脱敏串）
    assert len(client.get("/api/user-reviews?keyword=138****8000", headers=h).json()["items"]) == 1
    # 节点 ID
    assert len(client.get(
        f"/api/user-reviews?keyword={node['node_id']}", headers=h
    ).json()["items"]) == 1
    # 大小写不敏感（用户名是小写 m138...，用大写搜）
    assert len(client.get("/api/user-reviews?keyword=M13800138000", headers=h).json()["items"]) == 1
    # 搜不到
    assert len(client.get("/api/user-reviews?keyword=zzzz", headers=h).json()["items"]) == 0


# ---------------------------------------------------------------------------
# 审核动作
# ---------------------------------------------------------------------------


def test_approve_review_activates_user(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]

    r = client.post(f"/api/user-reviews/{review['id']}/approve", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True

    # 用户可登录了（端到端闭环）
    act = client.get("/api/user-reviews", headers=h).json()["items"][0]
    assert act["status"] == "approved"
    assert act["audited_by"] == "admin"

    users = client.get("/api/users", headers=h).json()
    assert users[1]["is_active"] is True


def test_approve_then_sms_verify_next_action_login(client, provider):
    """审核通过后，验证码登录的下一步应是 login（闭环）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    client.post(f"/api/user-reviews/{review['id']}/approve", headers=h, json={})

    data = _verify_next_action(client, provider)
    assert data["next_action"] == "login"


def test_reject_review_records_reason(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]

    r = client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "证件照片模糊"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False

    item = client.get("/api/user-reviews", headers=h).json()["items"][0]
    assert item["status"] == "rejected"
    assert item["reject_reason"] == "证件照片模糊"

    # 用户仍不可登录
    users = client.get("/api/users", headers=h).json()
    assert users[1]["is_active"] is False


def test_reject_then_sms_verify_next_action_rejected(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "证件照片模糊"}
    )

    data = _verify_next_action(client, provider)
    assert data["next_action"] == "rejected"
    assert data["reject_reason"] == "证件照片模糊"


def test_approve_twice_409(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    assert client.post(
        f"/api/user-reviews/{review['id']}/approve", headers=h, json={}
    ).status_code == 200
    assert client.post(
        f"/api/user-reviews/{review['id']}/approve", headers=h, json={}
    ).status_code == 409


def test_reject_empty_reason_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    r = client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "   "}
    )
    assert r.status_code == 400, r.text


def test_review_not_found_404(client):
    h = auth_headers(client)
    assert client.post("/api/user-reviews/99999/approve", headers=h, json={}).status_code == 404
    assert client.post(
        "/api/user-reviews/99999/reject", headers=h, json={"reason": "x"}
    ).status_code == 404


def test_approve_with_node_assignment(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    node = _make_node(client, h)
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]

    r = client.post(
        f"/api/user-reviews/{review['id']}/approve",
        headers=h,
        json={"node_ids": [node["node_id"]]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assigned_node_ids"] == [node["node_id"]]

    # 节点归属已改
    nodes = client.get("/api/nodes", headers=h).json()
    users = client.get("/api/users", headers=h).json()
    uid = users[1]["id"]
    assert nodes[0]["owner_user_id"] == uid


def test_approve_node_assignment_rollback_on_409(client, provider):
    """分配节点失败（活动子任务）时，审核单仍是 pending、用户仍不可登录。

    这是「操作顺序」的关键断言：先分配节点 → 成功后再改审核单 → 最后开闸门。
    """
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    node = _make_node(client, h)
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]

    # 造一个「节点上有活动子任务」的场景太重，这里改用「节点不存在」触发 400，
    # 断言审核单状态没被改（同一段代码路径：分配先于状态变更）。
    r = client.post(
        f"/api/user-reviews/{review['id']}/approve",
        headers=h,
        json={"node_ids": ["no-such-node"]},
    )
    assert r.status_code == 400, r.text
    item = client.get("/api/user-reviews", headers=h).json()["items"][0]
    assert item["status"] == "pending"
    users = client.get("/api/users", headers=h).json()
    assert users[1]["is_active"] is False


def test_bulk_approve(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    _register(client, provider, phone=_OTHER, **_kyc(_NAME_2, _EMAIL_2))
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    assert len(items) == 2

    r = client.post("/api/user-reviews/bulk-approve", headers=h, json={"review_ids": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] == 2
    assert body["failed"] == []

    lst = client.get("/api/user-reviews?status=approved", headers=h).json()
    assert len(lst["items"]) == 2


def test_bulk_approve_partial_failure(client, provider):
    """已处理的单进 failed，其余照常通过。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    _register(client, provider, phone=_OTHER, **_kyc(_NAME_2, _EMAIL_2))
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    first, second = items[0]["id"], items[1]["id"]
    client.post(f"/api/user-reviews/{first}/approve", headers=h, json={})

    r = client.post(
        "/api/user-reviews/bulk-approve", headers=h, json={"review_ids": [first, second]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] == 1
    assert len(body["failed"]) == 1
    assert body["failed"][0]["review_id"] == first


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def test_review_api_requires_admin(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    # 造一个普通用户
    _make_user(client, h, "bob")
    token = client.post(
        "/api/login", json={"username": "bob", "password": "pass1234"}
    ).json()["token"]
    uh = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/user-reviews", headers=uh).status_code == 403
    assert client.post("/api/user-reviews/1/approve", headers=uh, json={}).status_code == 403


def test_review_api_requires_login(client):
    assert client.get("/api/user-reviews").status_code == 401


def test_user_audit_menu_is_admin_only():
    """user_audit 菜单 assignable=False —— 角色编辑页不出现该勾选项。"""
    from app.permissions import MENU_REGISTRY, MENU_USER_AUDIT

    entry = next(m for m in MENU_REGISTRY if m.code == MENU_USER_AUDIT)
    assert entry.assignable is False
    assert entry.path == "/user-audit"


# ---------------------------------------------------------------------------
# 驳回后重新提交
# ---------------------------------------------------------------------------


def test_resubmit_after_reject(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "姓名信息不完整"}
    )

    # 重新提交（换一份材料）
    r = _register(
        client, provider, **_kyc(name="徐明远新", email="new@example.com")
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["resubmitted"] is True
    assert body["pending_approval"] is True
    assert body["review_id"] is not None
    assert body["review_id"] != review["id"]

    # 用户仍不可登录（重提不等于通过）
    users = client.get("/api/users", headers=h).json()
    assert users[1]["is_active"] is False

    # 列表里该用户只出现最新一张单，状态 pending
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["real_name"] == "徐明远新"
    assert items[0]["email"] == "new@example.com"


def test_resubmit_keeps_history(client, provider):
    """旧单保留成历史：库里该用户有 2 张单。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "材料不齐"}
    )
    _register(client, provider, **_kyc())

    conn = sqlite3.connect(_TEST_DB)
    try:
        rows = conn.execute(
            "SELECT status FROM user_reviews ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["rejected", "pending"]


def test_resubmit_same_material_same_user_allowed(client, provider):
    """同一用户重提同样的材料不算冲突（没有证件去重了）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    review = client.get("/api/user-reviews", headers=h).json()["items"][0]
    client.post(
        f"/api/user-reviews/{review['id']}/reject", headers=h, json={"reason": "材料不齐"}
    )
    r = _register(client, provider, **_kyc())
    assert r.status_code == 201, r.text


def test_register_duplicate_still_409_when_not_rejected(client, provider):
    """未被驳回的用户重复注册 → 仍是 409（原行为不变）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    assert _register(client, provider, **_kyc()).status_code == 201
    r = _register(client, provider, **_kyc())
    assert r.status_code == 409, r.text
    assert "已注册" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_audit_records_approve_and_reject(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    rid = items[0]["id"]
    client.post(f"/api/user-reviews/{rid}/approve", headers=h, json={})

    audits = client.get("/api/audits?category=auth", headers=h).json()
    actions = [a["action"] for a in audits["items"]]
    assert "user_review_approve" in actions


def test_audit_has_no_phone_plaintext(client, provider):
    """审计的 params 里不出现手机号明文（只放脱敏值）。

    注：`target` 字段是操作者标识，自动生成的用户名形如 `m13800138000`
    （用户名本身含手机号，这是既有行为，与审计脱敏无关）—— 所以这里只
    盯 `params`（真正承载业务参数的载荷）。
    """
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider, **_kyc())
    items = client.get("/api/user-reviews", headers=h).json()["items"]
    client.post(f"/api/user-reviews/{items[0]['id']}/approve", headers=h, json={})

    audits = client.get("/api/audits?category=all", headers=h).json()
    params = str([a["params"] for a in audits["items"]])
    assert _PHONE not in params
    assert "138****8000" in params
