"""登录页：输入框必须真实可见（曾经被固定高度容器压成 0 高度）。"""
from __future__ import annotations

import tkinter as tk

import pytest

import customtkinter as ctk


class _FakeSession:
    generation = 0
    _local = type("L", (), {"generation": None})()


class _FakeMaster(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.session = _FakeSession()


def _make_root():
    try:
        return _FakeMaster()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")


def test_login_inputs_are_visible_and_sized():
    """三个输入框 + 状态提示 + 登录按钮都必须是 mapped 且有真实尺寸。"""
    root = _make_root()
    root.geometry("1200x760")
    root.configure(fg_color="#0b1119")

    from login_view import LoginView
    view = LoginView(root, on_authenticated=lambda: None)
    view.pack(fill="both", expand=True)
    root.deiconify()
    root.update_idletasks()
    root.update()

    for name in ("_account_entry", "_password_entry", "_code_entry"):
        entry = getattr(view, name)
        assert entry.winfo_ismapped(), f"{name} 未被布局，输入框不可见"
        assert entry.winfo_width() > 50, f"{name} 宽度异常：{entry.winfo_width()}"
        assert entry.winfo_height() > 20, f"{name} 高度异常：{entry.winfo_height()}"

    assert view.status.winfo_ismapped(), "状态提示不可见"
    assert view.button.winfo_ismapped(), "登录按钮不可见"
    root.destroy()


def test_login_frame_height_grows_with_content():
    """外层容器高度必须跟着内容长（不能被锁死成默认值）。"""
    root = _make_root()
    root.geometry("1200x760")
    root.configure(fg_color="#0b1119")

    from login_view import LoginView
    view = LoginView(root, on_authenticated=lambda: None)
    view.pack(fill="both", expand=True)
    root.deiconify()
    root.update_idletasks()
    root.update()

    frame = view.winfo_children()[1]  # bg_canvas 之后就是 .login-frame
    assert frame.winfo_height() > 400, f"容器高度被锁死：{frame.winfo_height()}"
    root.destroy()
