# -*- mode: python ; coding: utf-8 -*-
"""WebView 版运维面板的 PyInstaller spec（onefile GUI）。

与 TK 版 `nodel_client_dashboard/dashboard.spec` 并存，互不影响。

关键点：
  - `webview/lib` 下的 Microsoft.Web.WebView2.*.dll 与 WebView2Loader.dll 必须打包，
    否则冻结后 CLR 解析不到 WebView2 托管程序集；
  - `clr_loader` 的 ClrLoader.dll（.NET Framework 加载器）与 pythonnet 的
    Python.Runtime.dll 必须打包；
  - 前端产物 `frontend-console/dist` 作为数据文件打进 `_internal`；
  - 排除 tkinter / PyQt / PySide / matplotlib / numpy，省约 6MB。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

SPEC_DIR = Path(SPECPATH)
REPO_ROOT = SPEC_DIR.parent

datas: list = []
binaries: list = []
hiddenimports: list = [
    # 打包前由 build_version.py stamp 生成
    "_build_info",
    # 版本更新编排层：在 dashboard_bridge.__init__ 里延迟导入，
    # 显式列出以免 PyInstaller 漏掉（漏了 = 冻结后点「版本更新」直接崩）
    "version_update",
    # 启动诊断模块：main.py 与 dashboard_bridge 都延迟导入它
    "diagnostics",
    # WebView2 运行时定位模块：app_web 在 start() 前延迟导入它，
    # 漏掉 = 冻结后不会设置 WEBVIEW2_RUNTIME_PATH，pywebview 会回落 IE 内核
    "webview_runtime",
    # webview 的平台后端是动态 import，必须显式列出
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "pythonnet",
]

# —— pywebview 的 WebView2 程序集与原生加载器 ——
webview_datas = collect_data_files("webview", subdir="lib")
webview_binaries = collect_dynamic_libs("webview")
datas += webview_datas
binaries += webview_binaries

# —— pythonnet / clr_loader ——
try:
    binaries += collect_dynamic_libs("clr_loader")
    binaries += collect_dynamic_libs("pythonnet")
except Exception:
    pass

# —— MetaTrader5（导入节点时附着本机终端读取当前登录号）——
# 与 TK 版 dashboard.spec 同源。必须 collect_all：MetaTrader5 的核心是
# 一个 .pyd 扩展，PyInstaller 不会自动发现它。
try:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all("MetaTrader5")
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
except Exception:
    pass

# —— numpy ——
# MetaTrader5 声明依赖 numpy，且实际是硬依赖：缺了它
# `import MetaTrader5` 直接抛 "numpy._core.multiarray failed to import"。
# 所以 numpy 必须一起打包，且不能出现在 excludes 里。
try:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all("numpy")
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
except Exception:
    pass

# —— 前端产物 ——
# 注意：PyInstaller datas 的第二项是「目标目录」，不是目标文件路径。
# 传文件路径会解包成 `.../index-xxx.js/index-xxx.js`，目录判断随之失败。
_frontend = REPO_ROOT / "frontend-console" / "dist"
if _frontend.is_dir():
    for item in sorted(_frontend.rglob("*")):
        if not item.is_file():
            continue
        rel_dir = item.parent.relative_to(_frontend)
        target = str(Path("frontend-console") / "dist" / rel_dir).replace("\\", "/")
        datas.append((str(item.resolve()), target))
else:
    print("[dashboard-web.spec] WARNING: frontend-console/dist 不存在，前端将不可用")

# —— 图标 ——
_icon_candidates = [
    SPEC_DIR / "logo.ico",
    REPO_ROOT / "nodel_client_dashboard" / "logo.ico",
    REPO_ROOT / "nodel_client_dashboard" / "yidiandian.ico",
]
_icon = next((p for p in _icon_candidates if p.is_file()), None)
if _icon is not None:
    datas.append((str(_icon.resolve()), "."))

block_cipher = None

a = Analysis(
    [str(SPEC_DIR / "main.py")],
    pathex=[str(SPEC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        # 注意：numpy 不能排除。MetaTrader5 声明依赖它，
        # 排除后冻结产物里 import MetaTrader5 会失败，
        # 导入节点时就读不到终端当前登录号。
        "pandas",
        "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="亿点点控制台 - WebView",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon.resolve()) if _icon is not None else None,
    uac_admin=True,
)
