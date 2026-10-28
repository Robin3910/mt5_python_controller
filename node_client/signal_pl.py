"""模版1 信号级盈亏控制纯函数：配置抽取与止盈止损价。

不再按浮盈采样平仓。分档手数只用批次1金额反推共同止盈；
信号盈亏比只用比例绝对值 × 账户余额反推共同止损。改单由调用方完成。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

import account_risk as ar

RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2

RULE_FLOAT_PL_RATIO = "float_pl_ratio"
RULE_LOT_PL_TIERS = "lot_pl_tiers"

ACTION_CLOSE_ALL = "close_all"
MONITOR_LOOP = "loop"
MONITOR_TIMES = "times"
MONITOR_MODES = (MONITOR_LOOP, MONITOR_TIMES)
CLOSE_SIDES = ("all", "buy", "sell")
MAX_TIERS = 10
REASON_LIMIT = 255

RULE_LABELS: dict[str, str] = {
    RULE_FLOAT_PL_RATIO: "信号盈亏比",
    RULE_LOT_PL_TIERS: "信号分档手数盈亏",
}


def default_float_pl_ratio() -> dict[str, Any]:
    return {
        "enabled": False,
        "ratio": -20.0,
        "action": ACTION_CLOSE_ALL,
        "monitor_mode": MONITOR_LOOP,
        "max_times": 1,
        "remaining_times": 0,
    }


def default_lot_pl_tiers() -> dict[str, Any]:
    return {
        "enabled": False,
        "batch_count": 2,
        "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 50.0},
            {"min_lot": 0.5, "pl_amount": 100.0},
        ],
    }


def default_config() -> dict[str, Any]:
    return {
        RULE_FLOAT_PL_RATIO: default_float_pl_ratio(),
        RULE_LOT_PL_TIERS: default_lot_pl_tiers(),
    }


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_float_pl_ratio(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_float_pl_ratio()
    mode = str(src.get("monitor_mode") or defaults["monitor_mode"]).strip().lower()
    if mode not in MONITOR_MODES:
        mode = defaults["monitor_mode"]
    max_times = max(1, _as_int(src.get("max_times", defaults["max_times"]), defaults["max_times"]))
    remaining = max(0, _as_int(src.get("remaining_times", 0), 0))
    enabled = bool(src.get("enabled", defaults["enabled"]))
    if enabled and mode == MONITOR_TIMES and remaining <= 0:
        remaining = max_times
    if mode == MONITOR_LOOP:
        remaining = 0
    if not enabled:
        remaining = 0
    return {
        "enabled": enabled,
        "ratio": _as_float(src.get("ratio", defaults["ratio"]), defaults["ratio"]),
        "action": ACTION_CLOSE_ALL,
        "monitor_mode": mode,
        "max_times": max_times,
        "remaining_times": remaining,
    }


def normalize_lot_pl_tiers(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_lot_pl_tiers()
    batch_count = _as_int(src.get("batch_count", defaults["batch_count"]), defaults["batch_count"])
    batch_count = max(1, min(MAX_TIERS, batch_count))
    close_action = str(src.get("close_action") or defaults["close_action"]).strip().lower()
    if close_action not in CLOSE_SIDES:
        close_action = defaults["close_action"]
    old_tiers = src.get("tiers") if isinstance(src.get("tiers"), list) else []
    default_tiers = defaults["tiers"]
    tiers: list[dict[str, Any]] = []
    for i in range(batch_count):
        prev = old_tiers[i] if i < len(old_tiers) and isinstance(old_tiers[i], dict) else {}
        fallback = default_tiers[i] if i < len(default_tiers) else {}
        min_lot = max(
            0.0,
            _as_float(
                prev.get("min_lot", fallback.get("min_lot", 0.1 * (i + 1))),
                float(fallback.get("min_lot", 0.1 * (i + 1))),
            ),
        )
        pl_amount = _as_float(
            prev.get("pl_amount", fallback.get("pl_amount", 50.0 * (i + 1))),
            float(fallback.get("pl_amount", 50.0 * (i + 1))),
        )
        tiers.append({"min_lot": min_lot, "pl_amount": pl_amount})
    return {
        "enabled": bool(src.get("enabled", defaults["enabled"])),
        "batch_count": batch_count,
        "close_action": close_action,
        "tiers": tiers,
    }


def normalize_config(raw: dict | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {
        RULE_FLOAT_PL_RATIO: normalize_float_pl_ratio(src.get(RULE_FLOAT_PL_RATIO)),
        RULE_LOT_PL_TIERS: normalize_lot_pl_tiers(src.get(RULE_LOT_PL_TIERS)),
    }


def _is_addon_rule(rule: object) -> bool:
    if not isinstance(rule, dict):
        return False
    return _as_int(rule.get("type"), 0) in (RULE_TYPE_COUNTER, RULE_TYPE_TREND)


def extract_config(rules: object) -> dict[str, Any]:
    """从加仓规则抽出信号级盈亏配置。优先已启用的加仓规则，否则第一条 type 1/2。"""
    if not isinstance(rules, list):
        return default_config()
    addon = [r for r in rules if _is_addon_rule(r)]
    if not addon:
        return default_config()
    enabled = [r for r in addon if _as_int(r.get("status"), 0)]
    picked = enabled[0] if enabled else addon[0]
    return normalize_config({
        RULE_FLOAT_PL_RATIO: picked.get(RULE_FLOAT_PL_RATIO),
        RULE_LOT_PL_TIERS: picked.get(RULE_LOT_PL_TIERS),
    })


def prepare_runtime(cfg: dict | None) -> dict[str, Any]:
    """任务启动时把指定次数的剩余次数灌进内存态。"""
    out = normalize_config(cfg)
    rule = dict(out[RULE_FLOAT_PL_RATIO])
    if rule["enabled"] and rule["monitor_mode"] == MONITOR_TIMES:
        rule["remaining_times"] = int(rule.get("max_times") or 1)
    else:
        rule["remaining_times"] = 0
    out[RULE_FLOAT_PL_RATIO] = rule
    return out


def any_enabled(cfg: dict | None) -> bool:
    src = cfg if isinstance(cfg, dict) else {}
    pl = src.get(RULE_FLOAT_PL_RATIO) if isinstance(src.get(RULE_FLOAT_PL_RATIO), dict) else {}
    lpt = src.get(RULE_LOT_PL_TIERS) if isinstance(src.get(RULE_LOT_PL_TIERS), dict) else {}
    return bool(pl.get("enabled") or lpt.get("enabled"))


def signal_floating_pl_ratio(balance: float, signal_profit: float) -> float | None:
    """该信号浮盈亏 / 账户余额 × 100%。余额 ≤ 0 时无法计算。"""
    try:
        bal = float(balance)
        profit = float(signal_profit)
    except (TypeError, ValueError):
        return None
    if bal <= 0:
        return None
    return profit / bal * 100.0


def _float_pl_ready(rule: dict) -> bool:
    if not rule.get("enabled"):
        return False
    if rule.get("monitor_mode") == MONITOR_TIMES and int(rule.get("remaining_times") or 0) <= 0:
        return False
    return True


def should_trigger_float_pl(
    cfg: dict | None,
    *,
    balance: float,
    signal_profit: float,
    has_positions: bool,
) -> dict | None:
    src = normalize_config(cfg)
    # normalize_config 会按配置态重置 remaining_times；运行态必须沿用调用方内存里的次数
    if isinstance(cfg, dict) and isinstance(cfg.get(RULE_FLOAT_PL_RATIO), dict):
        src[RULE_FLOAT_PL_RATIO]["remaining_times"] = max(
            0, _as_int(cfg[RULE_FLOAT_PL_RATIO].get("remaining_times"), 0),
        )
        src[RULE_FLOAT_PL_RATIO]["enabled"] = bool(cfg[RULE_FLOAT_PL_RATIO].get("enabled"))
    rule = src[RULE_FLOAT_PL_RATIO]
    if not _float_pl_ready(rule) or not has_positions:
        return None
    current = signal_floating_pl_ratio(balance, signal_profit)
    if current is None:
        return None
    threshold = float(rule["ratio"])
    if not ar.ratio_triggered(current, threshold):
        return None
    return {
        "rule": RULE_FLOAT_PL_RATIO,
        "close_action": "all",
        "threshold": threshold,
        "current_ratio": current,
        "floating_pl": float(signal_profit),
        "balance": float(balance),
        "action": ACTION_CLOSE_ALL,
        "monitor_mode": rule.get("monitor_mode"),
        "max_times": int(rule.get("max_times") or 1),
        "remaining_times": int(rule.get("remaining_times") or 0),
        "message_core": f"浮盈亏比 {current:.2f}% 达到阈值 {threshold}%",
    }


def float_pl_enabled(cfg: dict | None) -> bool:
    return bool(normalize_config(cfg)[RULE_FLOAT_PL_RATIO].get("enabled"))


def float_pl_loss_amount(cfg: dict | None, balance: float) -> Optional[float]:
    """启用时把盈亏比绝对值换成亏损金额：|比例|% × 账户余额。"""
    if not float_pl_enabled(cfg):
        return None
    ratio = abs(_as_float(normalize_config(cfg)[RULE_FLOAT_PL_RATIO].get("ratio"), 0.0))
    bal = _as_float(balance, 0.0)
    if ratio <= 0 or bal <= 0:
        return None
    return bal * ratio / 100.0


def lot_pl_amount(cfg: dict | None) -> Optional[float]:
    """启用时返回批次1盈亏金额的绝对值。未启用、无档或金额为 0 时返回 None。"""
    rule = normalize_config(cfg)[RULE_LOT_PL_TIERS]
    if not rule.get("enabled"):
        return None
    tiers = rule.get("tiers") or []
    if not tiers:
        return None
    amount = abs(_as_float(tiers[0].get("pl_amount"), 0.0))
    if amount <= 0:
        return None
    return amount


def lot_pl_min_lot(cfg: dict | None) -> float:
    """启用时返回批次1的总手数门槛。未启用时返回 0，表示不挡止盈。"""
    rule = normalize_config(cfg)[RULE_LOT_PL_TIERS]
    if not rule.get("enabled"):
        return 0.0
    tiers = rule.get("tiers") or []
    if not tiers:
        return 0.0
    return max(0.0, _as_float(tiers[0].get("min_lot"), 0.0))


def lot_pl_close_action(cfg: dict | None) -> str:
    action = str(normalize_config(cfg)[RULE_LOT_PL_TIERS].get("close_action") or "all").lower()
    if action not in CLOSE_SIDES:
        return "all"
    return action


def filter_lot_side(positions: list | None, close_action: str) -> list[dict]:
    """按全部 / 多单 / 空单留下要改止盈止损的持仓。"""
    action = str(close_action or "all").lower()
    out: list[dict] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        side = str(pos.get("type") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        if action == "buy" and side != "BUY":
            continue
        if action == "sell" and side != "SELL":
            continue
        out.append(pos)
    return out


def position_book_signature(positions: list | None) -> tuple:
    """票号、手数、方向。报价变化不改变这个签名。"""
    rows = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        rows.append((
            _as_int(pos.get("ticket"), 0),
            round(max(0.0, _as_float(pos.get("volume"), 0.0)), 4),
            str(pos.get("type") or "").upper(),
        ))
    return tuple(sorted(rows))


def book_has_open(previous: tuple, current: tuple) -> bool:
    """出现新票号，或同一票号手数变大，才算开仓。平仓和减仓不算。"""
    prev_vol = {ticket: vol for ticket, vol, _side in previous}
    for ticket, vol, _side in current:
        old = prev_vol.get(ticket)
        if old is None or vol > old + 1e-8:
            return True
    return False


def _price_eps(digits: int) -> float:
    step = 10 ** (-max(0, int(digits)))
    return max(step / 2, 1e-9)


def stops_match(
    pos: dict, sl: float | None, tp: float | None, digits: int,
) -> bool:
    """要改的那一侧已经是目标价。None 表示这一侧不改。"""
    if sl is None and tp is None:
        return True
    eps = _price_eps(digits)
    if sl is not None and abs(_as_float(pos.get("sl"), 0.0) - float(sl)) > eps:
        return False
    if tp is not None and abs(_as_float(pos.get("tp"), 0.0) - float(tp)) > eps:
        return False
    return True


def _round_price(price: float, digits: int) -> float:
    return round(float(price), max(0, int(digits)))


def _market_for_side(side: str, *, bid: float, ask: float, fallback: float) -> float:
    """平仓侧现价：多单看买价，空单看卖价。缺一侧时用事件价。"""
    raw = bid if side == "BUY" else ask
    price = _as_float(raw, 0.0)
    if price <= 0:
        price = _as_float(fallback, 0.0)
    return price if price > 0 else 0.0


def _cross_side(
    side: str, sl: float | None, tp: float | None, market: float,
) -> Optional[str]:
    """现价已经到了或越过要写的止盈 / 止损。没算的那一侧不参与。"""
    if market <= 0:
        return None
    if side == "BUY":
        if tp is not None and market >= tp:
            return "tp"
        if sl is not None and market <= sl:
            return "sl"
        return None
    if tp is not None and market <= tp:
        return "tp"
    if sl is not None and market >= sl:
        return "sl"
    return None


def _price_distance(amount: float, money_per_price: float) -> Optional[float]:
    target = abs(_as_float(amount, 0.0))
    if target <= 0 or money_per_price <= 0:
        return None
    return target / money_per_price


def plan_common_stops(
    positions: list | None,
    *,
    tick_size: float,
    tick_value: float,
    digits: int,
    tp_amount: float | None = None,
    sl_amount: float | None = None,
    tp_min_lot: float = 0.0,
    bid: float = 0.0,
    ask: float = 0.0,
    fallback_price: float = 0.0,
) -> list[dict]:
    """按方向算共同止盈和/或止损。

    加权开仓价 W = Σ(手数 × 开仓价) / Σ手数。
    距离 D = 金额 / ((tick_value / tick_size) × Σ手数)。
    止盈金额来自分档批次1，且该方向总手数达到 tp_min_lot 才计算；止损金额来自盈亏比 × 余额。
    多单止盈在 W 之上、止损在 W 之下；空单相反。
    """
    size = _as_float(tick_size, 0.0)
    value = _as_float(tick_value, 0.0)
    if size <= 0 or value <= 0:
        return []
    if _price_distance(tp_amount or 0, 1) is None and _price_distance(sl_amount or 0, 1) is None:
        return []
    grouped: dict[str, list[dict]] = {"BUY": [], "SELL": []}
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        side = str(pos.get("type") or "").upper()
        if side not in grouped:
            continue
        if _as_float(pos.get("volume"), 0.0) <= 0 or _as_float(pos.get("price_open"), 0.0) <= 0:
            continue
        grouped[side].append(pos)
    plans: list[dict] = []
    for side, rows in grouped.items():
        if not rows:
            continue
        volume = 0.0
        weighted = 0.0
        for pos in rows:
            vol = _as_float(pos.get("volume"), 0.0)
            volume += vol
            weighted += vol * _as_float(pos.get("price_open"), 0.0)
        money_per_price = (value / size) * volume
        if volume <= 0 or money_per_price <= 0:
            continue
        d_tp = _price_distance(tp_amount or 0, money_per_price)
        if d_tp is not None and volume + 1e-8 < max(0.0, _as_float(tp_min_lot, 0.0)):
            d_tp = None
        d_sl = _price_distance(sl_amount or 0, money_per_price)
        if d_tp is None and d_sl is None:
            continue
        anchor = weighted / volume
        if side == "BUY":
            tp = _round_price(anchor + d_tp, digits) if d_tp is not None else None
            sl = _round_price(anchor - d_sl, digits) if d_sl is not None else None
        else:
            tp = _round_price(anchor - d_tp, digits) if d_tp is not None else None
            sl = _round_price(anchor + d_sl, digits) if d_sl is not None else None
        market = _market_for_side(side, bid=bid, ask=ask, fallback=fallback_price)
        plans.append({
            "rule": RULE_LOT_PL_TIERS if d_tp is not None else RULE_FLOAT_PL_RATIO,
            "side": side,
            "sl": sl,
            "tp": tp,
            "weighted_open": anchor,
            "tp_distance": d_tp,
            "sl_distance": d_sl,
            "amount": abs(_as_float(tp_amount, 0.0)) if d_tp is not None else None,
            "sl_amount": abs(_as_float(sl_amount, 0.0)) if d_sl is not None else None,
            "volume": volume,
            "digits": max(0, int(digits)),
            "positions": rows,
            "crossed": _cross_side(side, sl, tp, market),
        })
    return plans


def describe_lot_stops(plan: dict | None) -> str:
    if not isinstance(plan, dict):
        return "信号止盈止损"
    side = "多单" if plan.get("side") == "BUY" else "空单"
    volume = _as_float(plan.get("volume"), 0.0)
    sl = plan.get("sl")
    tp = plan.get("tp")
    crossed = plan.get("crossed")
    if crossed == "tp" and tp is not None:
        text = f"信号止盈：{side}现价已越过共同止盈 {float(tp):g}，市价平仓"
    elif crossed == "sl" and sl is not None:
        text = f"信号止损：{side}现价已越过共同止损 {float(sl):g}，市价平仓"
    else:
        bits = []
        if tp is not None:
            bits.append(f"止盈 {float(tp):g}")
        if sl is not None:
            bits.append(f"止损 {float(sl):g}")
        extra = []
        if plan.get("amount") is not None:
            extra.append(f"批次1金额 {_as_float(plan.get('amount'), 0):g}")
        if plan.get("sl_amount") is not None:
            extra.append(f"盈亏比亏损 {_as_float(plan.get('sl_amount'), 0):g}")
        tail = f"（{'，'.join(extra)}，总手数 {volume:g}）" if extra else ""
        text = f"信号止盈止损：{side}共同{' '.join(bits)}{tail}"
    return text[:REASON_LIMIT]


def lot_stops_detail(plan: dict | None) -> Optional[dict]:
    if not isinstance(plan, dict):
        return None
    return {
        "kind": "signal_pl",
        "rule": RULE_LOT_PL_TIERS,
        "rule_label": RULE_LABELS[RULE_LOT_PL_TIERS],
        "side": plan.get("side"),
        "sl": plan.get("sl"),
        "tp": plan.get("tp"),
        "amount": plan.get("amount"),
        "sl_amount": plan.get("sl_amount"),
        "volume": plan.get("volume"),
        "weighted_open": plan.get("weighted_open"),
        "distance": plan.get("distance"),
        "crossed": plan.get("crossed"),
        "tier_index": 0,
    }


def find_triggered_rule(
    cfg: dict | None,
    *,
    balance: float,
    signal_profit: float,
    positions: list | None,
) -> dict | None:
    """只判定信号盈亏比。分档手数改为改写券商止盈止损，不再在这里市价平仓。"""
    return should_trigger_float_pl(
        cfg,
        balance=balance,
        signal_profit=signal_profit,
        has_positions=bool(positions),
    )


def apply_float_pl_trigger(cfg: dict) -> tuple[dict, dict]:
    """指定次数模式下扣一次剩余次数；耗尽则关掉本任务内存开关。"""
    out = deepcopy(cfg) if isinstance(cfg, dict) else default_config()
    rule = dict(out.get(RULE_FLOAT_PL_RATIO) or default_float_pl_ratio())
    disabled = False
    remaining = int(rule.get("remaining_times") or 0)
    if rule.get("monitor_mode") == MONITOR_TIMES:
        remaining = max(0, remaining - 1)
        rule["remaining_times"] = remaining
        if remaining <= 0:
            rule["enabled"] = False
            rule["remaining_times"] = 0
            disabled = True
    out[RULE_FLOAT_PL_RATIO] = rule
    return out, {"remaining_times": int(rule.get("remaining_times") or 0), "disabled": disabled}


def closes_all(hit: dict | None) -> bool:
    if not isinstance(hit, dict):
        return False
    action = str(hit.get("close_action") or hit.get("action") or "").lower()
    return action in ("all", "close_all", "account_all")


def select_close_targets(positions: list | None, hit: dict | None) -> list[dict]:
    action = str((hit or {}).get("close_action") or "all").lower()
    if action in ("close_all", "account_all"):
        action = "all"
    return ar.select_close_targets(positions, close_action=action)


def describe_trigger(hit: dict | None) -> str:
    if not isinstance(hit, dict):
        return "信号盈亏控制"
    rule = str(hit.get("rule") or "")
    label = RULE_LABELS.get(rule, rule or "信号盈亏控制")
    core = str(hit.get("message_core") or "").strip() or label
    action = hit.get("close_action") or hit.get("action") or ""
    action_label = ar.CLOSE_ACTION_LABELS.get(str(action), str(action) or "平仓")
    return f"{label}：{core}；动作 {action_label}"[:REASON_LIMIT]


def trigger_detail(hit: dict | None) -> Optional[dict]:
    if not isinstance(hit, dict):
        return None
    rule = str(hit.get("rule") or "")
    detail: dict[str, Any] = {
        "kind": "signal_pl",
        "rule": rule or None,
        "rule_label": RULE_LABELS.get(rule) or rule or None,
        "close_action": hit.get("close_action") or hit.get("action"),
        "close_action_label": ar.CLOSE_ACTION_LABELS.get(
            str(hit.get("close_action") or hit.get("action") or ""),
        ),
        "message_core": hit.get("message_core"),
        "monitor_mode": hit.get("monitor_mode"),
    }
    if rule == RULE_FLOAT_PL_RATIO:
        detail.update({
            "ratio_threshold": hit.get("threshold"),
            "current_ratio": hit.get("current_ratio"),
            "floating_pl": hit.get("floating_pl"),
            "balance": hit.get("balance"),
        })
    elif rule == RULE_LOT_PL_TIERS:
        detail.update({
            "tier_index": hit.get("tier_index"),
            "min_lot": hit.get("min_lot"),
            "pl_amount": hit.get("pl_amount"),
            "current_lot": hit.get("current_lot"),
            "current_pl": hit.get("current_pl"),
        })
    return detail
