"""跟单市场隔离在**分发层**的保证。

两个市场共用同一张关系表、同一套分发链路，靠「分组绑定的策略模版」天然互斥：

- 带单市场（tpl_2 趋势 / tpl_3 网格）：【带单】Tab 的跟单员应正常收到信号；
- AI 市场（tpl_1 AI 智能加仓）：【AI 交易】Tab 的跟单员**也**应正常收到信号
  —— 这是 AI 跟单能落地的前提，绝不能被市场过滤误伤；
- 模版未知（未绑策略 / 策略已删）：两个市场都不收，跟单节点**不**产生子任务（保守）。

这三个用例是本次改造的**回归红线**：任何一个变绿都说明市场隔离写错了。
"""

import pytest
from sqlalchemy import delete, select

from app import follow_service, group_service
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.models import GroupCreate
from app.orm import (
    GroupFollowerNode,
    Role,
    RoleMenu,
    User,
    UserRole,
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
from app.strategy_templates import (
    RULE_TYPE_GRID,
    RULE_TYPE_RISK_SIZED,
    TEMPLATE_1_ID,
    TEMPLATE_1_NAME,
    TEMPLATE_2_ID,
    TEMPLATE_2_NAME,
    TEMPLATE_3_ID,
    TEMPLATE_3_NAME,
)

_TABLES = (
    GroupTaskEvent,
    GroupTaskDispatch,
    GroupSignalTask,
    GroupFollowerNode,
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
        await s.execute(delete(RoleMenu))
        await s.execute(delete(UserRole))
        await s.execute(delete(Role))
        await s.execute(delete(User))
        await s.commit()
    import fakeredis

    st = RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    manager.nodes.clear()
    manager.admins.clear()
    yield st
    manager.nodes.clear()
    manager.admins.clear()


def mk_node(node_id, *, enabled=True, owner_user_id=None, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": enabled,
        "mt5_login": abs(hash(node_id)) % 100000,
        "filters": None,
        "created_at": 0,
        "owner_user_id": owner_user_id,
    }
    d.update(kw)
    return d


async def online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        manager.nodes[n["node_id"]] = object()


async def leader_nodes(store, leader_id, *node_ids):
    """建带单员名下的节点并置为在线。"""
    await online(store, *[
        mk_node(nid, owner_user_id=leader_id) for nid in node_ids
    ])


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", name=None, rules=None,
                      template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME,
                      owner_user_id=None):
    sid = f"sty_m{next(_strategy_seq)}"
    row = {
        "strategy_id": sid,
        "name": name or f"策略{sid}",
        "template_id": template_id,
        "template_name": template_name,
        "symbol": symbol,
        "enabled": True,
        "rules": rules if rules is not None else [],
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
                enabled=True,
                config_json=row["rules"],
                owner_user_id=owner_user_id,
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def mk_leader(store):
    """建一个带单员用户（持 follow_leader 能力）+ 角色，返回 (user_id, username)。

    分发层要查分组所有者的能力才知道跟单节点能不能收信号，
    所以这里必须有一个真实的用户，而不是管理员名下（管理员按不可带单处理）。
    """
    async with SessionLocal() as s:
        role = Role(code="mk_leader", name="带单员", capabilities='["follow_leader"]')
        s.add(role)
        await s.flush()
        role_id = role.id
        user = User(username="mkleader", display_name="带单员", role="user",
                    is_active=True, password_hash="x")
        s.add(user)
        await s.flush()
        user_id = user.id
        s.add(UserRole(user_id=user_id, role_id=role_id))
        await s.commit()
    return user_id, "mkleader"


async def mk_group(store, name, node_ids, *, symbol="XAUUSD", strategy=None,
                   owner_user_id=None):
    strategy = strategy or await mk_strategy(store, symbol=symbol)
    return await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=True, dispatch_mode="sync",
            strategy_id=strategy["strategy_id"], node_ids=list(node_ids),
        ),
        owner_user_id=owner_user_id,
    )


def capture_sender(sent):
    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return True

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


async def _ai_strategy(store, owner_user_id=None):
    return await mk_strategy(
        store, symbol="XAUUSD", name="AI加仓策略", rules=[],
        template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME,
        owner_user_id=owner_user_id,
    )


