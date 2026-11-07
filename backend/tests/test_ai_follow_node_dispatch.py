"""子节点暂停只拦截该节点 AI 新开仓，普通带单和 CLOSE 保持可用。"""
import pytest
from sqlalchemy import select

from app import group_service
from app.connections import manager
from app.db import SessionLocal
from app.group_dispatcher import GroupDispatcher
from app.orm import GroupFollowerNode
from app.parser import TradingSignal
from tests.test_follow_pause_dispatch import (
    _add_follower, _ai_strategy, _risk_sized_strategy, capture_sender,
    fetch_dispatched_nodes, leader_nodes, mk_group, mk_leader, mk_node, online, store,
)


@pytest.mark.parametrize("follow_state", ["paused", "stopped"])
async def test_node_state_blocks_only_selected_ai_follower(store, monkeypatch, follow_state):
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "primary")
    await online(store, mk_node("controlled"), mk_node("peer"))
    strategy = await _ai_strategy(store, leader_id)
    group = await mk_group(store, "node-controlled-ai", ["primary"], strategy=strategy, owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "controlled")
    await _add_follower(store, group["group_id"], "peer")
    async with SessionLocal() as session:
        relation = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.node_id == "controlled"))
        relation.follow_state = follow_state
        await session.commit()
    await group_service.warm_cache(store)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    result = await GroupDispatcher(store).dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "controlled-ai")
    assert result["targets"] == 2
    assert {node_id for node_id, command in sent} == {"primary", "peer"}
    assert "controlled" not in await fetch_dispatched_nodes("controlled-ai")
    assert await store.get_group_node_busy(group["group_id"], "controlled") is None


async def test_node_pause_keeps_normal_market_and_existing_close(store, monkeypatch):
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "primary")
    await online(store, mk_node("controlled"))
    ai = await _ai_strategy(store, leader_id)
    ai_group = await mk_group(store, "ai-node", ["primary"], strategy=ai, owner_user_id=leader_id)
    await _add_follower(store, ai_group["group_id"], "controlled")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)
    opened = await dispatcher.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "before-node-pause")
    assert opened["targets"] == 2
    async with SessionLocal() as session:
        relation = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.group_id == ai_group["group_id"]))
        relation.follow_state = "paused"
        await session.commit()
    await group_service.warm_cache(store)
    trend = await _risk_sized_strategy(store, leader_id)
    trend_group = await mk_group(store, "normal-node", ["primary"], strategy=trend, owner_user_id=leader_id)
    await _add_follower(store, trend_group["group_id"], "controlled")
    sent.clear()
    await dispatcher.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2300.0), "after-node-pause")
    assert any(node_id == "controlled" and command["group_id"] == trend_group["group_id"] for node_id, command in sent)
    assert not any(node_id == "controlled" and command.get("group_id") == ai_group["group_id"] for node_id, command in sent)
    sent.clear()
    closed = await dispatcher.dispatch(TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0, group_ids=[ai_group["group_id"]]), "close-paused-node")
    assert closed["targets"] == 2
    assert {node_id for node_id, command in sent} == {"primary", "controlled"}
    assert all(command["cmd"] == "strategy_stop" for node_id, command in sent)
