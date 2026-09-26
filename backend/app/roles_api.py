"""角色与菜单 API（仅超级管理员）：角色增删改、为角色勾选菜单、读取菜单注册表。

菜单是代码注册表（permissions.MENU_REGISTRY），这里只能勾选、不能新增；
仅管理员菜单（assignable=False）不可分配。所有操作写审计（category=auth），
变更后让持有该角色的用户重新拉取权限。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import permissions, persist, rbac_service
from .deps import client_ip, get_store, require_admin
from .models import MenuOut, RoleCreate, RoleMenusUpdate, RoleOut, RoleUpdate
from .permissions import Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api", tags=["roles"])


async def _role_or_404(role_id: int) -> dict:
    role = await rbac_service.get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail="role not found")
    return role


def _snapshot(role: dict | None) -> dict | None:
    if not role:
        return None
    return {k: role.get(k) for k in ("id", "code", "name", "enabled", "remark", "menus")}


@router.get("/menus", response_model=list[MenuOut])
async def list_menus(_: Principal = Depends(require_admin)):
    """菜单注册表（角色编辑页据此列出可勾选项）。"""
    return [
        MenuOut(code=m.code, name=m.name, path=m.path, assignable=m.assignable)
        for m in permissions.MENU_REGISTRY
    ]


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(_: Principal = Depends(require_admin)):
    return await rbac_service.list_roles()


@router.post("/roles", response_model=RoleOut, status_code=201)
async def create_role(
    body: RoleCreate,
    request: Request,
    p: Principal = Depends(require_admin),
):
    if await rbac_service.role_code_exists(body.code):
        raise HTTPException(status_code=409, detail=f"角色编码已存在：{body.code}")
    try:
        role = await rbac_service.create_role(
            code=body.code, name=body.name, remark=body.remark,
            enabled=body.enabled, menus=body.menus,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await persist.audit(
        p.username, "create_role", role["code"], None, "ok", client_ip(request),
        category="auth", before=None, after=_snapshot(role),
    )
    return role


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: int,
    body: RoleUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    before = await _role_or_404(role_id)
    try:
        role = await rbac_service.update_role(
            role_id, name=body.name, remark=body.remark, enabled=body.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    if body.enabled is not None and body.enabled != before["enabled"]:
        await rbac_service.refresh_sessions(store, await rbac_service.usernames_with_role(role_id))
    await persist.audit(
        p.username, "update_role", role["code"], body.model_dump(exclude_none=True), "ok",
        client_ip(request), category="auth", before=_snapshot(before), after=_snapshot(role),
    )
    return role


@router.put("/roles/{role_id}/menus", response_model=RoleOut)
async def set_role_menus(
    role_id: int,
    body: RoleMenusUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """整体替换角色菜单；未知菜单与仅管理员菜单拒收。"""
    before = await _role_or_404(role_id)
    try:
        role = await rbac_service.set_role_menus(role_id, body.menus)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    await rbac_service.refresh_sessions(store, await rbac_service.usernames_with_role(role_id))
    await persist.audit(
        p.username, "set_role_menus", role["code"], {"menus": body.menus}, "ok",
        client_ip(request), category="auth", before=_snapshot(before), after=_snapshot(role),
    )
    return role


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: int,
    request: Request,
    p: Principal = Depends(require_admin),
):
    """删除自定义角色；内置角色不可删，仍有用户在用时拒绝（先调整这些用户的角色）。"""
    before = await _role_or_404(role_id)
    if before["is_builtin"]:
        raise HTTPException(status_code=400, detail="内置角色不可删除")
    if before["user_count"]:
        raise HTTPException(
            status_code=409, detail=f"仍有 {before['user_count']} 个用户使用该角色，请先调整这些用户的角色",
        )
    try:
        ok = await rbac_service.delete_role(role_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="role not found")
    await persist.audit(
        p.username, "delete_role", before["code"], None, "ok", client_ip(request),
        category="auth", before=_snapshot(before), after=None,
    )
    return {"status": "deleted", "id": role_id}
