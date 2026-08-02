"""节点账户级风控：配置规范化、校验，以及向在线节点下发。

规则：
- float_pl_ratio：浮盈/浮亏相对余额达到阈值后自动清仓
- equity_min：账户净值低于指定 USD 后自动清仓
- symbol_pl_orders：品种盈亏金额 + 订单数条件 → 按动作平仓（可多条）
- symbol_pl_protect：品种浮盈亏保护状态机（可多条）
- lot_pl_tiers：按持仓手数分档 + 盈亏金额平仓

配置权威存库（nodes.risk_json），节点在登录成功与保存时经 WS 同步执行。
"""
from __future__ import annotations

import uuid
from typing import Any

from .connections import manager

RULE_FLOAT_PL_RATIO = "float_pl_ratio"
RULE_EQUITY_MIN = "equity_min"
RULE_SYMBOL_PL_ORDERS = "symbol_pl_orders"
RULE_SYMBOL_PL_PROTECT = "symbol_pl_protect"
RULE_LOT_PL_TIERS = "lot_pl_tiers"

ACTION_CLOSE_ALL = "close_all"
MONITOR_LOOP = "loop"
MONITOR_TIMES = "times"

ORDER_OPS = ("any", "gt", "gte", "eq", "lte", "lt")
CLOSE_ACTIONS_SYMBOL = ("all", "buy", "sell", "hedge")
CLOSE_ACTIONS_SIDE = ("all", "buy", "sell")

MAX_LIST_ITEMS = 20
MAX_TIERS = 10

DEFAULT_FLOAT_PL_RATIO: dict[str, Any] = {
    "enabled": False,
    "ratio": -20.0,
    "action": ACTION_CLOSE_ALL,
    "monitor_mode": MONITOR_LOOP,
    "max_times": 1,
    "remaining_times": 0,
}

DEFAULT_EQUITY_MIN: dict[str, Any] = {
    "enabled": False,
    "amount": 1000.0,
    "action": ACTION_CLOSE_ALL,
    "monitor_mode": MONITOR_LOOP,
    "max_times": 1,
    "remaining_times": 0,
}

DEFAULT_SYMBOL_PL_ORDER_ITEM: dict[str, Any] = {
    "enabled": False,
    "symbol": "",
    "pl_amount": 100.0,
    "order_op": "any",
    "order_count": 0,
    "close_action": "all",
    "monitor_mode": MONITOR_LOOP,
    "max_times": 1,
    "remaining_times": 0,
}

DEFAULT_SYMBOL_PL_PROTECT_ITEM: dict[str, Any] = {
    "enabled": False,
    "symbol": "",
    "trigger_amount": -100.0,
    "narrow_amount": -50.0,
    "monitor_mode": MONITOR_LOOP,
    "max_times": 1,
    "remaining_times": 0,
}

DEFAULT_LOT_PL_TIERS: dict[str, Any] = {
    "enabled": False,
    "batch_count": 2,
    "close_action": "all",
    "tiers": [
        {"min_lot": 0.1, "pl_amount": 50.0},
        {"min_lot": 0.5, "pl_amount": 100.0},
    ],
}


def default_risk() -> dict:
    return {
        RULE_FLOAT_PL_RATIO: dict(DEFAULT_FLOAT_PL_RATIO),
        RULE_EQUITY_MIN: dict(DEFAULT_EQUITY_MIN),
        RULE_SYMBOL_PL_ORDERS: {"items": []},
        RULE_SYMBOL_PL_PROTECT: {"items": []},
        RULE_LOT_PL_TIERS: {
            **DEFAULT_LOT_PL_TIERS,
            "tiers": [dict(t) for t in DEFAULT_LOT_PL_TIERS["tiers"]],
        },
    }


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_times_fields(rule_src: dict) -> dict:
    """规范化开关 / 监控模式 / 次数等公共字段（不含 action）。"""
    mode = str(rule_src.get("monitor_mode") or MONITOR_LOOP).lower()
    if mode not in (MONITOR_LOOP, MONITOR_TIMES):
        mode = MONITOR_LOOP
    try:
        max_times = int(rule_src.get("max_times", 1) or 1)
    except (TypeError, ValueError):
        max_times = 1
    max_times = max(1, max_times)
    try:
        remaining = int(rule_src.get("remaining_times", 0) or 0)
    except (TypeError, ValueError):
        remaining = 0
    remaining = max(0, remaining)
    enabled = bool(rule_src.get("enabled", False))
    if enabled and mode == MONITOR_TIMES and remaining <= 0:
        remaining = max_times
    if mode == MONITOR_LOOP:
        remaining = 0
    if not enabled and mode == MONITOR_TIMES:
        remaining = 0
    return {
        "enabled": enabled,
        "monitor_mode": mode,
        "max_times": max_times,
        "remaining_times": remaining,
    }


