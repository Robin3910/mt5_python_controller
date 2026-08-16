"""券商 MT5 服务器时区偏移推断。"""
from mt5_client import infer_server_time_offset


def test_infer_offset_ic_markets_gmt3():
    now = 1786893918.8
    tick = now + 10796.18  # BTCUSD tick.time 比真实 UTC 快约 3 小时
    assert infer_server_time_offset([tick], now) == 3 * 3600


def test_infer_offset_skips_stale_weekend_fx():
    now = 1786893918.8
    stale = now - 39.47 * 3600  # 周日看到的周五收盘 EURUSD
    assert infer_server_time_offset([stale], now) is None


def test_infer_offset_utc_broker_is_zero():
    now = 1_000.0
    assert infer_server_time_offset([now + 2], now) == 0


def test_infer_offset_uses_fresh_when_mixed_with_stale():
    now = 1_000.0
    stale = now - 20 * 3600
    fresh = now + 2 * 3600 + 30
    assert infer_server_time_offset([stale, fresh], now) == 2 * 3600


def test_infer_offset_empty():
    assert infer_server_time_offset([], 1_000.0) is None
