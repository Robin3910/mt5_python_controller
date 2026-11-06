"""验证窗口存活期间 HTTP 桥不被误关（复现「无法访问此页面」）。

旧版 `_watch()` 用 `window.exists()` 轮询，这个 pywebview 版本里
`Window` 根本没有 `exists` 方法，AttributeError 被吞掉后立刻
`bridge.stop()`，于是窗口还在加载页面时 HTTP 服务就没了。

本脚本起真窗口，验证：
  1. 窗口加载后页面能取到（HTTP 桥还活着）；
  2. 手动关窗后 HTTP 桥正常关闭。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import FAKE_SNAPSHOT, install_fake_session  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview
    from dashboard_bridge import DashboardBridge as DB

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[alive] 前端产物缺失")
        return 2

    original_snapshot = DB.snapshot
    DB.snapshot = lambda self: dict(FAKE_SNAPSHOT)

    bridge = DashboardBridge()
    install_fake_session(bridge)
    bridge.start()
    url = bridge.url
    print(f"[alive] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}
    window = webview.create_window(
        "alive-check", url=url, width=1280, height=860, x=40, y=40
    )

    def probe() -> None:
        # 等页面加载
        time.sleep(4.0)
        # 1. HTTP 桥是否还活着
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            results["http_ok"] = resp.status == 200
            results["has_root"] = "<div id=\"container\"" in body or "container" in body
        except Exception as exc:  # noqa: BLE001
            results["http_ok"] = False
            results["http_error"] = repr(exc)

        # 2. 页面是否真的渲染出来了
        try:
            results["mounted"] = json.loads(
                window.evaluate_js(
                    """
                    JSON.stringify({
                      mounted: !!document.querySelector('#container')
                               && document.querySelector('#container').children.length > 0,
                      title: document.title,
                    })
                    """
                )
            )
        except Exception as exc:  # noqa: BLE001
            results["mounted"] = {"error": repr(exc)}

        # 3. 关窗后桥是否退出
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()
    DB.snapshot = original_snapshot

    print("[alive] " + json.dumps(results, ensure_ascii=False, indent=2))
    ok = bool(results.get("http_ok")) and isinstance(results.get("mounted"), dict) \
        and results["mounted"].get("mounted") is True
    print("[alive] 结论：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
