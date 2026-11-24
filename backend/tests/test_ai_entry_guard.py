"""AI 开仓准入闸门（tpl_1）测试。

覆盖两层：
- **纯函数层**（`ai_entry_guard` / `rules.interval_decision`）：无 IO，纯判定语义；
- **接入层**（`group_dispatcher`）：L1 闸门在分组分发链路上的实际行为
  （被拦分组不建主任务、auto 路由同样被拦、tpl_2/tpl_3 不受影响、CLOSE 不受影响）。
"""
import asyncio

import fakeredis
import pytest
from sqlalchemy import delete, select

from app import ai_entry_guard, group_service
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.models import GroupCreate
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
from app.parser import TradingSignal
from app.redis_store import RedisStore
from app.signal_codes import REJECT_AI_ENTRY, SKIP_AI_ENTRY
from app.strategy_templates import (
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


@pytest.fixture
async def store():
    await init_db()
    async with SessionLocal() as s:
        for table in _TABLES:
            await s.execute(delete(table))
        await s.commit()
    return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture(autouse=True)
def clean_manager():
    manager.nodes.clear()
    manager.admins.clear()
    yield
    manager.nodes.clear()
    manager.admins.clear()


def mk_node(node_id, *, enabled=True, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": enabled,
        "mt5_login": abs(hash(node_id)) % 100000,
        "filters": None,
        "created_at": 0,
    }
    d.update(kw)
    return d


async def online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        manager.nodes[n["node_id"]] = object()


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", enabled=True, rules=None,
                      template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME,
                      owner_user_id=None):
    sid = f"sty_t{next(_strategy_seq)}"
    row = {
        "strategy_id": sid,
        "name": f"策略{sid}",
        "template_id": template_id,
        "template_name": template_name,
        "symbol": symbol,
        "enabled": enabled,
        "rules": rules if rules is not None else [],
        "owner_user_id": owner_user_id,
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
                owner_user_id=owner_user_id,
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def mk_group(store, name, node_ids, *, mode="sync", enabled=True,
                   symbol="XAUUSD", strategy=None, owner_user_id=None):
    strategy = strategy or await mk_strategy(store, symbol=symbol)
    return await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=enabled, dispatch_mode=mode,
            signal_concurrent_enabled=False,
            strategy_id=strategy["strategy_id"], node_ids=list(node_ids),
        ),
        owner_user_id=owner_user_id,
    )


def capture_sender(sent, *, ok=True):
    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return ok

    return fake_send


async def fetch_tasks(signal_id):
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(GroupSignalTask)
                .where(GroupSignalTask.signal_id == signal_id)
                .order_by(GroupSignalTask.task_id.asc())
            )
        ).scalars().all()


async def fetch_signal(signal_id):
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(SignalHistory).where(SignalHistory.signal_id == signal_id)
            )
        ).scalars().first()


def ai_cfg(**kw):
    """构造一份启用的 ai_entry 配置。"""
    base = {
        "enabled": True,
        "allow_buy": True,
        "allow_sell": False,
        "default_action": "block",
        "intervals": [{"low": 4000, "high": 4200, "allow": ["BUY"]}],
    }
    base.update(kw)
    return base


async def set_filters(store, cfg: dict):
    await store.set_filters(cfg)


# =====================================================================
# 1. 纯函数：归一化
# =====================================================================
def test_normalize_missing_ai_entry_is_disabled():
    cfg = ai_entry_guard.normalize_ai_entry(None)
    assert cfg["enabled"] is False
    assert cfg["intervals"] == []


def test_normalize_non_dict_is_disabled():
    cfg = ai_entry_guard.normalize_ai_entry("x")
    assert cfg["enabled"] is False


def test_normalize_enabled_requires_true():
    """`enabled` 缺失 -> False（存量配置零行为变化）。"""
    assert ai_entry_guard.normalize_ai_entry({})["enabled"] is False
    assert ai_entry_guard.normalize_ai_entry({"enabled": 1})["enabled"] is False
    assert ai_entry_guard.normalize_ai_entry({"enabled": True})["enabled"] is True


def test_normalize_illegal_default_action_falls_back_to_block():
    cfg = ai_entry_guard.normalize_ai_entry({"enabled": True, "default_action": "???"})
    assert cfg["default_action"] == "block"


def test_normalize_drops_illegal_intervals_only():
    """非法区间被丢弃，其它区间与整个配置不受影响。"""
    cfg = ai_entry_guard.normalize_ai_entry({
        "enabled": True,
        "intervals": [
            {"low": 4000, "high": 4200, "allow": ["BUY"]},   # 合法
            {"low": 4200, "high": 4000, "allow": ["BUY"]},   # low >= high
            {"low": 100, "high": 200, "allow": []},          # allow 为空
            {"low": "x", "high": 200, "allow": ["BUY"]},     # 非数字
            "not-a-dict",                                    # 格式非法
        ],
    })
    assert len(cfg["intervals"]) == 1
    assert cfg["intervals"][0] == {"low": 4000.0, "high": 4200.0, "allow": ["BUY"]}


