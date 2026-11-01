"""配置保存前的预检（dry-run）：不改任何运行态，只算「改了会怎样」。

节点侧做这个预检是因为只有节点知道真实持仓、合约规格和现价——服务端拿不到
这些，算不出新的止盈止损落在哪。

预检要回答的问题：
1. 新的 SL/TP 重算出来，现价是否已经在止盈 / 止损那一侧（crossed）？
   crossed 意味着这一改，节点下一拍就会市价平掉整仓——必须提前拦下。
2. 新的上限是否低于该规则已开笔数？
3. 手动分散仓是否已开过（配置锁定）？

本模块只做纯计算，不碰 MT5、不改 runner 状态。
"""
from __future__ import annotations

from typing import Optional

import manual_scatter
import signal_pl

RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rule_type_label(rule_type: int) -> str:
    return "逆势加仓" if int(rule_type or 0) == RULE_TYPE_COUNTER else "顺势加仓"


def describe_crossed(plan: dict) -> str:
    """把一个 crossed 的 plan 说成人话。

    两侧同时越价时两边都要说，不能只报一个——用户要知道自己改的哪几个
    目标价有问题。
    """
    side = str(plan.get("side") or "").upper()
    tp = plan.get("tp")
    sl = plan.get("sl")
    market = _as_float(plan.get("market"))
    direction = "多单" if side == "BUY" else "空单"
    crossed = plan.get("crossed_sides")
    if isinstance(crossed, (list, tuple, set)):
        kinds = [str(k) for k in crossed if str(k) in ("tp", "sl")]
    else:
        kinds = []
        if str(plan.get("crossed") or "") in ("tp", "sl"):
            kinds = [str(plan.get("crossed"))]
    bits: list[str] = []
    for kind in kinds:
        label = "止盈" if kind == "tp" else "止损"
        target = tp if kind == "tp" else sl
        if target is None:
            continue
        bits.append(
            f"{direction}按新配置算出的{label}价 {float(target):g}，"
            f"现价 {market:g} 已经越过，改动会立刻触发{label}平仓"
        )
    if not bits:
        which = "止盈" if plan.get("crossed") == "tp" else "止损"
        target = tp if plan.get("crossed") == "tp" else sl
        bits.append(
            f"{direction}按新配置算出的{which}价 {target}，"
            f"现价 {market:g} 已经越过，改动会立刻触发{which}平仓"
        )
    return "；".join(bits)


def check_sltp_crossed(
    positions: list[dict],
    *,
    rules: list[dict],
    tick_size: float,
    tick_value: float,
    digits: int,
    balance: Optional[float] = None,
    bid: float = 0.0,
    ask: float = 0.0,
    fallback_price: float = 0.0,
    manual_authority: bool = False,
) -> list[dict]:
    """用新规则重算共同止盈止损，检查现价是否已经越过。

    返回 crossed 列表，每项：
    {"code": "sltp_crossed", "side", "crossed": "tp"|"sl", "crossed_sides": [...],
     "tp", "sl", "market", "message"}

    manual_authority：手动分散仓的止盈价在场。此时节点**不会**写共同止盈
    （止盈听手动仓权威），所以只检查止损那一侧——拦一个永远不会被写入的
    目标价是误拦，尤其是在加仓收口、客户最需要调止损的阶段。
    """
    cfg = signal_pl.extract_config(rules)
    tp_amount = signal_pl.lot_pl_amount(cfg)
    ratio_on = signal_pl.float_pl_enabled(cfg)
    if manual_authority:
        tp_amount = None
    if (tp_amount is None and not ratio_on) or not positions:
        return []

    scoped = signal_pl.filter_lot_side(
        manual_scatter.exclude_manual(positions),
        signal_pl.lot_pl_close_action(cfg),
    )
    if not scoped:
        return []

    sl_amount = None
    if ratio_on and balance is not None:
        # 读不到余额就算不出止损金额；那时只检查止盈那一侧
        sl_amount = signal_pl.float_pl_loss_amount(cfg, balance)

    plans = signal_pl.plan_common_stops(
        scoped,
        tp_amount=tp_amount,
        sl_amount=sl_amount,
        tp_min_lot=signal_pl.lot_pl_min_lot(cfg),
        tick_size=_as_float(tick_size),
        tick_value=_as_float(tick_value),
        digits=int(digits or 0),
        bid=_as_float(bid),
        ask=_as_float(ask),
        fallback_price=_as_float(fallback_price),
    )

    out: list[dict] = []
    for plan in plans:
        kinds = [str(k) for k in (plan.get("crossed_sides") or []) if str(k) in ("tp", "sl")]
        if not kinds:
            continue
        item = {
            "code": "sltp_crossed",
            "side": str(plan.get("side") or "").upper(),
            "crossed": kinds[0],
            "crossed_sides": kinds,
            "tp": plan.get("tp"),
            "sl": plan.get("sl"),
            "market": plan.get("market"),
        }
        item["message"] = describe_crossed(item)
        out.append(item)
    return out


