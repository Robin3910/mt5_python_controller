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

偏离基准按规则类型分开：
- 顺势：最近一笔开仓价（继续沿有利方向铺仓）；
- 逆势：持仓中不利方向最深的开仓价（多单取最低、空单取最高）。
  尚未出现更深逆势仓时即首仓价——顺势加在更高/更低价后，
  必须先回到首仓不利侧达到阈值，才会触发逆势加仓。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from bar_metrics import BAR_PERIOD

RULE_TYPE_COUNTER = 1  # 逆势
RULE_TYPE_TREND = 2    # 顺势

# 分批档位的加仓间距计算方式（与后台 strategy_templates.BATCH_CALC_TYPES 对齐）
CALC_POINT = "point"    # 固定点数
CALC_PRICE = "price"    # 指定绝对价位，到价触发
CALC_ATR = "atr"        # ATR 的平均波动作为间距
CALC_RANGE = "range"    # 已收盘 K 线的最大高低波幅作为间距
CALC_BAR_TYPES = (CALC_ATR, CALC_RANGE)

CALC_TYPE_LABEL = {
    CALC_POINT: "点数",
    CALC_PRICE: "指定价",
    CALC_ATR: "ATR",
    CALC_RANGE: "波幅",
}


def metric_key(metric: str, timeframe: object) -> str:
    """行情指标的预取键；执行器按此预取，判定层按此读取。"""
    return f"{metric}:{str(timeframe or '').strip().upper()}"


@dataclass
class PositionCtx:
    """判定加仓所需的实时上下文。"""
    direction: str          # 本次任务持仓方向：BUY / SELL
    position_count: int     # 当前该魔术号的持仓笔数
    base_volume: float      # 首单手数（倍率的基准）
    base_price: float       # 最近一笔开仓价（顺势偏离基准）
    price: float            # 当前市价
    point: float            # 品种最小价格变动单位
    add_count: int = 0      # 已加仓次数
    # ATR / 波幅的预取值（键见 metric_key，值是价格距离）。
    # 判定层不做 I/O，读 K 线由执行器在判定前完成。
    bar_metrics: dict = field(default_factory=dict)
    # 逆势偏离基准；未显式传入时回退为 base_price（单测可只填一个价）
    counter_base_price: Optional[float] = None

    def __post_init__(self) -> None:
        if self.counter_base_price is None:
            self.counter_base_price = self.base_price


@dataclass
class AddDecision:
    """一次加仓决策，同时携带做出该决策的全部计算依据。

    除交易参数外还留存基准价 / 现价 / 倍率等中间量，供上报「开单原因与计算规则」，
    让每一笔加仓单都能在后台还原出为什么加、按什么公式算出的手数。
    """
    rule_type: int
    action: str             # 加仓方向（与原持仓同向）
    volume: float
    deviation: float        # 实际偏离点数
    threshold: float        # 触发阈值点数
    level_index: Optional[int] = None   # 命中的分批档位序号（从 0 起）
    # —— 以下为计算依据快照，只用于说明，不参与判定 ——
    calc_type: str = CALC_POINT     # 间距计算方式
    timeframe: str = ""             # ATR / 波幅所用的 K 线周期
    metric_value: float = 0.0       # ATR / 波幅算出的价格距离
    target_price: float = 0.0       # 指定价模式配置的价位
    rule_index: int = 0         # 规则在策略快照 rules 中的原始下标
    direction: str = ""         # 持仓方向（判定基准）
    base_price: float = 0.0     # 本次规则实际使用的偏离基准价
    price: float = 0.0          # 触发时的市价
    point: float = 0.0          # 品种最小价格变动单位
    base_volume: float = 0.0    # 首单手数（倍率基准）
    lot_times: float = 1.0
    extra_lot: float = 0.0
    position_count: int = 0     # 触发前的持仓笔数
    add_count: int = 0          # 触发前的已加仓次数
    limit_kind: str = ""        # 本次生效的上限类型：total_lot_limit / max_allow_num
    limit_value: int = 0        # 对应的上限值，0 表示不限制

    @property
    def event_type(self) -> str:
        return "add_counter" if self.rule_type == RULE_TYPE_COUNTER else "add_trend"

    @property
    def type_label(self) -> str:
        return "逆势加仓" if self.rule_type == RULE_TYPE_COUNTER else "顺势加仓"

    @property
    def next_position_no(self) -> int:
        """本次加仓将成为该魔术号下的第几笔持仓。"""
        return self.position_count + 1


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


