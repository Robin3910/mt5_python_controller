"""给浏览器验收用：起一个带假后端 + 假实例的桥接服务。

与 `serve_fake_console.py` 的差别：这里的后端**真的**会返回版本清单与
安装包，所以「版本更新」弹窗的每个阶段（检测 / 确认 / 进度 / 结果）
都能在浏览器里走通。

用法：
    python serve_fake_versions.py --port 8899
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import install_fake_session  # noqa: E402
from models import InstanceConfig  # noqa: E402

VERSION = "2.0.0-20260102030405"
OLD_VERSION = "1.0.0-20260101000000"
WORK = Path(__file__).resolve().parent / "_e2e_serve_ver"


def _exe_bytes(tag: str) -> bytes:
    return f"fake-node-client-{tag}".encode()


def _manifest(exe: bytes) -> str:
    return json.dumps(
        {
            "account_binding": True,
            "executable_sha256": hashlib.sha256(exe).hexdigest(),
        },
        ensure_ascii=False,
    )


def build_package() -> tuple[bytes, str]:
    """打包 v2，返回 (包字节, sha256)。"""
    exe = _exe_bytes("v2")
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("node_client.exe", exe)
        zf.writestr("client_capabilities.json", _manifest(exe))
        zf.writestr("version.txt", f"{VERSION}\n")
        zf.writestr(".env.example", "MANAGER_WS_URL=\nNODE_TOKEN=\n")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def start_backend(payload: bytes, sha: str) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/client-versions/available"):
                self._json(200, {
                    "items": [
                        {"version": VERSION, "size": len(payload), "sha256": sha,
                         "notes": "e2e 假版本：修了几个信号问题", "is_current": True},
                        {"version": OLD_VERSION, "size": len(payload), "sha256": sha,
                         "notes": "e2e 旧版本", "is_current": False},
                    ],
                    "current": VERSION,
                })
                return
            if self.path.startswith("/api/client-versions/current"):
                self._json(200, {
                    "version": VERSION, "size": len(payload), "sha256": sha,
                    "notes": "e2e 假版本",
                })
                return
            if self.path.endswith("/download"):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json(404, {"detail": "not found"})

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    port = 0
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    payload, sha = build_package()
    server = start_backend(payload, sha)
    backend_base = f"http://127.0.0.1:{server.server_address[1]}"

    bridge = DashboardBridge()
    session = install_fake_session(bridge)
    bridge.manager.session = session
    bridge._load_auto_login = lambda: None
    bridge._restore_snapshot()
    session.api.base = backend_base

    def _fake_stats(token: str) -> dict[str, dict]:
        return {
            "nd_9f21": {"mt5_server": "ICMarkets-Live", "today_signals": 128, "avg_latency_ms": 96.4},
        }

    session.api.stats = _fake_stats

    # 两个实例：一个装旧版本（可更新），一个装新版本（已最新）
    specs = [
        ("mt5-prod-01", "1.0.0-20260101000000", True),
        ("mt5-prod-02", VERSION, True),
    ]
    for name, version, alive in specs:
        instance_dir = WORK / name
        instance_dir.mkdir(parents=True, exist_ok=True)
        tag = "v1" if version == OLD_VERSION else "v2"
        exe = _exe_bytes(tag)
        (instance_dir / "node_client.exe").write_bytes(exe)
        (instance_dir / "client_capabilities.json").write_text(_manifest(exe), encoding="utf-8")
        (instance_dir / "version.txt").write_text(f"{version}\n", encoding="utf-8")
        (instance_dir / ".env").write_text("MANAGER_WS_URL=ws://x/ws/node\nNODE_TOKEN=t\n", encoding="utf-8")

        cfg = InstanceConfig.create(
            name=name, exe_path=str(instance_dir / "node_client.exe"), cwd=str(instance_dir)
        )
        cfg.status_port = 51840 + len(bridge.manager.configs())
        cfg.node_id = f"nd_{name[-2:]}"
        cfg.mt5_login = 50231488
        cfg.approval_status = "approved"
        cfg.enabled = True
        mp = bridge.manager.add(cfg)
        mp.runtime.process_alive = alive
        mp.runtime.health = "ok"
        mp.runtime.ws_state = "authenticated"
        mp.runtime.version = version
        mp.runtime.node_id = cfg.node_id
        mp.runtime.mt5_login = cfg.mt5_login
        mp.runtime.algo_trading_allowed = True

        def _fake_status(timeout: float = 0.6, _cfg=cfg, _mp=mp) -> dict:
            if not _mp.runtime.process_alive:
                return None
            return {
                "ok": True, "process": "alive", "pid": 12345,
                "node_id": _cfg.node_id, "mt5_login": _cfg.mt5_login,
                "ws_state": _mp.runtime.ws_state, "uptime_s": 93600,
                "version": _mp.runtime.version, "runners": [],
            }

        mp._fetch_status = _fake_status  # type: ignore[method-assign]

    if port:
        bridge.port = port
    bridge.start()
    print(f"[serve] {bridge.url}", flush=True)
    print(f"[serve] token={bridge.token}", flush=True)
    print(f"[serve] 后端={backend_base}", flush=True)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        bridge.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