def _normalize_float_pl_ratio(rule_src: dict | None) -> dict:
    src = rule_src if isinstance(rule_src, dict) else {}
    common = _normalize_times_fields(src)
    try:
        ratio = float(src.get("ratio", DEFAULT_FLOAT_PL_RATIO["ratio"]))
    except (TypeError, ValueError):
        ratio = float(DEFAULT_FLOAT_PL_RATIO["ratio"])
    return {**common, "ratio": ratio, "action": ACTION_CLOSE_ALL}


def _normalize_equity_min(rule_src: dict | None) -> dict:
    src = rule_src if isinstance(rule_src, dict) else {}
    common = _normalize_times_fields(src)
    try:
        amount = float(src.get("amount", DEFAULT_EQUITY_MIN["amount"]))
    except (TypeError, ValueError):
        amount = float(DEFAULT_EQUITY_MIN["amount"])
    return {**common, "amount": amount, "action": ACTION_CLOSE_ALL}


def _normalize_symbol_pl_order_item(raw: dict | None) -> dict:
    src = raw if isinstance(raw, dict) else {}
    common = _normalize_times_fields(src)
    item_id = str(src.get("id") or "").strip() or _new_id()
    symbol = str(src.get("symbol") or "").strip().upper()
    try:
        pl_amount = float(src.get("pl_amount", DEFAULT_SYMBOL_PL_ORDER_ITEM["pl_amount"]))
    except (TypeError, ValueError):
        pl_amount = float(DEFAULT_SYMBOL_PL_ORDER_ITEM["pl_amount"])
    order_op = str(src.get("order_op") or "any").lower()
    if order_op not in ORDER_OPS:
        order_op = "any"
    try:
        order_count = int(src.get("order_count", 0) or 0)
    except (TypeError, ValueError):
        order_count = 0
    order_count = max(0, order_count)
    close_action = str(src.get("close_action") or "all").lower()
    if close_action not in CLOSE_ACTIONS_SYMBOL:
        close_action = "all"
    return {
        "id": item_id,
        **common,
        "symbol": symbol,
        "pl_amount": pl_amount,
        "order_op": order_op,
        "order_count": order_count,
        "close_action": close_action,
    }


def _normalize_symbol_pl_protect_item(raw: dict | None) -> dict:
    src = raw if isinstance(raw, dict) else {}
    common = _normalize_times_fields(src)
    item_id = str(src.get("id") or "").strip() or _new_id()
    symbol = str(src.get("symbol") or "").strip().upper()
    try:
        trigger_amount = float(
            src.get("trigger_amount", DEFAULT_SYMBOL_PL_PROTECT_ITEM["trigger_amount"])
        )
    except (TypeError, ValueError):
        trigger_amount = float(DEFAULT_SYMBOL_PL_PROTECT_ITEM["trigger_amount"])
    try:
        narrow_amount = float(
            src.get("narrow_amount", DEFAULT_SYMBOL_PL_PROTECT_ITEM["narrow_amount"])
        )
    except (TypeError, ValueError):
        narrow_amount = float(DEFAULT_SYMBOL_PL_PROTECT_ITEM["narrow_amount"])
    return {
        "id": item_id,
        **common,
        "symbol": symbol,
        "trigger_amount": trigger_amount,
        "narrow_amount": narrow_amount,
    }


