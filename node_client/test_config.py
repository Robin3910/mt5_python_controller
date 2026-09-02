"""节点配置：账户上报间隔支持小数与下限。"""
from config import (
    ACCOUNT_REPORT_INTERVAL_DEFAULT,
    ACCOUNT_REPORT_INTERVAL_MIN,
    clamp_account_report_interval,
)


def test_clamp_account_report_interval_accepts_decimals():
    assert clamp_account_report_interval(0.5) == 0.5
    assert clamp_account_report_interval("0.25") == 0.25
    assert clamp_account_report_interval(1) == 1.0
    assert clamp_account_report_interval(5) == 5.0


def test_clamp_account_report_interval_floor_and_fallback():
    assert clamp_account_report_interval(0.01) == ACCOUNT_REPORT_INTERVAL_MIN
    assert clamp_account_report_interval(0) == ACCOUNT_REPORT_INTERVAL_DEFAULT
    assert clamp_account_report_interval(-1) == ACCOUNT_REPORT_INTERVAL_DEFAULT
    assert clamp_account_report_interval(None) == ACCOUNT_REPORT_INTERVAL_DEFAULT
    assert clamp_account_report_interval("x") == ACCOUNT_REPORT_INTERVAL_DEFAULT
