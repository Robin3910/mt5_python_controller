"""角色 / 菜单 / 登录身份（Principal）的持久化与缓存：MySQL 为权威，Redis 作缓存。

纯规则在 permissions.py；这里负责读写 sys_role / sys_role_menu / sys_user_role、
启动种子、把用户权限组装成 Principal 并缓存、权限变更后让旧会话失效，
以及后台推送时解析事件所属资源的所有者。
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import delete, func, select

from . import permissions
from .connections import WS_CLOSE_PERM_CHANGED, WS_CLOSE_REVOKED, manager
from .db import SessionLocal
from .orm import Role, RoleMenu, User, UserRole
from .permissions import ADMIN_ROLE_CODE, DEFAULT_ROLE_CODE, Principal
from .redis_store import RedisStore
from .security import decode_access_jwt

logger = logging.getLogger(__name__)

# 权限变更时会主动删缓存，TTL 只是兜底
PRINCIPAL_CACHE_TTL = 60

_BUILTIN_ROLES = (
    (ADMIN_ROLE_CODE, "超级管理员", "拥有全部菜单、可见全部数据（固定，不可修改）"),
    (DEFAULT_ROLE_CODE, "普通用户", "新建用户的默认角色，只能看 / 改本人名下数据"),
)


# ---------------------------------------------------------------------------
# 启动种子
# ---------------------------------------------------------------------------
async def seed_rbac() -> None:
    """确保内置角色存在；首次升级时把旧 role=admin 的账号绑定为超级管理员。

    旧账号绑定只在 sys_user_role 为空时做一次：之后管理员可以随意调整角色，
    重启不会把被收回权限的账号又加回超级管理员。
    """
    async with SessionLocal() as s:
        existing = {r.code: r for r in (await s.execute(select(Role))).scalars().all()}
        for code, name, remark in _BUILTIN_ROLES:
            if code in existing:
                continue
            role = Role(code=code, name=name, remark=remark, is_builtin=True, enabled=True)
            s.add(role)
            await s.flush()
            existing[code] = role
            if code == DEFAULT_ROLE_CODE:
                for menu in permissions.DEFAULT_USER_MENUS:
                    s.add(RoleMenu(role_id=role.id, menu_code=menu))
        bound = await s.scalar(select(func.count()).select_from(UserRole))
        if not bound:
            admin_role_id = existing[ADMIN_ROLE_CODE].id
            legacy = (
                await s.execute(select(User.id).where(User.role == "admin"))
            ).scalars().all()
            for uid in legacy:
                s.add(UserRole(user_id=uid, role_id=admin_role_id))
            if legacy:
                logger.info("rbac: bound %d legacy admin account(s) to role admin", len(legacy))
        await s.commit()


# ---------------------------------------------------------------------------
# 登录身份
# ---------------------------------------------------------------------------
async def _principal_data(username: str) -> Optional[dict]:
    """从库里组装权限快照（可 JSON 缓存）；用户不存在返回 None。"""
    async with SessionLocal() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if not user:
            return None
        rows = (
            await s.execute(
                select(Role.code, RoleMenu.menu_code)
                .select_from(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .outerjoin(RoleMenu, RoleMenu.role_id == Role.id)
                .where(UserRole.user_id == user.id, Role.enabled.is_(True))
            )
        ).all()
    is_admin = any(code == ADMIN_ROLE_CODE for code, _ in rows)
    menus = permissions.resolve_menus(is_admin, [m for _, m in rows if m])
    return {
        "user_id": user.id,
        "username": user.username,
        "is_active": bool(user.is_active),
        "is_admin": is_admin,
        "menus": permissions.normalize_menu_codes(menus),
        "token_version": int(user.token_version or 0),
    }


async def load_principal(store: Optional[RedisStore], username: str) -> Optional[Principal]:
    """读取用户的登录身份（先 Redis，未命中查库并回填）；不存在或已禁用返回 None。

    Redis 不可用时直接查库，鉴权不因缓存故障而全部失败。
    """
    data: Optional[dict] = None
    if store is not None:
        try:
            data = await store.get_principal_cache(username)
        except Exception:  # noqa: BLE001
            data = None
    if data is None:
        data = await _principal_data(username)
        if data is None:
            return None
        if store is not None:
            try:
                await store.set_principal_cache(username, data, PRINCIPAL_CACHE_TTL)
            except Exception:  # noqa: BLE001
                pass
    if not data.get("is_active"):
        return None
    return Principal(
        user_id=int(data["user_id"]),
        username=str(data["username"]),
        is_admin=bool(data.get("is_admin")),
        menus=frozenset(data.get("menus") or []),
        token_version=int(data.get("token_version") or 0),
    )


async def principal_from_token(store: Optional[RedisStore], token: str) -> Optional[Principal]:
    """校验 JWT 并换成登录身份：会话版本不符（改密 / 禁用后）或 uid 对不上都视为失效。"""
    claims = decode_access_jwt(token or "")
    if not claims:
        return None
    p = await load_principal(store, claims["sub"])
    if p is None or claims["ver"] != p.token_version:
        return None
    uid = claims.get("uid")
    # 用户被删后同名重建：旧 token 的 uid 对不上新账号，不能沿用
    if uid is not None and permissions.normalize_owner(uid) != p.user_id:
        return None
    return p


async def invalidate_principals(store: Optional[RedisStore], usernames: Iterable[str]) -> None:
    names = [u for u in usernames if u]
    if store is None or not names:
        return
    try:
        await store.delete_principal_cache(*names)
    except Exception:  # noqa: BLE001
        logger.warning("invalidate principal cache failed: %s", names)


async def revoke_sessions(store: Optional[RedisStore], username: str) -> None:
    """禁用 / 改密 / 重置密码后：清缓存并断开后台 WS（前端据 4401 退出登录）。"""
    await invalidate_principals(store, [username])
    await manager.close_user(username, WS_CLOSE_REVOKED)


async def refresh_sessions(store: Optional[RedisStore], usernames: Iterable[str]) -> None:
    """角色 / 菜单 / 节点归属变更后：清缓存并断开后台 WS（前端据 4001 重新拉取后重连）。"""
    names = sorted({u for u in usernames if u})
    await invalidate_principals(store, names)
    for name in names:
        await manager.close_user(name, WS_CLOSE_PERM_CHANGED)


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
def _role_dict(role: Role, menus: list[str], user_count: int) -> dict:
    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "is_builtin": bool(role.is_builtin),
        "enabled": bool(role.enabled),
        "remark": role.remark,
        "menus": (
            list(permissions.ALL_MENU_CODES) if role.code == ADMIN_ROLE_CODE
            else permissions.normalize_menu_codes(menus)
        ),
        "user_count": user_count,
        "created_at": role.created_at.timestamp() if role.created_at else None,
    }


async def list_roles() -> list[dict]:
    async with SessionLocal() as s:
        roles = (await s.execute(select(Role).order_by(Role.id.asc()))).scalars().all()
        menu_rows = (await s.execute(select(RoleMenu.role_id, RoleMenu.menu_code))).all()
        count_rows = (
            await s.execute(
                select(UserRole.role_id, func.count()).group_by(UserRole.role_id)
            )
        ).all()
    menus: dict[int, list[str]] = {}
    for rid, code in menu_rows:
        menus.setdefault(rid, []).append(code)
    counts = {rid: int(n) for rid, n in count_rows}
    return [_role_dict(r, menus.get(r.id, []), counts.get(r.id, 0)) for r in roles]


async def get_role(role_id: int) -> Optional[dict]:
    for role in await list_roles():
        if role["id"] == int(role_id):
            return role
    return None


async def role_code_exists(code: str) -> bool:
    async with SessionLocal() as s:
        return (await s.execute(select(Role.id).where(Role.code == code))).first() is not None


def _validate_menus(codes: Iterable[object]) -> list[str]:
    bad = permissions.invalid_menu_codes(codes)
    if bad:
        raise ValueError(f"菜单不存在或仅限管理员：{'、'.join(bad)}")
    return permissions.normalize_menu_codes(codes)


async def create_role(
    *, code: str, name: str, remark: Optional[str], enabled: bool, menus: Iterable[object],
) -> dict:
    """新建自定义角色；菜单只能从可分配菜单里选（未知 / 仅管理员菜单抛 ValueError）。"""
    menu_codes = _validate_menus(menus)
    async with SessionLocal() as s:
        role = Role(
            code=code, name=name.strip(), remark=(remark or "").strip() or None,
            is_builtin=False, enabled=enabled,
        )
        s.add(role)
        await s.flush()
        for menu in menu_codes:
            s.add(RoleMenu(role_id=role.id, menu_code=menu))
        await s.commit()
        role_id = role.id
    return await get_role(role_id)


async def update_role(
    role_id: int, *, name: Optional[str], remark: Optional[str], enabled: Optional[bool],
) -> Optional[dict]:
    """修改名称 / 备注 / 启用状态。超级管理员角色不可修改，内置角色不可停用。"""
    async with SessionLocal() as s:
        role = await s.get(Role, int(role_id))
        if not role:
            return None
        if role.code == ADMIN_ROLE_CODE:
            raise ValueError("超级管理员角色固定，不可修改")
        if enabled is not None and role.is_builtin and not enabled:
            raise ValueError("内置角色不可停用")
        if name is not None and name.strip():
            role.name = name.strip()
        if remark is not None:
            role.remark = remark.strip() or None
        if enabled is not None:
            role.enabled = enabled
        await s.commit()
    return await get_role(role_id)


async def set_role_menus(role_id: int, menus: Iterable[object]) -> Optional[dict]:
    """整体替换角色菜单；超级管理员角色固定拥有全部菜单，不可修改。"""
    menu_codes = _validate_menus(menus)
    async with SessionLocal() as s:
        role = await s.get(Role, int(role_id))
        if not role:
            return None
        if role.code == ADMIN_ROLE_CODE:
            raise ValueError("超级管理员角色固定拥有全部菜单，不可修改")
        await s.execute(delete(RoleMenu).where(RoleMenu.role_id == role.id))
        for menu in menu_codes:
            s.add(RoleMenu(role_id=role.id, menu_code=menu))
        await s.commit()
    return await get_role(role_id)


async def delete_role(role_id: int) -> bool:
    """删除自定义角色（内置角色抛 ValueError；是否仍有用户由路由层先行校验）。"""
    async with SessionLocal() as s:
        role = await s.get(Role, int(role_id))
        if not role:
            return False
        if role.is_builtin:
            raise ValueError("内置角色不可删除")
        await s.execute(delete(RoleMenu).where(RoleMenu.role_id == role.id))
        await s.execute(delete(UserRole).where(UserRole.role_id == role.id))
        await s.delete(role)
        await s.commit()
        return True


async def usernames_with_role(role_id: int) -> list[str]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(User.username)
                .join(UserRole, UserRole.user_id == User.id)
                .where(UserRole.role_id == int(role_id))
            )
        ).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# 用户 × 角色
# ---------------------------------------------------------------------------
async def roles_by_user() -> dict[int, list[dict]]:
    """{user_id: [{id, code, name, enabled}]}，供用户列表展示。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(UserRole.user_id, Role.id, Role.code, Role.name, Role.enabled)
                .join(Role, Role.id == UserRole.role_id)
                .order_by(Role.id.asc())
            )
        ).all()
    out: dict[int, list[dict]] = {}
    for uid, rid, code, name, enabled in rows:
        out.setdefault(uid, []).append(
            {"id": rid, "code": code, "name": name, "enabled": bool(enabled)}
        )
    return out


