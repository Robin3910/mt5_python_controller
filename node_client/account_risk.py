"""账户级风控纯函数：配置规范化与触发判定。

规则：
- float_pl_ratio / equity_min：账户级
- symbol_pl_orders：品种盈亏 + 订单数条件
- symbol_pl_protect：浮盈亏保护状态机（armed 由调用方维护）
- lot_pl_tiers：持仓手数分档 + 盈亏金额
"""
from __future__ import annotations

import uuid
from typing import Any

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

# 人读标签：落库到策略结束原因 / 平仓事件「开单原因」
RULE_LABELS: dict[str, str] = {
    RULE_FLOAT_PL_RATIO: "浮盈亏比例",
    RULE_EQUITY_MIN: "净值下限",
    RULE_SYMBOL_PL_ORDERS: "品种盈亏+订单数",
    RULE_SYMBOL_PL_PROTECT: "浮盈亏保护",
    RULE_LOT_PL_TIERS: "手数分档盈亏",
}
CLOSE_ACTION_LABELS: dict[str, str] = {
    "account_all": "账户全部平仓",
    "all": "全部平仓",
    "buy": "多单平仓",
    "sell": "空单平仓",
    "hedge": "锁单平仓",
}
REASON_LIMIT = 255

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


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def default_risk() -> dict:
    return {
        RULE_FLOAT_PL_RATIO: dict(DEFAULT_FLOAT_PL_RATIO),
        RULE_EQUITY_MIN: dict(DEFAULT_EQUITY_MIN),
        RULE_SYMBOL_PL_ORDERS: {"items": []},
        RULE_SYMBOL_PL_PROTECT: {"items": []},
        RULE_LOT_PL_TIERS: {
            "enabled": False,
            "batch_count": 2,
            "close_action": "all",
            "tiers": [
                {"min_lot": 0.1, "pl_amount": 50.0},
                {"min_lot": 0.5, "pl_amount": 100.0},
            ],
        },
    }


def _normalize_times_fields(rule_src: dict) -> dict:
    mode = str(rule_src.get("monitor_mode") or MONITOR_LOOP).lower()
    if mode not in (MONITOR_LOOP, MONITOR_TIMES):
        mode = MONITOR_LOOP
    try:
        max_times = max(1, int(rule_src.get("max_times", 1) or 1))
    except (TypeError, ValueError):
        max_times = 1
    try:
        remaining = max(0, int(rule_src.get("remaining_times", 0) or 0))
    except (TypeError, ValueError):
        remaining = 0
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


