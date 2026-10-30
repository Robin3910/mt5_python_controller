"""成交统计口径的**唯一实现**。

改动前请先读：
- 「节点概览绩效」= 「成交明细页的 summary」= `node_summary(node_id)`，
  **同一套函数**，不是两套实现。两处各写一份必然漂移，届时会出现
  「同一节点在成交明细页与概览页胜率不一致」。
- **胜率按 position_id 聚合**（持仓级），不是按成交笔数。
  一笔持仓可能分多次出场（分批止盈 + 移动止损），按成交计数会把一笔持仓算成多笔，
  胜率失真。这是本模块最容易写错的地方。

窗口定义（页面可切换，四档）：
    7d / 30d（默认）/ 90d / all
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import case, func, select

from .db import SessionLocal
from .orm import Deal

logger = logging.getLogger(__name__)

# 出场类 entry：只有这些计入「已实现盈亏」与胜率分母
OUT_ENTRIES = ("OUT", "INOUT", "OUT_BY")


def _out_profit():
    """出场类成交的 net_profit（其它 entry 记 0）。

    用 `case` 而非 `func.iif`：iif 是 SQLite 方言函数，生产 MySQL 上会直接报错。
    """
    return case(
        (Deal.entry_type.in_(OUT_ENTRIES), Deal.net_profit),
        else_=0.0,
    )

# 可选统计窗口（天）。all = 不限时间。
WINDOWS: dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}
DEFAULT_WINDOW = "30d"


def window_start(window: str) -> Optional[float]:
    """窗口起点（Unix 秒）；all 返回 None（不限时间）。"""
    days = WINDOWS.get(str(window or "").strip().lower())
    if days is None:
        # 非法窗口按默认档处理，避免「传错参数 = 全量查询」的意外
        days = WINDOWS[DEFAULT_WINDOW]
    if days is None:
        return None
    return time.time() - days * 86400


def _empty_summary() -> dict:
    """空统计的占位。data_available=False 表示「节点没数据」，与「有数据但全亏」区分。"""
    return {
        "total_profit": 0.0,
        "profit_rate": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "win_trades": 0,
        "lose_trades": 0,
        "gross_volume": 0.0,
        "commission_total": 0.0,
        "swap_total": 0.0,
        "base_equity": 0.0,
        "profit_rate_valid": True,
        "data_available": False,
        "updated_at": None,
    }


def _base_conds(
    node_ids: list[str],
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    symbol: Optional[str] = None,
) -> list:
    """构造查询条件。node_ids 为空列表时调用方应直接返回空统计。"""
    conds = [Deal.node_id.in_(node_ids)]
    if from_ts is not None:
        conds.append(Deal.deal_time >= int(from_ts))
    if to_ts is not None:
        conds.append(Deal.deal_time <= int(to_ts))
    if symbol:
        conds.append(Deal.symbol == symbol.upper())
    return conds


async def node_summary(
    node_id: str,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    *,
    symbol: Optional[str] = None,
) -> dict:
    """单个节点的成交汇总（带单页与订单历史页共用这套口径）。"""
    if not node_id:
        return _empty_summary()
    return await leader_batch([node_id], from_ts, to_ts, symbol=symbol)


async def leader_batch(
    node_ids: list[str],
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    *,
    symbol: Optional[str] = None,
) -> dict:
    """若干节点的合并汇总（节点详情页的绩效统计）。

    入参是 node_ids 列表是为了让调用方能一次查多个节点，避免逐节点查库的 N+1；
    本分支只用单节点，传单个 node_id 即可。

    一次查询算完，避免逐节点查库的 N+1。
    """
    ids = [str(i) for i in (node_ids or []) if i]
    if not ids:
        return _empty_summary()

    conds = _base_conds(ids, from_ts, to_ts, symbol)
    try:
        async with SessionLocal() as s:
            # —— 持仓级聚合（胜率的分子与分母）——
            # 出场盈亏按 position_id 汇总；同一持仓分多次出场只算一笔。
            pos_rows = (
                await s.execute(
                    select(
                        Deal.position_id,
                        func.sum(_out_profit()),
                    )
                    .where(*conds, Deal.position_id.is_not(None), Deal.position_id > 0)
                    .group_by(Deal.position_id)
                )
            ).all()
            # —— 汇总量 ——
            totals = (
                await s.execute(
                    select(
                        func.sum(_out_profit()),
                        func.sum(Deal.volume),
                        func.sum(Deal.commission),
                        func.sum(Deal.swap),
                        func.count(Deal.id),
                        func.max(Deal.equity),
                        func.max(Deal.deal_time),
                    ).where(*conds)
                )
            ).one()
            # 期初权益：窗口内**最早**一笔成交的 equity（不是期末，也不是 max）
            base_equity = (
                await s.execute(
                    select(Deal.equity)
                    .where(*conds, Deal.equity > 0)
                    .order_by(Deal.deal_time.asc(), Deal.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        logger.warning("leader_batch failed: %s", e)
        return _empty_summary()

    return _build_summary(
        position_profits=[float(pl or 0.0) for _pid, pl in pos_rows],
        net_profit=totals[0],
        gross_volume=totals[1],
        commission_total=totals[2],
        swap_total=totals[3],
        deal_count=totals[4],
        base_equity=base_equity,
        updated_at=totals[6],
    )


def _build_summary(
    *,
    position_profits: list[float],
    net_profit: Optional[float],
    gross_volume: Optional[float],
    commission_total: Optional[float],
    swap_total: Optional[float],
    deal_count: Optional[int],
    base_equity: Optional[float],
    updated_at: Optional[int],
) -> dict:
    """把原始聚合结果算成最终统计口径。

    **这是口径的唯一实现**：`leader_batch`（单/多节点）与 `node_summary`
    （单节点）都调它，保证成交明细页与概览页的胜率、净盈亏
    永远一致 —— 两处各算一次必然漂移。
    """
    net = float(net_profit or 0.0)
    vol = float(gross_volume or 0.0)
    comm = float(commission_total or 0.0)
    swap = float(swap_total or 0.0)
    count = int(deal_count or 0)

    # 持仓级胜率
    position_count = len(position_profits)
    win_positions = sum(1 for pl in position_profits if pl > 0)
    win_rate = round(win_positions / position_count * 100, 2) if position_count else 0.0

    # 收益率分母：区间期初权益
    equity = float(base_equity or 0.0)
    if equity > 0:
        profit_rate = round(net / equity * 100, 2)
        profit_rate_valid = True
    else:
        profit_rate = 0.0
        profit_rate_valid = False

    return {
        "total_profit": round(net, 2),
        "profit_rate": profit_rate,
        "win_rate": win_rate,
        "total_trades": position_count,
        "win_trades": win_positions,
        "lose_trades": max(0, position_count - win_positions),
        "gross_volume": round(vol, 2),
        "commission_total": round(comm, 2),
        "swap_total": round(swap, 2),
        "base_equity": round(equity, 2),
        "profit_rate_valid": profit_rate_valid,
        # 有成交才认为「有数据」；否则前端显示「暂无数据」而不是 0
        "data_available": count > 0,
        "updated_at": int(updated_at or 0) or None,
    }


# ---------------------------------------------------------------------------
# 近 N 日收益序列（柱状图数据源）
# ---------------------------------------------------------------------------
#
# 口径与 `leader_batch` **完全一致**：同一批出场类成交（OUT / INOUT / OUT_BY）、
# 同一份 net_profit（profit + swap + commission）、同一个节点范围（主节点）。
# 恒等式：7 根柱子的和 === 7d 窗口的 total_profit。柱状图与页面头部的
# 「累计收益」如果对不上，用户会立刻怀疑整个页面的数据可信度。
#
# **分日不在 SQL 里做**：`DATE(FROM_UNIXTIME(t))`（MySQL）与 `date(t,'unixepoch')`
# （SQLite）表达式不同，而本项目两种库都要跑（参见 `_out_profit` 用 `case`
# 而非 `iif` 的同类谨慎）。7 天单节点的成交行数在几百~几千量级，
# 查出来在 Python 侧累加完全够用，还顺带省掉了方言风险。
#
# **时区**（这里是最容易出错的地方）：
# deal_time 是 **MT5 服务器钟面**按 UTC 编码的 Unix —— 经纪商服务器不在 UTC+0 时，
# 它比真实 UTC 快 server_time_offset 秒。若直接按它分桶，MT5 里看到的收盘时间
# 会被归到错误的一天（UTC+3 的经纪商配 UTC+8 的用户，差 5 小时）。
# 所以分桶前先换算回真实 UTC：真实UTC = deal_time - server_time_offset。
# 真实 UTC 再按 UTC+8 自然日切分（用户所在时区），与 v4 的口径一致，
# 同时把「经纪商不在 UTC+8」这个偏差修正掉。
#
# 分桶放在后端而不是前端：服务端与浏览器分属不同机器/时区，
# 若在前端分日会整体错位一天。前端只拿 date 字符串显示。

# 分日用的固定偏移（秒）：UTC+8
_DAY_OFFSET = 8 * 3600

# 一天的秒数
_DAY_SECONDS = 86400


async def daily_net_profit(
    node_ids: list[str],
    days: int = 7,
) -> list[dict]:
    """近 N 个自然日的每日已实现净盈亏序列。

    返回 `days` 个元素，**没有成交的日子也占位**（profit=0）——
    X 轴必须连续，否则「没成交」会被渲染成「那天不存在」。

    每个元素：{date: 'MM-DD', ts: 该日 00:00(UTC+8) 的 Unix 秒, profit, deals}
    """
    ids = [str(i) for i in (node_ids or []) if i]
    days = max(1, int(days or 1))
    if not ids:
        return []

    # 窗口起点按「自然日」对齐：往前数 days 天，取当天 00:00(UTC+8)
    now = int(time.time())
    today_start = ((now + _DAY_OFFSET) // _DAY_SECONDS) * _DAY_SECONDS - _DAY_OFFSET
    start = today_start - (days - 1) * _DAY_SECONDS
    end = today_start + _DAY_SECONDS

    # 取出窗口内的出场类成交（净盈亏 = net_profit = profit + swap + commission），
    # 分日累加放在 Python 侧做（见文件头说明：不做 SQL 日期分组）。
    #
    # 窗口过滤放宽一天：deal_time 是服务器钟面，真实 UTC = deal_time - offset，
    # 而 offset 可能是负的（服务器比 UTC 慢）。这里按服务器钟面取宽出一天的
    # 范围，再用换算后的真实 UTC 精确归日。
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        Deal.deal_time,
                        Deal.net_profit,
                        Deal.server_time_offset,
                    ).where(
                        Deal.node_id.in_(ids),
                        Deal.deal_time >= int(start) - _DAY_SECONDS,
                        Deal.deal_time < int(end) + _DAY_SECONDS,
                        Deal.entry_type.in_(OUT_ENTRIES),
                    )
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("daily_net_profit failed: %s", e)
        return []

    # 分日累加：索引 = (deal_time - start) 落在第几天
    profits = [0.0] * days
    deals = [0] * days
    for deal_time, net_profit, offset in rows:
        # 服务器钟面 -> 真实 UTC，再按 UTC+8 自然日归集
        real_utc = int(deal_time or 0) - int(offset or 0)
        idx = (real_utc - start) // _DAY_SECONDS
        if idx < 0 or idx >= days:
            continue
        profits[idx] += float(net_profit or 0.0)
        deals[idx] += 1

    out: list[dict] = []
    for i in range(days):
        ts = start + i * _DAY_SECONDS
        # 用 gmtime(ts + 8h) 取「UTC+8 的墙上时钟」——
        # 不能用 localtime：服务器本地时区不确定，localtime 会再叠一层偏移。
        d = time.gmtime(ts + _DAY_OFFSET)
        out.append({
            "date": time.strftime("%m-%d", d),
            "ts": ts,
            "profit": round(profits[i], 2),
            "deals": deals[i],
        })
    return out


async def position_agg(
    node_id: str,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
) -> list[dict]:
    """持仓级聚合明细（绩效下钻用：每笔持仓的盈亏与出场次数）。"""
    if not node_id:
        return []
    conds = _base_conds([node_id], from_ts, to_ts)
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        Deal.position_id,
                        func.max(Deal.symbol),
                        func.sum(_out_profit()),
                        func.min(Deal.deal_time),
                        func.max(Deal.deal_time),
                        func.count(Deal.id),
                    )
                    .where(*conds, Deal.position_id.is_not(None), Deal.position_id > 0)
                    .group_by(Deal.position_id)
                    .order_by(func.max(Deal.deal_time).desc())
                )
            ).all()
            return [
                {
                    "position_id": int(pid or 0),
                    "symbol": sym,
                    "net_profit": round(float(pl or 0.0), 2),
                    "open_ts": int(o or 0),
                    "close_ts": int(c or 0),
                    "deal_count": int(cnt or 0),
                }
                for pid, sym, pl, o, c, cnt in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning("position_agg failed: %s", e)
        return []
