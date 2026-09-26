"""后台用户持久化：MySQL/SQLite 为权威数据源。"""
import json
from typing import Optional

from sqlalchemy import delete, func, select, update

from .db import SessionLocal
from .orm import User, UserRole
from .redis_store import RedisStore
from .security import compare_secret, hash_token
from .settings import settings
from .totp import generate_totp_secret, totp_provisioning_uri, totp_qr_data_uri, verify_totp_code

K_ADMIN_CREDS_LEGACY = "config:admin:creds"  # 旧版 Redis 凭据 key（仅启动时迁移用）


async def _migrate_legacy_redis_creds(store: RedisStore) -> Optional[tuple[str, str]]:
    """一次性从旧版 Redis 凭据迁移到用户表。"""
    raw = await store.r.get(K_ADMIN_CREDS_LEGACY)
    if not raw:
        return None
    creds = json.loads(raw)
    return creds["username"], creds["password_hash"]


async def get_user_by_username(username: str) -> Optional[User]:
    async with SessionLocal() as s:
        return (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()


async def verify_user_password(username: str, password: str) -> bool:
    """校验用户名与密码（常量时间比较哈希）。"""
    user = await get_user_by_username(username)
    if not user or not user.is_active:
        return False
    return compare_secret(hash_token(password), user.password_hash)


async def update_user_password(username: str, new_password: str) -> bool:
    """更新用户密码并吊销其全部旧会话（token_version + 1）；用户不存在时返回 False。"""
    async with SessionLocal() as s:
        result = await s.execute(
            update(User)
            .where(User.username == username)
            .values(
                password_hash=hash_token(new_password),
                token_version=User.token_version + 1,
            )
        )
        if result.rowcount == 0:
            return False
        await s.commit()
        return True


async def get_user_by_id(user_id: int) -> Optional[User]:
    async with SessionLocal() as s:
        return await s.get(User, int(user_id))


async def list_users() -> list[User]:
    async with SessionLocal() as s:
        return list((await s.execute(select(User).order_by(User.id.asc()))).scalars().all())


async def create_user(
    username: str, password: str, *, display_name: Optional[str] = None, is_active: bool = True,
) -> User:
    """新建后台用户。旧 role 字段写 user：它只在首次升级 RBAC 时被读取，不能写成 admin。

    用户名重复会触发 IntegrityError，由路由层转成 409。
    """
    async with SessionLocal() as s:
        user = User(
            username=username,
            display_name=(display_name or "").strip() or None,
            password_hash=hash_token(password),
            role="user",
            is_active=is_active,
            token_version=0,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def update_user(
    user_id: int, *, display_name: Optional[str] = None, is_active: Optional[bool] = None,
) -> Optional[User]:
    """更新显示名 / 启用状态；由启用改为禁用时吊销其全部旧会话。"""
    async with SessionLocal() as s:
        user = await s.get(User, int(user_id))
        if not user:
            return None
        if display_name is not None:
            user.display_name = display_name.strip() or None
        if is_active is not None:
            if user.is_active and not is_active:
                user.token_version = int(user.token_version or 0) + 1
            user.is_active = is_active
        await s.commit()
        await s.refresh(user)
        return user


async def delete_user(user_id: int) -> bool:
    """删除用户及其角色绑定；名下资源是否为空由路由层先行校验。"""
    async with SessionLocal() as s:
        user = await s.get(User, int(user_id))
        if not user:
            return False
        await s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await s.delete(user)
        await s.commit()
        return True


async def seed_default_admin(store: Optional[RedisStore] = None) -> None:
    """首次启动时写入默认管理员；若 Redis 中已有自定义凭据则迁移一次。"""
    async with SessionLocal() as s:
        count = await s.scalar(select(func.count()).select_from(User))
        if count and count > 0:
            return

        username = settings.admin_user
        password_hash = hash_token(settings.admin_password)
        if store:
            migrated = await _migrate_legacy_redis_creds(store)
            if migrated:
                username, password_hash = migrated

        s.add(User(username=username, password_hash=password_hash, role="admin"))
        await s.commit()


def user_2fa_status(user: User) -> dict:
    bound = bool(user.totp_secret)
    return {
        "enabled": bool(user.totp_enabled),
        "bound": bound,
        "pending_setup": bound and not user.totp_enabled,
    }


async def get_2fa_status(username: str) -> Optional[dict]:
    user = await get_user_by_username(username)
    if not user:
        return None
    return user_2fa_status(user)


async def user_requires_2fa(username: str) -> bool:
    user = await get_user_by_username(username)
    return bool(user and user.is_active and user.totp_enabled and user.totp_secret)


async def verify_user_totp(username: str, code: str) -> bool:
    user = await get_user_by_username(username)
    if not user or not user.totp_secret:
        return False
    return verify_totp_code(user.totp_secret, code.strip())


async def setup_totp(username: str) -> Optional[dict]:
    """生成新 TOTP 密钥（未启用，待 confirm）。"""
    secret = generate_totp_secret()
    async with SessionLocal() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if not user:
            return None
        user.totp_secret = secret
        user.totp_enabled = False
        await s.commit()

    uri = totp_provisioning_uri(username, secret)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_data_uri": totp_qr_data_uri(uri),
    }


async def confirm_totp(username: str, code: str) -> bool:
    """验证 TOTP 并启用 2FA。"""
    user = await get_user_by_username(username)
    if not user or not user.totp_secret:
        return False
    if not verify_totp_code(user.totp_secret, code.strip()):
        return False
    async with SessionLocal() as s:
        await s.execute(
            update(User).where(User.username == username).values(totp_enabled=True)
        )
        await s.commit()
    return True


async def enable_totp(username: str, code: str) -> bool:
    """已绑定密钥后重新开启 2FA。"""
    user = await get_user_by_username(username)
    if not user or not user.totp_secret:
        return False
    if not verify_totp_code(user.totp_secret, code.strip()):
        return False
    async with SessionLocal() as s:
        await s.execute(
            update(User).where(User.username == username).values(totp_enabled=True)
        )
        await s.commit()
    return True


async def disable_totp(username: str) -> bool:
    """关闭 2FA（保留密钥，可再次开启）。"""
    async with SessionLocal() as s:
        result = await s.execute(
            update(User).where(User.username == username).values(totp_enabled=False)
        )
        if result.rowcount == 0:
            return False
        await s.commit()
        return True


async def reset_totp(username: str) -> bool:
    """清除 TOTP 绑定。"""
    async with SessionLocal() as s:
        result = await s.execute(
            update(User)
            .where(User.username == username)
            .values(totp_secret=None, totp_enabled=False)
        )
        if result.rowcount == 0:
            return False
        await s.commit()
        return True