def _normalize_list_block(raw: Any, item_norm) -> dict:
    src = raw if isinstance(raw, dict) else {}
    items_raw = src.get("items") if isinstance(src.get("items"), list) else []
    items = []
    for it in items_raw[:MAX_LIST_ITEMS]:
        if isinstance(it, dict):
            items.append(item_norm(it))
    return {"items": items}


def _normalize_lot_pl_tiers(raw: dict | None) -> dict:
    src = raw if isinstance(raw, dict) else {}
    enabled = bool(src.get("enabled", False))
    try:
        batch_count = int(src.get("batch_count", 2) or 2)
    except (TypeError, ValueError):
        batch_count = 2
    batch_count = max(1, min(MAX_TIERS, batch_count))
    close_action = str(src.get("close_action") or "all").lower()
    if close_action not in CLOSE_ACTIONS_SIDE:
        close_action = "all"
    old_tiers = src.get("tiers") if isinstance(src.get("tiers"), list) else []
    tiers: list[dict] = []
    for i in range(batch_count):
        prev = old_tiers[i] if i < len(old_tiers) and isinstance(old_tiers[i], dict) else {}
        try:
            min_lot = float(prev.get("min_lot", 0.1 * (i + 1)))
        except (TypeError, ValueError):
            min_lot = 0.1 * (i + 1)
        try:
            pl_amount = float(prev.get("pl_amount", 50.0 * (i + 1)))
        except (TypeError, ValueError):
            pl_amount = 50.0 * (i + 1)
        tiers.append({"min_lot": max(0.0, min_lot), "pl_amount": pl_amount})
    return {
        "enabled": enabled,
        "batch_count": batch_count,
        "close_action": close_action,
        "tiers": tiers,
    }


def normalize_risk(raw: dict | None) -> dict:
    """把任意入参整理成稳定结构；未知字段丢弃，缺省用默认值。"""
    src = raw if isinstance(raw, dict) else {}
    return {
        RULE_FLOAT_PL_RATIO: _normalize_float_pl_ratio(src.get(RULE_FLOAT_PL_RATIO)),
        RULE_EQUITY_MIN: _normalize_equity_min(src.get(RULE_EQUITY_MIN)),
        RULE_SYMBOL_PL_ORDERS: _normalize_list_block(
            src.get(RULE_SYMBOL_PL_ORDERS), _normalize_symbol_pl_order_item,
        ),
        RULE_SYMBOL_PL_PROTECT: _normalize_list_block(
            src.get(RULE_SYMBOL_PL_PROTECT), _normalize_symbol_pl_protect_item,
        ),
        RULE_LOT_PL_TIERS: _normalize_lot_pl_tiers(src.get(RULE_LOT_PL_TIERS)),
    }


def _validate_times(rule: dict, label: str) -> str | None:
    mode = rule.get("monitor_mode")
    if mode not in (MONITOR_LOOP, MONITOR_TIMES):
        return f"{label}监控模式无效"
    if mode == MONITOR_TIMES:
        try:
            mt = int(rule.get("max_times") or 0)
        except (TypeError, ValueError):
            return f"{label}指定次数无效"
        if mt < 1:
            return f"{label}指定次数至少为 1"
    return None