def counter_anchor_price(direction: str, open_prices: Sequence[float]) -> float:
    """逆势偏离锚点：多单取最低开仓价，空单取最高开仓价。

    尚未出现更深的逆势仓时，结果等于首仓开仓价；已有逆势仓时取最深一笔，
    以便后续逆势加仓仍按档位间距递进，而不会被中间的顺势加仓抬高/压低锚点。
    """
    prices = [float(p) for p in open_prices]
    if not prices:
        return 0.0
    is_buy = str(direction or "").strip().upper() == "BUY"
    return min(prices) if is_buy else max(prices)


def rule_base_price(rule_type: int, ctx: PositionCtx) -> float:
    """按规则类型选取偏离基准价。"""
    if rule_type == RULE_TYPE_COUNTER:
        return float(ctx.counter_base_price if ctx.counter_base_price is not None else ctx.base_price)
    return ctx.base_price


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


@dataclass
class Threshold:
    """一次间距解析的结果：触发所需的偏离点数，以及它是怎么来的。"""
    points: float
    calc_type: str = CALC_POINT
    timeframe: str = ""
    metric_value: float = 0.0
    target_price: float = 0.0


def resolve_threshold(rule_type: int, level: dict, ctx: PositionCtx) -> Threshold:
    """把档位配置的加仓间距折算成「触发所需的偏离点数」。

    四种方式殊途同归，都归一到点数，这样后面的判定与偏离比较完全共用一套逻辑：
    - 点数：配置值即阈值；
    - 指定价：换算成该价位相对基准价的偏离，方向必须与本规则一致，否则等于要求
      行情反向到价、永远不会成立，此时返回 0 表示本档不可用；
    - ATR / 波幅：行情算出的价格距离除以 point 得到点数。

    算不出来（未配置价位、行情未就绪）时 points 为 0，调用方据此跳过本次判定。
    """
    calc_type = str(level.get("calc_type") or CALC_POINT).strip().lower()

    if calc_type == CALC_PRICE:
        target = _as_float(level.get("price"), 0.0)
        if target <= 0:
            return Threshold(0.0, calc_type)
        base = rule_base_price(rule_type, ctx)
        points = deviation_points(rule_type, ctx.direction, base, target, ctx.point)
        return Threshold(max(points, 0.0), calc_type, target_price=target)

    if calc_type in CALC_BAR_TYPES:
        timeframe = str(level.get("timeframe") or "").strip().upper()
        distance = _as_float(ctx.bar_metrics.get(metric_key(calc_type, timeframe)), 0.0)
        points = distance / ctx.point if ctx.point > 0 and distance > 0 else 0.0
        return Threshold(points, calc_type, timeframe=timeframe, metric_value=distance)

    return Threshold(_as_float(level.get("point"), 0.0), CALC_POINT)


