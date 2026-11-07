"""跟单者从机器人卡片暂停时，仅改变本人节点在当前分组的跟单。"""
import pytest
from sqlalchemy import select

from app import group_service, user_service
from app.db import SessionLocal
from app.orm import AuditLog, GroupFollowerNode, GroupTaskDispatch
from app.state import state
from tests.test_ai_follow_api import _login, client
from tests.test_ai_follow_nodes_api import join_groups, seed_positions
from tests.test_ai_robot_groups import setup_two_robots


def test_card_pause_and_resume_are_scoped_to_owned_nodes_and_selected_group(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    first_group = setup["ai_group_id"]
    second_group = setup["second_group_id"]
    node_id = setup["b_follow"]
    join_groups(client, setup, headers, [node_id], [first_group, second_group])
    join_groups(client, setup, setup["host_h"], [setup["h_extra"]], [first_group])
    client.portal.call(seed_positions, setup)
    busy_key = f"group:node:busy:{first_group}:{node_id}"
    client.portal.call(state.store.r.set, busy_key, "existing-task")

    response = client.post(
        "/api/ai-follow/nodes/state",
        json={"node_ids": [node_id], "state": "paused", "group_id": first_group},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed_count"] == 1
    client.portal.call(group_service.warm_cache, state.store)
    entries = client.get("/api/ai-follow/me", headers=headers).json()["following"]
    states = {entry["robot"]["group_id"]: entry["groups"][0]["my_nodes"][0]["follow_state"] for entry in entries}
    assert states == {first_group: "paused", second_group: "running"}
    assert client.portal.call(state.store.r.get, busy_key) == "existing-task"
    assert len(client.portal.call(state.store.get_account, node_id)["positions"]) == 4

    async def check_scope_and_audit():
        async with SessionLocal() as session:
            peer = await session.scalar(select(GroupFollowerNode).where(
                GroupFollowerNode.group_id == first_group, GroupFollowerNode.node_id == setup["h_extra"],
            ))
            assert peer.follow_state == "running"
            tasks = (await session.scalars(select(GroupTaskDispatch))).all()
            assert all(task.status == "running" for task in tasks)
            audit = await session.scalar(select(AuditLog).where(AuditLog.action == "pause_ai_follow_nodes"))
            assert audit.params_json["group_id"] == first_group
            assert audit.params_json["node_ids"] == [node_id]
        user = await user_service.get_user_by_id(setup["host"]["id"])
        assert user.ai_follow_paused is False
    client.portal.call(check_scope_and_audit)

    response = client.post(
        "/api/ai-follow/nodes/state",
        json={"node_ids": [node_id], "state": "paused", "group_id": second_group}, headers=headers,
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/ai-follow/nodes/state",
        json={"node_ids": [node_id], "state": "running", "group_id": first_group}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed_count"] == 1
    entries = client.get("/api/ai-follow/me", headers=headers).json()["following"]
    assert {
        entry["robot"]["group_id"]: entry["groups"][0]["my_nodes"][0]["follow_state"]
        for entry in entries
    } == {first_group: "running", second_group: "paused"}


@pytest.mark.parametrize("invalid_target", ["foreign", "primary", "other_group", "missing_group"])
def test_card_pause_rejects_invalid_scope_without_partial_updates(client, invalid_target):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    node_id = setup["b_follow"]
    group_id = setup["ai_group_id"]
    join_groups(client, setup, headers, [node_id], [group_id])
    node_ids = [node_id]
    if invalid_target == "foreign":
        node_ids.append(setup["h_extra"])
    elif invalid_target == "primary":
        node_ids.append(setup["b_primary"])
    elif invalid_target == "other_group":
        group_id = setup["second_group_id"]
    else:
        group_id = "grp_missing"
    response = client.post(
        "/api/ai-follow/nodes/state",
        json={"node_ids": node_ids, "state": "paused", "group_id": group_id}, headers=headers,
    )
    assert response.status_code == 404, response.text
    node = client.get("/api/ai-follow/nodes", headers=headers).json()["nodes"][0]
    assert node["groups"][0]["follow_state"] == "running"
