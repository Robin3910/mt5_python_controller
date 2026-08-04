"""策略模版定义（内置「策略模版1」「策略模版2」）。

模版本身不可通过 API 新建；新建策略时选择模版，会把模版默认规则复制到策略实例，
之后可按实例独立调整（不影响模版）。

结构说明：
- BatchLevel：分批加仓档位（持仓笔数区间内的点数 / 倍数）
- CounterTrendRule：逆势加仓（独立模型，模版1）
- TrendFollowRule：顺势加仓（独立模型，模版1）
- RiskSizedTrendRule：以损定量趋势单（独立模型，模版2）
- TemplateRuleSet / RiskSizedRuleSet：各模版的规则集

对外序列化仍为 rules 列表（带 type），兼容现有 API / 落库格式。各 type 的字段集合
不同，规范化按 type 分派到对应的 normalizer（见 _RULE_NORMALIZERS），因此新增模版
不会改变既有 type 的落库结构。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Protocol

# 规则 type
RULE_TYPE_COUNTER = 1      # 逆势加仓（模版1）
RULE_TYPE_TREND = 2        # 顺势加仓（模版1）
RULE_TYPE_RISK_SIZED = 3   # 以损定量趋势单（模版2）

RULE_ACTIONS = ("all", "buy", "sell")

# 分批档位的加仓间距计算方式：
# point 点数固定间距；price 指定绝对价位到价触发；
# atr 用 ATR 的平均波动作间距；range 用已收盘 K 线的最大高低波幅作间距。
BATCH_CALC_POINT = "point"
BATCH_CALC_PRICE = "price"
BATCH_CALC_ATR = "atr"
BATCH_CALC_RANGE = "range"
BATCH_CALC_TYPES = (BATCH_CALC_POINT, BATCH_CALC_PRICE, BATCH_CALC_ATR, BATCH_CALC_RANGE)
# 需要读 K 线才能算出间距的方式
BATCH_CALC_BAR_TYPES = (BATCH_CALC_ATR, BATCH_CALC_RANGE)

# ATR / 波幅可选的 K 线周期（不提供「当前图表周期」：节点是独立进程，没有图表上下文）
BATCH_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN")
DEFAULT_BATCH_TIMEFRAME = "M5"
# ATR / 波幅统计的已收盘 K 线根数，固定不可配
BATCH_BAR_PERIOD = 14

# --- 模版2：以损定量趋势单 ---
# 补仓方向：pullback 价格朝不利方向回撤时补齐剩余仓位（摊低成本）；
# breakout 价格朝有利方向突破时补齐剩余仓位（追势）。
ENTRY_PULLBACK = "pullback"
ENTRY_BREAKOUT = "breakout"
ENTRY_DIRECTIONS = (ENTRY_PULLBACK, ENTRY_BREAKOUT)

TEMPLATE_1_ID = "tpl_1"
TEMPLATE_1_NAME = "策略模版1"
TEMPLATE_2_ID = "tpl_2"
TEMPLATE_2_NAME = "策略模版2"


# ---------------------------------------------------------------------------
# 取值工具
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 结构模型
# ---------------------------------------------------------------------------

class RuleSet(Protocol):
    """模版规则集：内部按独立模型存放，对外统一序列化为 rules 列表。"""

    def to_rules(self) -> list[dict[str, Any]]: ...


@dataclass
class BatchLevel:
    """分批加仓档位：在持仓笔数 [pos_from, pos_to] 内使用本组参数。

    加仓间距由 calc_type 决定用哪个参数：point 用点数，price 用指定价位，
    atr / range 用 timeframe 周期上的 K 线统计值（间距由行情实时算出）。
    """
    pos_from: int = 2
    pos_to: int = 4
    calc_type: str = BATCH_CALC_POINT
    point: float = 100.0            # calc_type=point：触发点数
    price: float = 0.0              # calc_type=price：指定的绝对价位
    timeframe: str = DEFAULT_BATCH_TIMEFRAME  # calc_type=atr / range：K 线周期
    lot_times: float = 1.1
    extra_lot: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CounterTrendRule:
    """逆势加仓规则（独立模型，对齐 MTcommander「逆势」）。"""
    status: int = 1
    action: str = "all"
    point: float = 100.0
    lot_times: float = 1.1
    extra_lot: float = 0.0
    max_allow_num: int = 3
    # 分批加仓（对齐「逆势分批加」）
    batch_enabled: bool = True
    batch_action: str = "all"  # 分批独立监控方向
    batch_count: int = 3
    total_lot_limit: float = 10.0
    batch_levels: list[BatchLevel] = field(default_factory=list)

    @property
    def type(self) -> int:
        return RULE_TYPE_COUNTER

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "point": self.point,
            "lot_times": self.lot_times,
            "extra_lot": self.extra_lot,
            "max_allow_num": self.max_allow_num,
            "batch_enabled": self.batch_enabled,
            "batch_action": self.batch_action,
            "batch_count": self.batch_count,
            "total_lot_limit": self.total_lot_limit,
            "batch_levels": [lv.to_dict() for lv in self.batch_levels],
        }


@dataclass
class TrendFollowRule:
    """顺势加仓规则（独立模型，对齐 MTcommander「顺势」）。"""
    status: int = 1
    action: str = "all"
    point: float = 100.0
    lot_times: float = 0.8
    extra_lot: float = 0.0
    max_allow_num: int = 10
    # 分批加仓（结构与逆势对齐，截图未启用则默认关闭）
    batch_enabled: bool = False
    batch_action: str = "all"
    batch_count: int = 0
    total_lot_limit: float = 0.0
    batch_levels: list[BatchLevel] = field(default_factory=list)

    @property
    def type(self) -> int:
        return RULE_TYPE_TREND

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "point": self.point,
            "lot_times": self.lot_times,
            "extra_lot": self.extra_lot,
            "max_allow_num": self.max_allow_num,
            "batch_enabled": self.batch_enabled,
            "batch_action": self.batch_action,
            "batch_count": self.batch_count,
            "total_lot_limit": self.total_lot_limit,
            "batch_levels": [lv.to_dict() for lv in self.batch_levels],
        }


@dataclass
class RiskSizedTrendRule:
    """以损定量趋势单（独立模型，对齐 MTcommander「以损定量趋势单」）。

    手数不由配置直接给出，而是用风险金额和信号止损价反推：

        总手数 = risk_amount / (止损距离 × 每手每点价值)

    底仓按 base_ratio 立即市价成交，剩余仓位分 add_batches 批、每批相隔
    batch_gap_points 点补齐。所有批次共用信号那一个止损价，所以无论补进几批，
    整笔交易打到止损的亏损始终等于 risk_amount——这是「以损定量」的关键。

    止盈按盈亏比给出：止盈距离 = 止损距离 × rr_ratio。
    breakeven_enabled 开启后，浮盈达到「止损距离 × breakeven_times」时把止损
    移到持仓的加权均价（保本）。
    """
    status: int = 1
    action: str = "all"                     # 监控方向 all|buy|sell
    risk_amount: float = 300.0              # 风险金额（账户货币）
    rr_ratio: float = 2.5                   # 盈亏比
    base_ratio: float = 30.0                # 底仓占总手数的百分比
    add_batches: int = 2                    # 剩余仓位的补仓批数，0=底仓即全仓
    entry_direction: str = ENTRY_PULLBACK   # 补仓方向
    batch_gap_points: float = 100.0         # 相邻批次的触发间距（点）
    max_total_lot: float = 0.0              # 总手数上限，0=只受单笔上限约束
    breakeven_enabled: bool = False          # 保本触发
    breakeven_times: float = 1.0            # 浮盈达到止损距离 × N 倍时移动止损到保本

    @property
    def type(self) -> int:
        return RULE_TYPE_RISK_SIZED

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "risk_amount": self.risk_amount,
            "rr_ratio": self.rr_ratio,
            "base_ratio": self.base_ratio,
            "add_batches": self.add_batches,
            "entry_direction": self.entry_direction,
            "batch_gap_points": self.batch_gap_points,
            "max_total_lot": self.max_total_lot,
            "breakeven_enabled": self.breakeven_enabled,
            "breakeven_times": self.breakeven_times,
        }


@dataclass
class TemplateRuleSet:
    """模版1 规则集：逆势 / 顺势独立存放。"""
    counter: CounterTrendRule
    trend: TrendFollowRule

    def to_rules(self) -> list[dict[str, Any]]:
        """序列化为 API / 落库用的 rules 列表。"""
        return [self.counter.to_dict(), self.trend.to_dict()]


@dataclass
class RiskSizedRuleSet:
    """模版2 规则集：只有一条以损定量趋势单规则。"""
    risk_sized: RiskSizedTrendRule

    def to_rules(self) -> list[dict[str, Any]]:
        return [self.risk_sized.to_dict()]


def _default_counter_batch_levels() -> list[BatchLevel]:
    """MTcommander 风格默认分批档位。"""
    return [
        BatchLevel(pos_from=2, pos_to=4, point=100.0, lot_times=1.1, extra_lot=0.0),
        BatchLevel(pos_from=5, pos_to=7, point=200.0, lot_times=1.2, extra_lot=0.0),
        BatchLevel(pos_from=8, pos_to=10, point=300.0, lot_times=1.3, extra_lot=0.0),
    ]


def default_counter_rule() -> CounterTrendRule:
    """策略模版1 · 逆势默认参数（对齐 MTcommander 截图）。"""
    return CounterTrendRule(
        status=1,
        action="all",
        point=100.0,
        lot_times=1.1,
        extra_lot=0.0,
        max_allow_num=3,
        batch_enabled=True,
        batch_action="all",
        batch_count=3,
        total_lot_limit=10.0,
        batch_levels=_default_counter_batch_levels(),
    )


def default_trend_rule() -> TrendFollowRule:
    """策略模版1 · 顺势默认参数（对齐 MTcommander 截图）。"""
    return TrendFollowRule(
        status=1,
        action="all",
        point=100.0,
        lot_times=0.8,
        extra_lot=0.0,
        max_allow_num=10,
        batch_enabled=False,
        batch_action="all",
        batch_count=0,
        total_lot_limit=0.0,
        batch_levels=[],
    )


def default_template_1_rules() -> TemplateRuleSet:
    return TemplateRuleSet(
        counter=default_counter_rule(),
        trend=default_trend_rule(),
    )


def default_risk_sized_rule() -> RiskSizedTrendRule:
    """策略模版2 · 以损定量趋势单默认参数（对齐 MTcommander 截图）。"""
    return RiskSizedTrendRule(
        status=1,
        action="all",
        risk_amount=300.0,
        rr_ratio=2.5,
        base_ratio=30.0,
        add_batches=2,
        entry_direction=ENTRY_PULLBACK,
        batch_gap_points=100.0,
        max_total_lot=0.0,
        breakeven_enabled=False,
        breakeven_times=1.0,
    )


def default_template_2_rules() -> RiskSizedRuleSet:
    return RiskSizedRuleSet(risk_sized=default_risk_sized_rule())


# ---------------------------------------------------------------------------
# 模版注册表
# ---------------------------------------------------------------------------

_TPL1_RULES = default_template_1_rules()
_TPL2_RULES = default_template_2_rules()

STRATEGY_TEMPLATES: dict[str, dict] = {
    TEMPLATE_1_ID: {
        "template_id": TEMPLATE_1_ID,
        "name": TEMPLATE_1_NAME,
        "description": (
            "含逆势加仓与顺势加仓两条独立规则："
            "逆势支持分批加仓档位（点数 / 倍数随持仓加深）；"
            "实际手数 = lot_times × 基础手数 + extra_lot。"
        ),
        # 模版内部按独立模型存放；对外仍暴露 rules 列表
        "rule_set": _TPL1_RULES,
        "rules": _TPL1_RULES.to_rules(),
    },
    TEMPLATE_2_ID: {
        "template_id": TEMPLATE_2_ID,
        "name": TEMPLATE_2_NAME,
        "description": (
            "以损定量趋势单：按风险金额与信号止损价反推总手数（不使用信号手数），"
            "底仓先市价成交，剩余仓位按间距分批补齐，全部批次共用信号止损价，"
            "止盈距离 = 止损距离 × 盈亏比；可选浮盈达标后自动移动止损保本。"
            "信号必须携带止损价，否则该策略不参与分发。"
        ),
        "rule_set": _TPL2_RULES,
        "rules": _TPL2_RULES.to_rules(),
    },
}


def is_risk_sized_rule(rule: object) -> bool:
    """该条规则是否为以损定量趋势单。"""
    if not isinstance(rule, dict):
        return False
    return _as_int(rule.get("type"), RULE_TYPE_COUNTER) == RULE_TYPE_RISK_SIZED


def pick_risk_sized_rule(rules: object) -> Optional[dict]:
    """从规则列表里取出启用中的以损定量趋势单规则；没有则返回 None。

    模版2 只有一条规则，服务端与节点都靠它判断该走以损定量的执行路径。
    """
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if is_risk_sized_rule(rule) and _as_int(rule.get("status"), 0):
            return rule
    return None


def list_templates() -> list[dict]:
    """返回所有可用策略模版（含默认规则副本）。"""
    out: list[dict] = []
    for t in STRATEGY_TEMPLATES.values():
        item = {
            "template_id": t["template_id"],
            "name": t["name"],
            "description": t.get("description", ""),
            "rules": deepcopy(t["rules"]),
        }
        out.append(item)
    return out


def get_template(template_id: str) -> Optional[dict]:
    t = STRATEGY_TEMPLATES.get(template_id)
    if not t:
        return None
    return {
        "template_id": t["template_id"],
        "name": t["name"],
        "description": t.get("description", ""),
        "rules": deepcopy(t["rules"]),
    }


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def normalize_timeframe(value: object) -> str:
    """规范化 K 线周期；非法值回落到默认周期。"""
    tf = str(value or "").strip().upper()
    return tf if tf in BATCH_TIMEFRAMES else DEFAULT_BATCH_TIMEFRAME


def normalize_batch_level(raw: dict) -> dict[str, Any]:
    calc_type = str(raw.get("calc_type") or BATCH_CALC_POINT).strip().lower()
    if calc_type not in BATCH_CALC_TYPES:
        calc_type = BATCH_CALC_POINT
    pos_from = max(0, _as_int(raw.get("pos_from", 1), 1))
    pos_to = max(pos_from, _as_int(raw.get("pos_to", pos_from), pos_from))
    return {
        "pos_from": pos_from,
        "pos_to": pos_to,
        "calc_type": calc_type,
        "point": max(0.0, _as_float(raw.get("point", 100), 100.0)),
        # 指定价位是绝对价格，允许为 0（表示未设置，节点侧据此跳过该档）
        "price": max(0.0, _as_float(raw.get("price", 0.0), 0.0)),
        "timeframe": normalize_timeframe(raw.get("timeframe")),
        "lot_times": max(0.0, _as_float(raw.get("lot_times", 1.0), 1.0)),
        "extra_lot": max(0.0, _as_float(raw.get("extra_lot", 0.0), 0.0)),
    }


def _normalize_batch_levels(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [normalize_batch_level(x) for x in raw if isinstance(x, dict)]


def _normalize_status(raw: dict, default: object) -> int:
    try:
        return 1 if int(raw.get("status", default)) else 0
    except (TypeError, ValueError):
        return int(default)  # type: ignore[arg-type]


def _normalize_action(value: object, default: str) -> str:
    action = str(value or default).strip().lower()
    return action if action in RULE_ACTIONS else default


def _normalize_add_on_rule(rule_type: int, raw: dict) -> dict[str, Any]:
    """规范化加仓类规则（逆势 / 顺势，模版1）。"""
    defaults = (
        default_counter_rule().to_dict()
        if rule_type == RULE_TYPE_COUNTER
        else default_trend_rule().to_dict()
    )
    action = _normalize_action(raw.get("action"), defaults["action"])
    return {
        "type": rule_type,
        "status": _normalize_status(raw, defaults["status"]),
        "action": action,
        "point": max(0.0, _as_float(raw.get("point", defaults["point"]), defaults["point"])),
        "lot_times": max(0.0, _as_float(raw.get("lot_times", defaults["lot_times"]), defaults["lot_times"])),
        "extra_lot": max(0.0, _as_float(raw.get("extra_lot", defaults["extra_lot"]), defaults["extra_lot"])),
        "max_allow_num": max(0, _as_int(raw.get("max_allow_num", defaults["max_allow_num"]), defaults["max_allow_num"])),
        "batch_enabled": bool(raw.get("batch_enabled", defaults["batch_enabled"])),
        "batch_action": _normalize_action(
            raw.get("batch_action") or defaults.get("batch_action") or action, "all",
        ),
        "batch_count": max(0, _as_int(raw.get("batch_count", defaults["batch_count"]), defaults["batch_count"])),
        "total_lot_limit": max(
            0.0,
            _as_float(raw.get("total_lot_limit", defaults["total_lot_limit"]), defaults["total_lot_limit"]),
        ),
        "batch_levels": _normalize_batch_levels(raw.get("batch_levels", defaults["batch_levels"])),
    }


def _normalize_risk_sized_rule(rule_type: int, raw: dict) -> dict[str, Any]:
    """规范化以损定量趋势单（模版2）。

    底仓比例夹在 (0, 100] 内：0 会导致底仓无手数、首单无从下手；不分批时底仓
    必须是全仓，否则剩下的仓位永远补不进来、实际风险小于设定的风险金额。
    """
    defaults = default_risk_sized_rule().to_dict()

    entry_direction = str(raw.get("entry_direction") or defaults["entry_direction"]).strip().lower()
    if entry_direction not in ENTRY_DIRECTIONS:
        entry_direction = defaults["entry_direction"]

    add_batches = max(0, _as_int(raw.get("add_batches", defaults["add_batches"]), defaults["add_batches"]))
    base_ratio = _as_float(raw.get("base_ratio", defaults["base_ratio"]), defaults["base_ratio"])
    base_ratio = min(100.0, max(0.0, base_ratio)) or defaults["base_ratio"]
    if not add_batches:
        base_ratio = 100.0

    return {
        "type": rule_type,
        "status": _normalize_status(raw, defaults["status"]),
        "action": _normalize_action(raw.get("action"), defaults["action"]),
        "risk_amount": max(0.0, _as_float(raw.get("risk_amount", defaults["risk_amount"]), defaults["risk_amount"])),
        "rr_ratio": max(0.0, _as_float(raw.get("rr_ratio", defaults["rr_ratio"]), defaults["rr_ratio"])),
        "base_ratio": base_ratio,
        "add_batches": add_batches,
        "entry_direction": entry_direction,
        "batch_gap_points": max(
            0.0,
            _as_float(raw.get("batch_gap_points", defaults["batch_gap_points"]), defaults["batch_gap_points"]),
        ),
        "max_total_lot": max(
            0.0, _as_float(raw.get("max_total_lot", defaults["max_total_lot"]), defaults["max_total_lot"]),
        ),
        "breakeven_enabled": bool(raw.get("breakeven_enabled", defaults["breakeven_enabled"])),
        "breakeven_times": max(
            0.0, _as_float(raw.get("breakeven_times", defaults["breakeven_times"]), defaults["breakeven_times"]),
        ),
    }


# 各 type 的规范化实现。新增模版时在此登记，normalize_rule 无需再改。
_RULE_NORMALIZERS: dict[int, Callable[[int, dict], dict[str, Any]]] = {
    RULE_TYPE_COUNTER: _normalize_add_on_rule,
    RULE_TYPE_TREND: _normalize_add_on_rule,
    RULE_TYPE_RISK_SIZED: _normalize_risk_sized_rule,
}


def normalize_rule(raw: dict) -> dict[str, Any]:
    """规范化单条规则；非法字段回落默认值。

    按 type 分派到对应实现，各 type 只输出自己的字段——模版之间字段集合不同，
    统一成一个大字典会把无关默认值写进库里，反过来又会被前端当成有效配置。
    未知 type 一律按逆势加仓处理，保持历史行为。
    """
    rule_type = _as_int(raw.get("type", RULE_TYPE_COUNTER), RULE_TYPE_COUNTER)
    normalizer = _RULE_NORMALIZERS.get(rule_type)
    if normalizer is None:
        rule_type, normalizer = RULE_TYPE_COUNTER, _normalize_add_on_rule
    return normalizer(rule_type, raw)


def normalize_rules(rules: object) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        return []
    return [normalize_rule(r) for r in rules if isinstance(r, dict)]


def rules_to_rule_set(rules: object) -> TemplateRuleSet:
    """从 rules 列表还原模版1 的逆势 / 顺势独立模型。缺失的一侧用默认填充。

    其它 type（如模版2 的以损定量趋势单）不属于加仓类规则，直接忽略。
    """
    counter = default_counter_rule()
    trend = default_trend_rule()
    if isinstance(rules, list):
        for raw in rules:
            if not isinstance(raw, dict):
                continue
            normalized = normalize_rule(raw)
            if normalized["type"] not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
                continue
            levels = [
                BatchLevel(
                    pos_from=lv["pos_from"],
                    pos_to=lv["pos_to"],
                    calc_type=lv["calc_type"],
                    point=lv["point"],
                    lot_times=lv["lot_times"],
                    extra_lot=lv["extra_lot"],
                )
                for lv in normalized["batch_levels"]
            ]
            if normalized["type"] == RULE_TYPE_COUNTER:
                counter = CounterTrendRule(
                    status=normalized["status"],
                    action=normalized["action"],
                    point=normalized["point"],
                    lot_times=normalized["lot_times"],
                    extra_lot=normalized["extra_lot"],
                    max_allow_num=normalized["max_allow_num"],
                    batch_enabled=normalized["batch_enabled"],
                    batch_action=normalized["batch_action"],
                    batch_count=normalized["batch_count"],
                    total_lot_limit=normalized["total_lot_limit"],
                    batch_levels=levels,
                )
            elif normalized["type"] == RULE_TYPE_TREND:
                trend = TrendFollowRule(
                    status=normalized["status"],
                    action=normalized["action"],
                    point=normalized["point"],
                    lot_times=normalized["lot_times"],
                    extra_lot=normalized["extra_lot"],
                    max_allow_num=normalized["max_allow_num"],
                    batch_enabled=normalized["batch_enabled"],
                    batch_action=normalized["batch_action"],
                    batch_count=normalized["batch_count"],
                    total_lot_limit=normalized["total_lot_limit"],
                    batch_levels=levels,
                )
    return TemplateRuleSet(counter=counter, trend=trend)
