"""模版1 手动分散仓：配置抽取、手数反推、到价判定与挂限价单。

与分批加仓隔离：不进 batch_levels，MT5 注释用 M1/M2（禁止 R3B）。

入场走**挂限价单**：分批加满后把手动仓挂成 Buy Limit / Sell Limit 等成交，
不再由节点盯价市价开。挂单价固定在配置入场价上，手数在挂出那一刻算死
（按存量仓位到止盈时刻的预估亏损反推），所以价格中途怎么走都不影响
止盈时刻的净额。到价即由终端撮合成交，成交后手动仓的止盈价成为本魔术号
所有仓位的止盈权威。

成交侧已越过止盈则不开。止盈联动清仓（close_all_on_tp）：跟踪在场手动仓，
某一拍消失后查出场成交的 DEAL_REASON；确认是止盈（历史未到时用平仓侧现价兜底）
就平掉该魔术号其余持仓收口。
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
# 手动仓挂出限价单的事件（库字段 VARCHAR(16)）
PENDING_EVENT = "manual_pending"
# 手动仓限价单成交的事件（库字段 VARCHAR(16)）
PENDING_FILLED_EVENT = "manual_filled"
# 止盈联动清仓事件（库字段 VARCHAR(16)）
TP_CLOSE_EVENT = "manual_tp_close"
# 手动仓止盈价同步到其它持仓的事件
TP_ALIGN_EVENT = "manual_tp_align"
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


def pending_kind(direction: str, price: float, entry: float) -> Optional[str]:
    """手动仓该挂哪种单：限价单还是止损单。

    手动单的成交价必须**正好等于**配置入场价 —— 手数是按这个价反推的，
    成交价偏一点，止盈时刻的净额就对不上目标金额。

    所以按现价在入场价的哪一侧选单：
    - BUY 现价高于入场 → Buy Limit，等价格回落到入场
    - BUY 现价低于入场 → Buy Stop，等价格反弹回入场
    - SELL 现价低于入场 → Sell Limit，等价格反弹到入场
    - SELL 现价高于入场 → Sell Stop，等价格回落到入场

    两种单都在入场价上成交，区别只是等待的方向。
    """
    side = str(direction or "").strip().upper()
    p = _as_float(price)
    e = _as_float(entry)
    if side not in ("BUY", "SELL") or p <= 0 or e <= 0:
        return None
    if side == "BUY":
        if p > e + 1e-12:
            return "limit"   # 现价在上方，等回落
        if p < e - 1e-12:
            return "stop"    # 现价在下方，等反弹
        return None
    if p < e - 1e-12:
        return "limit"       # 现价在下方，等反弹
    if p > e + 1e-12:
        return "stop"        # 现价在上方，等回落
    return None


def pending_entry_ok(direction: str, entry: float, price: float) -> bool:
    """现价与入场价是否已经错开到可以挂单。

    挂单要求价格严格落在现价的一侧；现价正好压在入场价上时两种单都挂不上
    （限价单要低于/高于现价，止损单要高于/低于现价），此时返回 False。
    """
    return pending_kind(direction, price, entry) is not None


def pending_comment(rule_type: int) -> str:
    """挂单备注。与持仓的 M1/M2 同前缀，便于成交后从持仓认出是手动仓。"""
    return comment_for(rule_type)


def is_manual_pending(order: object) -> bool:
    """这张挂单是不是手动分散仓挂出去的。

    限价单与止损单都算：手动仓按现价在入场价的哪一侧决定挂哪种单，
    两种单都是在入场价上成交。
    """
    if not isinstance(order, dict):
        return False
    if parse_rule_type(order.get("comment")) is None:
        return False
    return str(order.get("pending_kind") or "limit").strip().lower() in ("limit", "stop")


def pending_rule_type(order: object) -> Optional[int]:
    """挂单属于哪条规则的手动仓；不是手动单返回 None。"""
    if not is_manual_pending(order):
        return None
    return parse_rule_type(order.get("comment"))


def pending_volume(order: object) -> float:
    """挂单的挂出量（部分成交后取剩余量）。"""
    if not isinstance(order, dict):
        return 0.0
    return _as_float(order.get("volume"))


def pending_price(order: object) -> float:
    """挂单价格。"""
    if not isinstance(order, dict):
        return 0.0
    return _as_float(order.get("price_open"))


def pending_ticket(order: object) -> int:
    """挂单票号。"""
    if not isinstance(order, dict):
        return 0
    return _as_int(order.get("ticket"))


def sl_is_sendable(
    direction: str, stop_loss: float, entry: float, *,
    stops_level: int = 0, point: float = 0.0,
) -> bool:
    """这个止损价能不能挂到限价单上。

    限价单带止损时，止损必须落在**入场价的反方向**且离入场价足够远：
    - BUY 限价单的止损要低于入场价（买进来后往下才是亏）
    - SELL 限价单的止损要高于入场价

    券商拒掉的是整张单（error 130 invalid stops），所以条件不满足就不带止损，
    宁可少一个保护位，也不能让这张单挂不上去。
    """
    side = str(direction or "").strip().upper()
    sl = _as_float(stop_loss)
    e = _as_float(entry)
    if side not in ("BUY", "SELL") or sl <= 0 or e <= 0:
        return False
    if side == "BUY" and sl >= e:
        return False
    if side == "SELL" and sl <= e:
        return False
    level = max(0, int(stops_level))
    step = _as_float(point, 0.0)
    if level > 0 and step > 0 and abs(e - sl) < level * step:
        return False
    return True


def describe_pending(
    watch: dict, volume: float, *, target_pl: Optional[float],
    current_lot_size: float, profit_per_lot_value: float = 0.0,
    volume_step: float = 0.01, projected_loss: float = 0.0,
) -> str:
    """挂出限价单的说明文案：手数怎么来的 + 挂在哪 + 止盈怎么对齐。"""
    cfg = watch.get("cfg") if isinstance(watch.get("cfg"), dict) else {}
    side = str(watch.get("direction") or "")
    label = "逆势" if int(watch.get("rule_type") or 0) == RULE_TYPE_COUNTER else "顺势"
    sl = _as_float(cfg.get("stop_loss"))
    bits = [
        f"手动分散仓（{label}）挂出限价单：{side} {_trim(volume)} 手",
        volume_formula(
            cfg=cfg, volume=volume, current_lot_size=current_lot_size,
            target_pl=target_pl, profit_per_lot_value=profit_per_lot_value,
            volume_step=volume_step, projected_loss=projected_loss,
        ),
        f"挂单价 {_trim(cfg.get('entry_price'))}",
        f"止盈 {_trim(cfg.get('take_profit'))}（成交后统一同步到全部持仓）",
    ]
    bits.append(f"止损 {_trim(sl) if sl else '不设'}")
    return "；".join(bits)


def pending_detail(
    watch: dict, volume: float, *, target_pl: Optional[float],
    current_lot_size: float, profit_per_lot_value: float,
    volume_step: float = 0.01, projected_loss: float = 0.0,
    ticket: object = None, kind: Optional[str] = None,
) -> dict[str, Any]:
    """挂单事件的明细：和开仓明细同构，另加挂单票号与挂单类型。"""
    cfg = watch.get("cfg") if isinstance(watch.get("cfg"), dict) else {}
    src = normalize(cfg)
    locked = bool(src.get("volume_locked") and src["volume"] > 0)
    used_target = None if locked else target_pl
    return {
        **open_detail(
            watch, volume, target_pl=used_target,
            current_lot_size=current_lot_size,
            profit_per_lot_value=profit_per_lot_value,
            volume_step=volume_step, projected_loss=projected_loss,
        ),
        "pending_ticket": _as_int(ticket) or None,
        "pending_kind": str(kind or ""),
        "pending_action": "place",
    }


def describe_pending_filled(rule_type: int, snap: dict, *, aligned: int) -> str:
    """限价单成交后的说明。"""
    label = "逆势" if int(rule_type or 0) == RULE_TYPE_COUNTER else "顺势"
    side = str(snap.get("type") or "").upper() or "?"
    return (
        f"手动分散仓（{label}）限价单成交：{side} {_trim(snap.get('volume'))} 手 "
        f"#{_as_int(snap.get('ticket'))} 开在 {_trim(snap.get('price_open'))}；"
        f"止盈价已同步到 {int(aligned)} 笔持仓"
    )


def pending_filled_detail(
    rule_type: int, snap: dict, *, aligned: int,
) -> dict[str, Any]:
    """成交事件的明细。"""
    return {
        "kind": DETAIL_KIND,
        "rule_type": int(rule_type or 0),
        "direction": str(snap.get("type") or "").upper() or None,
        "ticket": _as_int(snap.get("ticket")) or None,
        "volume": _as_float(snap.get("volume")),
        "entry_price": _as_float(snap.get("price_open")) or None,
        "take_profit": _as_float(snap.get("tp")) or None,
        "aligned_tickets": int(aligned),
        "pending_action": "filled",
        "comment": comment_for(rule_type),
    }


def tp_is_sendable(
    direction: str, take_profit: float, market: float, *,
    stops_level: int = 0, point: float = 0.0,
) -> bool:
    """这个止盈价能不能写进订单：方向在现价正确一侧，且离现价不近于券商的 stops_level。

    券商拒掉的是**整张单**，而不只是丢掉止盈这一侧，所以宁可这次不写。
    """
    side = str(direction or "").strip().upper()
    px = _as_float(take_profit)
    mkt = _as_float(market)
    if side not in ("BUY", "SELL") or px <= 0 or mkt <= 0:
        return False
    if side == "BUY" and px <= mkt:
        return False
    if side == "SELL" and px >= mkt:
        return False
    level = max(0, int(stops_level))
    step = _as_float(point, 0.0)
    if level > 0 and step > 0 and abs(px - mkt) < level * step:
        return False
    return True


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


def tier0_pl_amount(lot_pl_tiers: object) -> Optional[float]:
    """分档批次1的目标金额（绝对值）。分档未启用或金额为 0 时返回 None。

    手动分散仓按固定第一档对齐信号共同止盈的口径，不随当前手数命中高档。
    """
    src = lot_pl_tiers if isinstance(lot_pl_tiers, dict) else {}
    if not src.get("enabled"):
        return None
    tiers = src.get("tiers") if isinstance(src.get("tiers"), list) else []
    if not tiers:
        return None
    amount = abs(_as_float(tiers[0].get("pl_amount"), 0.0))
    return amount if amount > 0 else None


def position_profit_at(
    pos: dict, price: float, tick_size: float, tick_value: float,
) -> float:
    """某笔持仓在 `price` 处的浮动盈亏（不含 swap / 手续费）。

    用开仓价与目标价的距离折算，不看当下的浮盈：手动仓止盈那一刻，现价已经
    走到止盈价，存量仓位的盈亏必须按那一刻的价格重估，否则算出来的手数
    到价时对不上目标金额。
    """
    side = str(pos.get("type") or "").upper()
    vol = _as_float(pos.get("volume"))
    open_price = _as_float(pos.get("price_open"))
    px = _as_float(price)
    if side not in ("BUY", "SELL") or vol <= 0 or open_price <= 0 or px <= 0:
        return 0.0
    size = _as_float(tick_size)
    value = _as_float(tick_value)
    if size <= 0 or value <= 0:
        return 0.0
    diff = (px - open_price) if side == "BUY" else (open_price - px)
    return diff * vol * (value / size)


def projected_profit(
    positions: list | tuple | None, price: float, tick_size: float, tick_value: float,
) -> float:
    """全部持仓在 `price` 处的浮动盈亏合计。"""
    total = 0.0
    for pos in positions or []:
        if isinstance(pos, dict):
            total += position_profit_at(pos, price, tick_size, tick_value)
    return total


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
    projected_loss: float = 0.0,
) -> Optional[float]:
    """返回开仓手数；无法计算返回 None。

    volume_locked 且 volume>0 时直接用配置手数（仍夹到 min/max/step）。
    目标金额 = 分档批次1的 pl_amount + 存量仓位到止盈时刻的预估亏损，
    手动仓止盈那一刻净额刚好等于目标金额。
    """
    src = normalize(cfg)
    if src.get("volume_locked") and src["volume"] > 0:
        return ceil_to_step(
            src["volume"], volume_step, volume_min, volume_max,
        )
    target = tier0_pl_amount(lot_pl_tiers)
    if target is None:
        if src["volume"] > 0:
            return ceil_to_step(src["volume"], volume_step, volume_min, volume_max)
        return None
    per = profit_per_lot(src["entry_price"], src["take_profit"], tick_size, tick_value)
    if per <= 0:
        return None
    raw = (abs(target) + max(0.0, float(projected_loss or 0.0))) / per
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
            # 该规则的分批加仓是否启用；未启用时不设「先加完」的门槛
            "batch_enabled": bool(raw.get("batch_enabled")),
            # 原始规则，用于判定「本规则分批加仓是否已加完」
            "rule": raw,
        })
    return out


# ----------------------------------------------------------------------
# 先后顺序：手动分散仓要等本规则的分批加仓先加完
# ----------------------------------------------------------------------
def batch_add_limit(rule: object) -> int:
    """本规则分批加仓的笔数上限。

    分批启用时用 total_lot_limit；未启用时回退到 max_allow_num。
    两者都为 0 表示不限（此时不设门槛）。
    """
    src = rule if isinstance(rule, dict) else {}
    if src.get("batch_enabled"):
        limit = _as_int(src.get("total_lot_limit"), 0)
        if limit > 0:
            return limit
        return 0
    limit = _as_int(src.get("max_allow_num"), 0)
    return limit if limit > 0 else 0


def batch_pending(rule: object) -> bool:
    """该规则的分批加仓是否还有没加完的档位。

    已加笔数达到上限才算加完。分批未启用时不算门槛（False）。
    """
    src = rule if isinstance(rule, dict) else {}
    if not src.get("batch_enabled"):
        return False
    limit = batch_add_limit(src)
    if limit <= 0:
        return False
    return True


def manual_blocked_reason(
    rule: object, added_count: int, *, pending_counter: bool = False,
) -> Optional[str]:
    """手动分散仓现在能不能开；不能开时返回原因文案。

    门槛：同一条规则的分批加仓必须先加完（已加笔数达到上限）。
    在途的逆势限价单说明那一档还没成交，同样算没加完。
    规则没启用分批加仓时不设门槛。
    """
    src = rule if isinstance(rule, dict) else {}
    if not src.get("batch_enabled"):
        return None
    limit = batch_add_limit(src)
    if limit <= 0:
        return None
    if pending_counter:
        return "本规则还有在途的逆势加仓限价单，分批尚未加完，手动分散仓暂不开仓"
    if int(added_count or 0) < limit:
        return (
            f"本规则分批加仓尚未加完（已加 {int(added_count or 0)} / {limit} 笔），"
            "手动分散仓暂不开仓"
        )
    return None


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


def runtime_pending_payload(tracked: dict[int, dict] | None) -> dict[str, dict[str, Any]]:
    """写进 runtime 的在途手动限价单；键转字串以便 JSON 落库。

    挂出后节点就不知道这张单还在不在盘上了——重连时靠这个还原，
    避免重连后重复挂一张（挂出去两次就是双倍手数）。
    """
    out: dict[str, dict[str, Any]] = {}
    for rt, snap in (tracked or {}).items():
        if not isinstance(snap, dict):
            continue
        out[str(int(rt))] = {
            "ticket": _as_int(snap.get("ticket")),
            "type": str(snap.get("type") or ""),
            "volume": _as_float(snap.get("volume")),
            "price_open": _as_float(snap.get("price_open")),
            "sl": _as_float(snap.get("sl")),
            "tp": _as_float(snap.get("tp")),
            "time": _as_float(snap.get("time")),
        }
    return out


def parse_runtime_pending(raw: object) -> dict[int, dict[str, Any]]:
    """从服务端落库的 runtime 还原在途手动限价单；脏数据一律丢弃。"""
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, snap in raw.items():
        rt = _as_int(key, -1)
        if rt not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) or not isinstance(snap, dict):
            continue
        parsed = {
            "ticket": _as_int(snap.get("ticket")),
            "type": str(snap.get("type") or "").strip().upper(),
            "volume": _as_float(snap.get("volume")),
            "price_open": _as_float(snap.get("price_open")),
            "sl": _as_float(snap.get("sl")),
            "tp": _as_float(snap.get("tp")),
            "time": _as_float(snap.get("time")),
        }
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
    projected_loss: float = 0.0,
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
        loss = max(0.0, float(projected_loss or 0.0))
        if loss > 0:
            return (
                f"手数 = (分档目标 {_trim(abs(float(target_pl)), 2)}u"
                f" + 存量仓位到止盈预估亏损 {_trim(loss, 2)}u)"
                f" ÷ 每手止盈 {_trim(profit_per_lot_value, 4)}u"
                f" = {_trim(raw, 4)}，向上取整到步长 {_trim(step, 4)}"
                f" → {sized}；当前仓 {_trim(current_lot_size)} 手"
            )
        return (
            f"手数 = 分档目标 {_trim(abs(float(target_pl)), 2)}u"
            f" ÷ 每手止盈 {_trim(profit_per_lot_value, 4)}u"
            f" = {_trim(raw, 4)}，向上取整到步长 {_trim(step, 4)}"
            f" → {sized}；当前仓 {_trim(current_lot_size)} 手命中该档"
        )
    if src["volume"] > 0:
        return (
            f"手数 = 配置 {_trim(src['volume'])}"
            f"（分档未启用），取整为 {sized}"
        )
    return f"手数 {sized}"


def describe_open(
    watch: dict, volume: float, *, target_pl: Optional[float],
    current_lot_size: float, profit_per_lot_value: float = 0.0,
    volume_step: float = 0.01, projected_loss: float = 0.0,
) -> str:
    cfg = watch.get("cfg") if isinstance(watch.get("cfg"), dict) else {}
    side = str(watch.get("direction") or "")
    label = "逆势" if int(watch.get("rule_type") or 0) == RULE_TYPE_COUNTER else "顺势"
    bits = [
        f"手动分散仓（{label}）：{side} {_trim(volume)} 手",
        volume_formula(
            cfg=cfg, volume=volume, current_lot_size=current_lot_size,
            target_pl=target_pl, profit_per_lot_value=profit_per_lot_value,
            volume_step=volume_step, projected_loss=projected_loss,
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
                volume_step: float = 0.01, projected_loss: float = 0.0) -> dict[str, Any]:
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
            projected_loss=projected_loss,
        ),
        "target_pl": used_target,
        "current_lot": current_lot_size,
        "profit_per_lot": profit_per_lot_value,
        "volume_step": volume_step,
        "projected_loss": round(float(projected_loss or 0.0), 2),
        "comment": watch.get("comment"),
    }
