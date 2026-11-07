"""AI 机器人卡片的设置入口仅指向当前用户拥有的标题策略。"""
from tests.test_ai_follow_api import (
    CAP_AI,
    CAP_LEADER,
    _login,
    _mk_ai_strategy,
    _mk_group,
    _setup,
    client,
)


def test_robot_settings_visible_only_to_strategy_owner(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER])
    response = client.get("/api/strategies", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    strategy_id = response.json()[0]["strategy_id"]

    for headers, expected in (
        (setup["host_h"], strategy_id),
        (_login(client, "bob"), None),
        (admin_headers, None),
    ):
        response = client.get("/api/ai-follow/robots", headers=headers)
        assert response.status_code == 200, response.text
        robot = next(
            item for item in response.json()["robots"]
            if item["leader_user_id"] == setup["host"]["id"]
        )
        assert robot["editable_strategy_id"] == expected


def test_robot_settings_edits_title_strategy_with_multiple_groups(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER], ai_group=False)
    strategies_by_name = {}
    for name in ("first-ai", "second-ai"):
        strategy_id = _mk_ai_strategy(client, setup["host_h"], name)
        strategies_by_name[name] = strategy_id
        _mk_group(
            client, setup["host_h"], [setup["h_primary"]], name,
            strategy_id=strategy_id, accept_ai_follow=True,
        )

    response = client.get("/api/ai-follow/robots", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    robots = response.json()["robots"]
    assert len(robots) == 2
    assert {robot["strategy_name"] for robot in robots} == set(strategies_by_name)
    for robot in robots:
        assert robot["editable_strategy_id"] == strategies_by_name[robot["strategy_name"]]
    robot = robots[0]

    response = client.patch(
        f"/api/strategies/{robot['editable_strategy_id']}",
        json={"name": "renamed-ai"}, headers=setup["host_h"],
    )
    assert response.status_code == 200, response.text
    response = client.get("/api/ai-follow/robots", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    updated = next(item for item in response.json()["robots"] if item["group_id"] == robot["group_id"])
    assert updated["strategy_name"] == "renamed-ai"
    assert updated["editable_strategy_id"] == robot["editable_strategy_id"]
    for other in robots[1:]:
        unchanged = next(item for item in response.json()["robots"] if item["group_id"] == other["group_id"])
        assert unchanged["strategy_name"] == other["strategy_name"]
    response = client.get("/api/groups", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2


def test_robot_without_strategy_has_no_settings_entry(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER], ai_group=False)
    _mk_group(
        client, setup["host_h"], [setup["h_primary"]], "no-strategy",
        accept_ai_follow=True,
    )

    response = client.get("/api/ai-follow/robots", headers=setup["host_h"])
    assert response.status_code == 200, response.text
    robot = response.json()["robots"][0]
    assert robot["strategy_name"] is None
    assert robot["editable_strategy_id"] is None
