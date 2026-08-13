"""网格试算（纯计算，无 I/O，可单独单元测试）。

模版3 的配置助手：把「ATR × 倍数」折成格距推出网格数量，再按「满仓打止损」的
最坏亏损反推每格手数。**只在配置期生成建议值**——用户确认后写进 `grid_count` /
`lot_per_grid` 两个既有字段，网格运行时不读这里的任何参数。

格数与每格手数是两个未知数，而风险约束只有一条，所以必须先由格距定下格数：

    格距 S = ATR × 倍数（可手改覆盖）
    格数 N = round((上限 - 下限) / S)，夹到 [2, 200]
    每格手数 q = 最大可接受亏损 / Σ_i（第 i 格成本价到止损价的距离折成的每手金额）

价格距离折成金额用 `tick_value / tick_size`，与节点侧 `risk_sizing` 同一口径
（这是唯一能跨品种通用的换算，不必自己处理合约大小与计价货币）；规格由行情探针
从节点带回，拿不到就只给格数、不给手数。

ATR 的口径与 `node_client/bar_metrics` 一致（14 根已收盘 K 线、真实波幅含跳空），
网格线切法与 `node_client/grid_trading.build_levels` 一致，否则同一份配置在助手里
与真实执行时会算出两套数字。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .strategy_templates import (
    BATCH_BAR_PERIOD,
    BATCH_TIMEFRAMES,
    GRID_COUNT_MAX,
    GRID_COUNT_MIN,
    GRID_MODE_ARITHMETIC,
    GRID_MODE_GEOMETRIC,
    GRID_MODES,
    GRID_SIDE_LONG,
    GRID_SIDE_SHORT,
    GRID_SIDES,
)

# 试算默认周期。网格格距要反映的是区间级别的波动结构，比加仓档位的 M5 更大一档
DEFAULT_TIMEFRAME = "H1"
# ATR 倍数的合理区间：倍数过小会切出上百格、过大则只剩两三格，两头都无意义
ATR_MULT_MIN = 0.05
ATR_MULT_MAX = 50.0


def bars_needed() -> int:
    """算 ATR 需要取多少根已收盘 K 线：真实波幅要用到前一根收盘价，故多取一根。"""
    return BATCH_BAR_PERIOD + 1


def normalize_timeframe(value: object) -> str:
    tf = str(value or "").strip().upper()
    return tf if tf in BATCH_TIMEFRAMES else DEFAULT_TIMEFRAME


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
# ATR（与 node_client/bar_metrics 同口径）
# ---------------------------------------------------------------------------

def true_range(bar: dict, prev_close: float) -> float:
    """单根 K 线的真实波幅：本根高低差、以及高/低相对前收的跳空，取最大。"""
    high = _as_float((bar or {}).get("high"))
    low = _as_float((bar or {}).get("low"))
    if high <= 0 or low <= 0:
        return 0.0
    if prev_close <= 0:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(bars: list[dict]) -> float:
    """ATR：真实波幅的算术平均。K 线不足（少于 2 根）时返回 0。"""
    if not bars or len(bars) < 2:
        return 0.0
    ranges = [
        true_range(bar, _as_float((bars[i - 1] or {}).get("close")))
        for i, bar in enumerate(bars)
        if i > 0
    ]
    ranges = [r for r in ranges if r > 0]
    return sum(ranges) / len(ranges) if ranges else 0.0


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
    atr_mult: float = 1.0
    spacing: float = 0.0          # >0 表示手改覆盖，不再用 ATR×倍数
    max_loss: float = 0.0
    prefill_enabled: bool = True
    close_on_stop: bool = True
    trailing_up: bool = False
    total_lot_limit: float = 0.0

    @classmethod
    def from_params(cls, raw: dict | None) -> "SizingInput":
        src = raw if isinstance(raw, dict) else {}
        mode = str(src.get("grid_mode") or GRID_MODE_ARITHMETIC).strip().lower()
        side = str(src.get("grid_side") or GRID_SIDE_LONG).strip().lower()
        mult = _as_float(src.get("atr_mult"), 1.0)
        return cls(
            price_lower=max(0.0, _as_float(src.get("price_lower"))),
            price_upper=max(0.0, _as_float(src.get("price_upper"))),
            grid_mode=mode if mode in GRID_MODES else GRID_MODE_ARITHMETIC,
            grid_side=side if side in GRID_SIDES else GRID_SIDE_LONG,
            stop_lower=max(0.0, _as_float(src.get("stop_lower"))),
            stop_upper=max(0.0, _as_float(src.get("stop_upper"))),
            atr_mult=min(ATR_MULT_MAX, max(ATR_MULT_MIN, mult)) if mult > 0 else 1.0,
            spacing=max(0.0, _as_float(src.get("spacing"))),
            max_loss=max(0.0, _as_float(src.get("max_loss"))),
            prefill_enabled=bool(src.get("prefill_enabled", True)),
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


@dataclass
class SizingResult:
    """试算结果。`ready` 只表示格数算出来了，手数另看 `lot_per_grid`。"""
    ready: bool = False
    atr: float = 0.0
    spacing: float = 0.0
    spacing_source: str = "atr"          # atr=按倍数算 / manual=手改覆盖
    grid_count: int = 0
    lot_per_grid: float = 0.0
    worst_loss: float = 0.0              # 满仓打止损的估算亏损（按建议手数）
    worst_lot: float = 0.0               # 满仓总手数
    levels: list[float] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def warn(self, code: str, message: str, level: str = "warn") -> None:
        self.warnings.append({"code": code, "level": level, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "atr": self.atr,
            "spacing": self.spacing,
            "spacing_source": self.spacing_source,
            "grid_count": self.grid_count,
            "lot_per_grid": self.lot_per_grid,
            "worst_loss": self.worst_loss,
            "worst_lot": self.worst_lot,
            "levels": list(self.levels),
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


def grid_count_for_spacing(lower: float, upper: float, spacing: float) -> int:
    """按格距推网格数量并夹到规则允许的范围。

    等比模式下格距本身不均匀，这里用平均间距近似定格数；真正的风险数字仍按切出来
    的等比网格线逐格算，所以只有「格数怎么来的」是近似。
    """
    if spacing <= 0 or upper <= lower:
        return 0
    raw = int(round((upper - lower) / spacing))
    return max(GRID_COUNT_MIN, min(GRID_COUNT_MAX, raw))


def entry_costs(levels: list[float], *, is_long: bool, prefill_enabled: bool,
                price: float) -> list[float]:
    """满仓时每一格的成本价。

    未预填的格将来在自己的网格线上成交：多头买 `levels[i]`、空头开空 `levels[i+1]`。
    预填走的是**市价**，成本是现价而不是格线价，所以现价可用时要按现价计：现价上方
    的格因此变便宜、下方的格变贵（开仓即浮亏），两边都要如实计入才算得准。预填格位
    的判定与节点侧 `prefill_indices` 一致：多头取卖出线仍高于现价的格，空头取平仓线
    仍低于现价的格。
    """
    count = max(0, len(levels) - 1)
    costs: list[float] = []
    usable_price = price > 0 and prefill_enabled
    for i in range(count):
        base = levels[i] if is_long else levels[i + 1]
        if usable_price:
            prefilled = levels[i + 1] > price if is_long else levels[i] < price
            costs.append(price if prefilled else base)
        else:
            costs.append(base)
    return costs


def loss_per_lot(costs: list[float], stop: float, *, is_long: bool,
                 spec: SymbolSpec) -> float:
    """满仓打到止损时「每手」的总亏损金额（账户货币）。

    单格已经在盈利侧（成本比止损更有利）时不计负数，避免用浮盈去抵扣风险预算。
    """
    if stop <= 0 or not spec.usable():
        return 0.0
    per_price = spec.tick_value / spec.tick_size
    total = 0.0
    for cost in costs:
        distance = (cost - stop) if is_long else (stop - cost)
        if distance > 0:
            total += distance * per_price
    return total


def suggest_lot(max_loss: float, per_lot: float, spec: SymbolSpec) -> float:
    """按最大可接受亏损反推每格手数，并向下取整到品种步长。"""
    if max_loss <= 0 or per_lot <= 0 or not spec.usable():
        return 0.0
    return spec.floor_lot(max_loss / per_lot)


def evaluate(bars: list[dict], spec_raw: object, params: dict | None,
             *, price: float = 0.0) -> dict[str, Any]:
    """完整试算：ATR → 格距 → 格数 → 每格手数 → 最坏亏损，并给出提示。"""
    cfg = SizingInput.from_params(params)
    spec = SymbolSpec.from_probe(spec_raw)
    out = SizingResult(spacing_source="manual" if cfg.spacing > 0 else "atr")

    if cfg.price_lower <= 0 or cfg.price_upper <= cfg.price_lower:
        out.warn("range_invalid", "请先填写合法的价格区间（上限须大于下限）")
        return out.to_dict()

    out.atr = round(average_true_range(list(bars or [])), max(spec.digits, 2))
    if cfg.spacing > 0:
        out.spacing = cfg.spacing
    elif out.atr > 0:
        out.spacing = round(out.atr * cfg.atr_mult, max(spec.digits, 2))
    else:
        out.warn("atr_unavailable", "K 线不足，算不出 ATR；可手动填写格距后再试算")
        return out.to_dict()

    width = cfg.price_upper - cfg.price_lower
    if out.spacing >= width:
        out.warn("spacing_too_wide", "格距不小于区间宽度，至少要能切出 2 格")
        return out.to_dict()

    out.grid_count = grid_count_for_spacing(cfg.price_lower, cfg.price_upper, out.spacing)
    out.levels = build_levels(
        cfg.price_lower, cfg.price_upper, out.grid_count, cfg.grid_mode, digits=spec.digits,
    )
    out.ready = bool(out.levels)
    if not out.ready:
        out.warn("levels_unavailable", "无法按当前区间切出网格线")
        return out.to_dict()

    actual = round(width / out.grid_count, max(spec.digits, 2))
    if abs(actual - out.spacing) > 10 ** -max(spec.digits, 2):
        out.warn(
            "spacing_rounded",
            f"格数取整后实际格距为 {_trim(actual, spec.digits)}"
            f"（输入 {_trim(out.spacing, spec.digits)}）",
            level="info",
        )
    if out.grid_count in (GRID_COUNT_MIN, GRID_COUNT_MAX):
        out.warn(
            "grid_count_clamped",
            f"网格数量已夹到允许范围的 {out.grid_count} 格，实际格距与输入不同",
        )
    if cfg.grid_mode == GRID_MODE_GEOMETRIC:
        out.warn(
            "geometric_spacing",
            "等比网格每格间距不等，格距在此只用于估算格数；手数按真实格线逐格计算",
            level="info",
        )

    _size_lots(out, cfg, spec, price=price)
    _collect_risk_warnings(out, cfg, spec)
    return out.to_dict()


def _size_lots(out: SizingResult, cfg: SizingInput, spec: SymbolSpec,
               *, price: float) -> None:
    """反推每格手数并回算最坏亏损；缺规格 / 缺止损时只给格数。"""
    if not spec.usable():
        out.warn(
            "spec_unavailable",
            "节点未回传该品种的合约规格，只能给出网格数量；每格手数请手动填写",
        )
        return
    stop = cfg.stop_price
    stop_name = "止损价（区间下沿）" if cfg.is_long else "止损价（区间上沿）"
    if stop <= 0:
        out.warn("stop_missing", f"未设置{stop_name}，无法按最大亏损反推每格手数")
        return
    if cfg.max_loss <= 0:
        out.warn("max_loss_missing", "请填写最大可接受亏损金额")
        return

    costs = entry_costs(
        out.levels, is_long=cfg.is_long, prefill_enabled=cfg.prefill_enabled, price=price,
    )
    per_lot = loss_per_lot(costs, stop, is_long=cfg.is_long, spec=spec)
    if per_lot <= 0:
        out.warn("stop_unreachable", f"{stop_name}相对区间的位置算不出亏损距离")
        return

    lot = suggest_lot(cfg.max_loss, per_lot, spec)
    if lot <= 0 or (spec.volume_min > 0 and lot < spec.volume_min):
        out.warn(
            "lot_below_min",
            f"按该预算反推的每格手数低于最小手数 {_trim(spec.volume_min, spec.volume_digits)}；"
            "可减少网格数量、收窄区间或提高可接受亏损",
        )
        return

    out.lot_per_grid = lot
    out.worst_lot = round(lot * out.grid_count, spec.volume_digits)
    out.worst_loss = round(per_lot * lot, 2)
    if cfg.prefill_enabled and price > 0:
        out.warn(
            "prefill_market_cost",
            f"已按初始建仓的市价成本（现价 {_trim(price, spec.digits)}）估算最坏亏损",
            level="info",
        )


def _collect_risk_warnings(out: SizingResult, cfg: SizingInput, spec: SymbolSpec) -> None:
    """把「预览数字在哪些情况下不成立」一次讲清。"""
    if cfg.stop_lower > 0 and cfg.stop_lower >= cfg.price_lower:
        out.warn("stop_inside_range", "下沿终止价须低于区间下限，否则保存时会被视为未设")
    if cfg.stop_upper > 0 and cfg.stop_upper <= cfg.price_upper:
        out.warn("stop_inside_range", "上沿终止价须高于区间上限，否则保存时会被视为未设")

    lot = out.lot_per_grid
    if lot > 0 and cfg.total_lot_limit > 0:
        if cfg.total_lot_limit < lot:
            out.warn(
                "total_lot_limit_low",
                f"总手数上限 {_trim(cfg.total_lot_limit, spec.volume_digits)} "
                f"小于建议的每格手数，策略将无法保存",
            )
        elif cfg.total_lot_limit < out.worst_lot:
            out.warn(
                "total_lot_limit_caps",
                "总手数上限会截断满仓格数，实际风险低于上面的估算",
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