async def _risk_sized_strategy(store, owner_user_id=None):
    rule = {
        "type": RULE_TYPE_RISK_SIZED, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0,
    }
    return await mk_strategy(
        store, symbol="XAUUSD", name="趋势策略", rules=[rule],
        template_id=TEMPLATE_2_ID, template_name=TEMPLATE_2_NAME,
        owner_user_id=owner_user_id,
    )


async def _grid_strategy(store, owner_user_id=None):
    rule = {
        "type": RULE_TYPE_GRID, "status": 1, "action": "all",
        "price_lower": 2300.0, "price_upper": 2400.0,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
        "trailing_up": False, "trailing_max": 0,
    }
    return await mk_strategy(
        store, symbol="XAUUSD", name="网格策略", rules=[rule],
        template_id=TEMPLATE_3_ID, template_name=TEMPLATE_3_NAME,
        owner_user_id=owner_user_id,
    )


async def _add_follower(store, group_id, node_id, *, leader_user_id=7,
                        leader_primary_node_id="nd_leader"):
    await follow_service.add_following(
        store,
        group_id=group_id, node_id=node_id,
        follower_user_id=99, leader_user_id=leader_user_id,
        leader_primary_node_id=leader_primary_node_id,
    )


async def test_ai_group_follower_receives_signal(store, monkeypatch):
    """AI 分组（tpl_1）的跟单节点**正常**产生子任务 —— AI 跟单能落地的前提。"""
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(store, "AI组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_ai_follow",
    )

    assert res["mode"] == "group"
    assert "nd_f" in {s[0] for s in sent}, "AI 跟单节点应收 AI 信号"
    tasks = await fetch_tasks("sig_ai_follow")
    assert tasks, "AI 跟单应产生子任务"


async def test_follow_market_group_follower_receives_signal(store, monkeypatch):
    """带单市场（tpl_2 趋势 / tpl_3 网格）的跟单节点**正常**产生子任务（回归）。"""
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    trend = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "趋势组", ["nd_leader"], strategy=trend,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0),
        "sig_follow_market",
    )

    assert res["mode"] == "group"
    assert "nd_f" in {s[0] for s in sent}, "带单跟单节点应收带单信号"
    assert await fetch_tasks("sig_follow_market")


async def test_grid_group_follower_receives_signal(store, monkeypatch):
    """带单市场（tpl_3 网格）的跟单节点**正常**产生子任务（回归）。"""
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    grid = await _grid_strategy(store, leader_id)
    group = await mk_group(store, "网格组", ["nd_leader"], strategy=grid,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_grid_market",
    )

    assert res["mode"] == "group"
    assert "nd_f" in {s[0] for s in sent}, "网格跟单节点应收网格信号"
    assert await fetch_tasks("sig_grid_market")


async def test_unknown_template_follower_gets_no_task(store, monkeypatch):
    """模版未知（策略模版不在任一市场）：跟单节点**不**被下发（保守，宁可漏发）。

    分组本身**能**正常分发（owner 成员照常收信号）—— 这才真正验证了
    「模版未知 -> 跟单节点被排除」这条分支，而不是碰巧整个分组没参与分发。
    """
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    # 模版 tpl_999 不属于任何市场（既不是 AI，也不是带单市场）
    unknown = await mk_strategy(
        store, symbol="XAUUSD", name="未知模版策略", rules=[],
        template_id="tpl_999", template_name="未知模版",
        owner_user_id=leader_id,
    )
    group = await mk_group(store, "未知模版组", ["nd_leader"], strategy=unknown,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_unknown_tpl",
    )

    assert res["mode"] == "group", "分组本身应参与分发"
    assert "nd_leader" in {s[0] for s in sent}, "owner 成员照常收信号"
    assert "nd_f" not in {s[0] for s in sent}, "模版未知时跟单节点不得收到信号"
    tasks = await fetch_tasks("sig_unknown_tpl")
    assert tasks, "应产生子任务（owner 成员）"
    for t in tasks:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.task_id == t.task_id,
                    )
                )
            ).scalars().all()
            for row in rows:
                assert row.node_id != "nd_f", "模版未知时跟单节点不得被下发"
