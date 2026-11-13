"""**AI 信号管理中心**的上半屏口径（指标卡 / 质量总览 / 类型对比 / 周期总览）。

设计文档：`docs/superpowers/specs/2026-11-13-ai-signal-center-prototype-design.md`

## 三条铁律

1. **质量分只有一个实现**：本模块**不自己算胜率**，全部走
   `signal_quality`（它内部复用 `signal_perf._aggregate`）。
   质量分与绩效面板的数字必须**逐位相等** —— 两边各写一套必然漂移。
2. **等级是绝对阈值**：≥60 高 / 50~60 中 / <50 低（见 `signal_quality.level_for`）。
   等级**只是展示层翻译**，不参与打分。
3. **今日触发信号按 UTC+8 自然日**（对齐 `signal_stats.today_start_utc8`），
   与库里其它分日口径一致。

本模块**不参与任何交易判断**：查询失败、字段缺失都只影响统计展示。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import signal_meta, signal_quality, signal_stats

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 质量分区间分布（固定四档，与原型一致）
# ---------------------------------------------------------------------------
SCORE_BUCKETS: tuple[tuple[str, str, float, Optional[float]], ...] = (
    # key, 标签, 下界（含）, 上界（不含，None = 不限）
    ("gte90", "90 分以上", 90.0, None),
    ("80_89", "80 - 89 分", 80.0, 90.0),
    ("70_79", "70 - 79 分", 70.0, 80.0),
    ("lt70", "70 分以下", 0.0, 70.0),
)

# 口径说明（前端必须原样展示）
QUALITY_SCOPE = (
    "质量分 = 同类历史信号（类型×周期）的信号级胜率，小样本按贝叶斯收缩向全局回归；"
    "等级：60 分以上为高、50-60 分为中、50 分以下为低；仅供参考，不参与交易决策"
)

# 止盈分布的口径说明（前端必须原样展示）
#
# ⚠️ 「按账户分别统计」说的是**怎么数**，不是**怎么显示**：
# 统计单元是「信号 × 账户」，每个账户各自算档位（不合并、不取最大值）；
# 但界面只画**一条合并后的分布条** —— 一个模块里堆 4 条分布条，
# 横向对比反而看不清整体形态。
TP_SCOPE = (
    "档位 = 最终止盈那一刻的持仓笔数（含首仓）；"
    "各账户分别统计后汇总展示；止损与持仓中不计入分布"
)


def _bucket_of(score: float) -> str:
    """分数落在哪个区间。"""
    for key, _label, lo, hi in SCORE_BUCKETS:
        if score >= lo and (hi is None or score < hi):
            return key
    return SCORE_BUCKETS[-1][0]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def overview(f: Any) -> dict:
    """AI 信号管理中心上半屏。

    ```
    {
      "window": "30d",
      "kpi": {...},
      "reliability": [...],       # 等级分布
      "score_buckets": [...],     # 质量分区间分布
      "by_type": [...],           # 信号类型质量对比
      "by_timeframe": [...],      # 周期质量总览
      "top_signals": [...],       # 质量 TOP 5
      "updated_at": ...
    }
    ```
    """
    signals = await signal_stats._fetch_signals(f)
    prior = await signal_quality.get_prior(signal_quality.DEFAULT_PRIOR_WINDOW)
    quality_by_key = await signal_quality.quality_map(signals)

    # —— 逐条信号的质量分 + 等级 ——
    scored: list[dict] = []
    for sig in signals:
        st = signal_meta.normalize_signal_type(sig.signal_type) or ""
        tf = signal_meta.normalize_timeframe(sig.timeframe) or ""
        q = quality_by_key.get((st, tf))
        score = q.get("score") if q else None
        level = signal_quality.level_for(score)
        scored.append({
            "signal": sig,
            "quality": q,
            "score": score,
            "level": level,
            "advice": signal_quality.advice_for(level),
        })

    total = len(scored)
    scored_only = [s for s in scored if s["score"] is not None]

    # —— KPI ——
    today_count = await _today_signal_count()
    timeframes = {
        tf for tf in (
            signal_meta.normalize_timeframe(s["signal"].timeframe) for s in scored
        ) if tf
    }
    high_count = sum(1 for s in scored if s["level"] == signal_quality.LEVEL_HIGH)
    avg_quality = (
        round(sum(float(s["score"]) for s in scored_only) / len(scored_only), 2)
        if scored_only else 0.0
    )

    # —— 等级分布 ——
    reliability = []
    for lv in (signal_quality.LEVEL_HIGH, signal_quality.LEVEL_MID,
               signal_quality.LEVEL_LOW):
        c = sum(1 for s in scored if s["level"] == lv)
        reliability.append({
            "level": lv,
            "label": signal_quality.level_label(lv),
            "count": c,
            "pct": round(c / total * 100, 2) if total else 0.0,
            "advice": signal_quality.advice_for(lv),
        })

    # —— 质量分区间分布 ——
    bucket_counts = {key: 0 for key, _l, _lo, _hi in SCORE_BUCKETS}
    for s in scored_only:
        bucket_counts[_bucket_of(float(s["score"]))] += 1
    score_buckets = []
    for key, label, _lo, _hi in SCORE_BUCKETS:
        c = bucket_counts.get(key, 0)
        score_buckets.append({
            "key": key,
            "label": label,
            "count": c,
            "pct": round(c / len(scored_only) * 100, 2) if scored_only else 0.0,
        })

    # —— 按类型（业务分类）/ 按周期聚合 ——
    by_type = _group_by(scored, key="type")
    by_timeframe = _group_by(scored, key="timeframe")

    # —— 质量 TOP 5 ——
    top = sorted(
        [s for s in scored_only if s["score"] is not None],
        key=lambda s: (-float(s["score"]), -(s["signal"].received_at.timestamp()
                                             if s["signal"].received_at else 0)),
    )[:5]
    top_signals = [_signal_row(s) for s in top]

    return {
        "window": getattr(f, "window", None),
        "from_ts": int(f.from_ts) if getattr(f, "from_ts", None) is not None else None,
        "kpi": {
            "today_signals": today_count,
            "timeframes_covered": len(timeframes),
            "high_reliability": high_count,
            "avg_quality": avg_quality,
            "signals_total": total,
            "signals_scored": len(scored_only),
        },
        "reliability": reliability,
        "score_buckets": score_buckets,
        "by_type": by_type,
        "by_timeframe": by_timeframe,
        "top_signals": top_signals,
        "updated_at": int(time.time()),
        "scope": QUALITY_SCOPE,
    }


async def _today_signal_count() -> int:
    """今日触发信号（UTC+8 自然日）。

    **不套信号中心的筛选条** —— 它是「今天一共来了多少条」的绝对值。
    """
    from .db import SessionLocal
    from .orm import SignalHistory
    from sqlalchemy import select

    start = signal_stats.today_start_utc8()
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(SignalHistory.signal_id).where(
                        SignalHistory.received_at >= signal_stats.datetime_from_ts(start),
                    )
                )
            ).scalars().all()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_ai_center._today_signal_count failed: %s", e)
        return 0


def _group_by(scored: list[dict], *, key: str) -> list[dict]:
    """按类型（**业务分类**）/ 按周期聚合质量分。

    - 类型维度按**业务分类**聚合（结构分析 = ob + fvg + pdz）——
      原型的「信号类型质量对比」就是按分类展示的；
      分类下的**各类型码**记在 `types` 字段里，供前端联动筛选用。
    - `avg_score` 用**该组内信号质量分的算术平均**（不是桶先验分）——
      指标卡「平均信号质量」与这里必须是同一个口径，否则两处数字对不上。
    """
    groups: dict[str, dict] = {}
    for s in scored:
        sig = s["signal"]
        if key == "type":
            code = signal_meta.signal_type_group(sig.signal_type)
            label = code
            raw = signal_meta.normalize_signal_type(sig.signal_type) or ""
        else:
            code = signal_meta.normalize_timeframe(sig.timeframe) or "__none__"
            label = signal_meta.timeframe_label(sig.timeframe)
            raw = ""
        g = groups.setdefault(code, {
            "key": code,
            "label": label,
            "count": 0,
            "scores": [],
            "high_count": 0,
            "types": [],
            "best": None,
        })
        g["count"] += 1
        if key == "type" and raw and raw not in g["types"]:
            g["types"].append(raw)
        if s["score"] is not None:
            g["scores"].append(float(s["score"]))
        if s["level"] == signal_quality.LEVEL_HIGH:
            g["high_count"] += 1
        if s["score"] is not None:
            if g["best"] is None or float(s["score"]) > float(g["best"]["score"]):
                g["best"] = s

    out: list[dict] = []
    for g in groups.values():
        scores = g["scores"]
        avg = round(sum(scores) / len(scores), 2) if scores else 0.0
        best_row = _signal_row(g["best"]) if g["best"] else None
        lv = signal_quality.level_for(avg if scores else None)
        out.append({
            "key": g["key"],
            "label": g["label"],
            "count": g["count"],
            "avg_score": avg,
            "high_count": g["high_count"],
            "level": lv,
            "advice": signal_quality.advice_for(lv),
            "best_signal": best_row,
            # 该分类下的类型码（联动筛选用）
            "types": g["types"],
        })
    # 按信号数倒序（与原型「主要类型在前」一致）
    out.sort(key=lambda x: (-x["count"], x["key"]))
    return out


def _signal_row(s: Optional[dict]) -> Optional[dict]:
    """单条信号 → 前端要的形状。"""
    if not s:
        return None
    sig = s["signal"]
    q = s.get("quality")
    ts = sig.received_at.timestamp() if sig.received_at else None
    return {
        "signal_id": sig.signal_id,
        "received_at": ts,
        "timeframe": sig.timeframe,
        "timeframe_label": signal_meta.timeframe_label(sig.timeframe),
        "signal_type": sig.signal_type,
        "signal_type_label": signal_meta.signal_type_label(sig.signal_type),
        "type_group": signal_meta.signal_type_group(sig.signal_type),
        "name": signal_meta.signal_name(sig.action, sig.signal_type, sig.timeframe),
        "action": sig.action,
        "action_label": signal_meta.action_label(sig.action),
        "symbol": sig.symbol,
        "quality": q,
        "score": s.get("score"),
        "level": s.get("level"),
        "advice": s.get("advice"),
    }


async def tp_distribution(f: Any, *, node_names: Optional[dict[str, str]] = None) -> dict:
    """马丁止盈分布（按节点分开统计）。

    口径：
    - 档位 = 最终止盈那一刻的持仓笔数（含首仓）；
    - 统计单元 = (signal_id, node_id)，**按节点分开**，不合并；
    - 止损 / 持仓中不产生档位，不进分布分母。
    """
    from . import signal_tp_levels

    signals = await signal_stats._fetch_signals(f)
    units = await signal_tp_levels.units_for_signals([s.signal_id for s in signals])
    dist = signal_tp_levels.build_distribution(units, node_names=node_names or {})

    # 每个档位的重点信号 TOP 5（按质量分降序）
    top_by_level = await _top_by_level(signals, units)

    return {
        "window": getattr(f, "window", None),
        "total_signals": len(signals),
        "closed_tp_units": dist["closed_tp_units"],
        "by_node": dist["by_node"],
        "overall": dist["overall"],
        "states": dist["states"],
        "top_by_level": top_by_level,
        "updated_at": int(time.time()),
        "scope": TP_SCOPE,
    }


async def _top_by_level(signals: list[Any], units: list[dict]) -> dict[str, list[dict]]:
    """每个档位下质量分最高的 5 条信号。

    「按节点分开统计」的档位在展示上按**档位**聚合到信号 ——
    一条信号在两个节点上都是 3 档，它在 3 档的 TOP 列表里只出现一次。

    **键是字符串**（`"1"` ~ `"5"` + `"all"`）：
    - Pydantic 的 `dict[str, ...]` 要求键是字符串，int 键会让响应模型校验失败；
    - `"all"` 是「全部档位」的 TOP 5 —— 前端默认就选中「全部信号」，
      没有这个键的话默认视图的 TOP 列表恒为空。
    """
    by_id = {s.signal_id: s for s in signals}
    # 档位 → 信号号集合
    level_signals: dict[int, set[str]] = {}
    all_signals: set[str] = set()
    for u in units:
        lv = u.get("tp_level")
        if lv is None:
            continue
        sid = u.get("signal_id")
        if not sid or sid not in by_id:
            continue
        level_signals.setdefault(int(lv), set()).add(sid)
        all_signals.add(sid)

    if not level_signals:
        return {}

    # 只给需要的信号算质量分
    wanted = [s for s in signals if s.signal_id in all_signals]
    quality_by_key = await signal_quality.quality_map(wanted)

    def _rows_for(sids: set[str]) -> list[dict]:
        rows: list[dict] = []
        for sid in sids:
            sig = by_id[sid]
            st = signal_meta.normalize_signal_type(sig.signal_type) or ""
            tf = signal_meta.normalize_timeframe(sig.timeframe) or ""
            q = quality_by_key.get((st, tf))
            score = q.get("score") if q else None
            level = signal_quality.level_for(score)
            row = _signal_row({
                "signal": sig, "quality": q, "score": score,
                "level": level, "advice": signal_quality.advice_for(level),
            })
            if row:
                row["tp_level"] = None
                row["tp_level_label"] = None
                row["tp_state"] = "tp"
                rows.append(row)
        rows.sort(key=lambda r: (
            -(r.get("score") if r.get("score") is not None else -1),
            -(r.get("received_at") or 0),
        ))
        return rows[:5]

    out: dict[str, list[dict]] = {}
    for lv, sids in level_signals.items():
        rows = _rows_for(sids)
        for row in rows:
            row["tp_level"] = lv
            row["tp_level_label"] = f"{lv}单止盈"
        out[str(lv)] = rows
    # 「全部档位」的 TOP 5
    out["all"] = _rows_for(all_signals)
    return out


# ---------------------------------------------------------------------------
# 明细列表
# ---------------------------------------------------------------------------
async def signals_page(
    f: Any,
    *,
    page: int = 1,
    page_size: int = 10,
    tp_level: Optional[int] = None,
    node_id: Optional[str] = None,
    type_group: Optional[str] = None,
) -> dict:
    """信号明细（页码分页）。

    - 先按筛选条分页取信号，再对**当页**信号算止盈档位与质量分
      （档位是派生数据，不下推到 SQL）；
    - `tp_level` 过滤在**派生之后**做；
    - `type_group` 按业务分类过滤（结构分析 = ob + fvg + pdz）——
      分类下的类型码展开成集合匹配，只取一个码会漏掉另外两个。
    """
    from . import signal_tp_levels

    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 200))

    signals = await signal_stats._fetch_signals(f)

    # 业务分类过滤（在取信号之后做 —— 分类是展示层的归类，
    # 库里只有原始类型码，没有「分类」这一列）
    group_types: Optional[set[str]] = None
    if type_group:
        group_types = set(signal_meta.types_in_group(type_group))
        if not group_types:
            group_types = set()   # 未知分类 → 命中不到（而不是不过滤）
        signals = [
            s for s in signals
            if (signal_meta.normalize_signal_type(s.signal_type) or "") in group_types
        ]

    total_signals = len(signals)

    units = await signal_tp_levels.units_for_signals([s.signal_id for s in signals])
    unit_by_signal: dict[str, list[dict]] = {}
    for u in units:
        unit_by_signal.setdefault(u["signal_id"], []).append(u)

    prior = await signal_quality.get_prior(signal_quality.DEFAULT_PRIOR_WINDOW)
    quality_by_key = await signal_quality.quality_map(signals)

    # 逐条信号 + 它的止盈单元
    rows: list[dict] = []
    for sig in signals:
        st = signal_meta.normalize_signal_type(sig.signal_type) or ""
        tf = signal_meta.normalize_timeframe(sig.timeframe) or ""
        q = quality_by_key.get((st, tf))
        score = q.get("score") if q else None
        level = signal_quality.level_for(score)
        us = unit_by_signal.get(sig.signal_id, [])
        if node_id:
            us = [u for u in us if u["node_id"] == node_id]
        rows.append({
            "signal": sig,
            "quality": q,
            "score": score,
            "level": level,
            "advice": signal_quality.advice_for(level),
            "units": us,
        })

    # 档位筛选（派生之后）
    if tp_level is not None:
        def _match(r: dict) -> bool:
            if tp_level == 0:
                # 0 = 未止盈（持仓中 / 止损 / 无数据）
                return not any(u.get("tp_level") is not None for u in r["units"])
            return any(int(u.get("tp_level") or 0) == tp_level for u in r["units"])
        rows = [r for r in rows if _match(r)]

    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    items = []
    for r in page_rows:
        row = _signal_row(r)
        if row is None:
            continue
        us = r["units"]
        levels = [u.get("tp_level") for u in us if u.get("tp_level") is not None]
        states = [u.get("tp_state") for u in us]
        row["tp_level"] = max(levels) if levels else None
        row["tp_level_label"] = f"{row['tp_level']}单止盈" if levels else None
        row["tp_state"] = (
            "tp" if levels
            else ("running" if "running" in states else (states[0] if states else "none"))
        )
        row["tp_units"] = [
            {
                "node_id": u.get("node_id"),
                "tp_level": u.get("tp_level"),
                "tp_state": u.get("tp_state"),
                "tp_at": u.get("tp_at"),
            }
            for u in us
        ]
        items.append(row)

    return {
        "window": getattr(f, "window", None),
        "total": total,
        "total_signals": total_signals,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "items": items,
        "updated_at": int(time.time()),
        "scope": QUALITY_SCOPE,
    }
