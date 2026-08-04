"""网格交易的纯计算逻辑（无 I/O，可单独单元测试）。

对应后台「策略模版3」，规则字段随 strategy_start 的策略快照下发，复刻币安现货
手动创建网格的行为：

- price_lower / price_upper：价格区间
- grid_count：网格数量（切成 N 格，N+1 条线）
- grid_mode：arithmetic 等差 / geometric 等比
- grid_side：long 只做多 / short 只做空 / follow 跟随信号方向
- lot_per_grid：每格手数
- total_lot_limit：总手数上限，0=不额外限制
- trigger_price：触发价，0=立即启动
- stop_lower / stop_upper：止损 / 止盈价，0=不设
- close_on_stop：终止时是否清仓
- prefill_enabled：是否按现价上方格位初始建仓

核心循环：价格下跌穿越网格线 → 买入一格；上涨穿越 → 卖出对应格。空仓是正常
运行态，任务不会因持仓归零而结束。

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
# 备注编码：G4L{index} —— G=网格，4=规则 type，L=格位，后接序号
_COMMENT_RE = re.compile(r"^G4L(\d+)$")


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
    prefill_enabled: bool = True

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
            prefill_enabled=bool(rule.get("prefill_enabled", True)),
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
    prev_price: float = 0.0
    triggered: bool = False                 # 是否已过触发价 / 无需触发
    reject: str = ""

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

    levels = build_levels(cfg, digits=spec.digits)
    if len(levels) < 3:
        plan.reject = "网格数量过少，无法切出有效格位"
        return plan

    plan.levels = levels
    plan.lot_per_grid = lot
    plan.triggered = cfg.trigger_price <= 0
    return plan


def trigger_reached(cfg: GridConfig, price: float, prev_price: float = 0.0) -> bool:
    """当前价是否已触及触发价（未配置触发价视为已触发）。

    与币安一致：最新价达到触发价时启动。用 prev→price 是否穿越 / 落到触发价判定，
    避免同一价位反复判断；首个 tick（prev=0）时，现价已在触发价上方也视为到价。
    """
    if cfg.trigger_price <= 0:
        return True
    if price <= 0:
        return False
    t = cfg.trigger_price
    if prev_price <= 0:
        return price >= t
    # 穿越或落到触发价：两端乘积 ≤ 0
    return (prev_price - t) * (price - t) <= 0


def prefill_indices(plan: GridPlan, price: float) -> list[int]:
    """初始建仓应买入的格位下标列表（现价上方的空闲格）。

    多头网格：现价上方的格位 i（levels[i] < price <= levels[i+1] 的上方）需要先买入，
    这样价格继续上涨时才有货可卖。具体：所有满足 levels[i+1] <= price 的格位 i
    （即卖出价已在现价下方或等于现价的格）——币安逻辑是「买入现价以上所有网格的量」。

    币安现货：创建时按「当前价上方的网格数量」市价买入。对应本模型：
    格位 i 的买入线是 levels[i]，卖出线是 levels[i+1]。
    现价上方的格 = 卖出线 > 现价 的格，即 levels[i+1] > price 且 levels[i] < price
    的那一格之上的所有格……更准确：买入所有 levels[i] < price 的格位
    （因为这些格的买入价已在现价下方，相当于「已经跌破买入线」的持仓）。

    与币安一致的简化：买入所有买入线 < 现价 的格位（levels[i] < price）。
    """
    if not plan.ok or price <= 0:
        return []
    indices: list[int] = []
    # 格位 0..N-1：买入线 levels[i]，卖出线 levels[i+1]
    for i in range(plan.grid_count):
        buy_line = plan.levels[i]
        if plan.is_long:
            # 多头：买入线已在现价下方 → 视为应持有
            if buy_line < price:
                indices.append(i)
        else:
            # 空头：卖出线（对称）已在现价上方 → 视为应持有
            sell_line = plan.levels[i + 1]
            if sell_line > price:
                indices.append(i)
    return indices


def capped_prefill(plan: GridPlan, cfg: GridConfig, indices: list[int]) -> list[int]:
    """按 total_lot_limit 截断初始建仓格数。"""
    if not indices or cfg.total_lot_limit <= 0 or plan.lot_per_grid <= 0:
        return indices
    max_grids = int(math.floor((cfg.total_lot_limit + 1e-9) / plan.lot_per_grid))
    if max_grids <= 0:
        return []
    # 多头优先保留靠近现价的格（列表末尾）；空头同理取靠近现价的
    if len(indices) <= max_grids:
        return indices
    if plan.is_long:
        return indices[-max_grids:]
    return indices[:max_grids]


# ---------------------------------------------------------------------------
# 运行期判定
# ---------------------------------------------------------------------------

def crossings(plan: GridPlan, prev_price: float, price: float) -> list[Crossing]:
    """用上一次价格与本次价格判断穿越了哪些网格线及方向。

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


