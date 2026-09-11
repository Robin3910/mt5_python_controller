"""模版1 手动分散仓：配置抽取、手数反推与到价判定。

与分批加仓隔离：不进 batch_levels，MT5 注释用 M1/M2（禁止 R3B）。
手数：按当前任务总手数匹配 lot_pl_tiers 的 pl_amount，再按
|tp-entry| / tick_size * tick_value 反推，向上取整到 volume_step。
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2
COMMENT_PREFIX = "M"
# M1 / M2，可选后续字符；禁止与模版2 R3B 撞车
_COMMENT_RE = re.compile(r"^M([12])(?:\b|$)")
EVENT_TYPE = "add_manual"
DETAIL_KIND = "manual_scatter"


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


def price_reached(direction: str, price: float, entry: float) -> bool:
    """BUY：现价已到或越过入场（>=）；SELL：现价已到或越过入场（<=）。"""
    if entry <= 0 or price <= 0:
        return False
    side = str(direction or "").strip().upper()
    if side == "BUY":
        return price + 1e-12 >= entry
    if side == "SELL":
        return price - 1e-12 <= entry
    return False


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
