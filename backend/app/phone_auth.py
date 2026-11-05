"""手机号注册 / 验证码登录 API（公开、无鉴权）。

四个接口：
- POST /api/auth/sms/send    发送验证码（自适应人机验证）
- POST /api/auth/sms/verify  校验验证码 → 签发票据 + 告知下一步走向
- POST /api/auth/register    手机号注册（需票据）
- POST /api/auth/sms/login   验证码登录

**不改现有 /api/login 的任何行为**，不改 JWT 结构，不改 users.username 的主键语义。

手机号是新增的唯一别名 + 登录凭据：所有 JWT 的 sub、RBAC 的 user_id、审计的 operator
仍以 username 为主（下游零改动、零回归风险）。

## 「一次验证码，两种去向」

登录与注册**共用同一种验证码**（purpose=auth）。用户从登录入口点「获取验证码」时
还不知道自己有没有账号 —— 如果分两种用途，未注册的用户就得收两次短信。

验证码校验通过后，服务端查该号码是否已注册，据此告诉前端该走哪条路：

    next_action = "login"             已注册且已启用 → 前端换 token 登录
    next_action = "register"          未注册       → 前端直接进注册第二步（不再收码）
    next_action = "pending_approval"  已注册但待审核 → 提示等待管理员审核

这一步不构成号码枚举：调用方已经通过验证码证明了持有该号码。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from . import persist, rbac_service, user_review_service, user_service
from .captcha_provider import encrypt_scene_id, get_verifier
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
from .settings import settings
from .sms_config import (
    KEY_SMS_CONFIG,
    PURPOSE_AUTH,
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
    needs_captcha,
    normalize_phone,
    record_captcha_failure,
    verify_code,
)
from .user_review_service import KycInvalid, validate_kyc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 审计里记录的操作人（手机号注册场景下还没有 username，用脱敏手机号）
_AUDIT_NAME_MAX = 64

# 票据用途：
# - auth 票据由 /sms/verify 签发，可用于「登录」或「注册」两个动作
# - register 票据只允许注册（防止把注册票据拿去登录）
TICKET_PURPOSE_AUTH = "auth"
TICKET_PURPOSE_REGISTER = "register"
_TICKET_ALLOWED_FOR_REGISTER = frozenset({TICKET_PURPOSE_AUTH, TICKET_PURPOSE_REGISTER})
_TICKET_ALLOWED_FOR_LOGIN = frozenset({TICKET_PURPOSE_AUTH})

# 下一步走向
NEXT_ACTION_LOGIN = "login"
NEXT_ACTION_REGISTER = "register"
NEXT_ACTION_PENDING = "pending_approval"
# KYC 实名被驳回 → 允许用户重新提交（不是「已注册，别碰」）
NEXT_ACTION_REJECTED = "rejected"


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

    **自适应人机验证**：默认直接放行；只有命中风控阈值时才返回 428 要求前端
    弹滑块（带 captcha_verify_param 重试）。这样正常用户全程无感，
    而自动化脚本会被拦在发码之前。
    """
    cfg = await _require_sms_enabled()
    if body.purpose not in SMS_PURPOSES:
        raise HTTPException(status_code=400, detail="invalid purpose")
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    # —— 人机验证（自适应）——
    if await needs_captcha(store, phone=phone, ip=ip, cfg=cfg):
        if not body.captcha_verify_param:
            # 需要滑块但前端没带验证参数：告诉前端去弹滑块（428）
            await persist.audit(
                _masked_operator(phone), "sms_send_captcha_required",
                _masked_operator(phone), {"purpose": body.purpose}, "fail", ip,
                category="auth",
            )
            raise HTTPException(status_code=428, detail="需要完成人机验证")
        verifier = get_verifier(str(cfg.get("captcha_endpoint") or ""))
        captcha = verifier.verify(
            body.captcha_verify_param, str(cfg.get("captcha_scene_id") or "")
        )
        if not captcha.ok:
            await record_captcha_failure(store, ip)
            await persist.audit(
                _masked_operator(phone), "sms_send_captcha_failed",
                _masked_operator(phone),
                {"verify_code": captcha.verify_code}, "fail", ip, category="auth",
            )
            raise HTTPException(status_code=400, detail="人机验证未通过，请重试")

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
    """校验验证码：通过则签发「手机号已验证」票据（验证码即焚），并告知下一步走向。

    返回的 next_action 由服务端根据「该号码是否已注册」决定：

    - login            已注册且已启用，前端可用同一张票据换 token
    - register         未注册，前端直接进注册第二步（**不用再收验证码**）
    - pending_approval 已注册但尚未通过管理员审核
    """
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

    ticket = create_phone_pending_jwt(phone, TICKET_PURPOSE_AUTH)
    user = await user_service.get_user_by_phone(phone)
    if user is None:
        return {"ticket": ticket, "next_action": NEXT_ACTION_REGISTER}
    if not user.is_active:
        # 已注册但不可登录 → 区分「等审核」与「审核被驳回（可重新提交）」。
        # 驳回时把原因一并返回，前端提示用户去注册页重新提交。
        review = await user_review_service.latest_review(user.id)
        if review is not None and review.status == user_review_service.STATUS_REJECTED:
            return {
                "ticket": ticket,
                "next_action": NEXT_ACTION_REJECTED,
                "username": user.username,
                "reject_reason": review.reject_reason or "",
            }
        return {
            "ticket": ticket,
            "next_action": NEXT_ACTION_PENDING,
            "username": user.username,
        }
    return {"ticket": ticket, "next_action": NEXT_ACTION_LOGIN}


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
    等待管理员在用户权限页审核。

    **三条路径**：
    1. 手机号未注册 → 新建用户 + （有实名材料时）建审核单；
    2. 手机号已注册，但**最新审核单是 rejected** → **重新提交**（新建一张
       审核单，旧的驳回单保留成历史）。这是驳回必须给出的补救路径 ——
       否则用户永久卡死无法登录。
    3. 手机号已注册且不是「驳回待重提」 → 409（提示直接登录）。

    为什么第 2 条安全：能走到这一步的用户**已通过短信验证码验证**（持有该
    号码）；重提只能把审核单打回 pending，**审核通过仍必须管理员点**，
    「未审核」状态刷不成「已审核」。防刷目标不受影响。
    """
    cfg = await _require_sms_enabled()
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    # 1. 验票据：类型 / 手机号 / 用途都必须对上
    #    auth 票据（登录页发现未注册时签发的）也允许用于注册
    ticket_phone = verify_phone_pending_jwt(
        body.ticket or "", _TICKET_ALLOWED_FOR_REGISTER
    )
    if not ticket_phone or ticket_phone != phone:
        raise HTTPException(status_code=401, detail="invalid or expired ticket")

    # 2. 实名材料校验（姓名为空 = 不提交；邮箱格式错 → 400）
    try:
        kyc = validate_kyc(body.real_name, body.email)
    except KycInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    existing = await user_service.get_user_by_phone(phone)
    if existing is not None:
        return await _resubmit_review(
            existing, kyc=kyc, email=body.email, nickname=body.nickname, ip=ip
        )

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

    # 6. 邮箱（create_user 不支持 email，注册完补一次）
    email = (body.email or "").strip()
    if email:
        await user_service.update_profile(user.id, email=email)

    # 7. 建审核单。
    #    免审核模式（require_approval=false）**不建审核单** —— 该表只记录
    #    走过审核流程的用户，保持语义干净。
    review_id = None
    if require_approval:
        review = await user_review_service.create_review(
            user.id,
            real_name=kyc["real_name"] if kyc else None,
            email=email or None,
            phone=user_review_service.mask_phone_snapshot(phone),
            kyc_submitted=bool(kyc),
        )
        review_id = review.id

    await persist.audit(
        _masked_operator(phone), "phone_register", user.username,
        {"phone": mask_phone(phone), "pending_approval": require_approval,
         "kyc_submitted": bool(kyc)},
        "ok", ip, category="auth",
    )

    if require_approval:
        return PhoneRegisterResult(
            ok=True,
            pending_approval=True,
            user_id=user.id,
            username=user.username,
            review_id=review_id,
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


async def _resubmit_review(
    user,
    *,
    kyc: Optional[dict],
    email: str | None,
    nickname: str | None,
    ip: str | None,
) -> PhoneRegisterResult:
    """被驳回后重新提交实名材料。

    只有「最新审核单 status == rejected」才允许重提；其他情况一律 409。

    注意：**只更新用户自己可维护的字段**（昵称、邮箱），不动用户名、不动
    密码、不动角色、不动 `is_active` —— 重提不等于通过，用户依旧不能登录。
    """
    review = await user_review_service.latest_review(user.id)
    if review is None or review.status != user_review_service.STATUS_REJECTED:
        await persist.audit(
            _masked_operator(user.phone), "phone_register", _masked_operator(user.phone),
            {"reason": "phone_taken"}, "rejected", ip, category="auth",
        )
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")

    # 更新自己可维护的字段
    profile: dict = {}
    if nickname and nickname.strip():
        profile["display_name"] = nickname.strip()
    if email and email.strip():
        profile["email"] = email.strip()
    if profile:
        await user_service.update_profile(user.id, **profile)

    # 新建一张审核单；旧的驳回单保留成历史
    new_review = await user_review_service.create_review(
        user.id,
        real_name=kyc["real_name"] if kyc else None,
        email=(email or "").strip() or (profile.get("email")),
        phone=user_review_service.mask_phone_snapshot(user.phone),
        kyc_submitted=bool(kyc),
    )

    await persist.audit(
        _masked_operator(user.phone), "phone_review_resubmit", user.username,
        {"review_id": new_review.id, "kyc_submitted": bool(kyc)},
        "ok", ip, category="auth",
    )
    return PhoneRegisterResult(
        ok=True,
        pending_approval=True,
        user_id=user.id,
        username=user.username,
        review_id=new_review.id,
        resubmitted=True,
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

    code 与 ticket 二选一即可：
    - 只给 code：服务端自己校验验证码（前端一步到位）
    - 只给 ticket：复用 /sms/verify 已校验过的票据（省一次往返）
    """
    cfg = await _require_sms_enabled()
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    ticket_phone = None
    if body.ticket:
        ticket_phone = verify_phone_pending_jwt(body.ticket, _TICKET_ALLOWED_FOR_LOGIN)
        if not ticket_phone or ticket_phone != phone:
            raise HTTPException(status_code=401, detail="invalid or expired ticket")

    if not ticket_phone:
        if not body.code:
            raise HTTPException(status_code=400, detail="请输入验证码")
        ok = await verify_code(
            store, phone=phone, code=body.code, purpose=PURPOSE_AUTH, cfg=cfg
        )
        await persist.audit(
            _masked_operator(phone), "sms_verify", _masked_operator(phone),
            {"purpose": PURPOSE_AUTH, "ok": ok}, "ok" if ok else "fail", ip, category="auth",
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

    captcha_* 字段供前端初始化阿里云验证码 SDK 用；captcha_enabled=false 时
    前端不加载验证码 JS（省一次外部请求）。

    **captcha_scene_id 只在非加密模式下直接下发**：加密模式下前端必须用
    服务端生成的 EncryptedSceneId，明文 SceneId 不进浏览器。
    """
    cfg = await get_sms_config()
    captcha_enabled = bool(cfg.get("captcha_enabled")) and bool(
        cfg.get("captcha_prefix") and cfg.get("captcha_scene_id")
    )
    encrypted = captcha_enabled and bool(settings.aliyun_captcha_ekey)
    return {
        "enabled": bool(cfg.get("enabled")) and is_configured(cfg),
        "require_approval": bool(cfg.get("require_approval")),
        "code_ttl_seconds": int(cfg.get("code_ttl_seconds") or 300),
        "send_interval_seconds": int(cfg.get("send_interval_seconds") or 60),
        "captcha_enabled": captcha_enabled,
        "captcha_prefix": str(cfg.get("captcha_prefix") or ""),
        # 加密模式下不下发明文 SceneId
        "captcha_scene_id": "" if encrypted else str(cfg.get("captcha_scene_id") or ""),
        "captcha_encrypted": encrypted,
    }


@router.get("/sms/captcha-scene")
async def captcha_scene():
    """下发 EncryptedSceneId（阿里云验证码「加密模式」）。

    **加密模式**（V3 架构可选增强，默认建议开启）：SceneId 明文写在 JS 里
    的话，谁都能拿走白嫖你账号的验证码额度。开启后由服务端用 ekey 动态
    加密 SceneId + 时间戳 + 有效期，前端拿到的只是一次性短效串。

    未配置 ekey 时返回明文 SceneId（兼容未开启加密模式的场景）。
    """
    cfg = await get_sms_config()
    scene_id = str(cfg.get("captcha_scene_id") or "")
    ekey = settings.aliyun_captcha_ekey
    if not scene_id:
        raise HTTPException(status_code=404, detail="not found")
    if not ekey:
        return {"scene_id": scene_id, "encrypted_scene_id": ""}
    try:
        encrypted = encrypt_scene_id(scene_id, ekey)
    except Exception as exc:  # noqa: BLE001
        # ekey 配错（比如长度不对）不该让整个验证码不可用 —— 降级到明文
        logger.error("encrypt scene id failed: %s", exc)
        return {"scene_id": scene_id, "encrypted_scene_id": ""}
    return {"scene_id": scene_id, "encrypted_scene_id": encrypted}
