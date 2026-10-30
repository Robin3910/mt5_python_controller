"""回归：成交采集窗口必须落在 MT5 服务器钟面域。

背景（线上真实故障）：
MT5 的 `deal.time` 是「服务器钟面按 UTC 编码」的 Unix（IC Markets GMT+3 时比
真实 UTC 快 10800 秒），而 `history_deals_get(start, end)` 收的 naive datetime
底层按 `mktime()`（本机本地时区墙钟）解释。

旧实现用真实 UTC 直接当窗口边界，于是：
1. 上界 `end` 比服务器钟面慢 offset 秒 -> 最近 offset 秒的成交被整段截掉；
2. 游标推进到 `deal.time`（服务器钟面域）后，`start > end` -> 采集**永久停滞**，
   节点再也上报不了新成交，前端概览的数据从此不再更新。

这里的假 MT5 **忠实还原 MT5 的窗口语义**：把 naive datetime 按本机墙钟转成
epoch，再与 `deal.time`（服务器钟面）比较，只有落在 [start, end] 内的成交才返回。
这样测试才能真正区分「窗口对/错」，而不是恒定返回固定列表。

注意：旧实现用的是 `datetime.now()`（真实墙钟），所以本文件**不冻结 time.time()** ——
冻结了就测不出旧 bug。
"""
import time

import pytest

import mt5_client as mc

# —— 假 MT5：按 MT5 的真实语义筛窗口 ——


class _FakeDeal:
    """最小成交对象：只提供 history_deals_since 用到的字段。"""

    def __init__(self, ticket: int, t: float):
        self.ticket = ticket
        self.time = t
        self.order = 0
        self.position_id = 0
        self.symbol = "XAUUSD"
        self.entry = 0
        self.type = 0
        self.reason = 0
        self.volume = 0.1
        self.price = 2400.0
        self.profit = 0.0
        self.swap = 0.0
        self.commission = 0.0
        self.magic = 0
        self.comment = ""


class FakeMT5:
    """忠实模拟 history_deals_get 的窗口过滤。"""

    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_INOUT = 2
    DEAL_ENTRY_OUT_BY = 3
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self, deals: list[_FakeDeal]):
        self._deals = deals
        self.calls: list[tuple[float, float]] = []

    def history_deals_get(self, start, end):
        # MT5 收到 naive datetime，按本机本地时区墙钟解释成 epoch
        s = start.timestamp()
        e = end.timestamp()
        self.calls.append((s, e))
        # start > end 时 MT5 返回空
        if s > e:
            return []
        return [d for d in self._deals if s <= d.time <= e]

    @staticmethod
    def last_error():
        return (1, "Success")


def _client():
    """构造一个只关心采集窗口逻辑的 MT5Client（绕过连接断言）。"""
    c = mc.MT5Client.__new__(mc.MT5Client)
    c.ensure = lambda: None  # type: ignore[method-assign]
    c._server_tz_cache = (0.0, None)
    return c


# —— 基础换算 ——


def test_server_epoch_shifts_by_offset():
    assert mc.server_epoch(1_000.0, 3 * 3600) == 1_000.0 + 3 * 3600
    # 偏移为 None / 0 时按 UTC 处理
    assert mc.server_epoch(1_000.0, None) == 1_000.0
    assert mc.server_epoch(1_000.0, 0) == 1_000.0


def test_history_window_end_is_in_server_clock_domain(monkeypatch):
    """上界必须换算到服务器钟面域：否则最新成交会被截掉。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)
    offset = 3 * 3600
    start, end = mc.history_window(now - 3600, offset, end_margin_sec=0)
    # end 的墙钟 epoch = 真实 now + offset
    assert end.timestamp() == pytest.approx(now + offset)
    # 起点在服务器钟面域直接用，不做换算（与游标同域）
    assert start.timestamp() == pytest.approx(now - 3600)


def test_history_window_end_margin_widens_upper_bound(monkeypatch):
    """余量让上界更宽：即使偏移推断不出来也不会切掉真实成交。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)
    _, end_plain = mc.history_window(None, None, end_margin_sec=0)
    _, end_margin = mc.history_window(None, None, end_margin_sec=mc._HISTORY_END_MARGIN_SEC)
    assert end_margin.timestamp() - end_plain.timestamp() == pytest.approx(
        mc._HISTORY_END_MARGIN_SEC
    )


