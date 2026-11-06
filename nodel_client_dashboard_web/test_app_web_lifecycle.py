"""回归测试：窗口存活期间 HTTP 桥不能被误关。

背景：`app_web.run_app()` 起了一个 `_watch()` 线程负责在窗口关闭后
清理桥接层。旧实现轮询 `window.exists()`，但当前 pywebview 版本的
`Window` 并没有 `exists` 方法 —— AttributeError 被 `except Exception`
吞掉后直接 break，于是窗口刚要加载页面时 `bridge.stop()` 就把 HTTP
服务关掉了，用户看到「无法访问此页面」。

这个测试不依赖真窗口，只验证「存活检测用的 API 真实存在」，
否则同类错误会再次以静默方式回归。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_window_liveness_api_exists() -> None:
    """pywebview 的 Window 必须提供我们用来判断「窗口还在」的 API。"""
    import webview.window

    assert hasattr(webview.window.Window, "destroy")
    # events 是实例属性，这里检查实例
    window = webview.window.Window(uid="t", title="t", url=None)
    assert hasattr(window.events, "closed")
    assert hasattr(window.events.closed, "wait")


def test_app_web_does_not_use_nonexistent_window_exists() -> None:
    """`app_web` 里不能出现 `window.exists()` —— 该方法在 pywebview 中不存在。"""
    src = (Path(__file__).resolve().parent / "app_web.py").read_text(encoding="utf-8")
    # 只看真实代码调用，不看注释/docstring
    tree = ast.parse(src)
    called: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "exists"
        ):
            called.append(ast.unparse(node.func))
    assert not called, f"app_web 仍在调用不存在的 API：{called}"


def test_app_web_uses_closed_event_for_cleanup() -> None:
    """清理桥接层应挂 `events.closed`，而不是轮询。"""
    src = (Path(__file__).resolve().parent / "app_web.py").read_text(encoding="utf-8")
    assert "events.closed" in src, "app_web 应使用 window.events.closed 等待窗口关闭"


if __name__ == "__main__":
    tests = [
        test_window_liveness_api_exists,
        test_app_web_does_not_use_nonexistent_window_exists,
        test_app_web_uses_closed_event_for_cleanup,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[ok] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
    sys.exit(1 if failed else 0)