def test_normalize_direction_case_insensitive_and_dedup():
    cfg = ai_entry_guard.normalize_ai_entry({
        "enabled": True,
        "intervals": [{"low": 1, "high": 2, "allow": ["buy", "BUY", "sell"]}],
    })
    assert cfg["intervals"][0]["allow"] == ["BUY", "SELL"]


def test_lookup_ai_entry_from_filters():
    filters = {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}}
    cfg = ai_entry_guard.lookup_ai_entry(filters, "XAUUSD")
    assert cfg["enabled"] is True
    assert len(cfg["intervals"]) == 1


def test_lookup_ai_entry_missing_symbol_is_disabled():
    cfg = ai_entry_guard.lookup_ai_entry({"XAUUSD": {"ai_entry": ai_cfg()}}, "EURUSD")
    assert cfg["enabled"] is False


# =====================================================================
# 2. 纯函数：判定
# =====================================================================
def test_disabled_config_passes_everything():
    cfg = ai_entry_guard.normalize_ai_entry({"enabled": False, "allow_sell": False})
    assert ai_entry_guard.evaluate("SELL", "XAUUSD", 4100, cfg) == (True, None)


def test_missing_price_passes_when_disabled():
    """关键回归点：未启用时缺价完全不影响任何信号。"""
    cfg = ai_entry_guard.normalize_ai_entry({"enabled": False})
    ok, reason = ai_entry_guard.evaluate("BUY", "XAUUSD", None, cfg)
    assert ok is True
    assert reason is None


def test_missing_price_is_blocked_when_enabled():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    ok, reason = ai_entry_guard.evaluate("BUY", "XAUUSD", None, cfg)
    assert ok is False
    assert "无可用价格" in reason
    assert "AI 开仓准入" in reason


def test_direction_switch_blocks_sell():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    ok, reason = ai_entry_guard.evaluate("SELL", "XAUUSD", 4100, cfg)
    assert ok is False
    assert "方向总开关" in reason


def test_direction_switch_blocks_buy():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg(allow_buy=False, allow_sell=True))
    ok, reason = ai_entry_guard.evaluate("BUY", "XAUUSD", 4100, cfg)
    assert ok is False
    assert "方向总开关" in reason


def test_buy_inside_interval_passes():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    assert ai_entry_guard.evaluate("BUY", "XAUUSD", 4100, cfg) == (True, None)


def test_sell_inside_buy_only_interval_is_blocked():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg(allow_sell=True))
    ok, reason = ai_entry_guard.evaluate("SELL", "XAUUSD", 4100, cfg)
    assert ok is False
    assert "命中区间" in reason


def test_buy_outside_interval_is_blocked_by_default():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    ok, reason = ai_entry_guard.evaluate("BUY", "XAUUSD", 4300, cfg)
    assert ok is False
    assert "不在任何配置区间内" in reason


def test_buy_outside_interval_passes_when_default_pass():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg(default_action="pass"))
    assert ai_entry_guard.evaluate("BUY", "XAUUSD", 4300, cfg) == (True, None)


def test_close_is_never_filtered():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    assert ai_entry_guard.evaluate("CLOSE", "XAUUSD", 4100, cfg) == (True, None)
    assert ai_entry_guard.evaluate("CLOSE", "XAUUSD", None, cfg) == (True, None)


def test_interval_boundaries_are_closed():
    cfg = ai_entry_guard.normalize_ai_entry(ai_cfg())
    assert ai_entry_guard.evaluate("BUY", "XAUUSD", 4000, cfg) == (True, None)
    assert ai_entry_guard.evaluate("BUY", "XAUUSD", 4200, cfg) == (True, None)


def test_first_matching_interval_wins():
    """多区间重叠：首个命中即定。"""
    cfg = ai_entry_guard.normalize_ai_entry({
        "enabled": True, "allow_buy": True, "allow_sell": True,
        "default_action": "pass",
        "intervals": [
            {"low": 4000, "high": 4200, "allow": ["SELL"]},  # 先命中：只允许 SELL
            {"low": 4100, "high": 4300, "allow": ["BUY"]},
        ],
    })
    ok, reason = ai_entry_guard.evaluate("BUY", "XAUUSD", 4150, cfg)
    assert ok is False
    assert "命中区间" in reason


