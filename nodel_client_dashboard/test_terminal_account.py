"""导入账号：填写优先，留空才读终端，失败时不申请。"""
from unittest.mock import MagicMock

import pytest

import terminal_account as ta
from terminal_account import TerminalAccountError, enroll_import_login, read_terminal_account, resolve_import_login


def test_typed_login_wins_and_does_not_read_terminal():
    reader = MagicMock(side_effect=AssertionError("不应读取终端"))
    enroll = MagicMock()
    assert enroll_import_login("60108484", reader, enroll) == (60108484, "")
    reader.assert_not_called()
    enroll.assert_called_once_with(60108484, "")


def test_blank_login_reads_terminal_then_enrolls():
    reader = MagicMock(return_value={"login": 88, "server": "Demo-Server"})
    enroll = MagicMock()
    assert resolve_import_login("  ", reader) == (88, "Demo-Server")
    assert enroll_import_login("", reader, enroll) == (88, "Demo-Server")
    enroll.assert_called_once_with(88, "Demo-Server")


def test_blank_login_does_not_enroll_when_terminal_is_not_logged_in():
    enroll = MagicMock()
    with pytest.raises(TerminalAccountError, match="未登录"):
        enroll_import_login("", lambda: None, enroll)
    enroll.assert_not_called()


def test_blank_login_does_not_enroll_when_read_fails():
    enroll = MagicMock()

    def boom():
        raise TerminalAccountError("无法连接 MT5 终端")

    with pytest.raises(TerminalAccountError, match="无法连接"):
        enroll_import_login("", boom, enroll)
    enroll.assert_not_called()


def test_invalid_login_does_not_enroll():
    enroll = MagicMock()
    with pytest.raises(TerminalAccountError, match="正整数"):
        enroll_import_login("0", MagicMock(), enroll)
    with pytest.raises(TerminalAccountError, match="正整数"):
        enroll_import_login("abc", MagicMock(), enroll)
    enroll.assert_not_called()


def test_read_terminal_account_requires_terminal_exe(tmp_path):
    with pytest.raises(TerminalAccountError, match="没有 MT5 终端"):
        read_terminal_account(tmp_path)


def test_read_terminal_account_reports_logged_out(tmp_path, monkeypatch):
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")

    class FakeMT5:
        def initialize(self, path):
            assert path.endswith("terminal64.exe")
            return True

        def account_info(self):
            return None

        def shutdown(self):
            self.closed = True

        def last_error(self):
            return (1, "not logged in")

    fake = FakeMT5()
    monkeypatch.setattr(ta, "_load_mt5", lambda: fake)
    with pytest.raises(TerminalAccountError, match="未登录"):
        read_terminal_account(tmp_path)
    assert fake.closed is True
