"""WebView2 运行时定位：让面板永远走 Edge Chromium，绝不回落到 MSHTML。

问题
----
pywebview 在 Windows 上的后端选择逻辑（`webview/platforms/winforms.py`）：

    def _is_chromium():
        if settings['WEBVIEW2_RUNTIME_PATH']:
            return True
        ...  # 查注册表里的 WebView2 Runtime 版本号
        return False

    is_chromium = not is_cef and _is_chromium() and forced_gui_ != 'mshtml'
    if is_chromium:  ... edgechromium
    else:            ... mshtml   # ← 古老的 IE 内核

也就是说：**只要注册表里查不到 WebView2 Runtime，pywebview 就静默切到 MSHTML**，
只打一条 WARNING 日志。而 MSHTML（IE 内核）：

  - 不支持 ES module（`<script type="module">`）、不支持 `async/await`、
    不支持 CSS grid/flex 的现代写法 —— 我们的前端是 Vite + Vue 3 构建产物，
    直接跑不起来；
  - 结果就是**一个完全空白的窗口**，而且没有任何错误提示。

「有问题的电脑」正是这种情况：Windows Server 2022（10.0.20348）没预装
WebView2 Runtime → 注册表查不到 → 静默回落 MSHTML → 白屏。

解决
----
本项目自带一份 Fixed Version 运行时（`portable/webview2/`），随 exe 一起分发。
只要在 `webview.start()` **之前**把 `WEBVIEW2_RUNTIME_PATH` 指向它，
`_is_chromium()` 就会直接返回 True —— 不再查注册表，也就不会回落 MSHTML。

`WEBVIEW2_RUNTIME_PATH` 必须是**绝对路径**：pywebview 只对相对路径做
`os.path.join(get_app_root(), ...)`，而 `get_app_root()` 在 PyInstaller 产物里
返回的是 `_MEIPASS`（临时解包目录），把运行时指到那里是错的。

为什么放在 `webview.start()` 之前
--------------------------------
`webview.platforms.winforms` 是在 `webview.start()` 里才被 import 的，
模块级的 `is_chromium = _is_chromium()` 也在那一刻求值。设置晚一步就来不及。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 随包运行时的目录名（相对 exe 所在目录）
BUNDLED_DIR_NAME = "webview2"

# 随包运行时可能所在的父目录名。
# `portable/` 是打包脚本产出的可携带目录，运行时放在 `portable/webview2/`；
# 直接放在 exe 旁边（`webview2/`）也支持。
BUNDLED_PARENT_DIRS = ("", "portable")

# 运行时目录的识别标志：Fixed Version 运行时的目录名形如
# Microsoft.WebView2.FixedVersionRuntime.<版本>.<架构>
RUNTIME_DIR_PREFIX = "Microsoft.WebView2.FixedVersionRuntime"

# 判定运行时「可用」所需的标志性文件
RUNTIME_MARKER_FILES = ("msedgewebview2.exe", "msedge.dll")


def app_root() -> Path:
    """exe 所在目录（打包态）/ 本包目录（开发态）。

    随包运行时是**放在 exe 旁边**的（不进 PyInstaller 的 _MEIPASS）——
    430MB 的运行时打进单文件 exe 会让每次启动都解包 430MB，不可接受。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 开发态：portable/ 在本包目录下。
    return Path(__file__).resolve().parent


def candidate_roots(root: Path | None = None) -> list[Path]:
    """运行时可能出现的所有根目录，按优先级排列。"""
    if root is not None:
        return [Path(root)]
    if getattr(sys, "frozen", False):
        return [Path(sys.executable).resolve().parent]
    here = Path(__file__).resolve().parent
    # 开发态：本包目录（portable/ 在这儿），以及仓库根（portable/ 挪到根下时）。
    return [here, here.parent]


def _is_runtime_dir(path: Path) -> bool:
    """这个目录像不像一份可用的 WebView2 Fixed Version 运行时。"""
    if not path.is_dir():
        return False
    name = path.name
    if not name.startswith(RUNTIME_DIR_PREFIX):
        return False
    # 必须有标志性文件，否则可能是个空壳目录。
    return all((path / marker).is_file() for marker in RUNTIME_MARKER_FILES)


