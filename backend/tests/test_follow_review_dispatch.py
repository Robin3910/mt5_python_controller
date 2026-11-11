"""带单主节点跟单与频道暂停只影响新的开仓。"""
from sqlalchemy import select

import pytest

from app import follow_service, group_service, node_service
from app.connections import manager
from app.db import SessionLocal
from app.group_dispatcher import GroupDispatcher
from app.orm import GroupFollowerNode
from app.parser import TradingSignal
from tests.test_follow_pause_dispatch import (
    _add_follower, _risk_sized_strategy, capture_sender, leader_nodes,
    mk_group, mk_leader, mk_node, online, store,
)


async def test_primary_follower_pause_preserves_existing_close_and_busy(store, monkeypatch):
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "source")
    await online(store, mk_node("primary-follower", is_primary=True))
    strategy = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "primary-follow", ["source"], strategy=strategy, owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "primary-follower", leader_user_id=leader_id, leader_primary_node_id="source")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)
    await dispatcher.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=.1, stop_loss=2300), "approved-primary")
    assert {node_id for node_id, _ in sent} == {"source", "primary-follower"}
    busy = await store.get_group_node_busy(group["group_id"], "primary-follower")
    assert busy
    async with SessionLocal() as session:
        relation = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.group_id == group["group_id"]))
        relation.leader_paused = True
        await session.commit()
    await group_service.warm_cache(store)
    sent.clear()
    await dispatcher.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=.1, stop_loss=2300), "paused-primary")
    assert not any(node_id == "primary-follower" for node_id, _ in sent)
    assert await store.get_group_node_busy(group["group_id"], "primary-follower") == busy
    await follow_service.remove_following(store, group_id=group["group_id"], node_ids=["primary-follower"], follower_user_id=99)
    assert (await store.get_group(group["group_id"]))["followers"] == []
    assert await store.get_group_node_busy(group["group_id"], "primary-follower") == busy
    sent.clear()
    await dispatcher.dispatch(TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0, group_ids=[group["group_id"]]), "close-primary")
    assert {node_id for node_id, _ in sent} == {"source", "primary-follower"}
    assert all(command["cmd"] == "strategy_stop" for _, command in sent)


async def test_global_pause_still_limits_owner_when_followers_are_paused(store, monkeypatch):
    leader_id, _ = await mk_leader(store, follow_paused=True)
    await leader_nodes(store, leader_id, "source", "owner-extra")
    await online(store, mk_node("paused-follower"))
    strategy = await _risk_sized_strategy(store, leader_id)
    group = await mk_group(store, "global-pause", ["source", "owner-extra"], strategy=strategy, owner_user_id=leader_id)
    await _add_follower(store, group["group_id"], "paused-follower")
    async with SessionLocal() as session:
        relation = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.group_id == group["group_id"]))
        relation.leader_paused = True
        await session.commit()
    await group_service.warm_cache(store)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await GroupDispatcher(store).dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=.1, stop_loss=2300), "global-pause")
    assert {node_id for node_id, _ in sent} == {"source"}


async def test_inflight_old_primary_still_blocks_switching(store, monkeypatch):
    leader_id, _ = await mk_leader(store)
    await leader_nodes(store, leader_id, "source", "replacement")
    strategy = await _risk_sized_strategy(store, leader_id)
    await mk_group(store, "inflight-primary", ["source"], strategy=strategy, owner_user_id=leader_id)
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    await GroupDispatcher(store).dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=.1, stop_loss=2300), "inflight-primary")
    with pytest.raises(node_service.PrimarySwitchBlocked) as error:
        await node_service.set_primary(store, "replacement", True)
    assert error.value.code == "primary_switch_blocked"
    assert (await store.get_node("source"))["is_primary"]
