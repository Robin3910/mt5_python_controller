"""本机面板：账号鉴权、申请、专属凭证、守护能力与严格操作审计。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import dashboard_audit as audit
from . import dashboard_grants, node_credentials as credentials, node_service, permissions, rbac_service
from .connections import manager
from .db import SessionLocal
from .deps import client_ip, get_principal, get_store, require_menu
from .nodes import _to_node_out
from .orm import AuditLog, Node, NodeCredential
from .permissions import MENU_NODES, Principal
from .redis_store import RedisStore
from .security import hash_token, make_node_id

router = APIRouter(prefix="/api/node-dashboard", tags=["node-dashboard"])


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Enrollment(Payload):
    mt5_login: int = Field(gt=0)
    name: str | None = Field(default=None, max_length=64)
    mt5_server: str | None = Field(default=None, max_length=64)


class NodeRef(Payload):
    node_id: str = Field(min_length=1, max_length=32)


class VerifyCredential(NodeRef):
    token: str = Field(min_length=1, max_length=256)


class RotateCredential(NodeRef):
    expected_generation: int = Field(ge=0)


class Action(Payload):
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    action: str = Field(min_length=1, max_length=40)
    node_id: str | None = Field(default=None, max_length=32)
    instance_id: str | None = Field(default=None, max_length=64)
    params: dict = Field(default_factory=dict)


class Result(Payload):
    result: Literal["ok", "fail"]
    params: dict = Field(default_factory=dict)


class Grant(Payload):
    grant_token: str = Field(min_length=1, max_length=2048)


class GrantEvent(Grant):
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    phase: Literal["intent", "result"]
    result: Literal["ok", "fail"] | None = None
    params: dict = Field(default_factory=dict)


class ReplayEvent(Payload):
    grant_operation_id: int = Field(gt=0)
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    phase: Literal["intent", "result"]
    result: Literal["ok", "fail"] | None = None
    params: dict = Field(default_factory=dict)


PREPARE_ACTIONS = frozenset({
    "add_instance", "import_node", "edit_instance", "edit_config", "edit_env", "bind_instance",
    "save_connection", "connection_config", "logout", "version_check", "download_package",
})
ACTIONS = PREPARE_ACTIONS | frozenset({
    "view_status", "view_log", "remove_instance", "start", "stop", "restart", "set_daemon",
    "daemon_on", "daemon_off", "refresh_health", "clear_log", "open_cwd", "update_client",
    "rollback_client", "replace_client", "update", "rollback", "replace",
    "credential_issue", "credential_rotate", "credential_verify",
})
ACTIVE_ACTIONS = frozenset({"start", "restart", "daemon_on"})
PENDING_ACTIONS = PREPARE_ACTIONS | frozenset({
    "view_status", "open_cwd", "remove_instance", "update", "update_client",
    "rollback", "rollback_client", "replace", "replace_client",
})
# 未绑定后端节点的本机实例：管理员可查看并替换客户端文件，不能因此获得启动权。
LOCAL_INVENTORY_ACTIONS = frozenset({
    "view_status", "update", "update_client", "rollback", "rollback_client", "replace", "replace_client",
})


@asynccontextmanager
async def strict_session():
    try:
        async with SessionLocal() as session:
            yield session
            await session.commit()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "audit or database unavailable") from exc


def retry_unique_request(function):
    """并发首次写入冲突后重新读取已提交记录；业务校验仍由原入口完成。"""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except HTTPException as exc:
            if exc.status_code != 503 or not isinstance(exc.__cause__, IntegrityError):
                raise
            return await function(*args, **kwargs)
    return wrapped


def operation_out(row, request_id: str | None = None, node=None):
    return {
        "operation_id": row.id, "request_id": request_id, "result": row.result,
        "node_id": node.node_id if node else (row.params_json or {}).get("node_id"),
        "mt5_login": node.mt5_login if node else (row.params_json or {}).get("mt5_login"),
    }


async def owned_node(session, p, node_id, *, application=False, active=False, locked=False):
    row = await node_service._locked_node(session, node_id) if locked else await session.get(Node, node_id)
    node = credentials.require_owned(p, row, allow_application=application)
    if active:
        credentials.require_active(node)
    return node


@router.get("/enrollments")
async def enrollments(store: RedisStore = Depends(get_store), p: Principal = Depends(require_menu(MENU_NODES))):
    async with strict_session() as session:
        stmt = select(Node).where(Node.requested_by_user_id.is_not(None)).order_by(Node.created_at)
        if not p.is_admin:
            stmt = stmt.where(Node.requested_by_user_id == p.user_id,
                              or_(Node.approval_status == "pending", Node.owner_user_id == p.user_id))
        items = [node_service.node_row_to_dict(n) for n in (await session.scalars(stmt)).all()]
    names = await rbac_service.owner_names_for(p)
    return {"items": [await _to_node_out(store, n, names) for n in items]}


@router.post("/enrollments", status_code=201)
async def enroll(body: Enrollment, request: Request, store: RedisStore = Depends(get_store),
                 p: Principal = Depends(require_menu(MENU_NODES))):
    try:
        async with strict_session() as session:
            existing = await session.scalar(select(Node).where(Node.mt5_login == body.mt5_login))
            if existing:
                pending_applicant = existing.approval_status == "pending" and existing.requested_by_user_id == p.user_id
                if not pending_applicant and not permissions.owns(p, existing.owner_user_id):
                    raise HTTPException(409, "MT5 account already registered; contact administrator")
                node = existing
            else:
                node = Node(node_id=make_node_id(), name=(body.name or "").strip() or str(body.mt5_login),
                            mt5_login=body.mt5_login, mt5_server=body.mt5_server,
                            enabled=False, approval_status="pending", requested_by_user_id=p.user_id,
                            owner_user_id=None, admin_enable_requested=False,
                            lot_mode=node_service.AUTO_NODE_DEFAULTS["lot_mode"],
                            lot=node_service.AUTO_NODE_DEFAULTS["lot"],
                            poll_order=node_service.AUTO_NODE_DEFAULTS["poll_order"])
                session.add(node)
                await session.flush()
                await credentials.credential_row(session, node.node_id, create=True, legacy_allowed=False)
                await audit.begin_operation(session, p, "enrollment", target=node.node_id,
                                            params={"mt5_login": node.mt5_login}, ip=client_ip(request), result="ok")
            # 新建行的 created_at / updated_at 由数据库填充，后续 flush 会把它们标成过期。
            # 同步读取会在异步会话里触发隐式 IO，被收成 503。
            await session.refresh(node)
            data = node_service.node_row_to_dict(node)
    except HTTPException as exc:
        if exc.status_code == 503 and isinstance(exc.__cause__, IntegrityError):
            raise HTTPException(409, "MT5 account already registered; retry enrollment") from exc
        raise
    await store.cache_node(data)
    await manager.broadcast_admin({"type": "node_registered", "data": data})
    return await _to_node_out(store, data, await rbac_service.owner_names_for(p))


async def issue_credential(body, p, request, *, rotate=False):
    async with strict_session() as session:
        node = await owned_node(session, p, body.node_id, active=True, locked=True)
        cred = await credentials.credential_row(session, node.node_id, create=True)
        generation = cred.generation
        if rotate:
            if body.expected_generation != generation:
                raise HTTPException(409, "credential generation changed")
        elif cred.token_sha256:
            raise HTTPException(409, "credential already issued; verify local token or rotate explicitly")
        token = credentials.new_scoped_token(node.node_id)
        new_generation = generation + 1 if rotate else generation
        conditions = [NodeCredential.node_id == node.node_id, NodeCredential.generation == generation]
        if not rotate:
            conditions.append(NodeCredential.token_sha256.is_(None))
        changed = await session.execute(update(NodeCredential).where(*conditions)
                                        .values(token_sha256=hash_token(token), generation=new_generation,
                                                legacy_allowed=False, owner_user_id=node.owner_user_id))
        if changed.rowcount != 1:
            raise HTTPException(409, "credential generation changed")
        await audit.begin_operation(session, p, "credential_rotate" if rotate else "credential_issue",
                                    target=node.node_id, params={"generation": new_generation},
                                    ip=client_ip(request), result="ok")
        result = {"node_id": node.node_id, "mt5_login": node.mt5_login, "token": token, "generation": new_generation}
    return result


@router.post("/credentials/issue")
@retry_unique_request
async def credential_issue(body: NodeRef, request: Request, p: Principal = Depends(require_menu(MENU_NODES))):
    return await issue_credential(body, p, request)


@router.post("/credentials/rotate")
async def credential_rotate(body: RotateCredential, request: Request, p: Principal = Depends(require_menu(MENU_NODES))):
    return await issue_credential(body, p, request, rotate=True)


@router.post("/credentials/verify")
async def credential_verify(body: VerifyCredential, request: Request, p: Principal = Depends(require_menu(MENU_NODES))):
    async with strict_session() as session:
        node = await owned_node(session, p, body.node_id, active=True)
        cred = await session.get(NodeCredential, node.node_id)
        from .security import compare_secret
        valid = bool(cred and cred.token_sha256 and cred.owner_user_id == node.owner_user_id
                     and compare_secret(hash_token(body.token), cred.token_sha256))
        generation = cred.generation if cred else 0
        await audit.begin_operation(session, p, "credential_verify", target=node.node_id,
                                    params={"generation": generation}, ip=client_ip(request),
                                    result="ok" if valid else "fail")
        return {"valid": valid, "node_id": node.node_id, "mt5_login": node.mt5_login, "generation": generation}


@router.post("/actions")
@retry_unique_request
async def action_intent(body: Action, request: Request, p: Principal = Depends(get_principal)):
    if body.action not in ACTIONS:
        raise HTTPException(422, "unsupported dashboard action")
    if body.action != "logout" and not permissions.can_menu(p, MENU_NODES):
        raise HTTPException(403, "无权访问该功能")
    if body.action == "bind_instance" and not p.is_admin:
        raise HTTPException(403, "仅管理员可绑定旧实例")
    if body.action == "set_daemon" and not isinstance(body.params.get("enabled"), bool):
        raise HTTPException(422, "set_daemon requires a boolean enabled")
    async with strict_session() as session:
        active = body.action in ACTIVE_ACTIONS or (body.action == "set_daemon" and body.params.get("enabled") is True)
        node = await owned_node(session, p, body.node_id, application=body.action in PENDING_ACTIONS and not active,
                                active=active) if body.node_id else None
        local_inventory = p.is_admin and body.action in LOCAL_INVENTORY_ACTIONS
        if node is None and body.action not in PREPARE_ACTIONS and not local_inventory:
            raise HTTPException(422, "node_id required")
        params = audit.clean_params(body.params)
        if body.instance_id:
            params["instance_id"] = body.instance_id
        if node:
            params.update(node_id=node.node_id, mt5_login=node.mt5_login)
        target = node.node_id if node else body.instance_id
        prior = await audit.prior_operation(session, p.user_id, body.request_id)
        if prior:
            if prior.action != f"dashboard_{body.action}" or prior.target != target or prior.params_json != params:
                raise HTTPException(409, "request_id reused for a different action")
            return operation_out(prior, body.request_id, node)
        row = await audit.begin_operation(session, p, body.action, request_id=body.request_id,
                                          target=target, params=params, ip=client_ip(request))
        return operation_out(row, body.request_id, node)


@router.post("/actions/{operation_id}/result")
async def action_result(operation_id: int, body: Result, p: Principal = Depends(get_principal)):
    async with strict_session() as session:
        actor_id = p.user_id
        if p.is_admin:
            previous = await session.get(AuditLog, operation_id)
            if previous and previous.actor_user_id is not None:
                actor_id = previous.actor_user_id
        row = await audit.finish_operation(session, actor_id, operation_id, body.result, body.params)
        return operation_out(row)


@router.post("/daemon-grants")
@retry_unique_request
async def daemon_grant(body: NodeRef, request: Request, p: Principal = Depends(require_menu(MENU_NODES))):
    async with strict_session() as session:
        node = await owned_node(session, p, body.node_id, active=True, locked=True)
        cred = await credentials.credential_row(session, node.node_id, create=True)
        row = await audit.begin_operation(session, p, "daemon_grant", target=node.node_id,
                                          params={"generation": cred.generation, "node_id": node.node_id,
                                                  "mt5_login": node.mt5_login}, ip=client_ip(request), result="ok")
        token = dashboard_grants.create_grant(p, node, cred.generation, row.id)
        return {"grant_token": token, "node_id": node.node_id, "mt5_login": node.mt5_login,
                "generation": cred.generation, "grant_operation_id": row.id, "actor_user_id": p.user_id}


@router.post("/daemon-grants/check")
async def daemon_check(body: Grant):
    try:
        _, node, cred = await dashboard_grants.validate_grant(body.grant_token)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"ok": True, "node_id": node.node_id, "mt5_login": node.mt5_login, "generation": cred.generation}


@router.post("/daemon-grants/events")
@retry_unique_request
async def daemon_event(body: GrantEvent, request: Request):
    try:
        principal, node, _ = await dashboard_grants.validate_grant(body.grant_token)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "database unavailable") from exc
    async with strict_session() as session:
        claims = dashboard_grants.decode_grant(body.grant_token)
        prior = await audit.prior_operation(session, principal.user_id, body.request_id)
        if body.phase == "result":
            if (not prior or prior.action != "dashboard_daemon_restart" or prior.target != node.node_id
                    or (prior.params_json or {}).get("grant_operation_id") != claims["grant_operation_id"]):
                raise HTTPException(404, "operation not found")
            if not body.result:
                raise HTTPException(422, "result required")
            row = await audit.finish_operation(session, principal.user_id, prior.id, body.result, body.params)
        else:
            params = audit.clean_params(body.params)
            params.update(node_id=node.node_id, mt5_login=node.mt5_login,
                          grant_operation_id=claims["grant_operation_id"])
            if prior:
                if prior.action != "dashboard_daemon_restart" or prior.target != node.node_id or prior.params_json != params:
                    raise HTTPException(409, "request_id reused for a different action")
                row = prior
            else:
                row = await audit.begin_operation(session, principal, "daemon_restart", request_id=body.request_id,
                                                  target=node.node_id, params=params, ip=client_ip(request))
        return operation_out(row, body.request_id, node)


@router.post("/daemon-events/replay")
@retry_unique_request
async def daemon_replay(body: ReplayEvent, request: Request, p: Principal = Depends(get_principal)):
    """只补记原授予者的历史重启事件；不恢复、签发或延长任何守护能力。"""
    async with strict_session() as session:
        grant = await session.get(AuditLog, body.grant_operation_id)
        if (not grant or grant.action != "dashboard_daemon_grant" or not grant.actor_user_id
                or (not p.is_admin and grant.actor_user_id != p.user_id)):
            raise HTTPException(404, "grant operation not found")
        actor = SimpleNamespace(user_id=grant.actor_user_id, username=grant.operator)
        prior = await audit.prior_operation(session, actor.user_id, body.request_id)
        grant_params = grant.params_json or {}
        params = audit.clean_params(body.params)
        params.update(node_id=grant.target, mt5_login=grant_params.get("mt5_login"), grant_operation_id=grant.id)
        if prior and (prior.action != "dashboard_daemon_restart" or prior.target != grant.target
                      or (prior.params_json or {}).get("grant_operation_id") != grant.id):
            raise HTTPException(409, "request_id reused for a different grant")
        if body.phase == "result":
            if not prior:
                raise HTTPException(404, "operation not found")
            if not body.result:
                raise HTTPException(422, "result required")
            row = await audit.finish_operation(session, actor.user_id, prior.id, body.result, body.params)
        elif prior:
            if prior.params_json != params:
                raise HTTPException(409, "request_id reused for a different action")
            row = prior
        else:
            row = await audit.begin_operation(session, actor, "daemon_restart", request_id=body.request_id,
                                              target=grant.target, params=params, ip=client_ip(request))
        return operation_out(row, body.request_id)