def find_bundled_runtime(root: Path | None = None) -> Path | None:
    """在 exe 旁边找随包运行时。

    支持两种布局：
      - `webview2/Microsoft.WebView2.FixedVersionRuntime.<版本>.<架构>/`
      - `webview2/` 直接就是运行时目录本身
    """
    for base in candidate_roots(root):
        for parent in BUNDLED_PARENT_DIRS:
            container = base / parent / BUNDLED_DIR_NAME if parent else base / BUNDLED_DIR_NAME
            if not container.is_dir():
                continue

            # 情况一：webview2/ 本身就是运行时目录
            if _is_runtime_dir(container):
                return container

            # 情况二：webview2/ 下有若干版本目录，取最新的一个
            candidates = [
                p for p in container.iterdir() if p.is_dir() and _is_runtime_dir(p)
            ]
            if candidates:
                # 目录名里的版本号是单调的，按名称倒序取第一个即最新。
                return sorted(candidates, key=lambda p: p.name, reverse=True)[0]
    return None


def system_runtime_installed() -> bool:
    """系统里是否装了 Evergreen WebView2 Runtime。

    与 pywebview `_is_chromium()` 的判定保持一致：查注册表里
    EdgeUpdate\\Clients 的 WebView2 运行时键。
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False

    key = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    paths = (
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{key}",
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{key}",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in paths:
            try:
                with winreg.OpenKey(hive, path) as reg:
                    version, _ = winreg.QueryValueEx(reg, "pv")
                    if version and str(version) != "0":
                        return True
            except OSError:
                continue
    return False


def resolve_runtime(root: Path | None = None) -> tuple[Path | None, str]:
    """定位要用的运行时，返回 (路径, 来源说明)。

    优先级：
      1. 随包运行时 —— 目标机没装系统运行时时的唯一救命稻草；
      2. 系统运行时 —— 存在则不指定路径，让 WebView2 自己挑已装版本。
    """
    bundled = find_bundled_runtime(root)
    if bundled is not None:
        return bundled, "随包运行时"
    if system_runtime_installed():
        return None, "系统安装的 WebView2"
    return None, "未找到"


def apply_runtime(webview_module: Any, root: Path | None = None) -> dict[str, Any]:
    """把运行时写进 pywebview 设置，并返回诊断信息。

    必须在 `webview.start()` 之前调用 —— winforms 后端在那一刻才导入，
    模块级的 `is_chromium` 也在那一刻才求值。

    返回的字典会进日志，排查时一眼能看出「到底用了哪个运行时」。
    """
    path, source = resolve_runtime(root)

    info: dict[str, Any] = {
        "source": source,
        "path": str(path) if path else "",
        "applied": False,
        "system_installed": system_runtime_installed(),
    }

    if path is None:
        # 没找到随包运行时。若系统装了，pywebview 会自己查注册表；
        # 若系统也没装，它会静默回落 MSHTML —— 那就是白屏。
        return info

    try:
        webview_module.settings["WEBVIEW2_RUNTIME_PATH"] = str(path)
        info["applied"] = True
    except Exception as exc:  # noqa: BLE001
        info["error"] = repr(exc)
    return info


def renderer_in_use() -> str:
    """当前 pywebview 实际选中的渲染后端。

    只有在 winforms 模块已经导入后才有意义（`webview.start()` 之后）。
    """
    module = sys.modules.get("webview.platforms.winforms")
    return str(getattr(module, "renderer", "") or "")


def describe() -> str:
    """一行话说明运行时状况，用于日志。"""
    path, source = resolve_runtime()
    if path is not None:
        return f"{source}：{path}"
    if source == "系统安装的 WebView2":
        return "系统安装的 WebView2（未指定路径）"
    return (
        "未找到任何 WebView2 运行时 —— 面板会回落到 MSHTML(IE 内核) 并白屏。"
        "请随 exe 分发 webview2/ 目录，或在目标机安装 "
        "Microsoft Edge WebView2 Evergreen Runtime。"
    )


def environment_summary() -> dict[str, Any]:
    """给体检报告用的摘要。"""
    path, source = resolve_runtime()
    return {
        "ok": path is not None or system_runtime_installed(),
        "source": source,
        "path": str(path) if path else "",
        "system_installed": system_runtime_installed(),
        "detail": describe(),
    }


if __name__ == "__main__":
    # 手工验证用：python webview_runtime.py
    sys.stdout.reconfigure(encoding="utf-8")
    print(describe())
    print(f"app_root = {app_root()}")
    print(f"bundled  = {find_bundled_runtime()}")