def buy_level_for_crossing(plan: GridPlan, crossing: Crossing) -> Optional[int]:
    """穿越事件对应的买入格位；无可买返回 None。

    多头：下跌穿越第 i 线 → 买入格位 i（在 levels[i] 买入，等涨到 levels[i+1] 卖）。
      注意：穿越下限（i=0）可买格位 0；穿越上限（i=N）不产生新买入格。
    空头：上涨穿越第 i 线 → 买入（开空）格位 i-1。
    """
    i = crossing.level_index
    if plan.is_long:
        if crossing.direction != "down":
            return None
        # 下跌穿越线 i → 买入格位 i（需 i < N，即不是穿过上限之上）
        if i >= plan.grid_count:
            return None
        level = i
    else:
        if crossing.direction != "up":
            return None
        # 上涨穿越线 i → 开空格位 i-1
        if i <= 0:
            return None
        level = i - 1
    if level in plan.holdings:
        return None
    return level


def sell_level_for_crossing(plan: GridPlan, crossing: Crossing) -> Optional[int]:
    """穿越事件对应的卖出格位；无可卖返回 None。

    多头：上涨穿越第 i 线 → 卖出格位 i-1（该格的卖出价是 levels[i]）。
    空头：下跌穿越第 i 线 → 平空格位 i。
    """
    i = crossing.level_index
    if plan.is_long:
        if crossing.direction != "up":
            return None
        if i <= 0:
            return None
        level = i - 1
    else:
        if crossing.direction != "down":
            return None
        if i >= plan.grid_count:
            return None
        level = i
    if level not in plan.holdings:
        return None
    return level


def can_buy_more(plan: GridPlan, cfg: GridConfig) -> bool:
    """是否还能再买一格（受 total_lot_limit 约束）。"""
    if cfg.total_lot_limit <= 0 or plan.lot_per_grid <= 0:
        return True
    used = plan.holding_count() * plan.lot_per_grid
    return used + plan.lot_per_grid <= cfg.total_lot_limit + 1e-9


def terminate_reason(cfg: GridConfig, price: float, *, side: str) -> Optional[str]:
    """当前价是否触发止损 / 止盈；未触发返回 None。"""
    if price <= 0:
        return None
    is_long = str(side or "BUY").upper() == "BUY"
    if is_long:
        if cfg.stop_lower > 0 and price <= cfg.stop_lower:
            return f"触发止损价 {cfg.stop_lower}"
        if cfg.stop_upper > 0 and price >= cfg.stop_upper:
            return f"触发止盈价 {cfg.stop_upper}"
    else:
        if cfg.stop_upper > 0 and price >= cfg.stop_upper:
            return f"触发止损价 {cfg.stop_upper}"
        if cfg.stop_lower > 0 and price <= cfg.stop_lower:
            return f"触发止盈价 {cfg.stop_lower}"
    return None


