"""节点配置：账户上报间隔支持小数与下限。"""
import pytest
from pydantic import ValidationError

from config import (
    ACCOUNT_REPORT_INTERVAL_DEFAULT,
    ACCOUNT_REPORT_INTERVAL_MIN,
    NodeSettings,
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


def test_dashboard_expected_login_from_environment(monkeypatch):
    monkeypatch.setenv("DASHBOARD_EXPECTED_MT5_LOGIN", "12345")
    assert NodeSettings(_env_file=None).dashboard_expected_mt5_login == 12345


@pytest.mark.parametrize("value", ["", "0", "-1", "other-account"])
def test_dashboard_expected_login_rejects_invalid_environment(monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_EXPECTED_MT5_LOGIN", value)
    with pytest.raises(ValidationError):
        NodeSettings(_env_file=None)
