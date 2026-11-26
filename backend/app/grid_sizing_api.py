"""网格试算 API（只读，需登录；普通用户只能以本人节点为行情源）。

模版3 配置助手的取数与编排：向目标节点索取合约规格与当前报价（market_probe），
再用 grid_sizing 的纯函数算出建议的网格数量与每格手数。本模块只做鉴权、参数收集
与编排，不含任何算法，也不改任何策略——建议值是否采纳由前端交给用户点「应用」。

节点是**行情源**而非绑定关系：策略绑的是分组，这里的 node_id 只用来取该终端上的
合约规格，不会落进策略配置。

试算**不依赖 K 线 / ATR**：只要品种规格（tick_size / tick_value / 手数步长）与
区间、止损、预算，就能算出格数与每格手数，因此这里不再请求任何周期与根数。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from . import grid_sizing, market_probe
from .deps import get_principal, get_store, owned_node_or_404
from .permissions import Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["grid"])

# 与趋势面板一致：品种代码在入口就挡掉异常输入，不透传给节点终端
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._#/-]{0,31}$")


def quote_price_gap(quote: dict | None) -> float:
    """当前点差 = ask - bid。探针不带买卖价（老节点 / 休市）时按 0 处理。"""
    src = quote if isinstance(quote, dict) else {}
    try:
        bid = float(src.get("bid") or 0.0)
        ask = float(src.get("ask") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    return ask - bid


def sizing_params(
    price_lower: float = Query(0.0, description="网格区间下限"),
    price_upper: float = Query(0.0, description="网格区间上限"),
    grid_mode: str = Query("arithmetic", description="arithmetic 等差 / geometric 等比"),
    grid_side: str = Query("long", description="long 只做多 / short 只做空"),
    stop_lower: float = Query(0.0, description="下沿终止价，多头的止损价"),
    stop_upper: float = Query(0.0, description="上沿终止价，空头的止损价"),
    max_loss: float = Query(0.0, description="最大可接受亏损（账户货币）"),
    close_on_stop: bool = Query(True, description="终止时是否清仓"),
    trailing_up: bool = Query(False, description="是否开启向上追踪"),
    total_lot_limit: float = Query(0.0, description="总手数上限，0=不限"),
) -> dict:
    """把查询串收成试算入参；范围夹取统一交给 grid_sizing.SizingInput。"""
    return {
        "price_lower": price_lower,
        "price_upper": price_upper,
        "grid_mode": grid_mode,
        "grid_side": grid_side,
        "stop_lower": stop_lower,
        "stop_upper": stop_upper,
        "max_loss": max_loss,
        "close_on_stop": close_on_stop,
        "trailing_up": trailing_up,
        "total_lot_limit": total_lot_limit,
    }


@router.get("/{node_id}/grid_sizing")
async def node_grid_sizing(
    node_id: str,
    symbol: str = Query(..., description="品种代码，如 XAUUSD"),
    params: dict = Depends(sizing_params),
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """按风险预算与套利优先口径试算网格数量与每格手数（只给建议，不写任何配置）。

    格数完全由预算决定：每格按最小手数算能开多少格就开多少格（上限 200），剩余预算
    再补到每格手数上。不取 K 线、不用 ATR。
    普通用户只能以本人节点为行情源。
    """
    await owned_node_or_404(store, p, node_id)

    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail="品种代码无效")

    try:
        # count=0：只要报价与合约规格，不要 K 线
        market = await market_probe.fetch(node_id, sym, "", 0)
    except market_probe.ProbeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    quote = market.get("quote") or {}
    result = grid_sizing.evaluate(
        market.get("spec"),
        params,
        price=market_probe.quote_price(quote),
    )
    return {
        "node_id": node_id,
        "symbol": sym,
        "quote": quote,
        "cached": bool(market.get("cached")),
        "fetched_at": float(market.get("fetched_at") or 0.0),
        "updated_at": time.time(),
        **result,
    }
