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
        group = client.portal.call(state.store.get_group, robot["group_id"])
        assert robot["symbol"] == "XAUUSD"
        assert robot["started_at"] == group["created_at"]
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


def test_group_summaries_batch_matches_per_group_leader_batch(client):
    """`group_summaries_batch`（一次查询）必须与逐个 `leader_batch` 口径**完全一致**。

    列表接口把「逐个机器人 leader_batch」（N+1，分组一多就卡）换成了
    「一次查询算完所有分组」(`group_summaries_batch`)。这条红线保证替换后
    每个机器人卡片上的收益 / 胜率 / 笔数与原来逐个算的**一模一样**，
    不会因为「一次查」而混入别的分组或丢掉分批出场的持仓。
    """
    import asyncio

    from app import deal_stats

    setup = setup_two_robots(client)
    # 两个分组各一个 magic；第一组再放一笔「同一持仓分两次出场」验证持仓级聚合
    entries = [(setup["ai_group_id"], 701, 40.0), (setup["second_group_id"], 702, -15.0)]

    async def seed():
        async with SessionLocal() as session:
            for group_id, magic, _realized in entries:
                task = GroupSignalTask(signal_id=f"g-{magic}", group_id=group_id, strategy_snapshot_json={"template_id": "tpl_1"}, status="done")
                session.add(task)
                await session.flush()
                session.add(GroupTaskDispatch(task_id=task.task_id, signal_id=task.signal_id, group_id=group_id, node_id=setup["h_primary"], magic=magic, status="done"))
            await session.commit()
        now = int(time.time())
        # 第一组：同一持仓 pid=1 分两笔出场（+30 +10 => 一笔盈利持仓，不拆成两笔）
        await deal_store.save_deals(setup["h_primary"], [
            {"deal_id": 7011, "position_id": 1, "magic": 701, "entry_type": "OUT", "profit": 30.0, "volume": 0.1, "symbol": "XAUUSD", "time": now, "equity": 10000.0},
            {"deal_id": 7012, "position_id": 1, "magic": 701, "entry_type": "OUT", "profit": 10.0, "volume": 0.1, "symbol": "XAUUSD", "time": now + 60, "equity": 10000.0},
        ])
        # 第二组：一笔亏损持仓
        await deal_store.save_deals(setup["h_primary"], [
            {"deal_id": 7021, "position_id": 2, "magic": 702, "entry_type": "OUT", "profit": -15.0, "volume": 0.1, "symbol": "XAUUSD", "time": now, "equity": 10000.0},
        ])

    async def compare():
        now = int(time.time())
        from_ts = now - 30 * 86400
        scopes = {
            setup["ai_group_id"]: {setup["h_primary"]: {701}},
            setup["second_group_id"]: {setup["h_primary"]: {702}},
        }
        batch = await deal_stats.group_summaries_batch(scopes, from_ts)
        # 逐个 leader_batch（旧口径）对照
        for gid, magic in ((setup["ai_group_id"], 701), (setup["second_group_id"], 702)):
            single = await deal_stats.leader_batch(
                [setup["h_primary"]], from_ts, None,
                node_magics={setup["h_primary"]: {magic}},
            )
            assert batch[gid] == single, f"分组 {gid} 批量结果与逐个 leader_batch 不一致: {batch[gid]} vs {single}"
        # 分组隔离：第一组 +40（一个盈利持仓），第二组 -15
        assert batch[setup["ai_group_id"]]["total_profit"] == pytest.approx(40.0)
        assert batch[setup["ai_group_id"]]["total_trades"] == 1, "分批出场应按持仓计一笔，不是两笔"
        assert batch[setup["ai_group_id"]]["win_rate"] == pytest.approx(100.0)
        assert batch[setup["second_group_id"]]["total_profit"] == pytest.approx(-15.0)
        assert batch[setup["second_group_id"]]["win_rate"] == pytest.approx(0.0)

    client.portal.call(seed)
    client.portal.call(compare)


def test_robot_stats_refresh_runs_without_leaders(client, monkeypatch):
    """没有合格带单员时，机器人绩效预聚合**仍要刷新**（早期版本会整体跳过）。

    `refresh()` 早退条件曾经是 `if not leader_ids: return 0`，会连带跳过
    AI 机器人统计。现在 leader 部分独立判断，robot 部分不受影响。
    这里把「所有用户都没有 follow_leader」造出来，断言 robot 刷新仍被调用。
    """
    import asyncio

    from app import deal_agg

    async def _run():
        store = state.store
        agg = deal_agg.LeaderStatsAggregator(store)
        calls = {"robot": 0}

        async def _fake_all_nodes():
            return [{"node_id": "nd_primary", "owner_user_id": 999, "is_primary": True}]

        def _fake_primary(all_nodes):
            # 模拟存在一个主节点
            return {999: ["nd_primary"]}

        async def _fake_robot(store_, primary_nodes):
            calls["robot"] += 1
            return len(primary_nodes)

        monkeypatch.setattr(deal_agg, "primary_nodes_of", _fake_primary)
        monkeypatch.setattr(agg, "_refresh_robot_stats", _fake_robot)

        async def _no_leaders():
            return []

        async def _no_followers(ids):
            return []

        # 让「没有任何带单员」：list_users 返回空 -> leader_ids 为空
        monkeypatch.setattr(deal_agg.user_service, "list_users", _no_leaders)
        monkeypatch.setattr(deal_agg.follow_service, "list_for_leaders", _no_followers)
        monkeypatch.setattr(store, "all_nodes", _fake_all_nodes)

        await agg.refresh()
        assert calls["robot"] == 1, "没有带单员时机器人统计仍必须刷新一次"

    client.portal.call(_run)
