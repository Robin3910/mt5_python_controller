"""打包资源路径解析。

onefile 打包后资源在 `sys._MEIPASS`，开发时在包目录。两处都要找得到
`assets/logo.jpg`（顶栏与登录页的圆形 logo）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 顶栏 / 登录页共用的圆形 logo
LOGO_FILENAME = "logo.jpg"
ASSETS_DIRNAME = "assets"


def bundle_dir() -> Path:
    """打包资源根目录：onefile 在 `_MEIPASS`，开发时在本包目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def asset_path(*parts: str) -> Path | None:
    """按相对路径取资源；找不到返回 None（调用方负责降级）。"""
    candidate = bundle_dir().joinpath(*parts)
    return candidate if candidate.is_file() else None


def logo_path() -> Path | None:
    """圆形 logo 图片路径。"""
    return asset_path(ASSETS_DIRNAME, LOGO_FILENAME)