def normalize_risk(raw: dict | None) -> dict:
    src = raw if isinstance(raw, dict) else {}

    pl_src = src.get(RULE_FLOAT_PL_RATIO) if isinstance(src.get(RULE_FLOAT_PL_RATIO), dict) else {}
    try:
        ratio = float(pl_src.get("ratio", DEFAULT_FLOAT_PL_RATIO["ratio"]))
    except (TypeError, ValueError):
        ratio = float(DEFAULT_FLOAT_PL_RATIO["ratio"])

    eq_src = src.get(RULE_EQUITY_MIN) if isinstance(src.get(RULE_EQUITY_MIN), dict) else {}
    try:
        amount = float(eq_src.get("amount", DEFAULT_EQUITY_MIN["amount"]))
    except (TypeError, ValueError):
        amount = float(DEFAULT_EQUITY_MIN["amount"])

    spo_src = src.get(RULE_SYMBOL_PL_ORDERS) if isinstance(src.get(RULE_SYMBOL_PL_ORDERS), dict) else {}
    spo_items = []
    for it in (spo_src.get("items") or [])[:MAX_LIST_ITEMS]:
        if not isinstance(it, dict):
            continue
        common = _normalize_times_fields(it)
        try:
            pla = float(it.get("pl_amount", 100))
        except (TypeError, ValueError):
            pla = 100.0
        op = str(it.get("order_op") or "any").lower()
        if op not in ORDER_OPS:
            op = "any"
        try:
            oc = max(0, int(it.get("order_count", 0) or 0))
        except (TypeError, ValueError):
            oc = 0
        ca = str(it.get("close_action") or "all").lower()
        if ca not in CLOSE_ACTIONS_SYMBOL:
            ca = "all"
        spo_items.append({
            "id": str(it.get("id") or "").strip() or _new_id(),
            **common,
            "symbol": str(it.get("symbol") or "").strip().upper(),
            "pl_amount": pla,
            "order_op": op,
            "order_count": oc,
            "close_action": ca,
        })

    spp_src = src.get(RULE_SYMBOL_PL_PROTECT) if isinstance(src.get(RULE_SYMBOL_PL_PROTECT), dict) else {}
    spp_items = []
    for it in (spp_src.get("items") or [])[:MAX_LIST_ITEMS]:
        if not isinstance(it, dict):
            continue
        common = _normalize_times_fields(it)
        try:
            trigger = float(it.get("trigger_amount", -100))
        except (TypeError, ValueError):
            trigger = -100.0
        try:
            narrow = float(it.get("narrow_amount", -50))
        except (TypeError, ValueError):
            narrow = -50.0
        spp_items.append({
            "id": str(it.get("id") or "").strip() or _new_id(),
            **common,
            "symbol": str(it.get("symbol") or "").strip().upper(),
            "trigger_amount": trigger,
            "narrow_amount": narrow,
        })

    lpt_src = src.get(RULE_LOT_PL_TIERS) if isinstance(src.get(RULE_LOT_PL_TIERS), dict) else {}
    try:
        batch_count = max(1, min(MAX_TIERS, int(lpt_src.get("batch_count", 2) or 2)))
    except (TypeError, ValueError):
        batch_count = 2
    ca = str(lpt_src.get("close_action") or "all").lower()
    if ca not in CLOSE_ACTIONS_SIDE:
        ca = "all"
    old_tiers = lpt_src.get("tiers") if isinstance(lpt_src.get("tiers"), list) else []
    tiers = []
    for i in range(batch_count):
        prev = old_tiers[i] if i < len(old_tiers) and isinstance(old_tiers[i], dict) else {}
        try:
            min_lot = max(0.0, float(prev.get("min_lot", 0.1 * (i + 1))))
        except (TypeError, ValueError):
            min_lot = 0.1 * (i + 1)
        try:
            pla = float(prev.get("pl_amount", 50.0 * (i + 1)))
        except (TypeError, ValueError):
            pla = 50.0 * (i + 1)
        tiers.append({"min_lot": min_lot, "pl_amount": pla})

    return {
        RULE_FLOAT_PL_RATIO: {
            **_normalize_times_fields(pl_src),
            "ratio": ratio,
            "action": ACTION_CLOSE_ALL,
        },
        RULE_EQUITY_MIN: {
            **_normalize_times_fields(eq_src),
            "amount": amount,
            "action": ACTION_CLOSE_ALL,
        },
        RULE_SYMBOL_PL_ORDERS: {"items": spo_items},
        RULE_SYMBOL_PL_PROTECT: {"items": spp_items},
        RULE_LOT_PL_TIERS: {
            "enabled": bool(lpt_src.get("enabled", False)),
            "batch_count": batch_count,
            "close_action": ca,
            "tiers": tiers,
        },
    }


def floating_pl_ratio(balance: float, equity: float) -> float | None:
    try:
        bal = float(balance)
        eq = float(equity)
    except (TypeError, ValueError):
        return None
    if bal <= 0:
        return None
    return (eq - bal) / bal * 100.0


def ratio_triggered(current_ratio: float, threshold: float) -> bool:
    if threshold < 0:
        return current_ratio <= threshold
    if threshold > 0:
        return current_ratio >= threshold
    return False


def amount_triggered(current: float, threshold: float) -> bool:
    """盈亏金额触达：正阈值为盈利侧 >=，负阈值为亏损侧 <=。"""
    if threshold > 0:
        return current >= threshold
    if threshold < 0:
        return current <= threshold
    return False


def _rule_ready(rule: dict) -> bool:
    if not rule.get("enabled"):
        return False
    if rule.get("monitor_mode") == MONITOR_TIMES and int(rule.get("remaining_times") or 0) <= 0:
        return False
    return True


def _match_symbol(pos_symbol: str, target: str) -> bool:
    a = str(pos_symbol or "").upper().replace("/", "")
    b = str(target or "").upper().replace("/", "")
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def filter_positions(
    positions: list | None,
    *,
    symbol: str | None = None,
    side: str | None = None,
) -> list[dict]:
    """按品种 / 方向筛选持仓。side: BUY/SELL/None。"""
    out = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if symbol and not _match_symbol(str(p.get("symbol") or ""), symbol):
            continue
        if side and str(p.get("type") or "").upper() != side.upper():
            continue
        out.append(p)
    return out


