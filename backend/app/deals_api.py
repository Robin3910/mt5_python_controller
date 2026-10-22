"""成交明细 API（订单历史 / 带单绩效的数据出口）。

- `GET /api/deals/nodes/{node_id}/deals`：某节点的成交明细分页（带 summary）
- `GET /api/deals/follow/leaders/{leader_user_id}/performance`：带单员绩效

数据源全部是 `mt5_deal` 表（节点上报的历史成交）。**统计口径集中在
deal_stats 一处**（胜率按持仓级聚合，不是按成交笔数），订单历史页与带单页
读的是同一套实现，保证同一节点两处看到的胜率、净盈亏完全一致。

权限与数据可见性（设计文档要求，务必遵守）：
- 聚合绩效（收益 / 胜率 / 笔数）：所有能进【带单】菜单的人可见 —— 带单广场
  是公开的，绩效就是给潜在跟单者看的；
- 成交明细（价位 / 手数 / 备注 / 订单号）：**仅本人 + 管理员**。含具体价位，
  可能泄露策略参数，所以节点级明细接口走 owned_node_or_404 严格校验归属。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from . import deal_stats, deal_store, follow_rules, follow_service, user_service
from .deps import get_store, owned_node_or_404, require_menu
from .models import DealSummary, PaginatedDeals
from .permissions import MENU_FOLLOW, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/deals", tags=["deals"])


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
async def get_principal_deals(
    p: Principal = Depends(require_menu("nodes", "trading")),
) -> Principal:
    """节点成交明细的访问身份。

    订单历史出现在【交易】与【节点】两个页面，因此要求拥有其中任一菜单；
    数据归属仍由 owned_node_or_404 兜住。
    """
    return p


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
    p: Principal = Depends(get_principal_deals),
):
    """某节点的成交明细分页（订单历史 Tab 的数据源）。

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
# 带单员绩效
# ---------------------------------------------------------------------------
@router.get("/follow/leaders/{leader_user_id}/performance")
async def leader_performance(
    leader_user_id: int,
    window: str = "30d",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """带单员绩效（累计收益 / 收益率 / 胜率 / 笔数 / 跟单人数）。

    口径：
    - 绩效 = 该带单员**主节点**的成交聚合（与订单历史页同一套实现）；
      主节点就是带单源，跟单员收到的是它的信号，所以广场上给他看的绩效
      也必须来自同一个账户 —— 混进名下其它节点会让选单依据失真；
    - 跟单人数 = 跟了他（任一分组）的**去重节点数**（跟单关系表，非订单数据）。

    跟单员看的是「带单员赚了多少」—— 这正是选单的依据，所以聚合绩效公开可见。
    """
    user = await user_service.get_user_by_id(leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="带单员不存在或已停用")

    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    all_nodes = await store.all_nodes()
    primary = follow_rules.primary_node_of(all_nodes, leader_user_id)

    # 没有主节点就没有带单源，也就无从谈起带单绩效 —— 返回空统计（前端显示
    # 「暂无数据」），而不是 0，与 deal_stats._empty_summary 的语义一致。
    performance: dict
    if primary:
        performance = await deal_stats.leader_batch(
            [str(primary.get("node_id"))], deal_stats.window_start(win), None,
        )
    else:
        performance = DealSummary().model_dump()

    # 跟单人数：跟了该带单员任一分组的去重节点数
    followers = await follow_service.list_for_leaders({leader_user_id})
    follower_count = len({str(f.get("node_id")) for f in followers if f.get("node_id")})

    return {
        "leader_user_id": leader_user_id,
        "window": win,
        "performance": performance,
        "follower_count": follower_count,
    }
