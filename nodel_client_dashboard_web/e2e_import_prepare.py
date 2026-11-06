"""端到端验证：导入节点「账号留空」的完整准备流程。

走真实链路：HTTP /api/import/prepare -> resolve_account -> 读终端登录号。
终端用 D:\\icmarkets（已登录的 ICMarkets 演示账户）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import install_fake_session  # noqa: E402

TERMINAL_DIR = r"D:\icmarkets"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    bridge = DashboardBridge()
    install_fake_session(bridge)
    bridge.start()
    print(f"[import-prep] bridge on 127.0.0.1:{bridge.port}", flush=True)

    def post(path: str, payload: dict):
        url = f"http://127.0.0.1:{bridge.port}{path}?token={bridge.token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    results: dict[str, object] = {}
    try:
        # 目录校验
        url = f"http://127.0.0.1:{bridge.port}/api/import/check-dir?path={TERMINAL_DIR}&token={bridge.token}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            results["check_dir"] = json.loads(resp.read().decode("utf-8"))

        # 准备（账号留空 -> 应自动读到终端登录号）
        status, payload = post(
            "/api/import/prepare",
            {
                "directory": TERMINAL_DIR,
                "name": "",
                "version": "1.0.0",
                "account": "",
                "write_env": True,
            },
        )
        results["prepare_status"] = status
        results["prepare"] = payload
    finally:
        bridge.stop()

    print("[import-prep] " + json.dumps(results, ensure_ascii=False, indent=2))

    prepare = results.get("prepare") or {}
    problems = []
    if not (results.get("check_dir") or {}).get("ok"):
        problems.append("目录校验没通过")
    if prepare.get("error"):
        problems.append(f"prepare 失败：{prepare.get('error')}")
    if not prepare.get("login"):
        problems.append("没有自动读到终端登录号")

    if problems:
        for p in problems:
            print(f"[import-prep] FAIL: {p}")
        print("[import-prep] 结论：失败")
        return 1
    print(
        f"[import-prep] 结论：通过"
        f"（自动读到 login={prepare.get('login')} server={prepare.get('server')}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
