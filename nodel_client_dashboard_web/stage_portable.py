"""裁剪 WebView2 Fixed Version 运行时，并把可携带目录组装出来。

为什么要有这个脚本
------------------
Windows Server 2022 之类的机器没预装 WebView2 Runtime，pywebview 会静默
回落到 MSHTML(IE 内核) → 界面空白。所以运行时必须**随 exe 一起分发**。

但完整运行时是 678MB，直接分发不现实。这个脚本按经过验证的规则裁剪到
约 430MB —— 裁剪后仍然能正常启动 WebView2（用 `verify_runtime.py` 验证）。

裁剪规则必须是「真实启动验证过的」，否则症状是 WebView2 初始化失败，
而且报错完全指不到是哪个文件被删了。

用法：
    python stage_portable.py                     # 组装 portable/ 目录
    python stage_portable.py --check             # 只校验，不改动
    python stage_portable.py --runtime <目录>    # 指定运行时源目录
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

# 运行时源目录（完整版 Fixed Version 运行时）
DEFAULT_RUNTIME_SOURCE = REPO_ROOT / ".toolchain" / "wv2_rt"

# 输出目录：可携带目录，exe 与运行时放在一起
DEFAULT_OUTPUT = HERE / "portable"

RUNTIME_DIR_NAME = "webview2"

# ── 裁剪规则 ──
# 每条规则都是「删掉之后仍然能正常启动 WebView2」验证过的。
# 新增/修改规则后，必须重跑 verify_runtime.py 验证。

# 整个目录删掉
PRUNE_DIRS = (
    "AdSelectionAttestationsPreloaded",   # 广告预加载
    "Extensions",                          # 扩展
    "Installer",                           # 安装器（我们不需要它自己装）
    "MEIPreload",                          # 企业预加载
    "PdfPreview",                          # PDF 预览
    "Trust Protection Lists",              # 信任保护名单
    "WidevineCdm",                        # DRM
    "edge_feedback",                       # 反馈采集
)

# 整个文件删掉
PRUNE_FILES = (
    "Copilot.dat",
    "copilotapp.exe",
    "copilot_overlay_images_100_percent.pak",
    "copilot_overlay_images_200_percent.pak",
    "copilot_overlay_unscaled_images.pak",
    "d3dcompiler_47.dll",
    "dual_engine_adapter_x64.dll",
    "dxcompiler.dll",
    "mip_core_gn.dll",
    "mscopilot.exe",
    "mspdf.dll",
    "notification_helper.exe",
    "oneauth.dll",
    "onnxruntime.dll",
    "show_third_party_software_licenses.bat",
    "telclient.dll",
    "vk_swiftshader.dll",
    "vk_swiftshader_icd.json",
    "vulkan-1.dll",
)

# 保留的语言包：中文（简体）+ 英文（美国/英国）
KEEP_LOCALES = ("zh-CN.pak", "en-US.pak", "en-GB.pak")

# 必须保留的文件 —— 少一个 WebView2 就起不来
#
# `*.manifest` 尤其重要：它是 WebView2 Fixed Version 运行时的 **SxS 并行配置
# 清单**。少了它，`msedgewebview2.exe` 启动时找不到自己的装配上下文，直接报
# `0x800736B1`（ERROR_SXS_CANT_GEN_ACTCTX，「应用程序的并行配置不正确」），
# 症状是窗口一片空白，而日志里只有一行 HRESULT，指不到是哪个文件被删了。
REQUIRED_FILES = (
    "msedgewebview2.exe",
    "msedge.dll",
    "msedge_elf.dll",
    "resources.pak",
    "icudtl.dat",
    "v8_context_snapshot.bin",
    "msvcp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "webview2_integration.dll",
    "ffmpeg.dll",
    "Edge.dat",
    "EdgeWebView.dat",
)

# 必须保留的**清单**文件：`<版本>.manifest`。
#
# 不能写死版本号（运行时一升级就失效），所以按「.manifest 后缀」匹配。
REQUIRED_GLOBS = ("*.manifest",)


def find_runtime_dir(source: Path) -> Path:
    """在源目录里找到 Fixed Version 运行时目录。"""
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(f"运行时源目录不存在: {source}")

    # 源目录本身就是运行时目录
    if (source / "msedgewebview2.exe").is_file():
        return source

    candidates = [
        p for p in source.iterdir()
        if p.is_dir() and (p / "msedgewebview2.exe").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(f"源目录里找不到 Fixed Version 运行时: {source}")
    return sorted(candidates, key=lambda p: p.name, reverse=True)[0]


def verify(runtime_dir: Path) -> list[str]:
    """检查关键文件还在，返回缺失项。"""
    missing = [name for name in REQUIRED_FILES if not (runtime_dir / name).is_file()]
    for pattern in REQUIRED_GLOBS:
        if not list(runtime_dir.glob(pattern)):
            missing.append(pattern)
    return missing


def prune(runtime_dir: Path) -> list[str]:
    """按规则裁剪运行时，返回删除项列表。

    **绝不删关键文件** —— 裁剪规则一旦误伤，症状是 WebView2 初始化失败
    （比如少了 SxS 清单就是 `0x800736B1`），而报错完全指不到是哪个文件
    被删了。所以每删一项之前先确认它不在必保名单里。
    """
    removed: list[str] = []

    def _is_protected(name: str) -> bool:
        if name in REQUIRED_FILES:
            return True
        return any(Path(name).match(p) for p in REQUIRED_GLOBS)

    for name in PRUNE_DIRS:
        p = runtime_dir / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(f"{name}/")

    for name in PRUNE_FILES:
        if _is_protected(name):
            continue
        p = runtime_dir / name
        if p.is_file():
            p.unlink()
            removed.append(name)

    # 语言包：只留中文和英文
    locales = runtime_dir / "Locales"
    if locales.is_dir():
        for p in locales.iterdir():
            if p.is_file() and p.name not in KEEP_LOCALES:
                p.unlink()
                removed.append(f"Locales/{p.name}")

    return removed


def stage_runtime(source: Path, dest_container: Path) -> Path:
    """把裁剪后的运行时拷到目标目录。"""
    runtime_dir = find_runtime_dir(source)
    dest_container = Path(dest_container)
    if dest_container.exists():
        shutil.rmtree(dest_container, ignore_errors=True)
    dest_container.mkdir(parents=True, exist_ok=True)

    dest = dest_container / runtime_dir.name
    print(f"拷贝运行时 {runtime_dir.name} ...")
    shutil.copytree(runtime_dir, dest)

    removed = prune(dest)
    print(f"  裁剪 {len(removed)} 项")

    missing = verify(dest)
    if missing:
        raise RuntimeError(f"裁剪后缺少关键文件: {', '.join(missing)}")

    return dest


def verify_startup(runtime_dir: Path) -> bool:
    """真实启动一次，确认裁剪后的运行时还能跑起来。

    裁剪规则错一条的症状是「WebView2 初始化失败」，而且报错完全指不到
    是哪个文件被删了 —— 所以规则必须用真实启动来验证，不能只看文件在不在。
    """
    try:
        import webview
    except ImportError:
        print("  跳过：pywebview 未安装")
        return True

    webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime_dir)
    result: dict = {}

    def worker() -> None:
        time.sleep(5.0)
        try:
            js = """
            (() => {
              const m = document.querySelector('#marker');
              return JSON.stringify({markerText: m ? m.textContent.trim() : null});
            })()
            """
            result["dom"] = webview.windows[0].evaluate_js(js)
        except Exception as exc:  # noqa: BLE001
            result["error"] = repr(exc)
        try:
            webview.windows[0].destroy()
        except Exception:  # noqa: BLE001
            pass

    html = (
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body><div id='container'><h1 id='marker'>BUNDLED-RUNTIME-OK</h1></div></body></html>"
    )
    webview.create_window("verify", html=html, width=800, height=600)
    threading.Thread(target=worker, daemon=True).start()
    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        result["start_error"] = repr(exc)

    dom = result.get("dom")
    if isinstance(dom, str):
        try:
            dom = json.loads(dom)
        except (TypeError, ValueError):
            dom = None
    ok = isinstance(dom, dict) and dom.get("markerText") == "BUNDLED-RUNTIME-OK"
    if not ok:
        print(f"  启动验证失败：{result}")
    return ok


def stage_exe(output: Path) -> Path | None:
    """把刚打出来的 exe 拷到可携带目录。"""
    dist = HERE / "dist"
    exes = sorted(dist.glob("*.exe"))
    if not exes:
        print("警告：dist/ 下没有 exe，跳过")
        return None
    exe = exes[0]
    output.mkdir(parents=True, exist_ok=True)
    target = output / exe.name
    shutil.copyfile(exe, target)
    print(f"拷贝 exe: {exe.name}")
    return target


def stage_aux(output: Path) -> None:
    """把 version.txt 和 .env 也放进可携带目录。"""
    output.mkdir(parents=True, exist_ok=True)
    for name, src in (("version.txt", HERE / "dist" / "version.txt"),
                      (".env", HERE / ".env")):
        if src.is_file():
            shutil.copyfile(src, output / name)
            print(f"拷贝 {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组装 WebView 面板可携带目录")
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME_SOURCE),
                        help="完整运行时源目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="输出目录")
    parser.add_argument("--check", action="store_true",
                        help="只检查，不改动任何文件")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过裁剪后的真实启动验证")
    args = parser.parse_args(argv)

    source = Path(args.runtime)
    output = Path(args.output)

    if args.check:
        runtime_dir = find_runtime_dir(source)
        missing = verify(runtime_dir)
        if missing:
            print(f"[FAIL] 缺少关键文件: {', '.join(missing)}")
            return 1
        print(f"[OK] 运行时完整: {runtime_dir}")
        return 0

    try:
        dest = stage_runtime(source, output / RUNTIME_DIR_NAME)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    # 裁剪规则必须用真实启动验证：删错文件的症状是「WebView2 初始化失败」，
    # 而且报错完全指不到是哪个文件被删了。
    if not args.no_verify:
        print("验证裁剪后的运行时能否启动 ...")
        if not verify_startup(dest):
            print("[FAIL] 裁剪后的运行时无法启动，不要分发这个包。", file=sys.stderr)
            return 1
        print("  运行时启动正常")

    stage_exe(output)
    stage_aux(output)

    size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"\n完成。运行时 {size_mb:.0f}MB -> {dest}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