def evaluate_rule(rule: dict, ctx: PositionCtx, rule_index: int = 0) -> Optional[AddDecision]:
    """单条规则的加仓判定；不满足条件返回 None。

    rule_index 是该规则在策略快照中的原始下标，只用于在说明里指明是哪条规则命中。
    """
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
        # 分批模式：方向可独立配置，笔数上限用 total_lot_limit，间距按档位的计算方式解析
        if not action_matches(rule.get("batch_action") or rule.get("action"), ctx.direction):
            return None
        limit = _as_int(rule.get("total_lot_limit"), 0)
        if limit and ctx.position_count >= limit:
            return None
        gap = resolve_threshold(rule_type, level, ctx)
        lot_times = _as_float(level.get("lot_times"), 1.0)
        extra_lot = _as_float(level.get("extra_lot"), 0.0)
        limit_kind, limit_value = "total_lot_limit", limit
    else:
        # 分批未启用（或已超出所有档位区间）：走基础参数 + 次数上限，基础参数只支持点数
        if rule.get("batch_enabled"):
            return None
        if not action_matches(rule.get("action"), ctx.direction):
            return None
        max_allow = _as_int(rule.get("max_allow_num"), 0)
        if max_allow and ctx.add_count >= max_allow:
            return None
        gap = Threshold(_as_float(rule.get("point"), 0.0))
        lot_times = _as_float(rule.get("lot_times"), 1.0)
        extra_lot = _as_float(rule.get("extra_lot"), 0.0)
        limit_kind, limit_value = "max_allow_num", max_allow

    threshold = gap.points
    if threshold <= 0:
        return None
    base = rule_base_price(rule_type, ctx)
    moved = deviation_points(rule_type, ctx.direction, base, ctx.price, ctx.point)
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
        calc_type=gap.calc_type,
        timeframe=gap.timeframe,
        metric_value=gap.metric_value,
        target_price=gap.target_price,
        rule_index=rule_index,
        direction=str(ctx.direction).upper(),
        base_price=base,
        price=ctx.price,
        point=ctx.point,
        base_volume=ctx.base_volume,
        lot_times=lot_times,
        extra_lot=extra_lot,
        position_count=ctx.position_count,
        add_count=ctx.add_count,
        limit_kind=limit_kind,
        limit_value=limit_value,
    )


def evaluate(rules: list, ctx: PositionCtx) -> Optional[AddDecision]:
    """按规则顺序取第一条命中的加仓决策（逆势优先于顺势）。

    排序只影响判定优先级，决策里记录的仍是规则在原快照中的下标。
    """
    indexed = [(i, r) for i, r in enumerate(rules or []) if isinstance(r, dict)]
    ordered = sorted(indexed, key=lambda item: _as_int(item[1].get("type"), RULE_TYPE_COUNTER))
    for index, rule in ordered:
        decision = evaluate_rule(rule, ctx, index)
        if decision is not None:
            return decision
    return None


# ======================================================================
# 开单原因与计算规则的说明生成
#
# 加仓单是节点按规则自主下的，后台看不到判定过程。这里把决策还原成三种形态：
# 人读的说明、结构化明细、以及能塞进 MT5 备注的紧凑编码，随上报一起送回后台。
# ======================================================================

# MT5 订单备注上限约 31 字符，紧凑编码必须远短于该长度
MT5_COMMENT_LIMIT = 31

# MT5 备注里标识间距来历的单字母
_CALC_COMMENT_CODE = {
    CALC_POINT: "D",    # deviation，固定点数
    CALC_PRICE: "P",    # price，指定价位
    CALC_ATR: "A",
    CALC_RANGE: "W",    # wave，波幅
}


def _trim(value: float, digits: int) -> str:
    """按位数格式化并去掉尾随零，避免说明里出现 0.10000000000000001。"""
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _limit_text(decision: AddDecision) -> str:
    """本次生效的上限说明。"""
    if not decision.limit_value:
        return "不限"
    if decision.limit_kind == "total_lot_limit":
        return f"分批笔数上限 {decision.limit_value}"
    return f"最大加仓次数 {decision.limit_value}"


def gap_text(decision: AddDecision) -> str:
    """加仓间距的来历：点数直接给值，其余方式说明折算成了多少点。"""
    points = f"{_trim(decision.threshold, 1)} 点"
    if decision.calc_type == CALC_PRICE:
        return f"到价 {_trim(decision.target_price, 5)}（折合 {points}）"
    if decision.calc_type in CALC_BAR_TYPES:
        label = CALC_TYPE_LABEL[decision.calc_type]
        return (
            f"{label}({decision.timeframe},{BAR_PERIOD}) "
            f"{_trim(decision.metric_value, 5)}（折合 {points}）"
        )
    return f"阈值 {points}"


