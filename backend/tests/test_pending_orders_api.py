"""按订单号精确撤挂单（交易页「逐张撤」用）。

与 `test_pending_api.py`（按魔术号批量撤）分开：这里的语义是
**精确到张、不停策略、不动持仓**，两者的不变量完全不同。
"""
import fakeredis
import pytest

from app import node_rpc, pending_api
from app.connections import manager
from app.models import CancelPendingTicketsRequest
from app.redis_store import RedisStore
from app.state import state


class _Req:
    """最小 Request 替身：client_ip 只用到 headers 与 client.host。"""
    headers: dict = {}

    class client:  # noqa: N801
        host = "127.0.0.1"


class _Principal:
    username = "tester"


def _order(ticket: int, symbol: str = "XAUUSD", magic: int = 88888888) -> dict:
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": "BUY",
        "volume": 0.01,
        "price_open": 2400.0,
        "sl": 0.0,
        "tp": 0.0,
        "magic": magic,
        "comment": "",
        "time": 1700000000.0,
    }


@pytest.fixture(autouse=True)
def _clean():
    node_rpc.reset_waiters()
    yield
    node_rpc.reset_waiters()


@pytest.fixture
async def store(monkeypatch):
    redis = RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    state.store = redis

    async def no_ownership_check(_store, _p, _node_id):
        return None

    monkeypatch.setattr(pending_api, "owned_node_or_404", no_ownership_check)
    yield redis
    state.store = None


def _patch_cancel(monkeypatch, replies):
    """按调用次序回放节点回包，并记录下发的命令。"""
    sent = []

    async def fake_request(node_id, cmd, **kwargs):
        sent.append({"node_id": node_id, "cmd": cmd, **kwargs})
        idx = len(sent) - 1
        return replies[idx] if idx < len(replies) else {"success": True}

    monkeypatch.setattr(pending_api.node_rpc, "request", fake_request)
    return sent


async def _call_tickets(store, tickets, node_id="nd_a", online=True):
    if online:
        manager.nodes.setdefault(node_id, object())
    try:
        return await pending_api.cancel_pending_orders_route(
            node_id,
            CancelPendingTicketsRequest(tickets=tickets),
            _Req(),
            store=store,
            p=_Principal(),
        )
    finally:
        if online:
            manager.nodes.pop(node_id, None)


async def _call_ticket(store, ticket, node_id="nd_a", online=True):
    if online:
        manager.nodes.setdefault(node_id, object())
    try:
        return await pending_api.cancel_pending_order_route(
            node_id, ticket, _Req(), store=store, p=_Principal(),
        )
    finally:
        if online:
            manager.nodes.pop(node_id, None)


# ---------------------------------------------------------------------------
# 范围精确性：这是本端点存在的理由
# ---------------------------------------------------------------------------
async def test_cancels_only_named_tickets(store, monkeypatch):
    """只撤点名的这几张，不按魔术号扩散到别的品种。"""
    await store.save_account("nd_a", {"orders": [
        _order(1001, "XAUUSD", magic=555),
        _order(1002, "XAUUSD", magic=555),
        _order(2001, "EURUSD", magic=555),   # 同魔术号，另一个品种
    ]})
    sent = _patch_cancel(monkeypatch, [
        {"success": True}, {"success": True},
    ])

    res = await _call_tickets(store, [1001, 1002])

    assert res["status"] == "ok"
    assert res["cancelled"] == 2
    assert [c["cmd"]["ticket"] for c in sent] == [1001, 1002]
    assert all(c["cmd"]["cmd"] == "cancel_pending" for c in sent)


async def test_does_not_stop_strategies(store, monkeypatch):
    """撤单**不停策略**：strategy_stop 在节点侧会 close_by_magic 平掉持仓。"""
    await store.save_account("nd_a", {"orders": [_order(1001, magic=555)]})
    _patch_cancel(monkeypatch, [{"success": True}])

    res = await _call_tickets(store, [1001])

    # 结果里标出「这张单属于策略」，提示可能被重新挂出
    assert res["strategy_owned"] == [555]
    assert "strategies" not in res


async def test_marks_manual_cancelled_ticket(store, monkeypatch):
    """撤掉的 ticket 要记账：限价监听的撤单失败据此不误报。"""
    await store.save_account("nd_a", {"orders": [_order(1001)]})
    _patch_cancel(monkeypatch, [{"success": True}])

    await _call_tickets(store, [1001])

    assert await store.is_manual_cancelled_ticket("nd_a", 1001) is True


