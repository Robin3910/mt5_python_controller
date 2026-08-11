"""托盘图标生成测试。"""
from tray_icon import build_tray_image, tray_available


def test_tray_image_builds():
    assert tray_available()
    img = build_tray_image(64)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
