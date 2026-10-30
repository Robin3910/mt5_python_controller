"""成交明细 API（节点详情页概览的绩效数据出口）。

- `GET /api/deals/nodes/{node_id}/deals`：某节点的成交明细分页（含 summary）
- `GET /api/deals/nodes/{node_id}/performance`：某节点的绩效汇总 + 按日序列

数据源是 `mt5_deal` 表（节点上报的历史成交）。
**统计口径集中在 deal_stats 一处**（胜率按持仓级聚合，不是按成交笔数），
成交明细页与概览页读的是同一套实现，保证同一节点两处看到的胜率、净盈亏完全一致。

权限与数据可见性：
- 成交明细（价格 / 手数 / 备注 / 订单号）含具体价位，可能泄露策略参数，
  所以节点级接口用 `owned_node_or_404` 严格校验归属；
- 聚合绩效（收益 / 胜率 / 笔数）同样归属到节点，归属不通过一律 404，
  不暴露「这个节点是否存在」。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from . import deal_stats, deal_store
from .deps import get_store, owned_node_or_404, require_menu
from .models import NodePerformanceResponse, PaginatedDeals
from .permissions import MENU_NODES, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/deals", tags=["deals"])

# 概览页默认 7 天柱状图；统计窗口另有 7d/30d/90d/all 四档
DEFAULT_DAILY_DAYS = 7


# ---------------------------------------------------------------------------
# 节点级成交明细
# ---------------------------------------------------------------------------
@router.get("/nodes/{node_id}/deals", response_model=PaginatedDeals)
async def node_deals(
    node_id: str,
    page: int = 1,
    page_size: int = 50,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    entry_type: Optional[str] = None,
    source: Optional[str] = None,
    keyword: Optional[str] = None,
    magic: Optional[int] = None,
    from_time: Optional[int] = None,
    to_time: Optional[int] = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_NODES)),
):
    """某节点的成交明细分页。

    响应里带 summary（与 items 同一 WHERE 条件），前端直接展示，不要自己再算。
    """
    await owned_node_or_404(store, p, node_id)
    return await deal_store.list_deals(
        node_id=node_id,
        symbol=symbol,
        direction=direction,
        entry_type=entry_type,
        source=source,
        keyword=keyword,
        magic=magic,
        from_ts=from_time,
        to_ts=to_time,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 节点绩效（概览页）
# ---------------------------------------------------------------------------
@router.get("/nodes/{node_id}/performance", response_model=NodePerformanceResponse)
async def node_performance(
    node_id: str,
    window: str = "30d",
    days: int = DEFAULT_DAILY_DAYS,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_NODES)),
):
    """某节点的绩效汇总与按日收益序列（概览页数据源）。

    - summary：按窗口（7d/30d/90d/all）聚合的收益 / 胜率 / 笔数
    - daily：按日柱状图数据，**固定 days 天**（默认 7 天），
      不随统计窗口变化 —— 柱状图看的是近况，数字看的是区间收益，两者口径不同
      是有意为之（与 v4 的带单页一致）。

    恒等式（重要）：daily 的总和与 `days` 天内所有出场成交的净盈亏之和相等。
    daily 与 summary 用的是同一批成交（出场类 OUT / INOUT / OUT_BY）、
    同一份 net_profit（profit + swap + commission），只是统计区间不同。
    """
    await owned_node_or_404(store, p, node_id)

    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    from_ts = deal_stats.window_start(win)
    days = max(1, min(int(days or DEFAULT_DAILY_DAYS), 90))

    summary = await deal_stats.node_summary(node_id, from_ts, None)
    daily = await deal_stats.daily_net_profit([node_id], days=days)
    return {
        "node_id": node_id,
        "window": win,
        "summary": summary,
        "daily": daily,
    }
