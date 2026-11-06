"""视觉验收：确认 WebView 主控制台不再展示日志与状态地址。

用假快照把面板置于「已登录」状态，点开一个节点卡片，
然后断言 DOM 里：
  - 没有 `.console-log`（运行日志）
  - 没有第二个 `.console-summary`（策略任务日志）
  - 详情摘要里不含「状态地址」

最后截图，供人工过目。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_bridge as bridge_mod  # noqa: E402
from dashboard_bridge import DashboardBridge  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402
from shot_console import FAKE_SNAPSHOT  # noqa: E402

OUT = Path(__file__).resolve().parent / "e2e_console_detail.png"


def main() -> int:
    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[shot-detail] 前端产物缺失，请先 npm run build")
        return 2

    original_snapshot = DashboardBridge.snapshot

    bridge = DashboardBridge()
    # 用假会话顶掉后端认证（面板要显示已登录态），
    # 但快照不造假：节点列表直接由 manager 生成，
    # 这样卡片 id 与 /api/detail 的 id 一致，点得开。
    from e2e_support import install_fake_session

    session = install_fake_session(bridge)
    bridge.manager.session = session
    bridge._load_auto_login = lambda: None
    # install_fake_session 会把 snapshot 换成固定的假快照（节点 id 是 n1/n2…），
    # 那样卡片 id 与 /api/detail 的 id 对不上，详情区点不开。
    # 这里还原成真快照：节点列表由 manager 生成，id 自然一致。
    bridge._restore_snapshot()

    # 详情面板要真数据：往 manager 里塞一个假实例，
    # 否则 /api/detail 拿到的是 empty，页面不渲染详情区。
    from models import InstanceConfig
    from process_manager import ManagedProcess

    cfg = InstanceConfig.create(
        name="mt5-prod-01",
        exe_path=r"C:\Mt5\prod-01\node_client.exe",
        cwd=r"C:\Mt5\prod-01",
    )
    cfg.status_port = 51830
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    cfg.approval_status = "approved"
    cfg.enabled = True
    mp = ManagedProcess(cfg)
    mp.runtime.process_alive = True
    mp.runtime.health = "ok"
    mp.runtime.ws_state = "connected"
    mp.runtime.version = "1.0.0"
    mp.runtime.node_id = "nd_9f21"
    mp.runtime.mt5_login = 50231488
    mp.runtime.pid = 12345
    mp.runtime.uptime_s = 93600
    mp.runtime.last_error = ""
    # 策略任务：详情区以前会显示这段，现在应该不再出现
    mp.runtime.runners = [
        {
            "task_id": "t-001", "magic": 20261103, "symbol": "EURUSD",
            "mode": "signal", "direction": "both", "stopping": False,
            "done": False, "template_id": "tmpl-9",
        }
    ]
    bridge.manager._items[cfg.id] = mp

    bridge.start()
    print(f"[shot-detail] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}

    window = webview.create_window(
        "shot · 节点详情",
        url=bridge.url,
        width=1440,
        height=900,
        x=40,
        y=40,
    )

    def probe() -> None:
        time.sleep(3.5)
        # 点第一个节点卡片，触发详情面板
        try:
            window.evaluate_js(
                """
                const card = document.querySelector('.node-card');
                if (card) card.click();
                """
            )
            time.sleep(1.2)
            # 把详情面板滚进可视区，截图才看得到内容
            try:
                window.evaluate_js(
                    """
                    const d = document.querySelector('.console-detail');
                    if (d) d.scrollIntoView({block: 'end'});
                    """
                )
                time.sleep(0.8)
            except Exception:  # noqa: BLE001
                pass
            results["dom"] = json.loads(
                window.evaluate_js(
                    """
                    (() => {
                      const summaries = Array.from(document.querySelectorAll('.console-summary'));
                      const detail = document.querySelector('.console-detail');
                      return JSON.stringify({
                        detailVisible: !!detail,
                        summaryCount: summaries.length,
                        summaryText: summaries.map(s => s.innerText || '').join('\\n'),
                        logBlocks: document.querySelectorAll('.console-log').length,
                        hasStatusAddr: (document.body.innerText || '').includes('状态地址'),
                        bodyHasLogWord: (document.body.innerText || '').includes('策略任务'),
                        summaryRect: (() => {
                          const r = summaries[0] && summaries[0].getBoundingClientRect();
                          return r ? {x: r.x, y: r.y, w: r.width, h: r.height} : null;
                        })(),
                        detailRect: (() => {
                          const r = detail && detail.getBoundingClientRect();
                          return r ? {x: r.x, y: r.y, w: r.width, h: r.height} : null;
                        })(),
                      });
                    })()
                    """
                )
            )
        except Exception as exc:  # noqa: BLE001
            results["dom_error"] = repr(exc)

        try:
            import ctypes
            import time as _time

            from PIL import Image

            # 用 PrintWindow 直接从窗口句柄取画面：不受遮挡影响，
            # ImageGrab 只能截屏幕上可见的区域，窗口被终端挡住就是一片灰。
            # pywebview 的 Handle 是 .NET 的 IntPtr，用 ToInt64() 取数值。
            raw_handle = window.native.Handle
            if hasattr(raw_handle, "ToInt64"):
                handle = int(raw_handle.ToInt64())
            else:
                handle = int(getattr(raw_handle, "value", raw_handle))
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            import ctypes.wintypes as wt

            rect = wt.RECT()
            user32.GetWindowRect(handle, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top

            hdc = user32.GetWindowDC(handle)
            mem_dc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            gdi32.SelectObject(mem_dc, bmp)
            # 2 = PW_RENDERFULLCONTENT，能抓到 WebView2 这类合成窗口的内容
            user32.PrintWindow(handle, mem_dc, 2)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = w
            header.biHeight = -h  # 负数 = 自上而下
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0

            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(header), 0)

            image = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
            image.save(OUT)
            results["screenshot"] = str(OUT)
            results["screenshot_size"] = [w, h]

            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(handle, hdc)
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
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    print("[shot-detail] " + json.dumps(results, ensure_ascii=False, indent=2))

    dom = results.get("dom") or {}
    problems: list[str] = []
    if not dom.get("detailVisible"):
        problems.append("详情面板没出现")
    if dom.get("summaryCount") != 1:
        problems.append(f"摘要块应只剩 1 个，实际 {dom.get('summaryCount')}")
    if dom.get("logBlocks"):
        problems.append(f"仍有 {dom.get('logBlocks')} 个运行日志块")
    if dom.get("hasStatusAddr"):
        problems.append("摘要里仍有「状态地址」")
    if dom.get("bodyHasLogWord"):
        problems.append("页面仍出现「策略任务」")

    if problems:
        for p in problems:
            print(f"[shot-detail] FAIL: {p}")
        print("[shot-detail] 结论：失败")
        return 1
    print("[shot-detail] 结论：通过（无运行日志、无策略任务日志、无状态地址）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