def test_history_window_back_off_widens_lower_bound(monkeypatch):
    """重叠窗口：起点再往前放宽 back_sec。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)
    start_plain, _ = mc.history_window(now, 0, back_sec=0)
    start_back, _ = mc.history_window(now, 0, back_sec=120)
    assert start_plain.timestamp() - start_back.timestamp() == pytest.approx(120)


def test_history_window_none_since_defaults_to_seven_days(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)
    start, end = mc.history_window(None, 0)
    assert (end.timestamp() - start.timestamp()) == pytest.approx(7 * 86400)


def test_history_window_margin_is_adaptive(monkeypatch):
    """余量自适应：偏移已知用小余量，推断不出来才用 14 小时兜底。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)

    _, end_known = mc.history_window(None, 3 * 3600)
    _, end_unknown = mc.history_window(None, None)

    assert end_known.timestamp() == pytest.approx(now + 3 * 3600 + mc._HISTORY_END_MARGIN_KNOWN_SEC)
    assert end_unknown.timestamp() == pytest.approx(now + mc._HISTORY_END_MARGIN_SEC)
    # 已知偏移时余量更小（少扫无用数据），未知时兜底更大（不漏读）
    assert mc._HISTORY_END_MARGIN_KNOWN_SEC < mc._HISTORY_END_MARGIN_SEC


# —— 核心回归：窗口能覆盖到最新成交 ——


def test_history_deals_since_window_reaches_latest_deal(monkeypatch):
    """核心回归：一笔「刚刚成交」的成交必须能被采到。

    旧实现在 GMT+3 经纪商上采不到 —— 上界比服务器钟面慢 3 小时，
    这笔成交落在窗口之外，被整段丢掉。
    """
    now = time.time()
    offset = 3 * 3600
    # 这笔成交发生在「服务器钟面的现在」附近
    deal = _FakeDeal(999, now + offset - 60)
    fake = FakeMT5([deal])
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: offset)

    rows = c.history_deals_since(now - 3600, 0)
    assert len(rows) == 1, "最近成交被窗口截断了"
    assert rows[0]["deal_id"] == 999


def test_history_deals_since_window_upper_bound_covers_server_now(monkeypatch):
    """直接锁定不变量：窗口上界（服务器钟面域）必须 >= 服务器钟面的「现在」。"""
    now = time.time()
    offset = 3 * 3600
    fake = FakeMT5([])
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: offset)

    c.history_deals_since(now - 3600, 0)
    _, end_epoch = fake.calls[-1]
    assert end_epoch >= now + offset, "上界没换算到服务器钟面域"


def test_history_deals_since_steady_state_does_not_stall(monkeypatch):
    """稳态回归：游标在服务器钟面域时，start 不能大于 end。

    旧实现在这里 start > end -> history_deals_get 返回空 -> 游标不再推进，
    节点从此静默，前端概览数据永远停在旧值。
    """
    now = time.time()
    offset = 3 * 3600
    fake = FakeMT5([_FakeDeal(1001, now + offset)])
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: offset)

    # 游标 = 上一轮的最大 deal.time（服务器钟面域）
    cursor_ts = now + offset - 30
    rows = c.history_deals_since(cursor_ts, 1000)
    assert len(rows) == 1, "稳态下必须还能采到新成交，否则采集永久停滞"
    # 窗口两端顺序正确
    s, e = fake.calls[-1]
    assert s < e


def test_history_deals_since_read_failure_raises(monkeypatch):
    """读失败必须抛错（不能返回空列表），否则游标会误推、历史永久丢失。"""
    now = time.time()

    class FailingMT5(FakeMT5):
        def history_deals_get(self, start, end):
            return None

        @staticmethod
        def last_error():
            return (-1, "IPC timeout")

    monkeypatch.setattr(mc, "mt5", FailingMT5([]))
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: 0)

    with pytest.raises(mc.MT5Error):
        c.history_deals_since(now - 3600, 0)


def test_history_deals_since_works_without_offset(monkeypatch):
    """偏移推断不出来（周末报价不跳）时退化成 UTC 处理，仍能采到成交。"""
    now = time.time()
    fake = FakeMT5([_FakeDeal(2001, now - 10)])
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: None)

    rows = c.history_deals_since(now - 3600, 0)
    assert len(rows) == 1


def test_history_deals_returns_server_clock_time(monkeypatch):
    """上报给服务端的 `time` 是服务器钟面域 —— 与 deal.time 一致，不做换算。"""
    now = time.time()
    offset = 3 * 3600
    server_ts = now + offset - 5
    fake = FakeMT5([_FakeDeal(3001, server_ts)])
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: offset)

    rows = c.history_deals_since(now - 3600, 0)
    assert rows[0]["time"] == server_ts


def test_history_deals_covers_recent_exit_deals(monkeypatch):
    """策略收口算已实现盈亏时，最近的出场成交也必须能读到。

    旧实现在 GMT+3 经纪商上读不到最近 3 小时的成交，
    收口上报的 realized_profit 会偏小甚至为 0。
    """
    now = time.time()
    offset = 3 * 3600
    exits = [_FakeDeal(4001, now + offset - 300), _FakeDeal(4002, now + offset - 100)]
    fake = FakeMT5(exits)
    monkeypatch.setattr(mc, "mt5", fake)
    c = _client()
    monkeypatch.setattr(c, "server_time_offset_sec", lambda: offset)

    deals = c._history_deals(now - 3600)
    assert len(deals) == 2, "最近 1 小时的出场成交被窗口截断了"
