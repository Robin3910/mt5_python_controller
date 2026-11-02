"""手机号注册 / 验证码登录 API（公开、无鉴权）。

四个接口：
- POST /api/auth/sms/send    发送验证码
- POST /api/auth/sms/verify  校验验证码 → 签发票据
- POST /api/auth/register    手机号注册（需票据）
- POST /api/auth/sms/login   验证码登录

**不改现有 /api/login 的任何行为**，不改 JWT 结构，不改 users.username 的主键语义。
手机号是新增的唯一别名 + 登录凭据：所有 JWT 的 sub、RBAC 的 user_id、审计的 operator
仍以 username 为主（下游零改动、零回归风险）。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from . import persist, rbac_service, user_service
from .deps import client_ip, get_store
from .models import (
    PhoneRegisterRequest,
    PhoneRegisterResult,
    PhoneSmsLoginRequest,
    PhoneSmsSendRequest,
    PhoneSmsVerifyRequest,
)
from .redis_store import RedisStore
from .security import create_jwt, create_phone_pending_jwt, verify_phone_pending_jwt
from .sms_config import (
    KEY_SMS_CONFIG,
    PURPOSE_LOGIN,
    PURPOSE_REGISTER,
    SMS_PURPOSES,
    get_sms_config,
    is_configured,
)
from .sms_service import (
    SmsInvalidPhone,
    SmsRateLimited,
    check_rate_limits,
    issue_code,
    mask_phone,
    normalize_phone,
    verify_code,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 审计里记录的操作人（手机号注册场景下还没有 username，用脱敏手机号）
_AUDIT_NAME_MAX = 64


def _masked_operator(phone: str) -> str:
    return mask_phone(phone)[:_AUDIT_NAME_MAX]


async def _require_sms_enabled() -> dict:
    """短信服务是否可用；未配置 / 已关闭一律 404（不暴露原因）。"""
    cfg = await get_sms_config()
    if not cfg.get("enabled") or not is_configured(cfg):
        raise HTTPException(status_code=404, detail="not found")
    return cfg


def _normalize_or_400(raw: str) -> str:
    try:
        return normalize_phone(raw)
    except SmsInvalidPhone as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sms/send")
async def send_sms_code(
    body: PhoneSmsSendRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
):
    """发送验证码。

    **无论手机号是否已注册，响应体与状态码完全一致**（号码枚举防护）。
    差异只在验证码校验通过之后才体现 —— 此时请求方已证明持有该号码。
    """
    cfg = await _require_sms_enabled()
    if body.purpose not in SMS_PURPOSES:
        raise HTTPException(status_code=400, detail="invalid purpose")
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    try:
        await check_rate_limits(store, phone=phone, ip=ip, cfg=cfg)
    except SmsRateLimited as exc:
        await persist.audit(
            _masked_operator(phone), "sms_send_blocked", _masked_operator(phone),
            {"purpose": body.purpose, "reason": exc.message}, "fail", ip, category="auth",
        )
        raise HTTPException(status_code=429, detail=exc.message)

    try:
        _code, result = await issue_code(
            store, phone=phone, purpose=body.purpose, cfg=cfg
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("sms send failed: %s", exc)
        await persist.audit(
            _masked_operator(phone), "sms_send", _masked_operator(phone),
            {"purpose": body.purpose, "error": "internal"}, "fail", ip, category="auth",
        )
        raise HTTPException(status_code=503, detail="服务暂不可用")

    await persist.audit(
        _masked_operator(phone), "sms_send", _masked_operator(phone),
        {
            "purpose": body.purpose,
            "ok": result.ok,
            "request_id": result.request_id,
            "error_code": result.error_code,
        },
        "ok" if result.ok else "fail", ip, category="auth",
    )
    if not result.ok:
        # 余额不足 / 套餐包耗尽 / 签名模板填错 —— 一律 503，不透传厂商原始错误
        logger.error(
            "sms provider rejected send (code=%s request_id=%s)",
            result.error_code, result.request_id,
        )
        raise HTTPException(status_code=503, detail="服务暂不可用")

    return {
        "ok": True,
        "expire_in": int(cfg.get("code_ttl_seconds") or 300),
        "cooldown": int(cfg.get("send_interval_seconds") or 60),
    }


@router.post("/sms/verify")
async def verify_sms_code(
    body: PhoneSmsVerifyRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
):
    """校验验证码：通过则签发「手机号已验证」票据（验证码即焚）。"""
    cfg = await _require_sms_enabled()
    if body.purpose not in SMS_PURPOSES:
        raise HTTPException(status_code=400, detail="invalid purpose")
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    ok = await verify_code(
        store, phone=phone, code=body.code, purpose=body.purpose, cfg=cfg
    )
    await persist.audit(
        _masked_operator(phone), "sms_verify", _masked_operator(phone),
        {"purpose": body.purpose, "ok": ok}, "ok" if ok else "fail", ip, category="auth",
    )
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    return {"ticket": create_phone_pending_jwt(phone, body.purpose)}


async def _generate_username(phone: str) -> Optional[str]:
    """自动生成用户名：m{国内号码}；被占用时回退 m{国内号码}_{短码}。

    用国内号码（去掉 +86 国家码）而不是 E.164 —— 用户名里带国家码既不直观，
    也超过 32 位的用户名长度上限。
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    base = f"m{digits}"
    if not await _username_exists(base):
        return base
    for _ in range(5):
        candidate = f"{base}_{secrets.token_hex(3)}"
        if not await _username_exists(candidate):
            return candidate
    return None