def positions_stats(positions: list[dict]) -> dict:
    profit = 0.0
    volume = 0.0
    buys = 0
    sells = 0
    for p in positions:
        try:
            profit += float(p.get("profit") or 0)
        except (TypeError, ValueError):
            pass
        try:
            volume += float(p.get("volume") or 0)
        except (TypeError, ValueError):
            pass
        t = str(p.get("type") or "").upper()
        if t == "BUY":
            buys += 1
        elif t == "SELL":
            sells += 1
    return {
        "profit": profit,
        "volume": volume,
        "count": len(positions),
        "buys": buys,
        "sells": sells,
        "hedged": buys > 0 and sells > 0,
    }


def order_count_match(count: int, op: str, expect: int) -> bool:
    if op == "any":
        return True
    if op == "gt":
        return count > expect
    if op == "gte":
        return count >= expect
    if op == "eq":
        return count == expect
    if op == "lte":
        return count <= expect
    if op == "lt":
        return count < expect
    return False


def select_close_targets(
    positions: list | None,
    *,
    close_action: str,
    symbol: str | None = None,
) -> list[dict]:
    """按平仓动作选出要平的持仓。

    - account_all：全账户
    - all + symbol：该品种全部
    - buy/sell：可带 symbol（品种内）或不带（全账户该方向）
    - hedge：仅品种内，且需多空同时存在
    """
    action = (close_action or "all").lower()
    if action == "account_all":
        return [p for p in (positions or []) if isinstance(p, dict)]
    scoped = filter_positions(positions, symbol=symbol) if symbol else [
        p for p in (positions or []) if isinstance(p, dict)
    ]
    if action == "all":
        return scoped if symbol else [p for p in (positions or []) if isinstance(p, dict)]
    if action == "buy":
        return filter_positions(scoped, side="BUY")
    if action == "sell":
        return filter_positions(scoped, side="SELL")
    if action == "hedge":
        if not symbol:
            return []
        st = positions_stats(scoped)
        return scoped if st["hedged"] else []
    return []


def should_trigger_float_pl(
    risk: dict | None,
    *,
    balance: float,
    equity: float,
    has_positions: bool,
) -> dict | None:
    cfg = normalize_risk(risk)
    rule = cfg[RULE_FLOAT_PL_RATIO]
    if not _rule_ready(rule) or not has_positions:
        return None
    current = floating_pl_ratio(balance, equity)
    if current is None:
        return None
    threshold = float(rule["ratio"])
    if not ratio_triggered(current, threshold):
        return None
    return {
        "rule": RULE_FLOAT_PL_RATIO,
        "close_action": "account_all",
        "symbol": None,
        "threshold": threshold,
        "current_ratio": current,
        "floating_pl": float(equity) - float(balance),
        "balance": float(balance),
        "equity": float(equity),
        "action": ACTION_CLOSE_ALL,
        "monitor_mode": rule.get("monitor_mode"),
        "max_times": int(rule.get("max_times") or 1),
        "remaining_times": int(rule.get("remaining_times") or 0),
        "message_core": f"浮盈亏比 {current:.2f}% 达到阈值 {threshold}%",
    }


def should_trigger_equity_min(
    risk: dict | None,
    *,
    equity: float,
    has_positions: bool,
) -> dict | None:
    cfg = normalize_risk(risk)
    rule = cfg[RULE_EQUITY_MIN]
    if not _rule_ready(rule) or not has_positions:
        return None
    try:
        eq = float(equity)
        amount = float(rule["amount"])
    except (TypeError, ValueError):
        return None
    if amount <= 0 or eq >= amount:
        return None
    return {
        "rule": RULE_EQUITY_MIN,
        "close_action": "account_all",
        "symbol": None,
        "amount": amount,
        "equity": eq,
        "action": ACTION_CLOSE_ALL,
        "monitor_mode": rule.get("monitor_mode"),
        "max_times": int(rule.get("max_times") or 1),
        "remaining_times": int(rule.get("remaining_times") or 0),
        "message_core": f"账户净值 {eq:.2f} USD 低于阈值 {amount:g} USD",
    }


