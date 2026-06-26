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


def mk_node(node_id, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": True,
        "lot_mode": "signal",
        "lot": None,
        "follow_sync": True,
        "follow_poll": True,
        "poll_order": 0,
        "filters": None,
        "created_at": 0,
    }
    d.update(kw)
    return d


async def _online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        await store.save_account(n["node_id"], {"positions": [], "prices": {}})
        manager.nodes[n["node_id"]] = object()


async def test_sync_broadcasts_to_all_online(store, monkeypatch):
    await _online(store, mk_node("nd_a"), mk_node("nd_b"))
    await store.set_dispatch({"mode": "sync", "position_scope": "symbol"})
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


async def test_position_gate_skips_node_with_position(store, monkeypatch):
    await _online(store, mk_node("nd_a"))
    await store.save_account("nd_a", {"positions": [{"symbol": "EURUSD"}], "prices": {}})
    await store.set_dispatch({"mode": "sync", "position_scope": "symbol"})
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig2")
    assert sent == []  # gated, nothing sent


async def test_global_lot_overrides_volume(store, monkeypatch):
    await _online(store, mk_node("nd_a", lot_mode="global"))
    await store.set_dispatch({"mode": "sync", "position_scope": "symbol"})
    await store.set_lot_global({"enabled": True, "value": 0.25})
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
    await store.set_dispatch({"mode": "poll", "position_scope": "symbol"})
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
    await _online(store, mk_node("nd_a"), mk_node("nd_b"))
    await store.set_dispatch({"mode": "sync", "position_scope": "symbol"})
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
