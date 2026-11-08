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


# ---------------------------------------------------------------------------
# 改绑手机号票据
# ---------------------------------------------------------------------------
# 改绑 = 转移账号控制权，所以票据比「手机号已验证」票据更强：
# 它把「哪个用户」和「哪个新号码」一起钉死，缺一不可。
TYP_PHONE_REBIND = "phone_rebind"
# 票据有效期 10 分钟：够用户从容走完「验证新号 → 验证本人身份」两步。
PHONE_REBIND_TICKET_TTL = 600


def create_phone_rebind_jwt(phone: str, user_id: int) -> str:
    """签发「新号已验证」票据（改绑专用），有效期 10 分钟。

    payload：
    - sub  = 归一化后的**新**手机号
    - uid  = 要改绑的用户 id
    - typ  = phone_rebind（与 phone_pending 类型隔离，不能互相顶替）
    """
    payload = {
        "sub": phone,
        "uid": int(user_id),
        "typ": TYP_PHONE_REBIND,
        "exp": int(time.time()) + PHONE_REBIND_TICKET_TTL,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_phone_rebind_jwt(token: str, user_id: int, phone: str) -> Optional[str]:
    """校验改绑票据；类型 / uid / 手机号三者都要对上才通过。

    三者全对是必须的：
    - 类型对：防止拿「手机号已验证」票据（登录 / 注册用）来改绑；
    - uid 对：防止拿 A 用户的新号票据去改绑 B 的账号；
    - 手机号对：防止票据里的号与提交的号不一致（票据被截胡/改号）。
    """
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except Exception:
        return None
    if data.get("typ") != TYP_PHONE_REBIND:
        return None
    try:
        if int(data.get("uid")) != int(user_id):
            return None
    except (TypeError, ValueError):
        return None
    sub = data.get("sub")
    if not sub or str(sub) != phone:
        return None
    return str(sub)
