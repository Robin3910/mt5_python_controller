"""裁剪规则的验证工具：确认裁剪后的固定版 WebView2 Runtime 仍能启动。

为什么需要它
------------
`prune_runtime.py` 的每条规则都在删微软官方的运行时文件。删错的症状是
「WebView2 初始化失败 / FileNotFoundException」—— 窗口根本起不来，
而且报错完全指不到是哪个文件被删了。

所以裁剪规则**必须**用真实启动来验证，不能只看文件在不在。
本脚本做两件事：
  1. 起一个真实的 WebView2 窗口，访问页面，确认能挂载；
  2. 支持逐组试探，找出是哪条规则导致了失败。

用法：
    python verify_runtime.py <运行时目录>              # 验证单个目录
    python verify_runtime.py --explore <完整运行时目录>  # 逐组试探可删项

依赖：pywebview + pythonnet + .NET Framework 4.6.2+。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

# 探针用的页面：只要能读到 marker 就说明 WebView2 正常工作
HTML = """
<!doctype html><html><head><meta charset="utf-8"></head>
<body style="background:#0a0e17;color:#e6edf7;font-family:sans-serif">
<div id="container"><h1 id="marker">BUNDLED-RUNTIME-OK</h1></div>
</body></html>
"""

MARKER = "BUNDLED-RUNTIME-OK"

# 逐组试探时用的候选清单
CANDIDATE_GROUPS: dict[str, list[str]] = {
    "pdf_engine": ["mspdf.dll", "PdfPreviewHandler.dll", "PdfPreview"],
    "onnx": ["onnxruntime.dll"],
    "mip": ["mip_core_gn.dll"],
    "telclient": ["telclient.dll"],
    "dual_engine": ["dual_engine_adapter_x64.dll"],
    "swiftshader": ["vk_swiftshader.dll", "vulkan-1.dll", "vk_swiftshader_icd.json"],
    "d3d_compiler": ["d3dcompiler_47.dll"],
    "dxcompiler": ["dxcompiler.dll"],
    "oneauth": ["oneauth.dll"],
    "trust_lists": ["Trust Protection Lists"],
    "widevine": ["WidevineCdm"],
    "installer": ["Installer"],
    "ebwebview": ["EBWebView"],
}


def probe_once(runtime_dir: Path) -> tuple[bool, dict]:
    """真起一个 WebView2 窗口，看页面能不能挂载。

    返回 (是否成功, 详情)。
    """
    import webview

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

    window = webview.create_window("verify", html=HTML, width=800, height=600)
    threading.Thread(target=worker, daemon=True).start()
    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        result["start_error"] = repr(exc)

    dom = result.get("dom")
    # evaluate_js 返回的是 JSON **字符串**，不是 dict
    if isinstance(dom, str):
        try:
            dom = json.loads(dom)
        except (TypeError, ValueError):
            dom = None
    ok = isinstance(dom, dict) and dom.get("markerText") == MARKER
    return ok, result


def verify(runtime_dir: Path) -> bool:
    ok, detail = probe_once(runtime_dir)
    print(f"{'OK  ' if ok else 'FAIL'} {runtime_dir}")
    if not ok:
        print(f"      {detail}")
    return ok


def explore(full_runtime: Path) -> bool:
    """逐组试探：哪些文件组删掉后运行时仍能启动。"""
    work = REPO_ROOT / ".toolchain" / "_wv2_explore"
    sys.path.insert(0, str(HERE))
    import prune_runtime

    def prepare(groups: list[str]) -> Path:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(full_runtime, work)
        prune_runtime.prune(work)
        for g in groups:
            for item in CANDIDATE_GROUPS[g]:
                p = work / item
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
        return work

    base_ok = verify(prepare([]))
    if not base_ok:
        print("基线就失败，无法试探")
        return False

    safe: list[str] = []
    for name in CANDIDATE_GROUPS:
        if verify(prepare(safe + [name])):
            safe.append(name)

    print(f"\n可安全删除：{safe}")
    print(f"全部一起删：{'OK' if verify(prepare(safe)) else 'FAIL'}")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--explore" in argv:
        idx = argv.index("--explore")
        target = Path(argv[idx + 1]) if len(argv) > idx + 1 else None
        if target is None:
            print("用法: python verify_runtime.py --explore <完整运行时目录>", file=sys.stderr)
            return 2
        return 0 if explore(target.resolve()) else 1

    if not argv:
        print("用法: python verify_runtime.py <运行时目录>", file=sys.stderr)
        return 2
    target = Path(argv[0]).resolve()
    if not target.is_dir():
        print(f"目录不存在: {target}", file=sys.stderr)
        return 1
    return 0 if verify(target) else 1


if __name__ == "__main__":
    raise SystemExit(main())
