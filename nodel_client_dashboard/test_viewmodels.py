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
    assert a[0].algo_trading_disabled is False

    cfg.algo_trading_disabled = True
    marked = build_list(mgr, cfg.id)
    assert marked[0].algo_trading_disabled is True
    assert marked != a

    mp.runtime.algo_trading_allowed = False
    cfg.algo_trading_disabled = False
    assert build_list(mgr, cfg.id)[0].algo_trading_disabled is True
    mp._apply_status_payload({"algo_trading_allowed": True})
    assert mp.runtime.algo_trading_allowed is True
    assert build_list(mgr, cfg.id)[0].algo_trading_disabled is False

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


def test_format_uptime_distinguishes_none_and_zero():
    from viewmodels import format_uptime
    assert format_uptime(None) == "--"
    assert format_uptime(0) == "0天 0小时"
    assert format_uptime(3600) == "0天 1小时"
    assert format_uptime(3600 * 24 * 3 + 3600 * 14) == "3天 14小时"


def test_hero_counts_each_state():
    from viewmodels import build_hero
    mgr = ProcessManager()
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    b = InstanceConfig.create(name="b", exe_path="/x/b.exe", cwd="/x")
    c = InstanceConfig.create(name="c", exe_path="/x/c.exe", cwd="/x")
    d = InstanceConfig.create(name="d", exe_path="/x/d.exe", cwd="/x")
    e = InstanceConfig.create(name="e", exe_path="/x/e.exe", cwd="/x")
    ma, mb, mc, md, me = (mgr.add(x) for x in (a, b, c, d, e))
    ma.runtime = RuntimeState(process_alive=True, health="ok", ws_state="authenticated")
    mb.runtime = RuntimeState(process_alive=True, health="ok", ws_state="connecting")
    mc.runtime = RuntimeState(process_alive=False, busy="starting", health="starting")
    md.runtime = RuntimeState(process_alive=False, health="dead")
    e.approval_status = "pending"
    hero = build_hero(mgr)
    assert hero.total == 5
    assert hero.online == 2
    assert hero.starting == 1
    assert hero.stopped == 1
    assert hero.pending == 1
    assert hero.connected_mt5 == 2
    assert hero.authenticated == 1
    assert hero.alerts == 0


def test_hero_states_are_mutually_exclusive():
    """四个状态互斥：在线+启动中+已停止+待审核 必须等于总数。

    否则分布条的比例和会超过 1，条形图画错。
    """
    from viewmodels import build_hero
    mgr = ProcessManager()
    # 同一实例同时满足「在线」和「待审核」两个条件
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    a.approval_status = "pending"
    mp = mgr.add(a)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    # 同时满足「已停止」和「待审核」
    b = InstanceConfig.create(name="b", exe_path="/x/b.exe", cwd="/x")
    b.approval_status = "pending"
    mb = mgr.add(b)
    mb.runtime = RuntimeState(process_alive=False, health="dead")
    hero = build_hero(mgr)
    assert hero.online + hero.starting + hero.stopped + hero.pending == hero.total
    assert hero.online == 1
    assert hero.pending == 1


def test_hero_alerts_count_last_error():
    from viewmodels import build_hero
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(process_alive=True, health="error", last_error="连接断开")
    assert build_hero(mgr).alerts == 1


def test_hero_zero_instances():
    from viewmodels import build_hero
    hero = build_hero(ProcessManager())
    assert hero.total == 0
    assert hero.online == 0


def test_card_fields_reflect_runtime():
    from viewmodels import build_list
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x", status_port=18765)
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(
        process_alive=True, health="ok", ws_state="authenticated",
        algo_trading_allowed=True, uptime_s=3600 * 25, node_id="TID-1", mt5_login=88001234,
    )
    item = build_list(mgr, None)[0]
    assert item.mt5_connected is True
    assert item.mt5_authenticated is True
    assert item.algo_trading_ok is True
    assert item.algo_trading_known is True
    assert item.uptime_text == "1天 1小时"
    assert item.node_id == "TID-1"
    assert item.server_label == "账号 88001234"
    assert item.badge_text == "运行中"
    assert item.badge_tone == "online"


def test_card_algo_unknown_is_not_ok():
    from viewmodels import build_list
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    item = build_list(mgr, None)[0]
    assert item.algo_trading_known is False
    assert item.algo_trading_ok is False


def test_card_badge_tone_matches_hero_counting():
    """卡片徽章的状态判定必须与 hero 分布条一致。

    一个实例同时「在线」且「待审核」时，卡片显示运行中，hero 也必须算进 online。
    """
    from viewmodels import build_hero, build_list
    mgr = ProcessManager()
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    a.approval_status = "pending"
    mp = mgr.add(a)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    item = build_list(mgr, None)[0]
    assert item.badge_tone == "online"
    assert item.badge_text == "运行中"
    hero = build_hero(mgr)
    assert hero.online == 1
    assert hero.pending == 0
