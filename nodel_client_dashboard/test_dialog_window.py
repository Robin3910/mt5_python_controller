"""对话框居中定位的边界行为。"""
from __future__ import annotations

from dialog_window import centered_origin

SCREEN = (1920, 1080)


def test_centers_inside_parent():
    # 父窗口 1000x800 位于 (100, 100)，对话框 800x600 应居中
    assert centered_origin((800, 600), (100, 100, 1000, 800), SCREEN) == (200, 200)


def test_parent_smaller_than_dialog_does_not_shift_negative():
    """父窗口比对话框还小时不能算出负偏移，否则对话框会挂在父窗口左上角外。"""
    assert centered_origin((800, 600), (300, 200, 400, 300), SCREEN) == (300, 200)


def test_clamped_into_screen_when_parent_near_edge():
    """父窗口贴着屏幕右下角时，对话框要被拉回屏幕内，标题栏才抓得住。"""
    x, y = centered_origin((800, 600), (1800, 1000, 100, 60), SCREEN)
    assert (x, y) == (1120, 480)


def test_dialog_larger_than_screen_pinned_to_origin():
    assert centered_origin((2400, 1400), (0, 0, 1920, 1080), SCREEN) == (0, 0)


def test_negative_parent_origin_clamped():
    """父窗口被拖到屏幕左上角外（坐标为负）时也不能算出负坐标。"""
    x, y = centered_origin((800, 600), (-200, -150, 400, 300), SCREEN)
    assert x >= 0 and y >= 0
