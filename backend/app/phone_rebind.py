"""自助改绑手机号（`/api/me/phone/*`）。

改绑手机号 = **转移账号控制权**。手机号是验证码登录的唯一凭据，谁掌握了
号码谁就能登录这个账号。所以这条链路必须同时满足两件事：

1. **新号所有权**：新号收到验证码并校验通过 —— 只有号码持有者能做到；
2. **本人身份**：证明发起改绑的人就是当前账号的主人（二选一）
   - 登录密码（账号开了 2FA 时再加 TOTP）
   - 旧手机号收到的验证码

为什么必须留「旧号验证码」这条路：手机号注册允许不填密码，服务端会生成
随机强密码（见 phone_auth.register_by_phone）。这类用户**不知道自己的密码**，
只有旧号能证明身份 —— 只留密码这一条路，这批用户永远改不了号。

三个接口：
- POST /api/me/phone/send-code   发验证码（target=new|old）
- POST /api/me/phone/verify-new  校验新号验证码 → 换发改绑票据
- POST /api/me/phone/rebind      最终提交（票据 + 身份确认 → 落库 + 审计）

## 号码枚举防护

发码接口**不返回**「这个号是否已被别的账号绑定」：号码占用与否只在最终提交
时以 409 呈现，而那时调用方已经用短信验证码证明了自己是这个号的持有者，
不构成号码枚举。发码阶段的响应体在「号被占用」与「号空闲」两种情况下
完全一致（都是 200 + 同样的 expire_in / cooldown）。

**被占用的号照样会收到验证码** —— 这不是泄露：能收到验证码恰恰证明调用方
是这个号的持有者，占用与否要到「提交改绑」时才知道。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from . import persist, user_service
from .captcha_provider import get_verifier
from .deps import client_ip, get_principal, get_store
from .models import (
    PhoneRebindRequest,
    PhoneRebindSendRequest,
    PhoneRebindVerifyRequest,
)
from .orm import User
from .permissions import Principal
from .redis_store import RedisStore
from .security import (
    PHONE_REBIND_TICKET_TTL,
    create_phone_rebind_jwt,
    verify_phone_rebind_jwt,
)
from .sms_config import (
    PURPOSE_REBIND_NEW,
    PURPOSE_REBIND_OLD,
    get_sms_config,
    is_usable,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/phone", tags=["auth"])

# 同一账号 24h 内的改绑发码次数上限（按**账号**而不是按号码 ——
# 一个被盗账号可以换着号码发码，只按号码设闸挡不住它）。
REBIND_QUOTA_PER_DAY = 5

# 改绑发码配额的计数窗口（覆盖一天即可）
_QUOTA_TTL_SECONDS = 24 * 3600

TARGET_NEW = "new"
TARGET_OLD = "old"

AUTH_METHOD_PASSWORD = "password"
AUTH_METHOD_OLD_CODE = "old_code"

# 审计里记录的操作人（脱敏号码）最大长度
_AUDIT_NAME_MAX = 64


def _day_stamp(now: Optional[datetime] = None) -> str:
    """本地日期戳（YYYYMMDD），用于按天计数的 key。"""
    now = now or datetime.now(timezone.utc).astimezone()
    return now.strftime("%Y%m%d")


async def _require_sms_enabled() -> dict:
    """短信服务是否可用；未配置 / 已关闭 / 没注入 AccessKey 一律 404（不暴露原因）。"""
    cfg = await get_sms_config()
    if not is_usable(cfg):
        raise HTTPException(status_code=404, detail="not found")
    return cfg


def _normalize_or_400(raw: str) -> str:
    try:
        return normalize_phone(raw)
    except SmsInvalidPhone as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _current_user(p: Principal) -> User:
    """取当前用户（不存在 → 404）。"""
    user = await user_service.get_user_by_id(p.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _masked(phone: str | None) -> str:
    """脱敏号码（审计里绝不放明文）。"""
    return mask_phone(phone or "")[:_AUDIT_NAME_MAX]


@router.post("/send-code")
async def send_rebind_code(
    body: PhoneRebindSendRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """发送改绑验证码。

    **必须登录**（普通 JWT 鉴权）—— 这是与公开注册接口的关键区别：这里要把
    「当前账号」带进限频配额与用途判断里。

    target=new：码发到新号（用途 rebind_new）。
      该号是否已被别的账号绑定**不在这里透露**（响应体两种情况完全一致），
      用户会在最终提交时看到 409。

    target=old：码发到**当前绑定的**旧号（用途 rebind_old）。请求里的 phone
      必须与已绑定号码一致，否则 400。
    """
    cfg = await _require_sms_enabled()
    user = await _current_user(p)
    ip = client_ip(request)

    if body.target not in (TARGET_NEW, TARGET_OLD):
        raise HTTPException(status_code=400, detail="invalid target")

    # —— 决定收码号码与验证码用途 ——
    if body.target == TARGET_NEW:
        if not body.phone:
            raise HTTPException(status_code=400, detail="请填写新手机号")
        phone = _normalize_or_400(body.phone)
        purpose = PURPOSE_REBIND_NEW
    else:
        if not user.phone:
            raise HTTPException(status_code=400, detail="账号未绑定手机号")
        # 旧号由服务端取当前绑定号码 —— 前端只拿到脱敏号，
        # 不该让前端回传完整号码（旧号验证码只发到「当前绑定的那一个号」，
        # 否则这条路径会退化成「给任意号码发码」的资源）。
        phone = user.phone
        purpose = PURPOSE_REBIND_OLD

    # —— 改绑专属配额（按账号）——
    day = _day_stamp()
    used = await store.get_rebind_quota(p.user_id, day)
    if used >= REBIND_QUOTA_PER_DAY:
        await persist.audit(
            _masked(user.phone), "phone_rebind_send_quota", p.username,
            {"target": body.target, "used": used}, "fail", ip, category="auth",
        )
        raise HTTPException(
            status_code=429, detail="改绑验证码发送次数已达上限，请明天再试"
        )

    # —— 自适应人机验证（与注册链路一致）——
    if await needs_captcha(store, phone=phone, ip=ip, cfg=cfg):
        if not body.captcha_verify_param:
            await persist.audit(
                _masked(user.phone), "phone_rebind_send_captcha_required", p.username,
                {"target": body.target}, "fail", ip, category="auth",
            )
            raise HTTPException(status_code=428, detail="需要完成人机验证")
        verifier = get_verifier(str(cfg.get("captcha_endpoint") or ""))
        captcha = verifier.verify(
            body.captcha_verify_param, str(cfg.get("captcha_scene_id") or "")
        )
        if not captcha.ok:
            await record_captcha_failure(store, ip)
            await persist.audit(
                _masked(user.phone), "phone_rebind_send_captcha_failed", p.username,
                {"verify_code": captcha.verify_code}, "fail", ip, category="auth",
            )
            raise HTTPException(status_code=400, detail="人机验证未通过，请重试")

    # —— 常规限频（号间隔 / 号日 / IP时 / IP日 / 全局总量）——
    try:
        await check_rate_limits(store, phone=phone, ip=ip, cfg=cfg)
    except SmsRateLimited as exc:
        await persist.audit(
            _masked(user.phone), "phone_rebind_send_blocked", p.username,
            {"target": body.target, "reason": exc.message}, "fail", ip, category="auth",
        )
        raise HTTPException(status_code=429, detail=exc.message)

    # —— 真正发码 ——
    # 注意：这里**不**因为「新号已被别人绑定」而跳过发码。
    #
    # 号码占用与否只在最终提交时以 409 呈现 —— 而那时调用方已经用短信验证码
    # 证明了自己是这个号的持有者，不构成号码枚举。发码阶段跳过只会让用户
    # 卡在第一步（「验证码收不到」），却换不来真正的隐私收益。
    try:
        _code, result = await issue_code(
            store, phone=phone, purpose=purpose, cfg=cfg
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rebind sms send failed: %s", exc)
        await persist.audit(
            _masked(user.phone), "phone_rebind_send", p.username,
            {"target": body.target, "error": "internal"}, "fail", ip, category="auth",
        )
        raise HTTPException(status_code=503, detail="服务暂不可用")

    if not result.ok:
        logger.error(
            "rebind sms provider rejected send (code=%s request_id=%s)",
            result.error_code, result.request_id,
        )
        await persist.audit(
            _masked(user.phone), "phone_rebind_send", p.username,
            {"target": body.target, "error_code": result.error_code}, "fail", ip,
            category="auth",
        )
        raise HTTPException(status_code=503, detail="服务暂不可用")

    await store.incr_rebind_quota(p.user_id, day, _QUOTA_TTL_SECONDS)
    await persist.audit(
        _masked(user.phone), "phone_rebind_send", p.username,
        {"target": body.target, "ok": True, "request_id": result.request_id},
        "ok", ip, category="auth",
    )
    return {
        "ok": True,
        "expire_in": int(cfg.get("code_ttl_seconds") or 300),
        "cooldown": int(cfg.get("send_interval_seconds") or 60),
    }


@router.post("/verify-new")
async def verify_new_phone(
    body: PhoneRebindVerifyRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """校验**新号**验证码 → 换发改绑票据。

    票据把「哪个用户」和「哪个新号码」一起钉死，最终提交时三者都要对上。
    """
    cfg = await _require_sms_enabled()
    await _current_user(p)
    phone = _normalize_or_400(body.phone)
    ip = client_ip(request)

    ok = await verify_code(
        store, phone=phone, code=body.code, purpose=PURPOSE_REBIND_NEW, cfg=cfg
    )
    await persist.audit(
        p.username, "phone_rebind_verify_new", p.username,
        {"ok": ok}, "ok" if ok else "fail", ip, category="auth",
    )
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    ticket = create_phone_rebind_jwt(phone, p.user_id)
    return {"ok": True, "ticket": ticket, "expire_in": PHONE_REBIND_TICKET_TTL}


@router.post("/rebind")
async def rebind_phone(
    body: PhoneRebindRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """最终提交改绑。

    编排：票据 → 号码唯一性 → 本人身份 → 落库 → 审计。
    """
    cfg = await _require_sms_enabled()
    user = await _current_user(p)
    ip = client_ip(request)
    new_phone = _normalize_or_400(body.new_phone)

    # 1. 票据：类型 / uid / 手机号三者都要对上
    if not verify_phone_rebind_jwt(body.ticket or "", p.user_id, new_phone):
        raise HTTPException(status_code=401, detail="invalid or expired ticket")

    # 2. 唯一性：号已被别人绑定 → 409（改回自己的号是幂等的，允许）
    owner = await user_service.get_user_by_phone(new_phone)
    if owner is not None and owner.id != user.id:
        await persist.audit(
            p.username, "phone_rebind", p.username,
            {"reason": "phone_taken", "new": _masked(new_phone)}, "rejected", ip,
            category="auth",
        )
        raise HTTPException(status_code=409, detail="该手机号已被其他账号绑定")

    # 3. 本人身份确认
    #    账号**没有**绑定手机号时跳过（没有旧凭据可被抢占，验新号即足够，
    #    语义等同「绑定」）；已绑定则必须二选一验证，不可跳过。
    if user.phone:
        if body.auth_method == AUTH_METHOD_PASSWORD:
            if not await user_service.verify_user_password(
                p.username, body.password or ""
            ):
                raise HTTPException(status_code=401, detail="密码不正确")
            # 账号开了 2FA 就再加一道 TOTP —— 改绑是转移控制权，
            # 强度不该低于登录本身。
            if await user_service.user_requires_2fa(p.username):
                if not await user_service.verify_user_totp(
                    p.username, (body.totp_code or "").strip()
                ):
                    raise HTTPException(status_code=401, detail="验证码错误或已过期")
        elif body.auth_method == AUTH_METHOD_OLD_CODE:
            ok = await verify_code(
                store, phone=user.phone, code=body.old_code or "",
                purpose=PURPOSE_REBIND_OLD, cfg=cfg,
            )
            if not ok:
                raise HTTPException(status_code=400, detail="验证码错误或已过期")
        else:
            raise HTTPException(status_code=400, detail="无效的验证方式")

    before = {"phone": _masked(user.phone)}

    # 4. 落库（update_user 内部再查一次唯一性，防并发）
    try:
        updated = await user_service.update_user(
            user.id, phone=new_phone, phone_verified=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    # 5. 审计（只放脱敏号码）
    await persist.audit(
        p.username, "phone_rebind", p.username,
        {
            "new": _masked(new_phone),
            "method": body.auth_method if user.phone else "bind",
        },
        "ok", ip, category="auth",
        before=before, after={"phone": _masked(updated.phone)},
    )
    return {"ok": True, "phone": mask_phone(updated.phone or "")}
