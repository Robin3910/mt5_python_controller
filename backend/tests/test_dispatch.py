"""Integration tests for the dispatcher (sync + poll) using fakeredis + stub manager."""
import fakeredis
import pytest

from app import persist, results
from app.connections import manager
from app.dispatcher import Dispatcher
from app.parser import TradingSignal
from app.poll_queue import PollWorker
from app.redis_store import RedisStore


@pytest.fixture
def store():
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return RedisStore(client)


@pytest.fixture(autouse=True)
def patch_side_effects(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(persist, "record_signal", noop)
    monkeypatch.setattr(persist, "record_dispatch", noop)
    monkeypatch.setattr(persist, "update_dispatch_result", noop)
    monkeypatch.setattr(persist, "audit", noop)
    monkeypatch.setattr(manager, "broadcast_admin", noop)
    manager.nodes.clear()
    manager.admins.clear()
    yield
    manager.nodes.clear()
    manager.admins.clear()


def node_symbol_filters(symbol="EURUSD", **extra):
    rule = {"lot_mode": "signal", "follow_sync": True, "follow_poll": True}
    rule.update(extra)
    return {symbol: rule}


def mk_node(node_id, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": True,
        "lot_mode": "signal",
        "lot": None,
        "poll_order": 0,
        "filters": node_symbol_filters(),
        "created_at": 0,
    }
    d.update(kw)
    return d


async def set_symbol_filters(store, symbol, *, mode="sync", scope="symbol"):
    await store.set_filters(
        {
            symbol: {
                "enabled": True,
                "allow_buy": True,
                "allow_sell": True,
                "dispatch_mode": mode,
                "position_scope": scope,
                "default_action": "pass",
                "intervals": [],
            }
        }
    )


async def _online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        await store.save_account(n["node_id"], {"positions": [], "prices": {}})
        manager.nodes[n["node_id"]] = object()


async def test_sync_broadcasts_to_all_online(store, monkeypatch):
    await _online(store, mk_node("nd_a"), mk_node("nd_b"))
    await set_symbol_filters(store, "EURUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    res = await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig1")

    assert res["mode"] == "sync"
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}
    assert all(s[1]["cmd"] == "open" and s[1]["volume"] == 0.1 for s in sent)


async def test_node_symbol_follow_sync_excludes_node(store, monkeypatch):
    await _online(
        store,
        mk_node("nd_a"),
        mk_node("nd_b", filters={"EURUSD": {"follow_sync": False}}),
    )
    await set_symbol_filters(store, "EURUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig1b")
    assert {s[0] for s in sent} == {"nd_a"}


async def test_position_gate_skips_node_with_position(store, monkeypatch):
    await _online(store, mk_node("nd_a"))
    await store.save_account("nd_a", {"positions": [{"symbol": "EURUSD"}], "prices": {}})
    await set_symbol_filters(store, "EURUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig2")
    assert sent == []  # gated, nothing sent


async def test_global_lot_overrides_volume(store, monkeypatch):
    await _online(
        store,
        mk_node("nd_a", filters={"EURUSD": {"lot_mode": "global"}}),
    )
    await store.set_filters(
        {
            "EURUSD": {
                "enabled": True,
                "allow_buy": True,
                "allow_sell": True,
                "dispatch_mode": "sync",
                "position_scope": "symbol",
                "default_action": "pass",
                "lot_enabled": True,
                "lot": 0.25,
                "intervals": [],
            }
        }
    )
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig3")
    assert len(sent) == 1
    assert sent[0][1]["volume"] == 0.25


async def test_poll_sequential_consumes_all_nodes(store, monkeypatch):
    await _online(store, mk_node("nd_a"), mk_node("nd_b"))
    await set_symbol_filters(store, "EURUSD", mode="poll")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        if msg.get("cmd") == "open":
            # simulate node ACK so the sequential waiter proceeds
            results.resolve(msg["signal_id"], node_id, {"success": True, "symbol": msg["symbol"]})
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    res = await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sigp")
    assert res["mode"] == "poll" and res["targets"] == 2

    worker = PollWorker(store, d)
    await worker.process("sigp")

    progress = await store.get_poll_progress("sigp")
    assert progress["cursor"] == 2
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}


async def test_close_signal_sends_close_to_online_nodes(store, monkeypatch):
    await set_symbol_filters(store, "EURUSD", mode="sync")
    await _online(
        store,
        mk_node("nd_a"),
        mk_node("nd_b", filters={"EURUSD": {"follow_sync": False}}),
    )
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    res = await d.dispatch(TradingSignal(action="CLOSE", symbol="EURUSD", volume=0.1), "sigc")
    assert res["mode"] == "close"
    assert all(s[1]["cmd"] == "close" for s in sent)
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}


async def test_node_without_symbol_config_skipped(store, monkeypatch):
    dispatches = []

    async def capture_dispatch(*args, **kwargs):
        dispatches.append(args)

    monkeypatch.setattr(persist, "record_dispatch", capture_dispatch)

    await _online(store, mk_node("nd_a", filters=None))
    await set_symbol_filters(store, "EURUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    res = await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig_no_cfg")

    assert res["mode"] == "sync"
    assert res["targets"] == 0
    assert sent == []
    assert len(dispatches) == 1
    assert dispatches[0][1] == "nd_a"
    assert dispatches[0][3] == "skipped"
    assert "节点未配置" in (dispatches[0][4] or "")
    assert "拒收" in (dispatches[0][4] or "")


async def test_unconfigured_symbol_rejected(store):
    d = Dispatcher(store)
    res = await d.dispatch(TradingSignal(action="BUY", symbol="GBPUSD", volume=0.1), "sigx")
    assert res["mode"] == "rejected"
    assert "未配置" in (res.get("reason") or "")
