"""用户管理 API（仅超级管理员）：用户增删改、分配角色、重置密码 / 2FA、分配节点。

所有操作写审计（category=auth）。节点分配是唯一会动交易数据的操作，规则见
`assign_nodes`：节点上有未收口的策略子任务时拒绝；节点仍在分组里时需二次确认，
确认后把它移出分组，保证「分组成员与分组同一所有者」。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from . import (
    group_persist,
    group_service,
    node_service,
    permissions,
    persist,
    rbac_service,
    user_service,
)
from .deps import client_ip, get_store, require_admin
from .models import (
    RoleRef,
    UserCreate,
    UserNodesUpdate,
    UserOut,
    UserPasswordReset,
    UserRolesUpdate,
    UserUpdate,
)
from .orm import User
from .permissions import ADMIN_ROLE_CODE, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/users", tags=["users"])


def _is_admin_roles(roles: list[dict]) -> bool:
    return any(r.get("code") == ADMIN_ROLE_CODE and r.get("enabled", True) for r in roles)


async def _ownership(store: RedisStore) -> tuple[dict[int, list[str]], dict[int, int], dict[int, int]]:
    """按所有者汇总：{uid: [node_id]}、{uid: 分组数}、{uid: 策略数}（管理员名下的不计）。"""
    nodes: dict[int, list[str]] = {}
    for n in sorted(await store.all_nodes(), key=lambda d: d.get("created_at", 0)):
        owner = permissions.normalize_owner(n.get("owner_user_id"))
        if owner is not None:
            nodes.setdefault(owner, []).append(n["node_id"])
    groups: dict[int, int] = {}
    for g in await store.all_groups():
        owner = permissions.normalize_owner(g.get("owner_user_id"))
        if owner is not None:
            groups[owner] = groups.get(owner, 0) + 1
    strategies: dict[int, int] = {}
    for s in await store.all_strategies():
        owner = permissions.normalize_owner(s.get("owner_user_id"))
        if owner is not None:
            strategies[owner] = strategies.get(owner, 0) + 1
    return nodes, groups, strategies


def _to_user_out(user: User, roles: list[dict], ownership) -> UserOut:
    nodes, groups, strategies = ownership
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_active=bool(user.is_active),
        is_admin=_is_admin_roles(roles),
        totp_enabled=bool(user.totp_enabled and user.totp_secret),
        roles=[RoleRef(**r) for r in roles],
        node_ids=nodes.get(user.id, []),
        group_count=groups.get(user.id, 0),
        strategy_count=strategies.get(user.id, 0),
        created_at=user.created_at.timestamp() if user.created_at else None,
    )


async def _user_out(store: RedisStore, user: User) -> UserOut:
    roles = (await rbac_service.roles_by_user()).get(user.id, [])
    return _to_user_out(user, roles, await _ownership(store))


def _snapshot(user: User, roles: Optional[list[dict]] = None) -> dict:
    """用户审计快照：不含密码与 2FA 密钥。"""
    out = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_active": bool(user.is_active),
    }
    if roles is not None:
        out["roles"] = [r.get("code") for r in roles]
    return out


async def _user_or_404(user_id: int) -> User:
    user = await user_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    return user


async def _ensure_not_last_admin(user: User, *, action: str) -> None:
    admins = await rbac_service.active_admin_ids()
    if user.id in admins and len(admins) <= 1:
        raise HTTPException(status_code=400, detail=f"至少保留一个启用中的超级管理员，无法{action}")


@router.get("", response_model=list[UserOut])
async def list_users(
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    users = await user_service.list_users()
    roles = await rbac_service.roles_by_user()
    ownership = await _ownership(store)
    return [_to_user_out(u, roles.get(u.id, []), ownership) for u in users]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """新建用户；未指定角色时绑定内置「普通用户」角色。"""
    if await user_service.get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail=f"用户名已存在：{body.username}")
    role_ids = list(dict.fromkeys(body.role_ids))
    if not role_ids:
        default_id = await rbac_service.default_role_id()
        role_ids = [default_id] if default_id else []
    known = {r["id"] for r in await rbac_service.list_roles()}
    missing = [rid for rid in role_ids if rid not in known]
    if missing:
        raise HTTPException(status_code=400, detail=f"角色不存在：{'、'.join(map(str, missing))}")
    try:
        user = await user_service.create_user(
            body.username, body.password,
            display_name=body.display_name, is_active=body.is_active,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"用户名已存在：{body.username}")
    roles = await rbac_service.set_user_roles(user.id, role_ids)
    await persist.audit(
        p.username, "create_user", user.username, None, "ok", client_ip(request),
        category="auth", before=None, after=_snapshot(user, roles),
    )
    return await _user_out(store, user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """修改显示名 / 启用状态。禁用即吊销其全部会话；名下分组不会被自动停止。"""
    user = await _user_or_404(user_id)
    roles = (await rbac_service.roles_by_user()).get(user.id, [])
    deactivating = body.is_active is False and bool(user.is_active)
    if deactivating:
        if user.id == p.user_id:
            raise HTTPException(status_code=400, detail="不能禁用自己")
        await _ensure_not_last_admin(user, action="禁用")
    before = _snapshot(user, roles)
    updated = await user_service.update_user(
        user.id, display_name=body.display_name, is_active=body.is_active,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    if deactivating:
        await rbac_service.revoke_sessions(store, updated.username)
    elif body.is_active is not None:
        await rbac_service.invalidate_principals(store, [updated.username])
    await persist.audit(
        p.username, "update_user", updated.username, body.model_dump(exclude_none=True), "ok",
        client_ip(request), category="auth", before=before, after=_snapshot(updated, roles),
    )
    return await _user_out(store, updated)


@router.put("/{user_id}/roles", response_model=UserOut)
async def set_user_roles(
    user_id: int,
    body: UserRolesUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """整体替换用户角色；不能移除自己的超级管理员角色，也不能让管理员变成 0 个。"""
    user = await _user_or_404(user_id)
    before_roles = (await rbac_service.roles_by_user()).get(user.id, [])
    admin_rid = await rbac_service.admin_role_id()
    was_admin = _is_admin_roles(before_roles)
    will_admin = admin_rid is not None and admin_rid in body.role_ids
    if was_admin and not will_admin:
        if user.id == p.user_id:
            raise HTTPException(status_code=400, detail="不能移除自己的超级管理员角色")
        await _ensure_not_last_admin(user, action="移除其超级管理员角色")
    try:
        roles = await rbac_service.set_user_roles(user.id, body.role_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await rbac_service.refresh_sessions(store, [user.username])
    await persist.audit(
        p.username, "set_user_roles", user.username, {"role_ids": body.role_ids}, "ok",
        client_ip(request), category="auth",
        before=_snapshot(user, before_roles), after=_snapshot(user, roles),
    )
    return await _user_out(store, user)


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    body: UserPasswordReset,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """管理员重置用户密码；该用户的全部旧会话立即失效。审计不记录密码。"""
    user = await _user_or_404(user_id)
    if not await user_service.update_user_password(user.username, body.new_password):
        raise HTTPException(status_code=404, detail="user not found")
    await rbac_service.revoke_sessions(store, user.username)
    await persist.audit(
        p.username, "reset_user_password", user.username, None, "ok", client_ip(request),
        category="auth",
    )
    return {"ok": True}


@router.post("/{user_id}/reset-2fa")
async def reset_2fa(
    user_id: int,
    request: Request,
    p: Principal = Depends(require_admin),
):
    """清除用户的 2FA 绑定（用户丢失验证器时由管理员协助恢复登录）。"""
    user = await _user_or_404(user_id)
    if not await user_service.reset_totp(user.username):
        raise HTTPException(status_code=404, detail="user not found")
    await persist.audit(
        p.username, "reset_user_2fa", user.username, None, "ok", client_ip(request),
        category="auth",
    )
    return {"ok": True}


@router.get("/{user_id}/nodes")
async def user_nodes(
    user_id: int,
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    user = await _user_or_404(user_id)
    nodes, _, _ = await _ownership(store)
    return {"node_ids": nodes.get(user.id, [])}


def _dedup(ids: list[str]) -> list[str]:
    out: list[str] = []
    for raw in ids:
        nid = str(raw or "").strip()
        if nid and nid not in out:
            out.append(nid)
    return out


@router.put("/{user_id}/nodes")
async def assign_nodes(
    user_id: int,
    body: UserNodesUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """整体替换用户名下节点：新增的从原所有者转来，移除的回到管理员名下。

    1. 变动节点上有未收口的策略子任务 → 409 active_tasks（先在分组页平仓 / 终止）；
    2. 变动节点仍在分组里 → 409 confirm_required，confirm=True 后移出这些分组；
    3. 写库刷新缓存，并让原所有者与新所有者的后台连接重新拉取权限。
    """
    user = await _user_or_404(user_id)
    roles = (await rbac_service.roles_by_user()).get(user.id, [])
    if _is_admin_roles(roles):
        raise HTTPException(status_code=400, detail="超级管理员可见全部节点，无需分配")

    target = _dedup(body.node_ids)
    nodemap = {n["node_id"]: n for n in await store.all_nodes()}
    missing = [nid for nid in target if nid not in nodemap]
    if missing:
        raise HTTPException(status_code=400, detail=f"节点不存在：{'、'.join(missing)}")
    current = [
        nid for nid, n in nodemap.items()
        if permissions.same_owner(n.get("owner_user_id"), user.id)
    ]
    added = [nid for nid in target if nid not in current]
    removed = [nid for nid in current if nid not in target]
    changed = added + removed
    ip = client_ip(request)

    busy = []
    for nid in changed:
        subs = await group_persist.active_subtasks_for_node(nid)
        if subs:
            busy.append({"node_id": nid, "name": nodemap[nid].get("name"), "tasks": len(subs)})
    if busy:
        await persist.audit(
            p.username, "assign_nodes", user.username, {"node_ids": target}, "rejected", ip,
            category="auth", before={"node_ids": current}, after={"active_tasks": busy},
        )
        raise HTTPException(status_code=409, detail={
            "reason": "active_tasks",
            "message": "以下节点仍有进行中的策略任务，请先在分组页平仓或终止后再分配",
            "nodes": busy,
        })

    changed_set = set(changed)
    memberships = []
    for g in await store.all_groups():
        for m in g.get("members") or []:
            nid = m.get("node_id")
            if nid in changed_set:
                memberships.append({
                    "group_id": g["group_id"],
                    "group_name": g.get("name"),
                    "node_id": nid,
                    "node_name": nodemap[nid].get("name"),
                })
    if memberships and not body.confirm:
        raise HTTPException(status_code=409, detail={
            "reason": "confirm_required",
            "message": "以下节点仍在分组中，确认后将把它们移出这些分组",
            "memberships": memberships,
        })

    for nid in sorted({m["node_id"] for m in memberships}):
        await group_service.remove_node_from_all_groups(store, nid)
    previous_owners = {
        permissions.normalize_owner(nodemap[nid].get("owner_user_id")) for nid in added
    }
    for nid in added:
        await node_service.set_owner(store, nid, user.id)
    for nid in removed:
        await node_service.set_owner(store, nid, None)

    names = await rbac_service.username_map()
    affected = {user.username} | {names[o] for o in previous_owners if o in names}
    if changed:
        await rbac_service.refresh_sessions(store, affected)
    result = {
        "node_ids": target,
        "added": added,
        "removed": removed,
        "removed_memberships": memberships,
    }
    await persist.audit(
        p.username, "assign_nodes", user.username, {"node_ids": target}, "ok", ip,
        category="auth", before={"node_ids": current}, after=result,
    )
    return result


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """删除用户：仅限名下没有节点、分组、策略（否则先回收 / 删除，或改为禁用）。"""
    user = await _user_or_404(user_id)
    if user.id == p.user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    await _ensure_not_last_admin(user, action="删除")
    nodes, groups, strategies = await _ownership(store)
    owned = {
        "nodes": len(nodes.get(user.id, [])),
        "groups": groups.get(user.id, 0),
        "strategies": strategies.get(user.id, 0),
    }
    if any(owned.values()):
        raise HTTPException(status_code=409, detail={
            "reason": "owns_resources",
            "message": "该用户名下仍有节点、分组或策略，请先回收 / 删除，或改为禁用",
            **owned,
        })
    roles = (await rbac_service.roles_by_user()).get(user.id, [])
    before = _snapshot(user, roles)
    await rbac_service.revoke_sessions(store, user.username)
    if not await user_service.delete_user(user.id):
        raise HTTPException(status_code=404, detail="user not found")
    await persist.audit(
        p.username, "delete_user", user.username, None, "ok", client_ip(request),
        category="auth", before=before, after=None,
    )
    return {"status": "deleted", "id": user.id}
