"""对话框的「构建期遮挡 → 居中显示」流程。

遮挡不能用 `withdraw()`：customtkinter 的 `CTkToplevel` 在创建时会自行 withdraw
窗口去设置暗色标题栏，并挂一个延时回调把窗口恢复到「之前的状态」。我们若在这
期间也调 `withdraw()`，那个回调记下的「之前状态」就变成 withdrawn，稍后会把已经
显示并 `grab_set` 的对话框重新隐藏 —— 现象是对话框一闪而过，而输入权仍握在这个
不可见的窗口上，主窗口从此点不动也关不掉。

改用 `-alpha` 透明度遮挡：窗口状态始终是 normal，customtkinter 的状态机不受影响。
"""
from __future__ import annotations

import tkinter as tk

from tray_icon import apply_window_icon


def centered_origin(
    size: tuple[int, int],
    parent_box: tuple[int, int, int, int],
    screen: tuple[int, int],
) -> tuple[int, int]:
    """算出窗口左上角坐标：在父窗口内居中，并夹紧到屏幕范围内。

    父窗口贴边或比对话框还小时，居中偏移会算成负数，夹紧保证标题栏不会跑到
    屏幕外面变得抓不住。
    """
    w, h = size
    px, py, pw, ph = parent_box
    sw, sh = screen
    x = min(max(0, px + max(0, (pw - w) // 2)), max(0, sw - w))
    y = min(max(0, py + max(0, (ph - h) // 2)), max(0, sh - h))
    return x, y


def _set_alpha(win, value: float) -> None:
    """设置窗口透明度；平台不支持时忽略，退化为构建过程可见。"""
    try:
        win.attributes("-alpha", value)
    except tk.TclError:
        pass


def _release_grab(win) -> None:
    try:
        win.grab_release()
    except tk.TclError:
        pass


def hide_while_building(win) -> None:
    """构建期把窗口设为全透明，避免用户看到控件逐个落位。"""
    apply_window_icon(win)
    _set_alpha(win, 0.0)


def show_centered(win, width: int, height: int) -> None:
    """定位到父窗口中央并显示：恢复不透明 → 置顶 → 独占输入 → 取焦点。"""
    win.update_idletasks()
    try:
        parent = win.master
        parent.update_idletasks()
        box = (
            int(parent.winfo_rootx()),
            int(parent.winfo_rooty()),
            max(int(parent.winfo_width()), 1),
            max(int(parent.winfo_height()), 1),
        )
    except (tk.TclError, AttributeError):
        box = (0, 0, int(win.winfo_screenwidth()), int(win.winfo_screenheight()))

    screen = (int(win.winfo_screenwidth()), int(win.winfo_screenheight()))
    x, y = centered_origin((width, height), box, screen)
    win.geometry(f"{width}x{height}+{x}+{y}")

    _set_alpha(win, 1.0)
    win.lift()
    try:
        win.grab_set()
    except tk.TclError:
        pass
    win.focus_force()

    # 兜底：窗口一旦被隐藏就交还输入权，绝不让主窗口被看不见的窗口锁死
    def on_unmap(event) -> None:
        if event.widget is win:
            _release_grab(win)

    win.bind("<Unmap>", on_unmap, add="+")
