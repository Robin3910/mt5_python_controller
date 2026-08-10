"""趋势面板 API（只读观测，需管理员鉴权）。

按品种 / 周期向目标节点索取 K 线与报价（market_probe），再用 trend_indicators 的
纯函数算出 EMA、RSI 与综合趋势得分。本模块只做鉴权、参数合并与编排，不含任何
指标算法，也不参与下单与过滤。

参数优先级：查询串 > 全局已保存配置（system_setting.trend_config）> 内置默认。
面板上可以边调边看，确认合适后再走 PUT /api/config/trend 落成全后台共用配置。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from . import market_probe, system_settings, trend_indicators
from .deps import get_current_admin, get_store
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["trend"])

# 品种代码：字母数字加券商常见的后缀连接符。入口就挡掉异常输入，不透传给节点终端
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._#/-]{0,31}$")


def trend_overrides(
    timeframe: str | None = None,
    ema_period: int | None = None,
    rsi_period: int | None = None,
    ema_weight: float | None = None,
    rsi_weight: float | None = None,
    ema_full_scale_pct: float | None = None,
    rsi_bull: float | None = None,
    rsi_bear: float | None = None,
    rsi_overbought: float | None = None,
    rsi_oversold: float | None = None,
    bullish: float | None = None,
    bearish: float | None = None,
    bars: int | None = None,
) -> dict:
    """把查询串里显式给出的参数收成覆盖项。

    未给出的键不放进结果，交由节点已保存的配置兜底；范围校验与关系修正统一由
    trend_indicators.normalize_config 负责。
    """
    given = {
        "timeframe": timeframe,
        "ema_period": ema_period,
        "rsi_period": rsi_period,
        "ema_weight": ema_weight,
        "rsi_weight": rsi_weight,
        "ema_full_scale_pct": ema_full_scale_pct,
        "rsi_bull": rsi_bull,
        "rsi_bear": rsi_bear,
        "rsi_overbought": rsi_overbought,
        "rsi_oversold": rsi_oversold,
        "bullish": bullish,
        "bearish": bearish,
        "bars": bars,
    }
    return {k: v for k, v in given.items() if v is not None}


@router.get("/{node_id}/trend")
async def node_trend(
    node_id: str,
    symbol: str = Query(..., description="品种代码，如 XAUUSD"),
    overrides: dict = Depends(trend_overrides),
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """某节点某品种的实时趋势快照（K 线 + EMA/RSI + 综合得分）。

    权重与阈值只影响得分换算，K 线取数与之无关，因此面板上调参数时通常直接命中
    行情缓存、不会额外惊动节点终端。
    """
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail="品种代码无效")

    saved = await system_settings.get_trend_config()
    cfg = trend_indicators.normalize_config({**saved, **overrides})

    try:
        market = await market_probe.fetch(
            node_id, sym, str(cfg["timeframe"]), trend_indicators.bars_needed(cfg),
        )
    except market_probe.ProbeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    quote = market.get("quote") or {}
    result = trend_indicators.evaluate(
        market.get("bars") or [], market_probe.quote_price(quote), cfg,
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
