"""临时数据目录 + 假实例，用来在浏览器里复现「垃圾桶点了没反应」。

不碰用户真实的 dist/instances.json。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\work_space\mt5_python_controller\nodel_client_dashboard_web")
TMP = ROOT / "_e2e_rm"

FAKE = {
    "id": "e2eremove0001",
    "name": "e2e-remove",
    "exe_path": r"E:\mt5\node_client.exe",
    "cwd": r"E:\mt5",
    "status_port": 18991,
    "daemon": False,
    "restart_delay_s": 5.0,
    "enabled": True,
    "node_id": "nd_195a19f78c889df8",
    "mt5_login": 278025176,
    "owner_user_id": 2,
    "requested_by_user_id": None,
    "approval_status": "approved",
    "credential_generation": 1,
    "legacy_allowed": True,
    "has_credential": True,
    "backend_base": "http://localhost:8080",
    "algo_trading_disabled": False,
}


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shutil.copy(ROOT / "dist" / ".env", TMP / ".env")
    (TMP / "instances.json").write_text(
        json.dumps({"instances": [FAKE]}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.environ["DASHBOARD_DATA_DIR"] = str(TMP)
    sys.path.insert(0, str(ROOT))
    from dashboard_bridge import DashboardBridge

    bridge = DashboardBridge()
    bridge.start()
    print(f"URL {bridge.url}", flush=True)
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
