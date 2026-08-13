"""批量平仓的收口口径：closed 只统计真正平掉的笔数，且挂单要一并撤掉。

部分失败时若把尝试笔数当已平笔数上报，后台「全平 N 笔」和风控事件的 closed
都会偏大，掩盖掉还有持仓没平掉的事实。

平仓路径同时负责撤挂单：只平持仓会把未成交的限价单留在终端，等策略任务收口、
监控停掉之后它仍可能成交，变成没人管的孤儿仓。
"""
import pytest

import mt5_client as mc


def _client(orders: list[dict] | None = None) -> mc.MT5Client:
    """构造未连接的客户端；挂单查询默认打桩为空，避免走到 ensure()。"""
    client = mc.MT5Client(42, "", "S1", path=r"C:\MT5\terminal64.exe")
    rows = list(orders or [])
    client.pending_orders = lambda: [dict(o) for o in rows]  # type: ignore[method-assign]
    return client


def _pos(ticket: int, symbol: str = "XAUUSD", magic: int = 900001) -> dict:
    return {
        "ticket": ticket, "symbol": symbol, "type": "BUY", "volume": 0.1, "magic": magic,
    }


def _order(ticket: int, symbol: str = "XAUUSD", magic: int = 900001) -> dict:
    return {
        "ticket": ticket, "symbol": symbol, "type": "BUY", "pending_kind": "limit",
        "volume": 0.1, "price_open": 2300.0, "magic": magic,
    }


def _stub_cancel(client, failed: set[int], monkeypatch) -> None:
    """让指定 ticket 的撤单失败，其余成功。"""
    def fake(ticket: int, max_retry: int = 3) -> dict:
        ok = int(ticket) not in failed
        out = {"success": ok, "action": "CANCEL", "ticket": int(ticket)}
        if not ok:
            out["error"] = "order not found"
        return out

    monkeypatch.setattr(client, "cancel_order", fake)


def _stub_close(client, failed: set[int], monkeypatch) -> None:
    """让指定 ticket 的平仓失败，其余成功。"""
    def fake(pos: dict) -> dict:
        ok = pos["ticket"] not in failed
        out = {
            "success": ok, "ticket": pos["ticket"],
            "symbol": pos["symbol"], "action": "CLOSE",
        }
        if not ok:
            out["error"] = "position closed"
        return out

    monkeypatch.setattr(client, "close_position", fake)


def test_close_positions_counts_only_successful(monkeypatch):
    client = _client()
    _stub_close(client, {22}, monkeypatch)

    res = client.close_positions([_pos(11), _pos(22)])
    assert res["success"] is False
    assert res["closed"] == 1


def test_close_all_counts_only_successful(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "positions", lambda: [_pos(11), _pos(22), _pos(33)])
    _stub_close(client, {22}, monkeypatch)

    res = client.close_all()
    assert res["success"] is False
    assert res["closed"] == 2


def test_close_by_magic_counts_only_successful(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "positions",
        lambda: [_pos(11, magic=7), _pos(22, magic=7), _pos(33, magic=9)],
    )
    _stub_close(client, {11}, monkeypatch)

    res = client.close_by_magic(7)
    assert res["success"] is False
    assert res["closed"] == 1  # 只有 22 平掉，33 不属于该魔术号


def test_close_symbol_counts_only_successful(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "positions",
        lambda: [_pos(11, "XAUUSD"), _pos(22, "XAUUSD"), _pos(33, "EURUSD")],
    )
    _stub_close(client, {22}, monkeypatch)

    res = client.close_symbol("XAUUSD")
    assert res["success"] is False
    assert res["closed"] == 1


def test_full_success_keeps_total_count(monkeypatch):
    """全部成功时口径不变，既有上报与文案保持原样。"""
    client = _client()
    monkeypatch.setattr(client, "positions", lambda: [_pos(11), _pos(22)])
    _stub_close(client, set(), monkeypatch)

    res = client.close_all()
    assert res["success"] is True
    assert res["closed"] == 2


