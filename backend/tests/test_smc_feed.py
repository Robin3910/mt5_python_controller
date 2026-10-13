"""SMC 价位推送：解析、按任务覆写、以及只匹配启用中的逆势档位。"""
import fakeredis
import pytest

from app import group_persist, smc_feed, smc_rules
from app.redis_store import RedisStore
from app.smc_rules import SmcPayload


class _Tasks:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    def add_task(self, fn, *args, **kwargs) -> None:
        self.jobs.append((fn, args, kwargs))


def _sub(task_id: int, node_id: str, *, symbol="XAUUSD", levels=None, status=1) -> dict:
    return {
        "task_id": task_id,
        "dispatch_id": task_id * 10 + 1,
        "node_id": node_id,
        "magic": 900000000 + task_id,
        "symbol": symbol,
        "strategy_snapshot": {
            "rules": [{
                "type": 1,
                "status": status,
                "batch_enabled": True,
                "batch_levels": levels or [{"calc_type": "fvg", "timeframe": "M5"}],
            }],
        },
    }


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))


def test_parse_drops_bad_rows_and_rejects_bad_envelope():
    payload = smc_rules.parse_smc_payload({
        "type": "smc-data",
        "symbol": "xauusdt",
        "tf": "m5",
        "data": [
            {"type": "fvg", "side": 1, "price": 4300},
            {"type": "nope", "side": 1, "price": 1},
            {"type": "ob", "side": 2, "price": 1},
            {"type": "pdz", "side": -1, "price": 0},
            {"type": "fibo", "side": -1, "price": 4310.5},
        ],
    })
    assert payload.symbol == "XAUUSDT"
    assert payload.tf == "M5"
    assert payload.dropped == 3
    assert payload.levels == [
        {"type": "fvg", "side": 1, "price": 4300.0},
        {"type": "fibo", "side": -1, "price": 4310.5},
    ]

    with pytest.raises(ValueError, match="tf"):
        smc_rules.parse_smc_payload({
            "type": "smc-data", "symbol": "XAUUSD", "tf": "M7", "data": [],
        })
    with pytest.raises(ValueError, match="symbol"):
        smc_rules.parse_smc_payload({"type": "smc-data", "tf": "M5", "data": []})
    with pytest.raises(ValueError, match="data"):
        smc_rules.parse_smc_payload({"type": "smc-data", "symbol": "XAUUSD", "tf": "M5"})

    assert smc_rules.is_smc_payload({"Type": "SMC-DATA"}) is True
    assert smc_rules.is_smc_payload({"action": "buy"}) is False
    assert smc_rules.is_smc_payload("buy XAUUSD") is False


def test_snapshot_match_requires_enabled_counter_level():
    snap = _sub(1, "nd")["strategy_snapshot"]
    assert smc_rules.snapshot_uses_smc_tf(snap, "M5") is True
    assert smc_rules.snapshot_uses_smc_tf(snap, "H1") is False
    off = _sub(1, "nd", status=0)["strategy_snapshot"]
    assert smc_rules.snapshot_uses_smc_tf(off, "M5") is False
    trend = {"rules": [{"type": 2, "status": 1, "batch_enabled": True,
                        "batch_levels": [{"calc_type": "fvg", "timeframe": "M5"}]}]}
    assert smc_rules.snapshot_uses_smc_tf(trend, "M5") is False


async def test_ingest_overwrites_one_copy_per_task(store, monkeypatch):
    subs = [
        _sub(7, "nd_a"),
        _sub(7, "nd_b"),
        _sub(8, "nd_a", symbol="EURUSD"),
        _sub(9, "nd_c", levels=[
            {"calc_type": "fvg", "timeframe": "M5"},
            {"calc_type": "ob", "timeframe": "H1"},
        ]),
    ]

    async def fake_subs():
        return subs

    monkeypatch.setattr(group_persist, "active_subtasks_with_snapshot", fake_subs)
    bg = _Tasks()
    first = SmcPayload(
        symbol="XAUUSDT", tf="M5",
        levels=[{"type": "fvg", "side": 1, "price": 4300.0}],
        received_at=10,
    )
    result = await smc_feed.ingest(store, first, bg)
    assert result["tasks"] == 2
    assert result["nodes"] == 3
    assert result["levels"] == 1
    saved = await store.get_task_smc(7)
    assert saved["tf"] == "M5" and saved["levels"][0]["price"] == 4300.0
    assert await store.get_task_smc(8) is None
    assert (await store.get_task_smc(9))["tf"] == "M5"
    assert len(bg.jobs) == 1

    # 同一任务同时用了两个周期时，后一次推送整份覆写，不按周期合并
    second = SmcPayload(
        symbol="XAUUSD", tf="H1",
        levels=[{"type": "ob", "side": -1, "price": 4400.0}],
        received_at=20,
    )
    await smc_feed.ingest(store, second, _Tasks())
    assert (await store.get_task_smc(7))["tf"] == "M5"
    replaced = await store.get_task_smc(9)
    assert replaced["tf"] == "H1"
    assert replaced["levels"][0]["type"] == "ob"

    empty = SmcPayload(symbol="XAUUSD", tf="M5", levels=[], received_at=30)
    cleared = await smc_feed.ingest(store, empty, _Tasks())
    assert cleared["levels"] == 0
    assert (await store.get_task_smc(7))["levels"] == []


async def test_ingest_group_ids_keep_other_groups(store, monkeypatch):
    """手动触发点名分组时，别的分组上的任务副本保持不动。"""
    subs = [
        {**_sub(7, "nd_a"), "group_id": "g_a"},
        {**_sub(8, "nd_b", symbol="XAUUSD"), "group_id": "g_b"},
    ]

    async def fake_subs():
        return subs

    monkeypatch.setattr(group_persist, "active_subtasks_with_snapshot", fake_subs)
    payload = SmcPayload(
        symbol="XAUUSD", tf="M5",
        levels=[{"type": "fvg", "side": 1, "price": 4300.0}],
        received_at=1,
    )
    result = await smc_feed.ingest(store, payload, _Tasks(), group_ids=["g_a"])
    assert result["tasks"] == 1
    assert (await store.get_task_smc(7))["levels"][0]["price"] == 4300.0
    assert await store.get_task_smc(8) is None
