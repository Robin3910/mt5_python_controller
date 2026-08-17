"""以损定量趋势单的纯计算逻辑（无 I/O，可单独单元测试）。

对应后台「策略模版2」，对齐 MTcommander「以损定量趋势单」：

- risk_amount：本次交易愿意承担的亏损金额（账户货币）
- rr_ratio：盈亏比，止盈距离 = 止损距离 × rr_ratio，只有阶梯最远一档吃满
- base_ratio：底仓占总手数的百分比，底仓止盈为 0
- add_batches：剩余仓位拆成几笔「分散仓」，0 = 底仓即全仓
- max_total_lot：总手数上限，0 表示不额外限制
- breakeven_enabled / breakeven_times：浮盈达到止损距离 × N 倍时把止损移到保本
- breakeven_mode：once=按次（触发一次后停止）/ loop=循环（止损未到位时可反复移动）
- entry_mode：market=市价打齐 / limit=在信号入场价挂限价等成交

核心是「先定亏多少，再算开多少手」：

    每手止损亏损 = 止损距离 / tick_size × tick_value
    总手数       = risk_amount / 每手止损亏损

## 市价模式（entry_mode=market）

止损距离按「止损触发侧」的报价算（BUY 由 bid 触发、SELL 由 ask 触发），阶梯止盈
则以开仓侧报价为锚，两者差一个点差。开仓时一次下齐：1 笔底仓（TP=0）+ N 笔等手数
分散仓，第 i 笔止盈 = 开仓价 + 盈亏比 × 止损距离 × i / N——满盈亏比只落在最远一档，
前面各档按比例提前兑现。所有订单共用信号那一个止损价，各单开仓价相同，止损距离
是单值，打到止损的总亏损恰好等于 risk_amount。

## 限价模式（entry_mode=limit）

底仓与分散仓全部挂在信号给的入场价（同一点位），共用信号止损价。止损距离因此
是单值，手数口径与同等止损距离的市价计划一致。挂单一直等到成交（GTC）。
阶梯止盈仍以该入场价为锚，与市价模式同一套公式。

## 手数切分

剩余仓位按 ⌊剩余 ÷ 单数⌋ 等分，除不尽的余量宁可不开：阶梯止盈下最远档最难兑现，
把余量并到末笔等于把最重的仓压在最难到达的那一档上。切完之后一律按实际仓位分布
复核一次风险——向下取整与最小手数托底会让实际分布偏离配置比例，超预算就收缩
总手数重切。

手数计算需要品种的报价与手数规格（tick_value / volume_step 等），只有 MT5 能给，
所以这一层放在节点侧；服务端只负责下发风险参数。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# 规则 type，与后台 strategy_templates.RULE_TYPE_RISK_SIZED 对齐
RULE_TYPE_RISK_SIZED = 3

# 分散仓单数硬上限，防止配置写成天文数字一次打爆终端
DISTRIBUTE_COUNT_MAX = 50

# 保本监控模式（与后台 strategy_templates.BREAKEVEN_* 对齐）
BREAKEVEN_ONCE = "once"
BREAKEVEN_LOOP = "loop"
BREAKEVEN_MODES = (BREAKEVEN_ONCE, BREAKEVEN_LOOP)

# 开仓方式（与后台 strategy_templates.ENTRY_MODE_* 对齐）
ENTRY_MODE_MARKET = "market"
ENTRY_MODE_LIMIT = "limit"
ENTRY_MODES = (ENTRY_MODE_MARKET, ENTRY_MODE_LIMIT)

# 按实际仓位分布复核风险时，最多收缩几轮总手数
_RISK_FIT_ROUNDS = 4

# MT5 订单备注上限约 31 字符
MT5_COMMENT_LIMIT = 31

# 分散仓备注：R3B{index}（断线恢复靠它判断已开过哪些档，勿用持仓笔数）
_BATCH_COMMENT_RE = re.compile(r"^R3B(\d+)$")


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


def _trim(value: float, digits: int) -> str:
    """按位数格式化并去掉尾随零，避免说明里出现 0.10000000000000001。"""
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------

@dataclass
class SymbolSpec:
    """品种的报价与手数规格，由节点从 MT5 读出后传入。"""
    point: float = 0.0
    digits: int = 5
    tick_size: float = 0.0
    tick_value: float = 0.0     # 一手波动一个 tick 的价值（账户货币）
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0

    @property
    def volume_digits(self) -> int:
        """手数的小数位数，由 volume_step 推出（0.01 -> 2）。"""
        step = self.volume_step
        if step <= 0:
            return 2
        digits = 0
        while digits < 8 and abs(step * (10 ** digits) - round(step * (10 ** digits))) > 1e-9:
            digits += 1
        return digits

    def usable(self) -> bool:
        """规格是否足以支撑以损定量计算。"""
        return (
            self.point > 0 and self.tick_size > 0 and self.tick_value > 0
            and self.volume_step > 0 and self.volume_min > 0
        )

    def floor_lot(self, lot: float) -> float:
        """按 volume_step 向下取整——宁可少开一档，也不要超出风险金额。"""
        if self.volume_step <= 0:
            return round(max(lot, 0.0), 2)
        steps = math.floor((max(lot, 0.0) + 1e-9) / self.volume_step)
        return round(steps * self.volume_step, self.volume_digits)


@dataclass
class RiskSizedConfig:
    """一条以损定量趋势单规则的解析结果。"""
    risk_amount: float = 0.0
    rr_ratio: float = 0.0
    base_ratio: float = 100.0
    add_batches: int = 0          # 分散仓单数；0 = 底仓即全仓
    max_total_lot: float = 0.0
    breakeven_enabled: bool = False
    breakeven_times: float = 0.0
    breakeven_mode: str = BREAKEVEN_ONCE
    action: str = "all"
    entry_mode: str = ENTRY_MODE_MARKET

    @classmethod
    def from_rule(cls, rule: dict) -> "RiskSizedConfig":
        add_batches = max(0, min(DISTRIBUTE_COUNT_MAX, _as_int(rule.get("add_batches"), 0)))
        base_ratio = _as_float(rule.get("base_ratio"), 100.0)
        base_ratio = min(100.0, max(0.0, base_ratio)) or 100.0
        mode = str(rule.get("breakeven_mode") or BREAKEVEN_ONCE).strip().lower()
        if mode not in BREAKEVEN_MODES:
            mode = BREAKEVEN_ONCE
        entry_mode = str(rule.get("entry_mode") or ENTRY_MODE_MARKET).strip().lower()
        if entry_mode not in ENTRY_MODES:
            entry_mode = ENTRY_MODE_MARKET
        return cls(
            risk_amount=max(0.0, _as_float(rule.get("risk_amount"))),
            rr_ratio=max(0.0, _as_float(rule.get("rr_ratio"))),
            base_ratio=100.0 if not add_batches else base_ratio,
            add_batches=add_batches,
            max_total_lot=max(0.0, _as_float(rule.get("max_total_lot"))),
            breakeven_enabled=bool(rule.get("breakeven_enabled")),
            breakeven_times=max(0.0, _as_float(rule.get("breakeven_times"))),
            breakeven_mode=mode,
            action=str(rule.get("action") or "all").strip().lower(),
            entry_mode=entry_mode,
        )

    @property
    def breakeven_is_loop(self) -> bool:
        return self.breakeven_mode == BREAKEVEN_LOOP

    @property
    def breakeven_mode_label(self) -> str:
        return "循环" if self.breakeven_is_loop else "按次"

    @property
    def is_limit_entry(self) -> bool:
        return self.entry_mode == ENTRY_MODE_LIMIT

    @property
    def entry_mode_label(self) -> str:
        return "限价" if self.is_limit_entry else "市价"


def pick_risk_sized_rule(rules: object) -> Optional[dict]:
    """从策略快照里取出启用中的以损定量趋势单规则；没有则返回 None。"""
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _as_int(rule.get("type")) != RULE_TYPE_RISK_SIZED:
            continue
        if _as_int(rule.get("status")):
            return rule
    return None


def action_matches(rule_action: object, direction: str) -> bool:
    """规则的监控方向是否覆盖本次信号方向。"""
    act = str(rule_action or "all").strip().lower()
    if act == "all":
        return True
    return act == str(direction or "").strip().lower()


# ---------------------------------------------------------------------------
# 建仓计划
# ---------------------------------------------------------------------------

@dataclass
class Batch:
    """建仓计划里的一笔：底仓 TP=0，分散仓挂阶梯上属于自己的那一档。"""
    index: int
    volume: float
    take_profit: float = 0.0    # 0 表示不设止盈（底仓）
    price: float = 0.0          # 限价模式的挂单价；0 = 市价成交
    sl_distance: float = 0.0    # 该档开仓价到止损价的距离（各档相同）

    @property
    def is_base(self) -> bool:
        return self.index == 0

    @property
    def is_distribute(self) -> bool:
        return self.index > 0

    @property
    def is_pending(self) -> bool:
        return self.price > 0


@dataclass
class EntryPlan:
    """一次以损定量建仓的完整计划。reject 非空表示不可执行。"""
    direction: str = "BUY"
    entry_price: float = 0.0    # 开仓价：市价=开仓侧报价 / 限价=底仓挂单价，阶梯止盈的锚点
    risk_price: float = 0.0     # 止损触发侧报价（BUY=bid / SELL=ask），止损距离的基准
    spread: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0    # 阶梯最远一档（吃满盈亏比）；底仓为 0
    tp_step: float = 0.0        # 相邻两档的价格间隔
    sl_distance: float = 0.0    # 开仓价到止损价的距离（各档相同）
    sl_points: float = 0.0
    loss_per_lot: float = 0.0   # 每手止损亏损（单值止损距离）
    planned_lot: float = 0.0    # 按风险金额反推的总手数
    total_lot: float = 0.0      # 等分后实际下单的总手数（≤ planned_lot）
    risk_used: float = 0.0
    entry_mode: str = ENTRY_MODE_MARKET
    ladder_step: float = 0.0    # 历史字段：入场曾按阶梯铺开，同价挂单后恒为 0
    batches: list[Batch] = field(default_factory=list)
    reject: str = ""

    @property
    def is_limit_entry(self) -> bool:
        return self.entry_mode == ENTRY_MODE_LIMIT

    @property
    def dropped_lot(self) -> float:
        """等分除不尽、放弃不开的那部分手数。"""
        return round(max(self.planned_lot - self.total_lot, 0.0), 8)

    @property
    def ok(self) -> bool:
        return not self.reject and bool(self.batches)

    @property
    def base_volume(self) -> float:
        return self.batches[0].volume if self.batches else 0.0

    @property
    def distribute_volume(self) -> float:
        return round(sum(b.volume for b in self.batches if b.is_distribute), 8)

    @property
    def add_batch_count(self) -> int:
        return max(0, len(self.batches) - 1)

    @property
    def order_count(self) -> int:
        return len(self.batches)

    def batch_at(self, index: int) -> Optional[Batch]:
        for b in self.batches:
            if b.index == index:
                return b
        return None


def _is_buy(direction: object) -> bool:
    return str(direction or "").strip().upper() == "BUY"


def _favorable_sign(direction: object) -> int:
    """有利方向的价格符号：多单向上、空单向下。"""
    return 1 if _is_buy(direction) else -1


def _split_lots(total_lot: float, cfg: RiskSizedConfig, spec: SymbolSpec) -> list[float]:
    """把总手数切成「底仓 + 各分散仓」；分散仓严格等手数。

    每笔都不能低于 volume_min；单数因此可能少于配置值。除不尽的余量直接放弃不开，
    各笔之和会小于 total_lot——阶梯止盈下最远一档最难兑现，把余量并到末笔等于让
    最重的仓去赌最难到达的那一档。
    """
    base = spec.floor_lot(total_lot * cfg.base_ratio / 100.0)
    if base < spec.volume_min:
        base = spec.volume_min
    if base >= total_lot:
        return [total_lot]

    rest = round(total_lot - base, spec.volume_digits)
    if cfg.add_batches <= 0 or rest < spec.volume_min:
        return [total_lot]

    per = spec.floor_lot(rest / cfg.add_batches)
    if per < spec.volume_min:
        # 剩余不够按配置单数切：每笔顶到最小手数，能开几单算几单
        per = spec.volume_min
    count = min(cfg.add_batches, int(math.floor((rest + 1e-9) / per)))
    if count <= 0:
        return [total_lot]
    return [base] + [per] * count


def _risk_of(lots: list[float], distances: list[float], spec: SymbolSpec) -> float:
    """这组仓位全部成交后打到止损的总亏损。"""
    if spec.tick_size <= 0:
        return 0.0
    paired = sum(lot * dist for lot, dist in zip(lots, distances))
    return paired / spec.tick_size * spec.tick_value


def _fit_lots(cfg: RiskSizedConfig, spec: SymbolSpec, distances: list[float],
              budget: float, planned: float) -> tuple[list[float], float]:
    """在风险预算内切分手数，返回 (各档手数, 实际风险)；切不出来返回 ([], 0)。

    切完必须按实际分布复核：向下取整与最小手数托底会让实际仓位偏离配置比例（比如
    总手数太小时底仓被顶到 volume_min）。各档止损距离相同，偏离不会放大风险，
    但取整后总手数仍可能让金额对不齐，超预算就收缩重切。
    """
    for _ in range(_RISK_FIT_ROUNDS):
        lots = _split_lots(planned, cfg, spec)
        risk = _risk_of(lots, distances, spec)
        if risk <= budget + 1e-9:
            return lots, risk
        # 按超出比例收缩；取整可能让结果原地不动，此时强制退一个步长避免空转
        shrunk = spec.floor_lot(planned * budget / risk) if risk > 0 else 0.0
        if shrunk >= planned:
            shrunk = spec.floor_lot(planned - spec.volume_step)
        planned = shrunk
        if planned < spec.volume_min:
            return [], 0.0
    return [], 0.0


def _apply_ladder(plan: EntryPlan, cfg: RiskSizedConfig, spec: SymbolSpec) -> None:
    """给分散仓铺等分阶梯止盈：第 i 档 = 开仓价 + 盈亏比 × 止损距离 × i / 分散仓单数。

    满盈亏比只落在最远一档，前面各档按比例提前兑现。档数用实际排出来的分散仓单数，
    而不是配置值——手数不够被削过单数时，最远一档仍要吃满配置的盈亏比。
    底仓不设止盈，留给保本止损与趋势。
    """
    count = plan.add_batch_count
    span = plan.sl_distance * cfg.rr_ratio
    sign = _favorable_sign(plan.direction)
    laddered = count > 0 and span > 0
    plan.tp_step = round(span / count, spec.digits) if laddered else 0.0
    plan.take_profit = (
        round(plan.entry_price + sign * span, spec.digits) if laddered else 0.0
    )
    for batch in plan.batches:
        if batch.is_base or not laddered:
            batch.take_profit = 0.0
            continue
        # 用 span × i / count 而不是累加 tp_step，避免逐档累积取整误差
        batch.take_profit = round(
            plan.entry_price + sign * span * batch.index / count, spec.digits,
        )


def plan_entries(cfg: RiskSizedConfig, *, direction: str, entry_price: float,
                 stop_loss: float, spec: SymbolSpec, risk_price: float = 0.0,
                 limit_price: float = 0.0) -> EntryPlan:
    """按风险金额与止损价反推总手数，切成底仓 + 等手数分散仓，并铺阶梯止盈。

    市价模式下 entry_price 是开仓侧报价（BUY=ask / SELL=bid），阶梯止盈以它为锚；
    risk_price 是止损触发侧报价（BUY=bid / SELL=ask），止损距离以它为准；拿不到
    点差时 risk_price 留空，两者退化为同一个价。

    限价模式下 limit_price 是信号给的挂单价，底仓与分散仓都挂在这个价；它同时充当
    开仓价与止损距离基准——挂单按挂单价成交，没有点差与滑点，因此不需要区分开仓
    侧与止损触发侧。

    任何一步算不出可执行的结果都返回带 reject 的计划，由调用方上报后放弃本次任务。
    """
    plan = EntryPlan(direction=str(direction or "BUY").upper(), entry_price=entry_price,
                     stop_loss=stop_loss, entry_mode=cfg.entry_mode)
    plan.risk_price = risk_price if risk_price > 0 else entry_price
    if cfg.is_limit_entry:
        if limit_price <= 0:
            plan.reject = "限价开仓需要信号携带入场价"
            return plan
        # 挂单成交价就是挂单价，开仓侧与止损触发侧退化为同一个价
        plan.entry_price = limit_price
        plan.risk_price = limit_price
    if not action_matches(cfg.action, plan.direction):
        plan.reject = f"规则监控方向为 {cfg.action}，与信号方向 {plan.direction} 不符"
        return plan
    if cfg.risk_amount <= 0:
        plan.reject = "风险金额需大于 0"
        return plan
    if plan.entry_price <= 0:
        plan.reject = "无法取得开仓价"
        return plan
    if stop_loss <= 0:
        plan.reject = "以损定量需要信号携带止损价"
        return plan
    if not spec.usable():
        plan.reject = "品种规格不完整（point / tick / 手数步长），无法反推手数"
        return plan

    sign = _favorable_sign(plan.direction)
    if (stop_loss - plan.risk_price) * sign >= 0:
        plan.reject = (
            f"止损价 {_trim(stop_loss, spec.digits)} 不在 {plan.direction} 的不利方向"
        )
        return plan

    plan.spread = round(abs(plan.entry_price - plan.risk_price), spec.digits)
    plan.sl_distance = abs(plan.risk_price - stop_loss)
    plan.sl_points = plan.sl_distance / spec.point

    # 各档止损距离相同。限价也全部挂在入场价，手数口径与市价一致。
    distances = [plan.sl_distance] * (cfg.add_batches + 1)
    plan.loss_per_lot = (
        plan.sl_distance / spec.tick_size * spec.tick_value if spec.tick_size > 0 else 0.0
    )
    if plan.loss_per_lot <= 0:
        plan.reject = "每手止损亏损算得 0，无法反推手数"
        return plan

    planned = spec.floor_lot(cfg.risk_amount / plan.loss_per_lot)
    if cfg.max_total_lot > 0:
        planned = min(planned, spec.floor_lot(cfg.max_total_lot))
    if spec.volume_max > 0:
        planned = min(planned, spec.floor_lot(spec.volume_max))
    if planned < spec.volume_min:
        plan.reject = (
            f"按风险金额 {_trim(cfg.risk_amount, 2)} 与止损距离 "
            f"{_trim(plan.sl_points, 1)} 点算得 {_trim(planned, spec.volume_digits)} 手，"
            f"不足最小手数 {_trim(spec.volume_min, spec.volume_digits)}"
        )
        return plan

    lots, risk_used = _fit_lots(cfg, spec, distances, cfg.risk_amount, planned)
    if not lots:
        plan.reject = (
            f"按风险金额 {_trim(cfg.risk_amount, 2)} 切不出满足预算的仓位分布，"
            f"请调大风险金额或减少分散仓单数"
        )
        return plan

    plan.planned_lot = planned
    plan.total_lot = round(sum(lots), spec.volume_digits)
    plan.risk_used = round(risk_used, 2)
    pending_price = plan.entry_price if plan.is_limit_entry else 0.0
    plan.batches = [
        Batch(
            index=i, volume=volume,
            price=pending_price,
            sl_distance=plan.sl_distance,
        )
        for i, volume in enumerate(lots)
    ]
    _apply_ladder(plan, cfg, spec)
    return plan


def anchor_to_fill(plan: EntryPlan, cfg: RiskSizedConfig, fill_price: float,
                   spec: SymbolSpec) -> EntryPlan:
    """底仓实际成交后，按真实成交价重锚阶梯止盈；手数保持不变。

    手数是按下单前的预估价算的，成交价会有滑点。手数不能再动（底仓已经成交），
    但阶梯必须以真实成交价为锚，否则盈亏比不再是配置值。点差随开仓价一起平移，
    止损距离仍按止损触发侧计。

    限价模式不重锚：挂单按挂单价成交，计划里的价位本来就是最终成交价，拿终端回报
    的价格再算一遍只会引入取整噪声。
    """
    if not plan.ok or plan.is_limit_entry:
        return plan
    if fill_price <= 0 or fill_price == plan.entry_price:
        return plan
    sign = _favorable_sign(plan.direction)
    plan.entry_price = fill_price
    plan.risk_price = round(fill_price - sign * plan.spread, spec.digits)
    plan.sl_distance = abs(plan.risk_price - plan.stop_loss)
    plan.sl_points = plan.sl_distance / spec.point if spec.point > 0 else 0.0
    plan.loss_per_lot = (
        plan.sl_distance / spec.tick_size * spec.tick_value if spec.tick_size > 0 else 0.0
    )
    plan.risk_used = round(plan.total_lot * plan.loss_per_lot, 2)
    _apply_ladder(plan, cfg, spec)
    return plan


# ---------------------------------------------------------------------------
# 运行期判定
# ---------------------------------------------------------------------------

def next_batch(plan: EntryPlan, cfg: RiskSizedConfig, *, filled: int,
               price: float = 0.0) -> Optional[Batch]:
    """取下一笔待开的分散仓；已开完返回 None。

    分散仓全部市价，price 参数保留仅为兼容旧调用方，不再参与判定。
    filled 是已成交笔数（含底仓），也就是下一批的 index。
    """
    del cfg, price  # 市价分散，不依赖规则间距与现价
    if not plan.ok or filled <= 0:
        return None
    batch = plan.batch_at(filled)
    if batch is None or batch.is_base:
        return None
    return batch


def pending_batches(plan: EntryPlan, *, filled: int) -> list[Batch]:
    """尚未开出的分散仓列表（用于开仓时一次打齐 / 恢复补开）。"""
    if not plan.ok or filled < 0:
        return []
    return [b for b in plan.batches if b.index >= filled and b.is_distribute]


@dataclass
class BreakevenMove:
    """一次保本止损移动，同时携带判定依据。"""
    stop_loss: float
    avg_price: float
    price: float
    favorable: float
    threshold: float
    times: float
    mode: str = BREAKEVEN_ONCE

    def describe(self, digits: int) -> str:
        mode_label = "循环" if self.mode == BREAKEVEN_LOOP else "按次"
        return (
            f"保本触发（{mode_label}）：均价 {_trim(self.avg_price, digits)} → 现价 "
            f"{_trim(self.price, digits)}，有利偏离 {_trim(self.favorable, digits)} "
            f"≥ 止损距 × {_trim(self.times, 2)} = {_trim(self.threshold, digits)}；"
            f"止损移至 {_trim(self.stop_loss, digits)}"
        )

    def detail(self) -> dict:
        return {
            "kind": "breakeven",
            "avg_price": self.avg_price,
            "price": self.price,
            "favorable": self.favorable,
            "threshold": self.threshold,
            "times": self.times,
            "stop_loss": self.stop_loss,
            "breakeven_mode": self.mode,
            "breakeven_mode_label": "循环" if self.mode == BREAKEVEN_LOOP else "按次",
        }


def weighted_avg_price(positions: list[dict]) -> float:
    """持仓的加权平均开仓价——保本止损要挪到这里，而不是首单价。"""
    total_vol = 0.0
    weighted = 0.0
    for pos in positions:
        vol = _as_float(pos.get("volume"))
        price = _as_float(pos.get("price_open"))
        if vol <= 0 or price <= 0:
            continue
        total_vol += vol
        weighted += vol * price
    return weighted / total_vol if total_vol > 0 else 0.0


def _worst_stop_loss(positions: list[dict], *, direction: str) -> float:
    """当前持仓里最「差」的止损价（多单取最小、空单取最大）；无止损则返回 0。"""
    stops = [_as_float(p.get("sl")) for p in positions if _as_float(p.get("sl")) > 0]
    if not stops:
        return 0.0
    return min(stops) if _is_buy(direction) else max(stops)


def _stop_already_at_or_better(current_sl: float, target: float, *, direction: str,
                               digits: int) -> bool:
    """止损是否已经在目标均价或更优一侧（避免循环模式下反复改同一价）。"""
    if current_sl <= 0 or target <= 0:
        return False
    eps = 10 ** (-max(digits, 0)) / 2
    if _is_buy(direction):
        return current_sl + eps >= target
    return current_sl - eps <= target


def breakeven_move(cfg: RiskSizedConfig, plan: EntryPlan, *, positions: list[dict],
                   price: float, digits: int = 5) -> Optional[BreakevenMove]:
    """浮盈是否达到保本阈值；达标则返回要把止损挪到的均价。

    若当前止损已经在均价或更优一侧，不再重复改单（按次 / 循环都适用）。
    """
    if not cfg.breakeven_enabled or cfg.breakeven_times <= 0 or not plan.ok:
        return None
    if price <= 0 or not positions:
        return None
    avg = weighted_avg_price(positions)
    if avg <= 0:
        return None
    target = round(avg, digits)
    current_sl = _worst_stop_loss(positions, direction=plan.direction)
    if _stop_already_at_or_better(current_sl, target, direction=plan.direction, digits=digits):
        return None
    sign = _favorable_sign(plan.direction)
    favorable = (price - avg) * sign
    threshold = plan.sl_distance * cfg.breakeven_times
    if favorable + 1e-12 < threshold:
        return None
    return BreakevenMove(
        stop_loss=target,
        avg_price=target,
        price=price,
        favorable=round(favorable, digits),
        threshold=round(threshold, digits),
        times=cfg.breakeven_times,
        mode=cfg.breakeven_mode,
    )


# ---------------------------------------------------------------------------
# 开单原因与计算规则的说明生成
# ---------------------------------------------------------------------------

def describe_plan(plan: EntryPlan, cfg: RiskSizedConfig, spec: SymbolSpec) -> str:
    """建仓计划的一句话说明：手数是怎么反推出来的、仓位怎么拆、阶梯怎么铺。"""
    vd, pd = spec.volume_digits, spec.digits
    lot_text = f"总手数 {_trim(plan.planned_lot, vd)}"
    if plan.dropped_lot:
        lot_text += (
            f"（实下 {_trim(plan.total_lot, vd)}，"
            f"余 {_trim(plan.dropped_lot, vd)} 不开）"
        )
    limit = plan.is_limit_entry
    distance_text = f"止损距离 {_trim(plan.sl_points, 1)} 点"
    base_text = (
        f"底仓 {_trim(cfg.base_ratio, 1)}% = {_trim(plan.base_volume, vd)} 手"
        + (f"限价 @{_trim(plan.entry_price, pd)}（TP=0）" if limit else "市价（TP=0）")
    )
    parts = [
        f"以损定量（{cfg.entry_mode_label}开仓）：风险金额 {_trim(cfg.risk_amount, 2)}"
        f" ÷ 每手止损亏损 {_trim(plan.loss_per_lot, 2)}（{distance_text}）"
        f" = {lot_text}，实际风险 {_trim(plan.risk_used, 2)}",
        base_text,
    ]
    if plan.add_batch_count:
        way = (
            f"限价 @{_trim(plan.entry_price, pd)}"
            if limit else "市价"
        )
        parts.append(
            f"分散仓 {_trim(plan.distribute_volume, vd)} 手等分 "
            f"{plan.add_batch_count} 单{way}"
            + (f"（阶梯止盈 {_trim(plan.batches[1].take_profit, pd)} → "
               f"{_trim(plan.take_profit, pd)}，步长 {_trim(plan.tp_step, pd)}，"
               f"满档盈亏比 {_trim(cfg.rr_ratio, 2)}）"
               if plan.take_profit else "（不设止盈）")
        )
    else:
        parts.append("无分散仓，底仓即全仓")
    parts.append(
        f"共用止损 {_trim(plan.stop_loss, spec.digits)}，共 {plan.order_count} 单"
    )
    return "；".join(parts)


def plan_detail(plan: EntryPlan, cfg: RiskSizedConfig, spec: SymbolSpec) -> dict:
    """建仓计划的结构化明细，供后台逐项展示计算参数。"""
    return {
        "kind": "risk_sized_plan",
        "direction": plan.direction,
        "entry_mode": plan.entry_mode,
        "entry_mode_label": cfg.entry_mode_label,
        "ladder_step": plan.ladder_step or None,
        "entry_price": plan.entry_price,
        "risk_price": plan.risk_price,
        "spread": plan.spread or None,
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit or None,
        "tp_step": plan.tp_step or None,
        "risk_amount": cfg.risk_amount,
        "risk_used": plan.risk_used,
        "rr_ratio": cfg.rr_ratio,
        "sl_distance": round(plan.sl_distance, 8),
        "sl_points": round(plan.sl_points, 2),
        "loss_per_lot": round(plan.loss_per_lot, 4),
        "planned_lot": plan.planned_lot,
        "total_lot": plan.total_lot,
        "dropped_lot": plan.dropped_lot or None,
        "lot_formula": (
            f"{_trim(cfg.risk_amount, 2)} ÷ {_trim(plan.loss_per_lot, 2)}"
            f" = {_trim(plan.planned_lot, spec.volume_digits)}"
        ),
        "base_ratio": cfg.base_ratio,
        "base_volume": plan.base_volume,
        "distribute_volume": plan.distribute_volume,
        "add_batches": plan.add_batch_count,
        "order_count": plan.order_count,
        "breakeven_enabled": cfg.breakeven_enabled,
        "breakeven_times": cfg.breakeven_times if cfg.breakeven_enabled else None,
        "breakeven_mode": cfg.breakeven_mode if cfg.breakeven_enabled else None,
        "breakeven_mode_label": cfg.breakeven_mode_label if cfg.breakeven_enabled else None,
        "tick_value": spec.tick_value,
        "tick_size": spec.tick_size,
        "volume_step": spec.volume_step,
        "batches": [
            {
                "index": b.index,
                "volume": b.volume,
                "take_profit": b.take_profit or None,
                "price": b.price or None,
                "sl_distance": round(b.sl_distance, 8) or None,
                "role": "base" if b.is_base else "distribute",
            }
            for b in plan.batches
        ],
    }


def describe_batch(plan: EntryPlan, cfg: RiskSizedConfig, batch: Batch,
                   spec: SymbolSpec, price: float) -> str:
    """一笔分散仓的开单原因。"""
    del cfg  # 说明里用 plan / batch 即可
    way = (
        f"限价 {_trim(batch.volume, spec.volume_digits)} 手 @{_trim(batch.price, spec.digits)}"
        if batch.is_pending
        else f"市价 {_trim(batch.volume, spec.volume_digits)} 手"
    )
    return (
        f"以损定量分散仓 · 第 {batch.index}/{plan.add_batch_count} 档："
        f"{way}（现价 {_trim(price, spec.digits)}）；"
        f"共用止损 {_trim(plan.stop_loss, spec.digits)}，阶梯止盈 "
        + (f"{_trim(batch.take_profit, spec.digits)}"
           f"（满档 {_trim(plan.take_profit, spec.digits)}）"
           if batch.take_profit else "不设")
        + f"；总手数 {_trim(plan.total_lot, spec.volume_digits)}，"
        f"风险仍为 {_trim(plan.risk_used, 2)}"
    )


def batch_detail(plan: EntryPlan, cfg: RiskSizedConfig, batch: Batch,
                 spec: SymbolSpec, price: float) -> dict:
    """一笔分散仓的结构化明细。"""
    return {
        "kind": "risk_sized_distribute",
        "batch_index": batch.index,
        "batch_total": plan.add_batch_count,
        "order_count": plan.order_count,
        "direction": plan.direction,
        "entry_mode": plan.entry_mode,
        "entry_price": plan.entry_price,
        "limit_price": batch.price or None,
        "price": price,
        "volume": batch.volume,
        "total_lot": plan.total_lot,
        "stop_loss": plan.stop_loss,
        "take_profit": batch.take_profit or None,
        "tp_step": plan.tp_step or None,
        "tp_full": plan.take_profit or None,
        "risk_amount": cfg.risk_amount,
        "risk_used": plan.risk_used,
        "role": "distribute",
    }


def batch_comment(batch: Batch) -> str:
    """分散仓写进 MT5 的紧凑备注：R3=以损定量，B=序号。"""
    text = f"R3B{batch.index}"
    return text[:MT5_COMMENT_LIMIT]


def parse_batch_comment(comment: object) -> Optional[int]:
    """从 MT5 备注解析分散仓批次号；解析失败返回 None。"""
    text = str(comment or "").strip()
    m = _BATCH_COMMENT_RE.match(text)
    if not m:
        return None
    return int(m.group(1))


def filled_orders_from_positions(positions: list[dict]) -> int:
    """按持仓备注推断已开过的订单数（含底仓），供 pending_batches(filled=...) 使用。

    中间档被阶梯止盈平掉后，持仓笔数会少于真实已开档数；若仍用 len(positions)
    当 filled，会把已平掉的档再开一遍。见过 R3B{n} 时取 max(n)+1（底仓必已开过）；
    只有底仓备注（S* / 信号备注）时回退到持仓笔数。
    """
    if not positions:
        return 0
    max_idx = -1
    saw_distribute = False
    for pos in positions:
        idx = parse_batch_comment(pos.get("comment"))
        if idx is None:
            continue
        saw_distribute = True
        max_idx = max(max_idx, idx)
    if not saw_distribute:
        return len(positions)
    return max(max_idx, 0) + 1
