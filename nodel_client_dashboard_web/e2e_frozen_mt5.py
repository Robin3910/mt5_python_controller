"""端到端验证：冻结产物里能不能读取 MT5 终端账号。

这是导入节点「账号留空 -> 读终端当前登录号」这条路径的最后一环：
前面全对（服务层解析、spec 打包），但冻结产物里 `import MetaTrader5`
失败的话，功能依然不可用，而界面只会提示
「本机没有 MetaTrader5 接口，请手工填写账号」。

所以要真的跑一次冻结 exe，看它能不能读到终端登录号。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    exes = sorted(DIST.glob("*.exe"))
    if not exes:
        print("[frozen-mt5] dist 里没有 exe")
        return 2
    exe = exes[0]
    print(f"[frozen-mt5] 用 {exe.name} 验证", flush=True)

    proc = subprocess.run(
        [str(exe), "--mt5-probe"], capture_output=True, text=True, timeout=120
    )
    stdout = proc.stdout.strip()
    if stdout:
        print(f"[frozen-mt5] stdout: {stdout}", flush=True)
    if proc.stderr.strip():
        print(f"[frozen-mt5] stderr: {proc.stderr.strip()}", flush=True)

    data: dict = {}
    try:
        data = json.loads(stdout[stdout.index("{") :])
    except Exception:  # noqa: BLE001
        pass

    if not data.get("import_ok"):
        print("[frozen-mt5] 结论：失败（冻结产物里 import MetaTrader5 失败）")
        return 1
    if not data.get("login"):
        print(f"[frozen-mt5] 结论：失败（能 import，但读不到登录号：{data}）")
        return 1

    print(
        f"[frozen-mt5] 结论：通过"
        f"（login={data['login']} server={data.get('server')}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
