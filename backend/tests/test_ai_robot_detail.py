"""我的机器人详情的访问归属、真实持仓及分组成交隔离。"""
import time

import pytest

from app import deal_store
from app.state import state
from app.db import SessionLocal
from app.orm import GroupSignalTask, GroupTaskDispatch
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


def test_private_owned_robot_detail_without_snapshot(client):
    headers = _login(client, "admin")
    setup = _setup(client, headers, caps=[CAP_AI], open_ai=False)
    response = client.get(f"/api/ai-follow/me/{setup['host']['id']}", headers=setup["host_h"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_owner"] is True
    assert body["can_control"] is False
    assert body["robot"]["editable_strategy_id"] == body["groups"][0]["strategy_id"]
    assert body["groups"][0]["symbol"] == "XAUUSD"
    assert body["metrics"]["node_count"] == 1
    assert body["metrics"]["follow_node_count"] == 0
    assert body["metrics"]["snapshot_node_count"] == 0
    assert body["metrics"]["floating_profit"] is None
    assert body["metrics"]["position_count"] is None
    assert body["metrics"]["investment_amount"] is None
    assert body["metrics"]["profit_rate"] is None
    assert body["performance"]["data_available"] is False
    assert len(body["daily_profit"]) == 7
    assert all(point["profit"] == 0 for point in body["daily_profit"])


def test_my_robot_detail_requires_owned_or_followed_robot(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER])
    for headers in (_login(client, "bob"), admin_headers):
        response = client.get(f"/api/ai-follow/me/{setup['host']['id']}", headers=headers)
        assert response.status_code == 404
    assert client.get("/api/ai-follow/me/999999", headers=setup["host_h"]).status_code == 404


def test_followed_robot_detail_scopes_nodes_groups_and_template_snapshots(client):
    admin_headers = _login(client, "admin")
    setup = _setup(client, admin_headers, caps=[CAP_AI, CAP_LEADER])
    bob_headers = _login(client, "bob")
    joined = client.post(
        "/api/ai-follow/join",
        json={"leader_user_id": setup["host"]["id"], "group_ids": [setup["ai_group_id"]], "node_ids": [setup["b_follow"]]},
        headers=bob_headers,
    )
    _approve_pending(client, setup["host_h"])
    assert joined.status_code == 200, joined.text
    other_strategy = _mk_ai_strategy(client, setup["host_h"], "other-ai")
    other_group = _mk_group(
        client, setup["host_h"], [setup["h_primary"]], "other-ai",
        strategy_id=other_strategy,
    )

    async def seed():
        entries = [
            (setup["b_follow"], setup["ai_group_id"], 101, "tpl_1", 10.0),
            (setup["b_follow"], other_group, 202, "tpl_1", 500.0),
            (setup["b_follow"], setup["ai_group_id"], 303, "tpl_2", 800.0),
            (setup["h_primary"], setup["ai_group_id"], 101, "tpl_1", 999.0),
            (setup["b_primary"], setup["ai_group_id"], 404, "tpl_1", 700.0),
        ]
        async with SessionLocal() as session:
            for index, (node_id, group_id, magic, template_id, profit) in enumerate(entries, start=1):
                task = GroupSignalTask(
                    signal_id=f"detail-{index}", group_id=group_id,
                    strategy_snapshot_json={"template_id": template_id}, status="done",
                )
                session.add(task)
                await session.flush()
                session.add(GroupTaskDispatch(
                    task_id=task.task_id, signal_id=task.signal_id, group_id=group_id,
                    node_id=node_id, magic=magic, status="done",
                ))
            await session.commit()
        for index, (node_id, group_id, magic, template_id, profit) in enumerate(entries, start=1):
            await deal_store.save_deals(node_id, [{
                "deal_id": index, "position_id": index, "symbol": "XAUUSD", "magic": magic,
                "entry_type": "OUT", "profit": profit, "swap": -0.5, "commission": -1.0,
                "volume": 0.1, "time": int(time.time()),
            }])
        await deal_store.save_deals(setup["b_follow"], [{
            "deal_id": 20, "position_id": 1, "symbol": "XAUUSD", "magic": 101,
            "entry_type": "IN", "profit": 555.0, "volume": 0.1, "time": int(time.time()),
        }, {
            "deal_id": 21, "position_id": 21, "symbol": "XAUUSD", "magic": 0,
            "entry_type": "OUT", "profit": 9999.0, "volume": 0.1, "time": int(time.time()),
        }])
        await state.store.save_account(setup["b_follow"], {
            "updated_at": 123456.0,
            "positions": [
                {"magic": 101, "profit": 12.0, "volume": 0.2},
                {"magic": 202, "profit": 500.0, "volume": 1.0},
                {"magic": 303, "profit": 800.0, "volume": 2.0},
                {"magic": 0, "profit": 9999.0, "volume": 3.0},
            ],
        })
        await state.store.save_account(setup["h_primary"], {
            "updated_at": 123457.0,
            "positions": [{"magic": 101, "profit": 100.0, "volume": 0.5}],
        })

    client.portal.call(seed)
    response = client.get(f"/api/ai-follow/me/{setup['host']['id']}", headers=bob_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["can_control"] is False
    assert body["robot"]["editable_strategy_id"] is None
    assert body["metrics"]["node_count"] == 1
    assert body["metrics"]["follow_node_count"] == 1
    assert body["metrics"]["position_count"] == 1
    assert body["metrics"]["profitable_node_count"] == 1
    assert body["metrics"]["floating_profit"] == pytest.approx(12.0)
    assert body["metrics"]["volume"] == pytest.approx(0.2)
    assert body["updated_at"] == 123456.0
    assert body["performance"]["total_profit"] == pytest.approx(8.5)
    assert len(body["daily_profit"]) == 7
    assert sum(point["profit"] for point in body["daily_profit"]) == pytest.approx(8.5)
    assert sum(point["deals"] for point in body["daily_profit"]) == 1

    response = client.get(
        f"/api/ai-follow/me/{setup['host']['id']}",
        params={"group_id": setup["ai_group_id"]}, headers=setup["host_h"],
    )
    assert response.status_code == 200, response.text
    owned = response.json()
    assert owned["can_control"] is True
    assert owned["metrics"]["follow_node_count"] == 0
    assert owned["metrics"]["floating_profit"] == pytest.approx(100.0)
    assert owned["performance"]["total_profit"] == pytest.approx(997.5)
