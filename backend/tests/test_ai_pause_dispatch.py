"""AI 暂停拦截整组新开仓，保留在跑任务、占位及平仓能力。"""
import pytest
from sqlalchemy import select

from app import group_service, user_service
from app.connections import manager
from app.db import SessionLocal
from app.group_dispatcher import GroupDispatcher
from app.models import GroupCreate
from app.orm import GroupSignalTask, GroupTaskDispatch
from app.parser import TradingSignal
from tests.test_follow_pause_dispatch import (
    _add_follower,
    _ai_strategy,
    capture_sender,
    fetch_dispatched_nodes,
    leader_nodes,
    mk_group,
    mk_leader,
    mk_node,
    online,
    store,
)


@pytest.mark.parametrize("action", ["BUY", "SELL"])
@pytest.mark.parametrize("mode", ["sync", "poll"])
@pytest.mark.parametrize("with_followers", [False, True])
async def test_ai_pause_blocks_all_nodes(store, monkeypatch, action, mode, with_followers):
    leader_id, _ = await mk_leader(store, ai_follow_paused=True)
    await leader_nodes(store, leader_id, "primary", "owner-extra")
    strategy = await _ai_strategy(store, leader_id)
    group = await group_service.create_group(
        store,
        GroupCreate(
            name="paused-ai", node_ids=["primary", "owner-extra"],
            strategy_id=strategy["strategy_id"], dispatch_mode=mode,
        ),
        owner_user_id=leader_id,
    )
    if with_followers:
        await online(store, mk_node("follower"))
        await _add_follower(store, group["group_id"], "follower")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    result = await GroupDispatcher(store).dispatch(
        TradingSignal(action=action, symbol="XAUUSD", volume=0.1), "paused-ai-signal",
    )

    assert sent == []
    assert result["targets"] == 0
    assert result["tasks"][0]["status"] == "skipped"
    assert "所有节点" in result["tasks"][0]["reason"]
    assert "主节点当前不可用" not in result["tasks"][0]["reason"]
    assert await fetch_dispatched_nodes("paused-ai-signal") == []
    for node_id in ("primary", "owner-extra", "follower"):
        assert await store.get_group_node_busy(group["group_id"], node_id) is None
    async with SessionLocal() as session:
        task = await session.scalar(
            select(GroupSignalTask).where(GroupSignalTask.signal_id == "paused-ai-signal"),
        )
        assert task.skip_code == "admission_rule"


async def test_ai_pause_blocks_owned_group_without_primary(store, monkeypatch):
    leader_id, _ = await mk_leader(store, ai_follow_paused=True)
    await leader_nodes(store, leader_id, "primary", "owner-extra")
    strategy = await _ai_strategy(store, leader_id)
    await mk_group(
        store, "secondary-only", ["owner-extra"], strategy=strategy,
        owner_user_id=leader_id,
    )
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    result = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "paused-secondary",
    )

    assert result["targets"] == 0
    assert sent == []


async def test_ai_pause_resume_reloads_persisted_state(store, monkeypatch):
    leader_id, _ = await mk_leader(store, ai_follow_paused=True)
    await leader_nodes(store, leader_id, "primary", "owner-extra")
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(
        store, "resumed-ai", ["primary", "owner-extra"], strategy=strategy,
        owner_user_id=leader_id,
    )
    await online(store, mk_node("follower"))
    await _add_follower(store, group["group_id"], "follower")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    paused = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "before-resume",
    )
    assert paused["targets"] == 0
    await user_service.set_ai_follow_paused(leader_id, False)
    resumed = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "after-resume",
    )

    assert resumed["targets"] == 3
    assert {node_id for node_id, command in sent} == {"primary", "owner-extra", "follower"}


async def test_ai_pause_preserves_running_tasks_and_close(store, monkeypatch):
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "primary", "owner-extra")
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(
        store, "running-ai", ["primary", "owner-extra"], strategy=strategy,
        owner_user_id=leader_id,
    )
    await online(store, mk_node("follower"))
    await _add_follower(store, group["group_id"], "follower")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)
    started = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "running-before-pause",
    )
    assert started["targets"] == 3
    node_ids = [node_id for node_id, command in sent]
    busy_before = {
        node_id: await store.get_group_node_busy(group["group_id"], node_id)
        for node_id in node_ids
    }
    async with SessionLocal() as session:
        rows = (await session.scalars(select(GroupTaskDispatch))).all()
        running_before = {row.id: (row.status, row.magic) for row in rows}

    await user_service.set_ai_follow_paused(leader_id, True)
    sent.clear()
    blocked = await dispatcher.dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "blocked-while-running",
    )
    assert blocked["targets"] == 0
    assert sent == []
    for node_id, busy in busy_before.items():
        assert busy is not None
        assert await store.get_group_node_busy(group["group_id"], node_id) == busy
    async with SessionLocal() as session:
        rows = (await session.scalars(select(GroupTaskDispatch))).all()
        assert {row.id: (row.status, row.magic) for row in rows} == running_before

    closed = await dispatcher.dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0, group_ids=[group["group_id"]]),
        "close-paused-ai",
    )
    assert closed["mode"] == "group_close"
    assert closed["targets"] == 3
    assert {node_id for node_id, command in sent} == set(node_ids)
    assert all(command["cmd"] == "strategy_stop" for node_id, command in sent)
