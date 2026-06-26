"""后台管理员登录 -> 签发 JWT；支持 2FA 与运行期修改密码。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .deps import client_ip, get_current_admin
from .models import ChangePasswordRequest, Login2FARequest, LoginRequest
from .security import create_2fa_pending_jwt, create_jwt, verify_2fa_pending_jwt
from .user_service import (
    update_user_password,
    user_requires_2fa,
    verify_user_password,
    verify_user_totp,
)

router = APIRouter(prefix="/api", tags=["auth"])

MIN_PASSWORD_LEN = 6


@router.post("/login")
async def login(body: LoginRequest):
    """校验账号密码；若已开启 2FA 则返回中间态 login_token。"""
    if not await verify_user_password(body.username, body.password):
        raise HTTPException(status_code=401, detail="invalid credentials")

    if await user_requires_2fa(body.username):
        return {
            "requires_2fa": True,
            "login_token": create_2fa_pending_jwt(body.username),
            "token_type": "bearer",
        }

    return {"token": create_jwt(body.username), "token_type": "bearer", "requires_2fa": False}


@router.post("/login/2fa")
async def login_2fa(body: Login2FARequest):
    """第二步：校验 TOTP 验证码，签发正式 JWT。"""
    username = verify_2fa_pending_jwt(body.login_token)
    if not username:
        raise HTTPException(status_code=401, detail="invalid or expired login token")

    if not await user_requires_2fa(username):
        raise HTTPException(status_code=400, detail="2fa not required")

    if not await verify_user_totp(username, body.totp_code):
        raise HTTPException(status_code=401, detail="invalid totp code")

    return {"token": create_jwt(username), "token_type": "bearer", "requires_2fa": False}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """修改管理员密码（需 JWT + 当前密码）；新密码写入用户表。"""
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail="password too short")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="password unchanged")

    if not await verify_user_password(admin, body.current_password):
        raise HTTPException(status_code=401, detail="invalid current password")

    if not await update_user_password(admin, body.new_password):
        raise HTTPException(status_code=404, detail="user not found")

    await persist.audit(admin, "change_password", admin, None, "ok", client_ip(request))
    return {"ok": True}
