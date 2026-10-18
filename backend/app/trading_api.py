"""交易页一键触发 API。

复用 /webhook 的解析与分发流程，针对主节点触发三种开仓策略（市价/限价/网格）。
首次触发按 (strategy_type, username, mt5_login) 自动创建专属分组+策略并把主
节点加入成员；后续复用。每次只触发一笔信号，不修改任何已有分发链路。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from . import (
    group_service,
    permissions,
    persist,
    strategy_service,
    webhook,
)
from .connections import manager
from .deps import (
    client_ip,
    get_dispatcher,
    get_group_dispatcher,
    get_principal,
    get_store,
    require_menu,
)
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .models import (
    GroupCreate,
    GroupUpdate,
    QuickTriggerRequest,
    QuickTriggerResult,
    StrategyCreate,
    StrategyUpdate,
    StrategyRule,
)
from .permissions import MENU_TRADING, Principal
from .redis_store import RedisStore
from .strategy_templates import (
    TEMPLATE_2_ID,
    TEMPLATE_3_ID,
    default_grid_rule,
    default_risk_sized_rule,
)
from .webhook import process_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])

# 主节点不在线 / 不存在 / 非主 / 跨 owner —— 归一错误信息
_PRIMARY_NOT_FOUND = "主节点不存在或未启用"
_PRIMARY_OFFLINE = "主节点不在线"
_PRIMARY_NOT_OWNED = "主节点不属于当前用户"


def _group_name(strategy_type: str, username: str, login: int, symbol: str) -> str:
    """专属分组/策略名：(策略类型-用户名-MT5 登录号-品种)。

    symbol 纳入命名 key 后，同一主节点在不同品种下会生成独立的 group+strategy，
    避免品种被静默覆盖。
    """
    return f"{strategy_type}-{username}-{int(login)}-{symbol}"


async def _find_group_by_name(
    store: RedisStore, name: str, *, owner_user_id: Optional[int],
) -> Optional[dict]:
    """在 Redis 缓存里按名称找分组（按 owner 隔离）。"""
    for g in await store.all_groups():
        if g.get("name") != name:
            continue
        if permissions.same_owner(g.get("owner_user_id"), owner_user_id):
            return g
    return None


async def _find_strategy_by_name(
    store: RedisStore, name: str, *, owner_user_id: Optional[int],
) -> Optional[dict]:
    """在 Redis 缓存里按名称找策略（按 owner 隔离）。"""
    for s in await store.all_strategies():
        if s.get("name") != name:
            continue
        if permissions.same_owner(s.get("owner_user_id"), owner_user_id):
            return s
    return None


def _build_default_rules(strategy_type: str) -> tuple[str, list[dict]]:
    """返回 (template_id, rules)，复用模版默认参数。"""
    if strategy_type == "trend_market":
        rule = deepcopy(default_risk_sized_rule().to_dict())
        rule["entry_mode"] = "market"
        return TEMPLATE_2_ID, [rule]
    if strategy_type == "trend_limit":
        rule = deepcopy(default_risk_sized_rule().to_dict())
        rule["entry_mode"] = "limit"
        return TEMPLATE_2_ID, [rule]
    if strategy_type == "grid":
        rule = deepcopy(default_grid_rule().to_dict())
        return TEMPLATE_3_ID, [rule]
    raise ValueError(f"未知策略类型：{strategy_type}")


def _infer_trend_action(
    strategy_type: str,
    *,
    entry_price: float,
    stop_loss: float,
    current_price: float,
) -> str:
    """趋势策略方向自动判断：BUY/SELL。"""
    ref = current_price if strategy_type == "trend_market" else entry_price
    if ref <= 0 or stop_loss <= 0:
        raise ValueError("止损价必须大于 0")
    if ref > stop_loss:
        return "BUY"
    if ref < stop_loss:
        return "SELL"
    raise ValueError("入场价与止损价相同，无法判断方向")


def _quote_mid(account: dict, symbol: str) -> float:
    """从节点账户快照取品种的中间价；缺失则 0。"""
    quotes = (account or {}).get("quotes") or {}
    q = quotes.get(symbol) or {}
    mid = q.get("mid")
    if mid is not None and mid > 0:
        return float(mid)
    prices = (account or {}).get("prices") or {}
    p = prices.get(symbol)
    if p is not None and p > 0:
        return float(p)
    return 0.0


async def _ensure_member(store: RedisStore, group: dict, node_id: str) -> None:
    """保证主节点是分组成员；缺则补。"""
    members = group.get("members") or []
    if any(m.get("node_id") == node_id for m in members):
        return
    new_ids = [m.get("node_id") for m in members if m.get("node_id")] + [node_id]
    await group_service.update_group(
        store, group["group_id"],
        GroupUpdate(node_ids=new_ids),
    )


@router.post("/quick-trigger", response_model=QuickTriggerResult)
async def quick_trigger(
    body: QuickTriggerRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_TRADING)),
):
    """交易页一键触发：找/创建专属策略+分组，调 webhook.process_signal 走 strategy 链路。"""
    # 1. 主节点校验
    node = await store.get_node(body.node_id)
    if not node or not node.get("is_primary") or not node.get("enabled", True):
        raise HTTPException(status_code=400, detail=_PRIMARY_NOT_FOUND)
    if not permissions.owns(p, node.get("owner_user_id")):
        raise HTTPException(status_code=403, detail=_PRIMARY_NOT_OWNED)
    if not manager.is_node_online(body.node_id):
        raise HTTPException(status_code=400, detail=_PRIMARY_OFFLINE)

    owner = permissions.normalize_owner(node.get("owner_user_id"))
    login = int(node.get("mt5_login") or 0)
    if login <= 0:
        raise HTTPException(status_code=400, detail="主节点未绑定 MT5 登录号")

    # 2. 限价模式必填入场价
    if body.strategy_type == "trend_limit" and not (body.entry_price and body.entry_price > 0):
        raise HTTPException(status_code=400, detail="限价开仓必须填写入场价")

    # 3. 网格需要方向
    grid_direction: Optional[str] = None
    if body.strategy_type == "grid":
        d = (body.direction or "all").lower()
        if d not in ("all", "buy", "sell"):
            raise HTTPException(status_code=400, detail="网格方向必须是 all/buy/sell")
        grid_direction = d

    name = _group_name(body.strategy_type, p.username, login, body.symbol)

    # 4. 找/建策略（按 (type, user, login, symbol) 隔离，不同品种各自独立）
    strategy = await _find_strategy_by_name(store, name, owner_user_id=owner)
    if not strategy:
        template_id, rules = _build_default_rules(body.strategy_type)
        # 网格首次创建时把方向写进 rule.action；其他模式不动
        if body.strategy_type == "grid" and grid_direction and rules:
            rules[0]["action"] = grid_direction if grid_direction != "all" else "all"
        try:
            strategy = await strategy_service.create_strategy(
                store,
                StrategyCreate(
                    name=name,
                    template_id=template_id,
                    symbol=body.symbol,
                    enabled=True,
                    remark=f"交易页自动创建（{body.strategy_type}）",
                    rules=[StrategyRule(**r) for r in rules],
                ),
                owner_user_id=owner,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"创建策略失败：{e}")

    # 5. 找/建分组
    group = await _find_group_by_name(store, name, owner_user_id=owner)
    if not group:
        try:
            group = await group_service.create_group(
                store,
                GroupCreate(
                    name=name,
                    enabled=True,
                    dispatch_mode="sync",
                    node_ids=[body.node_id],
                    strategy_id=strategy["strategy_id"],
                    remark=f"交易页自动创建（{body.strategy_type}）",
                ),
                owner_user_id=owner,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"创建分组失败：{e}")
    else:
        if group.get("strategy_id") != strategy["strategy_id"]:
            try:
                updated = await group_service.update_group(
                    store, group["group_id"],
                    GroupUpdate(strategy_id=strategy["strategy_id"]),
                )
                if updated:
                    group = updated
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"绑定策略失败：{e}")
        await _ensure_member(store, group, body.node_id)

    # 6. 构造信号
    if body.strategy_type in ("trend_market", "trend_limit"):
        account = await store.get_account(body.node_id) or {}
        current_price = _quote_mid(account, body.symbol)
        if body.strategy_type == "trend_market" and current_price <= 0:
            raise HTTPException(status_code=400, detail="无法获取主节点当前报价，请稍后再试")
        if not body.sl or body.sl <= 0:
            raise HTTPException(status_code=400, detail="趋势策略必须填写止损价")
        entry_ref = body.entry_price or 0.0
        if body.strategy_type == "trend_market":
            entry_ref = current_price
        try:
            action = _infer_trend_action(
                body.strategy_type,
                entry_price=entry_ref,
                stop_loss=float(body.sl),
                current_price=current_price,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        action = (body.direction or "all").lower()
        if action not in ("all", "buy", "sell"):
            action = "all"

    data: dict = {
        "action": action.upper() if action != "all" else "BUY",
        "model": "strategy",
        "group_ids": [group["group_id"]],
        "symbol": body.symbol,
        "volume": body.volume,
    }
    if body.sl and body.sl > 0:
        data["sl"] = body.sl
    if body.tp and body.tp > 0:
        data["tp"] = body.tp
    if body.strategy_type == "trend_limit" and body.entry_price and body.entry_price > 0:
        data["limit_price"] = body.entry_price
    if body.remark:
        data["comment"] = body.remark

    # 7. 走 webhook.process_signal
    ip = client_ip(request)
    result = await process_signal(
        data, source_ip=ip, source="manual",
        store=store, dispatcher=dispatcher, group_dispatcher=group_dispatcher,
    )
    await persist.audit(
        p.username, "quick_trigger", body.node_id,
        {
            "strategy_type": body.strategy_type,
            "symbol": body.symbol,
            "group_id": group["group_id"],
            "strategy_id": strategy["strategy_id"],
            "status": result.get("status"),
        },
        result.get("status", "ok"), ip,
        category="console", before=None, after=None,
    )
    return QuickTriggerResult(
        group_id=group["group_id"],
        strategy_id=strategy["strategy_id"],
        result=result,
    )
