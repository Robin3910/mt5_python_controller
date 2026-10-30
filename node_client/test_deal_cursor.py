"""成交采集游标测试。

游标是「不重复、不丢」的保证：读失败时绝不推进，读成功后只推进到已确认的位置。
"""
import json
import time

from deal_cursor import DealCursor


def test_fresh_cursor_uses_lookback(tmp_path):
    """首次运行没有记录：按 lookback_days 回看初始化。

    回看是必须的：节点第一次上线时账户里已经有历史成交，
    采集应当从「最近 N 天」开始，而不是只从「此刻」开始。
    """
    before = time.time()
    c = DealCursor(12345, path=tmp_path / "c.json")
    c.load(lookback_days=7)

    assert c.since_ts > 0
    assert c.since_ts < before
    assert c.last_deal_id == 0


def test_advance_then_reload_persists(tmp_path):
    """推进后写盘；重新加载能读回同样的位置（重启不重复也不丢）。"""
    path = tmp_path / "c.json"
    c = DealCursor(12345, path=path)
    c.load()
    # 用比初始回看位置更晚的时间戳（游标只前进）
    ts = c.since_ts + 1000.0
    c.advance(since_ts=ts, last_deal_id=99)

    c2 = DealCursor(12345, path=path)
    c2.load()
    assert c2.since_ts == ts
    assert c2.last_deal_id == 99


def test_advance_is_monotonic(tmp_path):
    """游标只前进不后退：旧的重复上报不能让游标倒退。"""
    c = DealCursor(1, path=tmp_path / "c.json")
    c.load()
    c.advance(since_ts=c.since_ts + 2000.0, last_deal_id=200)
    ts, did = c.since_ts, c.last_deal_id
    c.advance(since_ts=ts - 1000.0, last_deal_id=100)

    assert c.since_ts == ts
    assert c.last_deal_id == 200


def test_multiple_logins_are_independent(tmp_path):
    """一台机器可能绑过多个账号：游标按登录号分键，不能互相覆盖。"""
    path = tmp_path / "c.json"
    a = DealCursor(111, path=path)
    a.load()
    a.advance(since_ts=a.since_ts + 100.0, last_deal_id=10)

    b = DealCursor(222, path=path)
    b.load()
    b.advance(since_ts=b.since_ts + 200.0, last_deal_id=20)

    a2 = DealCursor(111, path=path)
    a2.load()
    assert a2.last_deal_id == 10
    assert a2.since_ts == a.since_ts

    b2 = DealCursor(222, path=path)
    b2.load()
    assert b2.last_deal_id == 20


def test_corrupted_file_starts_fresh(tmp_path):
    """游标文件损坏时按首次运行处理，不能直接崩掉（会卡住整个采集）。"""
    path = tmp_path / "c.json"
    path.write_text("{ not json", encoding="utf-8")

    c = DealCursor(1, path=path)
    c.load(lookback_days=3)

    assert c.since_ts > 0
    assert c.last_deal_id == 0


def test_file_is_valid_json_after_save(tmp_path):
    """落盘是原子替换，文件内容始终是完整 JSON。"""
    path = tmp_path / "c.json"
    c = DealCursor(1, path=path)
    c.load()
    c.advance(since_ts=c.since_ts + 5.0, last_deal_id=6)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["1"]["last_deal_id"] == 6
