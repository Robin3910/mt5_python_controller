"""后台双因素认证（TOTP）管理 API。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .deps import client_ip, get_current_admin
from .models import TwoFACodeRequest, TwoFAPasswordRequest
from .user_service import (
    confirm_totp,
    disable_totp,
    enable_totp,
    get_2fa_status,
    get_user_by_username,
    reset_totp,
    setup_totp,
    user_requires_2fa,
    verify_user_password,
    verify_user_totp,
)

router = APIRouter(prefix="/api/2fa", tags=["2fa"])


async def _require_totp_if_enabled(username: str, totp_code: str | None) -> None:
    if await user_requires_2fa(username):
        if not totp_code or not await verify_user_totp(username, totp_code):
            raise HTTPException(status_code=401, detail="invalid totp code")


@router.get("/status")
async def twofa_status(admin: str = Depends(get_current_admin)):
    status = await get_2fa_status(admin)
    if not status:
        raise HTTPException(status_code=404, detail="user not found")
    return status


@router.post("/setup")
async def twofa_setup(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """开始绑定或更换密钥：生成新二维码（需后续 confirm 启用）。"""
    data = await setup_totp(admin)
    if not data:
        raise HTTPException(status_code=404, detail="user not found")

    await persist.audit(admin, "2fa_setup", admin, None, "ok", client_ip(request))
    return data


@router.post("/confirm")
async def twofa_confirm(
    body: TwoFACodeRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """确认绑定并开启 2FA。"""
    if not await confirm_totp(admin, body.totp_code):
        raise HTTPException(status_code=400, detail="invalid totp code")

    await persist.audit(admin, "2fa_confirm", admin, None, "ok", client_ip(request))
    return {"ok": True, "enabled": True}


@router.post("/enable")
async def twofa_enable(
    body: TwoFACodeRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """已绑定密钥后重新开启 2FA。"""
    user = await get_user_by_username(admin)
    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2fa not bound")
    if user.totp_enabled:
        return {"ok": True, "enabled": True}

    if not await enable_totp(admin, body.totp_code):
        raise HTTPException(status_code=400, detail="invalid totp code")

    await persist.audit(admin, "2fa_enable", admin, None, "ok", client_ip(request))
    return {"ok": True, "enabled": True}


@router.post("/disable")
async def twofa_disable(
    body: TwoFAPasswordRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """关闭 2FA（保留密钥）。"""
    if not await verify_user_password(admin, body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    await _require_totp_if_enabled(admin, body.totp_code)

    if not await disable_totp(admin):
        raise HTTPException(status_code=404, detail="user not found")

    await persist.audit(admin, "2fa_disable", admin, None, "ok", client_ip(request))
    return {"ok": True, "enabled": False}


@router.post("/reset")
async def twofa_reset(
    body: TwoFAPasswordRequest,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """重置 2FA：清除绑定（需密码；若 2FA 已开启还需验证码）。"""
    if not await verify_user_password(admin, body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    await _require_totp_if_enabled(admin, body.totp_code)

    if not await reset_totp(admin):
        raise HTTPException(status_code=404, detail="user not found")

    await persist.audit(admin, "2fa_reset", admin, None, "ok", client_ip(request))
    return {"ok": True, "enabled": False, "bound": False}
