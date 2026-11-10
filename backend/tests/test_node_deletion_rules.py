import pytest

from app.node_deletion_rules import deletion_blockers


def test_empty_snapshot_after_latest_trade_can_be_deleted():
    assert deletion_blockers({"positions": [], "orders": [], "books_readable": True, "updated_at": 100},
        online=True, now=110, active=False, pending=False, occupied=False, last_trade_at=99) == []


@pytest.mark.parametrize("changes, expected", [
    ({"online": False}, "节点离线"),
    ({"active": True}, "仍有运行中的策略任务"),
    ({"pending": True}, "仍有在途交易指令"),
    ({"occupied": True}, "仍有执行占位"),
    ({"last_trade_at": 101}, "账户快照早于最近交易回报，请等待新快照"),
    ({"now": 161}, "缺少 60 秒内的可读账户快照"),
])
def test_delete_gate_has_no_transaction_or_busy_bypass(changes, expected):
    args = {"online": True, "now": 110, "active": False, "pending": False, "occupied": False, **changes}
    assert expected in deletion_blockers({"positions": [], "orders": [], "books_readable": True, "updated_at": 100}, **args)


def test_snapshot_without_explicit_readability_cannot_authorize_deletion():
    assert "缺少 60 秒内的可读账户快照" in deletion_blockers({"positions": [], "orders": [], "updated_at": 100},
        online=True, now=110, active=False, pending=False, occupied=False)
