"""策略加仓的纯判定逻辑（无 I/O，可单独单元测试）。

规则来自服务端随 strategy_start 下发的快照，字段与后台「策略模版」一致：

- type：1=逆势加仓，2=顺势加仓
- status：0=关闭，1=启用
- action：监控方向 all | buy | sell
- point：触发点数。顺势是偏离达到 point × Point() 后市价加仓；
  逆势由 plan_counter_limit 预先算好挂单价，不在这里等现价走到阈值
- lot_times / extra_lot：手数 = lot_times × 基础手数 + extra_lot
- max_allow_num：最大加仓次数（未启用分批时，按本规则已加次数计，不含开仓）
- batch_enabled / batch_levels：分批档位，按本规则第几次加仓命中不同的点数与倍数
- total_lot_limit：本规则最多加仓笔数（不含开仓，与另一条规则独立）

逆势 = 价格朝持仓不利方向偏离后同向加仓；顺势 = 朝有利方向偏离后同向加仓。
两者都只加与原持仓同方向的仓位。

偏离基准按规则类型分开：
- 顺势：最近一笔开仓价（继续沿有利方向铺仓）；
- 逆势：持仓中不利方向最深的开仓价（多单取最低、空单取最高）。
  尚未出现更深逆势仓时即首仓价——顺势加在更高/更低价后，
  逆势限价仍挂在首仓不利侧，顺势仓上的回撤不会把它成交。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from bar_metrics import BAR_PERIOD

# 加仓单备注：R1L0D121 / R1D100 / R1L2Fa1b2c3d4；避开模版2 的 R3B、手动仓 M1/M2、首单任意备注
_ADD_COMMENT_RE = re.compile(r"^R([12])(?:L|[DPAW]|$)")
# SMC 加仓备注末段是计算方式字母 + 8 位哈希，重连后据此补「已用过」
_SMC_KEY_RE = re.compile(r"^R[12]L\d+[FOZB]([0-9a-f]{8})")

RULE_TYPE_COUNTER = 1  # 逆势
RULE_TYPE_TREND = 2    # 顺势

# 分批档位的加仓间距计算方式（与后台 strategy_templates.BATCH_CALC_TYPES 对齐）
CALC_POINT = "point"    # 固定点数
CALC_PRICE = "price"    # 指定绝对价位，到价触发
CALC_ATR = "atr"        # ATR 的平均波动作为间距
CALC_RANGE = "range"    # 已收盘 K 线的最大高低波幅作为间距
# 以下只是标识，节点不计算指标；价位来自 TradingView 推送（与后台 BATCH_CALC_SMC_TYPES 对齐）
CALC_FVG = "fvg"
CALC_OB = "ob"
CALC_PDZ = "pdz"
CALC_FIBO = "fibo"
CALC_SMC_TYPES = (CALC_FVG, CALC_OB, CALC_PDZ, CALC_FIBO)
CALC_BAR_TYPES = (CALC_ATR, CALC_RANGE)

CALC_TYPE_LABEL = {
    CALC_POINT: "点数",
    CALC_PRICE: "指定价",
    CALC_ATR: "ATR",
    CALC_RANGE: "波幅",
    CALC_FVG: "FVG",
    CALC_OB: "OB",
    CALC_PDZ: "PDZ",
    CALC_FIBO: "FIBO",
}


def metric_key(metric: str, timeframe: object) -> str:
    """行情指标的预取键；执行器按此预取，判定层按此读取。"""
    return f"{metric}:{str(timeframe or '').strip().upper()}"


@dataclass
class PositionCtx:
    """判定加仓所需的实时上下文。"""
    direction: str          # 本次任务持仓方向：BUY / SELL
    position_count: int     # 当前该魔术号的持仓笔数（说明用，不含手动仓）
    base_volume: float      # 首单手数（倍率的基准）
    base_price: float       # 最近一笔开仓价（顺势偏离基准）
    price: float            # 当前市价
    point: float            # 品种最小价格变动单位
    add_count: int = 0      # 已加仓总次数；未填 add_counts 时作为单规则回退
    # 按规则类型的已加次数（不含开仓）；有值时优先于 add_count
    add_counts: dict = field(default_factory=dict)
    # ATR / 波幅的预取值（键见 metric_key，值是价格距离）。
    # 判定层不做 I/O，读 K 线由执行器在判定前完成。
    bar_metrics: dict = field(default_factory=dict)
    # 逆势偏离基准；未显式传入时回退为 base_price（单测可只填一个价）
    counter_base_price: Optional[float] = None
    # 当前任务的 SMC 价位副本，以及本节点已经用过的记录哈希
    smc: dict = field(default_factory=dict)
    smc_used: set = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.counter_base_price is None:
            self.counter_base_price = self.base_price

    def rule_add_count(self, rule_type: int) -> int:
        """本规则已加仓次数；开仓不计。未分类时回退到 add_count。"""
        if self.add_counts:
            try:
                return int(self.add_counts.get(rule_type, 0) or 0)
            except (TypeError, ValueError):
                return 0
        return self.add_count


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
    target_price: float = 0.0       # 指定价 / SMC 的绝对价位
    limit_price: float = 0.0       # 逆势限价的挂单价；市价判定为 0
    rule_index: int = 0         # 规则在策略快照 rules 中的原始下标
    direction: str = ""         # 持仓方向（判定基准）
    base_price: float = 0.0     # 本次规则实际使用的偏离基准价
    price: float = 0.0          # 触发时的市价
    point: float = 0.0          # 品种最小价格变动单位
    base_volume: float = 0.0    # 首单手数（倍率基准）
    lot_times: float = 1.0
    extra_lot: float = 0.0
    position_count: int = 0     # 触发前的持仓笔数
    add_count: int = 0          # 触发前本规则已加仓次数
    limit_kind: str = ""        # 本次生效的上限类型：total_lot_limit / max_allow_num
    limit_value: int = 0        # 对应的上限值，0 表示不限制
    smc_key: str = ""           # symbol|tf|type|side|price 的 8 位哈希，用过即不再用
    smc_side: int = 0           # 1=支撑，-1=阻力
    smc_received_at: float = 0.0

    @property
    def event_type(self) -> str:
        return "add_counter" if self.rule_type == RULE_TYPE_COUNTER else "add_trend"

    @property
    def type_label(self) -> str:
        return "逆势加仓" if self.rule_type == RULE_TYPE_COUNTER else "顺势加仓"

    @property
    def next_rule_add_no(self) -> int:
        """本次将是本规则的第几次加仓（从 1 起，不含开仓）。"""
        return self.add_count + 1

    @property
    def next_position_no(self) -> int:
        """本次加仓将成为该魔术号下的第几笔持仓（说明用）。"""
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
    """按「本规则下一次加仓序号」命中分批档位；未启用或未命中返回 (None, None)。"""
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


def parse_add_rule_type(comment: object) -> Optional[int]:
    """从加仓单 MT5 备注解析规则类型。R1=逆势，R2=顺势；R3B / M1 / 首单返回 None。"""
    text = str(comment or "").strip()
    m = _ADD_COMMENT_RE.match(text)
    if not m:
        return None
    return int(m.group(1))


def count_adds_by_rule(positions: Sequence | None) -> dict[int, int]:
    """按备注统计各规则已加仓次数；开仓与手动仓不计。"""
    counts = {RULE_TYPE_COUNTER: 0, RULE_TYPE_TREND: 0}
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        rule_type = parse_add_rule_type(pos.get("comment"))
        if rule_type in counts:
            counts[rule_type] += 1
    return counts


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


def smc_record_key(symbol: object, timeframe: object, calc_type: object,
                   side: object, price: object) -> str:
    """一条 SMC 记录在单次任务里的身份：symbol|tf|type|side|price 的 8 位哈希。"""
    token = _trim(_as_float(price), 8)
    raw = (
        f"{str(symbol or '').strip().upper()}|{str(timeframe or '').strip().upper()}"
        f"|{str(calc_type or '').strip().lower()}|{int(_as_int(side))}|"
        f"{token}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def smc_key_from_comment(comment: object) -> Optional[str]:
    """从已成交的 SMC 加仓备注里取出哈希；其它备注返回 None。"""
    match = _SMC_KEY_RE.match(str(comment or "").strip())
    return match.group(1) if match else None


def _smc_side_for(direction: str) -> Optional[int]:
    """多单只看支撑（1），空单只看阻力（-1）。"""
    name = str(direction or "").strip().upper()
    if name == "BUY":
        return 1
    if name == "SELL":
        return -1
    return None


def _smc_is_adverse(direction: str, anchor: float, level_price: float) -> bool:
    """价位必须在逆势锚点的不利一侧，否则触价也不是逆势加仓。"""
    if anchor <= 0 or level_price <= 0:
        return False
    if str(direction or "").strip().upper() == "BUY":
        return level_price < anchor
    return level_price > anchor


def pick_smc_level(level: dict, ctx: PositionCtx) -> Optional[dict]:
    """在当前副本里选出本档可用的最近一条 SMC 价位。

    顺序：周期、类型、side、锚点不利侧、本节点未用过，再按离现价的距离取最近。
    距离相同保留数据里靠前的一条。没有可用记录返回 None。
    """
    calc_type = str(level.get("calc_type") or "").strip().lower()
    want_tf = str(level.get("timeframe") or "").strip().upper()
    blob = ctx.smc if isinstance(ctx.smc, dict) else {}
    if str(blob.get("tf") or "").strip().upper() != want_tf or ctx.price <= 0:
        return None
    want_side = _smc_side_for(ctx.direction)
    if want_side is None:
        return None
    anchor = rule_base_price(RULE_TYPE_COUNTER, ctx)
    used = {str(item) for item in (ctx.smc_used or set())}
    symbol = blob.get("symbol") or ""
    received_at = _as_float(blob.get("received_at"), 0.0)
    best: Optional[dict] = None
    best_dist: Optional[float] = None
    for item in blob.get("levels") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != calc_type:
            continue
        side = _as_int(item.get("side"), 0)
        price = _as_float(item.get("price"), 0.0)
        if side != want_side or price <= 0:
            continue
        if not _smc_is_adverse(ctx.direction, anchor, price):
            continue
        key = smc_record_key(symbol, want_tf, calc_type, side, price)
        if key in used:
            continue
        dist = abs(price - ctx.price)
        if best is None or best_dist is None or dist < best_dist:
            best_dist = dist
            best = {
                "price": price, "side": side, "key": key, "received_at": received_at,
            }
    return best


@dataclass
class Threshold:
    """一次间距解析的结果：触发所需的偏离点数，以及它是怎么来的。"""
    points: float
    calc_type: str = CALC_POINT
    timeframe: str = ""
    metric_value: float = 0.0
    target_price: float = 0.0
    smc_key: str = ""
    smc_side: int = 0
    smc_received_at: float = 0.0


def resolve_threshold(rule_type: int, level: dict, ctx: PositionCtx) -> Threshold:
    """把档位配置的加仓间距折算成「触发所需的偏离点数」。

    点数、指定价、ATR / 波幅，以及 SMC 价位，最后都归一到点数，后面的偏离比较共用一套逻辑：
    - 点数：配置值即阈值；
    - 指定价：换算成该价位相对基准价的偏离，方向必须与本规则一致，否则等于要求
      行情反向到价、永远不会成立，此时返回 0 表示本档不可用；
    - ATR / 波幅：行情算出的价格距离除以 point 得到点数；
    - fvg / ob / pdz / fibo：从推送副本里选出一条价位，再按指定价同样折成偏离点数。

    算不出来（未配置价位、行情未就绪、没有可用 SMC 记录）时 points 为 0，调用方据此跳过本次判定。
    """
    calc_type = str(level.get("calc_type") or CALC_POINT).strip().lower()

    if calc_type in CALC_SMC_TYPES:
        timeframe = str(level.get("timeframe") or "").strip().upper()
        if rule_type != RULE_TYPE_COUNTER:
            return Threshold(0.0, calc_type, timeframe=timeframe)
        picked = pick_smc_level(level, ctx)
        if picked is None:
            return Threshold(0.0, calc_type, timeframe=timeframe)
        base = rule_base_price(rule_type, ctx)
        points = deviation_points(
            rule_type, ctx.direction, base, picked["price"], ctx.point,
        )
        return Threshold(
            max(points, 0.0), calc_type, timeframe=timeframe,
            target_price=picked["price"], smc_key=picked["key"],
            smc_side=int(picked["side"]), smc_received_at=picked["received_at"],
        )

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


@dataclass
class _AddPrep:
    """一条规则在比较现价之前就能确定的加仓参数。"""
    rule_type: int
    level_index: Optional[int]
    gap: Threshold
    lot_times: float
    extra_lot: float
    limit_kind: str
    limit_value: int
    rule_adds: int
    base: float
    volume: float


def _prepare_rule(rule: dict, ctx: PositionCtx) -> Optional[_AddPrep]:
    """方向、档位、笔数上限和间距都齐了才返回；现价够不够由调用方另判。"""
    if not rule or not _as_int(rule.get("status"), 0):
        return None
    rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
    if rule_type not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
        return None
    if ctx.position_count <= 0 or ctx.point <= 0:
        return None

    rule_adds = ctx.rule_add_count(rule_type)
    # 档位按「本规则第几次加仓」命中（从 1 起，开仓不占格）；pos_from 一律按字面解释
    level, level_index = pick_batch_level(rule, rule_adds + 1)

    if level is not None:
        # 分批模式：方向可独立配置，笔数上限用本规则已加次数，间距按档位解析
        if not action_matches(rule.get("batch_action") or rule.get("action"), ctx.direction):
            return None
        limit = _as_int(rule.get("total_lot_limit"), 0)
        if limit and rule_adds >= limit:
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
        if max_allow and rule_adds >= max_allow:
            return None
        gap = Threshold(_as_float(rule.get("point"), 0.0))
        lot_times = _as_float(rule.get("lot_times"), 1.0)
        extra_lot = _as_float(rule.get("extra_lot"), 0.0)
        limit_kind, limit_value = "max_allow_num", max_allow

    if gap.points <= 0:
        return None
    volume = round(lot_times * ctx.base_volume + extra_lot, 2)
    if volume <= 0:
        return None
    return _AddPrep(
        rule_type=rule_type,
        level_index=level_index,
        gap=gap,
        lot_times=lot_times,
        extra_lot=extra_lot,
        limit_kind=limit_kind,
        limit_value=limit_value,
        rule_adds=rule_adds,
        base=rule_base_price(rule_type, ctx),
        volume=volume,
    )


def _build_decision(
    prep: _AddPrep, ctx: PositionCtx, rule_index: int, *,
    deviation: float, price: float, limit_price: float = 0.0,
) -> AddDecision:
    gap = prep.gap
    return AddDecision(
        rule_type=prep.rule_type,
        action=str(ctx.direction).upper(),
        volume=prep.volume,
        deviation=deviation,
        threshold=gap.points,
        level_index=prep.level_index,
        calc_type=gap.calc_type,
        timeframe=gap.timeframe,
        metric_value=gap.metric_value,
        target_price=gap.target_price,
        limit_price=limit_price,
        rule_index=rule_index,
        direction=str(ctx.direction).upper(),
        base_price=prep.base,
        price=price,
        point=ctx.point,
        base_volume=ctx.base_volume,
        lot_times=prep.lot_times,
        extra_lot=prep.extra_lot,
        position_count=ctx.position_count,
        add_count=prep.rule_adds,
        limit_kind=prep.limit_kind,
        limit_value=prep.limit_value,
        smc_key=gap.smc_key,
        smc_side=gap.smc_side,
        smc_received_at=gap.smc_received_at,
    )


def counter_limit_price(direction: str, base: float, point: float, gap: Threshold) -> float:
    """下一档逆势限价的绝对价格。点数 / ATR / 波幅从锚点向不利侧量；指定价和 SMC 用绝对价。"""
    if gap.calc_type == CALC_PRICE or gap.calc_type in CALC_SMC_TYPES:
        return gap.target_price if gap.target_price > 0 else 0.0
    if base <= 0 or point <= 0 or gap.points <= 0:
        return 0.0
    distance = gap.points * point
    if str(direction or "").strip().upper() == "BUY":
        return base - distance
    return base + distance


def evaluate_rule(rule: dict, ctx: PositionCtx, rule_index: int = 0) -> Optional[AddDecision]:
    """单条规则的市价加仓判定；现价偏离不够返回 None。

    逆势的常态路径不走这里，见 plan_counter_limit。本函数仍保留逆势判定，
    供「偏离是否已经够」的单元测试使用。
    rule_index 是该规则在策略快照中的原始下标，只用于在说明里指明是哪条规则命中。
    """
    prep = _prepare_rule(rule, ctx)
    if prep is None:
        return None
    moved = deviation_points(prep.rule_type, ctx.direction, prep.base, ctx.price, ctx.point)
    if moved < prep.gap.points:
        return None
    return _build_decision(prep, ctx, rule_index, deviation=moved, price=ctx.price)


def evaluate(
    rules: list, ctx: PositionCtx, *, rule_types: Optional[Sequence[int]] = None,
) -> Optional[AddDecision]:
    """按规则顺序取第一条命中的市价加仓决策（逆势优先于顺势）。

    rule_types 给定时只跑这些类型。执行器里逆势已改成限价，市价判定只传入顺势。
    排序只影响判定优先级，决策里记录的仍是规则在原快照中的下标。
    """
    allowed = None if rule_types is None else {int(item) for item in rule_types}
    indexed = [(i, r) for i, r in enumerate(rules or []) if isinstance(r, dict)]
    ordered = sorted(indexed, key=lambda item: _as_int(item[1].get("type"), RULE_TYPE_COUNTER))
    for index, rule in ordered:
        rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
        if allowed is not None and rule_type not in allowed:
            continue
        decision = evaluate_rule(rule, ctx, index)
        if decision is not None:
            return decision
    return None


def plan_counter_limit(
    rules: list, ctx: PositionCtx, *, digits: int = 0,
) -> Optional[AddDecision]:
    """算出下一档逆势限价，不等现价走到阈值。

    没有启用的逆势规则、档位用尽或间距算不出时返回 None。挂单价按 digits 四舍五入。
    偏离字段写成计划阈值，备注才不会跟着现价每一拍变。
    """
    for index, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            continue
        if _as_int(rule.get("type"), RULE_TYPE_COUNTER) != RULE_TYPE_COUNTER:
            continue
        prep = _prepare_rule(rule, ctx)
        if prep is None:
            continue
        raw = counter_limit_price(ctx.direction, prep.base, ctx.point, prep.gap)
        if raw <= 0:
            continue
        limit_price = round(raw, digits) if digits > 0 else raw
        if limit_price <= 0:
            continue
        return _build_decision(
            prep, ctx, index,
            deviation=prep.gap.points, price=ctx.price, limit_price=limit_price,
        )
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
    CALC_FVG: "F",
    CALC_OB: "O",
    CALC_PDZ: "Z",
    CALC_FIBO: "B",
}
_COMMENT_CODE_CALC = {code: name for name, code in _CALC_COMMENT_CODE.items()}
# 逆势挂单备注：R1、可选档位、计算方式字母，SMC 再接 8 位哈希。偏离数字不参与身份比较。
_COUNTER_PENDING_RE = re.compile(r"^R1(?:L(\d+))?([DPAWFOZB])([0-9a-f]{8})?")


def _trim(value: float, digits: int) -> str:
    """按位数格式化并去掉尾随零，避免说明里出现 0.10000000000000001。"""
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _limit_text(decision: AddDecision) -> str:
    """本次生效的上限说明。"""
    if not decision.limit_value:
        return "不限"
    if decision.limit_kind == "total_lot_limit":
        return f"本规则加仓上限 {decision.limit_value}"
    return f"最大加仓次数 {decision.limit_value}"


def gap_text(decision: AddDecision) -> str:
    """加仓间距的来历：点数直接给值，其余方式说明折算成了多少点。"""
    points = f"{_trim(decision.threshold, 1)} 点"
    if decision.calc_type == CALC_PRICE:
        return f"到价 {_trim(decision.target_price, 5)}（折合 {points}）"
    if decision.calc_type in CALC_SMC_TYPES:
        label = CALC_TYPE_LABEL.get(decision.calc_type, decision.calc_type)
        role = "支撑" if decision.smc_side == 1 else "阻力"
        return (
            f"{label}({decision.timeframe}) {role} {_trim(decision.target_price, 5)}"
            f"（折合 {points}）"
        )
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
    本次本规则第 1 次加仓，已加 0 次，本规则加仓上限 2；当前持仓 1 笔
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
        f"本次本规则第 {decision.next_rule_add_no} 次加仓，已加 {decision.add_count} 次，"
        f"{_limit_text(decision)}；当前持仓 {decision.position_count} 笔"
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
        "limit_price": decision.limit_price or None,
        "smc_key": decision.smc_key or None,
        "smc_side": decision.smc_side or None,
        "smc_received_at": decision.smc_received_at or None,
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
        "next_rule_add_no": decision.next_rule_add_no,
        "next_position_no": decision.next_position_no,
        "limit_kind": decision.limit_kind,
        "limit_value": decision.limit_value,
    }


def decision_comment(decision: AddDecision) -> str:
    """加仓单写进 MT5 的紧凑备注：R=规则类型，L=分批档位，末段=计算方式+偏离点数。

    MT5 备注只有约 31 字符，放不下完整说明，这里只保留能反查到决策的最小信息：
    末段首字母即间距的来历（D 点数 / P 指定价 / A ATR / W 波幅 / F O Z B 为 SMC）。
    SMC 末段接 8 位哈希而不是偏离点数，重连后才能认出这条价位已经用过。
    """
    parts = [f"R{decision.rule_type}"]
    if decision.level_index is not None:
        parts.append(f"L{decision.level_index}")
    code = _CALC_COMMENT_CODE.get(decision.calc_type, "D")
    if decision.calc_type in CALC_SMC_TYPES and decision.smc_key:
        parts.append(f"{code}{decision.smc_key}")
    else:
        parts.append(f"{code}{int(round(decision.deviation))}")
    return "".join(parts)[:MT5_COMMENT_LIMIT]


def counter_order_key(decision: AddDecision) -> tuple:
    """逆势挂单的身份：手数、档位、计算方式、SMC 哈希。价格和止损不在里面。"""
    level = -1 if decision.level_index is None else int(decision.level_index)
    smc = decision.smc_key if decision.calc_type in CALC_SMC_TYPES else ""
    return (round(float(decision.volume), 2), level, decision.calc_type, smc or "")


def counter_pending_key(comment: object, volume: object) -> Optional[tuple]:
    """从在途挂单备注还原身份；不是逆势加仓单时返回 None。"""
    matched = _COUNTER_PENDING_RE.match(str(comment or "").strip())
    if matched is None:
        return None
    level = int(matched.group(1)) if matched.group(1) is not None else -1
    calc = _COMMENT_CODE_CALC.get(matched.group(2) or "", "")
    smc = matched.group(3) or ""
    if calc not in CALC_SMC_TYPES:
        smc = ""
    return (round(_as_float(volume), 2), level, calc, smc)


def describe_counter_limit(decision: AddDecision, *, verb: str) -> str:
    """逆势限价的挂出 / 改价 / 撤下 / 改市价说明。verb 用「挂出限价」这类短语。"""
    where = f"规则#{decision.rule_index}"
    if decision.level_index is not None:
        where += f" · 分批档位#{decision.level_index}"
    return (
        f"逆势加仓 · {where}：{verb} {decision.action}"
        f" 挂单价 {_trim(decision.limit_price, 5)}，"
        f"基准价 {_trim(decision.base_price, 5)}，{gap_text(decision)}；"
        f"手数 = 首单 {_trim(decision.base_volume, 2)} × 倍数 {_trim(decision.lot_times, 2)}"
        f" + 追加 {_trim(decision.extra_lot, 2)} = {_trim(decision.volume, 2)} 手；"
        f"本次本规则第 {decision.next_rule_add_no} 次加仓，已加 {decision.add_count} 次，"
        f"{_limit_text(decision)}"
    )
