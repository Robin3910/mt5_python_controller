"""托盘图标生成测试。"""
from tray_icon import build_tray_image, logo_path, tray_available


def test_logo_ico_exists():
    path = logo_path()
    assert path is not None
    assert path.name == "logo.ico"
    assert path.is_file()


def test_tray_image_builds():
    assert tray_available()
    img = build_tray_image(64)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