# ---------------------------------------------------------------------------
# 备注编解码（断线恢复用）
# ---------------------------------------------------------------------------

def grid_comment(level_index: int) -> str:
    """格位单写进 MT5 的紧凑备注：G4L{index}。"""
    return f"G4L{int(level_index)}"[:MT5_COMMENT_LIMIT]


def parse_grid_comment(comment: object) -> Optional[int]:
    """从 MT5 备注解析格位号；解析失败返回 None。"""
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


def rebuild_holdings(plan: GridPlan, positions: list[dict]) -> None:
    """按持仓 comment（失败则按开仓价就近）还原格位→ticket 映射。"""
    plan.holdings.clear()
    for pos in positions or []:
        ticket = _as_int(pos.get("ticket"))
        if ticket <= 0:
            continue
        level = parse_grid_comment(pos.get("comment"))
        if level is None:
            level = nearest_level_index(plan, _as_float(pos.get("price_open")))
        if level is None or level in plan.holdings:
            continue
        if 0 <= level < plan.grid_count:
            plan.holdings[level] = ticket


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
        f"网格交易：区间 [{_trim(cfg.price_lower, spec.digits)}, "
        f"{_trim(cfg.price_upper, spec.digits)}]，"
        f"{cfg.grid_count} 格{cfg.grid_mode_label}（{gap_text}），"
        f"方向 {cfg.grid_side_label}→{plan.side}，"
        f"每格 {_trim(plan.lot_per_grid, spec.volume_digits)} 手"
        + (f"，总量上限 {_trim(cfg.total_lot_limit, spec.volume_digits)}" if cfg.total_lot_limit else "")
        + ("；初始建仓开启" if cfg.prefill_enabled else "；不初始建仓")
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
        "prefill_enabled": cfg.prefill_enabled,
        "levels": list(plan.levels),
        "digits": spec.digits,
        "volume_step": spec.volume_step,
    }


def describe_fill(plan: GridPlan, level: int, *, price: float,
                  action: str, spec: SymbolSpec) -> str:
    """一笔网格买入 / 卖出的开单原因。"""
    buy_line = plan.levels[level] if 0 <= level < len(plan.levels) else 0.0
    sell_line = (
        plan.levels[level + 1] if 0 <= level + 1 < len(plan.levels) else 0.0
    )
    verb = "买入" if action.upper() in ("BUY", "GRID_ADD") else "卖出"
    return (
        f"网格{verb} · 格位 {level}："
        f"买线 {_trim(buy_line, spec.digits)} / 卖线 {_trim(sell_line, spec.digits)}，"
        f"现价 {_trim(price, spec.digits)}；"
        f"{_trim(plan.lot_per_grid, spec.volume_digits)} 手，"
        f"当前持格 {plan.holding_count()}/{plan.grid_count}"
    )


def fill_detail(plan: GridPlan, level: int, *, price: float,
                action: str, ticket: Optional[int] = None) -> dict:
    """一笔网格成交的结构化明细。"""
    kind = "grid_fill" if str(action).upper() in ("BUY", "SELL", "GRID_ADD") else "grid_close"
    if str(action).upper() in ("CLOSE", "GRID_CLOSE") or kind == "grid_close" and "卖" in str(action):
        kind = "grid_close"
    # 买入用 grid_fill，卖出用 grid_close
    is_buy = str(action).upper() in ("BUY", "GRID_ADD")
    return {
        "kind": "grid_fill" if is_buy else "grid_close",
        "level_index": level,
        "level_price": plan.levels[level] if 0 <= level < len(plan.levels) else None,
        "exit_price": (
            plan.levels[level + 1] if 0 <= level + 1 < len(plan.levels) else None
        ),
        "side": plan.side,
        "action": "BUY" if is_buy else "SELL",
        "price": price,
        "volume": plan.lot_per_grid,
        "ticket": ticket,
        "holding_count": plan.holding_count(),
        "grid_count": plan.grid_count,
    }
