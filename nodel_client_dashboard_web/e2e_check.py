"""端到端验证：真实前端产物 + 真实桥接层，在 WebView2 里跑一遍。

验证点：
  1. 桥能提供 frontend-console/dist 的静态资源
  2. 页面加载后 Vue 挂载成功（不是白屏）
  3. 登录页元素真的存在（账号 / 密码输入框可见）
  4. SSE 连接建立，收到快照
  5. 截图留证
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402


def main() -> int:
    import webview

    dist = frontend_dist_dir()
    if not (dist / "index.html").is_file():
        print(f"[e2e] 前端产物缺失：{dist}")
        print("      请先在 frontend-console 目录执行 npm run build")
        return 2

    bridge = DashboardBridge()
    port = bridge.start()
    url = bridge.url
    print(f"[e2e] bridge on 127.0.0.1:{port}", flush=True)

    screenshot = Path(__file__).resolve().parent / "e2e_login.png"
    results: dict[str, object] = {}

    window = webview.create_window(
        "e2e · 亿点点控制台",
        url=url,
        width=1280,
        height=820,
        x=40,
        y=40,
    )

    def probe() -> None:
        time.sleep(3.0)
        try:
            js = """
            (() => {
              const q = (s) => document.querySelector(s);
              const user = q('#login-user');
              const pass = q('#login-pass');
              const root = q('#container');
              return JSON.stringify({
                mounted: !!root && root.children.length > 0,
                hasUser: !!user,
                hasPass: !!pass,
                userVisible: !!user && user.getBoundingClientRect().height > 0,
                passVisible: !!pass && pass.getBoundingClientRect().height > 0,
                title: document.title,
                loginFrame: !!q('.login-frame'),
                bodyText: (document.body.innerText || '').slice(0, 160),
              });
            })()
            """
            raw = window.evaluate_js(js)
            results["dom"] = json.loads(raw) if raw else {}
        except Exception as exc:  # noqa: BLE001
            results["dom_error"] = repr(exc)

        # SSE：订阅后应收到 locked 或 snapshot。
        # evaluate_js 是同步的，不能直接 await Promise；
        # 所以先把结果写进 window.__sseResult，再轮询读。
        try:
            window.evaluate_js(
                """
                window.__sseResult = 'pending';
                const es = new EventSource('/api/events?token=' +
                  new URLSearchParams(location.search).get('token'));
                const t = setTimeout(() => { es.close(); window.__sseResult = 'timeout'; }, 6000);
                es.addEventListener('snapshot', (e) => {
                  clearTimeout(t); es.close();
                  window.__sseResult = JSON.parse(e.data).type;
                });
                es.onerror = () => { window.__sseResult = 'error'; };
                """
            )
            for _ in range(20):
                time.sleep(0.5)
                value = window.evaluate_js("window.__sseResult")
                if value and value != "pending":
                    results["sse"] = value
                    break
            else:
                results["sse"] = window.evaluate_js("window.__sseResult")
        except Exception as exc:  # noqa: BLE001
            results["sse_error"] = repr(exc)

        try:
            from PIL import ImageGrab
            x, y = window.x, window.y
            w, h = window.width, window.height
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(screenshot)
            results["screenshot"] = str(screenshot)
        except Exception as exc:  # noqa: BLE001
            results["screenshot_error"] = repr(exc)

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()

    print("[e2e] " + json.dumps(results, ensure_ascii=False, indent=2))

    dom = results.get("dom")
    ok = isinstance(dom, dict) and dom.get("mounted") and dom.get("hasUser") and dom.get("hasPass")
    print("[e2e] 结论：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
