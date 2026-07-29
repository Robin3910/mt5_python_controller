"""策略加仓的纯判定逻辑（无 I/O，可单独单元测试）。

规则来自服务端随 strategy_start 下发的快照，字段与后台「策略模版」一致：

- type：1=逆势加仓，2=顺势加仓
- status：0=关闭，1=启用
- action：监控方向 all | buy | sell
- point：触发点数，偏离达到 point × Point() 后加仓
- lot_times / extra_lot：手数 = lot_times × 基础手数 + extra_lot
- max_allow_num：最大加仓次数（未启用分批时的次数上限）
- batch_enabled / batch_levels：分批档位，按当前持仓笔数命中不同的点数与倍数
- total_lot_limit：分批持仓笔数上限（与后台档位末笔一致）

逆势 = 价格朝持仓不利方向偏离后同向加仓；顺势 = 朝有利方向偏离后同向加仓。
两者都只加与原持仓同方向的仓位。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

RULE_TYPE_COUNTER = 1  # 逆势
RULE_TYPE_TREND = 2    # 顺势


@dataclass
class PositionCtx:
    """判定加仓所需的实时上下文。"""
    direction: str          # 本次任务持仓方向：BUY / SELL
    position_count: int     # 当前该魔术号的持仓笔数
    base_volume: float      # 首单手数（倍率的基准）
    base_price: float       # 最近一笔订单的开仓价（偏离的基准）
    price: float            # 当前市价
    point: float            # 品种最小价格变动单位
    add_count: int = 0      # 已加仓次数


@dataclass
class AddDecision:
    """一次加仓决策。"""
    rule_type: int
    action: str             # 加仓方向（与原持仓同向）
    volume: float
    deviation: float        # 实际偏离点数
    threshold: float        # 触发阈值点数
    level_index: Optional[int] = None   # 命中的分批档位序号（从 0 起）

    @property
    def event_type(self) -> str:
        return "add_counter" if self.rule_type == RULE_TYPE_COUNTER else "add_trend"


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def action_matches(rule_action: object, direction: str) -> bool:
    """规则的监控方向是否覆盖当前持仓方向。"""
    act = str(rule_action or "all").strip().lower()
    if act == "all":
        return True
    return act == str(direction or "").strip().lower()


def pick_batch_level(rule: dict, next_position_no: int) -> tuple[Optional[dict], Optional[int]]:
    """按「下一笔的序号」命中分批档位；未启用或未命中返回 (None, None)。"""
    if not rule.get("batch_enabled"):
        return None, None
    for idx, level in enumerate(rule.get("batch_levels") or []):
        if not isinstance(level, dict):
            continue
        lo = _as_int(level.get("pos_from"), 0)
        hi = _as_int(level.get("pos_to"), 0)
        if lo <= next_position_no <= hi:
            return level, idx
    return None, None


def deviation_points(rule_type: int, direction: str, base_price: float,
                     price: float, point: float) -> float:
    """当前偏离的点数；负值表示偏离方向与该规则关注的方向相反。

    逆势关注「不利方向」：多单下跌 / 空单上涨。
    顺势关注「有利方向」：多单上涨 / 空单下跌。
    """
    if point <= 0:
        return 0.0
    diff = price - base_price
    is_buy = str(direction or "").strip().upper() == "BUY"
    if rule_type == RULE_TYPE_COUNTER:
        moved = -diff if is_buy else diff
    else:
        moved = diff if is_buy else -diff
    return moved / point


def evaluate_rule(rule: dict, ctx: PositionCtx) -> Optional[AddDecision]:
    """单条规则的加仓判定；不满足条件返回 None。"""
    if not rule or not _as_int(rule.get("status"), 0):
        return None
    rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
    if rule_type not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
        return None
    if ctx.position_count <= 0 or ctx.point <= 0:
        return None

    next_no = ctx.position_count + 1
    level, level_index = pick_batch_level(rule, next_no)

    if level is not None:
        # 分批模式：方向可独立配置，笔数上限用 total_lot_limit
        if not action_matches(rule.get("batch_action") or rule.get("action"), ctx.direction):
            return None
        limit = _as_int(rule.get("total_lot_limit"), 0)
        if limit and ctx.position_count >= limit:
            return None
        threshold = _as_float(level.get("point"), 0.0)
        lot_times = _as_float(level.get("lot_times"), 1.0)
        extra_lot = _as_float(level.get("extra_lot"), 0.0)
    else:
        # 分批未启用（或已超出所有档位区间）：走基础参数 + 次数上限
        if rule.get("batch_enabled"):
            return None
        if not action_matches(rule.get("action"), ctx.direction):
            return None
        max_allow = _as_int(rule.get("max_allow_num"), 0)
        if max_allow and ctx.add_count >= max_allow:
            return None
        threshold = _as_float(rule.get("point"), 0.0)
        lot_times = _as_float(rule.get("lot_times"), 1.0)
        extra_lot = _as_float(rule.get("extra_lot"), 0.0)

    if threshold <= 0:
        return None
    moved = deviation_points(rule_type, ctx.direction, ctx.base_price, ctx.price, ctx.point)
    if moved < threshold:
        return None

    volume = lot_times * ctx.base_volume + extra_lot
    if volume <= 0:
        return None
    return AddDecision(
        rule_type=rule_type,
        action=str(ctx.direction).upper(),
        volume=round(volume, 2),
        deviation=moved,
        threshold=threshold,
        level_index=level_index,
    )


def evaluate(rules: list, ctx: PositionCtx) -> Optional[AddDecision]:
    """按规则顺序取第一条命中的加仓决策（逆势优先于顺势）。"""
    ordered = sorted(
        (r for r in (rules or []) if isinstance(r, dict)),
        key=lambda r: _as_int(r.get("type"), RULE_TYPE_COUNTER),
    )
    for rule in ordered:
        decision = evaluate_rule(rule, ctx)
        if decision is not None:
            return decision
    return None