async def test_busy_window_set_and_cleared(store, monkeypatch):
    """撤单期间必须让限价监听避让，事后清干净。"""
    await store.save_account("nd_a", {"orders": [_order(1001)]})
    seen: list[bool] = []

    async def fake_request(_node_id, _cmd, **_k):
        seen.append(await store.is_cancel_pending_busy("nd_a"))
        return {"success": True}

    monkeypatch.setattr(pending_api.node_rpc, "request", fake_request)

    await _call_tickets(store, [1001])

    assert seen == [True]
    assert await store.is_cancel_pending_busy("nd_a") is False


# ---------------------------------------------------------------------------
# 失败分支
# ---------------------------------------------------------------------------
async def test_partial_when_some_fail(store, monkeypatch):
    await store.save_account("nd_a", {"orders": [
        _order(1001), _order(1002),
    ]})
    _patch_cancel(monkeypatch, [
        {"success": True},
        {"success": False, "error": "invalid price"},
    ])

    res = await _call_tickets(store, [1001, 1002])

    assert res["status"] == "partial"
    assert res["cancelled"] == 1
    assert res["failed"][0]["ticket"] == 1002
    assert res["failed"][0]["error"] == "invalid price"


async def test_all_failed_is_error(store, monkeypatch):
    _patch_cancel(monkeypatch, [{"success": False, "error": "order not found"}])

    res = await _call_tickets(store, [1001])

    assert res["status"] == "error"
    assert res["cancelled"] == 0


async def test_rejects_offline_node(store, monkeypatch):
    """离线节点直接 409，不下发任何命令。"""
    from fastapi import HTTPException

    sent = _patch_cancel(monkeypatch, [])

    with pytest.raises(HTTPException) as ei:
        await _call_tickets(store, [1001], online=False)

    assert ei.value.status_code == 409
    assert sent == []


async def test_rejects_empty_tickets(store, monkeypatch):
    """订单号非法 → 400，不静默当成「没有可撤的」。"""
    from fastapi import HTTPException

    _patch_cancel(monkeypatch, [])

    with pytest.raises(HTTPException) as ei:
        await _call_tickets(store, [0, -1])

    assert ei.value.status_code == 400


async def test_unknown_ticket_still_cancelled(store, monkeypatch):
    """快照里没有的 ticket 也要试着撤：快照有延迟，单可能仍在盘上。"""
    sent = _patch_cancel(monkeypatch, [{"success": True}])

    res = await _call_tickets(store, [9999])

    assert res["cancelled"] == 1
    assert sent[0]["cmd"]["ticket"] == 9999
    assert res["strategy_owned"] == []


# ---------------------------------------------------------------------------
# 单张端点
# ---------------------------------------------------------------------------
async def test_single_ticket_cancel_ok(store, monkeypatch):
    await store.save_account("nd_a", {"orders": [_order(1001, "XAUUSD", magic=777)]})
    sent = _patch_cancel(monkeypatch, [{"success": True, "ticket": 1001}])

    res = await _call_ticket(store, 1001)

    assert res["status"] == "ok"
    assert res["cancelled"] is True
    assert res["symbol"] == "XAUUSD"
    assert res["strategy_owned"] == 777
    assert sent[0]["cmd"]["cmd"] == "cancel_pending"
    assert await store.is_manual_cancelled_ticket("nd_a", 1001) is True


async def test_single_ticket_cancel_404_when_gone(store, monkeypatch):
    """单已不在盘上（已成交/已撤）：回 404，不是 500。"""
    from fastapi import HTTPException

    _patch_cancel(monkeypatch, [{"success": False, "error": "invalid ticket"}])

    with pytest.raises(HTTPException) as ei:
        await _call_ticket(store, 1001)

    assert ei.value.status_code == 404
    assert "invalid ticket" in ei.value.detail


async def test_single_ticket_cancel_offline(store, monkeypatch):
    from fastapi import HTTPException

    _patch_cancel(monkeypatch, [])

    with pytest.raises(HTTPException) as ei:
        await _call_ticket(store, 1001, online=False)

    assert ei.value.status_code == 409
