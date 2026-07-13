"""在本程序所在目录自动发现 MT5 终端（要求客户端放在 MT5 安装目录内）。"""
from __future__ import annotations

import sys
from pathlib import Path

TERMINAL_NAMES = ("terminal64.exe", "terminal.exe")


def app_dir() -> Path:
    """打包 exe 用可执行文件目录；源码运行用脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def discover_mt5_terminal(base: Path | None = None) -> Path:
    """在 base（默认本程序目录）查找 terminal64.exe / terminal.exe。

    找不到则抛出 FileNotFoundError，并附带中文说明。
    """
    root = (base or app_dir()).resolve()
    for name in TERMINAL_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    names = " / ".join(TERMINAL_NAMES)
    raise FileNotFoundError(
        f"未在程序目录找到 MT5 终端（{names}）。\n"
        f"当前目录：{root}\n"
        f"请将本程序放入 MetaTrader 5 安装目录后再启动。"
    )
