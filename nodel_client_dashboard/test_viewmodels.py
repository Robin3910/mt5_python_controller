"""viewmodels 快照可比较、差分稳定。"""
from __future__ import annotations

from models import InstanceConfig, RuntimeState
from process_manager import ProcessManager
from viewmodels import build_detail, build_list, list_order


def test_list_and_detail_snapshots_stable(tmp_path):
    mgr = ProcessManager()
    cfg = InstanceConfig.create(
        name="n1", exe_path=str(tmp_path / "a.exe"), cwd=str(tmp_path), status_port=18765
    )
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(
        pid=1,
        process_alive=True,
        health="ok",
        ws_state="authenticated",
        version="1.1.0",
        uptime_s=10.0,
    )
    a = build_list(mgr, cfg.id)
    b = build_list(mgr, cfg.id)
    assert a == b
    assert list_order(a) == (cfg.id,)
    assert a[0].selected is True
    assert "运行中" in a[0].status_line

    d1 = build_detail(mp)
    d2 = build_detail(mp)
    assert d1 == d2
    assert d1.empty is False
    assert any(c.key == "online" and "在线" in c.text for c in d1.chips)

    mp.runtime.uptime_s = 11.0
    d3 = build_detail(mp)
    assert d3.summary != d1.summary
    assert d3.chips == d1.chips  # 芯片不因运行时长抖动


def test_detail_empty():
    d = build_detail(None)
    assert d.empty is True
    assert d.chips[0].key == "empty"
