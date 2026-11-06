"""hero 统计卡：分布比例、零实例不崩、更新不重建控件。"""
from __future__ import annotations

import tkinter as tk

import pytest

from viewmodels import HeroVM


def test_distribution_segments_zero_total_is_empty():
    from hero_panel import distribution_segments
    hero = HeroVM(0, 0, 0, 0, 0, 0, 0, 0)
    assert distribution_segments(hero) == []


def test_distribution_segments_sum_to_one():
    from hero_panel import distribution_segments
    hero = HeroVM(5, 3, 1, 0, 1, 3, 2, 0)
    segs = distribution_segments(hero)
    assert abs(sum(ratio for _c, ratio in segs) - 1.0) < 1e-9
    assert dict(segs)["online"] == pytest.approx(0.6)


def test_distribution_skips_zero_count_states():
    from hero_panel import distribution_segments
    hero = HeroVM(4, 2, 0, 2, 0, 0, 0, 0)
    segs = dict(distribution_segments(hero))
    assert "starting" not in segs
    assert "pending" not in segs


def test_hero_panel_updates_in_place():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    first = panel._total_label
    panel.update(HeroVM(5, 3, 1, 1, 0, 3, 2, 0))
    assert panel._total_label is first          # 未重建
    assert "5" in panel._total_label.cget("text")
    root.destroy()


def test_hero_panel_shows_zero_total_without_crash():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    panel.update(HeroVM(0, 0, 0, 0, 0, 0, 0, 0))
    assert "0" in panel._total_label.cget("text")
    root.destroy()


def test_status_chip_has_dot_and_label():
    """`.stat-chip` 结构：7px 圆点 + 文字。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    import theme
    chip = theme.status_chip(root, "在线 3", theme.GREEN)
    assert chip.dot.cget("width") == 7
    assert chip.dot.cget("fg_color") == theme.GREEN
    assert chip.label.cget("text") == "在线 3"
    assert chip.label.cget("text_color") == theme.GREEN
    root.destroy()


def test_hero_chips_update_text_in_place():
    """更新胶囊只改文字，不重建控件。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    chip = panel._chips["online"]
    label = chip.label
    panel.update(HeroVM(5, 3, 1, 1, 0, 3, 2, 0))
    assert panel._chips["online"] is chip
    assert panel._chips["online"].label is label
    assert label.cget("text") == "在线 3"
    root.destroy()


def test_hero_metric_labels_match_viewmodel_semantics():
    """三个指标分别对应 connected_mt5 / authenticated / alerts。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    assert set(panel._metrics) == {"connected_mt5", "authenticated", "alerts"}
    root.destroy()


def test_hero_canvas_redraw_is_idempotent():
    """重复重画不应无限增加 Canvas item（拖动窗口时会被频繁触发）。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    root.update_idletasks()
    panel._redraw_texture()
    first = len(panel.canvas.find_all())
    panel._redraw_texture()
    assert len(panel.canvas.find_all()) == first
    root.destroy()
