"""网格交易的纯计算逻辑（无 I/O，可单独单元测试）。

对应后台「策略模版3」，规则字段随 strategy_start 的策略快照下发，实现手动
创建网格的行为：

- price_lower / price_upper：价格区间
- grid_count：网格数量（切成 N 格，N+1 条线）
- grid_mode：arithmetic 等差 / geometric 等比
- grid_side：long 只做多 / short 只做空（历史 follow 仍可解析，按信号定方向）
- lot_per_grid：每格手数
- total_lot_limit：总手数上限，0=不额外限制
- trigger_price：触发价，0=立即启动
- stop_lower / stop_upper：下沿 / 上沿终止价，0=不设
  （多头：下沿=止损、上沿=止盈；空头相反）
- close_on_stop：终止时是否清仓
- trailing_up / trailing_max：向上追踪开关与最大平移格数

核心循环（挂单网格）：每个格位在**入场价**上挂一张限价单等成交，成交的同时由
券商端止盈在**出场价**上自动平仓，券商端止损在**终止价**上自动止损。入场、出场、
止损都不在节点侧抢时间，所以网格只吃「挂单价 → 出场价」这一段差价。空仓是正常
运行态，任务不会因持仓归零而结束。

挂单口径：多头在 levels[i] 挂 BUY LIMIT（买线下方），出场价 levels[i+1]；
空头在 levels[i+1] 挂 SELL LIMIT（卖线上方），出场价 levels[i]。
入场价必须落在成交侧——挂在错误一侧会被券商直接拒单，所以只有「买线在现价
下方」的格位才会被挂上单。

终止价（多头 stop_lower、空头 stop_upper）是**整网**的止损，而入场价逐格不同：
离终止价最近的几格可能贴着它，券商按 stops_level 拒（130 invalid stops）。
所以每格单独校验，贴不上的那几格只带止盈。终止价随网格一起平移，已成交的持仓
要跟着改单，否则还守着旧止损价。

开启向上追踪后，价格越过区间外沿不会停机，而是把整个网格连同止损止盈平移一格
（多头追涨、空头追跌）。平移会丢弃最外侧一格，因此 MT5 备注里记的是**绝对格位
号**（以初始区间为基准，不随平移变化），断线恢复时减去平移量即可还原当前格位。

手数与价位计算需要品种规格（digits / volume_step），只有 MT5 能给，所以这一层
放在节点侧；服务端只负责下发参数，与以损定量的分工一致。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# 规则 type，与后台 strategy_templates.RULE_TYPE_GRID 对齐
RULE_TYPE_GRID = 4

GRID_MODE_ARITHMETIC = "arithmetic"
GRID_MODE_GEOMETRIC = "geometric"
GRID_MODES = (GRID_MODE_ARITHMETIC, GRID_MODE_GEOMETRIC)

GRID_SIDE_LONG = "long"
GRID_SIDE_SHORT = "short"
GRID_SIDE_FOLLOW = "follow"
GRID_SIDES = (GRID_SIDE_LONG, GRID_SIDE_SHORT, GRID_SIDE_FOLLOW)

# 网格入场挂单类型：等价格回到格线再成交，所以一律是限价单。
# 与 mt5_client.PENDING_LIMIT 同名同义，但这一层不依赖 MT5 包（可独立单测）。
PENDING_LIMIT = "limit"

# 单次 trailing_shift 调用的平移上限。只为兜住极端行情下的长循环——正常追踪一次
# 只移几格，断线恢复时则要一次追回全部平移量，所以取值必须远大于网格格数。
_MAX_SHIFT_PER_CALL = 1000

GRID_MODE_LABEL = {
    GRID_MODE_ARITHMETIC: "等差",
    GRID_MODE_GEOMETRIC: "等比",
}
GRID_SIDE_LABEL = {
    GRID_SIDE_LONG: "只做多",
    GRID_SIDE_SHORT: "只做空",
    GRID_SIDE_FOLLOW: "跟随信号",
}

# MT5 订单备注上限约 31 字符
MT5_COMMENT_LIMIT = 31
# 备注编码：G4L{index} —— G=网格，4=规则 type，L=格位，后接绝对格位号。
# 绝对格位号可能因向下追踪变成负数，故允许前导负号。
_COMMENT_RE = re.compile(r"^G4L(-?\d+)$")


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
    """按位数格式化并去掉尾随零。"""
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
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0
    # 挂单价 / 止损价距现价的最小点数（券商要求），0 = 不限制
    stops_level: int = 0

    @property
    def volume_digits(self) -> int:
        step = self.volume_step
        if step <= 0:
            return 2
        digits = 0
        while digits < 8 and abs(step * (10 ** digits) - round(step * (10 ** digits))) > 1e-9:
            digits += 1
        return digits

    def usable(self) -> bool:
        return self.volume_step > 0 and self.volume_min > 0

    def floor_lot(self, lot: float) -> float:
        if self.volume_step <= 0:
            return round(max(lot, 0.0), 2)
        steps = math.floor((max(lot, 0.0) + 1e-9) / self.volume_step)
        return round(steps * self.volume_step, self.volume_digits)


@dataclass
class GridConfig:
    """一条网格交易规则的解析结果。"""
    price_lower: float = 0.0
    price_upper: float = 0.0
    grid_count: int = 10
    grid_mode: str = GRID_MODE_ARITHMETIC
    grid_side: str = GRID_SIDE_LONG
    lot_per_grid: float = 0.01
    total_lot_limit: float = 0.0
    trigger_price: float = 0.0
    stop_lower: float = 0.0
    stop_upper: float = 0.0
    close_on_stop: bool = True
    trailing_up: bool = False
    trailing_max: int = 0

    @classmethod
    def from_rule(cls, rule: dict) -> "GridConfig":
        grid_mode = str(rule.get("grid_mode") or GRID_MODE_ARITHMETIC).strip().lower()
        if grid_mode not in GRID_MODES:
            grid_mode = GRID_MODE_ARITHMETIC
        grid_side = str(rule.get("grid_side") or GRID_SIDE_LONG).strip().lower()
        if grid_side not in GRID_SIDES:
            grid_side = GRID_SIDE_LONG
        count = max(2, min(200, _as_int(rule.get("grid_count"), 10)))
        return cls(
            price_lower=max(0.0, _as_float(rule.get("price_lower"))),
            price_upper=max(0.0, _as_float(rule.get("price_upper"))),
            grid_count=count,
            grid_mode=grid_mode,
            grid_side=grid_side,
            lot_per_grid=max(0.0, _as_float(rule.get("lot_per_grid"), 0.01)),
            total_lot_limit=max(0.0, _as_float(rule.get("total_lot_limit"))),
            trigger_price=max(0.0, _as_float(rule.get("trigger_price"))),
            stop_lower=max(0.0, _as_float(rule.get("stop_lower"))),
            stop_upper=max(0.0, _as_float(rule.get("stop_upper"))),
            close_on_stop=bool(rule.get("close_on_stop", True)),
            trailing_up=bool(rule.get("trailing_up", False)),
            trailing_max=max(0, _as_int(rule.get("trailing_max"))),
        )

    @property
    def grid_mode_label(self) -> str:
        return GRID_MODE_LABEL.get(self.grid_mode, self.grid_mode)

    @property
    def grid_side_label(self) -> str:
        return GRID_SIDE_LABEL.get(self.grid_side, self.grid_side)


def pick_grid_rule(rules: object) -> Optional[dict]:
    """从策略快照里取出启用中的网格交易规则；没有则返回 None。"""
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _as_int(rule.get("type")) != RULE_TYPE_GRID:
            continue
        if _as_int(rule.get("status")):
            return rule
    return None


def resolve_side(cfg: GridConfig, signal_action: object) -> str:
    """解析实际交易方向：BUY（多头网格）或 SELL（空头网格）。"""
    if cfg.grid_side == GRID_SIDE_LONG:
        return "BUY"
    if cfg.grid_side == GRID_SIDE_SHORT:
        return "SELL"
    action = str(signal_action or "BUY").strip().upper()
    return "SELL" if action == "SELL" else "BUY"


# ---------------------------------------------------------------------------
# 网格线与建仓计划
# ---------------------------------------------------------------------------

def build_levels(cfg: GridConfig, *, digits: int = 5) -> list[float]:
    """按等差 / 等比切出 grid_count+1 条网格线（含上下限），按价格升序。"""
    lower, upper, n = cfg.price_lower, cfg.price_upper, cfg.grid_count
    if lower <= 0 or upper <= lower or n < 1:
        return []
    levels: list[float] = []
    if cfg.grid_mode == GRID_MODE_GEOMETRIC:
        # 等比：lower * (upper/lower)^(i/n)
        ratio = (upper / lower) ** (1.0 / n)
        for i in range(n + 1):
            levels.append(round(lower * (ratio ** i), digits))
    else:
        step = (upper - lower) / n
        for i in range(n + 1):
            levels.append(round(lower + step * i, digits))
    # 强制首尾精确等于配置值，避免浮点漂移
    levels[0] = round(lower, digits)
    levels[-1] = round(upper, digits)
    return levels


@dataclass
class Crossing:
    """一次网格线穿越。"""
    level_index: int        # 被穿越的网格线下标（0=下限 … N=上限）
    direction: str          # "up" | "down"
    level_price: float


@dataclass
class GridPlan:
    """一次网格任务的完整计划。reject 非空表示不可执行。"""
    side: str = "BUY"                       # BUY=多头网格 / SELL=空头网格
    levels: list[float] = field(default_factory=list)
    lot_per_grid: float = 0.0
    # 格位 index → MT5 ticket；格位 i 对应「在 levels[i] 买入、在 levels[i+1] 卖出」
    # 即持有格位 i 意味着已在第 i 条线买入，等待涨到第 i+1 条线卖出
    holdings: dict[int, int] = field(default_factory=dict)
    # 格位 index → 在途挂单票号（尚未成交的入场单）
    pending: dict[int, int] = field(default_factory=dict)
    prev_price: float = 0.0
    triggered: bool = False                 # 是否已过触发价 / 无需触发
    reject: str = ""
    # 向上追踪的运行态：止损止盈随网格一起平移，所以判定要读这里而不是 cfg
    stop_lower: float = 0.0
    stop_upper: float = 0.0
    # 累计平移格数，上移为正、下移为负；绝对格位 = 相对格位 + shift_count
    shift_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.reject and len(self.levels) >= 3

    @property
    def grid_count(self) -> int:
        return max(0, len(self.levels) - 1)

    @property
    def is_long(self) -> bool:
        return self.side == "BUY"

    def holding_count(self) -> int:
        return len(self.holdings)

    def pending_count(self) -> int:
        return len(self.pending)

    def absolute_index(self, level: int) -> int:
        """相对格位 → 绝对格位。写进 MT5 备注，平移后仍能反查回原格。"""
        return int(level) + self.shift_count

    def relative_index(self, absolute: int) -> Optional[int]:
        """绝对格位 → 当前网格的相对格位；已被平移挤出网格返回 None。"""
        level = int(absolute) - self.shift_count
        return level if 0 <= level < self.grid_count else None

    def level_of(self, ticket: int) -> Optional[int]:
        """持仓票号 → 当前格位下标；不在持仓映射里返回 None。"""
        target = int(ticket)
        for level, held in self.holdings.items():
            if int(held) == target:
                return int(level)
        return None


def entry_price(plan: GridPlan, level: int) -> float:
    """格位 level 的**入场价**（挂单价）。

    多头在 levels[level] 买入，空头在 levels[level+1] 开空。挂单必须落在成交侧：
    多头的 BUY LIMIT 挂在现价下方，空头的 SELL LIMIT 挂在现价上方。
    """
    if not plan.ok or level < 0 or level >= plan.grid_count:
        return 0.0
    return plan.levels[level] if plan.is_long else plan.levels[level + 1]


def exit_price(plan: GridPlan, level: int) -> float:
    """格位 level 的**出场价**（券商端止盈价）。

    多头在 levels[level+1] 止盈，空头在 levels[level] 止盈。出场价恒在入场价的
    盈利侧：一格吃的就是这两条线之间的差价。
    """
    if not plan.ok or level < 0 or level >= plan.grid_count:
        return 0.0
    return plan.levels[level + 1] if plan.is_long else plan.levels[level]


def stop_price(plan: GridPlan) -> float:
    """网格的**终止价**（券商端止损价），0 表示不设。

    多头：跌破 stop_lower 为止损 → 止损价就是 stop_lower；
    空头：涨破 stop_upper 为止损 → 止损价就是 stop_upper。

    这个价在挂单时就已确定（配置项），所以可以直接写进挂单的止损：成交即带着
    止损，节点断线期间也有人管。开了向上追踪时它会随网格平移，见 `apply_shift`。
    """
    if not plan.ok:
        return 0.0
    return plan.stop_lower if plan.is_long else plan.stop_upper


def pending_kind_for(plan: GridPlan) -> str:
    """挂单类型：网格入场一律用限价单（等价格回到格线再成交）。"""
    return PENDING_LIMIT


def pending_action(plan: GridPlan) -> str:
    """挂单方向：与网格方向一致（多头 BUY LIMIT、空头 SELL LIMIT）。"""
    return plan.side


def placeable_levels(plan: GridPlan, price: float) -> list[int]:
    """当前可以挂单的格位下标（入场价落在现价的正确一侧）。

    多头的 BUY LIMIT 必须挂在现价**下方**，空头的 SELL LIMIT 必须挂在现价**上方**，
    挂在错误一侧会被券商直接拒单（10015 价格非法）。所以：

    - 多头：入场价 levels[i] < 现价 → 可挂；
    - 空头：入场价 levels[i+1] > 现价 → 可挂。

    现价两侧都会有一半格位不可挂，这是网格的正常形态——另一半等价格走过去
    之后再由下一轮补挂。
    """
    if not plan.ok or price <= 0:
        return []
    out: list[int] = []
    for i in range(plan.grid_count):
        ep = entry_price(plan, i)
        if ep <= 0:
            continue
        if plan.is_long:
            if ep < price:
                out.append(i)
        else:
            if ep > price:
                out.append(i)
    return out


def capped_levels(plan: GridPlan, cfg: GridConfig, indices: list[int]) -> list[int]:
    """按 total_lot_limit 截断可挂格数。

    与「在途挂单 + 已成交持仓」共同占用总手数额度，所以截断依据是两者之和。
    """
    if not indices or cfg.total_lot_limit <= 0 or plan.lot_per_grid <= 0:
        return indices
    used = (plan.holding_count() + plan.pending_count()) * plan.lot_per_grid
    budget = cfg.total_lot_limit - used
    max_grids = int(math.floor((budget + 1e-9) / plan.lot_per_grid))
    if max_grids <= 0:
        return []
    if len(indices) <= max_grids:
        return indices
    # 优先保留离现价最近的格（最先能成交的那几格）：
    # 多头的入场价随下标递增，取列表头部；空头同样递增，取尾部
    if plan.is_long:
        return indices[:max_grids]
    return indices[-max_grids:]


def can_place_more(plan: GridPlan, cfg: GridConfig) -> bool:
    """是否还能再挂一格（受 total_lot_limit 约束）。"""
    if cfg.total_lot_limit <= 0 or plan.lot_per_grid <= 0:
        return True
    used = (plan.holding_count() + plan.pending_count()) * plan.lot_per_grid
    return used + plan.lot_per_grid <= cfg.total_lot_limit + 1e-9


def tp_is_sendable(plan: GridPlan, level: int, *, stops_level: int = 0,
                   point: float = 0.0) -> bool:
    """这一格的出场价能不能写到入场挂单上。

    券商拒掉的是**整张单**（error 130 invalid stops），所以条件不满足就不能把
    止盈写进挂单——宁可不挂这一格，也不能让挂单挂不上去。

    条件：
    - 方向对：多头的出场价要高于入场价，空头的要低于；
    - 离入场价足够远：不小于券商的 stops_level。
    """
    ep = entry_price(plan, level)
    xp = exit_price(plan, level)
    if ep <= 0 or xp <= 0:
        return False
    if plan.is_long:
        if xp <= ep:
            return False
    else:
        if xp >= ep:
            return False
    level_pts = max(0, int(stops_level))
    step = float(point or 0.0)
    if level_pts > 0 and step > 0 and abs(ep - xp) < level_pts * step:
        return False
    return True


def sl_is_sendable(plan: GridPlan, level: int, *, stops_level: int = 0,
                   point: float = 0.0) -> bool:
    """这一格的终止价能不能写到入场挂单的止损上。

    与 `tp_is_sendable` 同理：券商拒的是**整张单**（error 130 invalid stops），
    止损不合法就不能写，宁可少写一个保护，也不能让挂单挂不上去。

    条件：
    - 终止价已配置（stop_price > 0）；
    - 方向对：多头的终止价要低于入场价，空头的要高于；
    - 离入场价足够远：不小于券商的 stops_level。

    注意终止价是**整网**的止损，而入场价逐格不同：最靠近终止价的那几格可能
    离得不够远，那些格就写不上止损——只影响那几格，不影响其余格。
    """
    return stop_is_sendable(
        plan, entry_price(plan, level), stops_level=stops_level, point=point,
    )


def stop_is_sendable(plan: GridPlan, reference: float, *, stops_level: int = 0,
                     point: float = 0.0) -> bool:
    """网格终止价相对某个参考价能不能作为止损下发。

    reference 是价格的下限判断基准：
    - 挂单阶段用**入场价**（止损必须落在入场价的反方向）；
    - 改单阶段用**现价**（券商校验持仓止损时看的是现价那一侧）。

    两处的方向要求一致：多头的终止价要低于参考价，空头的要高。
    """
    ref = float(reference or 0.0)
    sp = stop_price(plan)
    if ref <= 0 or sp <= 0:
        return False
    if plan.is_long:
        if sp >= ref:
            return False
    else:
        if sp <= ref:
            return False
    level_pts = max(0, int(stops_level))
    step = float(point or 0.0)
    if level_pts > 0 and step > 0 and abs(ref - sp) < level_pts * step:
        return False
    return True


def plan_grid(cfg: GridConfig, *, signal_action: object,
              spec: SymbolSpec) -> GridPlan:
    """校验参数并切出网格线；手数按品种步长取整。"""
    plan = GridPlan(side=resolve_side(cfg, signal_action))
    if cfg.price_lower <= 0 or cfg.price_upper <= 0:
        plan.reject = "网格区间上下限需大于 0"
        return plan
    if cfg.price_upper <= cfg.price_lower:
        plan.reject = "网格区间上限须大于下限"
        return plan
    if cfg.grid_mode == GRID_MODE_GEOMETRIC and cfg.price_lower <= 0:
        plan.reject = "等比网格要求区间下限大于 0"
        return plan
    if cfg.lot_per_grid <= 0:
        plan.reject = "每格手数需大于 0"
        return plan
    if not spec.usable():
        plan.reject = "品种手数规格不完整（volume_min / volume_step），无法下单"
        return plan

    lot = spec.floor_lot(cfg.lot_per_grid)
    if lot < spec.volume_min:
        plan.reject = (
            f"每格手数 {_trim(cfg.lot_per_grid, spec.volume_digits)} 不足最小手数 "
            f"{_trim(spec.volume_min, spec.volume_digits)}"
        )
        return plan
    if spec.volume_max > 0 and lot > spec.volume_max:
        lot = spec.floor_lot(spec.volume_max)
    if cfg.total_lot_limit > 0 and cfg.total_lot_limit + 1e-9 < lot:
        # 一格都开不出来：放行只会让任务空跑到底，白占节点互斥占位
        plan.reject = (
            f"总手数上限 {_trim(cfg.total_lot_limit, spec.volume_digits)} 小于单格手数 "
            f"{_trim(lot, spec.volume_digits)}，网格无法开仓"
        )
        return plan

    levels = build_levels(cfg, digits=spec.digits)
    if len(levels) < 3:
        plan.reject = "网格数量过少，无法切出有效格位"
        return plan
    if any(levels[i + 1] <= levels[i] for i in range(len(levels) - 1)):
        # 区间太窄 / 格数太多时，四舍五入会把多条线压到同一个价位，
        # 一次穿越就会连开好几格
        plan.reject = (
            f"网格线在 {spec.digits} 位报价精度下无法区分（相邻格位价格相同），"
            "请调宽区间或减少格数"
        )
        return plan

    plan.levels = levels
    plan.lot_per_grid = lot
    plan.triggered = cfg.trigger_price <= 0
    plan.stop_lower = cfg.stop_lower
    plan.stop_upper = cfg.stop_upper
    return plan


def trigger_reached(cfg: GridConfig, price: float, prev_price: float = 0.0) -> bool:
    """当前价是否已触及触发价（未配置触发价视为已触发）。

    「触及」与方向无关：首个报价只用来确定现价在触发价的哪一侧，之后价格从这一侧
    穿越 / 落到触发价才算到价。因此触发价在现价下方时等回落触及，在上方时等上涨
    触及——空头网格与「等回调再启动」的配置都能按字面语义工作。
    """
    if cfg.trigger_price <= 0:
        return True
    if price <= 0:
        return False
    t = cfg.trigger_price
    if prev_price <= 0:
        # 首个报价：正好落在触发价上才算到价，否则只记下所处一侧
        return price == t
    # 穿越或落到触发价：两端乘积 ≤ 0
    return (prev_price - t) * (price - t) <= 0


# ---------------------------------------------------------------------------
# 运行期判定
# ---------------------------------------------------------------------------

def crossings(plan: GridPlan, prev_price: float, price: float) -> list[Crossing]:
    """用上一次价格与本次价格判断穿越了哪些网格线及方向。

    挂单网格不再靠「穿越」下单：入场单在挂出的那一刻就躺在盘上，价格碰到
    挂单价由终端撮合成交。这个函数保留给「网格平移」的判定与断线恢复时的
    价位还原使用——平移要看价格是否越过区间外沿，那仍然是穿越语义。

    同价位不重复触发：只有 prev 与 price 分居线的两侧才算穿越。
    返回按穿越顺序排列（下跌时从高到低，上涨时从低到高）。
    """
    if not plan.ok or prev_price <= 0 or price <= 0 or prev_price == price:
        return []
    out: list[Crossing] = []
    going_up = price > prev_price
    lo, hi = (prev_price, price) if going_up else (price, prev_price)
    # 收集被穿越的线
    hit: list[tuple[int, float]] = []
    for i, level in enumerate(plan.levels):
        # 严格落在 (lo, hi] 或 [lo, hi) —— 用开闭区间避免边界重复
        # 上涨：prev < level <= price；下跌：price <= level < prev
        if going_up:
            if prev_price < level <= price:
                hit.append((i, level))
        else:
            if price <= level < prev_price:
                hit.append((i, level))
    if going_up:
        hit.sort(key=lambda x: x[0])          # 从低到高
        direction = "up"
    else:
        hit.sort(key=lambda x: -x[0])         # 从高到低
        direction = "down"
    for idx, level_price in hit:
        out.append(Crossing(level_index=idx, direction=direction, level_price=level_price))
    return out


def can_buy_more(plan: GridPlan, cfg: GridConfig) -> bool:
    """是否还能再买一格（受 total_lot_limit 约束）。"""
    if cfg.total_lot_limit <= 0 or plan.lot_per_grid <= 0:
        return True
    used = (plan.holding_count() + plan.pending_count()) * plan.lot_per_grid
    return used + plan.lot_per_grid <= cfg.total_lot_limit + 1e-9


def terminate_reason(plan: GridPlan, price: float) -> Optional[str]:
    """当前价是否触发止损 / 止盈；未触发返回 None。

    读 plan 上的止损止盈而不是 cfg：开了向上追踪时它们会随网格一起平移。
    多头：跌破 stop_lower 为止损、涨破 stop_upper 为止盈；
    空头：涨破 stop_upper 为止损、跌破 stop_lower 为止盈。
    """
    if price <= 0 or not plan.ok:
        return None
    if plan.is_long:
        if plan.stop_lower > 0 and price <= plan.stop_lower:
            return f"触发止损价 {plan.stop_lower}"
        if plan.stop_upper > 0 and price >= plan.stop_upper:
            return f"触发止盈价 {plan.stop_upper}"
    else:
        if plan.stop_upper > 0 and price >= plan.stop_upper:
            return f"触发止损价 {plan.stop_upper}"
        if plan.stop_lower > 0 and price <= plan.stop_lower:
            return f"触发止盈价 {plan.stop_lower}"
    return None


# ---------------------------------------------------------------------------
# 向上追踪
# ---------------------------------------------------------------------------

@dataclass
class TrailingShift:
    """一次网格平移的计算结果，应用前调用方需先兑现 dropped 里的持仓。"""
    steps: int                                          # 平移格数，正=上移 / 负=下移
    levels: list[float] = field(default_factory=list)
    stop_lower: float = 0.0
    stop_upper: float = 0.0
    holdings: dict[int, int] = field(default_factory=dict)      # 重映射后的格位→ticket
    dropped: list[tuple[int, int]] = field(default_factory=list)  # 被挤出网格的 (原格位, ticket)
    # 平移后格位号要跟着改，入场挂单也要跟着改价。
    # 挂在盘上的单改价后可能落到现价的错误一侧（券商会拒），所以这里只报出
    # 「哪些单要撤、哪些格位要重挂」，由调用方撤单后按新格线重新挂。
    pending: dict[int, int] = field(default_factory=dict)
    dropped_pending: list[tuple[int, int]] = field(default_factory=list)
    from_lower: float = 0.0                             # 平移前的区间，用于说明
    from_upper: float = 0.0

    @property
    def direction(self) -> str:
        return "up" if self.steps > 0 else "down"


def _grid_spacing(levels: list[float]) -> tuple[float, float]:
    """从现有网格线反推间距：(等差步长, 等比公比)。"""
    n = len(levels) - 1
    if n < 1 or levels[0] <= 0:
        return 0.0, 1.0
    step = (levels[-1] - levels[0]) / n
    ratio = (levels[-1] / levels[0]) ** (1.0 / n)
    return step, ratio


def _shift_price(price: float, *, geometric: bool, step: float, ratio: float,
                 up: bool, digits: int) -> float:
    """把单个价位按网格间距平移一格；0（未设置）保持为 0。"""
    if price <= 0:
        return 0.0
    if geometric:
        moved = price * ratio if up else price / ratio
    else:
        moved = price + step if up else price - step
    return round(max(moved, 0.0), digits)


def trailing_shift(plan: GridPlan, cfg: GridConfig, price: float,
                   *, digits: int = 5) -> Optional[TrailingShift]:
    """价格越过区间外沿时把网格连同止损止盈整体平移；无需平移返回 None。

    多头追涨（突破上限后上移）、空头追跌（跌破下限后下移）：网格不停机，
    而是滚动到新区间继续吃差价，止损止盈同步跟随。

    一次事件可能跳过多格，这里循环平移到区间重新覆盖现价为止；受 trailing_max
    与 _MAX_SHIFT_PER_CALL 双重约束，避免极端行情下无限平移。
    """
    if not cfg.trailing_up or not plan.ok or price <= 0:
        return None

    budget = _MAX_SHIFT_PER_CALL
    if cfg.trailing_max > 0:
        budget = min(budget, cfg.trailing_max - abs(plan.shift_count))
    if budget <= 0:
        return None

    geometric = cfg.grid_mode == GRID_MODE_GEOMETRIC
    levels = list(plan.levels)
    stop_lower, stop_upper = plan.stop_lower, plan.stop_upper
    up = plan.is_long
    steps = 0
    while steps < budget:
        if up:
            if price <= levels[-1]:
                break
        elif price >= levels[0]:
            break
        step, ratio = _grid_spacing(levels)
        # 只校验本模式真正用到的那个量：等差网格的公比可能因浮点精度贴近 1，
        # 一并校验会把间距很小的高价品种误判成无法平移
        if ratio <= 1.0 if geometric else step <= 0:
            break
        nxt = _shift_price(
            levels[-1] if up else levels[0],
            geometric=geometric, step=step, ratio=ratio, up=up, digits=digits,
        )
        if nxt <= 0 or (up and nxt <= levels[-1]) or (not up and nxt >= levels[0]):
            break
        levels = levels[1:] + [nxt] if up else [nxt] + levels[:-1]
        stop_lower = _shift_price(
            stop_lower, geometric=geometric, step=step, ratio=ratio, up=up, digits=digits,
        )
        stop_upper = _shift_price(
            stop_upper, geometric=geometric, step=step, ratio=ratio, up=up, digits=digits,
        )
        steps += 1

    if steps == 0:
        return None

    signed = steps if up else -steps
    holdings: dict[int, int] = {}
    dropped: list[tuple[int, int]] = []
    for level, ticket in sorted(plan.holdings.items()):
        moved = level - signed
        if 0 <= moved < len(levels) - 1:
            holdings[moved] = ticket
        else:
            dropped.append((level, ticket))
    # 在途挂单同样要跟着平移重挂：被挤出网格的单必须撤掉
    pending: dict[int, int] = {}
    dropped_pending: list[tuple[int, int]] = []
    for level, ticket in sorted(plan.pending.items()):
        moved = level - signed
        if 0 <= moved < len(levels) - 1:
            pending[moved] = ticket
        else:
            dropped_pending.append((level, ticket))
    return TrailingShift(
        steps=signed, levels=levels, stop_lower=stop_lower, stop_upper=stop_upper,
        holdings=holdings, dropped=dropped,
        pending=pending, dropped_pending=dropped_pending,
        from_lower=plan.levels[0], from_upper=plan.levels[-1],
    )


def apply_shift(plan: GridPlan, shift: TrailingShift) -> None:
    """把平移结果写回计划。dropped 的持仓须由调用方先行平掉。

    在途挂单在平移后**不能原地留着**：挂单价还是旧格线的价，而出场价已经跟着
    平移了，两者对不上。所以调用方要先撤掉这些单，再按新格线重挂——本函数
    因此把 pending 清空，让调用方的补挂流程重新挂一遍。
    """
    plan.levels = list(shift.levels)
    plan.stop_lower = shift.stop_lower
    plan.stop_upper = shift.stop_upper
    plan.holdings = dict(shift.holdings)
    plan.pending.clear()
    plan.shift_count += shift.steps


# ---------------------------------------------------------------------------
# 备注编解码（断线恢复用）
# ---------------------------------------------------------------------------

def grid_comment(absolute_index: int) -> str:
    """格位单写进 MT5 的紧凑备注：G4L{绝对格位号}。

    存绝对格位而非当前格位——MT5 改不了已成交订单的备注，网格平移后只有绝对
    格位才对得上原来那一格。
    """
    return f"G4L{int(absolute_index)}"[:MT5_COMMENT_LIMIT]


def parse_grid_comment(comment: object) -> Optional[int]:
    """从 MT5 备注解析绝对格位号；解析失败返回 None。"""
    text = str(comment or "").strip()
    m = _COMMENT_RE.match(text)
    if not m:
        return None
    return int(m.group(1))


def nearest_level_index(plan: GridPlan, price: float) -> Optional[int]:
    """按开仓价就近匹配格位（comment 解析失败时的兜底）。"""
    if not plan.ok or price <= 0:
        return None
    best_i, best_dist = None, float("inf")
    for i in range(plan.grid_count):
        dist = abs(plan.levels[i] - price)
        if dist < best_dist:
            best_dist, best_i = dist, i
    return best_i


def rebuild_holdings(plan: GridPlan, positions: list[dict],
                     orders: Optional[list[dict]] = None) -> list[int]:
    """按持仓 comment（失败则按开仓价就近）还原格位→ticket 映射。

    备注里存的是绝对格位号，网格平移过就要减掉 shift_count 才是当前格位。返回
    「备注可识别、但格位已被平移挤出网格」的 ticket 列表：这些持仓的卖出线已不
    在网格内，需要调用方平掉兑现，否则会一直挂到任务终止。

    orders 是该魔术号下的在途挂单：挂单成交后备注会继承到持仓上，所以挂单
    也带同一套 G4L{绝对格位号} 备注，据此还原「格位 → 在途单票号」的映射，
    断线恢复后不会重复挂同一格。
    """
    plan.holdings.clear()
    plan.pending.clear()
    orphans: list[int] = []
    for pos in positions or []:
        ticket = _as_int(pos.get("ticket"))
        if ticket <= 0:
            continue
        absolute = parse_grid_comment(pos.get("comment"))
        if absolute is not None:
            level = plan.relative_index(absolute)
            if level is None:
                orphans.append(ticket)
                continue
        else:
            level = nearest_level_index(plan, _as_float(pos.get("price_open")))
        if level is None or level in plan.holdings:
            continue
        if 0 <= level < plan.grid_count:
            plan.holdings[level] = ticket
    for order in orders or []:
        ticket = _as_int(order.get("ticket"))
        if ticket <= 0:
            continue
        absolute = parse_grid_comment(order.get("comment"))
        if absolute is None:
            # 认不出的挂单不认领：宁可当成没有，避免重挂出双份手数
            continue
        level = plan.relative_index(absolute)
        if level is None:
            # 格位已被平移挤出网格：这张单留在盘上是孤儿单，调用方需撤掉
            orphans.append(ticket)
            continue
        if level in plan.holdings or level in plan.pending:
            continue
        if 0 <= level < plan.grid_count:
            plan.pending[level] = ticket
    return orphans


# ---------------------------------------------------------------------------
# 开单原因与计算规则的说明生成
# ---------------------------------------------------------------------------

def describe_plan(plan: GridPlan, cfg: GridConfig, spec: SymbolSpec) -> str:
    """建仓计划的一句话说明。"""
    if not plan.ok:
        return f"网格计划无效：{plan.reject}"
    gap = (
        (cfg.price_upper - cfg.price_lower) / cfg.grid_count
        if cfg.grid_mode == GRID_MODE_ARITHMETIC
        else (cfg.price_upper / cfg.price_lower) ** (1.0 / cfg.grid_count) - 1.0
    )
    gap_text = (
        f"间距 {_trim(gap, spec.digits)}"
        if cfg.grid_mode == GRID_MODE_ARITHMETIC
        else f"公比 1+{_trim(gap * 100, 4)}%"
    )
    return (
        f"挂单网格：区间 [{_trim(cfg.price_lower, spec.digits)}, "
        f"{_trim(cfg.price_upper, spec.digits)}]，"
        f"{cfg.grid_count} 格{cfg.grid_mode_label}（{gap_text}），"
        f"方向 {cfg.grid_side_label}→{plan.side}，"
        f"每格 {_trim(plan.lot_per_grid, spec.volume_digits)} 手"
        + (f"，总量上限 {_trim(cfg.total_lot_limit, spec.volume_digits)}" if cfg.total_lot_limit else "")
        + "；入场挂限价单、成交后由券商止盈出场"
        + (
            "；向上追踪开启"
            + (f"（最多 {cfg.trailing_max} 格）" if cfg.trailing_max else "")
            if cfg.trailing_up else ""
        )
    )


def plan_detail(plan: GridPlan, cfg: GridConfig, spec: SymbolSpec) -> dict:
    """建仓计划的结构化明细。"""
    return {
        "kind": "grid_plan",
        "side": plan.side,
        "grid_side": cfg.grid_side,
        "grid_side_label": cfg.grid_side_label,
        "grid_mode": cfg.grid_mode,
        "grid_mode_label": cfg.grid_mode_label,
        "price_lower": cfg.price_lower,
        "price_upper": cfg.price_upper,
        "grid_count": cfg.grid_count,
        "lot_per_grid": plan.lot_per_grid,
        "total_lot_limit": cfg.total_lot_limit or None,
        "trigger_price": cfg.trigger_price or None,
        "stop_lower": cfg.stop_lower or None,
        "stop_upper": cfg.stop_upper or None,
        "close_on_stop": cfg.close_on_stop,
        "entry_mode": "pending",
        "trailing_up": cfg.trailing_up,
        "trailing_max": cfg.trailing_max or None,
        "shift_count": plan.shift_count,
        "levels": list(plan.levels),
        # 在途入场挂单所在格位：前端事件明细直接展示，不必再去查 runtime
        "pending_levels": sorted(plan.pending.keys()),
        "digits": spec.digits,
        "volume_step": spec.volume_step,
    }


def describe_shift(plan: GridPlan, shift: TrailingShift, *, price: float,
                   spec: SymbolSpec) -> str:
    """一次网格平移的原因说明（plan 须已应用该平移）。"""
    arrow = "上移" if shift.steps > 0 else "下移"
    text = (
        f"向上追踪 · 网格{arrow} {abs(shift.steps)} 格："
        f"现价 {_trim(price, spec.digits)} 越过原区间 "
        f"[{_trim(shift.from_lower, spec.digits)}, {_trim(shift.from_upper, spec.digits)}]，"
        f"新区间 [{_trim(plan.levels[0], spec.digits)}, {_trim(plan.levels[-1], spec.digits)}]"
    )
    if plan.stop_lower or plan.stop_upper:
        text += (
            f"，止损 / 止盈同步为 {_trim(plan.stop_lower, spec.digits) if plan.stop_lower else '不设'}"
            f" / {_trim(plan.stop_upper, spec.digits) if plan.stop_upper else '不设'}"
        )
    if shift.dropped:
        text += f"；{len(shift.dropped)} 格移出网格已兑现"
    if shift.dropped_pending:
        text += f"；{len(shift.dropped_pending)} 张挂单移出网格已撤销"
    return text


def shift_detail(plan: GridPlan, shift: TrailingShift, *, price: float) -> dict:
    """一次网格平移的结构化明细。"""
    return {
        "kind": "grid_shift",
        "direction": shift.direction,
        "steps": shift.steps,
        "shift_count": plan.shift_count,
        "from_lower": shift.from_lower,
        "from_upper": shift.from_upper,
        "price_lower": plan.levels[0] if plan.levels else None,
        "price_upper": plan.levels[-1] if plan.levels else None,
        "stop_lower": plan.stop_lower or None,
        "stop_upper": plan.stop_upper or None,
        "price": price,
        "dropped_levels": [lv for lv, _ in shift.dropped],
        "dropped_pending_levels": [lv for lv, _ in shift.dropped_pending],
        "holding_count": plan.holding_count(),
        "pending_count": plan.pending_count(),
        "grid_count": plan.grid_count,
    }


def describe_place(plan: GridPlan, level: int, *, price: float,
                   spec: SymbolSpec) -> str:
    """一张网格入场挂单的开单原因。"""
    ep = entry_price(plan, level)
    xp = exit_price(plan, level)
    verb = "买入" if plan.is_long else "卖出"
    return (
        f"网格{verb}挂单 · 格位 {level}："
        f"入场 {_trim(ep, spec.digits)} / 出场 {_trim(xp, spec.digits)}，"
        f"现价 {_trim(price, spec.digits)}；"
        f"{_trim(plan.lot_per_grid, spec.volume_digits)} 手，"
        f"当前持格 {plan.holding_count()}/{plan.grid_count}"
        f"、在途挂单 {plan.pending_count()}"
    )


def describe_fill(plan: GridPlan, level: int, *, price: float,
                  action: str, spec: SymbolSpec) -> str:
    """一笔网格买入 / 卖出的开单原因。"""
    ep = entry_price(plan, level)
    xp = exit_price(plan, level)
    verb = "买入" if action.upper() in ("BUY", "GRID_ADD") else "卖出"
    return (
        f"网格{verb} · 格位 {level}："
        f"入场 {_trim(ep, spec.digits)} / 出场 {_trim(xp, spec.digits)}，"
        f"现价 {_trim(price, spec.digits)}；"
        f"{_trim(plan.lot_per_grid, spec.volume_digits)} 手，"
        f"当前持格 {plan.holding_count()}/{plan.grid_count}"
    )


def fill_detail(plan: GridPlan, level: int, *, price: float,
                action: str, ticket: Optional[int] = None) -> dict:
    """一笔网格成交的结构化明细：买入记 grid_fill，卖出记 grid_close。"""
    is_buy = str(action).upper() in ("BUY", "GRID_ADD")
    return {
        "kind": "grid_fill" if is_buy else "grid_close",
        "level_index": level,
        "level_price": entry_price(plan, level) or None,
        "exit_price": exit_price(plan, level) or None,
        "side": plan.side,
        "action": "BUY" if is_buy else "SELL",
        "price": price,
        "volume": plan.lot_per_grid,
        "ticket": ticket,
        "holding_count": plan.holding_count(),
        "pending_count": plan.pending_count(),
        "grid_count": plan.grid_count,
    }


def place_detail(plan: GridPlan, level: int, *, price: float,
                 ticket: Optional[int] = None) -> dict:
    """一张网格入场挂单的结构化明细。"""
    return {
        "kind": "grid_fill",
        "level_index": level,
        "level_price": entry_price(plan, level) or None,
        "exit_price": exit_price(plan, level) or None,
        "side": plan.side,
        "action": plan.side,
        "pending": True,
        "price": price,
        "volume": plan.lot_per_grid,
        "ticket": ticket,
        "holding_count": plan.holding_count(),
        "pending_count": plan.pending_count(),
        "grid_count": plan.grid_count,
    }
