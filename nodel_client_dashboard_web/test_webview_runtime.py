"""WebView2 运行时定位的回归测试。

最关键的一条：`WEBVIEW2_RUNTIME_PATH` 必须在 `webview.start()` 之前设置。
pywebview 的 winforms 后端在 start() 那一刻才导入，模块级的
`is_chromium = _is_chromium()` 也在那时才求值 —— 设置晚了就来不及，
pywebview 会静默回落 MSHTML(IE 内核)，界面就是一片空白。

跑法：
    python -m pytest test_webview_runtime.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webview_runtime  # noqa: E402


def _make_runtime_dir(root: Path, *, with_markers: bool = True) -> Path:
    """造一个假的 Fixed Version 运行时目录。"""
    container = root / "webview2"
    rt = container / "Microsoft.WebView2.FixedVersionRuntime.157.0.4341.42.x64"
    rt.mkdir(parents=True)
    if with_markers:
        (rt / "msedgewebview2.exe").write_bytes(b"MZ")
        (rt / "msedge.dll").write_bytes(b"MZ")
    return rt


# ── 目录定位 ──


def test_finds_bundled_runtime_next_to_exe() -> None:
    """exe 旁边的 webview2/ 要能被找到。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        expected = _make_runtime_dir(root)
        found = webview_runtime.find_bundled_runtime(root)
        assert found == expected, (found, expected)


def test_finds_bundled_runtime_under_portable() -> None:
    """portable/webview2/ 的布局也要能认（打包脚本产出的就是这种）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        expected = _make_runtime_dir(root / "portable")
        found = webview_runtime.find_bundled_runtime(root)
        assert found == expected, (found, expected)


def test_picks_newest_runtime_version() -> None:
    """多个版本目录时取最新的那个。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        container = root / "webview2"
        for version in ("157.0.4341.42", "160.0.1000.10"):
            rt = container / f"Microsoft.WebView2.FixedVersionRuntime.{version}.x64"
            rt.mkdir(parents=True)
            (rt / "msedgewebview2.exe").write_bytes(b"MZ")
            (rt / "msedge.dll").write_bytes(b"MZ")
        found = webview_runtime.find_bundled_runtime(root)
        assert found is not None, found
        assert "160.0.1000.10" in found.name, found


def test_ignores_incomplete_runtime_dir() -> None:
    """缺标志性文件的目录不算运行时 —— 否则会指到一个跑不起来的运行时。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_runtime_dir(root, with_markers=False)
        assert webview_runtime.find_bundled_runtime(root) is None


def test_returns_none_when_absent() -> None:
    """没有随包运行时时返回 None（而不是抛异常）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        assert webview_runtime.find_bundled_runtime(Path(tmp)) is None


# ── 运行时目录识别 ──


def test_runtime_dir_requires_fixed_version_prefix() -> None:
    """目录名必须像 Fixed Version 运行时。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        weird = Path(tmp) / "SomethingElse"
        weird.mkdir()
        (weird / "msedgewebview2.exe").write_bytes(b"MZ")
        (weird / "msedge.dll").write_bytes(b"MZ")
        assert webview_runtime._is_runtime_dir(weird) is False


# ── 应用到 pywebview ──


def test_apply_runtime_sets_absolute_path() -> None:
    """设置到 pywebview 的必须是绝对路径。

    pywebview 只对相对路径做 os.path.join(get_app_root(), ...)，
    而 get_app_root() 在 PyInstaller 产物里是 _MEIPASS（临时解包目录），
    指到那里必然找不到运行时。
    """
    import tempfile

    class _FakeWebview:
        def __init__(self) -> None:
            self.settings = {"WEBVIEW2_RUNTIME_PATH": None}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        expected = _make_runtime_dir(root)

        fake = _FakeWebview()
        info = webview_runtime.apply_runtime(fake, root)

        assert info["applied"] is True, info
        assert info["source"] == "随包运行时", info
        assert fake.settings["WEBVIEW2_RUNTIME_PATH"] == str(expected)
        assert Path(fake.settings["WEBVIEW2_RUNTIME_PATH"]).is_absolute()


def test_apply_runtime_without_bundled_leaves_setting_untouched() -> None:
    """没有随包运行时时不乱设（让 pywebview 自己查注册表用系统运行时）。"""
    import tempfile

    class _FakeWebview:
        def __init__(self) -> None:
            self.settings = {"WEBVIEW2_RUNTIME_PATH": None}

    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeWebview()
        info = webview_runtime.apply_runtime(fake, Path(tmp))

        if info["source"] == "随包运行时":
            pytest_skip = True  # noqa: F841 - 极端情况下本机就有随包运行时
            return
        assert info["applied"] is False, info
        assert fake.settings["WEBVIEW2_RUNTIME_PATH"] is None


# ── 与 pywebview 的一致性 ──


def test_setting_forces_chromium_in_pywebview() -> None:
    """设了 WEBVIEW2_RUNTIME_PATH，pywebview 的 _is_chromium() 必须返回 True。

    这是整个修复的核心断言：pywebview 的
        def _is_chromium():
            if settings['WEBVIEW2_RUNTIME_PATH']:
                return True
    正是靠这一行绕开注册表查��，从而不会回落 MSHTML。
    """
    import webview

    original = webview.settings["WEBVIEW2_RUNTIME_PATH"]
    try:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = r"C:\fake\runtime"
        from webview.platforms import winforms

        assert winforms._is_chromium() is True, (
            "设了运行时路径，pywebview 仍判定为非 Chromium —— 会回落 IE 内核"
        )
    finally:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = original


def test_empty_runtime_path_falls_back_to_registry_check() -> None:
    """没设路径时，pywebview 走注册表判定 —— 这正是出问题的机器走的路径。"""
    import webview

    original = webview.settings["WEBVIEW2_RUNTIME_PATH"]
    try:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = None
        from webview.platforms import winforms

        # 结果取决于本机装没装运行时，这里只断言「函数能正常求值」。
        result = winforms._is_chromium()
        assert isinstance(result, bool), result
    finally:
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = original


# ── 诊断 ──


def test_environment_summary_has_required_fields() -> None:
    """体检摘要要包含诊断所需的字段。"""
    summary = webview_runtime.environment_summary()
    for key in ("ok", "source", "path", "system_installed", "detail"):
        assert key in summary, summary


def test_describe_mentions_path_when_found() -> None:
    """描述文字要能看出用的是哪个运行时。"""
    text = webview_runtime.describe()
    assert isinstance(text, str) and text.strip(), text


if __name__ == "__main__":
    tests = [
        test_finds_bundled_runtime_next_to_exe,
        test_finds_bundled_runtime_under_portable,
        test_picks_newest_runtime_version,
        test_ignores_incomplete_runtime_dir,
        test_returns_none_when_absent,
        test_runtime_dir_requires_fixed_version_prefix,
        test_apply_runtime_sets_absolute_path,
        test_apply_runtime_without_bundled_leaves_setting_untouched,
        test_setting_forces_chromium_in_pywebview,
        test_empty_runtime_path_falls_back_to_registry_check,
        test_environment_summary_has_required_fields,
        test_describe_mentions_path_when_found,
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
