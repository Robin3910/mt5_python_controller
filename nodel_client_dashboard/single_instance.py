"""运维面板单实例：Windows 命名互斥 + 激活已有窗口。"""
from __future__ import annotations

import atexit
import ctypes
import sys
from ctypes import wintypes

# 全局互斥名（Local\ 仅本会话；Global\ 需权限，这里用 Local）
MUTEX_NAME = "Local\\RobinNodeClientDashboard_SingleInstance"
WINDOW_TITLE = "节点控制台"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
_mutex_handle = None


def _win_error() -> int:
    return ctypes.get_last_error()


def acquire() -> bool:
    """尝试获取单实例锁。成功 True；已有实例 False。"""
    global _mutex_handle
    if sys.platform != "win32" or _kernel32 is None:
        # 非 Windows：用锁文件兜底
        return _acquire_file_lock()

    ERROR_ALREADY_EXISTS = 183
    _kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
    ]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    if _win_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    atexit.register(release)
    return True


def release() -> None:
    global _mutex_handle
    if sys.platform == "win32" and _kernel32 is not None and _mutex_handle:
        try:
            _kernel32.CloseHandle(_mutex_handle)
        except Exception:  # noqa: BLE001
            pass
        _mutex_handle = None
    _release_file_lock()


def activate_existing(title: str = WINDOW_TITLE) -> bool:
    """把已运行的主窗口拉到前台。"""
    if sys.platform != "win32" or _user32 is None:
        return False
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    hwnd = _user32.FindWindowW(None, title)
    if not hwnd:
        return False

    SW_RESTORE = 9
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.ShowWindow(hwnd, SW_RESTORE)

    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow(hwnd)
    return True


# —— 非 Windows / 兜底：数据目录锁文件 ——
_lock_fp = None


def _lock_path():
    from store import data_dir

    return data_dir() / ".dashboard.lock"


def _acquire_file_lock() -> bool:
    global _lock_fp
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fp = open(path, "a+", encoding="utf-8")
        if sys.platform == "win32":
            import msvcrt

            fp.seek(0)
            try:
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fp.close()
                return False
        else:
            import fcntl

            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fp.close()
                return False
        fp.seek(0)
        fp.truncate()
        fp.write(str(__import__("os").getpid()))
        fp.flush()
        _lock_fp = fp
        atexit.register(release)
        return True
    except OSError:
        return False


def _release_file_lock() -> None:
    global _lock_fp
    if _lock_fp is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            _lock_fp.seek(0)
            try:
                msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_UN)
        _lock_fp.close()
    except OSError:
        pass
    _lock_fp = None
