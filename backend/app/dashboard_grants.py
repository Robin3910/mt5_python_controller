"""仅供已授权守护的签名能力；不承载用户会话，不授权配置/平仓等交互操作。"""
from __future__ import annotations

import time

import jwt
from fastapi import HTTPException

from . import permissions, rbac_service
from .db import SessionLocal
from .node_credentials import require_active
from .orm import Node, NodeCredential, User
from .settings import settings

GRANT_TYPE = "dashboard_daemon"


def create_grant(principal, node, generation: int, operation_id: int) -> str:
    return jwt.encode({
        "typ": GRANT_TYPE, "iat": int(time.time()), "actor_uid": principal.user_id,
        "actor_username": principal.username,
        "node_id": node.node_id, "owner_uid": node.owner_user_id,
        "generation": generation,
        "grant_operation_id": operation_id,
    }, settings.jwt_secret, algorithm="HS256")


def decode_grant(token: str) -> dict:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                          options={"require": ["typ", "actor_uid", "actor_username", "node_id", "generation", "iat", "grant_operation_id"]})
        if data.get("typ") != GRANT_TYPE:
            raise ValueError("wrong token type")
        if not isinstance(data["actor_uid"], int) or not isinstance(data["generation"], int):
            raise ValueError("invalid identity")
        return data
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "invalid daemon grant") from exc


async def validate_grant(token: str):
    claims = decode_grant(token)
    async with SessionLocal() as session:
        user = await session.get(User, claims["actor_uid"])
        node = await session.get(Node, claims["node_id"])
        cred = await session.get(NodeCredential, claims["node_id"])
        if not user or not user.is_active or user.username != claims["actor_username"] or not node or not cred:
            raise HTTPException(403, "daemon grant revoked")
        principal = await rbac_service.load_principal(None, user.username)
        if (not principal or not permissions.can_menu(principal, permissions.MENU_NODES)
                or not permissions.owns(principal, node.owner_user_id)
                or node.owner_user_id != claims.get("owner_uid")
                or cred.owner_user_id != node.owner_user_id
                or cred.generation != claims["generation"]):
            raise HTTPException(403, "daemon grant revoked")
        require_active(node)
        return principal, node, cred