def validate_risk(risk: dict) -> str | None:
    """校验规范化后的风控配置；通过返回 None，否则返回中文错误。"""
    cfg = risk or {}

    pl = cfg.get(RULE_FLOAT_PL_RATIO) or {}
    try:
        ratio_f = float(pl.get("ratio"))
    except (TypeError, ValueError):
        return "账户盈亏比比例无效"
    if ratio_f == 0:
        return "账户盈亏比比例不能为 0"
    err = _validate_times(pl, "账户盈亏比")
    if err:
        return err

    eq = cfg.get(RULE_EQUITY_MIN) or {}
    try:
        amount_f = float(eq.get("amount"))
    except (TypeError, ValueError):
        return "账户净值金额无效"
    if amount_f <= 0:
        return "账户净值金额必须大于 0"
    err = _validate_times(eq, "账户净值")
    if err:
        return err

    for i, item in enumerate((cfg.get(RULE_SYMBOL_PL_ORDERS) or {}).get("items") or []):
        label = f"品种盈亏条件#{i + 1}"
        if item.get("enabled") and not item.get("symbol"):
            return f"{label}请填写品种"
        try:
            pla = float(item.get("pl_amount"))
        except (TypeError, ValueError):
            return f"{label}盈亏金额无效"
        if pla == 0:
            return f"{label}盈亏金额不能为 0"
        if item.get("order_op") not in ORDER_OPS:
            return f"{label}订单条件无效"
        if item.get("close_action") not in CLOSE_ACTIONS_SYMBOL:
            return f"{label}平仓动作无效"
        err = _validate_times(item, label)
        if err:
            return err

    for i, item in enumerate((cfg.get(RULE_SYMBOL_PL_PROTECT) or {}).get("items") or []):
        label = f"浮盈亏保护#{i + 1}"
        if item.get("enabled") and not item.get("symbol"):
            return f"{label}请填写品种"
        try:
            trigger = float(item.get("trigger_amount"))
            narrow = float(item.get("narrow_amount"))
        except (TypeError, ValueError):
            return f"{label}金额无效"
        if trigger == 0 or narrow == 0:
            return f"{label}触发/收窄金额不能为 0"
        if trigger > 0 and narrow > 0:
            if not (trigger > narrow):
                return f"{label}盈利保护要求：触发金额 > 收窄金额 > 0"
        elif trigger < 0 and narrow < 0:
            if not (trigger < narrow):
                return f"{label}亏损保护要求：触发金额 < 收窄金额 < 0（收窄更接近 0）"
        else:
            return f"{label}触发与收窄金额须同为正（盈利保护）或同为负（亏损保护）"
        err = _validate_times(item, label)
        if err:
            return err

    tiers_cfg = cfg.get(RULE_LOT_PL_TIERS) or {}
    if tiers_cfg.get("close_action") not in CLOSE_ACTIONS_SIDE:
        return "分档平仓动作无效"
    tiers = tiers_cfg.get("tiers") or []
    if not tiers:
        return "分档平仓至少需要 1 个批次"
    for i, tier in enumerate(tiers):
        try:
            min_lot = float(tier.get("min_lot"))
            pla = float(tier.get("pl_amount"))
        except (TypeError, ValueError):
            return f"分档批次#{i + 1}参数无效"
        if min_lot < 0:
            return f"分档批次#{i + 1}手数不能为负"
        if pla == 0:
            return f"分档批次#{i + 1}盈亏金额不能为 0"

    return None


def merge_runtime_state(current: dict | None, reported: dict | None) -> dict:
    """把节点回报的运行态合并进库里的权威配置。

    节点回报的是它上次收到的整份快照，真正由它改变的只有「开关」和「剩余次数」
    （次数耗尽自动关闭）。只取这两个字段，其余一律以库为准，否则管理员刚保存的
    新配置会被节点的旧快照覆盖掉。
    """
    base = normalize_risk(current)
    rep = normalize_risk(reported)

    for key in (RULE_FLOAT_PL_RATIO, RULE_EQUITY_MIN):
        base[key]["enabled"] = rep[key]["enabled"]
        base[key]["remaining_times"] = rep[key]["remaining_times"]

    for key in (RULE_SYMBOL_PL_ORDERS, RULE_SYMBOL_PL_PROTECT):
        reported_items = {it["id"]: it for it in rep[key]["items"]}
        for item in base[key]["items"]:
            hit = reported_items.get(item["id"])
            if hit is None:
                continue  # 该条目是回报之后新增的，保持库里的状态
            item["enabled"] = hit["enabled"]
            item["remaining_times"] = hit["remaining_times"]

    return normalize_risk(base)


def risk_ws_payload(risk: dict | None) -> dict:
    return {"risk": normalize_risk(risk)}


async def push_risk_config_to_node(node_id: str, risk: dict | None) -> bool:
    msg = {"type": "risk_config", "data": risk_ws_payload(risk)}
    return await manager.send_to_node(node_id, msg)
