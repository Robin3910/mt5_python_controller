"""分组限价挂单监听：纯函数识别 / 映射，以及编排层（撤单后再走 process_signal）。"""
import fakeredis
import pytest
from sqlalchemy import delete

from app import group_service, limit_watch
from app.db import SessionLocal, init_db
from app.models import GroupCreate, GroupUpdate
from app.orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    NodeGroup,
    NodeGroupMember,
    SignalDispatch,
    SignalHistory,
    TradingStrategy,
)
from app.redis_store import RedisStore
from app.state import state
from app.strategy_templates import (
    RULE_TYPE_RISK_SIZED,
    TEMPLATE_1_ID,
    TEMPLATE_1_NAME,
    TEMPLATE_2_ID,
    TEMPLATE_2_NAME,
)

_TABLES = (
    GroupTaskEvent,
    GroupTaskDispatch,
    GroupSignalTask,
    NodeGroupMember,
    NodeGroup,
    TradingStrategy,
    SignalDispatch,
    SignalHistory,
)

_strategy_seq = iter(range(1, 10_000))


@pytest.fixture
async def store():
    await init_db()
    async with SessionLocal() as s:
        for table in _TABLES:
            await s.execute(delete(table))
        await s.commit()
    redis = RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    state.store = redis
    state.dispatcher = object()
    state.group_dispatcher = object()
    limit_watch.reset_cancel_waiters()
    yield redis
    state.store = None
    state.dispatcher = None
    state.group_dispatcher = None
    limit_watch.reset_cancel_waiters()


def _async_raise(exc: BaseException):
    async def _inner(*_a, **_k):
        raise exc
    return _inner


def _order(**over) -> dict:
    row = {
        "ticket": 1001,
        "symbol": "XAUUSD",
        "type": "BUY",
        "pending_kind": "limit",
        "volume": 0.1,
        "price_open": 2390.0,
        "sl": 2380.0,
        "tp": 0.0,
        "magic": 0,
        "comment": "open limit here",
    }
    row.update(over)
    return row


def _risk_rule(**over) -> dict:
    rule = {
        "type": RULE_TYPE_RISK_SIZED, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0,
        "entry_mode": "market",
    }
    rule.update(over)
    return rule


async def _mk_strategy(store, *, template_id=TEMPLATE_2_ID, template_name=TEMPLATE_2_NAME,
                       symbol="XAUUSD", name=None, enabled=True, rules=None):
    sid = f"sty_lw{next(_strategy_seq)}"
    row = {
        "strategy_id": sid,
        "name": name or f"策略{sid}",
        "template_id": template_id,
        "template_name": template_name,
        "symbol": symbol,
        "enabled": enabled,
        "rules": rules if rules is not None else [_risk_rule()],
        "remark": None,
        "created_at": 0,
    }
    async with SessionLocal() as s:
        s.add(
            TradingStrategy(
                strategy_id=sid,
                name=row["name"],
                template_id=template_id,
                template_name=template_name,
                symbol=symbol,
                enabled=enabled,
                config_json=row["rules"],
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def _ensure_node(store, node_id="nd_a"):
    await store.cache_node({
        "node_id": node_id,
        "name": node_id,
        "enabled": True,
        "mt5_login": 10001,
        "created_at": 0,
    })


async def _mk_group(store, name, node_ids, *, strategy, watch=True, keyword="limit",
                    enabled=True):
    if node_ids:
        for nid in node_ids:
            if not await store.get_node(nid):
                await _ensure_node(store, nid)
    g = await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=enabled, dispatch_mode="sync",
            strategy_id=strategy["strategy_id"], node_ids=list(node_ids),
            limit_watch_enabled=watch, limit_watch_keyword=keyword,
        ),
    )
    return g


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_normalize_keyword_defaults_and_clips():
    assert limit_watch.normalize_keyword(None) == "limit"
    assert limit_watch.normalize_keyword("  ") == "limit"
    assert limit_watch.normalize_keyword(" SIGNAL ") == "SIGNAL"
    assert len(limit_watch.normalize_keyword("x" * 80)) == limit_watch.KEYWORD_MAX_LEN


def test_keyword_matches_is_case_insensitive_substring():
    assert limit_watch.keyword_matches("Open LIMIT here", "limit")
    assert not limit_watch.keyword_matches("manual", "limit")
    assert limit_watch.keyword_matches("abcSIGNALxyz", "signal")


def test_order_incomplete_reason_requires_limit_fields():
    assert limit_watch.order_incomplete_reason(_order()) is None
    assert "限价" in (limit_watch.order_incomplete_reason(_order(pending_kind="stop")) or "")
    assert "手数" in (limit_watch.order_incomplete_reason(_order(volume=0)) or "")
    assert "挂单价" in (limit_watch.order_incomplete_reason(_order(price_open=0)) or "")
    assert "止损" in (limit_watch.order_incomplete_reason(_order(sl=0)) or "")
    assert "方向" in (limit_watch.order_incomplete_reason(_order(type="CLOSE")) or "")


