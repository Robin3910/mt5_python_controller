"""自助改绑手机号端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖：密码路径改绑、旧号验证码路径改绑、2FA 叠加校验、新号被占 409、
票据越权（uid / 号码 / 类型三类都被拒）、验证码用途隔离（登录码不能用于
改绑、旧号码不能用于验证新号）、发码配额、未绑定手机号时跳过身份确认、
发码响应不泄露号码是否被占用、改绑后旧号不能再验证码登录。

**不真发短信**：注入 FakeSmsProvider。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.captcha_provider import FakeCaptchaVerifier, reset_verifier_cache, set_verifier
from app.redis_store import RedisStore
from app.security import create_phone_pending_jwt, create_phone_rebind_jwt
from app.sms_config import PURPOSE_AUTH, PURPOSE_REBIND_NEW, PURPOSE_REBIND_OLD
from app.sms_provider import FakeSmsProvider, reset_provider_cache, set_provider
from app.sms_service import normalize_phone
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"

_OLD_PHONE = "13800138000"
_OLD_E164 = "+8613800138000"
_NEW_PHONE = "13900139000"
_NEW_E164 = "+8613900139000"
_THIRD_PHONE = "13700137000"

_FAKE_SERVER = fakeredis.FakeServer()


def _flush_fake() -> None:
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).flushall()


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
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
        "daily_limit_per_phone": 100,
        "hourly_limit_per_ip": 100,
        "daily_limit_per_ip": 100,
        "daily_limit_total": 1000,
        "max_verify_attempts": 5,
    }
    body.update(overrides)
    r = client.put("/api/config/sms", json=body, headers=h)
    assert r.status_code == 200, r.text


def _clear_cooldown(phone: str) -> None:
    """清掉发送冷却锁（用例内要连续发多次时用）。"""
    from app.redis_store import K_SMS_LOCK

    try:
        key_phone = normalize_phone(phone)
    except Exception:
        key_phone = phone
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).delete(K_SMS_LOCK.format(key_phone))


def _last_code(provider, phone=_NEW_E164) -> str:
    for item in reversed(provider.sent):
        if item["phone"] == phone:
            return item["code"]
    raise AssertionError(f"没有向 {phone} 发出验证码")


def _create_user(client, h, username: str, *, phone: str | None = None,
                 password: str = "pass1234") -> int:
    """建一个普通用户，返回 user_id。"""
    r = client.post(
        "/api/users",
        json={"username": username, "password": password},
        headers=h,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    if phone:
        r = client.patch(f"/api/users/{uid}", json={"phone": phone}, headers=h)
        assert r.status_code == 200, r.text
    return uid


def _user_token(client, username: str, password: str = "pass1234") -> str:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send_rebind(client, token, target="new", phone=None, **extra):
    payload = {"target": target}
    if phone is not None:
        payload["phone"] = phone
    payload.update(extra)
    return client.post("/api/me/phone/send-code", json=payload, headers=_headers(token))


def _verify_new(client, token, phone=_NEW_PHONE, code=None):
    if code is None:
        raise AssertionError("需要显式传验证码")
    return client.post(
        "/api/me/phone/verify-new",
        json={"phone": phone, "code": code},
        headers=_headers(token),
    )


def _rebind(client, token, **body):
    payload = {"new_phone": _NEW_PHONE}
    payload.update(body)
    return client.post("/api/me/phone/rebind", json=payload, headers=_headers(token))


def _full_new_ticket(client, provider, token, phone=_NEW_PHONE) -> str:
    """走完「新号发码 → 校验 → 拿票据」，返回票据。"""
    _clear_cooldown(phone)
    r = _send_rebind(client, token, target="new", phone=phone)
    assert r.status_code == 200, r.text
    code = _last_code(provider, "+86" + phone)
    r = _verify_new(client, token, phone=phone, code=code)
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


# ---------------------------------------------------------------------------
# 基本链路
# ---------------------------------------------------------------------------
def test_rebind_with_password(client, provider):
    """密码路径：验证新号 + 密码 → 改绑成功。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    uid = _create_user(client, h, "rbuser", phone=_OLD_PHONE)
    token = _user_token(client, "rbuser")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["phone"] == "139****9000"

    # 数据库里确实是新号
    users = client.get("/api/users", headers=h).json()
    target = next(u for u in users if u["id"] == uid)
    assert target["phone"] == "139****9000"
    assert target["phone_verified"] is True


