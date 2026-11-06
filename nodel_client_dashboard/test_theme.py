"""主题 token 与控件工厂：新色板、圆角、金色主按钮。"""
from __future__ import annotations

import tkinter as tk

import pytest

import theme


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    r.withdraw()
    yield r
    r.destroy()


def test_palette_values():
    assert theme.BG == "#0a0e17"
    assert theme.PANEL == "#101826"
    assert theme.PANEL2 == "#151f31"
    assert theme.LINE == "#1e2a3d"
    assert theme.GOLD == "#d9b36c"
    assert theme.GOLD_STRONG == "#e9c98a"
    assert theme.ON_GOLD == "#101826"
    assert theme.GREEN == "#2fd381"
    assert theme.AMBER == "#f2b84b"
    assert theme.RED == "#f2645f"
    assert theme.TEXT == "#e8ecf4"
    assert theme.DIM == "#8b96ab"
    assert theme.MUTE == "#5a6a82"


def test_all_colors_are_tk_compatible():
    """Tk 不认 8 位十六进制和 rgba()，所有颜色令牌必须是 6 位 #rrggbb。"""
    import re

    pattern = re.compile(r"^#[0-9a-fA-F]{6}$")
    for name in dir(theme):
        if name.startswith("_"):
            continue
        value = getattr(theme, name)
        if isinstance(value, str) and value.startswith("#"):
            assert pattern.match(value), f"{name}={value} 不是 Tk 能识别的颜色"


def test_legacy_names_are_aliases():
    """旧名保留且指向新色板，避免 11 个 import 点同时改。"""
    assert theme.BG_ELEVATED == theme.PANEL2
    assert theme.GLASS == theme.PANEL
    assert theme.GLASS_SOFT == theme.PANEL2
    assert theme.GLASS_BORDER == theme.LINE
    assert theme.GLASS_BORDER_ACTIVE == theme.GOLD
    assert theme.ACCENT == theme.GOLD
    assert theme.CYAN == theme.BLUE
    assert theme.TEXT_MUTED == theme.DIM
    assert theme.TEXT_DIM == theme.MUTE
    assert theme.DANGER == theme.RED
    assert theme.WARNING == theme.AMBER
    assert theme.OK == theme.GREEN
    assert theme.DEAD == theme.MUTE


def test_primary_btn_is_gold(root):
    btn = theme.primary_btn(root, "启动")
    assert btn.cget("fg_color") == theme.GOLD
    assert btn.cget("text_color") == theme.ON_GOLD
    assert btn.cget("corner_radius") == 6


def test_ghost_btn_is_transparent_with_line_border(root):
    btn = theme.ghost_btn(root, "退出登录")
    assert btn.cget("fg_color") == "transparent"
    assert btn.cget("border_color") == theme.LINE


def test_glass_card_radius_is_8(root):
    """`.panel-card` 的圆角是 8px（参考稿 CSS 实测）。"""
    card = theme.glass_card(root)
    assert card.cget("corner_radius") == 8
    assert card.cget("fg_color") == theme.PANEL
    assert card.cget("border_color") == theme.LINE


def test_badge_and_cells_build(root):
    b = theme.badge(root, "运行中", theme.GREEN)
    assert b.cget("text_color") == theme.GREEN
    cell = theme.info_cell(root, "连接MT5", "已连接", True)
    assert cell.winfo_exists()
    assert cell.value_label.cget("text") == "已连接"
    m = theme.metric_cell(root, "3", "已连接MT5", theme.BLUE)
    assert m.winfo_exists()


def test_health_color_uses_new_semantics():
    assert theme.health_color("ok", True) == theme.GREEN
    assert theme.health_color("error", True) == theme.RED
    assert theme.health_color("starting", True) == theme.AMBER
    assert theme.health_color("ok", False) == theme.MUTE
