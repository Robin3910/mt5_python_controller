"""WebView 版面板的数据目录解析。

WebView 版与 TK 版必须共用同一份实例清单与面板配置，否则在两个版本之间
切换会看到不同的实例列表（用户会误以为 TK 版坏了）。

优先级：
1. 环境变量 `DASHBOARD_DATA_DIR` —— 显式指定，最优先。
2. 开发态：仓库内同级目录 `nodel_client_dashboard/`，与 TK 版共享。
3. 打包态：exe 同级目录 —— 两个 exe 放在同一文件夹时天然共享。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_DATA_DIR = "DASHBOARD_DATA_DIR"
TK_PACKAGE_NAME = "nodel_client_dashboard"


def _env_data_dir() -> Path | None:
    raw = os.environ.get(ENV_DATA_DIR, "").strip()
    return Path(raw) if raw else None


def _dev_tk_dir() -> Path | None:
    """开发态：仓库内同级 TK 包目录。"""
    here = Path(__file__).resolve().parent
    candidate = here.parent / TK_PACKAGE_NAME
    return candidate if candidate.is_dir() else None


def data_dir() -> Path:
    """面板数据目录：实例清单、面板配置、审计队列、日志。"""
    forced = _env_data_dir()
    if forced is not None:
        forced.mkdir(parents=True, exist_ok=True)
        return forced

    if getattr(sys, "frozen", False):
        # 打包态：exe 同级。两个版本放在同一文件夹即可共享。
        return Path(sys.executable).resolve().parent

    tk_dir = _dev_tk_dir()
    if tk_dir is not None:
        return tk_dir
    return Path(__file__).resolve().parent


def frontend_dist_dir() -> Path:
    """前端产物目录（Vite build 输出）。

    打包时由 PyInstaller 放进 `_internal/frontend-console/dist`；
    开发态指向仓库里的 `frontend-console/dist`。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidate = base / "frontend-console" / "dist"
        if candidate.is_dir():
            return candidate
        candidate = Path(sys.executable).resolve().parent / "frontend-console" / "dist"
        if candidate.is_dir():
            return candidate
        return base / "frontend-console" / "dist"

    repo = Path(__file__).resolve().parent.parent
    return repo / "frontend-console" / "dist"
