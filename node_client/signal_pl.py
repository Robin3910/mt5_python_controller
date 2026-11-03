"""模版1 信号级盈亏控制纯函数：配置抽取与止盈止损价。

不再按浮盈采样平仓。分档手数只用批次1金额反推共同止盈；
信号盈亏比只用比例绝对值 × 账户余额反推共同止损。
新开的一笔在下单时就带上这两个价；已有持仓仍由调用方改单对齐。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

import account_risk as ar
# 资金操作（出入金等）的成交类型口径见 deal_types.py。
from deal_types import FUNDING_DEAL_TYPES

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


def prepare_runtime(
    cfg: dict | None, *, keep_remaining: Optional[int] = None,
) -> dict[str, Any]:
    """任务启动时把指定次数的剩余次数灌进内存态。

    keep_remaining：热推时沿用运行态已消耗的次数（传运行态的 remaining_times）。
    传 None 表示按配置重置为 max_times（任务启动的语义）。
    """
    out = normalize_config(cfg)
    rule = dict(out[RULE_FLOAT_PL_RATIO])
    if rule["enabled"] and rule["monitor_mode"] == MONITOR_TIMES:
        if keep_remaining is None:
            rule["remaining_times"] = int(rule.get("max_times") or 1)
        else:
            # 热推沿用运行态：已消耗的次数不复活
            rule["remaining_times"] = max(0, min(int(keep_remaining), int(rule.get("max_times") or 1)))
    else:
        rule["remaining_times"] = 0
    out[RULE_FLOAT_PL_RATIO] = rule
    return out


def float_pl_remaining(cfg: dict | None) -> int:
    """当前运行态里，次数型盈亏比还剩几次可触发。"""
    src = cfg if isinstance(cfg, dict) else {}
    rule = src.get(RULE_FLOAT_PL_RATIO)
    if not isinstance(rule, dict):
        return 0
    if rule.get("monitor_mode") != MONITOR_TIMES:
        return 0
    return max(0, int(rule.get("remaining_times") or 0))


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


def side_crossed(side: str, kind: str, value: float | None, market: float) -> bool:
    """单独判这一侧（tp / sl）是否已被现价越过。"""
    if value is None or market <= 0:
        return False
    px = float(value)
    if px <= 0:
        return False
    if kind == "tp":
        return market >= px if side == "BUY" else market <= px
    if kind == "sl":
        return market <= px if side == "BUY" else market >= px
    return False


def stop_side_decision(
    side: str, kind: str, *,
    new_value: float | None, positions: list | None, market: float,
    allow_keep: bool = True,
) -> dict:
    """新算出的这一侧该不该写。

    判据是**持仓上已有的值**，不是「这是不是一次配置改动」：
    - 旧值还没被现价越过、新值却越了 → 配置把目标调得离现价太近，
      保留旧值（方案 2b）；
    - 旧值也已被越过 → 价格事件，止盈止损本来就该被打掉；
    - 本来就没有旧值 → 没有可保留的东西。

    allow_keep=False 时不走 2b：新值越价一律市价平掉。这是**加仓**场景的
    既有口径——加仓后重算出的止盈落到现价之下，说明行情已经走过去了，
    按原逻辑平掉；2b 只针对**配置改动**这一类。

    返回 {"action": "none"|"write"|"keep"|"close", "has_old", "old_crossed",
    "old_value"}。old_value 取「最容易先被触发」的那个极值。
    """
    if new_value is None or float(new_value) <= 0:
        return {"action": "none", "has_old": False, "old_crossed": False, "old_value": None}
    if not side_crossed(side, kind, float(new_value), market):
        return {"action": "write", "has_old": False, "old_crossed": False, "old_value": None}
    olds: list[float] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        val = _as_float(pos.get(kind), 0.0)
        if val > 0:
            olds.append(val)
    if not olds:
        return {"action": "close", "has_old": False, "old_crossed": False, "old_value": None}
    old_crossed = any(side_crossed(side, kind, val, market) for val in olds)
    if old_crossed or not allow_keep:
        return {
            "action": "close", "has_old": bool(olds), "old_crossed": old_crossed,
            "old_value": None,
        }
    # 保留旧值：取最容易先被触发的那一个
    if kind == "tp":
        old_value = min(olds) if side == "BUY" else max(olds)
    else:
        old_value = max(olds) if side == "BUY" else min(olds)
    return {
        "action": "keep", "has_old": True, "old_crossed": False, "old_value": old_value,
    }


def lot_pl_cfg_sig(cfg: dict | None) -> tuple:
    """止盈配置指纹：(开关, 批次1金额, 批次1门槛, 平仓方向)。

    改了就要重算共同止盈——与手数是否变化无关。
    """
    rule = normalize_config(cfg)[RULE_LOT_PL_TIERS]
    tiers = rule.get("tiers") or []
    first = tiers[0] if tiers else {}
    return (
        bool(rule.get("enabled")),
        round(abs(_as_float(first.get("pl_amount"), 0.0)), 6),
        round(_as_float(first.get("min_lot"), 0.0), 6),
        str(rule.get("close_action") or "all"),
    )


def float_pl_enabled(cfg: dict | None) -> bool:
    return bool(normalize_config(cfg)[RULE_FLOAT_PL_RATIO].get("enabled"))


def float_pl_ratio_value(cfg: dict | None) -> float:
    """启用时返回盈亏比阈值的原始值（带符号）；未启用返回 0。

    用于给止损配置做指纹：比例或开关变了就要重算止损。
    """
    rule = normalize_config(cfg)[RULE_FLOAT_PL_RATIO]
    if not rule.get("enabled"):
        return 0.0
    return _as_float(rule.get("ratio"), 0.0)


def float_pl_loss_amount(cfg: dict | None, balance: float) -> Optional[float]:
    """启用时把盈亏比绝对值换成亏损金额：|比例|% × 账户余额。"""
    if not float_pl_enabled(cfg):
        return None
    ratio = abs(_as_float(normalize_config(cfg)[RULE_FLOAT_PL_RATIO].get("ratio"), 0.0))
    bal = _as_float(balance, 0.0)
    if ratio <= 0 or bal <= 0:
        return None
    return bal * ratio / 100.0


# ----------------------------------------------------------------------
# 余额变动来源判定：只认「出入金等资金操作」
# ----------------------------------------------------------------------
#
# 口径与理由见 deal_types.py（MT5 领域常量集中在那里）：资金操作是客户主动
# 改变本金的那几种成交；交易平仓只是把浮动盈亏转成已实现，本金没变。


def is_funding_deal(deal: dict | None) -> bool:
    """这笔成交是不是「出入金等资金操作」。

    只认 MT5 原始的 `deal_type`（DEAL_TYPE_*）。`direction` 判不了：
    出入金成交没有方向，会被兜底成 "BUY"，靠它判断会把出入金误认成开仓。
    """
    if not isinstance(deal, dict):
        return False
    raw = deal.get("deal_type")
    if raw is None:
        return False
    try:
        return int(raw) in FUNDING_DEAL_TYPES
    except (TypeError, ValueError):
        return False


def filter_funding_deals(deals: list[dict] | None) -> list[dict]:
    """挑出其中的资金操作成交（出入金 / 赠送 / 扣费 / 授信）。"""
    return [d for d in (deals or []) if is_funding_deal(d)]


def funding_balance_delta(deals: list[dict] | None) -> float:
    """这些资金操作成交合起来给账户余额带来的净变化（正=入金，负=出金）。

    出入金成交的金额记在 `profit` 字段上（MT5 对 DEAL_TYPE_BALANCE 就是这么记的），
    出场成交则是「已实现盈亏」。这里只统计资金操作成交，所以不会把平仓结算算进来。
    """
    total = 0.0
    for deal in filter_funding_deals(deals):
        total += _as_float(deal.get("profit"), 0.0)
    return round(total, 8)


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


def market_for_side(side: str, *, bid: float, ask: float, fallback: float) -> float:
    """平仓侧现价：多单看买价，空单看卖价。缺一侧时用事件价。"""
    return _market_for_side(side, bid=bid, ask=ask, fallback=fallback)


def _market_for_side(side: str, *, bid: float, ask: float, fallback: float) -> float:
    """平仓侧现价：多单看买价，空单看卖价。缺一侧时用事件价。"""
    raw = bid if side == "BUY" else ask
    price = _as_float(raw, 0.0)
    if price <= 0:
        price = _as_float(fallback, 0.0)
    return price if price > 0 else 0.0


def cross_level(
    side: str, levels: tuple[float | None, float | None], market: float,
) -> Optional[str]:
    """现价是否越过了已经算好的目标价（止损, 止盈）。

    与 `_cross_side` 同口径，但吃的是**已经落在仓位上的**那一对价，
    用来在只有价格变动、仓位构成没变的那一拍也能判出「冲过目标价了」。

    levels = (sl, tp)；没算的那一侧传 None 或 0（价格 0 不是有效目标价）。
    """
    if market <= 0:
        return None
    raw_sl, raw_tp = levels
    sl = _as_float(raw_sl, 0.0)
    tp = _as_float(raw_tp, 0.0)
    if sl <= 0:
        sl = None
    if tp <= 0:
        tp = None
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


def crossed_sides(
    side: str, sl: float | None, tp: float | None, market: float,
) -> set[str]:
    """两侧各自是否已被现价越过。返回 {"tp"} / {"sl"} / {"tp","sl"} / 空集。

    与 `_cross_side` 同口径，但它能同时报出两侧——止盈和止损要分别决定
    写不写，一侧越价不该把另一侧一起拖下水。
    """
    out: set[str] = set()
    if market <= 0:
        return out
    if side == "BUY":
        if tp is not None and market >= float(tp):
            out.add("tp")
        if sl is not None and market <= float(sl):
            out.add("sl")
        return out
    if tp is not None and market <= float(tp):
        out.add("tp")
    if sl is not None and market >= float(sl):
        out.add("sl")
    return out


def _cross_side(
    side: str, sl: float | None, tp: float | None, market: float,
) -> Optional[str]:
    """现价已经到了或越过要写的止盈 / 止损。没算的那一侧不参与。"""
    crossed = crossed_sides(side, sl, tp, market)
    if "tp" in crossed:
        return "tp"
    if "sl" in crossed:
        return "sl"
    return None


def _price_distance(amount: float, money_per_price: float) -> Optional[float]:
    target = abs(_as_float(amount, 0.0))
    if target <= 0 or money_per_price <= 0:
        return None
    return target / money_per_price


def nearer_stop(side: str, own_sl: float, common_sl: float) -> float:
    """指定价仓该带哪道止损：取离现价更近的那道。

    MT5 一个持仓只有一个 SL，「两道止损」是节点侧模拟出来的：
    - 指定价档位挂单时在 broker 侧写进了自己的止损（`own_sl`）；
    - 大部队有一道共同止损（盈亏比 × 余额反推，`common_sl`）。

    两者只能留一道，取**离现价更近**的那道：
    - 自有止损通常更近（客户是按指定价算的），留它 = 客户真正想要的那道防线生效；
    - 共同止损反而更近时，说明自有止损根本够不到，共同止损先被打掉 →
      整个方向平掉，「自有止损触发后重算」这个场景自然不会发生，逻辑自洽。

    多单取更高（离现价更近），空单取更低。
    """
    own = _as_float(own_sl, 0.0)
    common = _as_float(common_sl, 0.0)
    if own <= 0:
        return common
    if common <= 0:
        return own
    side = str(side or "").strip().upper()
    if side == "BUY":
        return max(own, common)
    return min(own, common)


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
    own_sl_by_ticket: dict | None = None,
    tp_override: dict | None = None,
) -> list[dict]:
    """按方向算共同止盈和/或止损。

    加权开仓价 W = Σ(手数 × 开仓价) / Σ手数。
    距离 D = 金额 / ((tick_value / tick_size) × Σ手数)。
    止盈金额来自分档批次1，且该方向总手数达到 tp_min_lot 才计算；止损金额来自盈亏比 × 余额。
    多单止盈在 W 之上、止损在 W 之下；空单相反。

    `own_sl_by_ticket`：指定价档位持仓的**自有止损**（票号 → 止损价）。
    传入后会额外产出 `sl_by_ticket`：票号 → 这一票该带哪道止损。
    取「离现价更近的那道」（见 `nearer_stop`）：
    - 自有止损更近 → 记 `None`，表示**沿用 broker 上已经写好的自有止损**
      （注意：`None` 是「沿用现值」，不是「不设止损」）；
    - 共同止损更近 → 记共同止损价，改单时把它写上去。

    `tp_override`：方向 → 止盈权威价（指定价档位成交后由它的 `price_tp` 接管）。
    某一方向有权威价时，**该方向的止盈直接用这个绝对价**，不再按
    「加权均价 + 阶梯金额」反推——权威价是用户填的绝对价位，与仓位构成无关，
    加仓后它也不动。金额口径（tier1）仍然成立：手数就是按这个价反推出来的。
    """
    size = _as_float(tick_size, 0.0)
    value = _as_float(tick_value, 0.0)
    if size <= 0 or value <= 0:
        return []
    own_map = own_sl_by_ticket if isinstance(own_sl_by_ticket, dict) else {}
    override = tp_override if isinstance(tp_override, dict) else {}
    # 权威止盈价也算「有东西可写」：它本身就是止盈，
    # 不需要 tier1 金额（阶梯止盈没启用）也不需要止损金额。
    has_override = any(_as_float(v, 0.0) > 0 for v in override.values())
    if (
        _price_distance(tp_amount or 0, 1) is None
        and _price_distance(sl_amount or 0, 1) is None
        and not has_override
    ):
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
        # 止盈权威价：这一方向有指定价档位接管止盈 → 直接用那个绝对价，
        # 不按「加权均价 + 阶梯金额」反推（金额口径已经体现在手数里）。
        #
        # 权威价与 `tp_amount`/`tp_min_lot` 的关系：权威价是**绝对价位**，
        # 与总手数无关，所以 `tp_min_lot` 门槛对它不适用——
        # 手数就是按这个价反推出来的，门槛早就体现在那一笔上了。
        auth_tp = _as_float(override.get(side), 0.0)
        if auth_tp > 0:
            d_tp = None
        else:
            d_tp = _price_distance(tp_amount or 0, money_per_price)
            if d_tp is not None and volume + 1e-8 < max(0.0, _as_float(tp_min_lot, 0.0)):
                d_tp = None
        d_sl = _price_distance(sl_amount or 0, money_per_price)
        if auth_tp <= 0 and d_tp is None and d_sl is None:
            continue
        anchor = weighted / volume
        if side == "BUY":
            tp = auth_tp if auth_tp > 0 else (
                _round_price(anchor + d_tp, digits) if d_tp is not None else None
            )
            sl = _round_price(anchor - d_sl, digits) if d_sl is not None else None
        else:
            tp = auth_tp if auth_tp > 0 else (
                _round_price(anchor - d_tp, digits) if d_tp is not None else None
            )
            sl = _round_price(anchor + d_sl, digits) if d_sl is not None else None
        market = _market_for_side(side, bid=bid, ask=ask, fallback=fallback_price)
        # 指定价档位的逐票止损：取更近的那道。
        #
        # 「更近」的口径是**止损价的高低**（多单取更高、空单取更低）——
        # 与 `nearer_stop` 的口径一致：止损是保护价位的，多单的止损位越高，
        # 行情回落时越先被打到，也就是「更近的那道防线」。
        sl_by_ticket: dict[int, float | None] = {}
        for pos in rows:
            ticket = int(pos.get("ticket") or 0)
            if ticket <= 0 or ticket not in own_map:
                continue
            own_sl = _as_float(own_map.get(ticket), 0.0)
            if own_sl <= 0:
                continue
            picked = nearer_stop(side, own_sl, sl or 0.0)
            # picked == own_sl → 沿用 broker 上的自有止损（None = 沿用现值）
            sl_by_ticket[ticket] = None if picked == own_sl else picked
        plans.append({
            "rule": RULE_LOT_PL_TIERS if (d_tp is not None or auth_tp > 0) else RULE_FLOAT_PL_RATIO,
            "side": side,
            "sl": sl,
            "tp": tp,
            "weighted_open": anchor,
            "tp_distance": d_tp,
            "sl_distance": d_sl,
            "amount": (
                abs(_as_float(tp_amount, 0.0))
                if (d_tp is not None or auth_tp > 0) else None
            ),
            "sl_amount": abs(_as_float(sl_amount, 0.0)) if d_sl is not None else None,
            "volume": volume,
            "digits": max(0, int(digits)),
            "positions": rows,
            # 指定价档位的逐票止损（票号 → 止损价 / None=沿用自有止损）
            "sl_by_ticket": sl_by_ticket,
            # 这一方向的止盈由指定价档位的 price_tp 接管（权威价）
            "tp_authority": auth_tp if auth_tp > 0 else None,
            "crossed": _cross_side(side, sl, tp, market),
            # 两侧各自的越价情况：止盈和止损要分别决定写不写
            "crossed_sides": sorted(crossed_sides(side, sl, tp, market)),
            # 现价：预检提示与越价告警都要用它，少了这个只能显示「现价 0」
            "market": market,
        })
    return plans


def _sendable_stop(
    price: float | None, market: float, *, above: bool,
    stops_level: int, point: float,
) -> Optional[float]:
    """这一侧能写进市价单才返回价格。落在现价错误一侧或近于 stops_level 时返回 None。

    市价单把非法止损止盈带进去，券商会拒掉整张单，而不只是丢掉这一侧。
    """
    if price is None:
        return None
    px = float(price)
    if px <= 0 or market <= 0:
        return None
    if above and px <= market:
        return None
    if not above and px >= market:
        return None
    level = max(0, int(stops_level))
    step = _as_float(point, 0.0)
    if level > 0 and step > 0 and abs(px - market) < level * step:
        return None
    return px


def opening_stops(
    positions: list | None,
    *,
    side: str,
    volume: float,
    fill_price: float,
    tick_size: float,
    tick_value: float,
    digits: int,
    tp_amount: float | None = None,
    sl_amount: float | None = None,
    tp_min_lot: float = 0.0,
    close_action: str = "all",
    bid: float = 0.0,
    ask: float = 0.0,
    stops_level: int = 0,
    point: float = 0.0,
    tp_override: dict | None = None,
) -> Optional[tuple[Optional[float], Optional[float]]]:
    """新开这一笔要带上的 (止损, 止盈)。

    预计成交价算进同方向加权开仓价，口径与事后改单相同。
    该方向不在分档范围内、或缺少价格 / 规格 / 金额时返回 None，调用方沿用原有止损止盈。
    返回元组时，其中的 None 表示这一侧不挂，避免非法价把整张市价单拒掉。

    `tp_override`：方向 → 止盈权威价（指定价档位成交后由它的 `price_tp` 接管）。
    新加的这一笔也要带同一个权威价——否则它会带一个按加权均价反推的止盈，
    和其它持仓的权威止盈对不上。
    """
    action_side = str(side or "").upper()
    vol = _as_float(volume, 0.0)
    fill = _as_float(fill_price, 0.0)
    if action_side not in ("BUY", "SELL") or vol <= 0 or fill <= 0:
        return None
    selected = str(close_action or "all").lower()
    if selected == "buy" and action_side != "BUY":
        return None
    if selected == "sell" and action_side != "SELL":
        return None
    book = filter_lot_side(positions, selected)
    book.append({"type": action_side, "volume": vol, "price_open": fill})
    plans = plan_common_stops(
        book,
        tp_amount=tp_amount,
        sl_amount=sl_amount,
        tp_min_lot=tp_min_lot,
        tick_size=tick_size,
        tick_value=tick_value,
        digits=digits,
        bid=bid,
        ask=ask,
        fallback_price=fill,
        tp_override=tp_override,
    )
    plan = next((row for row in plans if row.get("side") == action_side), None)
    if plan is None:
        return None
    market = _market_for_side(action_side, bid=bid, ask=ask, fallback=fill)
    if action_side == "BUY":
        sl = _sendable_stop(plan.get("sl"), market, above=False, stops_level=stops_level, point=point)
        tp = _sendable_stop(plan.get("tp"), market, above=True, stops_level=stops_level, point=point)
    else:
        sl = _sendable_stop(plan.get("sl"), market, above=True, stops_level=stops_level, point=point)
        tp = _sendable_stop(plan.get("tp"), market, above=False, stops_level=stops_level, point=point)
    return sl, tp


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
    detail = {
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
    # 越价保留旧值时才有这些键：现价、保留的是哪一侧、为什么保留
    for key in (
        "status", "kept_side", "kept_reason", "market", "new_sl", "new_tp",
    ):
        if plan.get(key) is not None:
            detail[key] = plan.get(key)
    return detail


def describe_kept_side(
    side: str, *, kind: str, new_value: float | None,
    old_value: float | None, market: float,
) -> str:
    """越价被拒、保留旧值时的一句话说明。

    kind 是 "tp" 或 "sl"。old_value 为 0 / None 表示这一侧本来就没有值。
    """
    label = "止盈" if kind == "tp" else "止损"
    direction = "多单" if side == "BUY" else "空单"
    old_text = f"{float(old_value):g}" if old_value else "无"
    new_text = f"{float(new_value):g}" if new_value is not None else "无"
    return (
        f"{direction}新的{label}价 {new_text} 已在现价 {float(market):g} 的错误一侧，"
        f"本次未写入，保留原{label}价 {old_text}"
    )


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
