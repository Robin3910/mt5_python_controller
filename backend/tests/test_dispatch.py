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


def _ack_open_sender(sent):
    """构造一个 send_to_node stub：记录下发并对 open 命令立即模拟成功回报。"""

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        if msg.get("cmd") == "open":
            results.resolve(msg["signal_id"], node_id, {"success": True, "symbol": msg["symbol"]})
        return True

    return fake_send


async def test_poll_rotation_single_node_consumes_and_rotates(store, monkeypatch):
    """轮询轮转：每条信号只由队首一个节点领取，领取者移到队尾，依次轮转。"""
    await _online(
        store,
        mk_node("nd_a", poll_order=0),
        mk_node("nd_b", poll_order=1),
        mk_node("nd_c", poll_order=2),
    )
    await set_symbol_filters(store, "EURUSD", mode="poll")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", _ack_open_sender(sent))

    d = Dispatcher(store)
    worker = PollWorker(store, d)

    # 第 1 条：队首 a 领取，a -> 队尾
    res = await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig1")
    assert res["mode"] == "poll" and res["targets"] == 3
    await worker.process("sig1")
    assert [s[0] for s in sent] == ["nd_a"]
    p1 = await store.get_poll_progress("sig1")
    assert p1["consumer"] == "nd_a" and p1["status"] == "done"
    assert await store.get_poll_rotation("EURUSD") == ["nd_b", "nd_c", "nd_a"]

    # 第 2 条：轮到 b
    sent.clear()
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig2")
    await worker.process("sig2")
    assert [s[0] for s in sent] == ["nd_b"]
    assert await store.get_poll_rotation("EURUSD") == ["nd_c", "nd_a", "nd_b"]

    # 第 3 条：轮到 c，一轮结束回到初始顺序
    sent.clear()
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig3")
    await worker.process("sig3")
    assert [s[0] for s in sent] == ["nd_c"]
    assert await store.get_poll_rotation("EURUSD") == ["nd_a", "nd_b", "nd_c"]

    # 第 4 条：又回到 a（循环轮转）
    sent.clear()
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig4")
    await worker.process("sig4")
    assert [s[0] for s in sent] == ["nd_a"]


async def test_poll_rotation_falls_through_filtered_head(store, monkeypatch):
    """队首节点被持仓过滤跳过时顺延给下一个节点；只有真正成交的节点移到队尾。"""
    await _online(store, mk_node("nd_a", poll_order=0), mk_node("nd_b", poll_order=1))
    # a 已持有同品种仓位 → 9.3 持仓过滤跳过 a
    await store.save_account("nd_a", {"positions": [{"symbol": "EURUSD"}], "prices": {}})
    await set_symbol_filters(store, "EURUSD", mode="poll", scope="symbol")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", _ack_open_sender(sent))

    d = Dispatcher(store)
    worker = PollWorker(store, d)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sigf")
    await worker.process("sigf")

    # a 被过滤，b 领取；a 保持队首（下次仍先轮到它），只有 b 参与“移到队尾”
    assert [s[0] for s in sent] == ["nd_b"]
    p = await store.get_poll_progress("sigf")
    assert p["consumer"] == "nd_b"
    assert await store.get_poll_rotation("EURUSD") == ["nd_a", "nd_b"]


async def test_poll_rotation_skips_offline_head(store, monkeypatch):
    """队首节点离线时顺延给下一个在线节点，离线节点保留其轮转位置。"""
    await _online(store, mk_node("nd_a", poll_order=0), mk_node("nd_b", poll_order=1))
    # a 掉线（从连接管理器移除），但仍是静态参与节点
    manager.nodes.pop("nd_a", None)
    await set_symbol_filters(store, "EURUSD", mode="poll")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", _ack_open_sender(sent))

    d = Dispatcher(store)
    worker = PollWorker(store, d)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sigo")
    await worker.process("sigo")

    assert [s[0] for s in sent] == ["nd_b"]
    assert await store.get_poll_rotation("EURUSD") == ["nd_a", "nd_b"]


async def test_poll_rotation_unconsumed_when_none_available(store, monkeypatch):
    """无任何在线可成交节点时，信号标记为 unconsumed，不下发任何命令。"""
    await _online(store, mk_node("nd_a", poll_order=0))
    manager.nodes.pop("nd_a", None)  # 唯一候选离线
    await set_symbol_filters(store, "EURUSD", mode="poll")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", _ack_open_sender(sent))

    d = Dispatcher(store)
    worker = PollWorker(store, d)
    await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sigu")
    await worker.process("sigu")

    assert sent == []
    p = await store.get_poll_progress("sigu")
    assert p["status"] == "unconsumed" and p["consumer"] is None


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


async def test_disabled_symbol_rejected_including_close(store, monkeypatch):
    await store.set_filters(
        {
            "EURUSD": {
                "enabled": False,
                "allow_buy": True,
                "allow_sell": True,
                "dispatch_mode": "sync",
                "position_scope": "symbol",
                "default_action": "pass",
                "intervals": [],
            }
        }
    )
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)
    await _online(store, mk_node("nd_a"))

    d = Dispatcher(store)
    buy = await d.dispatch(TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig_dis_buy")
    assert buy["mode"] == "rejected"
    assert "已禁用" in (buy.get("reason") or "")

    close = await d.dispatch(TradingSignal(action="CLOSE", symbol="EURUSD", volume=0.1), "sig_dis_close")
    assert close["mode"] == "rejected"
    assert "已禁用" in (close.get("reason") or "")
    assert sent == []
