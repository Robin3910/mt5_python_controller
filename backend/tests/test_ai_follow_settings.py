"""两步跟单配置必须经审核进入执行、热推和重连，单次跟单只运行一轮。"""
import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select

from app import group_persist, group_rules, group_service
from app.ai_follow_settings import FollowSettings, apply_settings
from app.connections import manager
from app.db import SessionLocal, _migrate_follow_node_state
from app.group_dispatcher import GroupDispatcher, build_strategy_resume_command, build_strategy_start_command
from app.orm import GroupFollowerNode
from app.parser import TradingSignal
from app.state import state
from tests.test_ai_channel_review import approve, setup
from tests.test_ai_follow_api import client

SETTINGS = {"risk_mode": "custom", "loss_ratio": 35.0, "run_mode": "single"}


@pytest.mark.parametrize("value", [None, 0, -20, 101, float("inf")])
def test_custom_loss_ratio_requires_a_valid_percentage(value):
    with pytest.raises(ValidationError):
        FollowSettings(risk_mode="custom", loss_ratio=value)


def test_custom_risk_changes_only_signal_loss_control():
    snapshot = {"template_id": "tpl_1", "rules": [{"type": 1, "lot": 0.37, "max_allow_num": 8}]}
    result = apply_settings(snapshot, SETTINGS)
    assert result["rules"][0]["float_pl_ratio"]["ratio"] == -35
    assert result["rules"][0]["float_pl_ratio"]["action"] == "close_all"
    assert result["rules"][0]["lot"] == 0.37
    assert "float_pl_ratio" not in snapshot["rules"][0]
    snapshot["template_id"] = "tpl_2"
    assert apply_settings(snapshot, SETTINGS) == snapshot


def test_follow_settings_columns_migrate_once_on_existing_tables():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as session:
        for table in ("group_follower_node", "ai_follow_application_node", "group_task_dispatch"):
            session.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        _migrate_follow_node_state(session)
        _migrate_follow_node_state(session)
        for table in ("group_follower_node", "ai_follow_application_node", "group_task_dispatch"):
            assert "follow_settings_json" in {field["name"] for field in inspect(session).get_columns(table)}
    engine.dispose()


def configure(client, mode="single"):
    data = setup(client)
    settings = {**SETTINGS, "run_mode": mode}
    response = client.post("/api/ai-follow/join", headers=data["bob_h"], json={
        "leader_user_id": data["host"]["id"], "group_ids": [data["ai_group_id"]],
        "node_ids": [data["b_follow"]], "settings": settings,
    })
    assert response.status_code == 200, response.text
    application = client.get("/api/ai-follow/me", headers=data["bob_h"]).json()["applications"][0]
    assert application["nodes"][0]["settings"] == settings
    request_id = response.json()["pending"][0]["request_id"]
    approve(client, data, request_id)
    return data


async def open_round(data, signal_id):
    group = await state.store.get_group(data["ai_group_id"])
    strategy = await state.store.get_strategy(group["strategy_id"])
    snapshot = group_rules.strategy_rules_snapshot(strategy)
    signal = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.37)
    task_id = await group_persist.create_task(signal_id=signal_id, group=group, signal=signal,
        dispatch_mode="sync", strategy=strategy, strategy_snapshot=snapshot)
    command = build_strategy_start_command(signal_id, signal, 0.37, task_id, group["group_id"], snapshot)
    base = {"task_id": task_id, "signal_id": signal_id, "group_id": group["group_id"],
            "node_id": data["b_follow"], "symbol": "XAUUSD", "decided_vol": 0.37, "action": "BUY"}
    return await GroupDispatcher(state.store)._dispatch_locked_node(base, command)


def test_approved_config_survives_cache_warm_resume_and_leader_hot_push(client, monkeypatch):
    data = configure(client)
    sent = []
    async def send(node_id, command):
        sent.append(command)
        return True
    monkeypatch.setattr(manager, "send_to_node", send)
    async def run():
        await group_service.warm_cache(state.store)
        result = await open_round(data, "custom-start")
        assert result["status"] == "sent"
        assert sent[0]["entry"]["volume"] == 0.37
        assert sent[0]["strategy"]["rules"][0]["float_pl_ratio"]["ratio"] == -35
        subtasks = await group_persist.resumable_tasks(data["b_follow"])
        resumed = build_strategy_resume_command(subtasks[0])
        assert resumed["strategy"]["rules"][0]["float_pl_ratio"]["ratio"] == -35
        changed = deepcopy(subtasks[0]["strategy_snapshot"])
        changed["rules"][0]["float_pl_ratio"]["ratio"] = -80
        changed["rules"][0]["max_allow_num"] = 12
        rows = await group_persist.rewrite_running_strategy_snapshots(changed["strategy_id"], changed)
        await GroupDispatcher(state.store).push_strategy_updates(rows, changed)
        assert sent[-1]["strategy"]["rules"][0]["float_pl_ratio"]["ratio"] == -35
        assert sent[-1]["strategy"]["rules"][0]["max_allow_num"] == 12
    client.portal.call(run)


