"""node_log：按节点目录 + 按天轮转。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from models import InstanceConfig
from node_log import (
    DailyLogWriter,
    latest_log_path,
    log_path_for_day,
    node_logs_dir,
    sanitize_node_key,
)


def test_sanitize_node_key():
    cfg_id = "abcdefgh-1234"
    assert sanitize_node_key("node_client", cfg_id) == "node_client_abcdefgh"
    assert sanitize_node_key("a/b:c", cfg_id).startswith("a_b_c_")
    assert sanitize_node_key("", cfg_id).startswith("node_")


def test_daily_writer_path_and_rotate(tmp_path: Path, monkeypatch):
    import node_log as nl

    monkeypatch.setattr(nl, "logs_dir", lambda: tmp_path / "logs")
    cfg = InstanceConfig.create(name="alpha", exe_path="x.exe", cwd=str(tmp_path))
    folder = node_logs_dir(cfg)
    assert folder == tmp_path / "logs" / sanitize_node_key(cfg.name, cfg.id)

    w = DailyLogWriter(cfg)
    w.write_line("day1-line")
    p1 = log_path_for_day(cfg)
    assert p1.exists()
    assert p1.name == f"{date.today().isoformat()}.log"
    assert b"day1-line" in p1.read_bytes()

    # 模拟跨日：改 _day 并再写，应切到新文件
    tomorrow = date.today() + timedelta(days=1)
    with w._lock:
        w._day = date.today() - timedelta(days=1)  # 强制下次 _ensure 轮转
    # 直接指定 day 写路径校验辅助函数
    p_tmr = log_path_for_day(cfg, tomorrow)
    assert p_tmr != p1

    # 通过 monkeypatch date.today 触发真实轮转
    class _FakeDate(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(nl, "date", _FakeDate)
    w.write_line("day2-line")
    assert p_tmr.exists()
    assert b"day2-line" in p_tmr.read_bytes()
    w.close()

    latest = latest_log_path(cfg)
    assert latest is not None
    assert latest.name == f"{tomorrow.isoformat()}.log"


def test_process_log_under_node_dir(tmp_path: Path, monkeypatch):
    import sys
    import time

    import node_log as nl
    import process_manager as pm
    from process_manager import ProcessManager

    script = tmp_path / "fake_node.py"
    script.write_text(
        "import os, time\n"
        "print('fake start', os.environ.get('LOCAL_STATUS_PORT'), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setattr(nl, "logs_dir", lambda: tmp_path / "logs")

    mgr = ProcessManager()
    cfg = InstanceConfig.create(
        name="fake", exe_path=str(script), cwd=str(tmp_path), status_port=0
    )
    mp = mgr.add(cfg)
    mgr.start(cfg.id)
    deadline = time.time() + 3
    log = ""
    while time.time() < deadline:
        log = mp.read_log_tail()
        if "fake start" in log:
            break
        time.sleep(0.1)
    assert "fake start" in log
    day_file = log_path_for_day(cfg)
    assert day_file.is_file()
    assert day_file.parent == node_logs_dir(cfg)
    mgr.stop(cfg.id)
