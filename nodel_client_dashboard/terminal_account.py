"""导入节点时解析 MT5 账号：手工填写优先，留空则读取终端当前登录号。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import env_file as ef


class TerminalAccountError(RuntimeError):
    """账号无效，或终端当前登录号读不到。调用方不应再申请节点。"""


def typed_login(raw: str | None) -> int | None:
    """正整数账号；空白表示改从终端读取。非法输入直接失败。"""
    text = (raw or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) <= 0:
        raise TerminalAccountError("请输入正整数账号，或留空以读取终端当前登录号")
    return int(text)


def resolve_import_login(raw: str | None, read_account: Callable[[], dict | None]) -> tuple[int, str]:
    """返回 (账号, 服务器)。填写的正整数优先，且不会调用 read_account。"""
    login = typed_login(raw)
    if login is not None:
        return login, ""
    try:
        account = read_account()
    except TerminalAccountError:
        raise
    except Exception as exc:
        raise TerminalAccountError(f"无法读取 MT5 终端账号：{exc}") from exc
    if not isinstance(account, dict):
        raise TerminalAccountError("终端未登录，请先在 MT5 登录，或手工填写账号")
    got = account.get("login")
    if isinstance(got, bool) or not isinstance(got, int) or got <= 0:
        raise TerminalAccountError("终端未登录，请先在 MT5 登录，或手工填写账号")
    return got, str(account.get("server") or "")


def enroll_import_login(raw: str | None, read_account: Callable[[], dict | None], enroll: Callable[[int, str], None]) -> tuple[int, str]:
    """先解析账号，成功后才申请。读取失败或未登录时不调用 enroll。"""
    login, server = resolve_import_login(raw, read_account)
    enroll(login, server)
    return login, server


def _load_mt5():
    try:
        import MetaTrader5 as mt5
    except Exception as exc:
        raise TerminalAccountError("本机没有 MetaTrader5 接口，请手工填写账号") from exc
    return mt5


def read_terminal_account(directory: str | Path) -> dict:
    """附着所选目录中的终端，读取当前已登录账号。未登录或连不上则失败。"""
    terminal = ef.find_terminal(directory)
    if terminal is None:
        raise TerminalAccountError("所选目录没有 MT5 终端，无法读取当前登录号")
    mt5 = _load_mt5()
    if not mt5.initialize(path=str(terminal)):
        raise TerminalAccountError(f"无法连接 MT5 终端：{mt5.last_error()}")
    try:
        info = mt5.account_info()
        login = int(getattr(info, "login", 0) or 0) if info is not None else 0
        if login <= 0:
            raise TerminalAccountError("终端未登录，请先在 MT5 登录，或手工填写账号")
        return {"login": login, "server": str(getattr(info, "server", "") or "")}
    finally:
        mt5.shutdown()
