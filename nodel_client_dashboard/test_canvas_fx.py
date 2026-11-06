"""Canvas 特效层：颜色混合、圆角多边形、网格纹理、径向渐变。"""
from __future__ import annotations

import tkinter as tk

import pytest


def test_mix_blends_toward_base():
    """mix 应该把前景色按比例混到底色上。"""
    from canvas_fx import mix
    # 全 0 -> 全 1
    assert mix("#ffffff", 0.0, "#000000") == "#000000"
    assert mix("#ffffff", 1.0, "#000000") == "#ffffff"
    # 50%（int 截断）
    assert mix("#ffffff", 0.5, "#000000") == "#7f7f7f"
    # 与 CSS 的 rgba(148,163,184,.14) 混在 #101826 上应得 #222b3a
    assert mix("#94a3b8", 0.14, "#101826") == "#222b3a"


def test_mix_rgb_matches_mix():
    from canvas_fx import mix, mix_rgb, rgb
    assert mix("#d9b36c", 0.18, "#0a0e17") == mix_rgb(rgb("#d9b36c"), "#0a0e17", 0.18)


def test_rounded_rect_points_has_four_corners():
    """圆角多边形应有 4 组圆弧采样点。"""
    from canvas_fx import rounded_rect_points
    points = rounded_rect_points(100, 50, 10)
    # 每角 _ARC_STEPS+1 = 7 个点，共 28 个点 = 56 个坐标
    assert len(points) == 56
    xs, ys = points[0::2], points[1::2]
    assert min(xs) >= 0 and max(xs) <= 100
    assert min(ys) >= 0 and max(ys) <= 50


def test_rounded_rect_points_zero_radius_is_plain_rect():
    from canvas_fx import rounded_rect_points
    assert rounded_rect_points(100, 50, 0) == [0, 0, 100, 0, 100, 50, 0, 50]


def test_grid_texture_draws_lines_at_step(root=None):
    """网格纹理应按给定步距画线。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    import customtkinter as ctk

    from canvas_fx import grid_texture

    canvas = ctk.CTkCanvas(root, width=200, height=100)
    canvas.pack()
    root.update_idletasks()
    canvas.delete("all")
    grid_texture(canvas, 200, 100, 50, "#94a3b8", "#0a0e17", 0.05)
    # 竖线 200/50 = 5 条（含 x=200 端点），横线 100/50 = 3 条（含 y=100 端点）
    items = canvas.find_all()
    assert len(items) == 8
    root.destroy()


def test_grid_texture_fades_toward_mask_edge():
    """遮罩边缘的网格线应回到底色。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    import customtkinter as ctk

    from canvas_fx import grid_texture

    canvas = ctk.CTkCanvas(root, width=400, height=400)
    canvas.pack()
    root.update_idletasks()
    canvas.delete("all")
    grid_texture(canvas, 400, 400, 50, "#94a3b8", "#0a0e17", 0.05,
                 mask_cx=0.5, mask_cy=0.5, mask_rx=0.5, mask_ry=0.5)
    # 第一条竖线在 x=0，远离中心 x=200，应该被遮罩压回底色
    first = canvas.itemcget(canvas.find_all()[0], "fill")
    assert first == "#0a0e17"
    root.destroy()


def test_paint_radial_draws_concentric_ovals():
    """径向渐变应画出一圈圈椭圆。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    import customtkinter as ctk

    from canvas_fx import GRADIENT_RINGS, paint_radial

    canvas = ctk.CTkCanvas(root, width=200, height=100)
    canvas.pack()
    root.update_idletasks()
    canvas.delete("all")
    paint_radial(canvas, 200, 100, 200, 0, 260, 170, "#d9b36c", 0.14, "#151f31")
    assert len(canvas.find_all()) <= GRADIENT_RINGS
    assert len(canvas.find_all()) > 0
    root.destroy()


def test_corner_patches_cover_four_corners():
    """圆角补丁应画 4 块，把方角盖回页面底色。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    import customtkinter as ctk

    from canvas_fx import paint_corner_patches

    canvas = ctk.CTkCanvas(root, width=200, height=100)
    canvas.pack()
    root.update_idletasks()
    canvas.delete("all")
    paint_corner_patches(canvas, 200, 100, 10, "#0a0e17")
    assert len(canvas.find_all()) == 4
    root.destroy()