# =====================================================================
# 3. 纯函数：取价
# =====================================================================
def test_signal_price_takes_priority():
    sig = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, entry_price=4150.0)
    price, src = ai_entry_guard.resolve_price(sig, {}, {}, {})
    assert price == 4150.0
    assert "信号自带价" in src


def test_signal_price_ignores_non_positive():
    sig = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, entry_price=0)
    price, src = ai_entry_guard.resolve_price(sig, {}, {}, {})
    assert price is None
    assert "无可用价格" in src


def test_resolve_price_uses_primary_node_snapshot():
    group = {"members": [{"node_id": "nd_a", "sort_order": 0}]}
    nodemap = {"nd_a": {"node_id": "nd_a", "enabled": True, "is_primary": True}}
    accounts = {"nd_a": {"prices": {"XAUUSD": 4100.0}}}
    sig = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)
    price, src = ai_entry_guard.resolve_price(sig, group, nodemap, accounts, {"nd_a"})
    assert price == 4100.0
    assert "主节点" in src


def test_resolve_price_falls_back_to_online_member():
    group = {"members": [{"node_id": "nd_a", "sort_order": 0}]}
    nodemap = {"nd_a": {"node_id": "nd_a", "enabled": True}}
    accounts = {"nd_a": {"prices": {"XAUUSD": 4050.0}}}
    sig = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)
    price, src = ai_entry_guard.resolve_price(sig, group, nodemap, accounts, {"nd_a"})
    assert price == 4050.0
    assert "在线成员节点" in src


def test_resolve_price_ignores_follower_nodes():
    """跟单成员的报价不参与（跟单员的券商报价不代表带单方的市场）。"""
    group = {
        "members": [{"node_id": "nd_a", "sort_order": 0}],
        "followers": [{"node_id": "nd_f", "sort_order": 0}],
    }
    nodemap = {
        "nd_a": {"node_id": "nd_a", "enabled": True},
        "nd_f": {"node_id": "nd_f", "enabled": True, "is_primary": True},
    }
    accounts = {"nd_f": {"prices": {"XAUUSD": 4100.0}}}
    sig = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)
    price, _src = ai_entry_guard.resolve_price(sig, group, nodemap, accounts, {"nd_f"})
    assert price is None


# =====================================================================
# 4. 纯函数：配置校验
# =====================================================================
def test_validate_accepts_valid_config():
    filters = {"XAUUSD": {"ai_entry": ai_cfg()}}
    assert ai_entry_guard.validate_ai_entry_config(filters) is None


def test_validate_accepts_missing_ai_entry():
    assert ai_entry_guard.validate_ai_entry_config({"XAUUSD": {"enabled": True}}) is None


def test_validate_rejects_bad_interval_order():
    filters = {"XAUUSD": {"ai_entry": ai_cfg(
        intervals=[{"low": 4200, "high": 4000, "allow": ["BUY"]}],
    )}}
    err = ai_entry_guard.validate_ai_entry_config(filters)
    assert err and "下限必须小于上限" in err
    assert "XAUUSD" in err


def test_validate_rejects_empty_allow():
    filters = {"XAUUSD": {"ai_entry": ai_cfg(
        intervals=[{"low": 4000, "high": 4200, "allow": []}],
    )}}
    err = ai_entry_guard.validate_ai_entry_config(filters)
    assert err and "至少勾选一个允许方向" in err


def test_validate_rejects_bad_default_action():
    filters = {"XAUUSD": {"ai_entry": ai_cfg(default_action="nope")}}
    err = ai_entry_guard.validate_ai_entry_config(filters)
    assert err and "默认动作非法" in err


def test_validate_rejects_bad_direction():
    filters = {"XAUUSD": {"ai_entry": ai_cfg(
        intervals=[{"low": 4000, "high": 4200, "allow": ["HOLD"]}],
    )}}
    err = ai_entry_guard.validate_ai_entry_config(filters)
    assert err and "允许方向非法" in err


