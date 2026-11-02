"""手机号注册 / 验证码登录端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖：号码归一化、验证码生命周期（一次性 / 5 次作废 / purpose 隔离）、
限频、注册全流程（待审核 / 免审核）、重复注册 409、号码枚举防护、
验证码登录、2FA 用户仍走两步、票据越权被拒、未配置时 404、
管理员启用后登录成功，以及原 /api/login + /api/me 回归不变。

**不真发短信**：注入 FakeSmsProvider。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.security import create_phone_pending_jwt
from app.sms_config import PURPOSE_LOGIN, PURPOSE_REGISTER
from app.sms_provider import FakeSmsProvider, reset_provider_cache, set_provider
from app.sms_service import normalize_phone
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PHONE = "13800138000"
_E164 = "+8613800138000"


_FAKE_SERVER = fakeredis.FakeServer()


def _flush_fake() -> None:
    """清空共享的 fakeredis：限频计数是跨用例共享的，不清会互相干扰。

    用同步客户端执行（FakeServer 本身是同步的）。
    """
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).flushall()


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        # 共享 FakeServer：store fixture 能看到同一份数据
        return RedisStore(
            fakeredis.FakeAsyncRedis(server=_FAKE_SERVER, decode_responses=True)
        )

    reset_test_db(_TEST_DB)
    _flush_fake()
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


@pytest.fixture
def store(client):
    """直连测试用 Redis（FakeServer 是同步的，跨事件循环访问同一份数据安全）。

    用于绕过发送冷却锁，单独验证「每日上限」这类计数维度。
    """
    reset_provider_cache()
    fake = RedisStore(
        fakeredis.FakeAsyncRedis(server=_FAKE_SERVER, decode_responses=True)
    )
    yield fake
    reset_provider_cache()


@pytest.fixture
def provider():
    """注入 FakeSmsProvider：验证码明文记在内存里，供用例读取。"""
    reset_provider_cache()
    fake = FakeSmsProvider()
    set_provider(fake)
    yield fake
    reset_provider_cache()


def _enable_sms(client, h, **overrides) -> None:
    """开启短信服务并写入签名 / 模板（未配置时所有短信接口返回 404）。"""
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


def _send(client, phone=_PHONE, purpose=PURPOSE_REGISTER):
    return client.post("/api/auth/sms/send", json={"phone": phone, "purpose": purpose})


def _clear_cooldown(phone: str = _PHONE) -> None:
    """清掉发送冷却锁（用例内要连续发多次时用）。

    冷却锁（60s）本身由 test_send_code_rate_limited 覆盖；这里只是让同一用例里
    的第二次发送不被它挡住。锁的 key 用归一化后的 E.164。
    """
    from app.redis_store import K_SMS_LOCK
    from app.sms_service import normalize_phone

    try:
        key_phone = normalize_phone(phone)
    except Exception:
        key_phone = phone
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).delete(K_SMS_LOCK.format(key_phone))


def _send_fresh(client, phone=_PHONE, purpose=PURPOSE_REGISTER):
    """发送验证码（先清冷却锁）。"""
    _clear_cooldown(phone)
    return _send(client, phone=phone, purpose=purpose)


def _last_code(provider, phone=_E164) -> str:
    for item in reversed(provider.sent):
        if item["phone"] == phone:
            return item["code"]
    raise AssertionError(f"没有向 {phone} 发出验证码")


def _verify(client, code, phone=_PHONE, purpose=PURPOSE_REGISTER):
    return client.post(
        "/api/auth/sms/verify", json={"phone": phone, "code": code, "purpose": purpose}
    )


# ---------------------------------------------------------------------------
# 号码归一化（单元）
# ---------------------------------------------------------------------------
def test_normalize_phone_variants():
    assert normalize_phone("13800138000") == _E164
    assert normalize_phone("+8613800138000") == _E164
    assert normalize_phone("8613800138000") == _E164
    assert normalize_phone("138 0013 8000") == _E164
    assert normalize_phone("138-0013-8000") == _E164
    assert normalize_phone(" 13800138000 ") == _E164


@pytest.mark.parametrize("bad", ["", "12800138000", "1380013800", "23800138000", "abcdefghijk", "+1 380 013 8000"])
def test_normalize_phone_rejects_bad(bad):
    from app.sms_service import SmsInvalidPhone

    with pytest.raises(SmsInvalidPhone):
        normalize_phone(bad)


def test_mask_phone():
    from app.sms_service import mask_phone

    assert mask_phone(_E164) == "138****8000"


# ---------------------------------------------------------------------------
# 未配置 → 404
# ---------------------------------------------------------------------------
def test_sms_endpoints_404_when_not_configured(client, provider):
    """sign_name / template_code 为空 = 未配置：所有短信接口 404（不暴露原因）。"""
    h = auth_headers(client)
    client.put(
        "/api/config/sms",
        json={"enabled": True, "sign_name": "", "template_code": "100001"},
        headers=h,
    )
    assert _send(client).status_code == 404
    assert client.get("/api/auth/sms/status").json()["enabled"] is False


def test_sms_endpoints_404_when_disabled(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h, enabled=False)
    assert _send(client).status_code == 404


# ---------------------------------------------------------------------------
# 发送 / 校验
# ---------------------------------------------------------------------------
def test_send_code_ok(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _send(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "expire_in": 300, "cooldown": 60}
    assert len(provider.sent) == 1
    assert provider.sent[0]["code"].isdigit() and len(provider.sent[0]["code"]) == 6


def test_send_code_invalid_phone(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    assert _send(client, phone="12345").status_code == 400


def test_aliyun_request_uses_local_number_and_6_digits():
    """阿里云 PhoneNumber 只放国内纯号码（不带 +86），验证码长度必须显式给 6。

    传 E.164 会被阿里云拒；不传 code_length 时阿里云默认只发 4 位，
    与我们校验的 6 位对不上。
    """
    from app.sms_provider import AliyunSmsAuthProvider, _local_number

    assert _local_number("+8613800138000") == "13800138000"
    assert _local_number("13800138000") == "13800138000"

    captured: dict = {}

    class _FakeBody:
        code = "OK"
        request_id = "req-1"
        message = "成功"
        biz_id = "biz-1"

    class _FakeResponse:
        body = _FakeBody()

    class _FakeClient:
        def send_sms_verify_code(self, request):
            captured["request"] = request
            return _FakeResponse()

    provider = AliyunSmsAuthProvider("ak", "sk", "dypnsapi.aliyuncs.com")
    provider._client = lambda: _FakeClient()  # type: ignore[method-assign]

    result = provider.send_code(
        "+8613800138000", "123456", ttl_minutes=5, sign_name="签名", template_code="100001",
    )
    assert result.ok
    req = captured["request"]
    assert req.phone_number == "13800138000"
    assert req.country_code == "86"
    assert req.code_length == 6
    assert req.valid_time == 300


def test_send_code_rate_limited(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    assert _send(client).status_code == 200
    assert _send(client).status_code == 429


def test_send_code_daily_limit(client, provider):
    """同号每日上限：绕开发送冷却锁后，累计到上限即 429。"""
    h = auth_headers(client)
    _enable_sms(client, h, send_interval_seconds=1, daily_limit_per_phone=2)
    assert _send(client).status_code == 200
    _clear_cooldown()
    assert _send(client).status_code == 200
    _clear_cooldown()
    # 第 3 条超出 daily_limit=2
    assert _send(client).status_code == 429


def test_send_code_hourly_ip_limit(client, provider):
    """同 IP 每小时上限。"""
    h = auth_headers(client)
    _enable_sms(client, h, send_interval_seconds=1, hourly_limit_per_ip=2)
    assert _send(client, phone="13900139000").status_code == 200
    _clear_cooldown("13900139000")
    assert _send(client, phone="13700137000").status_code == 200
    _clear_cooldown("13700137000")
    # 第 3 条超出 hourly_limit_per_ip=2（同一 IP）
    assert _send(client, phone="13600136000").status_code == 429


def test_verify_code_success_and_burn(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    code = _last_code(provider)
    r = _verify(client, code)
    assert r.status_code == 200, r.text
    assert r.json()["ticket"]
    # 一次性：同一个码不能用第二次
    assert _verify(client, code).status_code == 400


def test_verify_code_wrong_then_ok(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    code = _last_code(provider)
    assert _verify(client, "000000").status_code == 400
    assert _verify(client, code).status_code == 200


def test_verify_code_burned_after_max_attempts(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h, max_verify_attempts=5)
    _send(client)
    code = _last_code(provider)
    for _ in range(5):
        assert _verify(client, "000000").status_code == 400
    # 超限后即使拿对码也失效
    assert _verify(client, code).status_code == 400


def test_purpose_isolation_register_code_cannot_login(client, provider):
    """register 的验证码不能通过 login 的校验（key 里带 purpose）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    code = _last_code(provider)
    assert _verify(client, code, purpose=PURPOSE_LOGIN).status_code == 400
    assert _verify(client, code, purpose=PURPOSE_REGISTER).status_code == 200


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def test_register_pending_approval_then_admin_enable_then_login(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]

    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket, "nickname": "张三"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pending_approval"] is True
    assert body.get("token") is None
    user_id = body["user_id"]

    # 审核前：验证码登录被拒（is_active=false）
    _send_fresh(client, purpose=PURPOSE_LOGIN)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 403, r.text

    # 管理员在用户管理页启用
    users = client.get("/api/users", headers=h).json()
    target = next(u for u in users if u["id"] == user_id)
    assert target["is_active"] is False
    assert target["phone"] == "138****8000"
    r = client.patch(f"/api/users/{user_id}", json={"is_active": True}, headers=h)
    assert r.status_code == 200, r.text

    # 启用后：验证码登录成功
    _send_fresh(client, purpose=PURPOSE_LOGIN)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["token"]
    assert r.json()["requires_2fa"] is False


