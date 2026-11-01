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


# --------------------------- 高低点总跨度 ---------------------------
def test_high_low_span_covers_whole_window():
    """总跨度 = 最高的高点 − 最低的低点，跨整段窗口。"""
    bars = [bar(10, 8, 9), bar(15, 14, 14.5), bar(20, 12, 16)]
    assert bm.high_low_span(bars) == 12          # 最高 20 − 最低 8


def test_high_low_span_ignores_empty_bars():
    assert bm.high_low_span([]) == 0.0
    assert bm.high_low_span([bar(0, 0, 0)]) == 0.0


# --------------------------- 分发与缓存时长 ---------------------------
def test_compute_dispatches_by_metric():
    bars = [bar(10, 8, 9), bar(11, 9, 10)]
    assert bm.compute(bm.METRIC_ATR, bars) == 2
    assert bm.compute(bm.METRIC_RANGE, bars) == 3      # 最高 11 − 最低 8
    assert bm.compute("unknown", bars) == 0.0


def test_cache_seconds_capped_at_five_minutes():
    """大周期也不缓存太久：过期只是多读一次 K 线，取到过期值代价更大。"""
    assert bm.cache_seconds("M1") == 60
    assert bm.cache_seconds("M5") == 300
    assert bm.cache_seconds("D1") == 300
    assert bm.cache_seconds("unknown") == 300


# --------------------------- 实时口径 ---------------------------
def test_live_span_uses_current_bar():
    assert bm.live_span({"high": 10.0, "low": 8.0}) == 2.0
    assert bm.live_span(None) == 0.0
    assert bm.live_span({"high": 0.0, "low": 0.0}) == 0.0


def test_split_window_separates_live_from_closed():
    """一次读取的顺序是时间升序（最老在前），最后一根才是当前这根。"""
    live, closed = bm.split_window([
        {"high": 11, "low": 8}, {"high": 12, "low": 9}, {"high": 20, "low": 10},
    ])
    assert live == {"high": 20, "low": 10}
    assert closed == [{"high": 11, "low": 8}, {"high": 12, "low": 9}]


def test_split_window_handles_empty():
    live, closed = bm.split_window([])
    assert live is None
    assert closed == []


def test_range_pair_returns_baseline_and_live():
    """基线取 14 根已收盘的总跨度；实时取当前这根的高低差。"""
    bars = [{"high": 11.0, "low": 8.0}] * 14           # 已收盘：包络 3
    bars += [{"high": 20.0, "low": 10.0}]              # 实时根：10
    baseline, live = bm.range_pair(bars)
    assert baseline == 3.0
    assert live == 10.0


def test_range_pair_baseline_ignores_extra_bars():
    """已收盘超过 14 根时只取最近 14 根（最老的被挤出窗口）。"""
    bars = [{"high": 500.0, "low": 500.0}]             # 最老的一根，不该进窗口
    bars += [{"high": 11.0, "low": 8.0}] * 14
    bars += [{"high": 20.0, "low": 10.0}]              # 实时根
    baseline, live = bm.range_pair(bars)
    assert baseline == 3.0
    assert live == 10.0


def test_bars_needed_with_live_adds_one_root():
    """带实时根要多取一根：最后一根是当前这根，前面才是已收盘的。"""
    assert bm.bars_needed_with_live(bm.METRIC_RANGE) == bm.BAR_PERIOD + 1
    assert bm.bars_needed_with_live(bm.METRIC_ATR) == bm.BAR_PERIOD + 2


def test_effective_range_takes_live_when_larger():
    effective, info = bm.effective_range(baseline=30.0, live=200.0, cap=1000.0)
    assert effective == 200.0
    assert info["adapted"] is True
    assert info["clamped"] is False


def test_effective_range_keeps_baseline_when_live_is_smaller():
    effective, info = bm.effective_range(baseline=30.0, live=10.0, cap=1000.0)
    assert effective == 30.0
    assert info["adapted"] is False


def test_effective_range_ignores_suspicious_live():
    """实时波幅超过 基线 × cap 时判为数据异常：忽略实时值、退回基线。

    不是裁剪成中间值——中间值既不保守（间距仍小于真实波幅）
    也不激进（仍大于基线），是最坏的选择。
    """
    effective, info = bm.effective_range(baseline=30.0, live=5000.0, cap=50.0)
    assert effective == 30.0
    assert info["clamped"] is True
    assert info["adapted"] is False


def test_effective_range_does_not_clamp_normal_crash():
    """正常急跌不该被上限挡掉，否则「跟随更大波幅」就失效了。

    场景：平稳时基线 30 点，暴跌时实时波幅 6000 点（200 倍）——
    这是真实行情，默认上限（1000 倍）不该把它挡下来。
    """
    effective, info = bm.effective_range(baseline=30.0, live=6000.0, cap=1000.0)
    assert effective == 6000.0
    assert info["clamped"] is False
    assert info["adapted"] is True


def test_effective_range_hands_over_at_bar_close():
    """bar 收盘瞬间：实时归零、基线并入这根，有效波幅连续不塌陷。"""
    before = bm.effective_range(baseline=30.0, live=200.0, cap=1000.0)[0]
    after = bm.effective_range(baseline=200.0, live=0.0, cap=1000.0)[0]
    assert before == after == 200.0


def test_effective_range_never_below_baseline():
    effective, _ = bm.effective_range(baseline=30.0, live=0.0, cap=1000.0)
    assert effective == 30.0


def test_live_cache_seconds_follows_refresh_interval():
    assert bm.live_cache_seconds(5) == 5.0
    assert bm.live_cache_seconds(10) == 10.0
    assert bm.live_cache_seconds(0.5) == 0.5
    assert bm.live_cache_seconds(0) == 0.2   # 下限 0.2 秒
    assert bm.live_cache_seconds(None) == 0.2
