"""时延埋点（M8）的服务端侧单测：抽取、落库口径与协议字段。

埋点是纯观测能力，任何一段坏掉都不该影响交易链路——这几条测试就是守这条线。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import latency_marks as slm
from app.group_dispatcher import build_strategy_start_command
from app.models import build_open_command
from app.parser import TradingSignal


# ---------------------------------------------------------------- 抽取口径


def test_extract_reads_node_marks():
    out = slm.extract({
        "recv_at": 1_700_000_000.5,
        "order_at": 1_700_000_000.7,
        "filled_at": 1_700_000_001.0,
        "queued_ms": 200.0,
        "order_to_fill_ms": 300.0,
    })
    assert isinstance(out["recv_at"], datetime)
    assert isinstance(out["order_at"], datetime)
    assert isinstance(out["filled_at"], datetime)
    assert out["queued_ms"] == pytest.approx(200.0)
    assert out["order_to_fill_ms"] == pytest.approx(300.0)


def test_extract_tolerates_bad_values():
    """坏值一律当作缺失——埋点坏了不能连累交易链路。"""
    out = slm.extract({
        "recv_at": "not-a-number",
        "order_at": None,
        "filled_at": -1,
        "queued_ms": "abc",
    })
    assert out["recv_at"] is None
    assert out["order_at"] is None
    assert out["filled_at"] is None
    assert out["queued_ms"] is None


def test_extract_empty_result():
    out = slm.extract({})
    assert all(v is None for v in out.values())


def test_extract_missing_key_returns_none():
    """节点没上报埋点时（旧版节点客户端），全部为 None 而不是抛错。"""
    out = slm.extract({"success": True, "price": 1.2345})
    assert all(v is None for v in out.values())


# ---------------------------------------------------------------- 协议字段


def test_open_command_carries_sent_at():
    """open 命令带 sent_at；缺省时不带该键（旧节点行为不变）。"""
    cmd = build_open_command(
        "s1", "BUY", "EURUSD", 0.1, None, None, "", 123, sent_at=1_700_000_000.25,
    )
    assert cmd["sent_at"] == pytest.approx(1_700_000_000.25, abs=1e-6)

    plain = build_open_command("s1", "BUY", "EURUSD", 0.1, None, None, "", 123)
    assert "sent_at" not in plain


def test_strategy_start_command_carries_sent_at():
    signal = TradingSignal(action="BUY", symbol="EURUSD", volume=0.1)
    cmd = build_strategy_start_command(
        "s1", signal, 0.1, 1, "g1", {}, sent_at=1_700_000_000.5,
    )
    assert cmd["sent_at"] == pytest.approx(1_700_000_000.5, abs=1e-6)

    plain = build_strategy_start_command("s1", signal, 0.1, 1, "g1", {})
    assert "sent_at" not in plain