def test_build_signal_payload_maps_to_webhook_shape():
    data = limit_watch.build_signal_payload(
        _order(tp=2410.0, comment="limit"), ["grp_a", "grp_b"],
    )
    assert data["action"] == "BUY"
    assert data["symbol"] == "XAUUSD"
    assert data["volume"] == 0.1
    assert data["model"] == "strategy"
    assert data["sl"] == 2380.0
    assert data["limit_price"] == 2390.0
    assert data["tp"] == 2410.0
    assert data["comment"] == "limit"
    assert data["group_ids"] == ["grp_a", "grp_b"]


def test_build_signal_payload_omits_empty_tp():
    data = limit_watch.build_signal_payload(_order(tp=0), ["grp_a"])
    assert "tp" not in data


def test_select_watch_targets_sends_all_matching_groups():
    sty = {
        "strategy_id": "sty_1", "template_id": TEMPLATE_2_ID, "enabled": True,
        "symbol": "XAUUSD", "rules": [_risk_rule()],
    }
    groups = [
        {
            "group_id": "g1", "name": "一组", "enabled": True,
            "limit_watch_enabled": True, "limit_watch_keyword": "limit",
            "strategy_id": "sty_1",
            "members": [{"node_id": "nd_a", "sort_order": 0}],
        },
        {
            "group_id": "g2", "name": "二组", "enabled": True,
            "limit_watch_enabled": True, "limit_watch_keyword": "limit",
            "strategy_id": "sty_1",
            "members": [{"node_id": "nd_a", "sort_order": 0}],
        },
        {
            "group_id": "g3", "name": "关键字不同", "enabled": True,
            "limit_watch_enabled": True, "limit_watch_keyword": "signal",
            "strategy_id": "sty_1",
            "members": [{"node_id": "nd_a", "sort_order": 0}],
        },
    ]
    passing, blocked = limit_watch.select_watch_targets(
        groups, {"sty_1": sty}, "nd_a", _order(),
    )
    assert [g["group_id"] for g in passing] == ["g1", "g2"]
    assert blocked == []


def test_select_watch_targets_ignores_non_member_and_tpl1():
    trend = {
        "strategy_id": "sty_t", "template_id": TEMPLATE_2_ID, "enabled": True,
        "symbol": "XAUUSD", "rules": [_risk_rule()],
    }
    other = {
        "strategy_id": "sty_1", "template_id": TEMPLATE_1_ID, "enabled": True,
        "symbol": "XAUUSD", "rules": [],
    }
    groups = [
        {
            "group_id": "g_other_node", "enabled": True,
            "limit_watch_enabled": True, "limit_watch_keyword": "limit",
            "strategy_id": "sty_t",
            "members": [{"node_id": "nd_b", "sort_order": 0}],
        },
        {
            "group_id": "g_tpl1", "enabled": True,
            "limit_watch_enabled": True, "limit_watch_keyword": "limit",
            "strategy_id": "sty_1",
            "members": [{"node_id": "nd_a", "sort_order": 0}],
        },
    ]
    passing, blocked = limit_watch.select_watch_targets(
        groups, {"sty_t": trend, "sty_1": other}, "nd_a", _order(),
    )
    assert passing == []
    assert [g["group_id"] for g, _ in blocked] == ["g_tpl1"]


def test_select_watch_targets_blocks_limit_mode_wrong_side():
    sty = {
        "strategy_id": "sty_1", "template_id": TEMPLATE_2_ID, "enabled": True,
        "symbol": "XAUUSD",
        "rules": [_risk_rule(entry_mode="limit")],
    }
    groups = [{
        "group_id": "g1", "name": "限价组", "enabled": True,
        "limit_watch_enabled": True, "limit_watch_keyword": "limit",
        "strategy_id": "sty_1",
        "members": [{"node_id": "nd_a", "sort_order": 0}],
    }]
    passing, blocked = limit_watch.select_watch_targets(
        groups, {"sty_1": sty}, "nd_a",
        _order(price_open=2380.0, sl=2390.0),
    )
    assert passing == []
    assert blocked and "入场价" in blocked[0][1]


