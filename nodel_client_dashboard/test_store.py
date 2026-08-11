"""split tests: store + process_manager."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from models import InstanceConfig
from process_manager import ProcessManager
from store import load_instances, save_instances


def test_store_roundtrip(tmp_path: Path):
    path = tmp_path / "instances.json"
    items = [
        InstanceConfig.create(name="a", exe_path="C:/a.exe", cwd="C:/", status_port=18765),
        InstanceConfig.create(name="b", exe_path="C:/b.exe", cwd="C:/", daemon=True),
    ]
    save_instances(items, path)
    loaded = load_instances(path)
    assert len(loaded) == 2
    assert loaded[0].name == "a"
    assert loaded[0].status_port == 18765
    assert loaded[1].daemon is True


def test_default_label_and_shorten_path():
    from models import default_label_from_path, shorten_path

    assert default_label_from_path(r"C:\MT5\node_client.exe") == "node_client"
    assert default_label_from_path("") == "node_client"
    cfg = InstanceConfig.create(exe_path=r"D:\apps\node_client.exe")
    assert cfg.name == "node_client"
    assert cfg.cwd == r"D:\apps"
    short = shorten_path("C:/very/long/path/to/node_client.exe", max_len=20)
    assert "..." in short
    assert len(short) <= 20


def test_process_manager_start_stop(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_node.py"
    script.write_text(
        "import os, time\n"
        "print('fake start', os.environ.get('LOCAL_STATUS_PORT'), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHON", sys.executable)

    import node_log as nl

    monkeypatch.setattr(nl, "logs_dir", lambda: tmp_path / "logs")

    mgr = ProcessManager()
    cfg = InstanceConfig.create(
        name="fake", exe_path=str(script), cwd=str(tmp_path), status_port=0
    )
    mp = mgr.add(cfg)
    assert mp.cfg.status_port > 0
    mgr.start(cfg.id)
    deadline = time.time() + 3
    log = ""
    while time.time() < deadline:
        log = mp.read_log_tail()
        if "fake start" in log:
            break
        time.sleep(0.1)
    assert mp.runtime.process_alive is True
    assert mp.runtime.pid is not None
    assert "fake start" in log
    mgr.stop(cfg.id)
    time.sleep(0.8)
    assert mp.runtime.process_alive is False


def test_daemon_restarts(tmp_path: Path, monkeypatch):
    script = tmp_path / "short.py"
    script.write_text(
        "import time\nprint('once', flush=True)\ntime.sleep(0.3)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHON", sys.executable)
    import node_log as nl

    monkeypatch.setattr(nl, "logs_dir", lambda: tmp_path / "logs")

    mgr = ProcessManager()
    cfg = InstanceConfig.create(
        name="d", exe_path=str(script), cwd=str(tmp_path),
        daemon=True, restart_delay_s=0.5,
    )
    mp = mgr.add(cfg)
    first_pid = None
    mgr.start(cfg.id)
    deadline = time.time() + 2
    while time.time() < deadline:
        if mp.runtime.pid:
            first_pid = mp.runtime.pid
            break
        time.sleep(0.1)
    assert first_pid is not None
    deadline = time.time() + 6
    restarted = False
    while time.time() < deadline:
        if mp.runtime.pid and mp.runtime.pid != first_pid and mp.runtime.process_alive:
            restarted = True
            break
        time.sleep(0.2)
    mgr.stop(cfg.id)
    assert restarted


def test_start_all_stop_all_skip_logic(tmp_path: Path, monkeypatch):
    """无真实子进程：已标记存活的跳过启动；停止对未运行返回已停止。"""
    mgr = ProcessManager()
    cfg = InstanceConfig.create(
        name="x", exe_path=str(tmp_path / "missing.exe"), cwd=str(tmp_path)
    )
    mp = mgr.add(cfg)
    mp.runtime.process_alive = True
    started = mgr.start_all()
    assert started[0][2] is True
    assert "运行" in started[0][3]

    mp.runtime.process_alive = False
    mp._proc = None
    monkeypatch.setattr(mp, "_status_reachable", lambda timeout=0.3: False)
    stopped = mgr.stop_all()
    assert stopped[0][2] is True
    assert "停止" in stopped[0][3]


def test_set_daemon_all(tmp_path: Path):
    mgr = ProcessManager()
    a = mgr.add(
        InstanceConfig.create(name="a", exe_path=str(tmp_path / "a.exe"), cwd=str(tmp_path))
    )
    b = mgr.add(
        InstanceConfig.create(
            name="b",
            exe_path=str(tmp_path / "b.exe"),
            cwd=str(tmp_path),
            daemon=True,
        )
    )
    assert a.cfg.daemon is False
    assert b.cfg.daemon is True

    on = mgr.set_daemon_all(True)
    assert all(ok for *_r, ok, _m in on)
    assert a.cfg.daemon is True
    assert b.cfg.daemon is True
    assert "已是开启" in on[1][3]

    off = mgr.set_daemon_all(False)
    assert all(ok for *_r, ok, _m in off)
    assert a.cfg.daemon is False
    assert b.cfg.daemon is False
    assert "已关闭守护" in off[0][3]


def test_recover_attaches_via_status_port(tmp_path: Path, monkeypatch):
    """面板重启后：仅凭已保存 status_port 即可接管仍在跑的客户端。"""
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    payload = {
        "ok": True,
        "process": "alive",
        "pid": 4242,
        "ws_state": "authenticated",
        "node_id": 9,
        "mt5_login": 1001,
        "uptime_s": 12,
        "runners": [],
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    httpd = HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        mgr = ProcessManager()
        cfg = InstanceConfig.create(
            name="recovered",
            exe_path=str(tmp_path / "missing.exe"),
            cwd=str(tmp_path),
            status_port=port,
        )
        mgr.load([cfg])
        mp = mgr.get(cfg.id)
        assert mp is not None
        deadline = time.time() + 3
        while time.time() < deadline and not mp.runtime.process_alive:
            time.sleep(0.1)
        assert mp.runtime.process_alive is True
        assert mp.runtime.health == "ok"
        assert mp.runtime.pid == 4242
        assert mp.runtime.node_id == 9
        # 再次 start 不应因 missing.exe 报错（已接管）
        mgr.start(cfg.id)
        assert mp.runtime.process_alive is True
    finally:
        httpd.shutdown()
