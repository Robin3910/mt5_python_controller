"""模版1 手动分散仓：配置抽取、手数反推与到价判定。

与分批加仓隔离：不进 batch_levels，MT5 注释用 M1/M2（禁止 R3B）。
到价是回踩/反弹到入场再市价开：BUY 等卖价 ≤ 入场，SELL 等买价 ≥ 入场。
成交侧已越过止盈则不开。手数按当前任务总手数匹配 lot_pl_tiers 的
pl_amount，再按 |tp-entry| / tick_size * tick_value 反推，向上取整到 volume_step。

止盈联动清仓（close_all_on_tp）：跟踪在场手动仓，某一拍消失后查出场成交的
DEAL_REASON；确认是止盈（历史未到时用平仓侧现价兜底）就平掉该魔术号其余持仓收口。
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from close_reason import (
    REASON_CLIENT,
    REASON_EXPERT,
    REASON_MOBILE,
    REASON_SL,
    REASON_SO,
    REASON_TP,
    REASON_WEB,
)

RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2
COMMENT_PREFIX = "M"
# M1 / M2，可选后续字符；禁止与模版2 R3B 撞车
_COMMENT_RE = re.compile(r"^M([12])(?:\b|$)")
EVENT_TYPE = "add_manual"
# 止盈联动清仓事件（库字段 VARCHAR(16)）
TP_CLOSE_EVENT = "manual_tp_close"
DETAIL_KIND = "manual_scatter"
# 手动仓消失后成交历史还没写进来：最多再等几拍再放弃判定
EXIT_RESOLVE_ATTEMPTS = 3
# 查出场成交的时间窗往前多留的秒数：持仓 time 是券商钟面，与本机可能差几个小时
EXIT_LOOKUP_MARGIN_SEC = 12 * 3600

EXIT_TP = "tp"
EXIT_OTHER = "other"
_EXIT_LABELS = {
    REASON_SL: "止损",
    REASON_TP: "止盈",
    REASON_SO: "强制平仓",
    REASON_EXPERT: "程序平仓",
    REASON_CLIENT: "人工平仓",
    REASON_MOBILE: "人工平仓",
    REASON_WEB: "人工平仓",
}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "entry_price": 0.0,
        "take_profit": 0.0,
        "stop_loss": 0.0,
        "volume": 0.0,
        "volume_locked": False,
        # 默认开启；旧快照缺字段也按开启处理，与后端 default_manual_scatter 一致
        "close_all_on_tp": True,
    }


def normalize(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_config()
    return {
        "enabled": bool(src.get("enabled", defaults["enabled"])),
        "entry_price": max(0.0, _as_float(src.get("entry_price"), defaults["entry_price"])),
        "take_profit": max(0.0, _as_float(src.get("take_profit"), defaults["take_profit"])),
        "stop_loss": max(0.0, _as_float(src.get("stop_loss"), defaults["stop_loss"])),
        "volume": max(0.0, _as_float(src.get("volume"), defaults["volume"])),
        "volume_locked": bool(src.get("volume_locked", defaults["volume_locked"])),
        "close_all_on_tp": bool(src.get("close_all_on_tp", defaults["close_all_on_tp"])),
    }


def comment_for(rule_type: int) -> str:
    return f"{COMMENT_PREFIX}{int(rule_type)}"


def parse_rule_type(comment: object) -> Optional[int]:
    text = str(comment or "").strip()
    m = _COMMENT_RE.match(text)
    if not m:
        return None
    return int(m.group(1))


def is_manual_position(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    return parse_rule_type(pos.get("comment")) is not None


def exclude_manual(positions: list | tuple | None) -> list[dict]:
    """分批判定用：去掉手动分散仓，避免抬高 position_count / 逆势锚点。"""
    out: list[dict] = []
    for p in positions or []:
        if isinstance(p, dict) and not is_manual_position(p):
            out.append(p)
    return out


def fired_rule_types(positions: list | tuple | None) -> set[int]:
    found: set[int] = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        rt = parse_rule_type(p.get("comment"))
        if rt is not None:
            found.add(rt)
    return found


def current_lot(positions: list | tuple | None) -> float:
    total = 0.0
    for p in positions or []:
        if isinstance(p, dict):
            total += _as_float(p.get("volume"))
    return round(total, 4)


def direction_from_prices(entry: float, take_profit: float) -> Optional[str]:
    if entry <= 0 or take_profit <= 0 or entry == take_profit:
        return None
    return "BUY" if take_profit > entry else "SELL"


def fill_side_price(
    direction: str, *, bid: float = 0.0, ask: float = 0.0, fallback: float = 0.0,
) -> float:
    """开仓成交侧报价，与 `place_market_order` 一致：BUY=ask，SELL=bid。

    缺对应侧时报 fallback（兼容只带平仓侧 `event.price` 的旧事件 / 单测）。
    """
    side = str(direction or "").strip().upper()
    if side == "BUY":
        return ask if ask > 0 else fallback
    if side == "SELL":
        return bid if bid > 0 else fallback
    return fallback


def price_reached(direction: str, price: float, entry: float) -> bool:
    """BUY：成交侧已回落到入场（<=）；SELL：成交侧已反弹到入场（>=）。"""
    if entry <= 0 or price <= 0:
        return False
    side = str(direction or "").strip().upper()
    if side == "BUY":
        return price - 1e-12 <= entry
    if side == "SELL":
        return price + 1e-12 >= entry
    return False


def tp_passed(direction: str, price: float, take_profit: float) -> bool:
    """成交侧已越过止盈：BUY 现价 >= 止盈，SELL 现价 <= 止盈。止盈未设则 False。"""
    if take_profit <= 0 or price <= 0:
        return False
    side = str(direction or "").strip().upper()
    if side == "BUY":
        return price + 1e-12 >= take_profit
    if side == "SELL":
        return price - 1e-12 <= take_profit
    return False


def describe_tp_passed(
    direction: str, price: float, entry: float, take_profit: float,
) -> str:
    side = str(direction or "").strip().upper() or "?"
    wait = "回落到入场" if side == "BUY" else "反弹到入场"
    return (
        f"手动分散仓越过止盈，暂不开仓：{side} 现价 {_trim(price)}"
        f" 已过止盈 {_trim(take_profit)}，等待{wait} {_trim(entry)}"
    )


def match_target_pl(lot_pl_tiers: object, current_lot_size: float) -> Optional[float]:
    """从高档往低档，第一条 current_lot >= min_lot 的 pl_amount。

    分档未启用或匹配不到返回 None（节点不开仓；表单提示手填）。
    """
    src = lot_pl_tiers if isinstance(lot_pl_tiers, dict) else {}
    if not src.get("enabled"):
        return None
    tiers = src.get("tiers") if isinstance(src.get("tiers"), list) else []
    lot = max(0.0, float(current_lot_size or 0.0))
    for idx in range(len(tiers) - 1, -1, -1):
        tier = tiers[idx]
        if not isinstance(tier, dict):
            continue
        min_lot = _as_float(tier.get("min_lot"))
        pla = _as_float(tier.get("pl_amount"))
        if pla == 0:
            continue
        if lot + 1e-12 >= min_lot:
            return pla
    return None


def profit_per_lot(entry: float, take_profit: float, tick_size: float, tick_value: float) -> float:
    distance = abs(float(take_profit) - float(entry))
    if distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0
    return distance / tick_size * tick_value


def ceil_to_step(lot: float, volume_step: float, volume_min: float, volume_max: float,
                 volume_digits: int = 2) -> float:
    """向上取整到 volume_step，保证实际利润 >= 目标；再夹到 min/max。"""
    raw = max(0.0, float(lot or 0.0))
    step = float(volume_step or 0.0)
    if step <= 0:
        sized = round(raw, volume_digits)
    else:
        steps = math.ceil((raw - 1e-9) / step)
        if steps < 0:
            steps = 0
        digits = volume_digits
        sized = round(steps * step, digits)
    lo = max(0.0, float(volume_min or 0.0))
    hi = float(volume_max or 0.0)
    if lo > 0 and sized > 0 and sized < lo:
        sized = lo
    if hi > 0 and sized > hi:
        sized = hi
    return sized


def compute_volume(
    *,
    cfg: dict,
    current_lot_size: float,
    lot_pl_tiers: object,
    tick_size: float,
    tick_value: float,
    volume_step: float = 0.01,
    volume_min: float = 0.01,
    volume_max: float = 100.0,
) -> Optional[float]:
    """返回开仓手数；无法计算返回 None。

    volume_locked 且 volume>0 时直接用配置手数（仍夹到 min/max/step）。
    """
    src = normalize(cfg)
    if src.get("volume_locked") and src["volume"] > 0:
        return ceil_to_step(
            src["volume"], volume_step, volume_min, volume_max,
        )
    target = match_target_pl(lot_pl_tiers, current_lot_size)
    if target is None:
        if src["volume"] > 0:
            return ceil_to_step(src["volume"], volume_step, volume_min, volume_max)
        return None
    per = profit_per_lot(src["entry_price"], src["take_profit"], tick_size, tick_value)
    if per <= 0:
        return None
    raw = abs(float(target)) / per
    sized = ceil_to_step(raw, volume_step, volume_min, volume_max)
    return sized if sized > 0 else None


def extract_watches(rules: object) -> list[dict[str, Any]]:
    """启用中的手动分散仓监视项；规则 status=0 的忽略。顺势/逆势各至多一条。"""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    if not isinstance(rules, list):
        return out
    for raw in rules:
        if not isinstance(raw, dict):
            continue
        rule_type = _as_int(raw.get("type"))
        if rule_type not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
            continue
        if rule_type in seen:
            continue
        if not _as_int(raw.get("status")):
            continue
        cfg = normalize(raw.get("manual_scatter"))
        if not cfg.get("enabled"):
            continue
        direction = direction_from_prices(cfg["entry_price"], cfg["take_profit"])
        if direction is None:
            continue
        seen.add(rule_type)
        lot_pl = raw.get("lot_pl_tiers") if isinstance(raw.get("lot_pl_tiers"), dict) else {}
        out.append({
            "rule_type": rule_type,
            "cfg": cfg,
            "direction": direction,
            "lot_pl_tiers": lot_pl,
            "comment": comment_for(rule_type),
        })
    return out


# ----------------------------------------------------------------------
# 止盈联动清仓：在场跟踪 → 消失 → 判离场原因
# ----------------------------------------------------------------------
def linkage_rule_types(rules: object) -> set[int]:
    """启用了「止盈联动清仓」的规则类型；规则关闭或手动仓关闭的不算。"""
    return {
        int(w["rule_type"]) for w in extract_watches(rules)
        if bool((w.get("cfg") or {}).get("close_all_on_tp"))
    }


def _snapshot_from_position(pos: dict) -> dict[str, Any]:
    return {
        "ticket": _as_int(pos.get("ticket")),
        "type": str(pos.get("type") or "").strip().upper(),
        "volume": _as_float(pos.get("volume")),
        "tp": _as_float(pos.get("tp")),
        "price_open": _as_float(pos.get("price_open")),
        "time": _as_float(pos.get("time")),
    }


def snapshot_open(positions: list | tuple | None) -> dict[int, dict[str, Any]]:
    """当前在场的手动分散仓，按规则类型索引（每种规则至多一条）。"""
    out: dict[int, dict[str, Any]] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        rt = parse_rule_type(p.get("comment"))
        if rt is None:
            continue
        out[rt] = _snapshot_from_position(p)
    return out


def snapshot_from_order(
    rule_type: int, direction: str, volume: float, *, ticket: object,
    take_profit: float, price: float, opened_at: float,
) -> dict[str, Any]:
    """刚下完单、还没等到下一拍持仓快照时先登记一条，让随事件落库的 runtime 带上票号。"""
    return {
        "ticket": _as_int(ticket),
        "type": str(direction or "").strip().upper(),
        "volume": _as_float(volume),
        "tp": _as_float(take_profit),
        "price_open": _as_float(price),
        "time": _as_float(opened_at),
    }


def gone_manual(
    tracked: dict[int, dict] | None, current: dict[int, dict] | None,
) -> dict[int, dict[str, Any]]:
    """上一拍在、这一拍不在的手动仓。同规则只会有一条，按规则类型比对即可。"""
    now = current or {}
    return {
        int(rt): dict(snap) for rt, snap in (tracked or {}).items()
        if int(rt) not in now and isinstance(snap, dict)
    }


def runtime_open_payload(tracked: dict[int, dict] | None) -> dict[str, dict[str, Any]]:
    """写进 runtime 的在场手动仓；键转字串以便 JSON 落库。"""
    out: dict[str, dict[str, Any]] = {}
    for rt, snap in (tracked or {}).items():
        if not isinstance(snap, dict):
            continue
        out[str(int(rt))] = {
            "ticket": _as_int(snap.get("ticket")),
            "type": str(snap.get("type") or ""),
            "volume": _as_float(snap.get("volume")),
            "tp": _as_float(snap.get("tp")),
            "price_open": _as_float(snap.get("price_open")),
            "time": _as_float(snap.get("time")),
        }
    return out


def parse_runtime_open(raw: object) -> dict[int, dict[str, Any]]:
    """从服务端落库的 runtime 还原在场手动仓；脏数据一律丢弃。"""
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, snap in raw.items():
        rt = _as_int(key, -1)
        if rt not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) or not isinstance(snap, dict):
            continue
        parsed = _snapshot_from_position(snap)
        if parsed["ticket"] <= 0:
            continue
        out[rt] = parsed
    return out


def exit_lookup_since(snap: dict, started_at: float) -> float:
    """查出场成交的起点：取开仓时间与任务启动时间较早者，再往前留钟面偏移余量。

    窗口偏早只是多扫几条（已按魔术号过滤）；偏晚会漏掉成交、误判成非止盈。
    """
    opened = _as_float(snap.get("time"))
    started = _as_float(started_at)
    candidates = [t for t in (opened, started) if t > 0]
    base = min(candidates) if candidates else 0.0
    return max(1.0, base - EXIT_LOOKUP_MARGIN_SEC) if base > 0 else 0.0


def deal_exit_reason(deals: list | tuple | None, ticket: object) -> Optional[int]:
    """出场成交里找该持仓（position_id）的 DEAL_REASON；找不到返回 None。

    分批出场会有多笔，任一笔是止盈即按止盈算。
    """
    target = _as_int(ticket)
    if target <= 0:
        return None
    found: list[int] = []
    for d in deals or []:
        if not isinstance(d, dict):
            continue
        if _as_int(d.get("position_id")) != target:
            continue
        found.append(_as_int(d.get("reason"), -1))
    if not found:
        return None
    return REASON_TP if REASON_TP in found else found[-1]


def close_side_price(
    direction: str, *, bid: float = 0.0, ask: float = 0.0, fallback: float = 0.0,
) -> float:
    """平仓侧报价：BUY=bid，SELL=ask——券商正是按这一侧触发止盈。"""
    side = str(direction or "").strip().upper()
    if side == "BUY":
        return bid if bid > 0 else fallback
    if side == "SELL":
        return ask if ask > 0 else fallback
    return fallback


def resolve_exit(
    snap: dict, deals: list | tuple | None, *,
    bid: float = 0.0, ask: float = 0.0, fallback: float = 0.0,
) -> Optional[dict[str, Any]]:
    """判定已消失手动仓的离场原因。

    返回 {"exit": tp|other, "reason": DEAL_REASON 或 None, "source": deal|price}；
    成交历史里还没有、且平仓侧现价也没到止盈时返回 None（调用方下一拍再试）。
    """
    reason = deal_exit_reason(deals, snap.get("ticket"))
    if reason is not None:
        return {
            "exit": EXIT_TP if reason == REASON_TP else EXIT_OTHER,
            "reason": reason,
            "source": "deal",
        }
    direction = str(snap.get("type") or "")
    price = close_side_price(direction, bid=bid, ask=ask, fallback=fallback)
    if tp_passed(direction, price, _as_float(snap.get("tp"))):
        return {"exit": EXIT_TP, "reason": None, "source": "price"}
    return None


def exit_label(reason: Optional[int]) -> str:
    if reason is None:
        return "未知"
    return _EXIT_LABELS.get(int(reason), f"原因 {int(reason)}")


def describe_tp_close(rule_type: int, snap: dict, *, remaining: int) -> str:
    label = "逆势" if int(rule_type or 0) == RULE_TYPE_COUNTER else "顺势"
    side = str(snap.get("type") or "").upper() or "?"
    return (
        f"手动分散仓（{label}）止盈离场，联动清仓："
        f"{side} {_trim(snap.get('volume'))} 手 #{_as_int(snap.get('ticket'))}"
        f" 止盈 {_trim(snap.get('tp'))}；平掉该信号其余 {int(remaining)} 笔持仓"
    )


def tp_close_detail(
    rule_type: int, snap: dict, *, remaining: int, source: str,
) -> dict[str, Any]:
    return {
        "kind": DETAIL_KIND,
        "rule_type": int(rule_type or 0),
        "direction": str(snap.get("type") or "").upper() or None,
        "ticket": _as_int(snap.get("ticket")) or None,
        "volume": _as_float(snap.get("volume")),
        "entry_price": _as_float(snap.get("price_open")) or None,
        "take_profit": _as_float(snap.get("tp")) or None,
        "exit_reason": EXIT_TP,
        "exit_source": str(source or ""),
        "linked_close": True,
        "remaining": int(remaining),
        "comment": comment_for(rule_type),
    }


def _trim(value: object, digits: int = 4) -> str:
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0"
    text = f"{num:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def volume_formula(
    *,
    cfg: dict,
    volume: float,
    current_lot_size: float,
    target_pl: Optional[float],
    profit_per_lot_value: float,
    volume_step: float = 0.01,
) -> str:
    """手数怎么来的：手填锁定 / 分档反推 / 配置手数。"""
    src = normalize(cfg)
    sized = _trim(volume)
    locked = bool(src.get("volume_locked") and src["volume"] > 0)
    if locked:
        filled = _trim(src["volume"])
        if abs(src["volume"] - volume) > 1e-9:
            return f"手数 = 手填锁定 {filled}，按品种步长取整为 {sized}（不按分档反推）"
        return f"手数 = 手填锁定 {sized}（不按分档反推）"
    if target_pl is not None and profit_per_lot_value > 0:
        raw = abs(float(target_pl)) / profit_per_lot_value
        step = volume_step if volume_step > 0 else 0.01
        return (
            f"手数 = 分档目标 {_trim(abs(float(target_pl)), 2)}u"
            f" ÷ 每手止盈 {_trim(profit_per_lot_value, 4)}u"
            f" = {_trim(raw, 4)}，向上取整到步长 {_trim(step, 4)}"
            f" → {sized}；当前仓 {_trim(current_lot_size)} 手命中该档"
        )
    if src["volume"] > 0:
        return (
            f"手数 = 配置 {_trim(src['volume'])}"
            f"（分档未启用或当前仓 {_trim(current_lot_size)} 手未命中），取整为 {sized}"
        )
    return f"手数 {sized}"


def describe_open(
    watch: dict, volume: float, *, target_pl: Optional[float],
    current_lot_size: float, profit_per_lot_value: float = 0.0,
    volume_step: float = 0.01,
) -> str:
    cfg = watch.get("cfg") if isinstance(watch.get("cfg"), dict) else {}
    side = str(watch.get("direction") or "")
    label = "逆势" if int(watch.get("rule_type") or 0) == RULE_TYPE_COUNTER else "顺势"
    bits = [
        f"手动分散仓（{label}）：{side} {_trim(volume)} 手",
        volume_formula(
            cfg=cfg, volume=volume, current_lot_size=current_lot_size,
            target_pl=target_pl, profit_per_lot_value=profit_per_lot_value,
            volume_step=volume_step,
        ),
        f"入场 {_trim(cfg.get('entry_price'))}",
        f"止盈 {_trim(cfg.get('take_profit'))}",
    ]
    sl = _as_float(cfg.get("stop_loss"))
    bits.append(f"止损 {_trim(sl) if sl else '不设'}")
    if bool(cfg.get("close_all_on_tp")):
        bits.append("止盈离场后联动清仓")
    return "；".join(bits)


def open_detail(watch: dict, volume: float, *, target_pl: Optional[float],
                current_lot_size: float, profit_per_lot_value: float,
                volume_step: float = 0.01) -> dict[str, Any]:
    cfg = watch.get("cfg") if isinstance(watch.get("cfg"), dict) else {}
    src = normalize(cfg)
    locked = bool(src.get("volume_locked") and src["volume"] > 0)
    used_target = None if locked else target_pl
    return {
        "kind": DETAIL_KIND,
        "rule_type": int(watch.get("rule_type") or 0),
        "direction": watch.get("direction"),
        "entry_price": cfg.get("entry_price"),
        "take_profit": cfg.get("take_profit"),
        "stop_loss": cfg.get("stop_loss") or None,
        "volume": volume,
        "volume_locked": locked,
        "volume_formula": volume_formula(
            cfg=cfg, volume=volume, current_lot_size=current_lot_size,
            target_pl=used_target,
            profit_per_lot_value=profit_per_lot_value,
            volume_step=volume_step,
        ),
        "target_pl": used_target,
        "current_lot": current_lot_size,
        "profit_per_lot": profit_per_lot_value,
        "volume_step": volume_step,
        "comment": watch.get("comment"),
    }
