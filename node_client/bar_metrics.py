"""K 线统计指标（纯计算，无 I/O，可单独单元测试）。

供分批加仓档位的「ATR」与「波幅」两种间距计算方式使用：两者都把一段已收盘
K 线折算成一个**价格距离**，再由判定层换算成点数与实际偏离比较。

统计根数固定为 BAR_PERIOD（14），与后台档位配置保持一致，不开放配置。

「波幅」还支持纳入**当前这根未收盘 K 线**的实时高低差（波幅自适应，见
波幅纳入当前K线技术方案.md）：基线仍是已收盘窗口的总跨度，实时值与之取大。
"""
from __future__ import annotations

from typing import Optional

# ATR / 波幅统计的已收盘 K 线根数
BAR_PERIOD = 14

# 指标名
METRIC_ATR = "atr"
METRIC_RANGE = "range"
METRICS = (METRIC_ATR, METRIC_RANGE)

# 各周期的秒数，用于推算指标的缓存时长
TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800, "MN": 2592000,
}


def bars_needed(metric: str) -> int:
    """算该指标需要取多少根已收盘 K 线。

    ATR 的真实波幅要用到前一根的收盘价，因此比统计根数多取一根。
    """
    return BAR_PERIOD + 1 if metric == METRIC_ATR else BAR_PERIOD


def true_range(bar: dict, prev_close: float) -> float:
    """单根 K 线的真实波幅：本根高低差、以及高/低相对前收的跳空，取最大。"""
    high = float(bar.get("high") or 0.0)
    low = float(bar.get("low") or 0.0)
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
        true_range(bar, float(bars[i - 1].get("close") or 0.0))
        for i, bar in enumerate(bars)
        if i > 0
    ]
    ranges = [r for r in ranges if r > 0]
    return sum(ranges) / len(ranges) if ranges else 0.0


def high_low_span(bars: list[dict]) -> float:
    """高低点总跨度：这段 K 线里最高的高点减最低的低点。

    与 ATR 的区别：ATR 是每根 K 线真实波幅的**平均**（逐根算再平均），这里把
    整个窗口当成一段行情，取首尾包络的极值差——反映的是「这 14 根行情走了多远」，
    而不是「单根 K 线平均波动多大」。
    """
    highs = [float(b.get("high") or 0.0) for b in bars or []]
    lows = [float(b.get("low") or 0.0) for b in bars or []]
    highs = [h for h in highs if h > 0]
    lows = [l for l in lows if l > 0]
    if not highs or not lows:
        return 0.0
    return max(highs) - min(lows)


def compute(metric: str, bars: list[dict]) -> float:
    """按指标名计算价格距离；数据不足或指标未知时返回 0。"""
    if metric == METRIC_ATR:
        return average_true_range(bars)
    if metric == METRIC_RANGE:
        return high_low_span(bars)
    return 0.0


# ======================================================================
# 实时口径：把「当前这根还没走完的 K 线」纳入统计
#
# 背景：逆势分批的加仓间距若只用「已收盘 K 线」算，急跌时大周期档位的间距
# 仍反映的是过去几十分钟的平稳行情，限价单会在下跌中位被立刻成交。
#
# 关键设计——基线与实时必须来自**同一次读取**：
# 若分开读，暴跌那根 K 线收盘的瞬间，实时值归零而基线还停留在旧缓存，
# 有效波幅会从 200 塌回 50，限价单被猛地拉回来直接成交在最低点。
# 因此这里提供「一次读取 → 切分出实时根与已收盘窗口」的一组函数。
# ======================================================================

# 一次读取时，多取几根已收盘 K 线（供 ATR 的前一根收盘价用）
LIVE_EXTRA_CLOSED = 1


def bars_needed_with_live(metric: str) -> int:
    """带实时根的一次读取需要取多少根。

    MT5 返回的数组按时间升序，最后一根才是当前正在走的这根，
    它前面那些都是已收盘的。所以总数 = 已收盘根数 + 1。
    """
    return bars_needed(metric) + 1


def split_window(bars: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """把一次读取的结果切成 (当前未收盘根, 已收盘 K 线列表)。

    约定：MT5 的 copy_rates_from_pos 返回的数组按时间**升序**排列（最老在前），
    所以最后一根才是当前正在走的这根。它前面那些都是已收盘的。
    """
    if not bars:
        return None, []
    live = bars[-1] if isinstance(bars[-1], dict) else None
    closed = [b for b in bars[:-1] if isinstance(b, dict)]
    return live, closed


def live_span(live_bar: Optional[dict]) -> float:
    """当前未收盘 K 线已走出的高低差；没有数据返回 0。"""
    if not live_bar:
        return 0.0
    high = float(live_bar.get("high") or 0.0)
    low = float(live_bar.get("low") or 0.0)
    if high <= 0 or low <= 0:
        return 0.0
    return high - low


def range_pair(bars: list[dict]) -> tuple[float, float]:
    """一次读取同时算出 (基线波幅, 实时波幅)。

    基线 = 最近 BAR_PERIOD 根已收盘 K 线的高低点总跨度（与只读已收盘时同口径）；
    实时 = 当前这根未收盘 K 线已走出的高低差。
    """
    live, closed = split_window(bars)
    baseline = high_low_span(closed[-BAR_PERIOD:])
    return baseline, live_span(live)


def effective_range(baseline: float, live: float, cap: float = 1000.0) -> tuple[float, dict]:
    """算出最终生效的波幅距离，并附带说明用的明细。

    规则：
    - 取 max(基线, 实时)：只放大不缩小，平稳时与原逻辑完全一致；
    - 上限是**数据异常的安全网**：实时波幅超过 基线 × cap 时判定为数据异常，
      直接忽略实时值、退回基线，而不是裁剪成中间值。
      裁剪出的中间值既不保守（间距仍小于真实波幅）也不激进（仍大于基线），
      是最坏的选择；退回基线至少是可解释的保守行为。
      默认 1000 倍，正常行情永远碰不到；
    - 不需要回落机制——K 线收盘时实时值并入已收盘窗口，有效波幅自然交接、连续不塌陷。
    """
    base = max(0.0, float(baseline or 0.0))
    live_value = max(0.0, float(live or 0.0))
    limit = float(cap or 0.0)
    suspicious = limit > 0 and live_value > base * limit
    effective = base if suspicious else max(base, live_value)
    return effective, {
        "baseline": base,
        "live": live_value,
        "effective": effective,
        "cap": limit,
        "adapted": effective > base,
        "clamped": suspicious,
    }


def cache_seconds(timeframe: str) -> float:
    """指标缓存时长。

    统计的是已收盘 K 线，同一根未收盘 K 线期间结果不变，所以可以按周期缓存。
    上限压到 5 分钟：大周期即便缓存过期也只是多读一次 K 线，代价远小于取到过期值。
    """
    return min(TIMEFRAME_SECONDS.get(str(timeframe or "").upper(), 300), 300)


def live_cache_seconds(refresh: float) -> float:
    """实时波幅的缓存时长 = 档位配置的重检间隔。

    实时口径要跟着行情走，缓存必须比基线短得多；这里直接用重检间隔，
    让「刷新波幅」与「允许改价」保持同一个节奏。
    下限 0.2 秒：M1 档在急跌时需要比 1 秒更细的刷新节奏，
    否则限价单来不及推开。0.2 秒也远小于采样间隔，不会把终端打满。
    """
    try:
        value = float(refresh)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.2, value)

