"""端到端验证：原生目录选择器的「选中」路径。

e2e_pick_dir.py 验证了弹窗出现 + 取消；本脚本再验证**选中**：
在原生对话框里输入一个真实目录，点「选择文件夹」，确认前端收到该路径，
并且目录校验的提示也更新了。

用法：
    python e2e_pick_dir2.py [目标目录]
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
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5


def _enum_children(hwnd: int) -> list[tuple[int, str, str]]:
    """列出子控件：(句柄, 类名, 标题)。"""
    found: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(child, _lparam):
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, cls_buf, 256)
        length = user32.GetWindowTextLengthW(child)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(child, title_buf, length + 1)
        found.append((int(child), cls_buf.value, title_buf.value))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return found


def find_dialog() -> int | None:
    result: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if "选择" in buf.value and user32.IsWindowVisible(hwnd):
                result.append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return result[0] if result else None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[e2e-pick2] 前端产物缺失")
        return 2

    target_dir = (
        sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
    )
    print(f"[e2e-pick2] 目标目录: {target_dir}", flush=True)

    original_snapshot = DashboardBridge.snapshot
    DashboardBridge.snapshot = lambda self: dict(FAKE_SNAPSHOT)

    bridge = DashboardBridge()
    install_fake_session(bridge)
    bridge.start()
    print(f"[e2e-pick2] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}
    window = webview.create_window(
        "e2e-pick2", url=bridge.url, width=1280, height=860, x=40, y=40
    )

    def probe() -> None:
        time.sleep(3.5)
        window.evaluate_js(
            """
            const btns = Array.from(document.querySelectorAll('.section-actions .btn-sm'));
            const t = btns.find(b => (b.textContent || '').includes('添加节点'));
            if (t) t.click();
            """
        )
        time.sleep(1.0)
        window.evaluate_js(
            """
            const b = document.querySelector('.browse-btn');
            if (b) b.click();
            """
        )

        hwnd = None
        for _ in range(30):
            time.sleep(0.4)
            hwnd = find_dialog()
            if hwnd:
                break
        results["dialog_appeared"] = bool(hwnd)
        if not hwnd:
            return

        time.sleep(0.8)
        children = _enum_children(hwnd)

        edit = next((h for h, c, _t in children if c == "Edit"), None)
        if edit is None:
            results["error"] = "找不到文件名编辑框"
            return
        user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.c_wchar_p(target_dir))
        time.sleep(0.4)

        ok_btn = next(
            (h for h, c, t in children if c == "Button" and "选择文件夹" in t), None
        )
        if ok_btn is None:
            results["error"] = "找不到「选择文件夹」按钮"
            return
        user32.SendMessageW(ok_btn, BM_CLICK, 0, 0)

        for _ in range(25):
            time.sleep(0.4)
            value = window.evaluate_js(
                "(document.querySelector('.dir-input-row input')||{}).value || ''"
            )
            if value:
                break
        results["dir_value"] = window.evaluate_js(
            "(document.querySelector('.dir-input-row input')||{}).value || ''"
        )

        # 目录校验的提示（合法目录是 hint-line，非法是 modal-error）
        results["hint"] = window.evaluate_js(
            "(document.querySelector('.modal-body .hint-line')||{}).innerText || ''"
        )
        results["dir_error"] = window.evaluate_js(
            "(document.querySelector('.modal-body .modal-error')||{}).innerText || ''"
        )

        try:
            from PIL import ImageGrab

            ImageGrab.grab(bbox=(window.x, window.y,
                                 window.x + window.width,
                                 window.y + window.height)).save(
                str(Path(__file__).resolve().parent / "e2e_pick_dir2.png")
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()
    DashboardBridge.snapshot = original_snapshot

    print("[e2e-pick2] " + json.dumps(results, ensure_ascii=False, indent=2))
    got = str(results.get("dir_value") or "")
    ok = bool(results.get("dialog_appeared")) and got.strip().lower() == target_dir.lower()
    print("[e2e-pick2] 结论：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
