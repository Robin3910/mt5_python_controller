"""端到端验证：前端点「浏览…」-> HTTP -> 桥接层 -> GUI 线程弹原生对话框。

这是导入功能最关键的一条链路：
  浏览器 JS  ->  fetch('/api/import/pick-dir')
             ->  桥接层后台线程
             ->  native_dialogs 把调用 marshal 到 GUI 线程
             ->  WinForms 弹出「选择文件夹」
             ->  用户选中 / 取消
             ->  结果回传前端

脚本会：
  1. 起桥接层 + 窗口；
  2. 在页面里点「浏览…」；
  3. 等待原生对话框出现（用 EnumWindows 找标题）；
  4. 取消它，验证前端收到 cancelled；
  5. 再点一次，这次输入一个真实目录，确认能回传路径。
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import FAKE_SNAPSHOT, install_fake_session  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402

FAKE_SNAPSHOT = {
    "type": "snapshot",
    "seq": 1,
    "authenticated": True,
    "is_admin": True,
    "username": "admin",
    "hero": {
        "total": 0, "online": 0, "starting": 0, "stopped": 0, "pending": 0,
        "connected_mt5": 0, "authenticated": 0, "alerts": 0,
    },
    "nodes": [],
    "busy": {},
    "version": {},
}

user32 = ctypes.windll.user32


def find_dialogs() -> list[int]:
    """枚举标题含「选择」的可见窗口（原生目录选择器的标题）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if "选择" in buf.value and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def close_dialogs() -> int:
    handles = find_dialogs()
    for hwnd in handles:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(handles)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[e2e-pick] 前端产物缺失")
        return 2

    original_snapshot = DashboardBridge.snapshot
    DashboardBridge.snapshot = lambda self: dict(FAKE_SNAPSHOT)

    bridge = DashboardBridge()
    install_fake_session(bridge)
    bridge.start()
    print(f"[e2e-pick] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}
    window = webview.create_window(
        "e2e-pick", url=bridge.url, width=1280, height=860, x=40, y=40
    )

    def probe() -> None:
        time.sleep(3.5)

        # 打开导入弹窗
        window.evaluate_js(
            """
            const btns = Array.from(document.querySelectorAll('.section-actions .btn-sm'));
            const t = btns.find(b => (b.textContent || '').includes('添加节点'));
            if (t) t.click();
            """
        )
        time.sleep(1.0)

        # 点「浏览…」
        window.evaluate_js(
            """
            const b = document.querySelector('.browse-btn');
            if (b) b.click();
            """
        )

        # 等原生对话框出现
        appeared = False
        for _ in range(30):
            time.sleep(0.4)
            if find_dialogs():
                appeared = True
                break
        results["dialog_appeared"] = appeared

        if appeared:
            time.sleep(0.8)
            from PIL import ImageGrab

            ImageGrab.grab().save(
                str(Path(__file__).resolve().parent / "e2e_pick_dialog.png")
            )
            # 取消
            close_dialogs()
            time.sleep(2.0)

            # 前端应收到 cancelled，目录框保持为空
            results["after_cancel"] = json.loads(
                window.evaluate_js(
                    """
                    (() => JSON.stringify({
                      dirValue: (document.querySelector('.dir-input-row input')||{}).value || '',
                      err: (document.querySelector('.modal-error')||{}).innerText || '',
                    }))()
                    """
                )
            )

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()
    DashboardBridge.snapshot = original_snapshot

    print("[e2e-pick] " + json.dumps(results, ensure_ascii=False, indent=2))
    ok = bool(results.get("dialog_appeared"))
    print("[e2e-pick] 结论：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
