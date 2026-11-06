"""诊断：原生目录选择器到底卡在哪。

开启 pywebview 的日志，看 `create_file_dialog` 内部是否抛异常被吞掉。
分三种调用方式各试一次：
  1. 后台线程直接调用
  2. native.Invoke（同步 marshal 到 GUI 线程）
  3. native.BeginInvoke（异步 marshal，不阻塞调用方）

每种都带超时，超时则记为 hang。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(name)s: %(message)s")
for noisy in ("urllib3", "httpcore", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
MODE = sys.argv[1] if len(sys.argv) > 1 else "invoke"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview

    results: dict[str, object] = {}
    window = webview.create_window(
        f"probe-{MODE}",
        url="data:text/html,<html><body>probe</body></html>",
        width=600,
        height=400,
    )

    def probe() -> None:
        time.sleep(2.0)
        print(f"[probe] mode={MODE} start", flush=True)
        try:
            import clr

            clr.AddReference("System.Windows.Forms")
            from System import Action

            native = window.native
            picked: list = []

            def do_pick() -> None:
                print("[probe] inside GUI thread, calling dialog", flush=True)
                picked.append(window.create_file_dialog(webview.FOLDER_DIALOG))
                print("[probe] dialog returned", flush=True)

            if MODE == "direct":
                do_pick()
            elif MODE == "invoke":
                native.Invoke(Action(do_pick))
            elif MODE == "begininvoke":
                native.BeginInvoke(Action(do_pick))
                time.sleep(20.0)
            results["picked"] = repr(picked)
            results["error"] = None
        except Exception as exc:  # noqa: BLE001
            results["error"] = repr(exc)
            print(f"[probe] EXC {exc!r}", flush=True)
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    print(f"[probe] mode={MODE} results={results}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
