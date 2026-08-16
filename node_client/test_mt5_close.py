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


def _pos(ticket: int, symbol: str = "XAUUSD", magic: int = 900001,
         *, side: str = "BUY", volume: float = 0.1) -> dict:
    return {
        "ticket": ticket, "symbol": symbol, "type": side, "volume": volume, "magic": magic,
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
    def fake(pos: dict, max_retry: int = 3, **_kw) -> dict:
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
        lambda pos, max_retry=3, **_kw: (calls.append("close"),
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


# --------------------------- 爆发式连发 / 对冲对敲 ---------------------------
def test_backoff_sleep_skipped_when_no_retry(monkeypatch):
    """首轮 max_retry=0 失败后不能 sleep，否则后面的仓都要排队等。"""
    slept: list[float] = []
    monkeypatch.setattr(mc.time, "sleep", lambda s: slept.append(s))
    mc._backoff_sleep(1, 0)
    assert slept == []
    mc._backoff_sleep(1, 3)
    assert slept == [0.3]


def test_too_many_requests_is_retryable():
    """爆发式连发容易碰到 10024，必须进第二轮重试而不是当致命错误丢掉。"""
    assert mc.RET_TOO_MANY_REQUESTS in mc.TRANSIENT
    assert mc._retryable_batch_fail({"success": False, "retcode": mc.RET_TOO_MANY_REQUESTS})
    assert not mc._retryable_batch_fail({"success": False, "retcode": 10019})  # NO_MONEY


def test_pair_hedge_positions_prefers_equal_volume():
    buy_small = _pos(1, volume=0.1)
    buy_big = _pos(2, volume=0.2)
    sell_big = _pos(3, side="SELL", volume=0.2)
    sell_small = _pos(4, side="SELL", volume=0.1)
    pairs = mc.pair_hedge_positions([buy_small, buy_big, sell_big, sell_small])
    tickets = {(int(a["ticket"]), int(b["ticket"])) for a, b in pairs}
    assert tickets == {(1, 4), (2, 3)}


def test_pair_hedge_positions_does_not_cross_symbol():
    pairs = mc.pair_hedge_positions([
        _pos(1, "XAUUSD"), _pos(2, "EURUSD", side="SELL"),
    ])
    assert pairs == []


def test_close_all_burst_then_retries_transient(monkeypatch):
    """首轮把请求全部打出；requote 的那笔才进入带重试的第二轮。"""
    client = _client()
    monkeypatch.setattr(client, "positions", lambda: [_pos(11), _pos(22), _pos(33)])
    calls: list[tuple[int, int]] = []

    def fake(pos: dict, max_retry: int = 3, **_kw) -> dict:
        calls.append((int(pos["ticket"]), max_retry))
        if pos["ticket"] == 22 and max_retry == 0:
            return {
                "success": False, "ticket": 22, "symbol": "XAUUSD",
                "action": "CLOSE", "retcode": mc.RET_REQUOTE,
            }
        return {"success": True, "ticket": pos["ticket"], "symbol": pos["symbol"], "action": "CLOSE"}

    monkeypatch.setattr(client, "close_position", fake)
    res = client.close_all()
    assert calls == [(11, 0), (22, 0), (33, 0), (22, 3)]
    assert res["success"] is True
    assert res["closed"] == 3


def test_close_all_does_not_retry_fatal(monkeypatch):
    """市价关闭等致命错误第二轮再试也没用，不能拖住整波清仓。"""
    client = _client()
    monkeypatch.setattr(client, "positions", lambda: [_pos(11), _pos(22)])
    calls: list[tuple[int, int]] = []

    def fake(pos: dict, max_retry: int = 3, **_kw) -> dict:
        calls.append((int(pos["ticket"]), max_retry))
        if pos["ticket"] == 22:
            return {
                "success": False, "ticket": 22, "symbol": "XAUUSD",
                "action": "CLOSE", "retcode": 10018, "error": "market closed",
            }
        return {"success": True, "ticket": pos["ticket"], "symbol": pos["symbol"], "action": "CLOSE"}

    monkeypatch.setattr(client, "close_position", fake)
    res = client.close_all()
    assert calls == [(11, 0), (22, 0)]
    assert res["success"] is False
    assert res["closed"] == 1


def test_close_all_close_by_then_market_leftover(monkeypatch):
    """对冲账户：同品种多空先对敲，剩下一笔再市价平。"""
    client = _client()
    live = [_pos(11), _pos(22, side="SELL"), _pos(33)]

    monkeypatch.setattr(client, "positions", lambda: list(live))
    monkeypatch.setattr(client, "_hedge_close_by_available", lambda: True)

    def fake_close_by(pos, pos_by, max_retry=0):
        gone = {int(pos["ticket"]), int(pos_by["ticket"])}
        live[:] = [p for p in live if int(p["ticket"]) not in gone]
        return {"success": True, "action": "CLOSE_BY"}

    monkeypatch.setattr(client, "_close_by_positions", fake_close_by)
    _stub_close(client, set(), monkeypatch)

    res = client.close_all()
    assert res["success"] is True
    assert res["closed"] == 3
    assert sum(1 for r in res["results"] if r.get("close_by")) == 2
    assert [r["ticket"] for r in res["results"] if not r.get("close_by")] == [33]


def test_close_by_magic_does_not_use_close_by(monkeypatch):
    """按魔术号收口不能 CLOSE_BY，否则可能误平另一条策略的反向仓。"""
    client = _client()
    monkeypatch.setattr(
        client, "positions",
        lambda: [_pos(11, magic=7), _pos(22, magic=7, side="SELL")],
    )
    monkeypatch.setattr(client, "_hedge_close_by_available", lambda: True)
    called: list[int] = []
    monkeypatch.setattr(
        client, "_close_by_positions",
        lambda *a, **k: called.append(1) or {"success": True},
    )
    _stub_close(client, set(), monkeypatch)

    res = client.close_by_magic(7)
    assert called == []
    assert res["closed"] == 2
