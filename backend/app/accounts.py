"""账户 / 持仓查询 API（普通用户只看本人节点）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from . import group_persist, permissions
from .connections import manager
from .deps import get_principal, get_store, owned_node_or_404
from .permissions import Principal
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["accounts"])


async def _annotate_strategy_template(node_id: str, acct: dict) -> dict:
    """给快照里的持仓 / 挂单补 `ai_template`（所属子任务的策略模版 ID）。

    节点只上报裸的 MT5 订单，不知道它属于哪条策略；按魔术号回子任务快照反查，
    前端据此把「AI 智能加仓（tpl_1）」的仓位从普通交易列表里摘出去。
    反查失败不报错：标不上模版，前端按普通持仓展示。
    """
    if not isinstance(acct, dict):
        return acct
    magics: set[int] = set()
    for key in ("positions", "orders"):
        for row in acct.get(key) or []:
            if not isinstance(row, dict):
                continue
            try:
                magic = int(row.get("magic") or 0)
            except (TypeError, ValueError):
                continue
            if magic:
                magics.add(magic)
    if not magics:
        return acct
    try:
        templates = await group_persist.templates_by_magic(node_id, magics)
    except Exception:  # noqa: BLE001
        logger.warning("annotate strategy template failed for node %s", node_id, exc_info=True)
        return acct
    if not templates:
        return acct
    for key in ("positions", "orders"):
        rows = acct.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                tpl = templates.get(int(row.get("magic") or 0))
            except (TypeError, ValueError):
                continue
            if tpl:
                row["ai_template"] = tpl
    return acct


@router.get("/accounts")
async def all_accounts(
    store: RedisStore = Depends(get_store), p: Principal = Depends(get_principal),
):
    """汇总可见节点的账户快照（含在线状态）。"""
    out = []
    for n in permissions.visible(p, await store.all_nodes_with_db_fallback()):
        acct = await store.get_account(n["node_id"]) or {}
        await _annotate_strategy_template(n["node_id"], acct)
        out.append(
            {
                "node_id": n["node_id"],
                "name": n.get("name"),
                "status": "online" if manager.is_node_online(n["node_id"]) else "offline",
                "account": acct,
            }
        )
    return out


@router.get("/nodes/{node_id}/account")
async def node_account(
    node_id: str, store: RedisStore = Depends(get_store), p: Principal = Depends(get_principal),
):
    """查询单个节点的账户快照；尚无上报时返回 404。"""
    await owned_node_or_404(store, p, node_id)
    acct = await store.get_account(node_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="no account snapshot yet")
    return await _annotate_strategy_template(node_id, acct)
