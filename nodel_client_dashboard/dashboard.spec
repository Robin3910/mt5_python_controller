# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 由 build_exe.bat 调用（onefile GUI）。"""
from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas: list = []
binaries: list = []
hiddenimports: list = ["customtkinter", "pystray", "PIL", "PIL.Image", "PIL.ImageDraw"]

try:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all("customtkinter")
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
except Exception:
    pass

for pkg in ("pystray", "PIL"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="node_client_dashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
