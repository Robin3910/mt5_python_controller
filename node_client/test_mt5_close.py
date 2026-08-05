"""批量平仓的收口口径：closed 只统计真正平掉的笔数。

部分失败时若把尝试笔数当已平笔数上报，后台「全平 N 笔」和风控事件的 closed
都会偏大，掩盖掉还有持仓没平掉的事实。
"""
import mt5_client as mc


def _client() -> mc.MT5Client:
    return mc.MT5Client(42, "", "S1", path=r"C:\MT5\terminal64.exe")


def _pos(ticket: int, symbol: str = "XAUUSD", magic: int = 900001) -> dict:
    return {
        "ticket": ticket, "symbol": symbol, "type": "BUY", "volume": 0.1, "magic": magic,
    }


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