def test_is_manual_pending_skips_strategy_magic():
    assert limit_watch.is_manual_pending(_order(magic=0))
    assert not limit_watch.is_manual_pending(_order(magic=900000001))


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
async def test_handle_cancels_then_dispatches_to_all_hits(store, monkeypatch):
    sty = await _mk_strategy(store)
    g1 = await _mk_group(store, "监听一组", ["nd_a"], strategy=sty, keyword="limit")
    sty2 = await _mk_strategy(store, name="第二趋势")
    g2 = await _mk_group(store, "监听二组", ["nd_a"], strategy=sty2, keyword="limit")

    cancelled = []
    dispatched = []

    async def fake_cancel(node_id, ticket):
        cancelled.append((node_id, ticket))
        return {"success": True, "ticket": ticket}

    async def fake_dispatch(payload, *, raw_payload, dedup_key):
        dispatched.append({"payload": payload, "dedup_key": dedup_key, "raw": raw_payload})
        return {"status": "accepted", "signal_id": "sig_lw", "model": "strategy"}

    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", fake_cancel)
    monkeypatch.setattr(limit_watch, "_dispatch", fake_dispatch)

    await limit_watch.handle_account_orders("nd_a", [_order()])

    assert cancelled == [("nd_a", 1001)]
    assert len(dispatched) == 1
    ids = dispatched[0]["payload"]["group_ids"]
    assert set(ids) == {g1["group_id"], g2["group_id"]}
    assert dispatched[0]["payload"]["limit_price"] == 2390.0
    assert dispatched[0]["payload"]["sl"] == 2380.0
    assert dispatched[0]["dedup_key"] == "nd_a:1001"
    assert "1001" in dispatched[0]["raw"]


async def test_handle_rejects_incomplete_without_cancel(store, monkeypatch):
    sty = await _mk_strategy(store)
    await _mk_group(store, "缺止损组", ["nd_a"], strategy=sty)

    cancelled = []

    async def fake_cancel(*_a, **_k):
        cancelled.append(True)
        return {"success": True}

    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", fake_cancel)
    monkeypatch.setattr(
        limit_watch, "_dispatch",
        _async_raise(AssertionError("should not dispatch")),
    )

    await limit_watch.handle_account_orders("nd_a", [_order(sl=0)])
    assert cancelled == []


async def test_handle_skips_strategy_magic_orders(store, monkeypatch):
    sty = await _mk_strategy(store)
    await _mk_group(store, "魔术号组", ["nd_a"], strategy=sty)
    called = []
    monkeypatch.setattr(
        limit_watch, "cancel_pending_on_node",
        lambda *a, **k: called.append("cancel") or {"success": True},
    )
    await limit_watch.handle_account_orders("nd_a", [_order(magic=900000007)])
    assert called == []


async def test_handle_does_not_retry_same_ticket(store, monkeypatch):
    sty = await _mk_strategy(store)
    await _mk_group(store, "幂等组", ["nd_a"], strategy=sty)
    n = {"cancel": 0, "dispatch": 0}

    async def fake_cancel(*_a, **_k):
        n["cancel"] += 1
        return {"success": True, "ticket": 1001}

    async def fake_dispatch(*_a, **_k):
        n["dispatch"] += 1
        return {"status": "accepted", "signal_id": "sig_1"}

    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", fake_cancel)
    monkeypatch.setattr(limit_watch, "_dispatch", fake_dispatch)

    await limit_watch.handle_account_orders("nd_a", [_order()])
    await limit_watch.handle_account_orders("nd_a", [_order()])
    assert n == {"cancel": 1, "dispatch": 1}


async def test_handle_releases_lock_when_cancel_fails(store, monkeypatch):
    sty = await _mk_strategy(store)
    await _mk_group(store, "撤单失败组", ["nd_a"], strategy=sty)
    n = {"dispatch": 0}

    async def fake_cancel(*_a, **_k):
        return {"success": False, "error": "节点离线"}

    async def fake_dispatch(*_a, **_k):
        n["dispatch"] += 1
        return {"status": "accepted"}

    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", fake_cancel)
    monkeypatch.setattr(limit_watch, "_dispatch", fake_dispatch)

    await limit_watch.handle_account_orders("nd_a", [_order()])
    assert n["dispatch"] == 0

    async def ok_cancel(*_a, **_k):
        return {"success": True, "ticket": 1001}

    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", ok_cancel)
    await limit_watch.handle_account_orders("nd_a", [_order()])
    assert n["dispatch"] == 1


async def test_create_group_rejects_watch_on_non_trend(store):
    sty = await _mk_strategy(
        store, template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME, rules=[],
    )
    with pytest.raises(ValueError, match="趋势策略"):
        await group_service.create_group(
            store,
            GroupCreate(
                name="非趋势监听", strategy_id=sty["strategy_id"],
                limit_watch_enabled=True, node_ids=[],
            ),
        )


async def test_unbinding_trend_strategy_turns_watch_off(store):
    sty = await _mk_strategy(store)
    g = await _mk_group(store, "待换绑组", [], strategy=sty, watch=True)
    assert g["limit_watch_enabled"] is True
    other = await _mk_strategy(
        store, template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME, rules=[],
    )
    updated = await group_service.update_group(
        store, g["group_id"], GroupUpdate(strategy_id=other["strategy_id"]),
    )
    assert updated["limit_watch_enabled"] is False
    assert updated["limit_watch_keyword"] == "limit"
