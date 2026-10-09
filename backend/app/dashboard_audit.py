"""面板操作的严格、幂等审计。调用方与业务写入共用事务，不吞落库错误。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from .orm import AuditLog

PARAM_KEYS = frozenset({
    "enabled", "version", "from_version", "to_version", "instance_id", "name",
    "field_names", "fields_changed", "error_code", "count", "mode", "source",
    "auto_start", "mt5_login", "generation", "expected_generation", "node_id",
    "grant_operation_id",
})


def clean_params(params: dict | None) -> dict:
    """只记录用途明确的字段；不接收令牌、原始异常、完整配置及嵌套载荷。"""
    out = {}
    for key, value in (params or {}).items():
        if key not in PARAM_KEYS:
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:128]
        elif key in {"field_names", "fields_changed"} and isinstance(value, list):
            out[key] = [str(v)[:64] for v in value[:64] if isinstance(v, str)]
    return out


def request_key(actor_id: int, request_id: str) -> str:
    return f"dashboard:{actor_id}:{request_id}"


async def prior_operation(session, actor_id: int, request_id: str):
    return await session.scalar(select(AuditLog).where(AuditLog.request_key == request_key(actor_id, request_id)))


async def begin_operation(session, principal, action: str, *, request_id: str | None = None,
                          target: str | None = None, params: dict | None = None,
                          ip: str | None = None, result: str = "pending") -> AuditLog:
    row = AuditLog(
        operator=principal.username, actor_user_id=principal.user_id,
        action=f"dashboard_{action}", target=target, params_json=clean_params(params),
        category="node", result=result, ip=ip,
        request_key=request_key(principal.user_id, request_id) if request_id else None,
    )
    session.add(row)
    await session.flush()
    return row


async def finish_operation(session, actor_id: int, operation_id: int, result: str, params: dict | None):
    row = await session.scalar(select(AuditLog).where(AuditLog.id == operation_id).with_for_update())
    if not row or row.actor_user_id != actor_id or not row.action.startswith("dashboard_"):
        raise HTTPException(404, "operation not found")
    cleaned = clean_params(params)
    if row.result != "pending":
        if row.result == result and (row.after_json or {}) == cleaned:
            return row
        raise HTTPException(409, "operation already completed")
    row.result = result
    row.after_json = cleaned
    await session.flush()
    return row
