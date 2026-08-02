"""K 线统计指标（纯计算，无 I/O，可单独单元测试）。

供分批加仓档位的「ATR」与「波幅」两种间距计算方式使用：两者都把一段已收盘
K 线折算成一个**价格距离**，再由判定层换算成点数与实际偏离比较。

统计根数固定为 BAR_PERIOD（14），与后台档位配置保持一致，不开放配置。
"""
from __future__ import annotations

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


def max_range(bars: list[dict]) -> float:
    """最大波幅：这段 K 线里单根高低差的最大值。"""
    spans = [
        float(b.get("high") or 0.0) - float(b.get("low") or 0.0)
        for b in bars or []
    ]
    spans = [s for s in spans if s > 0]
    return max(spans) if spans else 0.0


def compute(metric: str, bars: list[dict]) -> float:
    """按指标名计算价格距离；数据不足或指标未知时返回 0。"""
    if metric == METRIC_ATR:
        return average_true_range(bars)
    if metric == METRIC_RANGE:
        return max_range(bars)
    return 0.0


def cache_seconds(timeframe: str) -> float:
    """指标缓存时长。

    统计的是已收盘 K 线，同一根未收盘 K 线期间结果不变，所以可以按周期缓存。
    上限压到 5 分钟：大周期即便缓存过期也只是多读一次 K 线，代价远小于取到过期值。
    """
    return min(TIMEFRAME_SECONDS.get(str(timeframe or "").upper(), 300), 300)
