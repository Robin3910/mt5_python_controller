"""「暂停接单」在**分发层**的保证。

暂停的核心语义是第 ③ 条：**之后触发的信号只发主节点**，不再下发给跟单节点。
这与【交易】界面把「带单」开关关掉时的行为同构，但这里是服务端持久状态，
所以 webhook / 限价监听等非前端发起的信号同样必须被收窄。

覆盖三条红线：
- 暂停带单（tpl_2 / tpl_3）-> 跟单节点不再收到信号，只发主节点；
- 暂停 AI（tpl_1）-> AI 跟单节点不再收到信号，只发主节点；
- 两个市场**互不串台**：暂停一边不影响另一边。

这些用例是本次改造的**回归红线**：任何一个变红都说明市场隔离写错了。
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
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    NodeGroup,
    NodeGroupMember,
    Role,
    SignalDispatch,
    SignalHistory,
    TradingStrategy,
    User,
    UserRole,
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
    """建带单员名下的节点并置为在线；第一个是主节点。"""
    await online(store, *[
        mk_node(nid, owner_user_id=leader_id, is_primary=(i == 0))
        for i, nid in enumerate(node_ids)
    ])


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", name=None, rules=None,
                      template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME,
                      owner_user_id=None):
    sid = f"sty_p{next(_strategy_seq)}"
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
                symbol=row["symbol"],
                enabled=True,
                config_json=row["rules"],
                owner_user_id=owner_user_id,
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def mk_leader(store, *, follow_paused=False, ai_follow_paused=False):
    """建一个带单员用户（持 follow_leader 能力）+ 角色，返回 (user_id, username)。

    分发层要查分组所有者的能力与暂停状态才知道跟单节点能不能收信号，
    所以这里必须有一个真实的用户，而不是管理员名下（管理员按不可带单处理）。
    """
    async with SessionLocal() as s:
        role = Role(code="mk_leader_p", name="带单员", capabilities='["follow_leader"]')
        s.add(role)
        await s.flush()
        role_id = role.id
        user = User(
            username="mkleaderp", display_name="带单员", role="user",
            is_active=True, password_hash="x",
            follow_paused=follow_paused, ai_follow_paused=ai_follow_paused,
        )
        s.add(user)
        await s.flush()
        user_id = user.id
        s.add(UserRole(user_id=user_id, role_id=role_id))
        await s.commit()
    return user_id, "mkleaderp"


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


async def fetch_dispatched_nodes(signal_id):
    """该信号实际下发给哪些节点（从子任务派发表读，权威口径）。"""
    async with SessionLocal() as s:
        tasks = (
            await s.execute(
                select(GroupSignalTask)
                .where(GroupSignalTask.signal_id == signal_id)
                .order_by(GroupSignalTask.task_id.asc())
            )
        ).scalars().all()
        out = []
        for t in tasks:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.task_id == t.task_id,
                    )
                )
            ).scalars().all()
            out.extend(r.node_id for r in rows)
        return out


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


# ---------------------------------------------------------------------------
# 语义 ③：暂停后新信号只发主节点
# ---------------------------------------------------------------------------
async def test_paused_follow_market_dispatches_only_primary(store, monkeypatch):
    """暂停带单市场后：跟单节点**不**收到信号，主节点照常收到。"""
    leader_id, _ = await mk_leader(store, follow_paused=True)
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
        "sig_pause_follow",
    )

    assert res["mode"] == "group"
    targets = {s[0] for s in sent}
    assert "nd_f" not in targets, "暂停后跟单节点不得收到信号"
    assert "nd_leader" in targets, "暂停后主节点仍应收信号"
    dispatched = await fetch_dispatched_nodes("sig_pause_follow")
    assert "nd_f" not in dispatched, "暂停后不得为跟单节点建子任务"


async def test_paused_ai_market_dispatches_only_primary(store, monkeypatch):
    """暂停 AI 市场后：AI 跟单节点**不**收到信号，主节点照常收到。"""
    leader_id, _ = await mk_leader(store, ai_follow_paused=True)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(store, "AI组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_pause_ai",
    )

    assert res["mode"] == "group"
    targets = {s[0] for s in sent}
    assert "nd_f" not in targets, "暂停 AI 后跟单节点不得收到信号"
    assert "nd_leader" in targets, "暂停 AI 后主节点仍应收信号"


async def test_paused_grid_group_dispatches_only_primary(store, monkeypatch):
    """网格分组（tpl_3）同样受「暂停带单」约束 —— 两个模版都算带单市场。"""
    leader_id, _ = await mk_leader(store, follow_paused=True)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    grid = await _grid_strategy(store, leader_id)
    group = await mk_group(store, "网格组", ["nd_leader"], strategy=grid,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_pause_grid",
    )

    targets = {s[0] for s in sent}
    assert "nd_f" not in targets, "暂停后网格跟单节点不得收到信号"
    assert "nd_leader" in targets


async def test_not_paused_follower_still_receives_signal(store, monkeypatch):
    """未暂停时跟单节点照常收信号（回归：暂停逻辑没把正常路径打死）。"""
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    trend = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "趋势组", ["nd_leader"], strategy=trend,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0),
        "sig_not_paused",
    )

    targets = {s[0] for s in sent}
    assert "nd_f" in targets, "未暂停时跟单节点应收信号"
    assert "nd_leader" in targets


# ---------------------------------------------------------------------------
# 两个市场互不串台
# ---------------------------------------------------------------------------
async def test_pause_follow_does_not_block_ai_group(store, monkeypatch):
    """暂停带单**不**影响 AI 分组：AI 跟单节点照常收信号。"""
    leader_id, _ = await mk_leader(store, follow_paused=True)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(store, "AI组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_cross_1",
    )

    targets = {s[0] for s in sent}
    assert "nd_f" in targets, "暂停带单不应影响 AI 分组的跟单节点"


async def test_pause_ai_does_not_block_follow_market_group(store, monkeypatch):
    """暂停 AI **不**影响带单分组：带单跟单节点照常收信号。"""
    leader_id, _ = await mk_leader(store, ai_follow_paused=True)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    trend = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "趋势组", ["nd_leader"], strategy=trend,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0),
        "sig_cross_2",
    )

    targets = {s[0] for s in sent}
    assert "nd_f" in targets, "暂停 AI 不应影响带单分组的跟单节点"


# ---------------------------------------------------------------------------
# 语义 ②：已有跟单关系不动（暂停不踢人）
# ---------------------------------------------------------------------------
async def test_pause_keeps_follow_relationship(store):
    """暂停**不**删除已有跟单关系（等跟单员自行移出）。"""
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "nd_leader")
    await online(store, mk_node("nd_f"))
    trend = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "趋势组", ["nd_leader"], strategy=trend,
                           owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "nd_f")

    async with SessionLocal() as s:
        assert (
            await s.execute(
                select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == group["group_id"],
                )
            )
        ).scalars().all(), "前置：跟单关系已建立"

    # 暂停
    async with SessionLocal() as s:
        row = await s.get(User, leader_id)
        row.follow_paused = True
        await s.commit()

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == group["group_id"],
                )
            )
        ).scalars().all()
        assert rows, "暂停不应删除已有跟单关系"
