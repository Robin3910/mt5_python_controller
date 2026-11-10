"""我的机器人包含绑定主节点的自有 AI 策略，并保留既有跟单关系。"""
import pytest

from app import follow_service
from tests.test_ai_follow_api import _approve_pending
from tests.test_ai_follow_api import (
    CAP_AI,
    CAP_LEADER,
    _login,
    _mk_ai_strategy,
    _mk_group,
    _mk_node,
    _mk_trend_strategy,
    _setup,
    client,
)


@pytest.mark.parametrize("caps,open_ai", [
    ([CAP_AI, CAP_LEADER], True),
    ([CAP_AI, CAP_LEADER], False),
    ([CAP_AI], False),
])
def test_owned_ai_robot_visible_without_following(client, caps, open_ai):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=caps, open_ai=open_ai)

    response = client.get("/api/ai-follow/me", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["following"]) == 1
    entry = body["following"][0]
    assert entry["is_owner"] is True
    assert entry["robot"]["leader_user_id"] == setup["host"]["id"]
    assert entry["robot"]["primary_node"]["node_id"] == setup["h_primary"]
    assert entry["robot"]["primary_node"]["online"] is False
    assert [group["group_id"] for group in entry["groups"]] == [setup["ai_group_id"]]
    assert entry["groups"][0]["my_nodes"] == []
    assert body["summary"] == {"following_count": 0, "follow_node_count": 0}
    assert client.portal.call(follow_service.list_followings, setup["host"]["id"]) == []

    bob_headers = _login(client, "bob")
    assert client.get("/api/ai-follow/me", headers=bob_headers).json()["following"] == []


@pytest.mark.parametrize("binding", ["unbound", "secondary", "trend", "no_primary"])
def test_owned_robot_requires_ai_strategy_bound_to_primary(client, binding):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER], ai_group=False)
    host_headers = setup["host_h"]
    strategy_id = (
        _mk_trend_strategy(client, host_headers, "trend")
        if binding == "trend"
        else _mk_ai_strategy(client, host_headers, "ai")
    )
    if binding != "unbound":
        node_id = setup["h_extra"] if binding == "secondary" else setup["h_primary"]
        _mk_group(client, host_headers, [node_id], "owned", strategy_id=strategy_id)
    if binding == "no_primary":
        response = client.post(
            f"/api/nodes/{setup['h_primary']}/primary",
            json={"is_primary": False}, headers=admin_headers,
        )
        assert response.status_code == 200, response.text

    response = client.get("/api/ai-follow/me", headers=host_headers)
    assert response.status_code == 200, response.text
    assert response.json()["following"] == []


def test_owned_robot_merges_self_following_without_duplicate_or_primary_follower(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER])
    private_strategy = _mk_ai_strategy(client, setup["host_h"], "private-ai")
    private_group = _mk_group(
        client, setup["host_h"], [setup["h_primary"]], "private",
        strategy_id=private_strategy,
    )
    response = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": setup["host"]["id"],
            "group_ids": [setup["ai_group_id"]],
            "node_ids": [setup["h_extra"]],
        },
        headers=setup["host_h"],
    )
    _approve_pending(client, setup["host_h"])
    assert response.status_code == 200, response.text

    response = client.get("/api/ai-follow/me", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["following"]) == 2
    entries = {entry["robot"]["group_id"]: entry for entry in body["following"]}
    assert set(entries) == {setup["ai_group_id"], private_group}
    assert all(entry["is_owner"] and len(entry["groups"]) == 1 for entry in entries.values())
    assert [
        node["node_id"] for group in entries[setup["ai_group_id"]]["groups"] for node in group["my_nodes"]
    ] == [setup["h_extra"]]
    assert entries[private_group]["groups"][0]["my_nodes"] == []
    assert body["summary"] == {"following_count": 1, "follow_node_count": 1}


def test_owned_robot_keeps_following_other_robot(client):
    admin_headers = _login(client, "admin")
    own = _setup(client, admin_headers, caps=[CAP_AI], open_ai=False)
    other = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER], suffix="2")
    response = client.post(
        "/api/ai-follow/join",
        json={
            "leader_user_id": other["host"]["id"],
            "group_ids": [other["ai_group_id"]],
            "node_ids": [own["h_extra"]],
        },
        headers=own["host_h"],
    )
    _approve_pending(client, other["host_h"])
    assert response.status_code == 200, response.text

    response = client.get("/api/ai-follow/me", headers=own["host_h"])
    assert response.status_code == 200, response.text
    body = response.json()
    entries = {entry["robot"]["leader_user_id"]: entry for entry in body["following"]}
    assert set(entries) == {own["host"]["id"], other["host"]["id"]}
    assert entries[own["host"]["id"]]["is_owner"] is True
    assert entries[other["host"]["id"]]["is_owner"] is False
    assert entries[other["host"]["id"]]["groups"][0]["my_nodes"][0]["node_id"] == own["h_extra"]
    assert body["summary"] == {"following_count": 1, "follow_node_count": 1}


def test_admin_owned_robot_uses_admin_resource_ownership(client):
    headers = _login(client, "admin")
    primary_node = _mk_node(client, headers, "admin-primary", 99001)
    strategy_id = _mk_ai_strategy(client, headers, "admin-ai")
    group_id = _mk_group(client, headers, [primary_node], "admin-ai", strategy_id=strategy_id)

    response = client.get("/api/ai-follow/me", headers=headers)
    assert response.status_code == 200, response.text
    entry = response.json()["following"][0]
    assert entry["is_owner"] is True
    assert entry["robot"]["primary_node"]["node_id"] == primary_node
    assert entry["groups"][0]["group_id"] == group_id
    assert entry["groups"][0]["my_nodes"] == []
