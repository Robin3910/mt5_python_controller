"""策略模版定义（内置「策略模版1」「策略模版2」「策略模版3」）。

模版本身不可通过 API 新建；新建策略时选择模版，会把模版默认规则复制到策略实例，
之后可按实例独立调整（不影响模版）。

结构说明：
- BatchLevel：分批加仓档位（持仓笔数区间内的点数 / 倍数）
- CounterTrendRule：逆势加仓（独立模型，模版1）
- TrendFollowRule：顺势加仓（独立模型，模版1）
- RiskSizedTrendRule：以损定量趋势单（独立模型，模版2）
- GridTradingRule：网格交易（独立模型，模版3）
- TemplateRuleSet / RiskSizedRuleSet / GridRuleSet：各模版的规则集

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
RULE_TYPE_GRID = 4         # 网格交易（模版3）

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

# --- 模版1：信号级盈亏控制（按魔术号独立监控，只平该信号仓）---
# 形态对齐节点账户风控，但不写入 remaining_times（次数是任务内存态）。
SIGNAL_PL_CLOSE_ALL = "close_all"
SIGNAL_PL_MONITOR_LOOP = "loop"
SIGNAL_PL_MONITOR_TIMES = "times"
SIGNAL_PL_MONITOR_MODES = (SIGNAL_PL_MONITOR_LOOP, SIGNAL_PL_MONITOR_TIMES)
SIGNAL_PL_CLOSE_SIDES = ("all", "buy", "sell")
SIGNAL_PL_MAX_TIERS = 10

# --- 模版2：以损定量趋势单 ---
# 分散仓单数硬上限（与节点 risk_sizing.DISTRIBUTE_COUNT_MAX 对齐）
DISTRIBUTE_COUNT_MAX = 50
# 保本监控：once=按次（触发一次后停止）/ loop=循环（达标且止损未到位时可反复移动）
BREAKEVEN_ONCE = "once"
BREAKEVEN_LOOP = "loop"
BREAKEVEN_MODES = (BREAKEVEN_ONCE, BREAKEVEN_LOOP)
# 开仓方式：market=信号到达即市价打齐 / limit=在信号给的入场价挂限价单等成交
ENTRY_MODE_MARKET = "market"
ENTRY_MODE_LIMIT = "limit"
ENTRY_MODES = (ENTRY_MODE_MARKET, ENTRY_MODE_LIMIT)

# --- 模版3：网格交易 ---
GRID_MODE_ARITHMETIC = "arithmetic"   # 等差
GRID_MODE_GEOMETRIC = "geometric"     # 等比
GRID_MODES = (GRID_MODE_ARITHMETIC, GRID_MODE_GEOMETRIC)
# 网格方向：long 只做多 / short 只做空
GRID_SIDE_LONG = "long"
GRID_SIDE_SHORT = "short"
# 历史值 follow（跟随信号）已废弃；规范化时回落到默认 long
GRID_SIDE_FOLLOW = "follow"
GRID_SIDES = (GRID_SIDE_LONG, GRID_SIDE_SHORT)
GRID_COUNT_MIN = 2
GRID_COUNT_MAX = 200
# 向上追踪的平移次数上限；0 表示不限，此处只防止配置写出天文数字
GRID_TRAILING_MAX = 10000
# 试算助手：只在配置期把「ATR × 倍数」折成格距、再按最大亏损反推每格手数，
# 结果落到 grid_count / lot_per_grid 两个既有字段，网格执行不读这组参数。
# 周期复用分批档位那套（BATCH_TIMEFRAMES），默认比加仓档位大一档：网格格距要反映的
# 是区间级别的波动结构。
GRID_ASSIST_TIMEFRAME = "H1"
GRID_ASSIST_MULT_MIN = 0.05
GRID_ASSIST_MULT_MAX = 50.0

TEMPLATE_1_ID = "tpl_1"
TEMPLATE_1_NAME = "AI智能加仓策略"
TEMPLATE_2_ID = "tpl_2"
TEMPLATE_2_NAME = "趋势策略"
TEMPLATE_3_ID = "tpl_3"
TEMPLATE_3_NAME = "网格策略"


# 各模版允许的规则 type；创建/更新策略时据此拒收错配与空启用集
TEMPLATE_RULE_TYPES: dict[str, frozenset[int]] = {
    TEMPLATE_1_ID: frozenset({RULE_TYPE_COUNTER, RULE_TYPE_TREND}),
    TEMPLATE_2_ID: frozenset({RULE_TYPE_RISK_SIZED}),
    TEMPLATE_3_ID: frozenset({RULE_TYPE_GRID}),
}


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


def default_signal_float_pl_ratio() -> dict[str, Any]:
    """信号盈亏比默认关闭；阈值口径与账户风控一致，默认 -20%。"""
    return {
        "enabled": False,
        "ratio": -20.0,
        "action": SIGNAL_PL_CLOSE_ALL,
        "monitor_mode": SIGNAL_PL_MONITOR_LOOP,
        "max_times": 1,
    }


def default_signal_lot_pl_tiers() -> dict[str, Any]:
    """信号分档手数盈亏默认关闭。"""
    return {
        "enabled": False,
        "batch_count": 2,
        "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 50.0},
            {"min_lot": 0.5, "pl_amount": 100.0},
        ],
    }


def default_manual_scatter() -> dict[str, Any]:
    """手动分散仓默认关闭；顺势 / 逆势各至多一条。"""
    return {
        "enabled": False,
        "entry_price": 0.0,
        "take_profit": 0.0,
        "stop_loss": 0.0,
        "volume": 0.0,
        "volume_locked": False,
    }


def _normalize_signal_float_pl_ratio(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_signal_float_pl_ratio()
    mode = str(src.get("monitor_mode") or defaults["monitor_mode"]).strip().lower()
    if mode not in SIGNAL_PL_MONITOR_MODES:
        mode = defaults["monitor_mode"]
    max_times = max(1, _as_int(src.get("max_times", defaults["max_times"]), defaults["max_times"]))
    return {
        "enabled": bool(src.get("enabled", defaults["enabled"])),
        "ratio": _as_float(src.get("ratio", defaults["ratio"]), defaults["ratio"]),
        "action": SIGNAL_PL_CLOSE_ALL,
        "monitor_mode": mode,
        "max_times": max_times,
    }


def _normalize_signal_lot_pl_tiers(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_signal_lot_pl_tiers()
    enabled = bool(src.get("enabled", defaults["enabled"]))
    batch_count = _as_int(src.get("batch_count", defaults["batch_count"]), defaults["batch_count"])
    batch_count = max(1, min(SIGNAL_PL_MAX_TIERS, batch_count))
    close_action = str(src.get("close_action") or defaults["close_action"]).strip().lower()
    if close_action not in SIGNAL_PL_CLOSE_SIDES:
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
        "enabled": enabled,
        "batch_count": batch_count,
        "close_action": close_action,
        "tiers": tiers,
    }


def _normalize_manual_scatter(raw: object) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    defaults = default_manual_scatter()
    enabled = bool(src.get("enabled", defaults["enabled"]))
    return {
        "enabled": enabled,
        "entry_price": max(
            0.0, _as_float(src.get("entry_price", defaults["entry_price"]), defaults["entry_price"]),
        ),
        "take_profit": max(
            0.0, _as_float(src.get("take_profit", defaults["take_profit"]), defaults["take_profit"]),
        ),
        "stop_loss": max(
            0.0, _as_float(src.get("stop_loss", defaults["stop_loss"]), defaults["stop_loss"]),
        ),
        "volume": max(
            0.0, _as_float(src.get("volume", defaults["volume"]), defaults["volume"]),
        ),
        "volume_locked": bool(src.get("volume_locked", defaults["volume_locked"])),
    }


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
    status: int = 0
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
    # 信号级盈亏控制（与顺势规则写入相同值；运行时按魔术号独立监控）
    float_pl_ratio: dict[str, Any] = field(default_factory=default_signal_float_pl_ratio)
    lot_pl_tiers: dict[str, Any] = field(default_factory=default_signal_lot_pl_tiers)
    manual_scatter: dict[str, Any] = field(default_factory=default_manual_scatter)

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
            "float_pl_ratio": deepcopy(self.float_pl_ratio),
            "lot_pl_tiers": deepcopy(self.lot_pl_tiers),
            "manual_scatter": deepcopy(self.manual_scatter),
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
    float_pl_ratio: dict[str, Any] = field(default_factory=default_signal_float_pl_ratio)
    lot_pl_tiers: dict[str, Any] = field(default_factory=default_signal_lot_pl_tiers)
    manual_scatter: dict[str, Any] = field(default_factory=default_manual_scatter)

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
            "float_pl_ratio": deepcopy(self.float_pl_ratio),
            "lot_pl_tiers": deepcopy(self.lot_pl_tiers),
            "manual_scatter": deepcopy(self.manual_scatter),
        }


@dataclass
class RiskSizedTrendRule:
    """以损定量趋势单（独立模型，对齐 MTcommander「以损定量趋势单」）。

    手数不由配置直接给出，而是用风险金额和信号止损价反推：

        总手数 = risk_amount / (止损距离 × 每手每点价值)

    底仓按 base_ratio 成交（止盈为 0）；剩余仓位拆成 add_batches 笔「分散仓」，
    按盈亏比挂止盈。所有订单共用信号那一个止损价，所以打到止损的总亏损始终等于
    risk_amount——这是「以损定量」的关键。

    entry_mode 决定这批单怎么进场：
    - market：信号一到全部市价打齐，各单开仓价相同，止损距离是单值；
    - limit：底仓与分散仓全部挂在信号给的入场价（同一点位），止损距离与手数
      口径与市价相同；挂单一直等到成交（GTC）。

    止盈距离 = 止损距离 × rr_ratio（只挂在分散仓上）。
    breakeven_enabled 开启后，浮盈达到「止损距离 × breakeven_times」时把止损
    移到持仓的加权均价（保本）。breakeven_mode=once 只触发一次；loop 在止损
    尚未到位时可持续监控并再次移动。
    """
    status: int = 1
    action: str = "all"                     # 监控方向 all|buy|sell
    risk_amount: float = 100.0              # 风险金额（账户货币）
    rr_ratio: float = 2.5                   # 盈亏比
    base_ratio: float = 30.0                # 底仓占总手数的百分比
    add_batches: int = 10                   # 分散仓单数，0=底仓即全仓
    max_total_lot: float = 0.0              # 总手数上限，0=只受单笔上限约束
    breakeven_enabled: bool = True          # 保本触发
    breakeven_times: float = 2.0            # 浮盈达到止损距离 × N 倍时移动止损到保本
    breakeven_mode: str = BREAKEVEN_ONCE    # once=按次 / loop=循环
    entry_mode: str = ENTRY_MODE_MARKET     # market=市价打齐 / limit=同价挂限价

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
            "max_total_lot": self.max_total_lot,
            "breakeven_enabled": self.breakeven_enabled,
            "breakeven_times": self.breakeven_times,
            "breakeven_mode": self.breakeven_mode,
            "entry_mode": self.entry_mode,
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


@dataclass
class GridTradingRule:
    """网格交易规则（独立模型）。

    在 [price_lower, price_upper] 区间按 grid_mode 切成 grid_count 格（grid_count+1
    条网格线）。价格下跌穿越网格线时买入一格、上涨穿越时卖出对应格，反复吃差价。
    空仓是正常运行态（价格涨出区间顶部时全部卖光，等回落再买），因此任务不会因
    持仓归零而收口，只由止损 / 止盈 / strategy_stop 结束。

    lot_per_grid 是每格手数（MT5 原生口径）。
    prefill_enabled 开启时，启动会先市价买入「现价上方格位数 × 每格手数」的底仓，
    否则价格上涨时无货可卖、网格上半部分失效。

    trailing_up 开启时，价格突破区间外沿时不停机，整个网格连同止损止盈一起平移
    一格，继续在新区间运行。多头网格追涨（突破上限上移），空头网格追跌（跌破下限
    下移）。

    assist_* 是配置期的试算参数（默认关闭）：把 ATR × 倍数折成格距推出格数，再按
    最大可接受亏损反推每格手数。它们只是「这组格数与手数是怎么算出来的」的留痕，
    执行层（node_client/grid_trading）不读，因此改动它们不会影响已在跑的网格。
    """
    status: int = 1
    action: str = "all"                         # 保留字段，与其它规则对齐；实际方向看 grid_side
    price_lower: float = 0.0                    # 区间下限
    price_upper: float = 0.0                    # 区间上限
    grid_count: int = 10                        # 网格数量（2-200）
    grid_mode: str = GRID_MODE_ARITHMETIC       # arithmetic / geometric
    grid_side: str = GRID_SIDE_LONG             # long / short
    lot_per_grid: float = 0.01                  # 每格手数
    total_lot_limit: float = 0.0                # 总手数上限，0=不额外限制
    trigger_price: float = 0.0                  # 触发价，0=立即启动
    stop_lower: float = 0.0                     # 下沿终止价（多头止损 / 空头止盈），0=不设
    stop_upper: float = 0.0                     # 上沿终止价（多头止盈 / 空头止损），0=不设
    close_on_stop: bool = True                  # 终止时是否清仓
    prefill_enabled: bool = True                # 是否按现价上方格位初始建仓
    trailing_up: bool = False                   # 向上追踪：突破区间外沿时平移网格
    trailing_max: int = 0                       # 最大平移格数，0=不限
    # --- 试算助手（仅配置期，执行层不读）---
    assist_enabled: bool = False                # 是否展开试算，默认关闭
    assist_timeframe: str = GRID_ASSIST_TIMEFRAME   # 算 ATR 用的 K 线周期
    assist_atr_mult: float = 1.0                # 格距 = ATR × 该倍数
    assist_spacing: float = 0.0                 # 手改后的格距，0=用 ATR×倍数的结果
    assist_max_loss: float = 0.0                # 最大可接受亏损（账户货币）

    @property
    def type(self) -> int:
        return RULE_TYPE_GRID

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "price_lower": self.price_lower,
            "price_upper": self.price_upper,
            "grid_count": self.grid_count,
            "grid_mode": self.grid_mode,
            "grid_side": self.grid_side,
            "lot_per_grid": self.lot_per_grid,
            "total_lot_limit": self.total_lot_limit,
            "trigger_price": self.trigger_price,
            "stop_lower": self.stop_lower,
            "stop_upper": self.stop_upper,
            "close_on_stop": self.close_on_stop,
            "prefill_enabled": self.prefill_enabled,
            "trailing_up": self.trailing_up,
            "trailing_max": self.trailing_max,
            "assist_enabled": self.assist_enabled,
            "assist_timeframe": self.assist_timeframe,
            "assist_atr_mult": self.assist_atr_mult,
            "assist_spacing": self.assist_spacing,
            "assist_max_loss": self.assist_max_loss,
        }


@dataclass
class GridRuleSet:
    """模版3 规则集：只有一条网格交易规则。"""
    grid: GridTradingRule

    def to_rules(self) -> list[dict[str, Any]]:
        return [self.grid.to_dict()]


def _default_counter_batch_levels() -> list[BatchLevel]:
    """MTcommander 风格默认分批档位。"""
    return [
        BatchLevel(pos_from=2, pos_to=4, point=100.0, lot_times=1.1, extra_lot=0.0),
        BatchLevel(pos_from=5, pos_to=7, point=200.0, lot_times=1.2, extra_lot=0.0),
        BatchLevel(pos_from=8, pos_to=10, point=300.0, lot_times=1.3, extra_lot=0.0),
    ]


def default_counter_rule() -> CounterTrendRule:
    """策略模版1 · 逆势默认参数：关闭，需手动启用后才参与加仓。"""
    return CounterTrendRule(
        status=0,
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
        risk_amount=100.0,
        rr_ratio=2.5,
        base_ratio=30.0,
        add_batches=10,
        max_total_lot=0.0,
        breakeven_enabled=True,
        breakeven_times=2.0,
        breakeven_mode=BREAKEVEN_ONCE,
        entry_mode=ENTRY_MODE_MARKET,
    )


def default_template_2_rules() -> RiskSizedRuleSet:
    return RiskSizedRuleSet(risk_sized=default_risk_sized_rule())


def default_grid_rule() -> GridTradingRule:
    """策略模版3 · 网格交易默认参数。"""
    return GridTradingRule(
        status=1,
        action="all",
        price_lower=0.0,
        price_upper=0.0,
        grid_count=10,
        grid_mode=GRID_MODE_ARITHMETIC,
        grid_side=GRID_SIDE_LONG,
        lot_per_grid=0.01,
        total_lot_limit=0.0,
        trigger_price=0.0,
        stop_lower=0.0,
        stop_upper=0.0,
        close_on_stop=True,
        prefill_enabled=True,
        trailing_up=False,
        trailing_max=0,
        assist_enabled=False,
        assist_timeframe=GRID_ASSIST_TIMEFRAME,
        assist_atr_mult=1.0,
        assist_spacing=0.0,
        assist_max_loss=0.0,
    )


def default_template_3_rules() -> GridRuleSet:
    return GridRuleSet(grid=default_grid_rule())


# ---------------------------------------------------------------------------
# 模版注册表
# ---------------------------------------------------------------------------

_TPL1_RULES = default_template_1_rules()
_TPL2_RULES = default_template_2_rules()
_TPL3_RULES = default_template_3_rules()

STRATEGY_TEMPLATES: dict[str, dict] = {
    TEMPLATE_1_ID: {
        "template_id": TEMPLATE_1_ID,
        "name": TEMPLATE_1_NAME,
        "description": (
            "含逆势加仓与顺势加仓两条独立规则："
            "逆势支持分批加仓档位（点数 / 倍数随持仓加深）；"
            "实际手数 = lot_times × 基础手数 + extra_lot。"
            "可配信号级盈亏比与分档手数盈亏：每个信号按魔术号独立监控，触发只平该信号仓。"
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
            "底仓成交（止盈为 0），剩余仓位拆成多笔分散仓并按盈亏比挂止盈，"
            "全部订单共用信号止损价；可选浮盈达标后自动移动止损保本。"
            "开仓方式可选市价打齐，或在信号给的入场价挂限价等成交（底仓与分散仓同价）。"
            "信号必须携带止损价，否则该策略不参与分发。"
        ),
        "rule_set": _TPL2_RULES,
        "rules": _TPL2_RULES.to_rules(),
    },
    TEMPLATE_3_ID: {
        "template_id": TEMPLATE_3_ID,
        "name": TEMPLATE_3_NAME,
        "description": (
            "网格交易：在价格区间内按等差或等比切格，"
            "下跌穿越网格线买入、上涨穿越卖出对应格，反复吃差价。"
            "空仓是正常运行态，任务不会因持仓归零而结束；"
            "只由止损价 / 止盈价 / 终止信号收口。"
            "可选初始建仓（按现价上方格位先买入），否则上涨时无货可卖；"
            "可选向上追踪（价格突破区间外沿时整个网格连同止损止盈平移一格）。"
        ),
        "rule_set": _TPL3_RULES,
        "rules": _TPL3_RULES.to_rules(),
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


def is_limit_entry(rule: object) -> bool:
    """该条以损定量规则是否配成了限价开仓。

    只认显式的 limit，其余（含缺字段的历史配置）一律按市价，保证旧策略行为不变。
    """
    if not isinstance(rule, dict):
        return False
    return str(rule.get("entry_mode") or "").strip().lower() == ENTRY_MODE_LIMIT


def is_grid_rule(rule: object) -> bool:
    """该条规则是否为网格交易。"""
    if not isinstance(rule, dict):
        return False
    return _as_int(rule.get("type"), RULE_TYPE_COUNTER) == RULE_TYPE_GRID


def pick_grid_rule(rules: object) -> Optional[dict]:
    """从规则列表里取出启用中的网格交易规则；没有则返回 None。

    模版3 只有一条规则，服务端与节点都靠它判断该走网格执行路径，
    以及子任务是否应在空仓时继续存活（hold_when_empty）。
    """
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if is_grid_rule(rule) and _as_int(rule.get("status"), 0):
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


def live_template_name(template_id: str | None, fallback: str | None = None) -> str:
    """当前注册表中的模版显示名；用于改名后覆盖实例上快照的旧名称。"""
    t = STRATEGY_TEMPLATES.get(str(template_id or "").strip().lower())
    if t:
        return str(t["name"])
    return fallback or template_id or ""


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
        "float_pl_ratio": _normalize_signal_float_pl_ratio(
            raw.get("float_pl_ratio", defaults.get("float_pl_ratio")),
        ),
        "lot_pl_tiers": _normalize_signal_lot_pl_tiers(
            raw.get("lot_pl_tiers", defaults.get("lot_pl_tiers")),
        ),
        "manual_scatter": _normalize_manual_scatter(raw.get("manual_scatter")),
    }


def _normalize_risk_sized_rule(rule_type: int, raw: dict) -> dict[str, Any]:
    """规范化以损定量趋势单（模版2）。

    底仓比例夹在 (0, 100] 内：0 会导致底仓无手数、首单无从下手；无分散仓时底仓
    必须是全仓，否则剩下的仓位永远开不进来、实际风险小于设定的风险金额。
    """
    defaults = default_risk_sized_rule().to_dict()

    add_batches = max(0, _as_int(raw.get("add_batches", defaults["add_batches"]), defaults["add_batches"]))
    add_batches = min(DISTRIBUTE_COUNT_MAX, add_batches)
    base_ratio = _as_float(raw.get("base_ratio", defaults["base_ratio"]), defaults["base_ratio"])
    base_ratio = min(100.0, max(0.0, base_ratio)) or defaults["base_ratio"]
    if not add_batches:
        base_ratio = 100.0

    breakeven_mode = str(raw.get("breakeven_mode") or defaults["breakeven_mode"]).strip().lower()
    if breakeven_mode not in BREAKEVEN_MODES:
        breakeven_mode = defaults["breakeven_mode"]

    entry_mode = str(raw.get("entry_mode") or defaults["entry_mode"]).strip().lower()
    if entry_mode not in ENTRY_MODES:
        entry_mode = defaults["entry_mode"]

    return {
        "type": rule_type,
        "status": _normalize_status(raw, defaults["status"]),
        "action": _normalize_action(raw.get("action"), defaults["action"]),
        "risk_amount": max(0.0, _as_float(raw.get("risk_amount", defaults["risk_amount"]), defaults["risk_amount"])),
        "rr_ratio": max(0.0, _as_float(raw.get("rr_ratio", defaults["rr_ratio"]), defaults["rr_ratio"])),
        "base_ratio": base_ratio,
        "add_batches": add_batches,
        "max_total_lot": max(
            0.0, _as_float(raw.get("max_total_lot", defaults["max_total_lot"]), defaults["max_total_lot"]),
        ),
        "breakeven_enabled": bool(raw.get("breakeven_enabled", defaults["breakeven_enabled"])),
        "breakeven_times": max(
            0.0, _as_float(raw.get("breakeven_times", defaults["breakeven_times"]), defaults["breakeven_times"]),
        ),
        "breakeven_mode": breakeven_mode,
        "entry_mode": entry_mode,
    }


def _normalize_grid_rule(rule_type: int, raw: dict) -> dict[str, Any]:
    """规范化网格交易规则（模版3）。

    区间上下限必须满足 upper > lower > 0；等比模式同样要求 lower > 0。
    stop_lower 须低于区间下限、stop_upper 须高于区间上限（几何约束，与方向无关；
    为 0 表示不设）。运行时语义：多头 stop_lower=止损 / stop_upper=止盈；空头相反。
    网格数量夹在 [2, 200]，向上追踪的平移上限夹在 [0, 10000]。

    assist_* 只是配置期试算的留痕：关闭开关时也照常夹取并保留已填的值（与
    trailing_max 在追踪关闭时的处理一致），避免用户误关开关就把参数丢了。
    """
    defaults = default_grid_rule().to_dict()

    grid_mode = str(raw.get("grid_mode") or defaults["grid_mode"]).strip().lower()
    if grid_mode not in GRID_MODES:
        grid_mode = defaults["grid_mode"]

    grid_side = str(raw.get("grid_side") or defaults["grid_side"]).strip().lower()
    if grid_side not in GRID_SIDES:
        grid_side = defaults["grid_side"]

    price_lower = max(0.0, _as_float(raw.get("price_lower", defaults["price_lower"]), defaults["price_lower"]))
    price_upper = max(0.0, _as_float(raw.get("price_upper", defaults["price_upper"]), defaults["price_upper"]))
    # 上下限颠倒时交换，保证 upper >= lower；相等或未配留给准入校验拦下
    if price_upper and price_lower and price_upper < price_lower:
        price_lower, price_upper = price_upper, price_lower

    grid_count = _as_int(raw.get("grid_count", defaults["grid_count"]), defaults["grid_count"])
    grid_count = min(GRID_COUNT_MAX, max(GRID_COUNT_MIN, grid_count))

    stop_lower = max(0.0, _as_float(raw.get("stop_lower", defaults["stop_lower"]), defaults["stop_lower"]))
    stop_upper = max(0.0, _as_float(raw.get("stop_upper", defaults["stop_upper"]), defaults["stop_upper"]))
    # 几何约束：下沿价须低于区间下限，上沿价须高于区间上限；否则清零视为未设
    if stop_lower > 0 and price_lower > 0 and stop_lower >= price_lower:
        stop_lower = 0.0
    if stop_upper > 0 and price_upper > 0 and stop_upper <= price_upper:
        stop_upper = 0.0

    assist_timeframe = str(
        raw.get("assist_timeframe") or defaults["assist_timeframe"],
    ).strip().upper()
    if assist_timeframe not in BATCH_TIMEFRAMES:
        assist_timeframe = defaults["assist_timeframe"]
    assist_atr_mult = _as_float(
        raw.get("assist_atr_mult", defaults["assist_atr_mult"]), defaults["assist_atr_mult"],
    )
    assist_atr_mult = (
        min(GRID_ASSIST_MULT_MAX, max(GRID_ASSIST_MULT_MIN, assist_atr_mult))
        if assist_atr_mult > 0
        else defaults["assist_atr_mult"]
    )

    return {
        "type": rule_type,
        "status": _normalize_status(raw, defaults["status"]),
        "action": _normalize_action(raw.get("action"), defaults["action"]),
        "price_lower": price_lower,
        "price_upper": price_upper,
        "grid_count": grid_count,
        "grid_mode": grid_mode,
        "grid_side": grid_side,
        "lot_per_grid": max(
            0.0, _as_float(raw.get("lot_per_grid", defaults["lot_per_grid"]), defaults["lot_per_grid"]),
        ),
        "total_lot_limit": max(
            0.0, _as_float(raw.get("total_lot_limit", defaults["total_lot_limit"]), defaults["total_lot_limit"]),
        ),
        "trigger_price": max(
            0.0, _as_float(raw.get("trigger_price", defaults["trigger_price"]), defaults["trigger_price"]),
        ),
        "stop_lower": stop_lower,
        "stop_upper": stop_upper,
        "close_on_stop": bool(raw.get("close_on_stop", defaults["close_on_stop"])),
        "prefill_enabled": bool(raw.get("prefill_enabled", defaults["prefill_enabled"])),
        "trailing_up": bool(raw.get("trailing_up", defaults["trailing_up"])),
        "trailing_max": min(
            GRID_TRAILING_MAX,
            max(0, _as_int(raw.get("trailing_max", defaults["trailing_max"]), defaults["trailing_max"])),
        ),
        "assist_enabled": bool(raw.get("assist_enabled", defaults["assist_enabled"])),
        "assist_timeframe": assist_timeframe,
        "assist_atr_mult": assist_atr_mult,
        "assist_spacing": max(
            0.0, _as_float(raw.get("assist_spacing", defaults["assist_spacing"]), defaults["assist_spacing"]),
        ),
        "assist_max_loss": max(
            0.0, _as_float(raw.get("assist_max_loss", defaults["assist_max_loss"]), defaults["assist_max_loss"]),
        ),
    }


# 各 type 的规范化实现。新增模版时在此登记，normalize_rule 无需再改。
_RULE_NORMALIZERS: dict[int, Callable[[int, dict], dict[str, Any]]] = {
    RULE_TYPE_COUNTER: _normalize_add_on_rule,
    RULE_TYPE_TREND: _normalize_add_on_rule,
    RULE_TYPE_RISK_SIZED: _normalize_risk_sized_rule,
    RULE_TYPE_GRID: _normalize_grid_rule,
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


def validate_rules_for_template(template_id: str, rules: list[dict]) -> Optional[str]:
    """模版与规则一致性校验；通过返回 None，否则返回人读的拒收原因。

    - 规则 type 必须落在该模版允许集合内；
    - 至少一条启用中的规则；
    - 模版3 启用规则还要过区间 / 手数等基础合法性（与分发准入对齐）；
    - 模版1 的信号级盈亏控制字段即使加仓规则关闭也要过合法性（策略共享一套参数）。
    """
    allowed = TEMPLATE_RULE_TYPES.get(str(template_id or "").strip())
    if allowed is None:
        return None
    if not rules:
        return "请至少配置一条规则"
    active = 0
    signal_pl_checked = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = _as_int(rule.get("type"), 0)
        if rule_type not in allowed:
            return f"策略模版 {template_id} 不允许规则类型 {rule_type}"
        if rule_type in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) and not signal_pl_checked:
            reason = _validate_signal_pl_fields(rule)
            if reason:
                return reason
            signal_pl_checked = True
        if rule_type in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
            reason = _validate_manual_scatter_fields(rule)
            if reason:
                return reason
        if not _as_int(rule.get("status"), 0):
            continue
        active += 1
        if rule_type == RULE_TYPE_GRID:
            reason = _validate_grid_rule_fields(rule)
            if reason:
                return reason
    if active <= 0:
        return "请至少启用一条规则"
    return None


def _validate_signal_pl_fields(rule: dict) -> Optional[str]:
    """模版1 信号级盈亏控制合法性（与账户风控口径对齐，但不要求已启用）。"""
    pl = rule.get("float_pl_ratio") if isinstance(rule.get("float_pl_ratio"), dict) else {}
    try:
        ratio_f = float(pl.get("ratio"))
    except (TypeError, ValueError):
        return "信号盈亏比比例无效"
    if ratio_f == 0:
        return "信号盈亏比比例不能为 0"
    mode = pl.get("monitor_mode")
    if mode not in SIGNAL_PL_MONITOR_MODES:
        return "信号盈亏比监控模式无效"
    if mode == SIGNAL_PL_MONITOR_TIMES:
        mt = _as_int(pl.get("max_times"), 0)
        if mt < 1:
            return "信号盈亏比指定次数至少为 1"

    lpt = rule.get("lot_pl_tiers") if isinstance(rule.get("lot_pl_tiers"), dict) else {}
    if lpt.get("close_action") not in SIGNAL_PL_CLOSE_SIDES:
        return "信号分档平仓动作无效"
    tiers = lpt.get("tiers") or []
    if not isinstance(tiers, list) or not tiers:
        return "信号分档平仓至少需要 1 个批次"
    for i, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            return f"信号分档批次#{i + 1}参数无效"
        try:
            min_lot = float(tier.get("min_lot"))
            pla = float(tier.get("pl_amount"))
        except (TypeError, ValueError):
            return f"信号分档批次#{i + 1}参数无效"
        if min_lot < 0:
            return f"信号分档批次#{i + 1}手数不能为负"
        if pla == 0:
            return f"信号分档批次#{i + 1}盈亏金额不能为 0"
    return None


def _validate_manual_scatter_fields(rule: dict) -> Optional[str]:
    """手动分散仓：关闭时不校验价格；启用时入场/止盈须大于 0 且不相等。"""
    raw = rule.get("manual_scatter")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    entry = _as_float(raw.get("entry_price"), 0.0)
    tp = _as_float(raw.get("take_profit"), 0.0)
    sl = _as_float(raw.get("stop_loss"), 0.0)
    vol = _as_float(raw.get("volume"), 0.0)
    if entry <= 0 or tp <= 0:
        return "手动分散仓需要填写大于 0 的入场价与止盈价"
    if entry == tp:
        return "手动分散仓的入场价与止盈价不能相同"
    if sl < 0:
        return "手动分散仓止损价不能为负"
    if vol < 0:
        return "手动分散仓手数不能为负"
    return None


def _validate_grid_rule_fields(rule: dict) -> Optional[str]:
    """网格规则字段合法性（不依赖信号方向）。"""
    lower = _as_float(rule.get("price_lower"), 0.0)
    upper = _as_float(rule.get("price_upper"), 0.0)
    if lower <= 0 or upper <= 0 or upper <= lower:
        return "网格交易需要合法的价格区间（上限须大于下限，且均大于 0）"
    lot = _as_float(rule.get("lot_per_grid"), 0.0)
    if lot <= 0:
        return "网格交易的每格手数需大于 0"
    limit = _as_float(rule.get("total_lot_limit"), 0.0)
    if limit > 0 and limit < lot:
        return "网格交易的总手数上限须不小于每格手数"
    return None


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
                    float_pl_ratio=deepcopy(normalized["float_pl_ratio"]),
                    lot_pl_tiers=deepcopy(normalized["lot_pl_tiers"]),
                    manual_scatter=deepcopy(normalized["manual_scatter"]),
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
                    float_pl_ratio=deepcopy(normalized["float_pl_ratio"]),
                    lot_pl_tiers=deepcopy(normalized["lot_pl_tiers"]),
                    manual_scatter=deepcopy(normalized["manual_scatter"]),
                )
    return TemplateRuleSet(counter=counter, trend=trend)
