"""令牌、哈希与后台 JWT。"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

import jwt

from .settings import settings


def make_node_id() -> str:
    """生成节点 ID（带可读前缀，便于日志辨识）。"""
    return "nd_" + secrets.token_hex(8)


def make_group_id() -> str:
    """生成分组 ID（带可读前缀，便于日志辨识）。"""
    return "grp_" + secrets.token_hex(8)


def make_strategy_id() -> str:
    """生成策略 ID（带可读前缀，便于日志辨识）。"""
    return "sty_" + secrets.token_hex(8)


def gen_token() -> str:
    """生成节点接入令牌（明文，仅创建/重置时返回一次）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌哈希：数据库与 Redis 只保存哈希值，不保存明文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_secret(a: str, b: str) -> bool:
    """常量时间比较，避免时序侧信道（用于账号/密码校验）。"""
    return hmac.compare_digest(a or "", b or "")


def create_jwt(sub: str, *, uid: Optional[int] = None, ver: int = 0) -> str:
    """签发后台 JWT。ver 为用户的会话版本（users.token_version），版本变了旧 token 即失效。"""
    payload = {
        "sub": sub,
        "typ": "access",
        "ver": int(ver or 0),
        "exp": int(time.time()) + settings.jwt_expire_minutes * 60,
    }
    if uid is not None:
        payload["uid"] = int(uid)
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_2fa_pending_jwt(sub: str) -> str:
    """签发登录中间态 JWT（密码已通过，待 2FA 验证），有效期 5 分钟。"""
    payload = {
        "sub": sub,
        "typ": "2fa_pending",
        "exp": int(time.time()) + 300,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_jwt(token: str) -> Optional[dict]:
    """校验正式 JWT，返回 {sub, uid, ver}；非法/过期/2FA 中间态返回 None。

    RBAC 之前签发的 token 没有 ver，按 0 处理，与未改过会话版本的用户兼容。
    """
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except Exception:
        return None
    if data.get("typ") not in (None, "access") or not data.get("sub"):
        return None
    try:
        ver = int(data.get("ver") or 0)
    except (TypeError, ValueError):
        return None
    return {"sub": str(data["sub"]), "uid": data.get("uid"), "ver": ver}


def verify_jwt(token: str) -> Optional[str]:
    """校验 JWT，返回 sub（用户名）；非法/过期/2FA 中间态返回 None。"""
    claims = decode_access_jwt(token)
    return claims["sub"] if claims else None


def verify_2fa_pending_jwt(token: str) -> Optional[str]:
    """校验 2FA 登录中间态 JWT，返回用户名。"""
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if data.get("typ") != "2fa_pending":
            return None
        return data.get("sub")
    except Exception:
        return None


def create_phone_pending_jwt(phone: str, purpose: str) -> str:
    """签发「手机号已验证」票据（验证码校验通过后签发），有效期 10 分钟。

    typ=phone_pending；sub 是归一化后的手机号；purpose 记用途。

    票据用 JWT 而非 Redis：与 2FA 中间态保持同一套做法，无状态、少一次 IO。
    """
    payload = {
        "sub": phone,
        "typ": "phone_pending",
        "purpose": purpose,
        "exp": int(time.time()) + 600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_phone_pending_jwt(token: str, purpose: str | set[str] | frozenset[str]) -> Optional[str]:
    """校验手机号票据；类型不符或用途不被允许一律返回 None。

    purpose 允许传单值或集合（如 {"login"} / {"register"}）—— 票据携带的用途
    只要落在允许集合内即通过。

    这样「一次验证码、两种去向」才能成立：
    - 校验通过时签发的 auth 票据，允许用于 login 与 register 两个动作
    - 注册票据只允许 register，不允许拿去登录（防止越权登录）
    """
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except Exception:
        return None
    if data.get("typ") != "phone_pending":
        return None
    allowed = {purpose} if isinstance(purpose, str) else set(purpose or ())
    if data.get("purpose") not in allowed:
        return None
    sub = data.get("sub")
    return str(sub) if sub else None
