"""在 WebView2 里渲染「导入节点」弹窗，用于视觉验收。

用假快照走 console 分支，再点「＋ 添加节点」打开导入弹窗。
不触发真正的导入（那会下载安装包）。
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

FAKE_SNAPSHOT = {
    "type": "snapshot",
    "seq": 1,
    "authenticated": True,
    "is_admin": True,
    "username": "admin",
    "hero": {
        "total": 2,
        "online": 1,
        "starting": 0,
        "stopped": 1,
        "pending": 1,
        "connected_mt5": 1,
        "authenticated": 1,
        "alerts": 0,
    },
    "nodes": [],
    "busy": {},
    "version": {},
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[shot-import] 前端产物缺失，请先 npm run build")
        return 2

    original_snapshot = DashboardBridge.snapshot
    DashboardBridge.snapshot = lambda self: dict(FAKE_SNAPSHOT)

    bridge = DashboardBridge()
    bridge.start()
    print(f"[shot-import] bridge on 127.0.0.1:{bridge.port}", flush=True)

    out = Path(__file__).resolve().parent / "e2e_import.png"
    results: dict[str, object] = {}

    window = webview.create_window(
        "shot-import",
        url=bridge.url,
        width=1280,
        height=860,
        x=40,
        y=40,
    )

    def probe() -> None:
        time.sleep(3.5)

        # 点「＋ 添加节点」
        try:
            window.evaluate_js(
                """
                const btns = Array.from(document.querySelectorAll('.section-actions .btn-sm'));
                const target = btns.find(b => (b.textContent || '').includes('添加节点'));
                if (target) target.click();
                """
            )
            time.sleep(1.5)
            results["modal"] = json.loads(
                window.evaluate_js(
                    """
                    (() => JSON.stringify({
                      overlay: !!document.querySelector('.modal-overlay'),
                      title: (document.querySelector('.modal-title')||{}).textContent || '',
                      subtitle: (document.querySelector('.modal-subtitle')||{}).textContent || '',
                      hasDirRow: !!document.querySelector('.dir-input-row'),
                      hasBrowse: !!document.querySelector('.browse-btn'),
                      hasVersionRow: !!document.querySelector('.version-row'),
                      hasSelect: !!document.querySelector('.version-row select'),
                      hasCheckList: !!document.querySelector('.check-list'),
                      hasAccount: !!document.querySelectorAll('.form-field input.input-field').length,
                      body: (document.querySelector('.modal-body')||{}).innerText?.slice(0,400) || '',
                    }))()
                    """
                )
            )
        except Exception as exc:  # noqa: BLE001
            results["modal_error"] = repr(exc)

        try:
            from PIL import ImageGrab
            x, y = window.x, window.y
            w, h = window.width, window.height
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
    DashboardBridge.snapshot = original_snapshot

    print("[shot-import] " + json.dumps(results, ensure_ascii=False, indent=2))
    modal = results.get("modal")
    ok = (
        isinstance(modal, dict)
        and modal.get("overlay")
        and modal.get("hasDirRow")
        and modal.get("hasBrowse")
        and modal.get("hasVersionRow")
        and modal.get("hasCheckList")
    )
    print("[shot-import] 结论：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