def test_register_without_approval_returns_token(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pending_approval"] is False
    assert body["token"]
    assert body["token_type"] == "bearer"


def test_register_duplicate_phone_409(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    assert client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": ticket}
    ).status_code == 201

    _send_fresh(client, purpose=PURPOSE_REGISTER)
    ticket2 = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket2})
    assert r.status_code == 409, r.text


def test_register_ticket_purpose_cross_use_rejected(client, provider):
    """register 票据不能用于 login 用途。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    bad_ticket = create_phone_pending_jwt(_E164, PURPOSE_LOGIN)
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": bad_ticket})
    assert r.status_code == 401, r.text


def test_register_ticket_phone_mismatch_rejected(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    bad_ticket = create_phone_pending_jwt("+8613900139000", PURPOSE_REGISTER)
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": bad_ticket})
    assert r.status_code == 401, r.text


def test_register_auto_username_and_custom_username(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "m13800138000"


def test_register_custom_username_conflict_409(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = client.post("/api/users", json={"username": "taken_name", "password": "pass1234"}, headers=h)
    assert r.status_code == 201, r.text
    _send_fresh(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post(
        "/api/auth/register",
        json={"phone": _PHONE, "ticket": ticket, "username": "taken_name"},
    )
    assert r.status_code == 409, r.text


def test_send_response_identical_for_registered_and_unregistered(client, provider):
    """号码枚举防护：已注册 / 未注册的发送响应体逐字节一致。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    assert client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": ticket}
    ).status_code == 201

    _clear_cooldown()
    r_registered = _send(client, phone=_PHONE, purpose=PURPOSE_REGISTER)
    _clear_cooldown("13900139000")
    r_unregistered = _send(client, phone="13900139000", purpose=PURPOSE_REGISTER)

    assert r_registered.status_code == r_unregistered.status_code == 200
    assert r_registered.json() == r_unregistered.json()