async def _username_exists(username: str) -> bool:
    """用户名是否已被占用。"""
    return await user_service.get_user_by_username(username) is not None


@router.post("/register", response_model=PhoneRegisterResult, status_code=201)
async def register_by_phone(
    body: PhoneRegisterRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
):
    """手机号注册：需先通过短信校验拿到票据。

    require_approval=true 时新用户 is_active=false 且**不签发 token**，
    等待管理员在用户管理页启用。
    """
    cfg = await _require_sms_enabled()
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    # 1. 验票据：类型 / 手机号 / 用途都必须对上
    ticket_phone = verify_phone_pending_jwt(body.ticket or "", PURPOSE_REGISTER)
    if not ticket_phone or ticket_phone != phone:
        raise HTTPException(status_code=401, detail="invalid or expired ticket")

    # 2. 手机号未被占用
    if await user_service.get_user_by_phone(phone) is not None:
        await persist.audit(
            _masked_operator(phone), "phone_register", _masked_operator(phone),
            {"reason": "phone_taken"}, "rejected", ip, category="auth",
        )
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")

    # 3. 用户名
    username = (body.username or "").strip()
    if username:
        if await _username_exists(username):
            raise HTTPException(status_code=409, detail="该用户名已被占用，请自定义")
    else:
        username = await _generate_username(phone) or ""
        if not username:
            raise HTTPException(status_code=409, detail="该用户名已被占用，请自定义")

    # 4. 密码缺省时生成随机强密码 —— 用户只能走验证码登录
    password = body.password or secrets.token_urlsafe(24)
    require_approval = bool(cfg.get("require_approval"))

    try:
        user = await user_service.create_user(
            username,
            password,
            display_name=(body.nickname or "").strip() or None,
            is_active=not require_approval,
            phone=phone,
            phone_verified=True,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="该用户名或手机号已被占用，请自定义")

    # 5. 绑默认角色（内置「普通用户」）
    default_id = await rbac_service.default_role_id()
    if default_id:
        await rbac_service.set_user_roles(user.id, [default_id])

    await persist.audit(
        _masked_operator(phone), "phone_register", user.username,
        {"phone": mask_phone(phone), "pending_approval": require_approval},
        "ok", ip, category="auth",
    )

    if require_approval:
        return PhoneRegisterResult(
            ok=True,
            pending_approval=True,
            user_id=user.id,
            username=user.username,
        )

    token = create_jwt(user.username, uid=user.id, ver=int(user.token_version or 0))
    return PhoneRegisterResult(
        ok=True,
        pending_approval=False,
        user_id=user.id,
        username=user.username,
        token=token,
        token_type="bearer",
        requires_2fa=False,
    )


@router.post("/sms/login")
async def login_by_sms(
    body: PhoneSmsLoginRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
):
    """验证码登录：校验验证码 → 查用户 → 签 JWT。

    已开启 2FA 的用户**仍走 2FA 第二步**（返回 requires_2fa + login_token），
    不降低安全等级。
    """
    cfg = await _require_sms_enabled()
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    ok = await verify_code(store, phone=phone, code=body.code, purpose=PURPOSE_LOGIN, cfg=cfg)
    await persist.audit(
        _masked_operator(phone), "sms_verify", _masked_operator(phone),
        {"purpose": PURPOSE_LOGIN, "ok": ok}, "ok" if ok else "fail", ip, category="auth",
    )
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    user = await user_service.get_user_by_phone(phone)
    if user is None:
        raise HTTPException(status_code=404, detail="该手机号尚未注册")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    if await user_service.user_requires_2fa(user.username):
        from .security import create_2fa_pending_jwt

        await persist.audit(
            user.username, "phone_login", user.username, {"2fa": True}, "ok", ip,
            category="auth",
        )
        return {
            "requires_2fa": True,
            "login_token": create_2fa_pending_jwt(user.username),
            "token_type": "bearer",
        }

    token = create_jwt(user.username, uid=user.id, ver=int(user.token_version or 0))
    await persist.audit(
        user.username, "phone_login", user.username, {"2fa": False}, "ok", ip, category="auth",
    )
    return {"token": token, "token_type": "bearer", "requires_2fa": False}


@router.get("/sms/status")
async def sms_status():
    """短信服务是否可用（前端据此决定是否展示验证码入口）。

    未配置 / 已关闭时返回 enabled=false，不返回原因。
    """
    cfg = await get_sms_config()
    return {
        "enabled": bool(cfg.get("enabled")) and is_configured(cfg),
        "require_approval": bool(cfg.get("require_approval")),
        "code_ttl_seconds": int(cfg.get("code_ttl_seconds") or 300),
        "send_interval_seconds": int(cfg.get("send_interval_seconds") or 60),
    }