def should_trigger_symbol_pl_orders(
    risk: dict | None,
    *,
    positions: list | None,
) -> dict | None:
    cfg = normalize_risk(risk)
    for item in cfg[RULE_SYMBOL_PL_ORDERS]["items"]:
        if not _rule_ready(item):
            continue
        symbol = item.get("symbol") or ""
        if not symbol:
            continue
        sym_pos = filter_positions(positions, symbol=symbol)
        if not sym_pos:
            continue
        st = positions_stats(sym_pos)
        if not amount_triggered(st["profit"], float(item["pl_amount"])):
            continue
        if not order_count_match(st["count"], item["order_op"], int(item["order_count"])):
            continue
        # 该动作选不出可平的持仓就不算触发：例如动作是「多单平仓」但该品种只有空单，
        # 或动作是「锁单平仓」但当前并非锁单。否则会执行一次空平仓，白扣次数甚至关掉规则。
        targets = select_close_targets(
            sym_pos, close_action=item["close_action"], symbol=symbol,
        )
        if not targets:
            continue
        op_label = {
            "any": "不限", "gt": ">", "gte": ">=", "eq": "=", "lte": "<=", "lt": "<",
        }.get(item["order_op"], item["order_op"])
        return {
            "rule": RULE_SYMBOL_PL_ORDERS,
            "item_id": item["id"],
            "close_action": item["close_action"],
            "symbol": symbol,
            "pl_amount": float(item["pl_amount"]),
            "current_pl": st["profit"],
            "order_count": st["count"],
            "close_count": len(targets),
            "monitor_mode": item.get("monitor_mode"),
            "max_times": int(item.get("max_times") or 1),
            "remaining_times": int(item.get("remaining_times") or 0),
            "message_core": (
                f"{symbol} 盈亏 {st['profit']:.2f} 达 {item['pl_amount']:g}"
                f" 且订单数 {st['count']} {op_label} {item['order_count']}"
            ),
        }
    return None


def evaluate_protect_items(
    risk: dict | None,
    *,
    positions: list | None,
    armed_map: dict[str, bool],
) -> tuple[dict[str, bool], dict | None, list[dict]]:
    """推进保护状态机。

    返回 (新 armed_map, 若应收窄平仓则 hit, 新进入保护的事件列表)。
    """
    cfg = normalize_risk(risk)
    new_armed = dict(armed_map or {})
    armed_events: list[dict] = []
    hit: dict | None = None

    for item in cfg[RULE_SYMBOL_PL_PROTECT]["items"]:
        item_id = item["id"]
        if not _rule_ready(item):
            new_armed.pop(item_id, None)
            continue
        symbol = item.get("symbol") or ""
        if not symbol:
            continue
        sym_pos = filter_positions(positions, symbol=symbol)
        if not sym_pos:
            # 无持仓时解除 armed，避免下次开仓误触发收窄
            new_armed.pop(item_id, None)
            continue
        st = positions_stats(sym_pos)
        pl = st["profit"]
        trigger = float(item["trigger_amount"])
        narrow = float(item["narrow_amount"])
        is_profit = trigger > 0

        if not new_armed.get(item_id):
            reached = pl >= trigger if is_profit else pl <= trigger
            if reached:
                new_armed[item_id] = True
                armed_events.append({
                    "rule": RULE_SYMBOL_PL_PROTECT,
                    "item_id": item_id,
                    "event": "armed",
                    "symbol": symbol,
                    "current_pl": pl,
                    "trigger_amount": trigger,
                    "narrow_amount": narrow,
                    "message": (
                        f"{symbol} 浮盈亏 {pl:.2f} 达到触发 {trigger:g}，进入保护监控"
                        f"（收窄至 {narrow:g} 将平仓）"
                    ),
                })
            continue

        # 已进入保护：收窄到目标则平仓
        narrowed = pl <= narrow if is_profit else pl >= narrow
        if narrowed and hit is None:
            hit = {
                "rule": RULE_SYMBOL_PL_PROTECT,
                "item_id": item_id,
                "close_action": "all",
                "symbol": symbol,
                "current_pl": pl,
                "trigger_amount": trigger,
                "narrow_amount": narrow,
                "monitor_mode": item.get("monitor_mode"),
                "max_times": int(item.get("max_times") or 1),
                "remaining_times": int(item.get("remaining_times") or 0),
                "message_core": (
                    f"{symbol} 浮盈亏保护：已从触发 {trigger:g} 收窄至 {pl:.2f}"
                    f"（目标 {narrow:g}）"
                ),
            }
    return new_armed, hit, armed_events


