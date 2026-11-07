"""子节点统计的去重、运行状态、权限、持仓保留与平仓范围。"""
import pytest
from sqlalchemy import create_engine, select, text

from app import follow_service, group_service
from app.connections import manager
from app.db import SessionLocal, _migrate_follow_node_state
from app.orm import AuditLog, GroupFollowerNode, GroupSignalTask, GroupTaskDispatch
from app.state import state
from tests.test_ai_follow_api import _assign, _login, _mk_group, _mk_node, _mk_trend_strategy, client
from tests.test_ai_robot_groups import setup_two_robots


def join_groups(client, setup, headers, node_ids, group_ids):
    response = client.post(
        "/api/ai-follow/join",
        json={"leader_user_id": setup["host"]["id"], "group_ids": group_ids, "node_ids": node_ids},
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def seed_positions(setup):
    entries = [(setup["ai_group_id"], 1001, "tpl_1"), (setup["second_group_id"], 1002, "tpl_1"), (setup["ai_group_id"], 1003, "tpl_2")]
    async with SessionLocal() as session:
        for group_id, magic, template_id in entries:
            task = GroupSignalTask(signal_id=f"node-{magic}", group_id=group_id, strategy_snapshot_json={"template_id": template_id}, status="running")
            session.add(task)
            await session.flush()
            session.add(GroupTaskDispatch(task_id=task.task_id, signal_id=task.signal_id, group_id=group_id, node_id=setup["b_follow"], magic=magic, status="running"))
        await session.commit()
    await state.store.save_account(setup["b_follow"], {
        "balance": 5200.0,
        "positions": [
            {"ticket": 101, "magic": 1001, "profit": 42.9, "volume": 0.2},
            {"ticket": 102, "magic": 1002, "profit": 999.0, "volume": 1.0},
            {"ticket": 103, "magic": 1003, "profit": 888.0, "volume": 2.0},
            {"ticket": 104, "magic": 0, "profit": 777.0, "volume": 3.0},
        ],
    })


def test_node_stats_deduplicates_and_excludes_other_market_and_primary(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    group_ids = [setup["ai_group_id"], setup["second_group_id"]]
    join_groups(client, setup, headers, [setup["b_follow"]], group_ids)
    trend = _mk_trend_strategy(client, setup["host_h"], "trend-for-node-stats")
    trend_group = _mk_group(client, setup["host_h"], [setup["h_primary"]], "trend-node-group", strategy_id=trend)
    response = client.post("/api/follow/join", json={"leader_user_id": setup["host"]["id"], "group_ids": [trend_group], "node_ids": [setup["b_follow"]]}, headers=headers)
    assert response.status_code == 200, response.text
    response = client.get("/api/ai-follow/nodes", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["node_count"] == 1
    assert len(body["nodes"]) == 1
    node = body["nodes"][0]
    assert node["node_id"] == setup["b_follow"]
    assert {group["group_id"] for group in node["groups"]} == set(group_ids)
    assert node["follow_state"] == "running"
    assert node["floating_profit"] is None
    assert body["summary"]["investment_amount"] is None
    assert client.get("/api/ai-follow/nodes", headers=setup["host_h"]).json()["nodes"] == []


def test_node_pause_resume_stop_restart_and_scoped_rejoin(client):
    setup = setup_two_robots(client)
    admin_headers = _login(client, "admin")
    peer = _mk_node(client, admin_headers, "bob-peer", 99033, primary=False)
    assigned = _assign(client, admin_headers, setup["bob"]["id"], [setup["b_primary"], setup["b_follow"], peer])
    assert assigned.status_code == 200, assigned.text
    headers = _login(client, "bob")
    group_ids = [setup["ai_group_id"], setup["second_group_id"]]
    join_groups(client, setup, headers, [setup["b_follow"], peer], group_ids)
    response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "paused"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["changed_count"] == 2
    client.portal.call(group_service.warm_cache, state.store)
    nodes = {node["node_id"]: node for node in client.get("/api/ai-follow/nodes", headers=headers).json()["nodes"]}
    assert nodes[setup["b_follow"]]["follow_state"] == "paused"
    assert nodes[peer]["follow_state"] == "running"
    response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "running"}, headers=headers)
    assert response.json()["changed_count"] == 2
    response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "stopped"}, headers=headers)
    assert response.status_code == 200, response.text
    client.portal.call(group_service.warm_cache, state.store)
    body = client.get("/api/ai-follow/nodes", headers=headers).json()
    assert body["summary"]["node_count"] == 2
    assert body["summary"]["active_node_count"] == 1
    assert next(node for node in body["nodes"] if node["node_id"] == setup["b_follow"])["follow_state"] == "stopped"
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])

    async def check():
        async with SessionLocal() as session:
            rows = (await session.scalars(select(GroupFollowerNode).where(GroupFollowerNode.node_id == setup["b_follow"]))).all()
            assert {row.group_id: row.follow_state for row in rows} == {setup["ai_group_id"]: "running", setup["second_group_id"]: "stopped"}
            audits = (await session.scalars(select(AuditLog).where(AuditLog.actor_user_id == setup["bob"]["id"], AuditLog.category == "ai_follow"))).all()
            assert {audit.action for audit in audits}.issuperset({"pause_ai_follow_nodes", "resume_ai_follow_nodes", "stop_ai_follow_nodes"})
    client.portal.call(check)


