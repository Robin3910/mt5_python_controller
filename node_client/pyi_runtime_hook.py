"""PyInstaller 运行时钩子：冻结后的 exe 一启动就把标准流切成 UTF-8。

Windows 中文系统里，被管道/重定向拉起的子进程 `sys.stdout` 编码是本地代码页
（本机实测为 cp1252），节点启动时打印中文会抛 `UnicodeEncodeError` 直接崩掉。
运行时钩子比用户代码更早执行，能保证任何一行日志/提示都不会因编码问题挂掉。
"""
import sys


def _reconfigure(stream) -> None:
    if stream is None:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError, AttributeError):
        pass


_reconfigure(sys.stdout)
_reconfigure(sys.stderr)
