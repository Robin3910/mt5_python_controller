"""起一个带假实例的桥接服务，供浏览器视觉验收。

用法：
    python serve_fake_console.py            # 起服务，打印 URL
    python serve_fake_console.py --port 8899

节点列表与运行时态都是假的，但 /api/detail 走的是真链路
（真 viewmodels、真 summary），所以视觉验收反映的是真实渲染结果。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import install_fake_session  # noqa: E402
from models import InstanceConfig  # noqa: E402
from process_manager import ManagedProcess  # noqa: E402


def make_instances() -> list[InstanceConfig]:
    specs = [
        ("mt5-prod-01", r"C:\Mt5\prod-01", True, "ok", 50231488, "nd_9f21", 93600),
        ("mt5-prod-02", r"C:\Mt5\prod-02", True, "ok", 50231501, "nd_4c07", 93600),
        ("mt5-stage-01", r"C:\Mt5\stage", False, "starting", 0, "", None),
        ("mt5-legacy-07", r"C:\Mt5\legacy-07", False, "unknown", 50118877, "nd_0a55", None),
    ]
    out = []
    for name, cwd, alive, health, login, node_id, uptime in specs:
        cfg = InstanceConfig.create(
            name=name, exe_path=str(Path(cwd) / "node_client.exe"), cwd=cwd
        )
        cfg.status_port = 51830 + len(out)
        cfg.node_id = node_id
        cfg.mt5_login = login
        cfg.approval_status = "approved"
        cfg.enabled = True
        cfg.daemon = alive
        out.append(cfg)
    return out


def main() -> int:
    port = 0
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    bridge = DashboardBridge()
    session = install_fake_session(bridge)
    bridge.manager.session = session
    bridge._load_auto_login = lambda: None
    # 还原真快照：节点列表由 manager 生成
    bridge._restore_snapshot()

    # 假的后端统计：让视觉验收能看到服务器名、今日信号与平均延迟
    def _fake_stats(token: str) -> dict[str, dict]:
        return {
            "nd_9f21": {"mt5_server": "ICMarkets-Live", "today_signals": 128, "avg_latency_ms": 96.4},
            "nd_4c07": {"mt5_server": "ICMarkets-Demo", "today_signals": 37, "avg_latency_ms": 143.2},
            "nd_0a55": {"mt5_server": "", "today_signals": 0, "avg_latency_ms": None},
        }

    session.api.stats = _fake_stats

    cfgs = make_instances()
    for cfg in cfgs:
        mp = bridge.manager.add(cfg)
        mp.runtime.process_alive = cfg.daemon
        mp.runtime.health = "ok" if cfg.daemon else ("starting" if "stage" in cfg.name else "unknown")
        mp.runtime.ws_state = "connected" if cfg.daemon else "closed"
        mp.runtime.version = "1.0.0" if cfg.daemon else ""
        mp.runtime.node_id = cfg.node_id or None
        mp.runtime.mt5_login = cfg.mt5_login or None
        mp.runtime.pid = 12345 if cfg.daemon else None
        mp.runtime.uptime_s = cfg.daemon and 93600 or None
        mp.runtime.algo_trading_allowed = True

        # 假的状态口：真实例靠它确认身份（assert_identity），没有它「停止/移除」
        # 会因「无法确认当前身份」被拒。视觉验收要能走通完整链路，所以补上。
        def _fake_status(timeout: float = 0.6, _cfg=cfg, _mp=mp) -> dict:
            if not _mp.runtime.process_alive:
                return None
            return {
                "ok": True,
                "process": "alive",
                "pid": _mp.runtime.pid,
                "node_id": _cfg.node_id or "",
                "mt5_login": _cfg.mt5_login or 0,
                "ws_state": _mp.runtime.ws_state,
                "uptime_s": _mp.runtime.uptime_s,
                "version": _mp.runtime.version,
                "runners": _mp.runtime.runners,
            }

        mp._fetch_status = _fake_status  # type: ignore[method-assign]

        # 策略任务：详情区以前会显示这段
        mp.runtime.runners = [
            {
                "task_id": "t-001", "magic": 20261103, "symbol": "EURUSD",
                "mode": "signal", "direction": "both", "stopping": False,
                "done": False, "template_id": "tmpl-9",
            }
        ] if cfg.daemon else []

    if port:
        bridge.port = port
    bridge.start()
    print(f"[serve] {bridge.url}", flush=True)
    print(f"[serve] token={bridge.token}", flush=True)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
