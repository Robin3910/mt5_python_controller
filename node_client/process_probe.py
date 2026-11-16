"""进程级探测：判断某个可执行文件是否正在运行。

为什么需要它：``mt5.initialize()`` 在终端没运行时会**自动把终端拉起来**
（见 MQL5 官方文档 initialize 说明："If required, when making a call
initialize, the MetaTrader 5 terminal can be launched"）。

这带来两个问题：
- 用户手动关掉 MT5 通常是想**停止交易**，节点擅自把它再开起来就违背了意图；
- initialize() 默认要等 60 秒才返回，会把 MT5 专用线程整个卡住。

所以重连之前必须先确认「用户自己把终端开回来了」——也就是先看
**进程在不在**，再决定要不要 initialize。

用 ctypes 走 ToolHelp32 快照，而不是 tasklist 子进程：
- 不spawn 控制台进程（tasklist 在某些环境下会闪黑框）；
- 单次调用约 1 毫秒，看门狗每 2 秒轮询一次完全无压力。
"""
from __future__ import annotations

import sys

# 探不到就返回 None（"不知道"），绝不能把「探不到」当成「没在跑」——
# 那会让节点永远不重连。
_UNAVAILABLE: bool | None = None

if sys.platform == "win32":  # pragma: no cover - 平台相关
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel32.Process32FirstW.restype = wintypes.BOOL
        _kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)
        ]
        _kernel32.Process32NextW.restype = wintypes.BOOL
        _kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)
        ]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _AVAILABLE = True
    except (AttributeError, OSError):  # pragma: no cover - 极老系统
        _AVAILABLE = False


def _iter_process_names() -> list[str]:
    """列出当前所有进程的可执行文件名（小写）。探不到就抛异常。"""
    if not _AVAILABLE:  # pragma: no cover - 平台相关
        raise OSError("process enumeration unsupported on this platform")
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == _INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed")
    names: list[str] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return names
        while True:
            names.append(str(entry.szExeFile).lower())
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return names


def process_running(exe_names) -> bool | None:
    """给定的可执行文件名里，是否有至少一个正在运行。

    返回：
    - True：至少一个在跑；
    - False：都没在跑；
    - None：**探不到**（当前平台不支持 / 枚举失败）。

    None 绝不能当成 False —— 探不到就当「没在跑」，会让节点永远不重连。
    调用方要按「不知道」处理。
    """
    targets = {str(n or "").strip().lower() for n in (exe_names or [])}
    targets.discard("")
    if not targets:
        return None
    try:
        names = _iter_process_names()
    except Exception:  # noqa: BLE001
        return _UNAVAILABLE
    return any(n in targets for n in names)
