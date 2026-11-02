"""首页节点主页的频道划分与历史持仓聚合。

不做 I/O。AI 交易 / 带单两个市场的模版白名单以 follow_rules 为准：
tpl_1 归 AI，tpl_2 / tpl_3 归带单，其余模版（手工单、普通信号）不进频道。
"""
from __future__ import annotations

from typing import Optional

from . import follow_rules
from .group_rules import SUBTASK_TERMINAL

CHANNEL_AI = "ai"
CHANNEL_FOLLOW = "follow"
_OUT_ENTRIES = ("OUT", "OUT_BY", "INOUT")


def channel_of_template(template_id: object | None) -> Optional[str]:
    """策略模版属于哪个跟单频道；不在两个市场里则返回 None。"""
    if follow_rules.template_in_market(template_id, follow_rules.AI_MARKET_TEMPLATE_IDS):
        return CHANNEL_AI
    if follow_rules.template_in_market(template_id, follow_rules.FOLLOW_MARKET_TEMPLATE_IDS):
        return CHANNEL_FOLLOW
    return None


def is_active_status(status: object) -> bool:
    """子任务尚未收口（含残仓）时，频道平仓需要先停掉它，避免策略把仓补回来。"""
    return str(status or "") not in SUBTASK_TERMINAL


def prefer_magic_row(current: Optional[dict], incoming: dict) -> dict:
    """同一魔术号命中多条子任务时，留下仍未收口的；都收口或都在跑则留编号更大的。"""
    if current is None:
        return incoming
    cur_active = bool(current.get("active"))
    inc_active = bool(incoming.get("active"))
    if inc_active != cur_active:
        return incoming if inc_active else current
    if int(incoming.get("dispatch_id") or 0) >= int(current.get("dispatch_id") or 0):
        return incoming
    return current


def closed_status(reason: object) -> tuple[str, str]:
    """MT5 DEAL_REASON：4 止损、5 止盈，其余算普通平仓。"""
    try:
        code = int(reason)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        code = 0
    if code == 5:
        return "tp", "已止盈"
    if code == 4:
        return "sl", "已止损"
    return "closed", "已平仓"


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def position_direction(ins: list[dict], outs: list[dict]) -> str:
    """持仓方向：优先开仓成交的方向。平仓成交方向与持仓相反。"""
    if ins:
        return str(ins[0].get("direction") or "BUY").upper()
    last = str((outs[-1] if outs else {}).get("direction") or "SELL").upper()
    return "SELL" if last == "BUY" else "BUY"


def aggregate_closed_positions(deals: list[dict], *, limit: int = 50) -> list[dict]:
    """把同一 position_id 的开/平仓成交收成一行历史持仓。

    只保留有平仓成交的仓。开仓价取最早的 IN，平仓价与状态取最晚的 OUT。
    """
    buckets: dict[int, dict] = {}
    for row in deals:
        entry = str(row.get("entry_type") or "").upper()
        pid = row.get("position_id")
        key = _as_int(pid) if pid else _as_int(row.get("deal_id"))
        if not key:
            continue
        bucket = buckets.setdefault(key, {"ins": [], "outs": []})
        if entry == "IN":
            bucket["ins"].append(row)
        elif entry in _OUT_ENTRIES:
            bucket["outs"].append(row)
    out: list[dict] = []
    for pid, bucket in buckets.items():
        outs = sorted(bucket["outs"], key=lambda r: _as_int(r.get("deal_time")))
        if not outs:
            continue
        ins = sorted(bucket["ins"], key=lambda r: _as_int(r.get("deal_time")))
        last = outs[-1]
        profit = sum(_as_float(r.get("net_profit")) for r in outs)
        status, label = closed_status(last.get("reason"))
        symbol = str(last.get("symbol") or "")
        if not symbol and ins:
            symbol = str(ins[0].get("symbol") or "")
        out.append({
            "position_id": pid,
            "symbol": symbol,
            "direction": position_direction(ins, outs),
            "volume": sum(_as_float(r.get("volume")) for r in outs),
            "price_open": _as_float(ins[0].get("price")) if ins else 0.0,
            "price_close": _as_float(last.get("price")),
            "closed_at": _as_int(last.get("deal_time")),
            "status": status,
            "status_label": label,
            "profit": profit,
        })
    out.sort(key=lambda r: r["closed_at"], reverse=True)
    return out[: max(0, limit)]


def select_channel_positions(
    positions: list,
    magics: set[int],
    *,
    mode: str,
    ticket: Optional[int] = None,
) -> list[dict]:
    """从账户快照里挑出本频道要平的持仓。

    mode=all 全部；profit 只留浮动盈利；ticket 只留指定订单。
    """
    picked: list[dict] = []
    for raw in positions or []:
        if not isinstance(raw, dict):
            continue
        magic = _as_int(raw.get("magic"))
        if magic not in magics:
            continue
        if mode == "ticket" and _as_int(raw.get("ticket")) != _as_int(ticket):
            continue
        if mode == "profit" and _as_float(raw.get("profit")) <= 0:
            continue
        picked.append(raw)
    return picked
