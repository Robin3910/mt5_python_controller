"""Tests for the node client protocol logic using a fake websocket + mock MT5."""
import asyncio
import json

import node_client as nc


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self._q.get()

    def feed(self, obj: dict) -> None:
        self._q.put_nowait(json.dumps(obj))


def _node():
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    return n


async def test_authenticate_ok():
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    ws.feed({"type": "auth_ok", "data": {"node_id": "nd_x"}})
    assert await n._authenticate(ws) is True
    assert ws.sent[0]["type"] == "auth"
    assert ws.sent[0]["data"]["token"] == "test-token"
    assert ws.sent[0]["data"]["mt5_login"] == 90000001
    assert any(m["type"] == "hello" for m in ws.sent)


async def test_authenticate_fail():
    n = _node()
    ws = FakeWS()
    ws.feed({"type": "auth_fail", "data": {"reason": "invalid_token"}})
    assert await n._authenticate(ws) is False


async def test_open_emits_trade_result():
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "open", "signal_id": "s1", "action": "BUY", "symbol": "EURUSD",
         "volume": 0.1, "stop_loss": None, "take_profit": None, "comment": "", "magic": None},
    )
    tr = [m for m in ws.sent if m["type"] == "trade_result"]
    assert tr and tr[0]["data"]["success"] is True
    assert tr[0]["data"]["signal_id"] == "s1"
    assert tr[0]["data"]["symbol"] == "EURUSD"
    assert len(n.mt5.positions()) == 1


async def test_close_symbol_clears_position():
    n = _node()
    await n._exec(n.mt5.connect)
    await n._exec(n.mt5.place_market_order, "EURUSD", "BUY", 0.1)
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "close", "signal_id": "c1", "close_target": "symbol", "close_symbol": "EURUSD"},
    )
    tr = [m for m in ws.sent if m["type"] == "trade_result"][0]
    assert tr["data"]["success"] is True
    assert tr["data"]["symbol"] == "EURUSD"
    assert tr["data"]["action"] == "CLOSE"
    assert tr["data"]["detail"]
    assert len(n.mt5.positions()) == 0


async def test_close_ticket_includes_symbol():
    n = _node()
    await n._exec(n.mt5.connect)
    await n._exec(n.mt5.place_market_order, "EURUSD", "BUY", 0.1)
    ticket = n.mt5.positions()[0]["ticket"]
    ws = FakeWS()
    await n._handle(
        ws,
        {"cmd": "close", "signal_id": "c2", "close_target": "ticket", "close_ticket": ticket},
    )
    tr = [m for m in ws.sent if m["type"] == "trade_result"][0]
    assert tr["data"]["success"] is True
    assert tr["data"]["symbol"] == "EURUSD"
    assert tr["data"]["action"] == "CLOSE"
    assert str(ticket) in tr["data"]["detail"]


async def test_snapshot_shape():
    n = _node()
    await n._exec(n.mt5.connect)
    snap = await n._snapshot()
    assert set(snap.keys()) == {"account", "positions", "prices", "quotes"}
    assert "EURUSD" in snap["prices"]
    assert "EURUSD" in snap["quotes"]
    assert set(snap["quotes"]["EURUSD"].keys()) == {"bid", "ask", "mid", "change"}
    assert snap["account"]["login"]
