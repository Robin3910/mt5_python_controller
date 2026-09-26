"""后台登录 -> 签发 JWT；支持 2FA、运行期修改密码与当前用户信息。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import permissions, persist, rbac_service
from .deps import client_ip, get_current_admin, get_principal
from .models import ChangePasswordRequest, Login2FARequest, LoginRequest, MeOut, RoleRef
from .permissions import Principal
from .security import create_2fa_pending_jwt, create_jwt, verify_2fa_pending_jwt
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
    )


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
