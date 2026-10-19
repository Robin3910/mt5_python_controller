"""打开面板时的管理员提权：已是管理员则继续，否则只申请一次。"""
from __future__ import annotations

import admin_elevate as ae


def test_admin_or_non_windows_keeps_running(monkeypatch):
    monkeypatch.setattr(ae, "is_windows", lambda: True)
    monkeypatch.setattr(ae, "is_admin", lambda: True)
    monkeypatch.setattr(ae, "relaunch_elevated", lambda argv: (_ for _ in ()).throw(AssertionError("不应再申请")))
    assert ae.ensure_admin(["main.py"]) is True

    monkeypatch.setattr(ae, "is_windows", lambda: False)
    monkeypatch.setattr(ae, "is_admin", lambda: False)
    assert ae.ensure_admin(["main.py"]) is True


def test_skip_env_keeps_running(monkeypatch):
    monkeypatch.setenv("DASHBOARD_NO_ELEVATE", "1")
    monkeypatch.setattr(ae, "is_windows", lambda: True)
    monkeypatch.setattr(ae, "is_admin", lambda: False)
    monkeypatch.setattr(ae, "relaunch_elevated", lambda argv: (_ for _ in ()).throw(AssertionError("不应再申请")))
    assert ae.ensure_admin(["main.py"]) is True


def test_relaunch_hands_off_current_process(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(ae, "is_windows", lambda: True)
    monkeypatch.setattr(ae, "is_admin", lambda: False)
    monkeypatch.setattr(ae, "relaunch_elevated", lambda argv: seen.append(argv) or True)
    assert ae.ensure_admin(["main.py", "--stay"]) is False
    assert seen == [["main.py", "--stay"]]


def test_denied_after_flag_does_not_ask_again(monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(ae, "is_windows", lambda: True)
    monkeypatch.setattr(ae, "is_admin", lambda: False)
    monkeypatch.setattr(ae, "relaunch_elevated", lambda argv: (_ for _ in ()).throw(AssertionError("不应再次申请")))
    monkeypatch.setattr(ae, "notify", notes.append)
    assert ae.ensure_admin(["main.py", ae.ELEVATION_FLAG]) is False
    assert notes and "管理员权限" in notes[0]


def test_launch_args_include_flag_once(monkeypatch):
    monkeypatch.setattr(ae.sys, "frozen", False, raising=False)
    monkeypatch.setattr(ae.sys, "executable", r"C:\Python314\python.exe")
    file, params, _cwd = ae.launch_args([r"D:\panel\main.py", "--stay", ae.ELEVATION_FLAG])
    assert file.endswith("python.exe")
    assert params.count(ae.ELEVATION_FLAG) == 1
    assert "main.py" in params
    assert "--stay" in params
