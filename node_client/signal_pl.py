"""模版1 信号级盈亏控制纯函数：配置抽取与触发判定。

与账户风控隔离：分子只统计本魔术号持仓浮盈亏，分母用账户余额；
触发后由调用方只平该魔术号仓位。分档手数复用 account_risk 的匹配逻辑。
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


def should_trigger_lot_pl_tiers(
    cfg: dict | None,
    *,
    positions: list | None,
) -> dict | None:
    src = normalize_config(cfg)
    hit = ar.should_trigger_lot_pl_tiers(
        {ar.RULE_LOT_PL_TIERS: src[RULE_LOT_PL_TIERS]},
        positions=positions,
    )
    if hit is None:
        return None
    hit = dict(hit)
    hit["rule"] = RULE_LOT_PL_TIERS
    return hit


def find_triggered_rule(
    cfg: dict | None,
    *,
    balance: float,
    signal_profit: float,
    positions: list | None,
) -> dict | None:
    """先盈亏比、再分档手数。"""
    has_positions = bool(positions)
    hit = should_trigger_float_pl(
        cfg, balance=balance, signal_profit=signal_profit, has_positions=has_positions,
    )
    if hit:
        return hit
    return should_trigger_lot_pl_tiers(cfg, positions=positions)


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