def prune_armed_map(
    risk: dict | None,
    armed_map: dict[str, bool] | None,
    *,
    previous: dict | None = None,
) -> dict[str, bool]:
    """配置更新后保留仍然有效的保护进度。

    以下情况清除该条目的 armed：条目已删除、已关闭，或保护方向反转
    （盈利保护 ↔ 亏损保护）。仅调整金额时保留进度，按新目标继续判定。
    """
    cfg = normalize_risk(risk)
    items = {it["id"]: it for it in cfg[RULE_SYMBOL_PL_PROTECT]["items"]}
    old_items = {}
    if previous is not None:
        old_cfg = normalize_risk(previous)
        old_items = {it["id"]: it for it in old_cfg[RULE_SYMBOL_PL_PROTECT]["items"]}

    kept: dict[str, bool] = {}
    for item_id, armed in (armed_map or {}).items():
        if not armed:
            continue
        item = items.get(item_id)
        if not item or not item.get("enabled"):
            continue
        old = old_items.get(item_id)
        if old is not None:
            was_profit = float(old["trigger_amount"]) > 0
            now_profit = float(item["trigger_amount"]) > 0
            if was_profit != now_profit:
                continue
        kept[item_id] = True
    return kept


def should_trigger_lot_pl_tiers(
    risk: dict | None,
    *,
    positions: list | None,
) -> dict | None:
    cfg = normalize_risk(risk)
    rule = cfg[RULE_LOT_PL_TIERS]
    if not rule.get("enabled"):
        return None
    side = rule.get("close_action") or "all"
    if side == "buy":
        scoped = filter_positions(positions, side="BUY")
    elif side == "sell":
        scoped = filter_positions(positions, side="SELL")
    else:
        scoped = [p for p in (positions or []) if isinstance(p, dict)]
    if not scoped:
        return None
    st = positions_stats(scoped)
    # 从高手数档位优先匹配
    tiers = list(rule.get("tiers") or [])
    for idx in range(len(tiers) - 1, -1, -1):
        tier = tiers[idx]
        min_lot = float(tier["min_lot"])
        pla = float(tier["pl_amount"])
        if st["volume"] < min_lot:
            continue
        if not amount_triggered(st["profit"], pla):
            continue
        return {
            "rule": RULE_LOT_PL_TIERS,
            "item_id": f"tier-{idx}",
            "tier_index": idx,
            "close_action": side if side in CLOSE_ACTIONS_SIDE else "all",
            "symbol": None,  # 按账户侧向平仓，不用单品种
            "scope_all_symbols": True,
            "min_lot": min_lot,
            "pl_amount": pla,
            "current_lot": st["volume"],
            "current_pl": st["profit"],
            "monitor_mode": MONITOR_LOOP,
            "remaining_times": 0,
            "message_core": (
                f"分档#{idx + 1} 总手数 {st['volume']:g}>={min_lot:g}"
                f" 且盈亏 {st['profit']:.2f} 达 {pla:g}"
            ),
        }
    return None


def describe_trigger(hit: dict | None) -> str:
    """把命中的风控规则写成一句可落库展示的原因（含规则名、参数、动作）。"""
    if not isinstance(hit, dict):
        return "账户风控"
    rule = str(hit.get("rule") or "")
    label = RULE_LABELS.get(rule, rule or "账户风控")
    core = str(hit.get("message_core") or "").strip() or label
    action = hit.get("close_action") or hit.get("action") or ""
    action_label = CLOSE_ACTION_LABELS.get(str(action), str(action) or "平仓")
    text = f"账户风控·{label}：{core}；动作 {action_label}"
    return text[:REASON_LIMIT]


