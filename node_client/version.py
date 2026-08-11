"""node_client 版本号（打包时同步写入 dist/version.txt）。"""
from __future__ import annotations

import sys
from pathlib import Path

# 发版时改此常量；build_exe.bat 会写出同名 version.txt
VERSION = "1.1.0"


def get_version() -> str:
    """运行时版本：优先旁路 version.txt（便于替换后核对），否则用内置常量。"""
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent
        txt = base / "version.txt"
        if txt.is_file():
            text = txt.read_text(encoding="utf-8-sig").strip()
            if text:
                return text.splitlines()[0].strip()
    except OSError:
        pass
    return VERSION
