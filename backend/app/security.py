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
    if data.get("typ") == "2fa_pending" or not data.get("sub"):
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
