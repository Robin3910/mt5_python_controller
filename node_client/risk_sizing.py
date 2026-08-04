"""以损定量趋势单的纯计算逻辑（无 I/O，可单独单元测试）。

对应后台「策略模版2」，规则字段随 strategy_start 的策略快照下发：

- risk_amount：本次交易愿意承担的亏损金额（账户货币）
- rr_ratio：盈亏比，止盈距离 = 止损距离 × rr_ratio
- base_ratio：底仓占总手数的百分比，底仓市价成交
- add_batches：剩余仓位分几批补齐，0 表示底仓即全仓
- entry_direction：补仓方向，pullback 回撤补仓 / breakout 突破加仓
- batch_gap_points：相邻批次的触发间距（点）
- max_total_lot：总手数上限，0 表示不额外限制
- breakeven_enabled / breakeven_times：浮盈达到止损距离 × N 倍时把止损移到保本

核心是「先定亏多少，再算开多少手」：

    每手止损亏损 = 止损距离 / tick_size × tick_value
    总手数       = risk_amount / 每手止损亏损

所有批次共用信号那一个止损价，因此无论补进几批，打到止损的总亏损始终等于
risk_amount——这也是为什么补仓触发价必须落在止损之内（见 _effective_gap）：
触发价越过止损的批次永远不会成交，实际风险就会小于设定值、仓位也补不齐。

手数计算需要品种的报价与手数规格（tick_value / volume_step 等），只有 MT5 能给，
所以这一层放在节点侧；服务端只负责下发风险参数，与 ATR / 波幅的分工一致。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# 规则 type，与后台 strategy_templates.RULE_TYPE_RISK_SIZED 对齐
RULE_TYPE_RISK_SIZED = 3

ENTRY_PULLBACK = "pullback"
ENTRY_BREAKOUT = "breakout"

ENTRY_DIRECTION_LABEL = {
    ENTRY_PULLBACK: "回撤补仓",
    ENTRY_BREAKOUT: "突破加仓",
}

# MT5 订单备注上限约 31 字符
MT5_COMMENT_LIMIT = 31


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
    add_batches: int = 0
    entry_direction: str = ENTRY_PULLBACK
    batch_gap_points: float = 0.0
    max_total_lot: float = 0.0
    breakeven_enabled: bool = False
    breakeven_times: float = 0.0
    action: str = "all"

    @classmethod
    def from_rule(cls, rule: dict) -> "RiskSizedConfig":
        entry_direction = str(rule.get("entry_direction") or ENTRY_PULLBACK).strip().lower()
        if entry_direction not in (ENTRY_PULLBACK, ENTRY_BREAKOUT):
            entry_direction = ENTRY_PULLBACK
        add_batches = max(0, _as_int(rule.get("add_batches"), 0))
        base_ratio = _as_float(rule.get("base_ratio"), 100.0)
        base_ratio = min(100.0, max(0.0, base_ratio)) or 100.0
        return cls(
            risk_amount=max(0.0, _as_float(rule.get("risk_amount"))),
            rr_ratio=max(0.0, _as_float(rule.get("rr_ratio"))),
            base_ratio=100.0 if not add_batches else base_ratio,
            add_batches=add_batches,
            entry_direction=entry_direction,
            batch_gap_points=max(0.0, _as_float(rule.get("batch_gap_points"))),
            max_total_lot=max(0.0, _as_float(rule.get("max_total_lot"))),
            breakeven_enabled=bool(rule.get("breakeven_enabled")),
            breakeven_times=max(0.0, _as_float(rule.get("breakeven_times"))),
            action=str(rule.get("action") or "all").strip().lower(),
        )

    @property
    def entry_direction_label(self) -> str:
        return ENTRY_DIRECTION_LABEL.get(self.entry_direction, self.entry_direction)


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
    """建仓计划里的一笔：底仓 index=0 市价成交，其余到价补仓。"""
    index: int
    volume: float
    trigger_price: float = 0.0      # 0 表示市价（底仓）
    gap_points: float = 0.0         # 相对首单开仓价的偏离点数

    @property
    def is_base(self) -> bool:
        return self.index == 0


@dataclass
class EntryPlan:
    """一次以损定量建仓的完整计划。reject 非空表示不可执行。"""
    direction: str = "BUY"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    sl_distance: float = 0.0        # 止损距离（价格）
    sl_points: float = 0.0          # 止损距离（点）
    loss_per_lot: float = 0.0       # 一手打到止损的亏损（账户货币）
    total_lot: float = 0.0
    risk_used: float = 0.0          # 取整后的实际风险敞口
    gap_price: float = 0.0          # 实际生效的批次间距（价格）
    gap_points: float = 0.0         # 实际生效的批次间距（点）
    gap_capped: bool = False        # 间距是否因越过止损 / 止盈被压缩
    batches: list[Batch] = field(default_factory=list)
    reject: str = ""

    @property
    def ok(self) -> bool:
        return not self.reject and bool(self.batches)

    @property
    def base_volume(self) -> float:
        return self.batches[0].volume if self.batches else 0.0

    @property
    def add_batch_count(self) -> int:
        return max(0, len(self.batches) - 1)

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
    """把总手数切成「底仓 + 各补仓批」。

    每批都不能低于 volume_min，否则那一批根本下不出去；批数因此可能少于配置值。
    最后一批直接吃掉全部剩余，保证各批之和精确等于总手数——总手数才是风险的锚点，
    逐批取整累积的误差不能落在总量上。
    """
    base = spec.floor_lot(total_lot * cfg.base_ratio / 100.0)
    if base < spec.volume_min:
        base = spec.volume_min
    if base >= total_lot:
        return [total_lot]

    rest = round(total_lot - base, spec.volume_digits)
    if cfg.add_batches <= 0 or rest < spec.volume_min:
        # 补不出一笔合法手数：剩余并回底仓，避免留下永远补不进来的仓位
        return [total_lot]

    count = min(cfg.add_batches, int(math.floor((rest + 1e-9) / spec.volume_min)))
    count = max(1, count)
    per = max(spec.floor_lot(rest / count), spec.volume_min)

    lots = [base]
    used = base
    for i in range(count):
        volume = round(total_lot - used, spec.volume_digits) if i == count - 1 else per
        if volume < spec.volume_min:
            # 前面几批取整偏大，已经吃掉了剩余额度：就此收尾
            lots[-1] = round(lots[-1] + volume, spec.volume_digits)
            break
        lots.append(volume)
        used = round(used + volume, spec.volume_digits)
    return lots


def _effective_gap(cfg: RiskSizedConfig, plan_batches: int, sl_distance: float,
                   tp_distance: float, spec: SymbolSpec) -> tuple[float, bool]:
    """算出实际生效的批次间距（价格），以及是否被压缩过。

    回撤补仓的触发价必须留在止损之内，突破加仓的触发价必须留在止盈之内，否则最远
    那一批会落在出场价之外、永远不成交。把可用区间按批数均分即为间距上限。
    """
    configured = cfg.batch_gap_points * spec.point
    if plan_batches <= 0:
        return configured, False
    room = sl_distance if cfg.entry_direction == ENTRY_PULLBACK else tp_distance
    ceiling = room / (plan_batches + 1)
    if configured <= 0:
        return ceiling, True
    return (ceiling, True) if configured > ceiling else (configured, False)


def plan_entries(cfg: RiskSizedConfig, *, direction: str, entry_price: float,
                 stop_loss: float, spec: SymbolSpec) -> EntryPlan:
    """按风险金额与止损价反推总手数，并切分成底仓 + 各补仓批。

    任何一步算不出可执行的结果都返回带 reject 的计划，由调用方上报后放弃本次任务：
    宁可不开，也不要在参数不完整时凭默认值下单。
    """
    plan = EntryPlan(direction=str(direction or "BUY").upper(), entry_price=entry_price,
                     stop_loss=stop_loss)
    if not action_matches(cfg.action, plan.direction):
        plan.reject = f"规则监控方向为 {cfg.action}，与信号方向 {plan.direction} 不符"
        return plan
    if cfg.risk_amount <= 0:
        plan.reject = "风险金额需大于 0"
        return plan
    if entry_price <= 0:
        plan.reject = "无法取得开仓价"
        return plan
    if stop_loss <= 0:
        plan.reject = "以损定量需要信号携带止损价"
        return plan
    if not spec.usable():
        plan.reject = "品种规格不完整（point / tick / 手数步长），无法反推手数"
        return plan

    # 止损必须在持仓的不利侧，否则这一单开出来就已经触发止损
    sign = _favorable_sign(plan.direction)
    if (stop_loss - entry_price) * sign >= 0:
        plan.reject = (
            f"止损价 {_trim(stop_loss, spec.digits)} 不在 {plan.direction} 的不利方向"
        )
        return plan

    plan.sl_distance = abs(entry_price - stop_loss)
    plan.sl_points = plan.sl_distance / spec.point
    plan.loss_per_lot = plan.sl_distance / spec.tick_size * spec.tick_value
    if plan.loss_per_lot <= 0:
        plan.reject = "每手止损亏损算得 0，无法反推手数"
        return plan

    total = spec.floor_lot(cfg.risk_amount / plan.loss_per_lot)
    if cfg.max_total_lot > 0:
        total = min(total, spec.floor_lot(cfg.max_total_lot))
    if spec.volume_max > 0:
        total = min(total, spec.floor_lot(spec.volume_max))
    if total < spec.volume_min:
        plan.reject = (
            f"按风险金额 {_trim(cfg.risk_amount, 2)} 与止损距离 "
            f"{_trim(plan.sl_points, 1)} 点算得 {_trim(total, spec.volume_digits)} 手，"
            f"不足最小手数 {_trim(spec.volume_min, spec.volume_digits)}"
        )
        return plan

    plan.total_lot = total
    plan.risk_used = round(total * plan.loss_per_lot, 2)
    tp_distance = plan.sl_distance * cfg.rr_ratio
    plan.take_profit = round(entry_price + sign * tp_distance, spec.digits) if cfg.rr_ratio > 0 else 0.0

    lots = _split_lots(total, cfg, spec)
    plan.gap_price, plan.gap_capped = _effective_gap(
        cfg, len(lots) - 1, plan.sl_distance, tp_distance, spec,
    )
    plan.gap_points = plan.gap_price / spec.point if spec.point > 0 else 0.0

    # 回撤补仓朝不利方向挂，突破加仓朝有利方向挂
    step_sign = -sign if cfg.entry_direction == ENTRY_PULLBACK else sign
    plan.batches = [
        Batch(
            index=i,
            volume=volume,
            trigger_price=(
                0.0 if i == 0
                else round(entry_price + step_sign * plan.gap_price * i, spec.digits)
            ),
            gap_points=0.0 if i == 0 else plan.gap_points * i,
        )
        for i, volume in enumerate(lots)
    ]
    return plan


def anchor_to_fill(plan: EntryPlan, cfg: RiskSizedConfig, fill_price: float,
                   spec: SymbolSpec) -> EntryPlan:
    """底仓实际成交后，把止盈与各批触发价重挂到成交价上；手数保持不变。

    手数是按下单前的预估价算的，成交价会有滑点。手数不能再动（底仓已经成交），
    但止盈与补仓触发价必须以真实成交价为锚，否则整套价位都会偏移一个滑点，
    盈亏比与「触发价留在止损内」的前提也就不再成立。
    """
    if not plan.ok or fill_price <= 0 or fill_price == plan.entry_price:
        return plan
    sign = _favorable_sign(plan.direction)
    plan.entry_price = fill_price
    plan.sl_distance = abs(fill_price - plan.stop_loss)
    plan.sl_points = plan.sl_distance / spec.point if spec.point > 0 else 0.0
    plan.loss_per_lot = (
        plan.sl_distance / spec.tick_size * spec.tick_value if spec.tick_size > 0 else 0.0
    )
    plan.risk_used = round(plan.total_lot * plan.loss_per_lot, 2)
    tp_distance = plan.sl_distance * cfg.rr_ratio
    plan.take_profit = round(fill_price + sign * tp_distance, spec.digits) if cfg.rr_ratio > 0 else 0.0

    plan.gap_price, plan.gap_capped = _effective_gap(
        cfg, plan.add_batch_count, plan.sl_distance, tp_distance, spec,
    )
    plan.gap_points = plan.gap_price / spec.point if spec.point > 0 else 0.0
    step_sign = -sign if cfg.entry_direction == ENTRY_PULLBACK else sign
    for batch in plan.batches:
        if batch.is_base:
            continue
        batch.trigger_price = round(fill_price + step_sign * plan.gap_price * batch.index, spec.digits)
        batch.gap_points = plan.gap_points * batch.index
    return plan


# ---------------------------------------------------------------------------
# 运行期判定
# ---------------------------------------------------------------------------

def reached(plan: EntryPlan, batch: Batch, price: float, *, entry_direction: str) -> bool:
    """当前价是否已经走到该批的触发价。"""
    if batch.is_base or batch.trigger_price <= 0 or price <= 0:
        return False
    toward_up = (entry_direction == ENTRY_BREAKOUT) == _is_buy(plan.direction)
    return price >= batch.trigger_price if toward_up else price <= batch.trigger_price


def next_batch(plan: EntryPlan, cfg: RiskSizedConfig, *, filled: int,
               price: float) -> Optional[Batch]:
    """取下一批待补的仓位；未到价或已补完返回 None。

    filled 是已成交的笔数（含底仓），也就是下一批的 index。
    """
    if not plan.ok or filled <= 0:
        return None
    batch = plan.batch_at(filled)
    if batch is None:
        return None
    return batch if reached(plan, batch, price, entry_direction=cfg.entry_direction) else None


@dataclass
class BreakevenMove:
    """一次保本止损移动，同时携带判定依据。"""
    stop_loss: float
    avg_price: float
    price: float
    favorable: float        # 当前浮盈的价格距离
    threshold: float        # 触发所需的价格距离
    times: float
    sl_distance: float
    position_count: int = 0

    def describe(self, digits: int) -> str:
        return (
            f"保本触发：均价 {_trim(self.avg_price, digits)} → 现价 "
            f"{_trim(self.price, digits)}，有利偏离 {_trim(self.favorable, digits)}"
            f" ≥ 止损距离 {_trim(self.sl_distance, digits)} × {_trim(self.times, 2)}"
            f" = {_trim(self.threshold, digits)}；"
            f"{self.position_count} 笔持仓止损移到均价 {_trim(self.stop_loss, digits)}"
        )

    def detail(self) -> dict:
        return {
            "kind": "breakeven",
            "stop_loss": self.stop_loss,
            "avg_price": self.avg_price,
            "price": self.price,
            "favorable": round(self.favorable, 8),
            "threshold": round(self.threshold, 8),
            "breakeven_times": self.times,
            "sl_distance": round(self.sl_distance, 8),
            "position_count": self.position_count,
        }


def weighted_avg_price(positions: list[dict]) -> float:
    """持仓的加权平均开仓价——保本止损要挪到这里，而不是首单价。"""
    total = 0.0
    weighted = 0.0
    for p in positions or []:
        volume = _as_float(p.get("volume"))
        price = _as_float(p.get("price_open"))
        if volume <= 0 or price <= 0:
            continue
        total += volume
        weighted += volume * price
    return weighted / total if total > 0 else 0.0


def breakeven_move(cfg: RiskSizedConfig, plan: EntryPlan, *, positions: list[dict],
                   price: float, digits: int) -> Optional[BreakevenMove]:
    """浮盈是否已达标、该把止损挪到哪里；未达标返回 None。"""
    if not cfg.breakeven_enabled or cfg.breakeven_times <= 0:
        return None
    if plan.sl_distance <= 0 or price <= 0:
        return None
    avg = weighted_avg_price(positions)
    if avg <= 0:
        return None
    favorable = (price - avg) * _favorable_sign(plan.direction)
    threshold = plan.sl_distance * cfg.breakeven_times
    if favorable < threshold:
        return None
    return BreakevenMove(
        stop_loss=round(avg, digits),
        avg_price=avg,
        price=price,
        favorable=favorable,
        threshold=threshold,
        times=cfg.breakeven_times,
        sl_distance=plan.sl_distance,
        position_count=len(positions or []),
    )


# ---------------------------------------------------------------------------
# 开单原因与计算规则的说明生成
#
# 以损定量的手数是节点算出来的，后台看不到中间过程。这里把计划与每批的判定还原成
# 人读的说明、结构化明细，以及能塞进 MT5 备注的紧凑编码，随上报一起送回后台。
# ---------------------------------------------------------------------------

def describe_plan(plan: EntryPlan, cfg: RiskSizedConfig, spec: SymbolSpec) -> str:
    """建仓计划的一句话说明：手数是怎么反推出来的、仓位怎么分批。"""
    vd = spec.volume_digits
    parts = [
        f"以损定量：风险金额 {_trim(cfg.risk_amount, 2)} ÷ 每手止损亏损 "
        f"{_trim(plan.loss_per_lot, 2)}（止损距离 {_trim(plan.sl_points, 1)} 点）"
        f" = 总手数 {_trim(plan.total_lot, vd)}，实际风险 {_trim(plan.risk_used, 2)}",
        f"底仓 {_trim(cfg.base_ratio, 1)}% = {_trim(plan.base_volume, vd)} 手市价成交",
    ]
    if plan.add_batch_count:
        parts.append(
            f"剩余分 {plan.add_batch_count} 批{cfg.entry_direction_label}，间距 "
            f"{_trim(plan.gap_points, 1)} 点" + ("（已压缩至止损内）" if plan.gap_capped else "")
        )
    else:
        parts.append("不分批补仓")
    parts.append(
        f"共用止损 {_trim(plan.stop_loss, spec.digits)}，止盈 "
        + (f"{_trim(plan.take_profit, spec.digits)}（盈亏比 {_trim(cfg.rr_ratio, 2)}）"
           if plan.take_profit else "不设")
    )
    return "；".join(parts)


def plan_detail(plan: EntryPlan, cfg: RiskSizedConfig, spec: SymbolSpec) -> dict:
    """建仓计划的结构化明细，供后台逐项展示计算参数。"""
    return {
        "kind": "risk_sized_plan",
        "direction": plan.direction,
        "entry_price": plan.entry_price,
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit or None,
        "risk_amount": cfg.risk_amount,
        "risk_used": plan.risk_used,
        "rr_ratio": cfg.rr_ratio,
        "sl_distance": round(plan.sl_distance, 8),
        "sl_points": round(plan.sl_points, 2),
        "loss_per_lot": round(plan.loss_per_lot, 4),
        "total_lot": plan.total_lot,
        "lot_formula": (
            f"{_trim(cfg.risk_amount, 2)} ÷ {_trim(plan.loss_per_lot, 2)}"
            f" = {_trim(plan.total_lot, spec.volume_digits)}"
        ),
        "base_ratio": cfg.base_ratio,
        "base_volume": plan.base_volume,
        "add_batches": plan.add_batch_count,
        "entry_direction": cfg.entry_direction,
        "entry_direction_label": cfg.entry_direction_label,
        "gap_points": round(plan.gap_points, 2),
        "gap_capped": plan.gap_capped,
        "breakeven_enabled": cfg.breakeven_enabled,
        "breakeven_times": cfg.breakeven_times if cfg.breakeven_enabled else None,
        "tick_value": spec.tick_value,
        "tick_size": spec.tick_size,
        "volume_step": spec.volume_step,
        "batches": [
            {
                "index": b.index,
                "volume": b.volume,
                "trigger_price": b.trigger_price or None,
                "gap_points": round(b.gap_points, 2) or None,
            }
            for b in plan.batches
        ],
    }


def describe_batch(plan: EntryPlan, cfg: RiskSizedConfig, batch: Batch,
                   spec: SymbolSpec, price: float) -> str:
    """一笔补仓的开单原因。"""
    return (
        f"以损定量补仓 · 第 {batch.index + 1}/{len(plan.batches)} 批："
        f"{cfg.entry_direction_label} 触发价 {_trim(batch.trigger_price, spec.digits)}"
        f"（首单 {_trim(plan.entry_price, spec.digits)} 偏离 "
        f"{_trim(batch.gap_points, 1)} 点）已到价，现价 {_trim(price, spec.digits)}；"
        f"本批 {_trim(batch.volume, spec.volume_digits)} 手，"
        f"共用止损 {_trim(plan.stop_loss, spec.digits)}，止盈 "
        + (f"{_trim(plan.take_profit, spec.digits)}" if plan.take_profit else "不设")
        + f"；总手数 {_trim(plan.total_lot, spec.volume_digits)}，"
        f"风险仍为 {_trim(plan.risk_used, 2)}"
    )


def batch_detail(plan: EntryPlan, cfg: RiskSizedConfig, batch: Batch,
                 spec: SymbolSpec, price: float) -> dict:
    """一笔补仓的结构化明细。"""
    return {
        "kind": "risk_sized_add",
        "batch_index": batch.index,
        "batch_total": len(plan.batches),
        "direction": plan.direction,
        "entry_direction": cfg.entry_direction,
        "entry_direction_label": cfg.entry_direction_label,
        "entry_price": plan.entry_price,
        "trigger_price": batch.trigger_price,
        "price": price,
        "gap_points": round(batch.gap_points, 2),
        "volume": batch.volume,
        "total_lot": plan.total_lot,
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit or None,
        "risk_amount": cfg.risk_amount,
        "risk_used": plan.risk_used,
    }


def batch_comment(batch: Batch) -> str:
    """补仓单写进 MT5 的紧凑备注：R3=以损定量，B=批次序号。

    MT5 备注只有约 31 字符，这里只保留能反查到批次的最小信息。
    """
    return f"R{RULE_TYPE_RISK_SIZED}B{batch.index}"[:MT5_COMMENT_LIMIT]
