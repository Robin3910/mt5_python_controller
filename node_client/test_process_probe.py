"""进程探针：判断 MT5 终端进程在不在运行。

这个探针是「该不该重连」的闸门。用户手动关掉 MT5 通常是想停止交易，
而 `mt5.initialize()` 在终端没跑的时候会自动把它启动起来 —— 所以必须
先确认进程状态，再决定要不要 initialize。
"""
import os
import subprocess
import sys

import pytest

import process_probe


def test_process_running_finds_current_python():
    """当前进程自己必然在跑。"""
    exe = os.path.basename(sys.executable).lower()
    assert process_probe.process_running([exe]) is True


def test_process_running_is_false_for_unknown_name():
    """没有这个进程 → False。"""
    assert process_probe.process_running(["definitely_not_a_real_process_xyz.exe"]) is False


def test_process_running_is_case_insensitive():
    """大小写不敏感（Windows 文件名不区分大小写）。"""
    exe = os.path.basename(sys.executable)
    assert process_probe.process_running([exe.upper()]) is True


def test_process_running_none_for_empty_list():
    """没给名字 → None（探不到），不能当成 False。"""
    assert process_probe.process_running([]) is None
    assert process_probe.process_running(None) is None
    assert process_probe.process_running(["", "  "]) is None


def test_process_running_none_when_enumeration_fails(monkeypatch):
    """枚举失败 → None（探不到），不能当成 False。

    这是最关键的一条：把「探不到」当成「没在跑」，会让节点永远不重连，
    用户重启 MT5 也恢复不了。
    """
    def boom():
        raise OSError("enumeration failed")

    monkeypatch.setattr(process_probe, "_iter_process_names", boom)
    assert process_probe.process_running(["terminal64.exe"]) is None


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 可用")
def test_process_running_finds_spawned_child():
    """起一个子进程，探针应当能发现它；结束后应当探不到。"""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        exe = os.path.basename(sys.executable).lower()
        assert process_probe.process_running([exe]) is True
    finally:
        child.terminate()
        child.wait(timeout=10)
