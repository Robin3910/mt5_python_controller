"""后台用户持久化：MySQL/SQLite 为权威数据源。"""
import json
from datetime import datetime
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


async def get_user_by_phone(phone: str) -> Optional[User]:
    """按手机号查用户；phone 存归一化后的 E.164。"""
    if not phone:
        return None
    async with SessionLocal() as s:
        return (
            await s.execute(select(User).where(User.phone == phone))
        ).scalar_one_or_none()


async def bind_phone(user_id: int, phone: str, *, verified: bool = False) -> Optional[User]:
    """给用户绑定手机号；已绑定到别人时返回 None（唯一索引冲突由调用方兜底）。"""
    async with SessionLocal() as s:
        user = await s.get(User, int(user_id))
        if user is None:
            return None
        owner = (
            await s.execute(select(User).where(User.phone == phone, User.id != int(user_id)))
        ).scalar_one_or_none()
        if owner is not None:
            return None
        user.phone = phone
        user.phone_verified = bool(verified)
        await s.commit()
        await s.refresh(user)
        return user


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


# ---------------------------------------------------------------------------
# 暂停接单（两个市场各一个开关）
# ---------------------------------------------------------------------------
#
# 共同语义是拒绝新跟单、保留已有关系与在跑任务。
#   ① 新的客户无法再加入该市场名下的跟单分组；
#   ② 已有跟单关系与在跑任务**不动**，等跟单员自行移出；
#   ③ 带单暂停仍只发主节点，AI 暂停拦截分组所有节点的新开仓。
#
# 写库与「跟单关系」「任务」完全无关 —— 暂停不动任何跟单数据。
async def set_follow_paused(user_id: int, paused: bool) -> Optional[User]:
    """设置【带单】市场的暂停开关；返回更新后的用户（用户不存在返回 None）。"""
    async with SessionLocal() as s:
        row = await s.get(User, int(user_id))
        if row is None:
            return None
        row.follow_paused = bool(paused)
        row.follow_paused_at = datetime.now() if paused else None
        await s.commit()
        await s.refresh(row)
        return row


async def set_ai_follow_paused(user_id: int, paused: bool) -> Optional[User]:
    """设置【AI 交易】市场的暂停开关；返回更新后的用户（用户不存在返回 None）。"""
    async with SessionLocal() as s:
        row = await s.get(User, int(user_id))
        if row is None:
            return None
        row.ai_follow_paused = bool(paused)
        row.ai_follow_paused_at = datetime.now() if paused else None
        await s.commit()
        await s.refresh(row)
        return row


async def create_user(
    username: str, password: str, *, display_name: Optional[str] = None, is_active: bool = True,
    phone: Optional[str] = None, phone_verified: bool = False,
) -> User:
    """新建后台用户。旧 role 字段写 user：它只在首次升级 RBAC 时被读取，不能写成 admin。

    用户名重复会触发 IntegrityError，由路由层转成 409。
    phone 传了就一并写入（手机号注册路径用），避免「建号后再补手机号」的两步竞态。
    """
    async with SessionLocal() as s:
        user = User(
            username=username,
            display_name=(display_name or "").strip() or None,
            password_hash=hash_token(password),
            role="user",
            is_active=is_active,
            token_version=0,
            phone=phone,
            phone_verified=bool(phone_verified),
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def update_profile(
    user_id: int, *, display_name: Optional[str] = None, email: Optional[str] = None,
    leader_name: Optional[str] = None, leader_intro: Optional[str] = None,
) -> Optional[User]:
    """更新个人中心的自助维护字段（只改自己）。

    与 `update_user` 的区别：这里**不碰** is_active / phone / 角色 ——
    那三样要么是管理员的事，要么要走短信验证，不能由自助入口改。

    字段级语义与 update_user 一致：
    - 传 None（不传）-> 不改该字段
    - 传空串 -> 清空（置 NULL）
    """
    async with SessionLocal() as s:
        user = await s.get(User, int(user_id))
        if not user:
            return None
        if display_name is not None:
            user.display_name = display_name.strip() or None
        if email is not None:
            user.email = email.strip() or None
        if leader_name is not None:
            user.leader_name = leader_name.strip() or None
        if leader_intro is not None:
            user.leader_intro = leader_intro.strip() or None
        await s.commit()
        await s.refresh(user)
        return user


async def update_user(
    user_id: int, *, display_name: Optional[str] = None, is_active: Optional[bool] = None,
    phone: Optional[str] = None, phone_verified: Optional[bool] = None,
) -> Optional[User]:
    """更新显示名 / 启用状态 / 手机号；由启用改为禁用时吊销其全部旧会话。

    phone 传空串表示解绑手机号（置 NULL）；不传（None）表示不改。
    """
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
        if phone is not None:
            new_phone = phone.strip() or None
            if new_phone is not None:
                owner = (
                    await s.execute(
                        select(User).where(User.phone == new_phone, User.id != int(user_id))
                    )
                ).scalar_one_or_none()
                if owner is not None:
                    raise ValueError("该手机号已被其他用户绑定")
            user.phone = new_phone
            # 换绑手机号等于放弃旧的短信验证身份，除非调用方显式声明已验证
            user.phone_verified = bool(phone_verified) if phone_verified is not None else False
        elif phone_verified is not None:
            user.phone_verified = bool(phone_verified)
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
