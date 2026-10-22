"""成交明细（deal）的持久化与查询。

权威数据在 MySQL 的 `mt5_deal` 表；Redis 只做统计缓存，不做权威存储。

幂等写入：节点的采集是增量的（带重叠窗口），同一笔成交被重传是常态。
写入靠 (node_id, deal_id) 唯一键去重 —— **先查后写**存在并发竞态
（同一节点的两个批次同时到达），所以真正的保证是数据库唯一约束，
捕获 IntegrityError 后视为「已存在」而非失败。

来源回填（source）：节点只知道自己发了什么单，不知道这单属于哪个信号 /
策略任务 / 手工平仓。入库后按 magic / order_id 反查关联关系回填，
回填不到就保持 'terminal'（终端手工单）—— 这正是设计文档要求的行为。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .orm import Deal, GroupTaskDispatch, SignalDispatch

logger = logging.getLogger(__name__)

# 出场类 entry：只有这些计入「已实现盈亏」与胜率分母
OUT_ENTRIES = ("OUT", "INOUT", "OUT_BY")

# 来源取值
SOURCE_STRATEGY = "strategy"
SOURCE_SIGNAL = "signal"
SOURCE_MANUAL = "manual"
SOURCE_TERMINAL = "terminal"

# 手工平仓的 comment 关键字（后端平仓 API 写下的标记）
MANUAL_COMMENT_KEYS = ("close", "manual", "平仓", "手平")


def entry_label(entry_type: str) -> str:
    """开平仓的人读标签。"""
    return {
        "IN": "开仓",
        "OUT": "平仓",
        "INOUT": "双向",
        "OUT_BY": "对冲平仓",
    }.get(str(entry_type or "").upper(), "开仓")


def source_label(source: str) -> str:
    """来源的人读标签。"""
    return {
        SOURCE_STRATEGY: "策略",
        SOURCE_SIGNAL: "信号",
        SOURCE_MANUAL: "手工",
        SOURCE_TERMINAL: "终端",
    }.get(str(source or "").lower(), "终端")


def _deal_dict(row: Deal, position_direction: Optional[str] = None) -> dict:
    """ORM 行 -> 对外 dict（成交时间统一给 Unix 秒）。

    position_direction = 该持仓**开仓时**的方向。出场成交的 direction 是
    MT5 的成交方向（平多单是 SELL），直接展示会让人误以为又开了一单空仓，
    所以前端要按 position_direction 上色。
    """
    return {
        "id": row.id,
        "node_id": row.node_id,
        "deal_id": row.deal_id,
        "order_id": row.order_id,
        "position_id": row.position_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "position_direction": position_direction or row.direction,
        "entry_type": row.entry_type,
        "entry_label": entry_label(row.entry_type),
        "reason": row.reason,
        "volume": row.volume,
        "price": row.price,
        "profit": row.profit,
        "swap": row.swap,
        "commission": row.commission,
        "net_profit": row.net_profit,
        "magic": row.magic,
        "comment": row.comment,
        "source": row.source,
        "source_label": source_label(row.source),
        "signal_id": row.signal_id,
        "task_id": row.task_id,
        "group_id": row.group_id,
        "deal_time": row.deal_time,
    }


def _normalize(raw: dict) -> Optional[dict]:
    """把节点上报的一笔成交规范化成入库字段；缺关键字段返回 None（跳过）。

    deal_id 缺失就无法幂等去重，必须丢弃 —— 否则同一笔会被反复入库。
    """
    try:
        deal_id = int(raw.get("deal_id") or 0)
    except (TypeError, ValueError):
        return None
    if deal_id <= 0:
        return None

    def _num(key: str, default: float = 0.0) -> float:
        try:
            return float(raw.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _int(key: str) -> Optional[int]:
        v = raw.get(key)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    profit = _num("profit")
    swap = _num("swap")
    commission = _num("commission")
    # net_profit 统一口径：profit + swap + commission
    try:
        net = float(raw.get("net_profit"))
    except (TypeError, ValueError):
        net = profit + swap + commission

    # 来源：节点默认给 terminal，服务端回填为 strategy / signal / manual
    source = str(raw.get("source") or SOURCE_TERMINAL).strip().lower()[:16]
    if source not in (SOURCE_STRATEGY, SOURCE_SIGNAL, SOURCE_MANUAL, SOURCE_TERMINAL):
        source = SOURCE_TERMINAL

    return {
        "deal_id": deal_id,
        "order_id": _int("order_id"),
        "position_id": _int("position_id"),
        "symbol": (str(raw.get("symbol") or "").upper() or None),
        "direction": str(raw.get("direction") or "BUY").upper()[:8],
        "entry_type": str(raw.get("entry_type") or "IN").upper()[:8],
        "reason": _int("reason") or 0,
        "volume": _num("volume"),
        "price": _num("price"),
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "net_profit": net,
        "magic": _int("magic"),
        "comment": (str(raw.get("comment") or "")[:128] or None),
        "deal_time": _int("time") or 0,
        "source": source,
        "signal_id": (str(raw.get("signal_id") or "")[:64] or None),
        "task_id": _int("task_id"),
        "group_id": (str(raw.get("group_id") or "")[:32] or None),
        # 账户快照（收益率分母依赖 equity）
        "balance": _num("balance"),
        "equity": _num("equity"),
        "backfill": bool(raw.get("backfill") or False),
    }


async def save_deals(
    node_id: str,
    rows: list[dict],
    *,
    account: Optional[dict] = None,
) -> int:
    """幂等写入一批成交，返回**新增**条数。

    节点可能重传同一批（断线重连 / 采集重叠窗口），重复的按已存在处理。
    account 是节点上报的账户快照（balance/equity），冗余进每条成交 ——
    收益率的分母（区间期初权益）靠它算。
    """
    if not node_id or not rows:
        return 0
    bal = 0.0
    eq = 0.0
    if isinstance(account, dict):
        try:
            bal = float(account.get("balance") or 0.0)
        except (TypeError, ValueError):
            bal = 0.0
        try:
            eq = float(account.get("equity") or 0.0)
        except (TypeError, ValueError):
            eq = 0.0

    items = []
    seen: set[int] = set()
    for raw in rows:
        norm = _normalize(raw)
        if norm is None:
            continue
        # 同一批内部也可能有重复成交号
        if norm["deal_id"] in seen:
            continue
        seen.add(norm["deal_id"])
        # 成交自带账户快照时优先用它，否则用本批的账户快照
        if not norm["balance"]:
            norm["balance"] = bal
        if not norm["equity"]:
            norm["equity"] = eq
        items.append(norm)
    if not items:
        return 0

    try:
        async with SessionLocal() as s:
            existing = set(
                (
                    await s.execute(
                        select(Deal.deal_id).where(
                            Deal.node_id == node_id,
                            Deal.deal_id.in_([i["deal_id"] for i in items]),
                        )
                    )
                ).scalars().all()
            )
            fresh = [i for i in items if i["deal_id"] not in existing]
            if not fresh:
                return 0
            s.add_all([Deal(node_id=node_id, **i) for i in fresh])
            try:
                await s.commit()
            except IntegrityError:
                # 并发批次撞唯一键：回滚后按「已存在」处理，不算失败
                await s.rollback()
                logger.info("save_deals: concurrent duplicate batch ignored")
                return 0
            return len(fresh)
    except Exception as e:  # noqa: BLE001
        logger.warning("save_deals failed: %s", e)
        return 0


async def backfill_sources(node_id: str, deal_ids: Optional[list[int]] = None) -> int:
    """回填成交的来源与关联（signal_id / task_id / group_id）。

    回填规则（按可靠性排序，先命中先算）：
    1. magic 能对上 group_task_dispatch.magic -> source=strategy（并带出 task_id/group_id）
    2. order_id 能对上 signal_dispatch.order_ticket -> source=signal（并带出 signal_id）
    3. comment 含手工平仓标记 -> source=manual
    4. 都对不上 -> source=terminal（终端手工单，这是合法结果，不是错误）

    回填是**尽力而为**：对不上的保持 terminal，不影响统计。
    """
    if not node_id:
        return 0
    try:
        async with SessionLocal() as s:
            stmt = select(Deal).where(Deal.node_id == node_id)
            if deal_ids:
                stmt = stmt.where(Deal.deal_id.in_(list(deal_ids)))
            else:
                # 只回填还没关联上的（terminal 且无 task_id）
                stmt = stmt.where(Deal.source == SOURCE_TERMINAL, Deal.task_id.is_(None))
            rows = (await s.execute(stmt.limit(5000))).scalars().all()
            if not rows:
                return 0

            # 批量取关联关系，避免逐条查
            magics = {r.magic for r in rows if r.magic}
            order_ids = {r.order_id for r in rows if r.order_id}

            strategy_by_magic: dict[int, tuple] = {}
            if magics:
                for row in (
                    await s.execute(
                        select(
                            GroupTaskDispatch.magic,
                            GroupTaskDispatch.task_id,
                            GroupTaskDispatch.group_id,
                        ).where(GroupTaskDispatch.magic.in_(list(magics)))
                    )
                ).all():
                    if row[0]:
                        strategy_by_magic[int(row[0])] = (row[1], row[2])

            signal_by_order: dict[int, str] = {}
            if order_ids:
                for row in (
                    await s.execute(
                        select(SignalDispatch.order_ticket, SignalDispatch.signal_id).where(
                            SignalDispatch.order_ticket.in_(list(order_ids))
                        )
                    )
                ).all():
                    if row[0]:
                        signal_by_order[int(row[0])] = row[1]

            changed = 0
            for r in rows:
                hit = None
                if r.magic and r.magic in strategy_by_magic:
                    task_id, group_id = strategy_by_magic[r.magic]
                    r.source = SOURCE_STRATEGY
                    r.task_id = task_id
                    r.group_id = group_id
                    hit = True
                elif r.order_id and r.order_id in signal_by_order:
                    r.source = SOURCE_SIGNAL
                    r.signal_id = signal_by_order[r.order_id]
                    hit = True
                elif r.comment and any(
                    k in r.comment.lower() for k in MANUAL_COMMENT_KEYS
                ):
                    r.source = SOURCE_MANUAL
                    hit = True
                if hit:
                    changed += 1
            if changed:
                await s.commit()
            return changed
    except Exception as e:  # noqa: BLE001
        logger.warning("backfill_sources failed: %s", e)
        return 0


async def list_deals(
    *,
    node_id: Optional[str] = None,
    node_ids: Optional[list[str]] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    entry_type: Optional[str] = None,
    source: Optional[str] = None,
    keyword: Optional[str] = None,
    magic: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """分页查询成交明细。

    排序用 (deal_time DESC, deal_id DESC) **复合排序**：同一秒内多笔成交时
    单按时间排序不稳定，跨页会出现重复或遗漏。

    返回里带 summary（与 items 用同一 WHERE 条件算出），前端直接展示，
    不要自己再算一遍 —— 两处各算一次必然对不上。
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 200))

    conds = []
    if node_id:
        conds.append(Deal.node_id == node_id)
    elif node_ids:
        if not node_ids:
            return {"items": [], "total": 0, "page": page, "page_size": page_size,
                    "summary": _empty_summary()}
        conds.append(Deal.node_id.in_(list(node_ids)))
    if symbol:
        conds.append(Deal.symbol == symbol.upper())
    if direction:
        conds.append(Deal.direction == direction.upper())
    if entry_type:
        conds.append(Deal.entry_type == entry_type.upper())
    if source:
        conds.append(Deal.source == source.lower())
    if magic is not None:
        conds.append(Deal.magic == int(magic))
    if from_ts is not None:
        conds.append(Deal.deal_time >= int(from_ts))
    if to_ts is not None:
        conds.append(Deal.deal_time <= int(to_ts))
    if keyword:
        kw = f"%{str(keyword).strip()}%"
        conds.append(
            or_(
                Deal.comment.like(kw),
                cast(Deal.deal_id, String).like(kw),
                cast(Deal.order_id, String).like(kw),
            )
        )

    try:
        async with SessionLocal() as s:
            total = 0
            if conds:
                total = (
                    await s.execute(
                        select(func.count()).select_from(Deal).where(*conds)
                    )
                ).scalar_one()
            rows = (
                await s.execute(
                    select(Deal)
                    .where(*conds)
                    .order_by(Deal.deal_time.desc(), Deal.deal_id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).scalars().all()

            # 出场行的方向色要按「被平掉的那个持仓的方向」取：
            # 平多单在 MT5 里是 SELL，直接展示会让人误以为又开了空仓。
            pos_dirs = await _position_directions(s, rows)
            items = [
                _deal_dict(r, pos_dirs.get((r.node_id, r.position_id)))
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning("list_deals failed: %s", e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "summary": _empty_summary()}

    # summary 与 items 用同一 WHERE 条件（口径统一在 deal_stats）
    from . import deal_stats
    summary = await deal_stats.leader_batch(
        [node_id] if node_id else (node_ids or []),
        from_ts, to_ts, symbol=symbol,
    )
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "summary": summary,
    }


async def _position_directions(s, rows: list[Deal]) -> dict:
    """查这些持仓的**开仓方向** {(node_id, position_id): 'BUY'/'SELL'}。"""
    keys = {(r.node_id, r.position_id) for r in rows if r.position_id}
    if not keys:
        return {}
    node_ids = {n for n, _ in keys}
    pids = {p for _, p in keys}
    try:
        out: dict = {}
        for row in (
            await s.execute(
                select(Deal.node_id, Deal.position_id, Deal.direction).where(
                    Deal.node_id.in_(list(node_ids)),
                    Deal.position_id.in_(list(pids)),
                    Deal.entry_type == "IN",
                )
            )
        ).all():
            out[(row[0], row[1])] = row[2]
        return out
    except Exception:  # noqa: BLE001
        return {}


def _empty_summary() -> dict:
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
