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
