"""同一主人名下的 AI 策略按分组独立展示、跟单和统计。"""
import time

import pytest

from app import deal_store
from app.db import SessionLocal
from app.orm import GroupSignalTask, GroupTaskDispatch
from app.state import state
from tests.test_ai_follow_api import _approve_pending
from tests.test_ai_follow_api import (
    CAP_AI,
    CAP_LEADER,
    _login,
    _mk_ai_strategy,
    _mk_group,
    _setup,
    client,
)


def setup_two_robots(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER])
    strategy_id = _mk_ai_strategy(client, setup["host_h"], "second-ai")
    group_id = _mk_group(
        client, setup["host_h"], [setup["h_primary"]], "second-ai-group",
        strategy_id=strategy_id, accept_ai_follow=True,
    )
    setup["second_group_id"] = group_id
    setup["second_strategy_id"] = strategy_id
    return setup


def test_two_strategies_are_two_public_and_owned_robots(client):
    setup = setup_two_robots(client)
    response = client.get("/api/ai-follow/robots", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    robots = response.json()["robots"]
    assert len(robots) == 2
    assert {robot["group_id"] for robot in robots} == {setup["ai_group_id"], setup["second_group_id"]}
    assert {robot["strategy_name"] for robot in robots} == {"host-ai", "second-ai"}
    for robot in robots:
        assert robot["strategy_id"] == robot["editable_strategy_id"]
        assert robot["ai_group_count"] == 1
        assert robot["strategy_names"] == [robot["strategy_name"]]
        detail = client.get(
            f"/api/ai-follow/robots/{setup['host']['id']}",
            params={"group_id": robot["group_id"]}, headers=setup["host_h"],
        )
        assert detail.status_code == 200, detail.text
        assert [group["group_id"] for group in detail.json()["groups"]] == [robot["group_id"]]
        assert detail.json()["robot"]["strategy_id"] == robot["strategy_id"]
    response = client.get("/api/ai-follow/me", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    entries = response.json()["following"]
    assert len(entries) == 2
    assert all(entry["is_owner"] and len(entry["groups"]) == 1 for entry in entries)
    assert client.get(
        f"/api/ai-follow/me/{setup['host']['id']}", headers=setup["host_h"],
    ).status_code == 400


def test_follow_and_leave_are_isolated_per_robot_group(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    for group_id in (setup["ai_group_id"], setup["second_group_id"]):
        response = client.post(
            "/api/ai-follow/join",
            json={"leader_user_id": setup["host"]["id"], "group_ids": [group_id], "node_ids": [setup["b_follow"]]},
            headers=headers,
        )
        _approve_pending(client, setup["host_h"])
        assert response.status_code == 200, response.text
    response = client.get("/api/ai-follow/me", headers=headers)
    assert len(response.json()["following"]) == 2
    assert response.json()["summary"] == {"following_count": 2, "follow_node_count": 2}
    response = client.post(
        "/api/ai-follow/leave",
        json={"leader_user_id": setup["host"]["id"], "group_ids": [setup["ai_group_id"]], "node_ids": [setup["b_follow"]], "close_tasks": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    response = client.get("/api/ai-follow/me", headers=headers)
    assert [entry["robot"]["group_id"] for entry in response.json()["following"]] == [setup["second_group_id"]]
    response = client.get("/api/ai-follow/robots", headers=headers)
    robots = {robot["group_id"]: robot for robot in response.json()["robots"]}
    assert robots[setup["ai_group_id"]]["my_follow_node_count"] == 0
    assert robots[setup["second_group_id"]]["my_follow_node_count"] == 1
    assert all(robot["editable_strategy_id"] is None for robot in robots.values())


def test_robot_holdings_and_performance_do_not_mix_two_groups(client):
    setup = setup_two_robots(client)
    entries = [(setup["ai_group_id"], 601, 3.0, 20.0), (setup["second_group_id"], 602, 9.0, 70.0)]

    async def seed():
        async with SessionLocal() as session:
            for group_id, magic, floating, realized in entries:
                task = GroupSignalTask(signal_id=f"group-{magic}", group_id=group_id, strategy_snapshot_json={"template_id": "tpl_1"}, status="done")
                session.add(task)
                await session.flush()
                session.add(GroupTaskDispatch(task_id=task.task_id, signal_id=task.signal_id, group_id=group_id, node_id=setup["h_primary"], magic=magic, status="done"))
            await session.commit()
        for group_id, magic, floating, realized in entries:
            await deal_store.save_deals(setup["h_primary"], [{"deal_id": magic, "position_id": magic, "magic": magic, "entry_type": "OUT", "profit": realized, "volume": 0.1, "symbol": "XAUUSD", "time": int(time.time())}])
        await state.store.save_account(setup["h_primary"], {"positions": [{"magic": magic, "profit": floating, "volume": 0.1} for group_id, magic, floating, realized in entries]})

    client.portal.call(seed)
    response = client.get("/api/ai-follow/robots", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    robots = {robot["group_id"]: robot for robot in response.json()["robots"]}
    for group_id, magic, floating, realized in entries:
        assert robots[group_id]["performance"]["total_profit"] == pytest.approx(realized)
        response = client.get(
            f"/api/ai-follow/me/{setup['host']['id']}", params={"group_id": group_id},
            headers=setup["host_h"],
        )
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["robot"]["group_id"] == group_id
        assert detail["metrics"]["floating_profit"] == pytest.approx(floating)
        assert detail["metrics"]["position_count"] == 1
        assert detail["performance"]["total_profit"] == pytest.approx(realized)
        assert sum(point["profit"] for point in detail["daily_profit"]) == pytest.approx(realized)
