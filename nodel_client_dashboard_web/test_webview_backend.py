"""WebView 后端选择的集成测试：确认面板永远不静默回落到 MSHTML。

背景
----
pywebview 在 Windows 上的选择逻辑是：

    def _is_chromium():
        if settings['WEBVIEW2_RUNTIME_PATH']:
            return True
        ...  # 查注册表
        return False

    is_chromium = not is_cef and _is_chromium() and forced_gui_ != 'mshtml'
    if is_chromium:  ... edgechromium
    else:            ... mshtml   # 静默回落 → 白屏

「有问题的电脑」（Windows Server 2022）就是注册表查不到的那一类。

跑法：python -m pytest test_webview_backend.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webview_runtime  # noqa: E402


def test_bundled_runtime_forces_chromium_even_without_registry() -> None:
    """没有系统运行时时，随包运行时也必须让 pywebview 选 Chromium。

    这就是出问题的机器上的那条路径：
    注册表查不到 → 若不指定随包运行时就会回落 IE → 白屏。
    """
    import tempfile

    import webview

    class _FakeWebview:
        def __init__(self) -> None:
            self.settings = {"WEBVIEW2_RUNTIME_PATH": None}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rt = root / "webview2" / "Microsoft.WebView2.FixedVersionRuntime.1.0.0.0.x64"
        rt.mkdir(parents=True)
        (rt / "msedgewebview2.exe").write_bytes(b"MZ")
        (rt / "msedge.dll").write_bytes(b"MZ")

        fake = _FakeWebview()
        info = webview_runtime.apply_runtime(fake, root)
        assert info["applied"] is True, info

    # 现在 pywebview 自己的判定逻辑（复刻 winforms._is_chromium）
    original = webview.settings["WEBVIEW2_RUNTIME_PATH"]
    try:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = fake.settings["WEBVIEW2_RUNTIME_PATH"]
        from webview.platforms import winforms

        assert winforms._is_chromium() is True, (
            "指定了随包运行时，pywebview 仍判定为非 Chromium —— 会回落 IE 内核白屏"
        )
    finally:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = original


def test_resolve_reports_missing_everywhere() -> None:
    """既没系统运行时也没随包运行时时，必须如实报告「未找到」。

    这种情况下面板会回落 IE —— app_web 会在 start() 后弹窗提示。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path, source = webview_runtime.resolve_runtime(Path(tmp))
        assert path is None
        assert source in ("未找到", "系统安装的 WebView2"), source


def test_describe_warns_when_nothing_found() -> None:
    """没有任何运行时时，描述文字要明确说出后果（回落 IE + 白屏）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path, source = webview_runtime.resolve_runtime(Path(tmp))
        if source == "未找到":
            text = webview_runtime.describe()
            assert "MSHTML" in text or "白屏" in text, text


def test_renderer_in_use_returns_string() -> None:
    """renderer_in_use() 在 winforms 未导入时也要安全返回。"""
    assert isinstance(webview_runtime.renderer_in_use(), str)


def test_app_web_applies_runtime_before_start() -> None:
    """静态检查：app_web 必须在 webview.start() 之前调用 apply_runtime。

    顺序错了这个修复就完全无效 —— winforms 后端在 start() 那一刻才导入，
    模块级的 is_chromium 也在那时才求值。
    """
    app_web = Path(__file__).resolve().parent / "app_web.py"
    text = app_web.read_text(encoding="utf-8")

    apply_idx = text.find("apply_runtime(webview)")
    start_idx = text.find("webview.start()")
    assert apply_idx != -1, "app_web 里找不到 apply_runtime 调用"
    assert start_idx != -1, "app_web 里找不到 webview.start()"
    assert apply_idx < start_idx, (
        "apply_runtime 必须早于 webview.start() —— 晚一步就来不及了"
    )


def test_app_web_checks_renderer_after_start() -> None:
    """静态检查：app_web 必须在 start() 之后检查实际 renderer。

    只有那时才知道 pywebview 最终选了哪个内核。
    """
    app_web = Path(__file__).resolve().parent / "app_web.py"
    text = app_web.read_text(encoding="utf-8")

    start_idx = text.find("webview.start()")
    check_idx = text.find('renderer == "mshtml"')
    assert check_idx != -1, "app_web 里没有 mshtml 回落检查"
    assert check_idx > start_idx, "renderer 检查必须晚于 webview.start()"


if __name__ == "__main__":
    tests = [
        test_bundled_runtime_forces_chromium_even_without_registry,
        test_resolve_reports_missing_everywhere,
        test_describe_warns_when_nothing_found,
        test_renderer_in_use_returns_string,
        test_app_web_applies_runtime_before_start,
        test_app_web_checks_renderer_after_start,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
    print("-" * 50)
    print(f"失败 {failures} / {len(tests)}")
    sys.exit(1 if failures else 0)
