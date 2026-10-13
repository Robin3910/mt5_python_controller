"""把 SMC 价位推送写成每个任务一份 Redis 副本，并推给在跑的节点。

副本按任务整体覆写，不按 K 线周期合并。推送是后台动作：写 Redis 完成即可
回应 TradingView，单个节点发送超时不拖住入口。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import BackgroundTasks

from . import group_persist, group_rules
from .connections import manager
from .group_dispatcher import STRATEGY_UPDATE_SEND_TIMEOUT, build_strategy_smc_command
from .redis_store import RedisStore
from .smc_rules import SmcPayload, snapshot_uses_smc_tf

logger = logging.getLogger(__name__)


def _match_subtasks(
    subs: list[dict], payload: SmcPayload, group_ids: list[str] | None = None,
) -> list[dict]:
    """品种一致、且快照正在用该周期 SMC 档位的未收口子任务。

    group_ids 非空时再按分组收窄。Webhook 不传，手动触发用来避免改到没点名的分组。
    """
    allowed = {str(gid) for gid in group_ids} if group_ids else None
    matched: list[dict] = []
    for sub in subs:
        if allowed is not None and str(sub.get("group_id") or "") not in allowed:
            continue
        symbol = sub.get("symbol")
        if not group_rules.symbol_match(symbol, payload.symbol):
            continue
        if not snapshot_uses_smc_tf(sub.get("strategy_snapshot"), payload.tf):
            continue
        matched.append(sub)
    return matched


async def ingest(
    store: RedisStore,
    payload: SmcPayload,
    background_tasks: BackgroundTasks,
    group_ids: list[str] | None = None,
) -> dict:
    """为每个命中的任务覆写副本，并安排推送。没有命中的任务不写 Redis。"""
    subs = await group_persist.active_subtasks_with_snapshot()
    matched = _match_subtasks(subs, payload, group_ids)
    body = payload.to_store()
    task_ids = []
    for sub in matched:
        task_id = sub.get("task_id")
        if task_id in task_ids:
            continue
        task_ids.append(task_id)
        await store.set_task_smc(int(task_id), body)
    node_ids = []
    for sub in matched:
        node_id = str(sub.get("node_id") or "")
        if node_id and node_id not in node_ids:
            node_ids.append(node_id)
    if matched:
        background_tasks.add_task(push_smc, matched, body)
    logger.info(
        "smc-data %s %s levels=%s dropped=%s tasks=%s nodes=%s",
        payload.symbol, payload.tf, len(payload.levels), payload.dropped,
        len(task_ids), len(node_ids),
    )
    return {
        "status": "accepted",
        "type": "smc-data",
        "symbol": payload.symbol,
        "tf": payload.tf,
        "levels": len(payload.levels),
        "dropped": payload.dropped,
        "tasks": len(task_ids),
        "nodes": len(node_ids),
    }


async def push_smc(subtasks: list[dict], smc: dict) -> int:
    """把同一份副本发给命中子任务所在的节点。离线节点只靠 Redis，重连 resume 再带上。"""
    sent = 0
    for sub in subtasks or []:
        node_id = str(sub.get("node_id") or "")
        if not node_id:
            continue
        try:
            task_id = int(sub.get("task_id"))
            dispatch_id = int(sub.get("dispatch_id"))
        except (TypeError, ValueError):
            continue
        cmd = build_strategy_smc_command(
            task_id=task_id,
            dispatch_id=dispatch_id,
            magic=sub.get("magic"),
            smc=smc,
        )
        try:
            ok = await asyncio.wait_for(
                manager.send_to_node(node_id, cmd),
                timeout=STRATEGY_UPDATE_SEND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "strategy_smc send timed out: node %s task %s", node_id, task_id,
            )
            continue
        if ok:
            sent += 1
        else:
            logger.info(
                "strategy_smc queued-by-redis only: node %s task %s offline",
                node_id, task_id,
            )
    return sent