def test_nothing_to_close_stays_success(monkeypatch):
    """无仓可平仍是成功、closed 为 0：风控清仓与策略收口先后到达时靠这条不误报。"""
    client = _client()
    monkeypatch.setattr(client, "positions", lambda: [])

    res = client.close_all()
    assert res["success"] is True
    assert res["closed"] == 0
    assert res["cancelled"] == 0


# --------------------------- 挂单一并撤掉 ---------------------------
def test_close_all_cancels_pending_orders(monkeypatch):
    """清仓要连挂单一起撤，否则清完仓挂单成交等于又开了一笔无人监管的仓。"""
    client = _client([_order(51), _order(52, "EURUSD")])
    monkeypatch.setattr(client, "positions", lambda: [_pos(11)])
    _stub_close(client, set(), monkeypatch)
    _stub_cancel(client, set(), monkeypatch)

    res = client.close_all()
    assert res["success"] is True
    assert res["closed"] == 1
    assert res["cancelled"] == 2


def test_close_by_magic_only_cancels_own_orders(monkeypatch):
    """只撤本任务魔术号的挂单，同品种其它策略的单不受影响。"""
    client = _client([_order(51, magic=7), _order(52, magic=9)])
    monkeypatch.setattr(client, "positions", lambda: [_pos(11, magic=7)])
    _stub_close(client, set(), monkeypatch)
    _stub_cancel(client, set(), monkeypatch)

    res = client.close_by_magic(7)
    assert res["cancelled"] == 1
    assert [r["ticket"] for r in res["cancel_results"]] == [51]


def test_close_symbol_matches_broker_suffix(monkeypatch):
    """撤单的品种匹配沿用平仓那套前缀规则，兼容 XAUUSD.m 之类的券商后缀。"""
    client = _client([_order(51, "XAUUSD.m"), _order(52, "EURUSD")])
    monkeypatch.setattr(client, "positions", lambda: [])
    _stub_cancel(client, set(), monkeypatch)

    res = client.close_symbol("XAUUSD")
    assert res["cancelled"] == 1


def test_cancel_failure_marks_close_unsuccessful(monkeypatch):
    """撤单失败必须让整体判失败：留在盘上的挂单同样是没收干净的残留。"""
    client = _client([_order(51)])
    monkeypatch.setattr(client, "positions", lambda: [_pos(11)])
    _stub_close(client, set(), monkeypatch)
    _stub_cancel(client, {51}, monkeypatch)

    res = client.close_all()
    assert res["success"] is False
    assert res["closed"] == 1
    assert res["cancelled"] == 0


def test_cancel_runs_before_close(monkeypatch):
    """必须先撤单再平仓：反过来的话，两步之间价格触及挂单价会当场又开一笔。"""
    client = _client([_order(51)])
    monkeypatch.setattr(client, "positions", lambda: [_pos(11)])
    calls: list[str] = []
    monkeypatch.setattr(
        client, "cancel_order",
        lambda ticket, max_retry=3: (calls.append("cancel"),
                                     {"success": True, "ticket": ticket})[1],
    )
    monkeypatch.setattr(
        client, "close_position",
        lambda pos, max_retry=3: (calls.append("close"),
                                  {"success": True, "ticket": pos["ticket"]})[1],
    )

    client.close_by_magic(900001)
    assert calls == ["cancel", "close"]


@pytest.mark.parametrize("method", ["close_all", "close_by_magic", "close_symbol"])
def test_close_paths_expose_cancelled_count(monkeypatch, method):
    """三个收口口径都要给出 cancelled，运维才能看出挂单有没有清干净。"""
    client = _client([_order(51)])
    monkeypatch.setattr(client, "positions", lambda: [])
    _stub_cancel(client, set(), monkeypatch)

    args = {"close_by_magic": (900001,), "close_symbol": ("XAUUSD",)}.get(method, ())
    res = getattr(client, method)(*args)
    assert res["cancelled"] == 1