async def set_user_roles(user_id: int, role_ids: Iterable[int]) -> list[dict]:
    """整体替换用户角色；角色不存在抛 ValueError。返回替换后的角色列表。"""
    wanted = sorted({int(r) for r in role_ids})
    async with SessionLocal() as s:
        if wanted:
            found = set(
                (await s.execute(select(Role.id).where(Role.id.in_(wanted)))).scalars().all()
            )
            missing = [r for r in wanted if r not in found]
            if missing:
                raise ValueError(f"角色不存在：{'、'.join(str(r) for r in missing)}")
        await s.execute(delete(UserRole).where(UserRole.user_id == int(user_id)))
        for rid in wanted:
            s.add(UserRole(user_id=int(user_id), role_id=rid))
        await s.commit()
    return (await roles_by_user()).get(int(user_id), [])


async def default_role_id() -> Optional[int]:
    async with SessionLocal() as s:
        return (
            await s.execute(select(Role.id).where(Role.code == DEFAULT_ROLE_CODE))
        ).scalar_one_or_none()


async def active_admin_ids() -> set[int]:
    """当前启用中的超级管理员用户 ID（最后一个管理员保护用）。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(User.id)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    Role.code == ADMIN_ROLE_CODE,
                    Role.enabled.is_(True),
                    User.is_active.is_(True),
                )
            )
        ).scalars().all()
    return set(rows)


async def admin_role_id() -> Optional[int]:
    async with SessionLocal() as s:
        return (
            await s.execute(select(Role.id).where(Role.code == ADMIN_ROLE_CODE))
        ).scalar_one_or_none()


async def username_map() -> dict[int, str]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(User.id, User.username))).all()
    return {uid: name for uid, name in rows}


async def owner_names_for(p: Principal) -> dict[int, str]:
    """列表「所有者」列用的 {user_id: username}：普通用户只会看到自己名下的数据，不必查库。"""
    if not p.is_admin:
        return {p.user_id: p.username}
    return await username_map()


# ---------------------------------------------------------------------------
# 后台推送：事件所属资源的所有者
# ---------------------------------------------------------------------------
async def resolve_owner(store: RedisStore, resource: tuple[str, str]) -> Optional[int]:
    """("group"|"node", id) -> owner_user_id；资源已不存在时返回 None（只有管理员可见）。"""
    kind, rid = resource
    try:
        d = await (store.get_group(rid) if kind == "group" else store.get_node(rid))
    except Exception:  # noqa: BLE001
        return None
    return permissions.normalize_owner((d or {}).get("owner_user_id"))