def trigger_detail(hit: dict | None) -> dict | None:
    """结构化触发参数，供前端「计算依据」展开。"""
    if not isinstance(hit, dict):
        return None
    rule = str(hit.get("rule") or "")
    detail: dict[str, Any] = {
        "kind": "account_risk",
        "rule": rule or None,
        "rule_label": RULE_LABELS.get(rule) or rule or None,
        "close_action": hit.get("close_action") or hit.get("action"),
        "close_action_label": CLOSE_ACTION_LABELS.get(
            str(hit.get("close_action") or hit.get("action") or ""),
        ),
        "symbol": hit.get("symbol"),
        "message_core": hit.get("message_core"),
        "monitor_mode": hit.get("monitor_mode"),
        "item_id": hit.get("item_id"),
    }
    if rule == RULE_FLOAT_PL_RATIO:
        detail.update({
            "ratio_threshold": hit.get("threshold"),
            "current_ratio": hit.get("current_ratio"),
            "floating_pl": hit.get("floating_pl"),
            "balance": hit.get("balance"),
            "equity": hit.get("equity"),
        })
    elif rule == RULE_EQUITY_MIN:
        detail.update({
            "amount_threshold": hit.get("amount"),
            "equity": hit.get("equity"),
        })
    elif rule == RULE_SYMBOL_PL_ORDERS:
        detail.update({
            "pl_amount": hit.get("pl_amount"),
            "current_pl": hit.get("current_pl"),
            "order_count": hit.get("order_count"),
            "close_count": hit.get("close_count"),
        })
    elif rule == RULE_SYMBOL_PL_PROTECT:
        detail.update({
            "trigger_amount": hit.get("trigger_amount"),
            "narrow_amount": hit.get("narrow_amount"),
            "current_pl": hit.get("current_pl"),
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


def find_triggered_rule(
    risk: dict | None,
    *,
    balance: float,
    equity: float,
    positions: list | None,
    armed_map: dict[str, bool] | None = None,
) -> tuple[dict | None, dict[str, bool], list[dict]]:
    """按优先级找触发项。

    返回 (hit, new_armed_map, protect_armed_events)。
    """
    has_positions = bool(positions)
    armed = dict(armed_map or {})

    hit = should_trigger_float_pl(
        risk, balance=balance, equity=equity, has_positions=has_positions,
    )
    if hit:
        return hit, armed, []

    hit = should_trigger_equity_min(risk, equity=equity, has_positions=has_positions)
    if hit:
        return hit, armed, []

    hit = should_trigger_symbol_pl_orders(risk, positions=positions)
    if hit:
        return hit, armed, []

    new_armed, protect_hit, armed_events = evaluate_protect_items(
        risk, positions=positions, armed_map=armed,
    )
    if protect_hit:
        return protect_hit, new_armed, armed_events

    hit = should_trigger_lot_pl_tiers(risk, positions=positions)
    if hit:
        return hit, new_armed, armed_events

    return None, new_armed, armed_events


def apply_trigger_to_risk(
    risk: dict | None,
    rule_id: str,
    item_id: str | None = None,
) -> tuple[dict, dict]:
    """触发成功后更新配置。列表规则按 item_id 定位。"""
    cfg = normalize_risk(risk)
    disabled = False
    remaining = 0

    if rule_id in (RULE_FLOAT_PL_RATIO, RULE_EQUITY_MIN):
        rule = dict(cfg[rule_id])
        if rule.get("monitor_mode") == MONITOR_TIMES:
            remaining = max(0, int(rule.get("remaining_times") or 0) - 1)
            rule["remaining_times"] = remaining
            if remaining <= 0:
                rule["enabled"] = False
                rule["remaining_times"] = 0
                disabled = True
        cfg[rule_id] = rule
        remaining = int(rule.get("remaining_times") or 0)
        return cfg, {"remaining_times": remaining, "disabled": disabled, "matched": True}

    if rule_id in (RULE_SYMBOL_PL_ORDERS, RULE_SYMBOL_PL_PROTECT):
        items = list((cfg.get(rule_id) or {}).get("items") or [])
        matched = False
        for i, item in enumerate(items):
            if item_id and item.get("id") != item_id:
                continue
            if not item_id and i != 0:
                continue
            rule = dict(item)
            if rule.get("monitor_mode") == MONITOR_TIMES:
                remaining = max(0, int(rule.get("remaining_times") or 0) - 1)
                rule["remaining_times"] = remaining
                if remaining <= 0:
                    rule["enabled"] = False
                    rule["remaining_times"] = 0
                    disabled = True
            items[i] = rule
            remaining = int(rule.get("remaining_times") or 0)
            matched = True
            break
        cfg[rule_id] = {"items": items}
        return cfg, {
            "remaining_times": remaining, "disabled": disabled, "matched": matched,
        }

    # lot_pl_tiers 无次数，触发后不改开关
    return cfg, {"remaining_times": 0, "disabled": False, "matched": True}
