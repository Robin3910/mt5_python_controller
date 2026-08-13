"""网格试算 API（只读，需管理员鉴权）。

模版3 配置助手的取数与编排：向目标节点索取 K 线与合约规格（market_probe），再用
grid_sizing 的纯函数算出建议的网格数量与每格手数。本模块只做鉴权、参数收集与编排，
不含任何算法，也不改任何策略——建议值是否采纳由前端交给用户点「应用」。

节点是**行情源**而非绑定关系：策略绑的是分组，这里的 node_id 只用来取该终端上的
K 线与品种规格，不会落进策略配置。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from . import grid_sizing, market_probe
from .deps import get_current_admin, get_store
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["grid"])

# 与趋势面板一致：品种代码在入口就挡掉异常输入，不透传给节点终端
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._#/-]{0,31}$")


def sizing_params(
    price_lower: float = Query(0.0, description="网格区间下限"),
    price_upper: float = Query(0.0, description="网格区间上限"),
    grid_mode: str = Query("arithmetic", description="arithmetic 等差 / geometric 等比"),
    grid_side: str = Query("long", description="long 只做多 / short 只做空"),
    stop_lower: float = Query(0.0, description="下沿终止价，多头的止损价"),
    stop_upper: float = Query(0.0, description="上沿终止价，空头的止损价"),
    atr_mult: float = Query(1.0, description="格距 = ATR × 该倍数"),
    spacing: float = Query(0.0, description="手改格距，>0 时不再用 ATR×倍数"),
    max_loss: float = Query(0.0, description="最大可接受亏损（账户货币）"),
    prefill_enabled: bool = Query(True, description="是否初始建仓，影响最坏成本"),
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
        "atr_mult": atr_mult,
        "spacing": spacing,
        "max_loss": max_loss,
        "prefill_enabled": prefill_enabled,
        "close_on_stop": close_on_stop,
        "trailing_up": trailing_up,
        "total_lot_limit": total_lot_limit,
    }


@router.get("/{node_id}/grid_sizing")
async def node_grid_sizing(
    node_id: str,
    symbol: str = Query(..., description="品种代码，如 XAUUSD"),
    timeframe: str = Query(grid_sizing.DEFAULT_TIMEFRAME, description="算 ATR 的 K 线周期"),
    params: dict = Depends(sizing_params),
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """按 ATR 与风险预算试算网格数量与每格手数（只给建议，不写任何配置）。

    ATR 只跟品种与周期有关，倍数、亏损预算这些参数改了通常直接命中行情缓存，
    不会额外惊动节点终端。
    """
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail="品种代码无效")
    tf = grid_sizing.normalize_timeframe(timeframe)

    try:
        market = await market_probe.fetch(node_id, sym, tf, grid_sizing.bars_needed())
    except market_probe.ProbeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    quote = market.get("quote") or {}
    result = grid_sizing.evaluate(
        market.get("bars") or [],
        market.get("spec"),
        params,
        price=market_probe.quote_price(quote),
    )
    return {
        "node_id": node_id,
        "symbol": sym,
        "timeframe": tf,
        "quote": quote,
        "cached": bool(market.get("cached")),
        "fetched_at": float(market.get("fetched_at") or 0.0),
        "updated_at": time.time(),
        **result,
    }
