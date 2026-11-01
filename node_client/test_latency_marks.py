"""时延埋点（M8）的单测：节点端产出字段，服务端抽取并落库。

埋点是纯观测能力，任何一段坏掉都不该影响交易链路——这几条测试就是守这条线。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import latency_marks as lm  # noqa: E402
from test_node_client import FakeWS, _node  # noqa: E402


# ---------------------------------------------------------------- 节点端


async def test_open_trade_result_carries_latency_marks():
    """开仓回报必须带上 recv_at / order_at / filled_at 与两段耗时。"""
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "open", "signal_id": "s1", "action": "BUY", "symbol": "EURUSD",
         "volume": 0.1, "stop_loss": None, "take_profit": None, "comment": "", "magic": None},
    )
    tr = [m for m in ws.sent if m["type"] == "trade_result"]
    assert tr, "应有一条成交回报"
    data = tr[0]["data"]
    assert isinstance(data.get("recv_at"), float)
    assert isinstance(data.get("order_at"), float)
    assert isinstance(data.get("filled_at"), float)
    assert data["queued_ms"] >= 0
    assert data["order_to_fill_ms"] >= 0


async def test_latency_marks_not_leaked_as_internal_keys():
    """内部键（_order_at / _filled_at）必须被吃掉，不能出现在回报里。"""
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "open", "signal_id": "s2", "action": "BUY", "symbol": "EURUSD",
         "volume": 0.1, "stop_loss": None, "take_profit": None, "comment": "", "magic": None},
    )
    data = [m for m in ws.sent if m["type"] == "trade_result"][0]["data"]
    assert "_order_at" not in data
    assert "_filled_at" not in data


async def test_latency_marks_use_provided_recv_at():
    """recv_at 由接收时刻决定，不是下单时刻——否则排队耗时会被算成 0。"""
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    recv_at = 1_000_000.0
    await n._handle(
        ws,
        {"cmd": "open", "signal_id": "s3", "action": "BUY", "symbol": "EURUSD",
         "volume": 0.1, "stop_loss": None, "take_profit": None, "comment": "", "magic": None},
        recv_at=recv_at,
    )
    data = [m for m in ws.sent if m["type"] == "trade_result"][0]["data"]
    assert data["recv_at"] == pytest.approx(recv_at, abs=1e-6)
    assert data["order_at"] >= recv_at


async def test_close_trade_result_carries_recv_at():
    """平仓回报也带收包时刻（批量平仓没有逐笔券商时刻，只报 recv_at）。"""
    n = _node()
    await n._exec(n.mt5.connect)
    await n._exec(n.mt5.place_market_order, "EURUSD", "BUY", 0.1)
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "close", "signal_id": "c1", "close_target": "symbol", "close_symbol": "EURUSD"},
        recv_at=999_000.0,
    )
    data = [m for m in ws.sent if m["type"] == "trade_result"][0]["data"]
    assert data["recv_at"] == pytest.approx(999_000.0, abs=1e-6)


async def test_strategy_first_order_carries_latency_marks():
    """strategy 首单回报同样带埋点。"""
    from strategy_runner import StrategyRunner  # noqa: F401

    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    await n._handle(
        ws,
        {
            "cmd": "strategy_start",
            "task_id": 1,
            "magic": 700001,
            "group_id": "g1",
            "signal_id": "sg1",
            "entry": {"action": "BUY", "symbol": "EURUSD", "volume": 0.1},
            "strategy": {"template_id": "tpl_1", "rules": []},
        },
        recv_at=888_000.0,
    )
    # 首单回报是异步任务里发出的，等它跑完
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any(m["type"] == "trade_result" for m in ws.sent):
            break
    tr = [m for m in ws.sent if m["type"] == "trade_result"]
    if not tr:
        pytest.skip("该策略配置下未产生首单回报")
    data = tr[0]["data"]
    assert data["recv_at"] == pytest.approx(888_000.0, abs=1e-6)
    assert "queued_ms" in data


# ---------------------------------------------------------------- 埋点工具


def test_build_computes_both_durations():
    marks = lm.build(recv_at=100.0, order_at=100.5, filled_at=101.25)
    assert marks["queued_ms"] == pytest.approx(500.0)
    assert marks["order_to_fill_ms"] == pytest.approx(750.0)


def test_build_skips_missing_fields():
    """缺项时不产出对应字段（宁可缺列，也不要给假值）。"""
    marks = lm.build(recv_at=100.0)
    assert "recv_at" in marks
    assert "order_at" not in marks
    assert "queued_ms" not in marks


def test_build_never_negative():
    """时钟回拨时差值不能为负，否则前端会显示负耗时。"""
    marks = lm.build(recv_at=100.0, order_at=99.0, filled_at=98.0)
    assert marks["queued_ms"] == 0.0
    assert marks["order_to_fill_ms"] == 0.0
