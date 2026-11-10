"""链路闸门（Link-Gate）测试：终端与券商断联时在发单前拦下开仓。

要挡住的是「断联期间下单、回执不可靠、单却可能已成交」这条孤儿单成因。
判据是终端链路状态（terminal_info().connected），三态：online / offline / unknown。
"""
import asyncio
import json

import pytest

import mock_mt5
import mt5_client
import node_client as nc


def test_connected_from_terminal_reads_link_field():
    """connected 与 trade_allowed 是两回事：一个看链路，一个看算法交易开关。"""
    assert mt5_client.connected_from_terminal(None) is None
    assert mt5_client.connected_from_terminal(object()) is None
    assert mt5_client.connected_from_terminal(
        type("Info", (), {"connected": False})()
    ) is False
    assert mt5_client.connected_from_terminal(
        type("Info", (), {"connected": True})()
    ) is True
    assert mt5_client.connected_from_terminal(
        type("Info", (), {"connected": 0})()
    ) is False


def test_link_state_debounces_offline():
    """单次抖动不该判离线：连续 3 次读到断开才算。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = False
    assert mt5.terminal_link_state() == mt5_client.LINK_ONLINE
    assert mt5.terminal_link_state() == mt5_client.LINK_ONLINE
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE
    # 判定离线后保持离线
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE


def test_link_state_recovers_only_after_confirmation():
    """恢复也要确认：终端刚重连时券商可能还没就绪，第一单最容易失败。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = False
    for _ in range(mt5_client.LINK_OFFLINE_CONFIRM):
        mt5.terminal_link_state()
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE

    mt5.link_connected = True
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE
    assert mt5.terminal_link_state() == mt5_client.LINK_ONLINE


def test_link_state_unknown_keeps_offline_but_never_claims_online():
    """读不到链路状态时不得当成连通；已判离线也不因读不到而恢复。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = False
    for _ in range(mt5_client.LINK_OFFLINE_CONFIRM):
        mt5.terminal_link_state()
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE

    # 读不到：既不恢复，也不改变已有判定
    mt5.link_connected = None
    assert mt5.terminal_link_state() == mt5_client.LINK_OFFLINE


def test_market_order_blocked_when_link_offline():
    """断联时市价单被拦下：确定没发出去，调用方可立即按失败收口。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = False

    res = mt5.place_market_order("XAUUSD", "BUY", 0.1, None, None, "", 900000001)

    assert res["success"] is False
    assert res["link_offline"] is True
    assert "断连" in res["error"]
    # 关键：单根本没发出去，账本里不能有任何持仓
    assert mt5.positions_by_magic(900000001) == []


def test_pending_order_blocked_when_link_offline():
    """限价挂单同样拦下：挂上去也可能成交，断联期间不该挂。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = False

    res = mt5.place_pending_order(
        "XAUUSD", "BUY", 0.1, 2300.0, None, None, "", 900000002, "limit",
    )

    assert res["success"] is False
    assert res["link_offline"] is True
    assert mt5.pending_orders_by_magic(900000002) == []


def test_market_order_allowed_when_link_online():
    """链路正常时闸门不介入。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True

    res = mt5.place_market_order("XAUUSD", "BUY", 0.1, None, None, "", 900000003)

    assert res["success"] is True
    assert len(mt5.positions_by_magic(900000003)) == 1


def test_link_unknown_does_not_block():
    """读不到链路状态时放行：闸门不能因为自己读不到而误伤正常交易。"""
    mt5 = mock_mt5.MockMT5Client()
    mt5.connected = True
    mt5.link_connected = None

    res = mt5.place_market_order("XAUUSD", "BUY", 0.1, None, None, "", 900000004)

    assert res["success"] is True
    assert len(mt5.positions_by_magic(900000004)) == 1


# ---------------------------------------------------------------------------
# 节点侧：strategy_start 入口闸门
# ---------------------------------------------------------------------------


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _node():
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    return n


async def test_strategy_start_rejected_when_link_offline():
    """断联时不建监控、不下单，直接回报失败 —— 杜绝孤儿单的入口闸门。"""
    n = _node()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = False
    ws = FakeWS()

    await n._handle(ws, {
        "cmd": "strategy_start", "task_id": 77, "magic": 900000077,
        "group_id": "g1", "signal_id": "s1",
        "entry": {"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        "strategy": {"rules": []},
    })

    trades = [m for m in ws.sent if m["type"] == "trade_result"]
    assert len(trades) == 1
    assert trades[0]["data"]["success"] is False
    assert trades[0]["data"]["link_offline"] is True
    # 不能建监控：没有监控就没有人管仓位
    assert 77 not in n.runners
    # 也没下单
    assert n.mt5.positions_by_magic(900000077) == []


async def test_strategy_start_allowed_when_link_online():
    """链路正常时正常建监控。"""
    n = _node()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = True
    ws = FakeWS()

    await n._handle(ws, {
        "cmd": "strategy_start", "task_id": 78, "magic": 900000078,
        "group_id": "g1", "signal_id": "s1",
        "entry": {"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        "strategy": {"rules": []},
    })

    assert 78 in n.runners
    assert not [m for m in ws.sent if m["type"] == "trade_result"]


async def test_strategy_resume_not_blocked_when_link_offline():
    """恢复场景不拦：已有持仓需要监控恢复，拦了等于放任孤儿仓。"""
    n = _node()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = False
    ws = FakeWS()

    await n._handle(ws, {
        "cmd": "strategy_resume", "task_id": 79, "magic": 900000079,
        "group_id": "g1", "signal_id": "s1",
        "entry": {"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        "strategy": {"rules": []},
    })

    assert 79 in n.runners


async def test_degraded_snapshot_reports_link_state():
    """账本读不到时的降级快照：带 mt5_link，books_ok=False。

    服务端据此保留上次快照并跳过对账（不会误判成「已平完」），
    同时仍然知道「这个节点现在断联了」。
    """
    n = _node()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = False

    def boom():
        raise RuntimeError("IPC timeout")

    n.mt5.positions = boom
    snap = await n._degraded_snapshot()

    assert snap is not None
    assert snap["books_ok"] is False
    assert snap["mt5_link"] == "online"  # 去抖口径：还没到 3 次，仍是 online


@pytest.mark.parametrize("state,expected", [
    ("online", "online"),
    ("offline", "offline"),
    ("unknown", "unknown"),
    ("garbage", "unknown"),
    (None, "unknown"),
])
async def test_link_state_normalises_unknown_values(state, expected):
    """非法值一律按 unknown（保守放行）。"""
    n = _node()

    class _Fake:
        def link_state(self):
            return state

    n.mt5 = _Fake()
    assert n._link_state() == expected


async def test_degraded_snapshot_does_not_run_risk_check(monkeypatch):
    """降级快照不得推进风控状态机。

    降级快照的 positions 是空的（账本读不到），拿它跑保护状态机会把 armed
    清掉 —— 下次开仓就误触发一次「浮盈亏保护收窄」，白扣次数甚至关掉规则。
    """
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()

    called = []

    async def fake_snap():
        return None

    async def fake_risk(_ws, _snap):
        called.append(1)

    n._snapshot = fake_snap  # type: ignore[method-assign]
    n._check_account_risk = fake_risk  # type: ignore[method-assign]
    n._check_login = lambda acct: None  # type: ignore[method-assign]

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(nc.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await n._reporter(ws)

    assert called == []
