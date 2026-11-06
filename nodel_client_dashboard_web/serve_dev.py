"""开发态：只起桥接层（不起 WebView 窗口），供浏览器调试前端。

用法：
    python serve_dev.py            # 起服务并打印带 token 的 URL
    python serve_dev.py --port 5179
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        type=int,
        default=5179,
        help="监听端口；与 vite.config.ts 的 proxy 目标一致（5179）",
    )
    args = parser.parse_args()

    bridge = DashboardBridge()
    bridge.port = args.port
    bridge.start()

    print(f"[dev] bridge on 127.0.0.1:{bridge.port}", flush=True)
    print(f"[dev] 直接打开: {bridge.url}", flush=True)
    print(
        "[dev] 或用 vite dev server（token 走同一个 query）:\n"
        f"      http://127.0.0.1:5178/?token={bridge.token}",
        flush=True,
    )
    print("[dev] Ctrl+C to stop", flush=True)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
