"""单实例锁测试。"""
import single_instance as si


def test_window_title_constant():
    assert si.WINDOW_TITLE == "节点控制台"
    assert "Dashboard" in si.MUTEX_NAME or "Robin" in si.MUTEX_NAME


def test_acquire_release_file_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_lock_path", lambda: tmp_path / ".dashboard.lock")
    # 强制走文件锁路径
    monkeypatch.setattr(si, "_kernel32", None)
    assert si._acquire_file_lock() is True
    assert si._acquire_file_lock() is False
    si._release_file_lock()
    assert si._acquire_file_lock() is True
    si._release_file_lock()
