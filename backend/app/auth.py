"""后台登录 -> 签发 JWT；支持 2FA、运行期修改密码与当前用户信息。"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from . import permissions, persist, rbac_service, user_service
from .deps import client_ip, get_current_admin, get_principal
from .models import (
    ChangePasswordRequest,
    Login2FARequest,
    LoginRequest,
    MeOut,
    ProfileUpdate,
    RoleRef,
)
from .permissions import Principal
from .security import create_2fa_pending_jwt, create_jwt, verify_2fa_pending_jwt
from .sms_service import mask_phone
from .state import state
from .user_service import (
    get_user_by_username,
    update_user_password,
    user_requires_2fa,
    verify_user_password,
    verify_user_totp,
)

router = APIRouter(prefix="/api", tags=["auth"])

MIN_PASSWORD_LEN = 6
# 登录失败时审计里记录的用户名是调用方随意填的，截断到操作人列宽
_AUDIT_NAME_MAX = 64

# 邮箱格式：够用即可，不追求 RFC 5322 全集。空串是合法的（表示清空）。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _issue_token(username: str) -> str:
    """按用户当前的会话版本签发 JWT（改密 / 禁用后版本递增，旧 token 随即失效）。"""
    user = await get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return create_jwt(username, uid=user.id, ver=int(user.token_version or 0))


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    """校验账号密码；若已开启 2FA 则返回中间态 login_token。"""
    ip = client_ip(request)
    name = (body.username or "")[:_AUDIT_NAME_MAX]
    if not await verify_user_password(body.username, body.password):
        await persist.audit(name, "login_failed", name, None, "fail", ip, category="auth")
        raise HTTPException(status_code=401, detail="invalid credentials")

    if await user_requires_2fa(body.username):
        return {
            "requires_2fa": True,
            "login_token": create_2fa_pending_jwt(body.username),
            "token_type": "bearer",
        }

    token = await _issue_token(body.username)
    await persist.audit(name, "login", name, None, "ok", ip, category="auth")
    return {"token": token, "token_type": "bearer", "requires_2fa": False}


@router.post("/login/2fa")
async def login_2fa(body: Login2FARequest, request: Request):
    """第二步：校验 TOTP 验证码，签发正式 JWT。"""
    username = verify_2fa_pending_jwt(body.login_token)
    if not username:
        raise HTTPException(status_code=401, detail="invalid or expired login token")

    if not await user_requires_2fa(username):
        raise HTTPException(status_code=400, detail="2fa not required")

    ip = client_ip(request)
    if not await verify_user_totp(username, body.totp_code):
        await persist.audit(username, "login_2fa_failed", username, None, "fail", ip, category="auth")
        raise HTTPException(status_code=401, detail="invalid totp code")

    token = await _issue_token(username)
    await persist.audit(username, "login", username, {"2fa": True}, "ok", ip, category="auth")
    return {"token": token, "token_type": "bearer", "requires_2fa": False}


@router.get("/me", response_model=MeOut)
async def me(p: Principal = Depends(get_principal)):
    """当前登录用户：角色与可用菜单（前端据此渲染导航、守卫路由）。"""
    user = await get_user_by_username(p.username)
    roles = (await rbac_service.roles_by_user()).get(p.user_id, [])
    return MeOut(
        user_id=p.user_id,
        username=p.username,
        display_name=user.display_name if user else None,
        is_admin=p.is_admin,
        roles=[RoleRef(**r) for r in roles],
        menus=permissions.normalize_menu_codes(p.menus),
        capabilities=permissions.normalize_capability_codes(p.capabilities),
        # 我自己的两个「暂停接单」开关（两个市场各自独立）
        follow_paused=bool(getattr(user, "follow_paused", False)),
        ai_follow_paused=bool(getattr(user, "ai_follow_paused", False)),
        # 个人中心自助维护字段（存量用户一律 None / False）
        email=getattr(user, "email", None) if user else None,
        leader_name=getattr(user, "leader_name", None) if user else None,
        leader_intro=getattr(user, "leader_intro", None) if user else None,
        # 手机号脱敏：完整号码只留在库里，不进任何列表 / 详情接口
        phone=mask_phone(user.phone) if user and user.phone else None,
        phone_verified=bool(getattr(user, "phone_verified", False)),
    )


def _normalize_profile_field(name: str, raw: Optional[str]) -> Optional[str]:
    """规范化个人中心的一个字段：去空白；空串 -> None（清空）。

    返回 None 有两种含义，需要调用方区分：
    - 「不传」（None）-> 不改该字段
    - 「清空」（字段存在但值为空串）-> 置 NULL
    所以这里对「清空」返回空串，由调用方判断是否写入。
    """
    if raw is None:
        return None
    return raw.strip()


@router.patch("/me/profile", response_model=MeOut)
async def update_profile(
    body: ProfileUpdate,
    request: Request,
    p: Principal = Depends(get_principal),
):
    """个人中心自助修改资料：只改自己。

    字段级「传什么改什么」—— 不传的字段保持原值。传空串表示清空该字段。

    带单员资料（leader_name / leader_intro）**只有持有带单能力的人才允许写**：
    带单广场是公开页，任意用户都能改自己的带单员文案会让广场内容失真。
    """
    user = await user_service.get_user_by_id(p.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    # —— 带单员资料：能力校验 ——
    wants_leader = body.leader_name is not None or body.leader_intro is not None
    if wants_leader:
        can_leader = p.is_admin or permissions.can_capability(p, permissions.CAP_FOLLOW_LEADER)
        can_ai = p.is_admin or permissions.can_capability(p, permissions.CAP_AI_STRATEGY)
        if not (can_leader or can_ai):
            raise HTTPException(status_code=403, detail="只有带单员才能修改带单员资料")

    # —— 邮箱格式校验（空串 = 清空，合法）——
    email: Optional[str] = None
    if body.email is not None:
        email = body.email.strip()
        if email and not _EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="邮箱格式不正确")

    display_name = _normalize_profile_field("display_name", body.display_name)
    leader_name = _normalize_profile_field("leader_name", body.leader_name)
    leader_intro = _normalize_profile_field("leader_intro", body.leader_intro)

    before = {
        "display_name": user.display_name,
        "email": user.email,
        "leader_name": user.leader_name,
        "leader_intro": user.leader_intro,
    }

    try:
        updated = await user_service.update_profile(
            p.user_id,
            display_name=display_name,
            email=email,
            leader_name=leader_name,
            leader_intro=leader_intro,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    await persist.audit(
        p.username, "update_profile", p.username, None, "ok", client_ip(request),
        category="auth", before=before,
        after={
            "display_name": updated.display_name,
            "email": updated.email,
            "leader_name": updated.leader_name,
            "leader_intro": updated.leader_intro,
        },
    )
    return await me(p)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """修改当前用户密码（需 JWT + 当前密码）；成功后该用户的全部旧会话失效。"""
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail="password too short")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="password unchanged")

    if not await verify_user_password(admin, body.current_password):
        raise HTTPException(status_code=401, detail="invalid current password")

    if not await update_user_password(admin, body.new_password):
        raise HTTPException(status_code=404, detail="user not found")

    await rbac_service.revoke_sessions(state.store, admin)
    await persist.audit(
        admin, "change_password", admin, None, "ok", client_ip(request), category="auth",
    )
    return {"ok": True}