def test_single_round_is_claimed_once_then_stops_after_completion(client, monkeypatch):
    data = configure(client)
    async def send(*args):
        return True
    monkeypatch.setattr(manager, "send_to_node", send)
    async def run():
        results = await asyncio.gather(open_round(data, "single-a"), open_round(data, "single-b"))
        assert sorted(row["status"] for row in results) == ["sent", "skipped"]
        dispatch_id = next(row["dispatch_id"] for row in results if row["status"] == "sent")
        await group_persist.update_dispatch_result(node_id=data["b_follow"], dispatch_id=dispatch_id,
                                                   result={"success": True, "order": 77})
        async with SessionLocal() as session:
            relation = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.node_id == data["b_follow"]))
            relation.leader_paused = True
            await session.commit()
        finished = await group_persist.finish_subtask(node_id=data["b_follow"], dispatch_id=dispatch_id,
                                                     data={"status": "done", "total_orders": 1})
        await GroupDispatcher(state.store)._release(finished)
        async with SessionLocal() as session:
            row = await session.scalar(select(GroupFollowerNode).where(GroupFollowerNode.node_id == data["b_follow"]))
            assert row.follow_state == "stopped"
        assert (await state.store.get_group(data["ai_group_id"]))["followers"] == []
        assert (await open_round(data, "single-c"))["status"] == "skipped"
    client.portal.call(run)
    mine = client.get("/api/ai-follow/me", headers=data["bob_h"]).json()
    assert mine["following"][0]["groups"][0]["my_nodes"][0]["follow_state"] == "stopped"
    stats = client.get("/api/ai-follow/nodes", headers=data["bob_h"]).json()
    assert stats["nodes"][0]["follow_state"] == "stopped"


def test_single_round_start_failure_can_retry(client, monkeypatch):
    data = configure(client)
    succeeds = False
    async def send(*args):
        return succeeds
    monkeypatch.setattr(manager, "send_to_node", send)
    async def run():
        nonlocal succeeds
        assert (await open_round(data, "failed-send"))["status"] == "offline"
        succeeds = True
        started = await open_round(data, "retry-send")
        assert started["status"] == "sent"
        failed = await group_persist.update_dispatch_result(node_id=data["b_follow"], dispatch_id=started["dispatch_id"], result={"success": False})
        await GroupDispatcher(state.store)._release(failed)
        assert (await open_round(data, "retry-open"))["status"] == "sent"
    client.portal.call(run)


def test_loop_mode_continues_after_a_completed_round(client, monkeypatch):
    data = configure(client, mode="loop")
    async def send(*args):
        return True
    monkeypatch.setattr(manager, "send_to_node", send)
    async def run():
        started = await open_round(data, "loop-a")
        finished = await group_persist.finish_subtask(node_id=data["b_follow"], dispatch_id=started["dispatch_id"], data={"status": "done", "total_orders": 1})
        await GroupDispatcher(state.store)._release(finished)
        assert (await open_round(data, "loop-b"))["status"] == "sent"
    client.portal.call(run)


def test_new_robot_inherits_settings_into_a_pending_application(client):
    from tests.test_ai_follow_api import _mk_ai_strategy, _mk_group
    data = configure(client)
    strategy_id = _mk_ai_strategy(client, data["host_h"], "inherited-risk")
    group_id = _mk_group(client, data["host_h"], [data["h_primary"]], "inherited", strategy_id, True)
    response = client.post(f"/api/groups/{group_id}/adopt-followers", headers=data["host_h"])
    assert response.status_code == 200, response.text
    applications = client.get("/api/ai-follow/me", headers=data["bob_h"]).json()["applications"]
    pending = next(row for row in applications if row["group_id"] == group_id)
    assert pending["status"] == "pending"
    assert pending["source"] == "inherit"
    assert pending["nodes"][0]["settings"] == SETTINGS
