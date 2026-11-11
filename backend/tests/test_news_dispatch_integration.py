"""财经日历黑名单接入**分发引擎**的集成用例。

这是本需求最关键的一组回归，验证三件事：

1. 命中黑名单时**新开仓被拦**，节点**收不到** open 指令，且落 skip_code；
2. 黑名单期间**CLOSE 仍能正常下发**（平仓绝不能被黑名单挡住）；
3. 黑名单只影响**新开仓**，不影响已在跑的任务（不触发任何终止指令）。

用 fakeredis + stub manager，与 test_dispatch.py 的既有做法一致。
"""
import time

import fakeredis
import pytest

from app import news_guard, persist
from app.connections import manager
from app.dispatcher import Dispatcher
from app.parser import TradingSignal
from app.redis_store import RedisStore

# 判定用的假事件：XAUUSD / USD / high，落在当前时刻之后一点
EVENT_TIME = int(time.time()) + 600


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
    monkeypatch.setattr(persist, "update_signal_status", noop)
    monkeypatch.setattr(persist, "audit", noop)
    monkeypatch.setattr(manager, "broadcast_admin", noop)
    manager.nodes.clear()
    manager.admins.clear()
    yield
    manager.nodes.clear()
    manager.admins.clear()


_DEFAULT_PRICES = {"XAUUSD": 2400.0, "EURUSD": 1.1000}


def node_symbol_filters(symbol="XAUUSD", **extra):
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


async def _online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        await store.save_account(
            n["node_id"], {"positions": [], "prices": dict(_DEFAULT_PRICES)},
        )
        manager.nodes[n["node_id"]] = object()


async def set_symbol_filters(store, symbol, *, mode="sync", scope="symbol"):
    await store.set_filters({
        symbol: {
            "enabled": True,
            "allow_buy": True,
            "allow_sell": True,
            "dispatch_mode": mode,
            "position_scope": scope,
            "default_action": "pass",
            "intervals": [],
        }
    })


@pytest.fixture
def blackout(monkeypatch):
    """让判定层永远返回「命中黑名单」，并记录放行次数。

    直接替换 `news_guard.block_open_reason` —— 判定逻辑本身在
    test_news_blackout.py 里逐条覆盖了，这里只验**接入点接对了没**。
    """
    calls: list = []

    async def fake_block(user_id, symbol, *, now=None):
        calls.append((user_id, symbol))
        return "CPI y/y (USD, high) 公布时段黑名单（±30 分钟）"

    monkeypatch.setattr(news_guard, "block_open_reason", fake_block)
    return calls


async def test_open_is_blocked_during_blackout(store, monkeypatch, blackout):
    """命中黑名单时新开仓被拦，节点收不到 open 指令。"""
    await _online(store, mk_node("nd_a"))
    await set_symbol_filters(store, "XAUUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig-bl")

    # 判定被调用过，且**没有下发任何指令**
    assert blackout, "黑名单判定应被调用"
    assert sent == [], f"黑名单期间不应下发开仓指令，实际下发了 {sent}"


async def test_close_still_sent_during_blackout(store, monkeypatch, blackout):
    """黑名单期间 CLOSE 仍能正常下发 —— 平仓绝不能被挡住。

    这是本需求最重要的一条回归：拦错就会让持仓平不掉。
    """
    await _online(store, mk_node("nd_a"))
    await set_symbol_filters(store, "XAUUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.0), "sig-close")

    # 平仓必须下发出去
    assert sent, "黑名单期间平仓仍须下发"
    assert all(msg.get("cmd") == "close" for _, msg in sent), "下发的应是 close 指令"
    # 平仓路径不经过黑名单判定
    assert blackout == [], "平仓不应触发开仓黑名单判定"


async def test_no_blackout_still_opens(store, monkeypatch):
    """判定放行时，开仓照常下发（黑名单不应误伤正常交易）。"""
    await _online(store, mk_node("nd_a"))
    await set_symbol_filters(store, "XAUUSD", mode="sync")
    sent = []

    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    async def allow(user_id, symbol, *, now=None):
        return None

    monkeypatch.setattr(news_guard, "block_open_reason", allow)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig-open")

    assert sent, "未命中黑名单时开仓应照常下发"
    assert all(msg.get("cmd") == "open" for _, msg in sent)


async def test_blackout_uses_node_owner_identity(store, monkeypatch, blackout):
    """判定用的是**节点所有者**的身份（节点归谁，就用谁的开关）。"""
    await _online(store, mk_node("nd_a", owner_user_id=4242))
    await set_symbol_filters(store, "XAUUSD", mode="sync")

    async def fake_send(node_id, msg):
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    d = Dispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig-owner")

    assert blackout, "黑名单判定应被调用"
    assert blackout[0][0] == 4242, "判定应使用节点所有者的 user_id"
