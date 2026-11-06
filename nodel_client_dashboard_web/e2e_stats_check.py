"""临时数据目录：把正在交易、且有毫秒级时延样本的节点注册进面板，
验证「今日信号 / 平均延迟」这条链路（DB → API → 桥接 → ViewModel → Vue → DOM）
能一路走通。

只在临时目录里跑，不碰用户真实的 instances.json，也不启动/停止任何进程。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\work_space\mt5_python_controller\nodel_client_dashboard_web")
DIST = ROOT / "dist"
TMP = Path(r"D:\work_space\mt5_python_controller\nodel_client_dashboard_web\_e2e_data")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shutil.copy(DIST / ".env", TMP / ".env")

    instances = {
        "instances": [
            {
                "id": "e2eexness01",
                "name": "exness",
                "exe_path": r"E:\mt5\node_client.exe",
                "cwd": r"E:\mt5",
                "status_port": 18999,
                "daemon": False,
                "restart_delay_s": 5.0,
                "enabled": True,
                "node_id": "nd_195a19f78c889df8",
                "mt5_login": 278025176,
                "owner_user_id": 2,
                "requested_by_user_id": None,
                "approval_status": "approved",
                "credential_generation": 1,
                # has_credential=True → sync_credential 不会去开新凭证、改 .env
                "legacy_allowed": True,
                "has_credential": True,
                "backend_base": "http://localhost:8080",
                "algo_trading_disabled": False,
            }
        ]
    }
    (TMP / "instances.json").write_text(
        json.dumps(instances, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[e2e] 临时数据目录：{TMP}")

    import os

    os.environ["DASHBOARD_DATA_DIR"] = str(TMP)
    sys.path.insert(0, str(ROOT))
    sys.argv = ["e2e"]
    from shot_console import main as shot_main

    return shot_main()


if __name__ == "__main__":
    sys.exit(main())
