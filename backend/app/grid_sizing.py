"""网格试算（纯计算，无 I/O，可单独单元测试）。

模版3 的配置助手：**套利优先**——目标是在预算内把格子切得尽量密、每格手数尽量小，
因为网格吃的就是「挂单价 → 出场价」这一小段差价，格子越密、穿越越频繁，套利越快。

算法只用**预算**和**合约规格**，不依赖 ATR / K 线：

    1. 均价到止损距离 D = 区间半宽 + 止损价离区间边界的距离
       （满仓后成本大致围绕区间分布，中点是这一组成本的稳健代表；逐格求和会把
       「每一格都亏到止损」的不可能场景当成约束，预算稍小就反推出配不出来的手数）
    2. 每格一手打止损的亏损 = D × 每手每美金价值
       每手每美金价值 = tick_value / tick_size，与节点侧 `risk_sizing` 同一口径
       （这是唯一能跨品种通用的换算，不必自己处理合约大小与计价货币）
    3. 预算能开多少格 N = floor(预算 ÷ 每格一手的亏损)，夹到 [2, 58]
       —— 格数由预算直接决定：预算越大格子越多，套利越频繁
    4. 每格手数 q = floor(预算 ÷ (每格一手的亏损 × N))，向下取到品种手数步长
       —— 先把格子开满，剩下的预算才补到每格手数上；格数被 58 上限卡住时，
       每格手数才会往上走（例如预算 10000 能开到每格 0.02 手）

规格由行情探针从节点带回，拿不到就只给格数、不给手数。手数一律**向下取整**：
预算是硬约束，宁可少用一点预算也绝不超出，超出的可能只出现在「每格手数已被
最小手数占满、格数却仍嫌多」的场景里，那种情况降级为最小手数并明确提示超出金额。

网格线切法与 `node_client/grid_trading.build_levels` 一致，否则同一份配置在助手里
与真实执行时会算出两套数字。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .strategy_templates import (
    GRID_COUNT_MAX,
    GRID_COUNT_MIN,
    GRID_MODE_ARITHMETIC,
    GRID_MODE_GEOMETRIC,
    GRID_MODES,
    GRID_SIDE_LONG,
    GRID_SIDE_SHORT,
    GRID_SIDES,
)


def _as_float(value: object, fallback: float = 0.0) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return fallback if math.isnan(num) or math.isinf(num) else num


def _trim(value: float, digits: int) -> str:
    text = f"{float(value):.{max(0, digits)}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------

@dataclass
class SymbolSpec:
    """品种规格，来自行情探针回包的 `spec`。缺失时只影响手数，不影响格数。"""
    digits: int = 5
    tick_size: float = 0.0
    tick_value: float = 0.0
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0

    @classmethod
    def from_probe(cls, raw: object) -> "SymbolSpec":
        src = raw if isinstance(raw, dict) else {}
        try:
            digits = int(src.get("digits") or 5)
        except (TypeError, ValueError):
            digits = 5
        return cls(
            digits=max(0, min(8, digits)),
            tick_size=max(0.0, _as_float(src.get("tick_size"))),
            tick_value=max(0.0, _as_float(src.get("tick_value"))),
            volume_min=max(0.0, _as_float(src.get("volume_min"))),
            volume_step=max(0.0, _as_float(src.get("volume_step"))),
            volume_max=max(0.0, _as_float(src.get("volume_max"))),
        )

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
        """能否把价格距离折算成金额并按步长取整。"""
        return self.tick_size > 0 and self.tick_value > 0 and self.volume_step > 0

    def floor_lot(self, lot: float) -> float:
        if self.volume_step <= 0:
            return round(max(lot, 0.0), 2)
        steps = math.floor((max(lot, 0.0) + 1e-9) / self.volume_step)
        return round(steps * self.volume_step, self.volume_digits)


@dataclass
class SizingInput:
    """一次试算的入参。区间与止损直接取规则上已填的值，不另设一套。"""
    price_lower: float = 0.0
    price_upper: float = 0.0
    grid_mode: str = GRID_MODE_ARITHMETIC
    grid_side: str = GRID_SIDE_LONG
    stop_lower: float = 0.0
    stop_upper: float = 0.0
    max_loss: float = 0.0
    close_on_stop: bool = True
    trailing_up: bool = False
    total_lot_limit: float = 0.0

    @classmethod
    def from_params(cls, raw: dict | None) -> "SizingInput":
        src = raw if isinstance(raw, dict) else {}
        mode = str(src.get("grid_mode") or GRID_MODE_ARITHMETIC).strip().lower()
        side = str(src.get("grid_side") or GRID_SIDE_LONG).strip().lower()
        return cls(
            price_lower=max(0.0, _as_float(src.get("price_lower"))),
            price_upper=max(0.0, _as_float(src.get("price_upper"))),
            grid_mode=mode if mode in GRID_MODES else GRID_MODE_ARITHMETIC,
            grid_side=side if side in GRID_SIDES else GRID_SIDE_LONG,
            stop_lower=max(0.0, _as_float(src.get("stop_lower"))),
            stop_upper=max(0.0, _as_float(src.get("stop_upper"))),
            max_loss=max(0.0, _as_float(src.get("max_loss"))),
            close_on_stop=bool(src.get("close_on_stop", True)),
            trailing_up=bool(src.get("trailing_up", False)),
            total_lot_limit=max(0.0, _as_float(src.get("total_lot_limit"))),
        )

    @property
    def is_long(self) -> bool:
        return self.grid_side != GRID_SIDE_SHORT

    @property
    def stop_price(self) -> float:
        """本方向的止损价：多头在区间下沿、空头在区间上沿。"""
        return self.stop_lower if self.is_long else self.stop_upper

    @property
    def stop_distance(self) -> float:
        """止损价离本侧区间边界的距离（多头看下限、空头看上限）；未设止损为 0。"""
        stop = self.stop_price
        if stop <= 0:
            return 0.0
        edge = self.price_lower if self.is_long else self.price_upper
        return max(0.0, abs(edge - stop))

    @property
    def avg_stop_distance(self) -> float:
        """区间中点到止损价的距离：预算反推手数用的代表距离。

        满仓后各格成本大致铺满区间，用它当「均价到止损」比逐格求和对小预算友好，
        也不会像「取已有格线」那样随格数抖动。
        """
        if self.price_upper <= self.price_lower:
            return 0.0
        return (self.price_upper - self.price_lower) / 2.0 + self.stop_distance


@dataclass
class SizingResult:
    """试算结果。`ready` 只表示格数算出来了，手数另看 `lot_per_grid`。"""
    ready: bool = False
    grid_count: int = 0
    lot_per_grid: float = 0.0
    worst_loss: float = 0.0              # 满仓打止损的真实亏损（按建议手数）
    worst_lot: float = 0.0               # 满仓总手数
    levels: list[float] = field(default_factory=list)
    # ---- 口径信息（只用于展示，不参与运行期）----
    avg_stop_distance: float = 0.0       # 区间中点到止损价的距离
    risk_per_lot: float = 0.0            # 每手满仓打止损的亏损金额
    risk_per_grid: float = 0.0           # 每格一手打止损的亏损金额
    budget_grid_count: int = 0           # 预算能开的最多格（按每格一手最小手数）
    over_budget: bool = False            # 是否超出预算
    budget_excess: float = 0.0           # 超预算金额
    warnings: list[dict[str, str]] = field(default_factory=list)

    def warn(self, code: str, message: str, level: str = "warn") -> None:
        self.warnings.append({"code": code, "level": level, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "grid_count": self.grid_count,
            "lot_per_grid": self.lot_per_grid,
            "worst_loss": self.worst_loss,
            "worst_lot": self.worst_lot,
            "levels": list(self.levels),
            "avg_stop_distance": self.avg_stop_distance,
            "risk_per_lot": self.risk_per_lot,
            "risk_per_grid": self.risk_per_grid,
            "budget_grid_count": self.budget_grid_count,
            "over_budget": self.over_budget,
            "budget_excess": self.budget_excess,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# 纯计算
# ---------------------------------------------------------------------------

def build_levels(lower: float, upper: float, count: int, mode: str,
                 *, digits: int = 5) -> list[float]:
    """按等差 / 等比切出 count+1 条网格线，与 node_client/grid_trading 保持一致。"""
    if lower <= 0 or upper <= lower or count < 1:
        return []
    levels: list[float] = []
    if mode == GRID_MODE_GEOMETRIC:
        ratio = (upper / lower) ** (1.0 / count)
        for i in range(count + 1):
            levels.append(round(lower * (ratio ** i), digits))
    else:
        step = (upper - lower) / count
        for i in range(count + 1):
            levels.append(round(lower + step * i, digits))
    levels[0] = round(lower, digits)
    levels[-1] = round(upper, digits)
    return levels


def entry_costs(levels: list[float], *, is_long: bool) -> list[float]:
    """满仓时每一格的成本价。

    网格全程用挂单：每一格都在自己的网格线上成交，多头买 `levels[i]`、
    空头开空 `levels[i+1]`。成本就是格线价，没有市价建仓那一条路径。
    试算的风险口径用区间中点（见 `SizingInput.avg_stop_distance`），这里保留
    作为逐格核对的工具。
    """
    count = max(0, len(levels) - 1)
    return [levels[i] if is_long else levels[i + 1] for i in range(count)]


def risk_per_lot(stop_distance: float, *, spec: SymbolSpec) -> float:
    """均价到止损距离折成的每手亏损金额（账户货币）。"""
    if stop_distance <= 0 or not spec.usable():
        return 0.0
    return stop_distance * (spec.tick_value / spec.tick_size)


def grid_count_for_budget(max_loss: float, risk_per_grid: float) -> int:
    """预算能开多少格：每格按一手最小手数算，格数夹到 [2, 58]。

    格数直接由预算决定——预算越大格子越多，套利越频繁；上限 58 是系统硬限制，
    再多也存不进规则。
    """
    if max_loss <= 0 or risk_per_grid <= 0:
        return 0
    raw = int(math.floor(max_loss / risk_per_grid + 1e-9))
    return max(GRID_COUNT_MIN, min(GRID_COUNT_MAX, raw))


def budget_total_lot(max_loss: float, per_lot: float, spec: SymbolSpec) -> float:
    """预算允许的满仓总手数，向下取整到品种步长。"""
    if max_loss <= 0 or per_lot <= 0 or not spec.usable():
        return 0.0
    return spec.floor_lot(max_loss / per_lot)


def evaluate(spec_raw: object, params: dict | None, *, price: float = 0.0) -> dict[str, Any]:
    """完整试算：均价到止损 → 每格亏损 → 格数 → 每格手数 → 最坏亏损，并给出提示。"""
    cfg = SizingInput.from_params(params)
    spec = SymbolSpec.from_probe(spec_raw)
    out = SizingResult()

    if cfg.price_lower <= 0 or cfg.price_upper <= cfg.price_lower:
        out.warn("range_invalid", "请先填写合法的价格区间（上限须大于下限）")
        return out.to_dict()

    if price > 0 and not (cfg.price_lower <= price <= cfg.price_upper):
        out.warn(
            "price_outside_range",
            f"当前价 {_trim(price, spec.digits)} 不在区间内，网格只会在价格回到区间内后成交",
            level="info",
        )

    out.avg_stop_distance = round(cfg.avg_stop_distance, max(spec.digits, 2))
    stop = cfg.stop_price
    stop_name = "止损价（区间下沿）" if cfg.is_long else "止损价（区间上沿）"
    if stop <= 0:
        out.warn("stop_missing", f"未设置{stop_name}，无法按最大亏损反推每格手数")
        return out.to_dict()
    if cfg.max_loss <= 0:
        out.warn("max_loss_missing", "请填写最大可接受亏损金额")
        return out.to_dict()
    if not spec.usable():
        out.warn(
            "spec_unavailable",
            "节点未回传该品种的合约规格，只能给出网格数量；每格手数请手动填写",
        )
        return out.to_dict()

    out.risk_per_lot = round(risk_per_lot(cfg.avg_stop_distance, spec=spec), 2)
    if out.risk_per_lot <= 0:
        out.warn("stop_unreachable", f"{stop_name}相对区间的位置算不出亏损距离")
        return out.to_dict()

    min_lot = spec.volume_min if spec.volume_min > 0 else spec.volume_step
    
    # 步骤1：计算预算允许的总手数（不考虑格数）
    total_lot_budget = budget_total_lot(cfg.max_loss, out.risk_per_lot, spec)
    if total_lot_budget <= 0:
        out.warn(
            "lot_below_min",
            f"预算按一手 {_trim(min_lot, spec.volume_digits)} 手、止损距离 {_trim(cfg.avg_stop_distance, 2)} 算，"
            f"每手打止损要亏 {_trim(out.risk_per_lot, 2)}，"
            f"连一手都开不出来；请提高可接受亏损或缩小止损距离",
        )
        return out.to_dict()
    
    # 步骤2：套利优先 - 在格数上限内尽量多格，格数不足时才增加每格手数
    max_grids_at_min_lot = int(math.floor(total_lot_budget / min_lot + 1e-9))
    max_grids_at_min_lot = max(GRID_COUNT_MIN, min(GRID_COUNT_MAX, max_grids_at_min_lot))
    
    if max_grids_at_min_lot < GRID_COUNT_MAX:
        # 预算切不满 200 格：每格就用最小手数
        out.grid_count = max_grids_at_min_lot
        lot_per_grid = min_lot
    else:
        # 预算够 200+ 格：用 200 格，每格手数向上调整
        out.grid_count = GRID_COUNT_MAX
        lot_per_grid = spec.floor_lot(total_lot_budget / GRID_COUNT_MAX)
        if lot_per_grid < min_lot:
            lot_per_grid = min_lot
    
    # 验算：预报风险指标
    out.risk_per_grid = round(out.risk_per_lot * min_lot, 2)
    
    out.levels = build_levels(
        cfg.price_lower, cfg.price_upper, out.grid_count, cfg.grid_mode, digits=spec.digits,
    )
    out.ready = bool(out.levels)
    if not out.ready:
        out.warn("levels_unavailable", "无法按当前区间切出网格线")
        return out.to_dict()

    if out.grid_count == GRID_COUNT_MAX and lot_per_grid > min_lot:
        out.warn(
            "grid_count_clamped",
            f"预算能开更多格，但网格数量上限是 {out.grid_count} 格；"
            f"每格手数已增至 {_trim(lot_per_grid, spec.volume_digits)} 手",
            level="info",
        )
    if cfg.grid_mode == GRID_MODE_GEOMETRIC:
        out.warn(
            "geometric_spacing",
            "等比网格每格间距不等，格数按均价口径计算；每格手数一致",
            level="info",
        )

    _size_lots(out, cfg, spec, lot_per_grid)
    _collect_risk_warnings(out, cfg, spec)
    return out.to_dict()


def _size_lots(out: SizingResult, cfg: SizingInput, spec: SymbolSpec, lot_per_grid: float) -> None:
    """验证每格手数不超预算；允许微调但不改变格数。"""
    # 验证手数不超预算（已在上一步计算，此处为防守性校验）
    actual_loss = out.risk_per_lot * lot_per_grid * out.grid_count
    
    out.lot_per_grid = lot_per_grid
    out.worst_lot = round(lot_per_grid * out.grid_count, spec.volume_digits)
    out.worst_loss = round(actual_loss, 2)
    
    if actual_loss > cfg.max_loss + 1e-9:
        out.over_budget = True
        out.budget_excess = round(actual_loss - cfg.max_loss, 2)
        out.warn(
            "over_budget",
            f"每格手数 {_trim(lot_per_grid, spec.volume_digits)} × {out.grid_count} 格 = "
            f"{_trim(out.worst_lot, spec.volume_digits)} 手，"
            f"满仓最坏亏损约 {_trim(out.worst_loss, 2)}，"
            f"超出预算 {_trim(out.budget_excess, 2)}",
        )
    elif lot_per_grid < spec.volume_min > 0:
        # 每格手数不足最小手数时报提示（已在上一步处理，此处为防守）
        out.warn(
            "lot_below_min",
            f"每格手数 {_trim(lot_per_grid, spec.volume_digits)} 不足最小手数 "
            f"{_trim(spec.volume_min, spec.volume_digits)}",
        )


    if not out.over_budget and cfg.total_lot_limit > 0:
        # 总手数上限是另一道约束：它会截断满仓格数，实际风险低于上面的估算
        if out.worst_lot > cfg.total_lot_limit:
            out.warn(
                "total_lot_limit_caps",
                f"总手数上限 {_trim(cfg.total_lot_limit, spec.volume_digits)} 会截断满仓手数"
                f"（满仓 {_trim(out.worst_lot, spec.volume_digits)} 手），实际风险低于上面的估算",
                level="info",
            )


def _collect_risk_warnings(out: SizingResult, cfg: SizingInput, spec: SymbolSpec) -> None:
    """把「预览数字在哪些情况下不成立」一次讲清。"""
    if cfg.stop_lower > 0 and cfg.stop_lower >= cfg.price_lower:
        out.warn("stop_inside_range", "下沿终止价须低于区间下限，否则保存时会被视为未设")
    if cfg.stop_upper > 0 and cfg.stop_upper <= cfg.price_upper:
        out.warn("stop_inside_range", "上沿终止价须高于区间上限，否则保存时会被视为未设")

    if not out.over_budget and out.lot_per_grid > 0 and cfg.max_loss > 0:
        if out.worst_loss < cfg.max_loss - 1e-9:
            out.warn(
                "budget_underused",
                f"满仓最坏亏损约 {_trim(out.worst_loss, 2)}，未用满预算 "
                f"{_trim(cfg.max_loss, 2)}；每格手数已向下取整到品种手数步长",
                level="info",
            )
    if not cfg.close_on_stop:
        out.warn("close_on_stop_off", "终止时不清仓：到止损后持仓保留，亏损不封顶")
    if cfg.trailing_up:
        out.warn("trailing_up_on", "已开启向上追踪：止损会随网格平移，最坏亏损估算不再成立")
    if out.lot_per_grid > 0:
        out.warn(
            "soft_stop",
            "网格订单不带止损单，终止价由节点轮询判定后市价清仓；"
            "节点掉线或跳空时实际亏损可能超出预算",
            level="info",
        )
