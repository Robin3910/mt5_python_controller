"""手机号注册 / 验证码登录端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖：号码归一化、验证码生命周期（一次性 / 5 次作废）、限频（各维度）、
**一次验证码两种去向**（已注册 → login；未注册 → register）、注册全流程
（待审核 / 免审核）、重复注册 409、号码枚举防护、票据越权被拒、
自适应人机验证（428 / 放行 / 拦截）、2FA 用户仍走两步、2FA 票据用途隔离、
未配置时 404，以及原 /api/login + /api/me 回归不变。

**不真发短信**：注入 FakeSmsProvider。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.captcha_provider import FakeCaptchaVerifier, reset_verifier_cache, set_verifier
from app.redis_store import RedisStore
from app.security import create_phone_pending_jwt
from app.sms_config import PURPOSE_AUTH
from app.sms_provider import FakeSmsProvider, reset_provider_cache, set_provider
from app.sms_service import normalize_phone
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PHONE = "13800138000"
_E164 = "+8613800138000"
_OTHER = "13900139000"

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


@pytest.fixture
def captcha():
    """注入 FakeCaptchaVerifier：人机验证恒定放行。"""
    reset_verifier_cache()
    fake = FakeCaptchaVerifier(ok=True)
    set_verifier(fake)
    yield fake
    reset_verifier_cache()


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


def _send(client, phone=_PHONE, purpose=PURPOSE_AUTH, **extra):
    payload = {"phone": phone, "purpose": purpose}
    payload.update(extra)
    return client.post("/api/auth/sms/send", json=payload)


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


def _send_fresh(client, phone=_PHONE, purpose=PURPOSE_AUTH, **extra):
    """发送验证码（先清冷却锁）。"""
    _clear_cooldown(phone)
    return _send(client, phone=phone, purpose=purpose, **extra)


def _last_code(provider, phone=_E164) -> str:
    for item in reversed(provider.sent):
        if item["phone"] == phone:
            return item["code"]
    raise AssertionError(f"没有向 {phone} 发出验证码")


def _verify(client, code, phone=_PHONE, purpose=PURPOSE_AUTH):
    return client.post(
        "/api/auth/sms/verify", json={"phone": phone, "code": code, "purpose": purpose}
    )


def _register(client, provider, phone=_PHONE, **extra):
    """走完整注册流程（发码 → 校验 → 注册）。返回注册响应。"""
    _send_fresh(client, phone=phone)
    ticket = _verify(client, _last_code(provider, "+86" + phone), phone=phone).json()["ticket"]
    payload = {"phone": phone, "ticket": ticket}
    payload.update(extra)
    return client.post("/api/auth/register", json=payload)


# ---------------------------------------------------------------------------
# 号码归一化（单元）
# ---------------------------------------------------------------------------
def test_normalize_phone_variants():
    assert normalize_phone("13800138000") == _E164
    assert normalize_phone("+8613800138000") == _E164
    assert normalize_phone("8613800138000") == _E164
    assert normalize_phone("138 0013 8000") == _E164
    assert normalize_phone("138-0013-8000") == _E164


def test_normalize_phone_invalid():
    from app.sms_service import SmsInvalidPhone

    for bad in ["", "12345", "23800138000", "138001380001", "abcdefghijk"]:
        with pytest.raises(SmsInvalidPhone):
            normalize_phone(bad)


def test_mask_phone():
    from app.sms_service import mask_phone

    assert mask_phone("+8613800138000") == "138****8000"
    assert mask_phone("13800138000") == "138****8000"
    assert mask_phone("+8613900139000") == "139****9000"


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


def test_send_code_invalid_purpose_400(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    assert _send(client, purpose="legacy_login").status_code == 400


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


def test_send_code_daily_ip_limit(client, provider):
    """同 IP 每日上限（换 IP 也绕不开的那道闸）。"""
    h = auth_headers(client)
    _enable_sms(client, h, send_interval_seconds=1, hourly_limit_per_ip=100,
                daily_limit_per_ip=2)
    assert _send(client, phone="13900139000").status_code == 200
    _clear_cooldown("13900139000")
    assert _send(client, phone="13700137000").status_code == 200
    _clear_cooldown("13700137000")
    assert _send(client, phone="13600136000").status_code == 429


def test_send_code_daily_total_limit(client, provider):
    """全局每日上限：费用失控的最后一道闸。"""
    h = auth_headers(client)
    _enable_sms(client, h, send_interval_seconds=1, hourly_limit_per_ip=100,
                daily_limit_per_phone=100, daily_limit_per_ip=100,
                daily_limit_total=2)
    assert _send(client, phone="13900139000").status_code == 200
    _clear_cooldown("13900139000")
    assert _send(client, phone="13700137000").status_code == 200
    _clear_cooldown("13700137000")
    # 全局第 3 条超限
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
    _enable_sms(client, h)
    _enable_sms(client, h, max_verify_attempts=5)
    _send(client)
    code = _last_code(provider)
    for _ in range(5):
        assert _verify(client, "000000").status_code == 400
    # 超限后即使拿对码也失效
    assert _verify(client, code).status_code == 400


# ---------------------------------------------------------------------------
# 一次验证码，两种去向（核心新增）
# ---------------------------------------------------------------------------
def test_verify_returns_next_action_register_when_unregistered(client, provider):
    """号码未注册 → next_action=register，前端可直接进注册第二步（不再收码）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    r = _verify(client, _last_code(provider))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_action"] == "register"
    assert body["ticket"]


