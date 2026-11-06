"""打包产物的视觉验收：跑真实前端产物，截屏 + 抓 DOM 关键字段。

只用于开发/验收，不参与交付。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    import webview

    sys.path.insert(0, str(HERE))
    from dashboard_bridge import DashboardBridge

    bridge = DashboardBridge()
    bridge.start()
    print(f"[shot] bridge {bridge.url}", flush=True)

    out = HERE / "e2e_console.png"
    results: dict[str, object] = {}

    window = webview.create_window(
        "shot", url=bridge.url, width=1440, height=980, x=40, y=40,
    )

    def probe() -> None:
        time.sleep(6.0)
        try:
            js = """
            (() => {
              const q = (s) => document.querySelector(s);
              return JSON.stringify({
                view: q('.console-root') ? 'console' : 'login',
                hero: Array.from(document.querySelectorAll('.stat-hero-metrics .stat-metric')).map((m) => ({
                  label: (m.querySelector('.stat-metric-label')||{}).textContent?.trim(),
                  value: (m.querySelector('.stat-metric-value')||{}).textContent?.trim(),
                })),
                cards: Array.from(document.querySelectorAll('.node-card')).map((c) => ({
                  server: (c.querySelector('.meta-text')||{}).textContent?.trim(),
                  id: (c.querySelector('.mt5-id-inline')||{}).textContent?.trim(),
                  trash: !!c.querySelector('.row-actions .btn-icon.danger'),
                })),
                hasAllDaemon: document.body.innerText.includes('全部守护'),
                hasCloseDaemon: document.body.innerText.includes('关闭守护'),
              });
            })()
            """
            results["dom"] = json.loads(window.evaluate_js(js))
        except Exception as exc:  # noqa: BLE001
            results["dom_error"] = repr(exc)

        try:
            from PIL import ImageGrab

            x, y, w, h = window.x, window.y, window.width, window.height
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
            results["screenshot"] = str(out)
        except Exception as exc:  # noqa: BLE001
            results["screenshot_error"] = repr(exc)

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    print("[shot] " + json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
