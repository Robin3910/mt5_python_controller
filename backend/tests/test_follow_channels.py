"""频道划分与历史持仓聚合的纯函数。"""
from app.follow_channels import (
    aggregate_closed_positions,
    channel_of_template,
    prefer_magic_row,
    select_channel_positions,
)


def test_channel_of_template_splits_markets():
    assert channel_of_template("tpl_1") == "ai"
    assert channel_of_template("tpl_2") == "follow"
    assert channel_of_template("tpl_3") == "follow"
    assert channel_of_template("tpl_9") is None
    assert channel_of_template(None) is None
    assert channel_of_template("  TPL_1 ") == "ai"


def test_prefer_magic_row_keeps_active_then_newer():
    older = {"dispatch_id": 1, "active": False}
    newer = {"dispatch_id": 8, "active": False}
    running = {"dispatch_id": 3, "active": True}
    assert prefer_magic_row(older, newer)["dispatch_id"] == 8
    assert prefer_magic_row(newer, running)["active"] is True
    assert prefer_magic_row(running, newer)["dispatch_id"] == 3


def test_aggregate_closed_positions_pairs_open_and_close():
    rows = aggregate_closed_positions([
        {
            "position_id": 11, "deal_id": 1, "entry_type": "IN", "direction": "BUY",
            "symbol": "XAUUSD", "price": 4100, "volume": 1, "net_profit": 0,
            "deal_time": 10, "reason": 3,
        },
        {
            "position_id": 11, "deal_id": 2, "entry_type": "OUT", "direction": "SELL",
            "symbol": "XAUUSD", "price": 4120, "volume": 1, "net_profit": 200,
            "deal_time": 20, "reason": 5,
        },
        {
            "position_id": 12, "deal_id": 3, "entry_type": "OUT", "direction": "BUY",
            "symbol": "EURUSD", "price": 1.1, "volume": 0.5, "net_profit": -30,
            "deal_time": 30, "reason": 4,
        },
        {
            "position_id": 13, "deal_id": 4, "entry_type": "IN", "direction": "BUY",
            "symbol": "GBPUSD", "price": 1.2, "volume": 1, "net_profit": 0,
            "deal_time": 40, "reason": 3,
        },
    ])
    assert [r["position_id"] for r in rows] == [12, 11]
    closed = rows[1]
    assert closed["direction"] == "BUY"
    assert closed["price_open"] == 4100
    assert closed["price_close"] == 4120
    assert closed["status"] == "tp"
    assert closed["status_label"] == "已止盈"
    assert closed["profit"] == 200
    stopped = rows[0]
    assert stopped["direction"] == "SELL"
    assert stopped["status"] == "sl"
    assert stopped["price_open"] == 0


def test_select_channel_positions_filters_market_profit_and_ticket():
    positions = [
        {"ticket": 1, "magic": 10, "profit": 5},
        {"ticket": 2, "magic": 10, "profit": -3},
        {"ticket": 3, "magic": 99, "profit": 8},
    ]
    magics = {10}
    assert [p["ticket"] for p in select_channel_positions(positions, magics, mode="all")] == [1, 2]
    assert [p["ticket"] for p in select_channel_positions(positions, magics, mode="profit")] == [1]
    one = select_channel_positions(positions, magics, mode="ticket", ticket=2)
    assert [p["ticket"] for p in one] == [2]