def test_rebind_with_old_code(client, provider):
    """旧号验证码路径：给「没设密码」的用户用。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbcodeless", phone=_OLD_PHONE)
    token = _user_token(client, "rbcodeless")

    ticket = _full_new_ticket(client, provider, token)

    # 旧号验证码由服务端按「当前绑定号码」发
    _clear_cooldown(_OLD_PHONE)
    r = _send_rebind(client, token, target="old")
    assert r.status_code == 200, r.text
    old_code = _last_code(provider, _OLD_E164)

    r = _rebind(client, token, ticket=ticket, auth_method="old_code", old_code=old_code)
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "139****9000"


def test_rebind_password_wrong_401(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbwrong", phone=_OLD_PHONE)
    token = _user_token(client, "rbwrong")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="wrongpass")
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "密码不正确"


def test_rebind_old_code_wrong_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rboldwrong", phone=_OLD_PHONE)
    token = _user_token(client, "rboldwrong")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="old_code", old_code="000000")
    assert r.status_code == 400, r.text


def test_rebind_requires_auth(client, provider):
    """未登录不得发码。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    r = client.post("/api/me/phone/send-code", json={"target": "new", "phone": _NEW_PHONE})
    assert r.status_code == 401, r.text


def test_rebind_new_phone_taken_409(client, provider):
    """新号已被别的账号绑定 → 409。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbowner", phone=_NEW_PHONE)
    _create_user(client, h, "rbother", phone=_OLD_PHONE)
    token = _user_token(client, "rbother")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")
    assert r.status_code == 409, r.text


def test_rebind_to_same_phone_is_idempotent(client, provider):
    """改回自己原来的号：幂等成功。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbsame", phone=_OLD_PHONE)
    token = _user_token(client, "rbsame")

    # 新号就是自己的旧号
    ticket = _full_new_ticket(client, provider, token, phone=_OLD_PHONE)
    r = _rebind(
        client, token, ticket=ticket, auth_method="password",
        password="pass1234", new_phone=_OLD_PHONE,
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "138****8000"


def test_rebind_without_bound_phone_skips_identity(client, provider):
    """账号没绑定手机号 → 跳过身份确认，只验新号即可。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbnophone")
    token = _user_token(client, "rbnophone")

    ticket = _full_new_ticket(client, provider, token)
    # 不给 auth_method 也能过（服务端跳过身份确认）
    r = _rebind(client, token, ticket=ticket)
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "139****9000"


# ---------------------------------------------------------------------------
# 2FA：改绑强度不得低于登录
# ---------------------------------------------------------------------------
def test_rebind_2fa_user_requires_totp(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rb2fa", phone=_OLD_PHONE)
    token = _user_token(client, "rb2fa")

    # 绑定并启用 TOTP
    r = client.post("/api/2fa/setup", headers=_headers(token))
    secret = r.json()["secret"]
    import pyotp

    r = client.post(
        "/api/2fa/confirm",
        json={"totp_code": pyotp.TOTP(secret).now()},
        headers=_headers(token),
    )
    assert r.status_code == 200, r.text

    ticket = _full_new_ticket(client, provider, token)
    # 只给密码、缺 TOTP → 401
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")
    assert r.status_code == 401, r.text

    # 带上正确 TOTP → 成功
    r = _rebind(
        client, token, ticket=ticket, auth_method="password", password="pass1234",
        totp_code=pyotp.TOTP(secret).now(),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 票据：类型 / uid / 号码 三者都要对上
# ---------------------------------------------------------------------------
def test_rebind_rejects_phone_pending_ticket(client, provider):
    """「手机号已验证」票据（登录 / 注册用）不能拿去改绑。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    uid = _create_user(client, h, "rbticket", phone=_OLD_PHONE)
    token = _user_token(client, "rbticket")

    bad = create_phone_pending_jwt(_NEW_E164, "auth")
    r = _rebind(client, token, ticket=bad, auth_method="password", password="pass1234")
    assert r.status_code == 401, r.text


def test_rebind_rejects_ticket_of_other_user(client, provider):
    """A 的新号票据不能改绑 B 的账号。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbu_a", phone=_OLD_PHONE)
    _create_user(client, h, "rbu_b", phone=_THIRD_PHONE)
    token_a = _user_token(client, "rbu_a")
    token_b = _user_token(client, "rbu_b")

    ticket_a = _full_new_ticket(client, provider, token_a)
    r = _rebind(client, token_b, ticket=ticket_a, auth_method="password", password="pass1234")
    assert r.status_code == 401, r.text


def test_rebind_rejects_ticket_with_other_phone(client, provider):
    """票据里的号码与提交的号码不一致 → 401。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbp", phone=_OLD_PHONE)
    token = _user_token(client, "rbp")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(
        client, token, ticket=ticket, auth_method="password",
        password="pass1234", new_phone=_THIRD_PHONE,
    )
    assert r.status_code == 401, r.text


def test_rebind_rejects_garbage_ticket(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbg", phone=_OLD_PHONE)
    token = _user_token(client, "rbg")
    r = _rebind(client, token, ticket="not-a-jwt", auth_method="password", password="pass1234")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 验证码用途隔离
# ---------------------------------------------------------------------------
def test_auth_code_cannot_verify_new_phone(client, provider):
    """登录用的验证码不能用于「验证新号」—— 否则登录码就能改绑。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbpurp", phone=_OLD_PHONE)
    token = _user_token(client, "rbpurp")

    # 发一条 auth 码到新号
    _clear_cooldown(_NEW_PHONE)
    r = client.post(
        "/api/auth/sms/send", json={"phone": _NEW_PHONE, "purpose": PURPOSE_AUTH}
    )
    assert r.status_code == 200, r.text
    code = _last_code(provider, _NEW_E164)

    r = _verify_new(client, token, phone=_NEW_PHONE, code=code)
    assert r.status_code == 400, r.text


def test_old_code_cannot_verify_new_phone(client, provider):
    """旧号收到的 rebind_old 码不能用于「验证新号」。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rboldpurp", phone=_OLD_PHONE)
    token = _user_token(client, "rboldpurp")

    _clear_cooldown(_OLD_PHONE)
    r = _send_rebind(client, token, target="old")
    assert r.status_code == 200, r.text
    old_code = _last_code(provider, _OLD_E164)

    r = _verify_new(client, token, phone=_OLD_PHONE, code=old_code)
    assert r.status_code == 400, r.text


def test_rebind_new_code_cannot_login(client, provider):
    """改绑用的 rebind_new 码不能拿去登录。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rblogin", phone=_OLD_PHONE)
    token = _user_token(client, "rblogin")

    _clear_cooldown(_NEW_PHONE)
    r = _send_rebind(client, token, target="new", phone=_NEW_PHONE)
    assert r.status_code == 200, r.text
    code = _last_code(provider, _NEW_E164)

    # 登录接口用的是 auth 用途的码，改绑的码在这里不认
    r = client.post("/api/auth/sms/login", json={"phone": _NEW_PHONE, "code": code})
    assert r.status_code == 400, r.text


def test_old_code_target_uses_current_bound_phone(client, provider):
    """target=old 的码只发到「当前绑定的号码」，不理会请求里带的号码。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rboldtarget", phone=_OLD_PHONE)
    token = _user_token(client, "rboldtarget")

    _clear_cooldown(_OLD_PHONE)
    _clear_cooldown(_THIRD_PHONE)
    r = _send_rebind(client, token, target="old", phone=_THIRD_PHONE)
    assert r.status_code == 200, r.text
    # 只向旧号发了码
    assert _last_code(provider, _OLD_E164)
    assert not any(item["phone"] == "+86" + _THIRD_PHONE for item in provider.sent)


def test_old_code_target_without_bound_phone_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbnobound")
    token = _user_token(client, "rbnobound")
    r = _send_rebind(client, token, target="old")
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 号码枚举防护
# ---------------------------------------------------------------------------
def test_send_code_response_identical_for_taken_and_free_phone(client, provider):
    """号被占用与号空闲时，发码响应体逐字节一致（不泄露占用状态）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbenum_a", phone=_NEW_PHONE)
    _create_user(client, h, "rbenum_b", phone=_OLD_PHONE)
    token = _user_token(client, "rbenum_b")

    _clear_cooldown(_NEW_PHONE)
    r_taken = _send_rebind(client, token, target="new", phone=_NEW_PHONE)

    _clear_cooldown(_THIRD_PHONE)
    r_free = _send_rebind(client, token, target="new", phone=_THIRD_PHONE)

    assert r_taken.status_code == r_free.status_code == 200
    assert r_taken.json() == r_free.json()


def test_taken_phone_still_receives_code_then_409_at_submit(client, provider):
    """被占用的号照样能收到验证码 —— 占用状态只在提交时以 409 呈现。

    跳过发码会让用户卡在第一步（收不到码）却换不来隐私收益：能收到验证码
    本身就证明调用方是这个号的持有者。
    """
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbenum_c", phone=_NEW_PHONE)
    _create_user(client, h, "rbenum_d", phone=_OLD_PHONE)
    token = _user_token(client, "rbenum_d")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "该手机号已被其他账号绑定"


# ---------------------------------------------------------------------------
# 限频：改绑专属配额（按账号）
# ---------------------------------------------------------------------------
def test_rebind_quota_per_account(client, provider):
    """同一账号 24h 内改绑发码次数达上限 → 429。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbquota", phone=_OLD_PHONE)
    token = _user_token(client, "rbquota")

    from app.phone_rebind import REBIND_QUOTA_PER_DAY

    for i in range(REBIND_QUOTA_PER_DAY):
        _clear_cooldown(_NEW_PHONE)
        r = _send_rebind(client, token, target="new", phone=_NEW_PHONE)
        assert r.status_code == 200, f"第 {i + 1} 次应放行：{r.text}"

    _clear_cooldown(_NEW_PHONE)
    r = _send_rebind(client, token, target="new", phone=_NEW_PHONE)
    assert r.status_code == 429, r.text


def test_rebind_quota_is_per_account_not_global(client, provider):
    """配额按账号算：另一个账号不受影响。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbq_a", phone=_OLD_PHONE)
    _create_user(client, h, "rbq_b", phone=_THIRD_PHONE)
    token_a = _user_token(client, "rbq_a")
    token_b = _user_token(client, "rbq_b")

    from app.phone_rebind import REBIND_QUOTA_PER_DAY

    for _ in range(REBIND_QUOTA_PER_DAY):
        _clear_cooldown(_NEW_PHONE)
        assert _send_rebind(client, token_a, target="new", phone=_NEW_PHONE).status_code == 200

    _clear_cooldown(_NEW_PHONE)
    assert _send_rebind(client, token_a, target="new", phone=_NEW_PHONE).status_code == 429
    # 另一个账号仍可发码
    _clear_cooldown(_NEW_PHONE)
    assert _send_rebind(client, token_b, target="new", phone=_NEW_PHONE).status_code == 200


# ---------------------------------------------------------------------------
# 自适应人机验证
# ---------------------------------------------------------------------------
def test_rebind_captcha_required_returns_428(client, provider, captcha):
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene1", captcha_phone_threshold=1)
    _create_user(client, h, "rbcap", phone=_OLD_PHONE)
    token = _user_token(client, "rbcap")

    assert _send_rebind(client, token, target="new", phone=_NEW_PHONE).status_code == 200
    _clear_cooldown(_NEW_PHONE)
    r = _send_rebind(client, token, target="new", phone=_NEW_PHONE)
    assert r.status_code == 428, r.text
    assert r.json()["detail"] == "需要完成人机验证"

    # 带参重试 → 放行
    _clear_cooldown(_NEW_PHONE)
    r = _send_rebind(
        client, token, target="new", phone=_NEW_PHONE, captcha_verify_param="tok"
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 改绑后的效果
# ---------------------------------------------------------------------------
def test_old_phone_cannot_login_after_rebind(client, provider):
    """改绑后旧号不能再验证码登录（它已不绑定本账号）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbafter", phone=_OLD_PHONE)
    token = _user_token(client, "rbafter")

    ticket = _full_new_ticket(client, provider, token)
    r = _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")
    assert r.status_code == 200, r.text

    # 用旧号登录 → 404（该号码尚未注册 / 已不属于本账号）
    _clear_cooldown(_OLD_PHONE)
    client.post("/api/auth/sms/send", json={"phone": _OLD_PHONE, "purpose": PURPOSE_AUTH})
    code = _last_code(provider, _OLD_E164)
    r = client.post("/api/auth/sms/login", json={"phone": _OLD_PHONE, "code": code})
    assert r.status_code == 404, r.text

    # 用新号登录 → 成功
    _clear_cooldown(_NEW_PHONE)
    client.post("/api/auth/sms/send", json={"phone": _NEW_PHONE, "purpose": PURPOSE_AUTH})
    code = _last_code(provider, _NEW_E164)
    r = client.post("/api/auth/sms/login", json={"phone": _NEW_PHONE, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["token"]


def test_me_returns_masked_phone_after_rebind(client, provider):
    """改绑后 /api/me 返回新的**脱敏**号码。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _create_user(client, h, "rbme", phone=_OLD_PHONE)
    token = _user_token(client, "rbme")

    ticket = _full_new_ticket(client, provider, token)
    _rebind(client, token, ticket=ticket, auth_method="password", password="pass1234")

    r = client.get("/api/me", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "139****9000"
    assert r.json()["phone_verified"] is True


# ---------------------------------------------------------------------------
# 短信服务未配置
# ---------------------------------------------------------------------------
def test_rebind_404_when_sms_disabled(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h, enabled=False)
    _create_user(client, h, "rbnosms", phone=_OLD_PHONE)
    token = _user_token(client, "rbnosms")
    r = _send_rebind(client, token, target="new", phone=_NEW_PHONE)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 回归：个人中心资料接口仍然不能改手机号
# ---------------------------------------------------------------------------
def test_profile_update_still_rejects_phone(client, provider):
    """绕过改绑流程直接改 phone 是不可能的。"""
    h = auth_headers(client)
    _create_user(client, h, "rbprofile", phone=_OLD_PHONE)
    token = _user_token(client, "rbprofile")

    r = client.patch(
        "/api/me/profile", json={"phone": _NEW_PHONE}, headers=_headers(token)
    )
    # pydantic 忽略未知字段 → 手机号不变
    assert r.status_code == 200, r.text
    assert r.json().get("phone") == "138****8000"
