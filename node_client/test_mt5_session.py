"""Tests for reusing an already-logged-in MT5 terminal session."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import mt5_client as mc
from mt5_prompt import prompt_mt5_credentials


def test_peek_logged_in_account(monkeypatch):
    fake = MagicMock()
    fake.initialize.return_value = True
    fake.account_info.return_value = SimpleNamespace(login=60108484, server="Demo-Server")
    monkeypatch.setattr(mc, "mt5", fake)

    got = mc.peek_logged_in_account(r"C:\MT5\terminal64.exe")
    assert got == {"login": 60108484, "server": "Demo-Server"}
    fake.initialize.assert_called_once_with(path=r"C:\MT5\terminal64.exe")
    fake.shutdown.assert_called_once()


def test_peek_not_logged_in(monkeypatch):
    fake = MagicMock()
    fake.initialize.return_value = True
    fake.account_info.return_value = None
    monkeypatch.setattr(mc, "mt5", fake)

    assert mc.peek_logged_in_account(r"C:\MT5\terminal64.exe") is None
    fake.shutdown.assert_called_once()


def test_connect_reuses_session_without_login(monkeypatch):
    fake = MagicMock()
    fake.initialize.return_value = True
    fake.account_info.return_value = SimpleNamespace(login=42, server="S1")
    monkeypatch.setattr(mc, "mt5", fake)

    client = mc.MT5Client(
        42, "", "S1", path=r"C:\MT5\terminal64.exe", reuse_terminal_session=True,
    )
    assert client.connect() is True
    fake.login.assert_not_called()
    assert client.login == 42


def test_prompt_reuses_session(monkeypatch):
    monkeypatch.setattr("mt5_prompt.get_settings", lambda: SimpleNamespace(mt5_mock=False))
    monkeypatch.setattr(
        "mt5_prompt.discover_mt5_terminal",
        lambda: type("P", (), {"__str__": lambda self: r"C:\MT5\terminal64.exe"})(),
    )
    monkeypatch.setattr(
        "mt5_client.peek_logged_in_account",
        lambda path: {"login": 12345, "server": "Broker-Demo"},
    )

    creds = prompt_mt5_credentials()
    assert creds["mt5_login"] == 12345
    assert creds["mt5_password"] == ""
    assert creds["mt5_server"] == "Broker-Demo"
    assert creds["reuse_terminal_session"] is True


@pytest.mark.parametrize("session", [None, {}, {"login": 0}, {"login": True}, {"login": 54321}])
def test_dashboard_prompt_rejects_missing_or_other_account_without_input(monkeypatch, session):
    monkeypatch.setattr(
        "mt5_prompt.get_settings",
        lambda: SimpleNamespace(mt5_mock=False, dashboard_expected_mt5_login=12345),
    )
    monkeypatch.setattr("mt5_prompt.discover_mt5_terminal", lambda: r"C:\MT5\terminal64.exe")
    monkeypatch.setattr(mc, "peek_logged_in_account", lambda path: session)
    interactive = MagicMock(side_effect=AssertionError("面板启动不能交互输入账号"))
    monkeypatch.setattr("builtins.input", interactive)

    with pytest.raises(mc.MT5Error, match="面板启动失败"):
        prompt_mt5_credentials()
    interactive.assert_not_called()


def test_dashboard_prompt_reuses_only_expected_account(monkeypatch):
    monkeypatch.setattr(
        "mt5_prompt.get_settings",
        lambda: SimpleNamespace(mt5_mock=False, dashboard_expected_mt5_login=12345),
    )
    monkeypatch.setattr("mt5_prompt.discover_mt5_terminal", lambda: r"C:\MT5\terminal64.exe")
    monkeypatch.setattr(mc, "peek_logged_in_account", lambda path: {"login": 12345, "server": "Demo"})
    creds = prompt_mt5_credentials()
    assert creds["mt5_login"] == 12345
    assert creds["reuse_terminal_session"] is True
    assert creds["mt5_password"] == ""


def test_independent_prompt_still_accepts_interactive_login(monkeypatch):
    monkeypatch.setattr("mt5_prompt.get_settings", lambda: SimpleNamespace(mt5_mock=False))
    monkeypatch.setattr("mt5_prompt.discover_mt5_terminal", lambda: r"C:\MT5\terminal64.exe")
    monkeypatch.setattr(mc, "peek_logged_in_account", lambda path: None)
    answers = iter(["12345", "example-password", "Demo"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    creds = prompt_mt5_credentials()
    assert creds["mt5_login"] == 12345
    assert creds["reuse_terminal_session"] is False
