"""系统托盘：最小化/关闭隐藏到托盘，菜单可还原与退出。"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

LOGO_FILENAME = "logo.ico"


def _bundle_dir() -> Path:
    """打包资源目录：onefile 在 `_MEIPASS`，开发时在本包目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def logo_path() -> Path | None:
    p = _bundle_dir() / LOGO_FILENAME
    return p if p.is_file() else None


def apply_window_icon(win, *, as_default: bool = False) -> None:
    """给 Tk / CTk 窗口套上 logo.ico；缺文件或平台不支持时静默跳过。"""
    path = logo_path()
    if path is None:
        return
    s = str(path)
    try:
        win.iconbitmap(s)
    except Exception:  # noqa: BLE001
        return
    if as_default:
        try:
            win.iconbitmap(default=s)
        except Exception:  # noqa: BLE001
            pass


def tray_available() -> bool:
    return pystray is not None and Image is not None


def build_tray_image(size: int = 64) -> Any:
    """优先用 logo.ico；读不到再画青绿圆形兜底。"""
    assert Image is not None
    path = logo_path()
    if path is not None:
        try:
            img = Image.open(path).convert("RGBA")
            if img.size != (size, size):
                img = img.resize((size, size), Image.Resampling.LANCZOS)
            return img
        except Exception:  # noqa: BLE001
            pass
    return _fallback_tray_image(size)


def _fallback_tray_image(size: int) -> Any:
    assert Image is not None and ImageDraw is not None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=(46, 230, 168, 255),  # #2EE6A8
        outline=(8, 20, 36, 255),
        width=2,
    )
    cx, cy = size // 2, size // 2
    draw.line(
        [(cx - 10, cy + 12), (cx - 10, cy - 12), (cx + 10, cy + 12), (cx + 10, cy - 12)],
        fill=(4, 20, 15, 255),
        width=4,
    )
    return img


class TrayController:
    """在后台线程运行 pystray；回调通过 schedule(fn) 投递到 UI 线程。"""

    def __init__(
        self,
        *,
        schedule: Callable[[Callable[[], None]], None],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "节点控制台",
    ) -> None:
        self._schedule = schedule
        self._on_show = on_show
        self._on_quit = on_quit
        self._title = title
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> bool:
        if not tray_available() or self._started:
            return False

        def show(_icon=None, _item=None):
            self._schedule(self._on_show)

        def quit_app(_icon=None, _item=None):
            self._schedule(self._on_quit)

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", show, default=True),
            pystray.MenuItem("退出", quit_app),
        )
        self._icon = pystray.Icon(
            "node_client_dashboard",
            build_tray_image(),
            self._title,
            menu,
        )
        self._thread = threading.Thread(
            target=self._icon.run, name="systray", daemon=True
        )
        self._thread.start()
        self._started = True
        return True

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        self._started = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass

    def notify(self, title: str, message: str) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:  # noqa: BLE001
            pass