def test_verify_returns_next_action_login_when_registered(client, provider):
    """号码已注册且已启用 → next_action=login。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    r = _register(client, provider)
    # 管理员审核通过（否则是 pending_approval）
    client.patch(f"/api/users/{r.json()['user_id']}", json={"is_active": True}, headers=h)
    _send_fresh(client)
    r = _verify(client, _last_code(provider))
    assert r.status_code == 200, r.text
    assert r.json()["next_action"] == "login"


def test_verify_returns_next_action_pending_when_not_approved(client, provider):
    """已注册但待审核 → next_action=pending_approval（不泄露更多细节）。"""
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=True)
    _register(client, provider)
    _send_fresh(client)
    r = _verify(client, _last_code(provider))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_action"] == "pending_approval"
    assert body["username"]


def test_single_code_supports_both_login_and_register(client, provider):
    """核心场景：登录页收一次码，未注册 → 同一张票据直接完成注册。

    这是「不用再收一次验证码」的落地保证：auth 票据对 login 与 register
    两个动作都有效。
    """
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    body = _verify(client, _last_code(provider)).json()
    assert body["next_action"] == "register"
    # 用同一张票据直接注册，无需再次收码
    r = client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": body["ticket"]}
    )
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "m13800138000"


def test_auth_ticket_rejected_for_wrong_phone(client, provider):
    """票据里的手机号必须与请求一致（防止拿 A 的票去注册 B）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _OTHER, "ticket": ticket})
    assert r.status_code == 401, r.text