# =====================================================================
# 5. 接入层：L1 闸门在分组分发链路上的行为
# =====================================================================
async def test_sell_signal_is_dropped_by_admission(store, monkeypatch):
    """SELL 信号被 AI 开仓准入拦下：不建主任务、节点收不到指令。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "sig_ai_1",
    )

    assert res["targets"] == 0
    assert res["groups"] == 0
    assert res["mode"] == "rejected"
    assert sent == []
    # 被拦的分组不产生主任务（信号中心任务列表不被污染）
    assert await fetch_tasks("sig_ai_1") == []
    # 信号仍然入库，并带 AI 开仓准入的拒收编码
    row = await fetch_signal("sig_ai_1")
    assert row is not None
    assert row.reject_code == REJECT_AI_ENTRY
    assert row.status == "rejected"
    # 人读原因在响应里（signal_history 不存原因文本，只有结构化编码）
    assert "AI 开仓准入" in (res.get("reason") or "")
    assert res.get("filtered_groups")
    assert res["filtered_groups"][0]["code"] == REJECT_AI_ENTRY


async def test_buy_signal_inside_interval_is_dispatched(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, entry_price=4100.0),
        "sig_ai_2",
    )

    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]
    assert len(await fetch_tasks("sig_ai_2")) == 1


async def test_buy_signal_outside_interval_is_dropped(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, entry_price=4300.0),
        "sig_ai_3",
    )

    assert res["targets"] == 0
    assert sent == []
    assert await fetch_tasks("sig_ai_3") == []


async def test_missing_price_signal_is_dropped(store, monkeypatch):
    """缺价直接拦截。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_ai_4",
    )

    assert res["targets"] == 0
    assert sent == []
    row = await fetch_signal("sig_ai_4")
    assert row.reject_code == REJECT_AI_ENTRY


async def test_disabled_ai_entry_keeps_legacy_behavior(store, monkeypatch):
    """未启用 ai_entry 时行为与改动前完全一致（存量零影响）。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "sig_ai_5",
    )

    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]


async def test_tpl2_group_is_not_affected(store, monkeypatch):
    """tpl_2 分组完全不受 ai_entry 影响。"""
    await online(store, mk_node("nd_a"))
    strategy = await mk_strategy(store, template_id=TEMPLATE_2_ID, template_name=TEMPLATE_2_NAME)
    await mk_group(store, "带单组", ["nd_a"], strategy=strategy)
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "sig_ai_6",
    )

    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]


async def test_close_signal_is_not_filtered(store, monkeypatch):
    """CLOSE 不参与准入过滤。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "AI组", ["nd_a"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    # CLOSE 找不到活动任务时会被拒收，但**不会**被 AI 准入拦下 ——
    # 落选原因里不应出现「AI 开仓准入」。
    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.0), "sig_ai_7",
    )

    assert "AI 开仓准入" not in (res.get("reason") or "")


async def test_partial_groups_blocked_keeps_others(store, monkeypatch):
    """部分分组被拦 -> 放行的分组正常下发。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "AI组一", ["nd_a"])
    await mk_group(store, "AI组二", ["nd_b"])
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    # 信号自带价在区间内 -> 两个分组都放行
    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, entry_price=4100.0),
        "sig_ai_8",
    )
    assert res["targets"] == 2
    assert len(await fetch_tasks("sig_ai_8")) == 2


async def test_auto_route_is_also_blocked(store, monkeypatch):
    """auto 路由同样被拦截，且规则命中统计照常记录（不因准入拦下而失真）。"""
    from app import signal_route_rules

    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "auto组", ["nd_a"])
    # 把分组切成 auto 模式
    group["signal_source"] = "auto"
    await store.cache_group(group)

    await signal_route_rules.create_rule({
        "name": "自动组规则",
        "signal_types": ["ob"],
        "group_ids": [group["group_id"]],
    })
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1,
                      signal_type="ob", entry_price=4100.0),
        "sig_ai_9",
    )

    # 规则命中了（auto_groups 里有这个分组），但被 AI 准入拦下 -> 绝不下发
    assert group["group_id"] in (res.get("auto_groups") or [])
    assert res["targets"] == 0
    assert sent == []
    assert await fetch_tasks("sig_ai_9") == []
    assert res.get("filtered_groups")
    assert res["filtered_groups"][0]["group_id"] == group["group_id"]

    # 规则命中统计照常记录（顺序：规则匹配 -> 准入 -> 下发）
    rules = await signal_route_rules.list_rules()
    mine = [r for r in rules if group["group_id"] in (r.get("group_ids") or [])]
    assert mine, "应存在指向该分组的规则"
    hits = await store.get_route_rule_hits([r["rule_id"] for r in mine])
    assert sum(h["count"] for h in hits.values()) == 1


async def test_auto_route_allowed_signal_is_dispatched(store, monkeypatch):
    """auto 分组 + 规则命中 + 准入通过 -> 正常下发。"""
    from app import signal_route_rules

    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "auto组2", ["nd_a"])
    group["signal_source"] = "auto"
    await store.cache_group(group)

    await signal_route_rules.create_rule({
        "name": "自动组规则2",
        "signal_types": ["ob"],
        "group_ids": [group["group_id"]],
    })
    await set_filters(store, {"XAUUSD": {"enabled": True, "ai_entry": ai_cfg()}})
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1,
                      signal_type="ob", entry_price=4100.0),
        "sig_ai_10",
    )

    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]
    assert len(await fetch_tasks("sig_ai_10")) == 1
