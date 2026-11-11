"""WebView2 运行时裁剪的回归测试。

背景
----
Windows Server 2022 没预装 WebView2 Runtime，所以运行时必须**随 exe 一起
分发**（`portable/webview2/`）。完整运行时 678MB 太大，要裁剪到 ~430MB。

但裁剪规则一旦误伤，症状是 **WebView2 初始化失败**、窗口一片空白，
而日志里只有一行 HRESULT，指不到是哪个文件被删了。真实踩过的坑：

    少了 `<版本>.manifest`（SxS 并行配置清单）
        → msedgewebview2.exe 找不到自己的装配上下文
        → HRESULT 0x800736B1（ERROR_SXS_CANT_GEN_ACTCTX）
        → 界面白屏

所以裁剪规则必须保证**关键文件一个都不少**，并且真的能启动。

跑法：
    python -m pytest test_stage_portable.py -q
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage_portable as sp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_SOURCE = REPO_ROOT / ".toolchain" / "wv2_rt"


def _runtime_dir() -> Path:
    """定位一份完整的运行时源目录；没有就跳过这些测试。"""
    if not RUNTIME_SOURCE.is_dir():
        pytest.skip(f"运行时源目录不存在: {RUNTIME_SOURCE}")
    try:
        return sp.find_runtime_dir(RUNTIME_SOURCE)
    except FileNotFoundError:
        pytest.skip(f"运行时源目录里没有可用运行时: {RUNTIME_SOURCE}")


@pytest.fixture
def pruned(tmp_path):
    """裁剪一份运行时副本，返回裁剪后的目录。"""
    src = _runtime_dir()
    dest = tmp_path / src.name
    shutil.copytree(src, dest)
    sp.prune(dest)
    return dest


# ─────────────────────────────────────────────── 关键文件

def test_required_files_survive_pruning(pruned):
    """裁剪后关键文件一个都不能少。"""
    missing = sp.verify(pruned)
    assert missing == [], f"裁剪后缺少关键文件: {missing}"


def test_sxs_manifest_survives_pruning(pruned):
    """SxS 清单必须留下 —— 少了就是 0x800736B1 白屏。"""
    manifests = list(pruned.glob("*.manifest"))
    assert manifests, f"裁剪后没有 SxS 清单（会导致 0x800736B1）: {list(pruned.iterdir())}"


def test_prune_never_removes_protected_files(pruned):
    """裁剪函数自己也不能删到必保名单上的文件。"""
    for name in sp.REQUIRED_FILES:
        assert (pruned / name).is_file(), f"裁剪删掉了必保文件: {name}"
    for pattern in sp.REQUIRED_GLOBS:
        assert list(pruned.glob(pattern)), f"裁剪删掉了必保文件: {pattern}"


def test_prune_is_idempotent(pruned):
    """再裁一遍不会出事（也不会再删东西）。"""
    before = {p.relative_to(pruned).as_posix() for p in pruned.rglob("*") if p.is_file()}
    sp.prune(pruned)
    after = {p.relative_to(pruned).as_posix() for p in pruned.rglob("*") if p.is_file()}
    assert before == after, f"二次裁剪改变了文件集: {before ^ after}"


def test_locales_are_pruned_to_kept_set(pruned):
    """语言包只留中文和英文。"""
    locales = pruned / "Locales"
    assert locales.is_dir()
    names = {p.name for p in locales.iterdir() if p.is_file()}
    assert names == set(sp.KEEP_LOCALES), names


def test_pruning_actually_saves_space(pruned):
    """裁剪要真的省空间，否则没意义。"""
    src = _runtime_dir()
    src_size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    dst_size = sum(f.stat().st_size for f in pruned.rglob("*") if f.is_file())
    assert dst_size < src_size * 0.8, f"裁剪后仍有 {dst_size / 1024 / 1024:.0f}MB"


# ─────────────────────────────────────────────── 启动验证

def test_pruned_runtime_actually_starts(pruned):
    """**真起一个 WebView2 窗口** —— 文件在不在不算数，能启动才算。

    这是唯一能挡住「删错了文件但 verify() 恰好通过」的检查。
    需要 pywebview + .NET Framework；环境不支持时跳过。
    """
    try:
        import webview  # noqa: F401
    except ImportError:
        pytest.skip("pywebview 未安装")

    import json
    import threading
    import time

    webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(pruned)
    result: dict = {}

    def worker() -> None:
        time.sleep(6.0)
        try:
            # 注意：`evaluate_js` 返回的是表达式的**原始值**（字符串），
            # 不是 JSON —— 所以这里直接比字符串，别去 json.loads。
            result["dom"] = webview.windows[0].evaluate_js(
                "(document.querySelector('#marker')||{}).textContent||''"
            )
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
            dom = {"markerText": dom} if dom else None
    ok = isinstance(dom, dict) and dom.get("markerText") == "BUNDLED-RUNTIME-OK"
    assert ok, f"裁剪后的运行时起不来: {result}"
