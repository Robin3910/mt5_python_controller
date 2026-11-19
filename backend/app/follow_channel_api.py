"""首页节点主页：频道持仓读取，以及按频道平仓。

平仓只作用于该节点、该频道（AI 交易或带单）的魔术号。
跟单员的仓挂在自己的节点上，这里按节点归属鉴权，不要求调用方拥有带单分组。
账户级全平 / 全量删挂单仍走原有 close 与 cancel-pending。
"""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import follow_channel_store, persist
from .connections import manager
from .deps import client_ip, get_group_dispatcher, get_store, owned_node_or_404, require_menu
from .follow_channels import CHANNEL_AI, CHANNEL_FOLLOW, select_channel_positions
from .group_dispatcher import GroupDispatcher
from .models import (
    FollowChannelCloseRequest,
    FollowChannelCloseResult,
    FollowChannelsOut,
    build_close_command,
)
from .permissions import MENU_AI_TRADE, MENU_HOME, MENU_NODES, Principal, can_menu
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["follow-channels"])

_MARKETS = {CHANNEL_AI, CHANNEL_FOLLOW}
_MODES = {"all", "profit", "ticket"}
_STOP_REASON = "manual_close_channel"


def _cmd_id() -> str:
    return "fcc_" + format(int(time.time() * 1000), "x") + secrets.token_hex(2)


def _live_magics(account: dict) -> set[int]:
    found: set[int] = set()
    for key in ("positions", "orders"):
        for raw in account.get(key) or []:
            if not isinstance(raw, dict):
                continue
            try:
                magic = int(raw.get("magic") or 0)
            except (TypeError, ValueError):
                continue
            if magic:
                found.add(magic)
    return found


def _empty_strategies() -> dict:
    return {"stopped": 0, "forced": 0, "total": 0}


@router.get("/{node_id}/follow-channels", response_model=FollowChannelsOut)
async def get_follow_channels(
    node_id: str,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_HOME, MENU_NODES, MENU_AI_TRADE)),
):
    """节点主页的频道映射与历史持仓。实时持仓仍以账户快照为准，这里只负责归类。"""
    await owned_node_or_404(store, p, node_id)
    account = await store.get_account(node_id) or {}
    data = await follow_channel_store.load_follow_channels(node_id, _live_magics(account))
    return data


@router.post("/{node_id}/follow-channels/close", response_model=FollowChannelCloseResult)
async def close_follow_channel(
    node_id: str,
    body: FollowChannelCloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_HOME, MENU_NODES, MENU_AI_TRADE)),
):
    """平掉该节点某一个频道的持仓。

    all：先停这些魔术号上仍在跑的子任务（按魔术号平仓），再把没有在跑任务的残仓按订单平掉。
    profit / ticket：只平指定持仓，不停策略。
    """
    await owned_node_or_404(store, p, node_id)
    market = (body.market or "").strip().lower()
    if market != CHANNEL_AI and not can_menu(p, MENU_HOME, MENU_NODES):
        raise HTTPException(status_code=403, detail="无权操作该频道")
    mode = (body.mode or "all").strip().lower()
    if market not in _MARKETS:
        raise HTTPException(status_code=400, detail="未知频道")
    if mode not in _MODES:
        raise HTTPException(status_code=400, detail="未知平仓方式")
    if mode == "ticket" and not body.ticket:
        raise HTTPException(status_code=400, detail="缺少订单号")
    if node_id not in manager.nodes:
        raise HTTPException(status_code=409, detail="node offline")

    account = await store.get_account(node_id) or {}
    loaded = await follow_channel_store.load_follow_channels(node_id, _live_magics(account))
    rows = [item for item in loaded["magics"] if item["market"] == market]
    magic_set = {int(item["magic"]) for item in rows}
    targets = select_channel_positions(
        account.get("positions") or [],
        magic_set,
        mode=mode,
        ticket=body.ticket,
    )
    if not targets:
        detail = "该频道没有盈利持仓" if mode == "profit" else "该频道没有可平的持仓"
        raise HTTPException(status_code=400, detail=detail)

    strategies = _empty_strategies()
    covered: set[int] = set()
    if mode == "all":
        target_magics = set()
        for pos in targets:
            try:
                target_magics.add(int(pos.get("magic") or 0))
            except (TypeError, ValueError):
                continue
        active = [
            int(item["magic"])
            for item in rows
            if item.get("active") and int(item["magic"]) in target_magics
        ]
        if active:
            strategies = await group_dispatcher.stop_magics_on_node(
                node_id,
                active,
                signal_id=_cmd_id(),
                reason=_STOP_REASON,
            )
            covered = set(active)

    failed: list[int] = []
    sent = 0
    for pos in targets:
        try:
            magic = int(pos.get("magic") or 0)
            ticket = int(pos.get("ticket") or 0)
        except (TypeError, ValueError):
            continue
        if magic in covered or not ticket:
            continue
        signal_id = _cmd_id()
        await persist.record_manual_close(signal_id, node_id, "ticket", None, ticket)
        ok = await manager.send_to_node(
            node_id, build_close_command(signal_id, "ticket", None, ticket),
        )
        if ok:
            sent += 1
        else:
            failed.append(ticket)

    closed = len(targets) - len(failed)
    status = "partial" if failed else "ok"
    await persist.audit(
        p.username,
        "close_follow_channel",
        node_id,
        {"market": market, "mode": mode, "ticket": body.ticket},
        status,
        client_ip(request),
        category="node",
        before=None,
        after={
            "market": market,
            "mode": mode,
            "closed": closed,
            "ticket_sent": sent,
            "failed": failed,
            "strategies": strategies,
        },
    )
    if failed and sent == 0 and not strategies.get("total"):
        raise HTTPException(status_code=409, detail="node offline")
    return {
        "status": status,
        "node_id": node_id,
        "market": market,
        "mode": mode,
        "closed": closed,
        "failed": failed,
        "strategies": strategies,
    }
