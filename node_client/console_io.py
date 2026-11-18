"""控制台编码兜底。

Windows 中文系统里，管道/重定向场景下 `sys.stdout` / `sys.stderr` 的编码可能
是 cp1252 或 cp936，节点启动时打印中文（如「已定位 MT5 终端：…」）会直接抛
`UnicodeEncodeError`，进程在连上 MT5 之前就崩掉。

这里统一把标准输出/错误流切成 UTF-8，并对无法编码的字符降级为替换符，
保证任何 print / logging 输出都不会因为编码问题把节点打挂。
"""
from __future__ import annotations

import sys
from typing import Any

_CONFIGURED = False


def _reconfigure(stream: Any) -> None:
    """把一个文本流切到 UTF-8；不是 TextIOWrapper 或已关闭时静默跳过。"""
    if stream is None:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError, AttributeError):
        pass


def configure_console() -> None:
    """确保标准输出/错误可用 UTF-8 输出中文（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)


def safe_print(*args: Any, **kwargs: Any) -> None:
    """打印文本；即使编码再出问题也不会抛异常。"""
    configure_console()
    sep = kwargs.get("sep", " ")
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = sep.join(str(a) for a in args)
        # 1) 优先写底层字节流（UTF-8），管道/重定向场景可正确落盘
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            try:
                buffer.write(text.encode("utf-8", "replace") + b"\n")
                buffer.flush()
                return
            except (OSError, ValueError, UnicodeEncodeError):
                pass
        # 2) 兜底：只输出 ASCII 可编码内容，绝不因中文再次抛异常
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001 — 打印失败不应影响主流程
            pass