def check_limit_below_added(
    rules: list[dict], *, added_by_rule: Optional[dict] = None,
) -> list[dict]:
    """上限低于该规则已开笔数 → 阻断项。"""
    added_by_rule = added_by_rule or {}
    normalized: dict[int, int] = {}
    for key, value in (added_by_rule or {}).items():
        rule_type = _as_int(key, 0)
        n = _as_int(value, 0)
        if rule_type in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) and n > 0:
            normalized[rule_type] = max(normalized.get(rule_type, 0), n)

    out: list[dict] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
        added = normalized.get(rule_type, 0)
        if added <= 0:
            continue
        for field in ("total_lot_limit", "max_allow_num"):
            limit = _as_int(rule.get(field), 0)
            if limit <= 0 or limit >= added:
                continue
            label = rule_type_label(rule_type)
            out.append({
                "code": "limit_below_added",
                "rule_type": rule_type,
                "field": f"rules[type={rule_type}].{field}",
                "added": added,
                "limit": limit,
                "message": (
                    f"{label}已开 {added} 笔，新上限 {limit} 笔低于已开笔数。"
                    f"上限只能缩减剩余额度，不能低于已开笔数，请填写 ≥ {added} 的值。"
                ),
            })
    return out


def check_manual_locked(
    rules: list[dict], *, manual_locked: Optional[set] = None,
) -> list[dict]:
    """手动分散仓**已成交**的规则：整块配置锁定。

    挂单在盘上（PENDING）不算锁死：客户可以改参数，节点会撤旧单重算重挂。
    """
    manual_locked = manual_locked or set()
    out: list[dict] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
        if rule_type not in manual_locked:
            continue
        label = rule_type_label(rule_type)
        out.append({
            "code": "manual_locked",
            "rule_type": rule_type,
            "field": f"rules[type={rule_type}].manual_scatter",
            "message": (
                f"{label}手动分散仓已成交，配置锁定，不影响已开仓位"
            ),
        })
    return out


def check_batch_anchor(
    rules: list[dict], *, added_by_rule: Optional[dict] = None,
) -> list[dict]:
    """档位锚定校验：已开过的笔数区间不该被改。

    节点按「本规则第几次加仓」命中档位，所以第 N 次加仓必然落在包含 N 的
    那个档位里。已经开过的笔数（1..added）对应的档位参数如果被改，
    后续加仓就会命中不同的参数——等于在运行中偷改策略。

    校验规则：**已开笔数区间内的档位，其参数不能变**。这里只做结构性检查：
    追加的档位起始序号必须大于已开笔数（不能在已开区间里插新档）。
    """
    added_by_rule = added_by_rule or {}
    normalized: dict[int, int] = {}
    for key, value in (added_by_rule or {}).items():
        rule_type = _as_int(key, 0)
        n = _as_int(value, 0)
        if rule_type in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) and n > 0:
            normalized[rule_type] = max(normalized.get(rule_type, 0), n)

    out: list[dict] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not rule.get("batch_enabled"):
            continue
        rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
        added = normalized.get(rule_type, 0)
        if added <= 0:
            continue
        levels = [lv for lv in (rule.get("batch_levels") or []) if isinstance(lv, dict)]
        if not levels:
            continue
        # 档位区间必须连续且不重叠：命中靠区间，区间乱了命中就没意义
        prev_to = 0
        for idx, lv in enumerate(levels):
            pos_from = _as_int(lv.get("pos_from"), 0)
            pos_to = _as_int(lv.get("pos_to"), 0)
            if pos_from <= prev_to:
                out.append({
                    "code": "batch_inconsistent",
                    "rule_type": rule_type,
                    "field": f"rules[type={rule_type}].batch_levels[{idx}]",
                    "message": (
                        f"{rule_type_label(rule_type)}档位 {idx + 1} 的加仓笔数区间"
                        f"（{pos_from}~{pos_to}）与前一档重叠或倒退，档位区间必须首尾衔接"
                    ),
                })
                prev_to = max(prev_to, pos_to)
                continue
            if pos_to < pos_from:
                out.append({
                    "code": "batch_inconsistent",
                    "rule_type": rule_type,
                    "field": f"rules[type={rule_type}].batch_levels[{idx}]",
                    "message": (
                        f"{rule_type_label(rule_type)}档位 {idx + 1} 的终点小于起点"
                        f"（{pos_from}~{pos_to}）"
                    ),
                })
                prev_to = max(prev_to, pos_to)
                continue
            prev_to = pos_to
        # 已开笔数必须被某个档位覆盖：否则第 added+1 次加仓没有档位可命中
        if not any(
            _as_int(lv.get("pos_from"), 0) <= added < _as_int(lv.get("pos_to"), 0) + 1
            for lv in levels
        ):
            out.append({
                "code": "batch_inconsistent",
                "rule_type": rule_type,
                "field": f"rules[type={rule_type}].batch_levels",
                "message": (
                    f"{rule_type_label(rule_type)}已开 {added} 笔，但档位区间没有覆盖"
                    f"第 {added + 1} 次加仓，后续加仓会没有档位可命中"
                ),
            })
    return out


def summarize(blocks: list[dict]) -> str:
    """把阻断项拼成一句可读的提示。"""
    items = blocks or []
    if not items:
        return ""
    return "；".join(str(b.get("message") or "") for b in items)