def test_node_state_rejects_foreign_or_primary_without_partial_update(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    for invalid_node in (setup["h_extra"], setup["b_primary"], "missing-node"):
        response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"], invalid_node], "state": "stopped"}, headers=headers)
        assert response.status_code == 404
    assert client.get("/api/ai-follow/nodes", headers=headers).json()["nodes"][0]["follow_state"] == "running"
    assert client.post("/api/ai-follow/nodes/state", json={"node_ids": [], "state": "paused"}, headers=headers).status_code == 422
    assert client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "invalid"}, headers=headers).status_code == 422


def test_stopping_node_keeps_real_positions_tasks_and_busy(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    client.portal.call(seed_positions, setup)
    busy_key = f"group:node:busy:{setup['ai_group_id']}:{setup['b_follow']}"
    client.portal.call(state.store.r.set, busy_key, "existing-task")
    response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "stopped"}, headers=headers)
    assert response.status_code == 200, response.text
    body = client.get("/api/ai-follow/nodes", headers=headers).json()
    node = body["nodes"][0]
    assert node["follow_state"] == "stopped"
    assert node["position_count"] == 1
    assert node["floating_profit"] == pytest.approx(42.9)
    assert node["volume"] == pytest.approx(0.2)
    assert node["balance"] == 5200.0
    assert "positions" not in node
    assert body["summary"]["active_node_count"] == 0
    assert body["summary"]["floating_profit"] == 0
    assert client.portal.call(state.store.r.get, busy_key) == "existing-task"

    async def check_tasks():
        async with SessionLocal() as session:
            rows = (await session.scalars(select(GroupTaskDispatch))).all()
            assert all(row.status == "running" for row in rows)
    client.portal.call(check_tasks)


def test_node_close_only_submits_selected_ai_follow_tickets(client, monkeypatch):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    client.portal.call(seed_positions, setup)
    sent = []
    async def send(node_id, command):
        sent.append((node_id, command))
        return True
    monkeypatch.setitem(manager.nodes, setup["b_follow"], object())
    monkeypatch.setattr(manager, "send_to_node", send)
    response = client.post("/api/ai-follow/nodes/close", json={"node_ids": [setup["b_follow"]]}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["submitted"] == 1
    assert response.json()["failed"] == []
    assert [(node_id, command["close_ticket"]) for node_id, command in sent] == [(setup["b_follow"], 101)]
    assert all(command["cmd"] == "close" for node_id, command in sent)


def test_node_close_offline_reports_failure_and_records_audit(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    client.portal.call(seed_positions, setup)
    response = client.post("/api/ai-follow/nodes/close", json={"node_ids": [setup["b_follow"]]}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["submitted"] == 0
    assert response.json()["failed"][0]["node_id"] == setup["b_follow"]

    async def check_audit():
        async with SessionLocal() as session:
            audit = await session.scalar(select(AuditLog).where(AuditLog.action == "close_ai_follow_nodes"))
            assert audit.result == "partial"
    client.portal.call(check_audit)


def test_automatic_adoption_preserves_pause_and_does_not_restart_stopped(client):
    setup = setup_two_robots(client)
    headers = _login(client, "bob")
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    response = client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "paused"}, headers=headers)
    assert response.status_code == 200, response.text

    async def adopt():
        target = await state.store.get_group(setup["second_group_id"])
        return await follow_service.adopt_followers_for_group(state.store, target_group=target, source_group_ids=[setup["ai_group_id"]])
    adopted, skipped = client.portal.call(adopt)
    assert len(adopted) == 1
    assert all(group["follow_state"] == "paused" for group in client.get("/api/ai-follow/nodes", headers=headers).json()["nodes"][0]["groups"])
    client.post("/api/ai-follow/nodes/state", json={"node_ids": [setup["b_follow"]], "state": "stopped"}, headers=headers)
    join_groups(client, setup, headers, [setup["b_follow"]], [setup["ai_group_id"]])
    adopted, skipped = client.portal.call(adopt)
    assert adopted == []
    assert len(skipped) == 1
    groups = client.get("/api/ai-follow/nodes", headers=headers).json()["nodes"][0]["groups"]
    assert next(group for group in groups if group["group_id"] == setup["second_group_id"])["follow_state"] == "stopped"


def test_follow_node_state_migration_defaults_and_is_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE group_follower_node (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO group_follower_node (id) VALUES (1)"))
        _migrate_follow_node_state(connection)
        _migrate_follow_node_state(connection)
        assert connection.scalar(text("SELECT follow_state FROM group_follower_node WHERE id = 1")) == "running"
    engine.dispose()
