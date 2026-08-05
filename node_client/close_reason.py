"""平仓原因汇总（纯函数，无 I/O）。

节点在魔术号持仓归零时，从 MT5 成交历史取出出场单的 DEAL_REASON，
交给这里聚合成一句人读说明，写进 strategy_finished.reason / 事件「开单原因」。

MT5 ENUM_DEAL_REASON（与 MetaTrader5 包常量对齐）：
  0 CLIENT / 1 MOBILE / 2 WEB / 3 EXPERT / 4 SL / 5 TP / 6 SO / …
"""
from __future__ import annotations

from typing import Optional

# 与 MQL5 ENUM_DEAL_REASON 对齐；不依赖 MetaTrader5 包，便于无终端环境单测
REASON_CLIENT = 0
REASON_MOBILE = 1
REASON_WEB = 2
REASON_EXPERT = 3
REASON_SL = 4
REASON_TP = 5
REASON_SO = 6

# DEAL_ENTRY：只看出场成交
ENTRY_OUT = 1
ENTRY_INOUT = 2
ENTRY_OUT_BY = 3

# 默认占位码：节点尚未区分原因时沿用，前端也会看到这一串
DEFAULT_CLEAR_REASON = "positions_cleared"

_MANUAL = frozenset({REASON_CLIENT, REASON_MOBILE, REASON_WEB})


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def count_exit_reasons(deals: list[dict]) -> dict[str, int]:
    """把出场成交按原因归类计数。

    deals 每项至少含 entry / reason；非出场单忽略。
    返回键：sl / tp / so / manual / expert / other / total。
    """
    counts = {"sl": 0, "tp": 0, "so": 0, "manual": 0, "expert": 0, "other": 0, "total": 0}
    for deal in deals or []:
        if not isinstance(deal, dict):
            continue
        entry = _as_int(deal.get("entry"))
        if entry not in (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY):
            continue
        reason = _as_int(deal.get("reason"), -1)
        counts["total"] += 1
        if reason == REASON_SL:
            counts["sl"] += 1
        elif reason == REASON_TP:
            counts["tp"] += 1
        elif reason == REASON_SO:
            counts["so"] += 1
        elif reason in _MANUAL:
            counts["manual"] += 1
        elif reason == REASON_EXPERT:
            counts["expert"] += 1
        else:
            counts["other"] += 1
    return counts


def describe_close_reason(counts: dict[str, int]) -> str:
    """把出场原因计数写成一句中文；没有出场成交时退回默认码。"""
    total = int(counts.get("total") or 0)
    if total <= 0:
        return DEFAULT_CLEAR_REASON

    sl = int(counts.get("sl") or 0)
    tp = int(counts.get("tp") or 0)
    so = int(counts.get("so") or 0)
    manual = int(counts.get("manual") or 0)
    expert = int(counts.get("expert") or 0)
    other = int(counts.get("other") or 0)

    if sl == total:
        return f"止损打掉（{sl} 笔）"
    if tp == total:
        return f"止盈兑完（{tp} 笔）"
    if so == total:
        return f"强制平仓 Stop Out（{so} 笔）"
    if manual == total:
        return f"人工平仓（{manual} 笔）"
    if expert == total:
        return f"程序平仓（{expert} 笔）"
    if sl and tp and sl + tp == total:
        # 模版2 阶梯止盈常见：先兑掉若干档，剩余被共用止损收走
        return f"止盈 {tp} 笔后止损收口（止损 {sl} 笔）"
    if so and so + sl + tp + manual + expert + other == total and so:
        parts = [f"Stop Out {so} 笔"]
        if sl:
            parts.append(f"止损 {sl} 笔")
        if tp:
            parts.append(f"止盈 {tp} 笔")
        if manual:
            parts.append(f"人工 {manual} 笔")
        return "混合出场（" + " · ".join(parts) + "）"

    parts: list[str] = []
    if tp:
        parts.append(f"止盈 {tp}")
    if sl:
        parts.append(f"止损 {sl}")
    if so:
        parts.append(f"Stop Out {so}")
    if manual:
        parts.append(f"人工 {manual}")
    if expert:
        parts.append(f"程序 {expert}")
    if other:
        parts.append(f"其他 {other}")
    return f"混合出场（{' · '.join(parts)} 笔）" if parts else DEFAULT_CLEAR_REASON


def close_reason_detail(counts: dict[str, int], *, message: str) -> dict:
    """结构化明细，供后台事件 detail 展示。"""
    return {
        "kind": "close_reason",
        "message": message,
        "sl": int(counts.get("sl") or 0),
        "tp": int(counts.get("tp") or 0),
        "so": int(counts.get("so") or 0),
        "manual": int(counts.get("manual") or 0),
        "expert": int(counts.get("expert") or 0),
        "other": int(counts.get("other") or 0),
        "total": int(counts.get("total") or 0),
    }


def summarize_exit_deals(deals: list[dict]) -> tuple[str, Optional[dict]]:
    """出场成交 → (人读原因, detail)。无出场成交时返回默认码与 None。"""
    counts = count_exit_reasons(deals)
    if not counts["total"]:
        return DEFAULT_CLEAR_REASON, None
    message = describe_close_reason(counts)
    return message, close_reason_detail(counts, message=message)