def describe_decision(decision: AddDecision) -> str:
    """把加仓决策写成一句话的开单原因，随上报存入事件说明字段。

    示例：逆势加仓 · 规则#0 · 分批档位#1：BUY 基准价 2400.5 → 现价 2398，
    逆向偏离 200 点 ≥ 阈值 200 点；手数 = 首单 0.1 × 倍数 1.2 + 追加 0 = 0.12 手；
    本次第 5 笔，触发前持仓 4 笔 / 已加仓 3 次，分批笔数上限 10
    """
    where = f"规则#{decision.rule_index}"
    if decision.level_index is not None:
        where += f" · 分批档位#{decision.level_index}"
    toward = "逆向" if decision.rule_type == RULE_TYPE_COUNTER else "顺向"
    return (
        f"{decision.type_label} · {where}："
        f"{decision.direction} 基准价 {_trim(decision.base_price, 5)}"
        f" → 现价 {_trim(decision.price, 5)}，"
        f"{toward}偏离 {_trim(decision.deviation, 1)} 点 ≥ {gap_text(decision)}；"
        f"手数 = 首单 {_trim(decision.base_volume, 2)} × 倍数 {_trim(decision.lot_times, 2)}"
        f" + 追加 {_trim(decision.extra_lot, 2)} = {_trim(decision.volume, 2)} 手；"
        f"本次第 {decision.next_position_no} 笔，触发前持仓 {decision.position_count} 笔"
        f" / 已加仓 {decision.add_count} 次，{_limit_text(decision)}"
    )


def decision_detail(decision: AddDecision) -> dict:
    """加仓决策的结构化明细，供后台逐项展示计算参数。"""
    return {
        "kind": "add",
        "rule_type": decision.rule_type,
        "rule_type_label": decision.type_label,
        "rule_index": decision.rule_index,
        "level_index": decision.level_index,
        "batch": decision.level_index is not None,
        "direction": decision.direction,
        "base_price": decision.base_price,
        "price": decision.price,
        "point": decision.point,
        "deviation": round(decision.deviation, 2),
        "threshold": round(decision.threshold, 2),
        "calc_type": decision.calc_type,
        "calc_type_label": CALC_TYPE_LABEL.get(decision.calc_type, decision.calc_type),
        "gap_source": gap_text(decision),
        "timeframe": decision.timeframe or None,
        "bar_period": BAR_PERIOD if decision.calc_type in CALC_BAR_TYPES else None,
        "metric_value": decision.metric_value or None,
        "target_price": decision.target_price or None,
        "base_volume": decision.base_volume,
        "lot_times": decision.lot_times,
        "extra_lot": decision.extra_lot,
        "volume": decision.volume,
        "volume_formula": (
            f"{_trim(decision.base_volume, 2)} × {_trim(decision.lot_times, 2)}"
            f" + {_trim(decision.extra_lot, 2)} = {_trim(decision.volume, 2)}"
        ),
        "position_count": decision.position_count,
        "add_count": decision.add_count,
        "next_position_no": decision.next_position_no,
        "limit_kind": decision.limit_kind,
        "limit_value": decision.limit_value,
    }


def decision_comment(decision: AddDecision) -> str:
    """加仓单写进 MT5 的紧凑备注：R=规则类型，L=分批档位，末段=计算方式+偏离点数。

    MT5 备注只有约 31 字符，放不下完整说明，这里只保留能反查到决策的最小信息：
    末段首字母即间距的来历（D 点数 / P 指定价 / A ATR / W 波幅）。
    """
    parts = [f"R{decision.rule_type}"]
    if decision.level_index is not None:
        parts.append(f"L{decision.level_index}")
    parts.append(f"{_CALC_COMMENT_CODE.get(decision.calc_type, 'D')}{int(round(decision.deviation))}")
    return "".join(parts)[:MT5_COMMENT_LIMIT]
