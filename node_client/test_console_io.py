"""控制台编码兜底测试：cp1252 控制台下打印中文不能把节点打挂。"""
import io
import logging
import sys

import console_io
from mt5_prompt import prompt_mt5_credentials


class _Cp1252Stream(io.TextIOWrapper):
    """模拟 Windows 管道重定向下的 cp1252 标准流。"""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="cp1252", errors="strict", newline="")


def test_configure_console_switches_streams_to_utf8():
    console_io._CONFIGURED = False
    out = _Cp1252Stream()
    err = _Cp1252Stream()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        console_io.configure_console()
        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
        assert sys.stderr.encoding.lower().replace("-", "") == "utf8"
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        console_io._CONFIGURED = False


def test_safe_print_survives_cp1252_stream():
    console_io._CONFIGURED = False
    out = _Cp1252Stream()
    old_out = sys.stdout
    sys.stdout = out
    try:
        console_io.safe_print("已定位 MT5 终端：C:\\mt5_3")
    finally:
        sys.stdout = old_out
        console_io._CONFIGURED = False


def test_safe_print_falls_back_when_stream_rejects_chinese():
    console_io._CONFIGURED = True  # 跳过 reconfigure，模拟流不可改编码

    class _Strict:
        def write(self, _data: str) -> int:
            raise UnicodeEncodeError("charmap", "中", 0, 1, "undefined")

        def flush(self) -> None:
            pass

    old_out = sys.stdout
    sys.stdout = _Strict()
    try:
        console_io.safe_print("终端当前未登录，请输入账号信息：")
    finally:
        sys.stdout = old_out


def test_prompt_mock_path_prints_chinese_without_unicode_error(monkeypatch, capsys):
    monkeypatch.setattr(
        "mt5_prompt.get_settings",
        lambda: type("S", (), {"mt5_mock": True, "dashboard_expected_mt5_login": None})(),
    )
    creds = prompt_mt5_credentials()
    assert creds["mt5_login"] == 90000001
    assert "MT5_MOCK" in capsys.readouterr().out


def test_logging_chinese_to_cp1252_stderr_does_not_raise():
    """logging 走 stderr；cp1252 下中文日志不能抛异常把节点打挂。"""
    console_io._CONFIGURED = False
    err = _Cp1252Stream()
    old_err = sys.stderr
    sys.stderr = err
    handler = logging.StreamHandler(err)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger = logging.getLogger("test_console_io")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        console_io.configure_console()
        logger.info("节点已登录：账号 %s", 12345)
        handler.flush()
    finally:
        logger.handlers = []
        sys.stderr = old_err
        console_io._CONFIGURED = False
