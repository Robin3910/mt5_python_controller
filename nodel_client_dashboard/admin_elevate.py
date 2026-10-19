"""打开面板时申请管理员权限。

打包后的 exe 还会在清单里声明 requireAdministrator。这里覆盖开发态
`python main.py`，以及清单尚未生效的启动：未提权就用 runas 再开一次。
用户拒绝后本进程退出，避免在普通权限下继续导入到 Program Files。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ELEVATION_FLAG = "--admin-requested"
_SKIP_VALUES = ("1", "true", "yes", "on")


def elevation_skipped() -> bool:
    return os.environ.get("DASHBOARD_NO_ELEVATE", "").strip().lower() in _SKIP_VALUES


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(__import__("ctypes").windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def launch_args(argv: list[str]) -> tuple[str, str, str]:
    """返回 (可执行文件, 参数字符串, 工作目录)。参数里带上提权标记，避免拒绝后循环弹窗。"""
    extra = [arg for arg in argv[1:] if arg != ELEVATION_FLAG]
    extra.append(ELEVATION_FLAG)
    cwd = os.getcwd()
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(extra), cwd
    script = str(Path(argv[0]).resolve()) if argv and argv[0] else str(Path("main.py").resolve())
    return sys.executable, subprocess.list2cmdline([script, *extra]), cwd


def relaunch_elevated(argv: list[str]) -> bool:
    """弹出 UAC。用户同意且新进程已启动时返回 True。"""
    file, params, cwd = launch_args(argv)
    return _shell_execute(file, params, cwd) > 32


def _shell_execute(file: str, params: str, cwd: str) -> int:
    import ctypes

    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", file, params or None, cwd, 1)
    return int(rc)


def notify(text: str) -> None:
    if not is_windows():
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "节点控制台", 0x10)


def ensure_admin(argv: list[str] | None = None) -> bool:
    """当前进程可以继续跑时返回 True。已拉起提权进程或用户拒绝时返回 False。"""
    args = list(sys.argv if argv is None else argv)
    if not is_windows() or elevation_skipped() or is_admin():
        return True
    if ELEVATION_FLAG in args:
        notify("需要管理员权限才能继续。请在系统提示中允许后重新打开。")
        return False
    if relaunch_elevated(args):
        return False
    notify("需要管理员权限才能继续。请在系统提示中允许后重新打开。")
    return False