# ---------------------------------------------------------------------------
# 验证码登录
# ---------------------------------------------------------------------------
def test_login_phone_not_registered_404(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, phone="13900139000", purpose=PURPOSE_LOGIN)
    code = _last_code(provider, "+8613900139000")
    r = client.post("/api/auth/sms/login", json={"phone": "13900139000", "code": code})
    assert r.status_code == 404, r.text


def test_login_disabled_user_403(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    user_id = r.json()["user_id"]
    # 管理员启用后再禁用
    client.patch(f"/api/users/{user_id}", json={"is_active": True}, headers=h)
    client.patch(f"/api/users/{user_id}", json={"is_active": False}, headers=h)

    _send_fresh(client, purpose=PURPOSE_LOGIN)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 403, r.text


def test_login_2fa_user_still_requires_2fa(client, provider):
    """已开启 2FA 的用户走验证码登录后仍需 2FA 第二步。"""
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    _send(client, purpose=PURPOSE_REGISTER)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    username = r.json()["username"]
    token = r.json()["token"]

    # 绑定并启用 TOTP
    r = client.post("/api/2fa/setup", headers={"Authorization": f"Bearer {token}"})
    secret = r.json()["secret"]
    import pyotp

    code = pyotp.TOTP(secret).now()
    r = client.post("/api/2fa/confirm", json={"totp_code": code}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert username

    _send_fresh(client, purpose=PURPOSE_LOGIN)
    sms_code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": sms_code})
    assert r.status_code == 200, r.text
    assert r.json()["requires_2fa"] is True
    assert r.json()["login_token"]
    assert not r.json().get("token")


# ---------------------------------------------------------------------------
# 管理员手工绑定手机号
# ---------------------------------------------------------------------------
def test_admin_bind_phone_manual_login(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = client.post("/api/users", json={"username": "manualuser", "password": "pass1234"}, headers=h)
    user_id = r.json()["id"]
    r = client.patch(f"/api/users/{user_id}", json={"phone": _PHONE}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "138****8000"
    assert r.json()["phone_verified"] is False

    # 手工绑定的用户同样可用验证码登录
    _send_fresh(client, purpose=PURPOSE_LOGIN)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["token"]


def test_admin_bind_phone_conflict_409(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    a = client.post("/api/users", json={"username": "user_a", "password": "pass1234"}, headers=h).json()
    b = client.post("/api/users", json={"username": "user_b", "password": "pass1234"}, headers=h).json()
    assert client.patch(f"/api/users/{a['id']}", json={"phone": _PHONE}, headers=h).status_code == 200
    r = client.patch(f"/api/users/{b['id']}", json={"phone": _PHONE}, headers=h)
    assert r.status_code == 409, r.text


def test_admin_bind_invalid_phone_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    a = client.post("/api/users", json={"username": "user_x", "password": "pass1234"}, headers=h).json()
    r = client.patch(f"/api/users/{a['id']}", json={"phone": "123"}, headers=h)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 回归：原登录链路不变
# ---------------------------------------------------------------------------
def test_original_login_and_me_unchanged(client, provider):
    h = auth_headers(client)
    r = client.get("/api/me", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # MeOut 里的 phone 是**脱敏值**（个人中心只读展示用），不是完整号码。
    # 未绑定手机号时为 None。
    assert body.get("phone") is None
    assert body.get("phone_verified") is False
    assert body["username"] == "admin"
    assert body["is_admin"] is True


def test_sms_config_admin_only(client, provider):
    """短信配置只对管理员开放。"""
    h = auth_headers(client)
    r = client.post("/api/users", json={"username": "normaluser", "password": "pass1234"}, headers=h)
    uid = r.json()["id"]
    nh = {"Authorization": "Bearer " + client.post(
        "/api/login", json={"username": "normaluser", "password": "pass1234"}
    ).json()["token"]}
    assert client.get("/api/config/sms", headers=nh).status_code == 403
    assert client.put("/api/config/sms", json={"enabled": True}, headers=nh).status_code == 403
    assert client.get("/api/config/sms", headers=h).status_code == 200