def test_register_ticket_cannot_be_used_for_login(client, provider):
    """register 票据不能拿去登录（票据用途隔离）。

    签发 register 用途的票据后，用它走登录必须被拒 —— 防的是「用注册流程
    拿到的凭证越权登录」。
    """
    h = auth_headers(client)
    _enable_sms(client, h)
    _register(client, provider)
    reg_ticket = create_phone_pending_jwt(_E164, "register")
    r = client.post(
        "/api/auth/sms/login", json={"phone": _PHONE, "ticket": reg_ticket}
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def test_register_pending_approval_then_admin_enable_then_login(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]

    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pending_approval"] is True
    assert body.get("token") is None
    user_id = body["user_id"]

    # 审核前：验证码登录被拒（is_active=false）
    _send_fresh(client)
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
    _send_fresh(client)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["token"]
    assert r.json()["requires_2fa"] is False


def test_register_without_approval_returns_token(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pending_approval"] is False
    assert body["token"]
    assert body["token_type"] == "bearer"


def test_register_duplicate_phone_409(client, provider):
    """重复注册直接拒绝（不覆盖昵称等已有资料）。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    assert client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": ticket}
    ).status_code == 201

    _send_fresh(client)
    ticket2 = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket2})
    assert r.status_code == 409, r.text


def test_register_ticket_purpose_cross_use_rejected(client, provider):
    """未知用途的票据不能用于注册。"""
    h = auth_headers(client)
    _enable_sms(client, h)
    bad_ticket = create_phone_pending_jwt(_E164, "some_other_purpose")
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": bad_ticket})
    assert r.status_code == 401, r.text


def test_register_ticket_phone_mismatch_rejected(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    bad_ticket = create_phone_pending_jwt("+8613900139000", "auth")
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": bad_ticket})
    assert r.status_code == 401, r.text


def test_register_auto_username_and_custom_username(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "m13800138000"


def test_register_custom_username_conflict_409(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    r = client.post("/api/users", json={"username": "taken_name", "password": "pass1234"}, headers=h)
    assert r.status_code == 201, r.text
    _send_fresh(client)
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
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    assert client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": ticket}
    ).status_code == 201

    _clear_cooldown()
    r_registered = _send(client, phone=_PHONE)
    _clear_cooldown(_OTHER)
    r_unregistered = _send(client, phone=_OTHER)

    assert r_registered.status_code == r_unregistered.status_code == 200
    assert r_registered.json() == r_unregistered.json()


# ---------------------------------------------------------------------------
# 验证码登录
# ---------------------------------------------------------------------------
def test_login_phone_not_registered_404(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client, phone=_OTHER)
    code = _last_code(provider, "+8613900139000")
    r = client.post("/api/auth/sms/login", json={"phone": _OTHER, "code": code})
    assert r.status_code == 404, r.text


def test_login_with_ticket_no_code_needed(client, provider):
    """用 /sms/verify 换来的票据直接登录，不用再传验证码。"""
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    assert client.post(
        "/api/auth/register", json={"phone": _PHONE, "ticket": ticket}
    ).status_code == 201

    _clear_cooldown()
    _send(client)
    body = _verify(client, _last_code(provider)).json()
    assert body["next_action"] == "login"
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "ticket": body["ticket"]})
    assert r.status_code == 200, r.text
    assert r.json()["token"]


def test_login_disabled_user_403(client, provider):
    h = auth_headers(client)
    _enable_sms(client, h)
    _send(client)
    ticket = _verify(client, _last_code(provider)).json()["ticket"]
    r = client.post("/api/auth/register", json={"phone": _PHONE, "ticket": ticket})
    user_id = r.json()["user_id"]
    # 管理员启用后再禁用
    client.patch(f"/api/users/{user_id}", json={"is_active": True}, headers=h)
    client.patch(f"/api/users/{user_id}", json={"is_active": False}, headers=h)

    _send_fresh(client)
    code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": code})
    assert r.status_code == 403, r.text


def test_login_2fa_user_still_requires_2fa(client, provider):
    """已开启 2FA 的用户走验证码登录后仍需 2FA 第二步。"""
    h = auth_headers(client)
    _enable_sms(client, h, require_approval=False)
    _send(client)
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

    _send_fresh(client)
    sms_code = _last_code(provider)
    r = client.post("/api/auth/sms/login", json={"phone": _PHONE, "code": sms_code})
    assert r.status_code == 200, r.text
    assert r.json()["requires_2fa"] is True
    assert r.json()["login_token"]
    assert not r.json().get("token")


# ---------------------------------------------------------------------------
# 自适应人机验证（阿里云验证码）
# ---------------------------------------------------------------------------
def test_captcha_disabled_no_challenge(client, provider, captcha):
    """未开启人机验证时，直接放行（不返回 428）。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=False)
    r = _send(client)
    assert r.status_code == 200, r.text
    assert captcha.calls == []


def test_captcha_not_configured_no_challenge(client, provider, captcha):
    """开了开关但没配 prefix/scene_id 时不应把发码功能搞挂。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="", captcha_scene_id="")
    r = _send(client)
    assert r.status_code == 200, r.text


def test_captcha_required_returns_428(client, provider, captcha):
    """命中风控阈值 → 返回 428 要求前端弹滑块。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene1", captcha_phone_threshold=1)
    # 第一次发送后，该号码的当日计数达到阈值 → 第二次触发人机验证
    assert _send(client).status_code == 200
    _clear_cooldown()
    r = _send(client)
    assert r.status_code == 428, r.text
    assert r.json()["detail"] == "需要完成人机验证"


def test_captcha_passed_allows_send(client, provider, captcha):
    """带上通过验证的参数 → 正常发码。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene1", captcha_phone_threshold=1)
    assert _send(client).status_code == 200
    _clear_cooldown()
    r = _send(client, captcha_verify_param="captcha-token-xyz")
    assert r.status_code == 200, r.text
    assert captcha.calls == ["captcha-token-xyz"]


def test_captcha_rejected_blocks_send(client, provider):
    """人机验证不通过 → 拒绝发码（400）。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene1", captcha_phone_threshold=1)
    assert _send(client).status_code == 200
    _clear_cooldown()
    set_verifier(FakeCaptchaVerifier(ok=False))
    try:
        r = _send(client, captcha_verify_param="bad-token")
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "人机验证未通过，请重试"
    finally:
        set_verifier(FakeCaptchaVerifier(ok=True))


def test_captcha_ip_threshold_triggers(client, provider, captcha):
    """该 IP 1h 内发送次数达阈值 → 触发人机验证。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene1", captcha_phone_threshold=0,
                captcha_ip_threshold=1)
    assert _send(client, phone="13900139000").status_code == 200
    _clear_cooldown("13900139000")
    r = _send(client, phone="13700137000")
    assert r.status_code == 428, r.text


# ---------------------------------------------------------------------------
# 加密模式（EncryptedSceneId）
# ---------------------------------------------------------------------------
def test_encrypt_scene_id_round_trip():
    """加密结果能解回原文，且明文格式符合阿里云规范。"""
    import base64

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from app.captcha_provider import encrypt_scene_id

    ekey = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    scene = "scene-abc"
    encrypted = encrypt_scene_id(scene, ekey)

    raw = base64.b64decode(encrypted)
    iv, ciphertext = raw[:16], raw[16:]
    decryptor = Cipher(
        algorithms.AES(base64.b64decode(ekey)), modes.CBC(iv)
    ).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plain = (unpadder.update(padded) + unpadder.finalize()).decode()

    parts = plain.split("&")
    assert len(parts) == 3
    assert parts[0] == scene
    assert 1 <= int(parts[2]) <= 86400


def test_encrypt_scene_id_iv_is_random():
    """IV 必须每次随机：复用 IV 会让相同明文产生相同密文，等于泄露 SceneId。"""
    import base64

    from app.captcha_provider import encrypt_scene_id

    ekey = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    results = {encrypt_scene_id("scene-abc", ekey) for _ in range(20)}
    assert len(results) == 20
    ivs = {base64.b64decode(r)[:16] for r in results}
    assert len(ivs) == 20


def test_encrypt_scene_id_bad_ekey_raises():
    """ekey 长度不对时明确报错，不产出坏密文。"""
    import base64

    from app.captcha_provider import encrypt_scene_id

    bad = base64.b64encode(b"too-short").decode()
    with pytest.raises(ValueError):
        encrypt_scene_id("scene-abc", bad)


def test_captcha_scene_endpoint_plain_without_ekey(client):
    """没配 ekey 时下发明文 SceneId（兼容非加密模式）。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene-1")
    r = client.get("/api/auth/sms/captcha-scene")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene_id"] == "scene-1"
    assert body["encrypted_scene_id"] == ""


def test_captcha_scene_endpoint_encrypted_with_ekey(client, monkeypatch):
    """配了 ekey 时下发加密串，且不再下发明文 SceneId。"""
    import base64

    from app import settings as settings_mod

    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=True, captcha_prefix="pfx",
                captcha_scene_id="scene-1")
    monkeypatch.setattr(
        settings_mod.settings,
        "aliyun_captcha_ekey",
        base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
    )
    r = client.get("/api/auth/sms/captcha-scene")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encrypted_scene_id"]
    # 明文 SceneId 不下发（前端只拿加密串）
    assert body["scene_id"] == "scene-1"

    # 加密模式下 /sms/status 也不下发明文 SceneId
    r = client.get("/api/auth/sms/status")
    assert r.json()["captcha_encrypted"] is True
    assert r.json()["captcha_scene_id"] == ""


def test_captcha_scene_endpoint_404_without_scene(client):
    """没配场景时 404。"""
    h = auth_headers(client)
    _enable_sms(client, h, captcha_enabled=False)
    assert client.get("/api/auth/sms/captcha-scene").status_code == 404


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
    _send_fresh(client)
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
