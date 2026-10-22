"""批量撤挂单（人工「删挂单」节点侧落点）的范围过滤与 remaining 口径。

三条不变量：
- `scope` 决定撤哪些单：all 撤光 / manual 只撤 magic=0 / strategy 只撤指定魔术号；
- `remaining` 必须在撤完**复读**挂单簿，旧快照会骗人（服务端复扫靠它判残余）；
- 部分失败时 `cancelled` 只算真撤掉的，不能把尝试笔数当已撤笔数上报。
"""
import pytest

import mt5_client as mc


def _order(ticket: int, symbol: str = "XAUUSD", magic: int = 0) -> dict:
    return {
        "ticket": ticket, "symbol": symbol, "type": "BUY", "pending_kind": "limit",
        "volume": 0.1, "price_open": 2300.0, "magic": magic,
    }


class _FakeClient(mc.MT5Client):
    """可编排的挂单簿：撤单成功即从簿上消失。"""

    def __init__(self, orders, failed=None):
        super().__init__(42, "", "S1", path=r"C:\MT5\terminal64.exe")
        self._rows = [dict(o) for o in orders]
        self.failed = set(failed or ())
        self.cancelled: list[int] = []
        self.ensure = lambda: None      # type: ignore[method-assign]

    def pending_orders(self):
        return [dict(o) for o in self._rows]

    def cancel_order(self, ticket: int, max_retry: int = 3) -> dict:
        ticket = int(ticket)
        self.cancelled.append(ticket)
        if ticket in self.failed:
            return {"success": False, "action": "CANCEL", "ticket": ticket,
                    "retcode": 1, "error": "invalid request"}
        self._rows = [o for o in self._rows if int(o["ticket"]) != ticket]
        return {"success": True, "action": "CANCEL", "ticket": ticket,
                "symbol": "XAUUSD", "retcode": 10009}


def test_scope_all_cancels_everything():
    c = _FakeClient([_order(1), _order(2, magic=900001)])
    res = c.cancel_pending_all("all")
    assert res["success"] is True
    assert res["cancelled"] == 2
    assert res["remaining"] == 0
    assert sorted(c.cancelled) == [1, 2]


def test_scope_manual_only_cancels_magic_zero():
    """手工单与策略单都在时，manual 只能动 magic=0 的那些。"""
    c = _FakeClient([_order(1, magic=0), _order(2, magic=900001)])
    res = c.cancel_pending_all("manual")
    assert c.cancelled == [1]
    assert res["cancelled"] == 1
    assert res["remaining"] == 1          # 策略单还在，节点侧复读给出的权威值


def test_scope_strategy_filters_by_magics():
    c = _FakeClient([_order(1, magic=900001), _order(2, magic=900002), _order(3, magic=0)])
    res = c.cancel_pending_all("strategy", magics={900001, 900002})
    assert sorted(c.cancelled) == [1, 2]
    assert res["cancelled"] == 2
    assert res["remaining"] == 1


def test_scope_strategy_without_magics_cancels_nothing():
    """缺魔术号时必须「什么都不撤」而不是撤光 —— 宁可不动也不能误伤。"""
    c = _FakeClient([_order(1), _order(2)])
    res = c.cancel_pending_all("strategy")
    assert c.cancelled == []
    assert res["cancelled"] == 0
    assert res["remaining"] == 2
    assert res["success"] is True
    assert "魔术号" in res["error"]


def test_remaining_is_read_after_cancel_not_from_snapshot():
    """撤单后又有新单进来：remaining 必须是撤完复读的新值。"""
    c = _FakeClient([_order(1)])

    real_pending = c.pending_orders
    state = {"calls": 0}

    def flaky_pending():
        state["calls"] += 1
        if state["calls"] == 1:
            return real_pending()          # 撤单前的快照
        return real_pending() + [_order(99)]   # 撤单后冒出新单

    c.pending_orders = flaky_pending      # type: ignore[method-assign]
    res = c.cancel_pending_all("all")
    assert res["cancelled"] == 1
    assert res["remaining"] == 1           # 服务端据此判「撤了又长回来」


def test_failed_cancels_are_reported_and_not_counted():
    c = _FakeClient([_order(1), _order(2), _order(3)], failed={2})
    res = c.cancel_pending_all("all")
    assert res["success"] is False
    assert res["cancelled"] == 2           # 尝试 3 笔，只计真撤掉的 2 笔
    assert len(res["failed"]) == 1
    assert res["failed"][0]["ticket"] == 2
    assert res["remaining"] == 1


def test_no_orders_returns_clean_success():
    """空簿不是故障：success=True / cancelled=0 / remaining=0。"""
    c = _FakeClient([])
    res = c.cancel_pending_all("all")
    assert res == {
        "success": True, "action": "CANCEL_ALL", "scope": "all",
        "cancelled": 0, "failed": [], "results": [], "remaining": 0,
    }


def test_missing_magic_is_treated_as_manual():
    """老节点/老数据可能没有 magic 字段：按手工单处理，不因 None 崩掉。"""
    row = _order(1)
    row.pop("magic")
    c = _FakeClient([row, _order(2, magic=900001)])
    res = c.cancel_pending_all("manual")
    assert c.cancelled == [1]
    assert res["remaining"] == 1


@pytest.mark.parametrize("scope,expected,cancelled", [
    ("ALL", "all", 2),
    (" manual ", "manual", 1),           # 空白与大小写是合法的规范化输入
    ("Strategy", "strategy", 0),         # strategy 未给 magics：一张都不撤
    (None, "all", 2),                    # 缺省 = 撤光
    ("", "all", 2),
    ("weird", "all", 2),                 # 未知值兜底
])
def test_scope_is_normalized_or_falls_back(scope, expected, cancelled):
    """合法值做规范化（大小写/空白），非法值兜底成 all —— 回显值永远是已知口径。"""
    c = _FakeClient([_order(1, magic=0), _order(2, magic=900001)])
    res = c.cancel_pending_all(scope)
    assert res["scope"] == expected
    assert res["cancelled"] == cancelled