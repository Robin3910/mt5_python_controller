"""专属节点凭证与审核开通；MySQL 为权威，旧全局凭证兼容由独立记录限定。"""
from __future__ import annotations

import secrets

from fastapi import HTTPException
from sqlalchemy import select

from . import permissions
from .db import SessionLocal
from .orm import Node, NodeCredential, Role, User, UserRole
from .security import compare_secret, hash_token


async def is_admin_user(session, user_id: int | None) -> bool:
    if user_id is None:
        return False
    role = await session.scalar(
        select(Role.id).join(UserRole, UserRole.role_id == Role.id)
        .join(User, User.id == UserRole.user_id)
        .where(User.id == user_id, User.is_active.is_(True), Role.code == permissions.ADMIN_ROLE_CODE,
               Role.enabled.is_(True))
    )
    return role is not None


async def activate_if_ready(session, node: Node) -> None:
    if node.approval_status != "pending" or not node.admin_enable_requested:
        return
    applicant = node.requested_by_user_id
    owner_matches = applicant is not None and node.owner_user_id == applicant
    if applicant is not None and node.owner_user_id is None:
        owner_matches = await is_admin_user(session, applicant)
    if owner_matches:
        node.approval_status = "approved"
        node.enabled = True


async def credential_row(session, node_id: str, *, create: bool = False, legacy_allowed: bool = True):
    row = await session.get(NodeCredential, node_id)
    if row is None and create:
        node = await session.get(Node, node_id)
        row = NodeCredential(node_id=node_id, generation=1, legacy_allowed=legacy_allowed,
                             owner_user_id=node.owner_user_id if node else None)
        session.add(row)
        await session.flush()
    return row


async def revoke_on_transfer(session, node_id: str, new_owner: int | None) -> None:
    row = await credential_row(session, node_id, create=True)
    row.token_sha256 = None
    row.generation += 1
    row.owner_user_id = new_owner


async def legacy_allowed(node_id: str) -> bool:
    async with SessionLocal() as session:
        row = await session.get(NodeCredential, node_id)
        return row is None or bool(row.legacy_allowed)


async def authenticate_scoped(token: str, mt5_login: int | None) -> dict | None:
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != "ndv1" or not parts[1] or not parts[2]:
        return None
    async with SessionLocal() as session:
        node = await session.get(Node, parts[1])
        cred = await session.get(NodeCredential, parts[1])
        if (not node or not cred or not cred.token_sha256 or node.mt5_login != mt5_login
                or cred.owner_user_id != node.owner_user_id):
            return None
        if not compare_secret(hash_token(token), cred.token_sha256):
            return None
        from .node_service import node_row_to_dict
        return node_row_to_dict(node)


def new_scoped_token(node_id: str) -> str:
    return f"ndv1.{node_id}.{secrets.token_urlsafe(32)}"


def require_owned(principal, node: Node | None, *, allow_application: bool = False):
    applicant = bool(node and allow_application and node.approval_status == "pending"
                     and node.requested_by_user_id == principal.user_id)
    if node is None or not (permissions.owns(principal, node.owner_user_id) or applicant):
        raise HTTPException(404, "node not found")
    return node


def require_active(node: Node):
    if node.approval_status != "approved" or not node.enabled:
        raise HTTPException(403, "node not approved or disabled")
