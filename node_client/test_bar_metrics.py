"""K 线统计指标的单元测试（纯计算，无需 MT5）。"""
import bar_metrics as bm


def bar(high: float, low: float, close: float) -> dict:
    return {"time": 0.0, "open": (high + low) / 2, "high": high, "low": low, "close": close}


# --------------------------- 取数根数 ---------------------------
def test_atr_needs_one_extra_bar():
    """真实波幅要用前一根的收盘价，所以比统计根数多取一根。"""
    assert bm.bars_needed(bm.METRIC_ATR) == bm.BAR_PERIOD + 1
    assert bm.bars_needed(bm.METRIC_RANGE) == bm.BAR_PERIOD


# --------------------------- 真实波幅 ---------------------------
def test_true_range_uses_high_low_when_no_gap():
    assert bm.true_range(bar(11, 9, 10), prev_close=10) == 2


def test_true_range_covers_gap_against_prev_close():
    """跳空时高低差不足以描述波动，要用相对前收的距离。"""
    assert bm.true_range(bar(15, 14, 14.5), prev_close=9) == 6


def test_true_range_without_prev_close_falls_back_to_span():
    assert bm.true_range(bar(11, 9, 10), prev_close=0) == 2


# --------------------------- ATR ---------------------------
def test_average_true_range_averages_all_but_first_bar():
    bars = [bar(10, 8, 9), bar(11, 9, 10), bar(12, 10, 11)]
    assert bm.average_true_range(bars) == 2


def test_average_true_range_needs_at_least_two_bars():
    assert bm.average_true_range([bar(10, 8, 9)]) == 0.0
    assert bm.average_true_range([]) == 0.0


# --------------------------- 最大波幅 ---------------------------
def test_max_range_picks_widest_bar():
    bars = [bar(10, 8, 9), bar(15, 14, 14.5), bar(20, 12, 16)]
    assert bm.max_range(bars) == 8


def test_max_range_ignores_empty_bars():
    assert bm.max_range([]) == 0.0


# --------------------------- 分发与缓存时长 ---------------------------
def test_compute_dispatches_by_metric():
    bars = [bar(10, 8, 9), bar(11, 9, 10)]
    assert bm.compute(bm.METRIC_ATR, bars) == 2
    assert bm.compute(bm.METRIC_RANGE, bars) == 2
    assert bm.compute("unknown", bars) == 0.0


def test_cache_seconds_capped_at_five_minutes():
    """大周期也不缓存太久：过期只是多读一次 K 线，取到过期值代价更大。"""
    assert bm.cache_seconds("M1") == 60
    assert bm.cache_seconds("M5") == 300
    assert bm.cache_seconds("D1") == 300
    assert bm.cache_seconds("unknown") == 300
