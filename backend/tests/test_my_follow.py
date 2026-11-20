"""本人跟单的市场隔离、状态持久化与收益窗口。"""
from datetime import datetime, timedelta

from sqlalchemy import select

from app import group_service, my_follow
from app.db import SessionLocal
from app.orm import AuditLog, Deal, GroupSignalTask, GroupTaskDispatch, TradingStrategy
from app.state import state
from tests.test_follow_api import _mk_group_with_strategy, _mk_strategy
from tests.test_follow_review import apply, approve, client, setup


def test_state_is_owned_atomic_idempotent_and_survives_warmup(client):
    d = setup(client)
    nodes = [d["node"], d["bob_primary"]]
    requests = apply(client, d, nodes)["pending"]
    pairs = [{"group_id": d["group"], "node_id": node} for node in nodes]

    def change(relations, paused=True, headers=None):
        return client.post("/api/follow/me/state", headers=headers or d["follower"],
                           json={"paused": paused, "relations": relations})

    approve(client, d, requests[0]["request_id"])
    assert change(pairs).status_code == 409
    approve(client, d, requests[1]["request_id"])
    assert change(pairs, headers=d["leader"]).status_code == 409
    assert change(pairs + pairs).json() == {"changed": 2}
    assert change(pairs).json() == {"changed": 0}
    client.portal.call(group_service.warm_cache, state.store)
    rows = client.portal.call(state.store.get_group, d["group"])["followers"]
    assert {r["follow_state"] for r in rows} == {"paused"}
    assert change(pairs[:1], False).json() == {"changed": 1}
    assert change([pairs[1], {**pairs[0], "node_id": d["primary"]}], False).status_code == 409

    async def switch_market():
        async with SessionLocal() as session:
            assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "follow_resume_mine"))
            strategy = await session.get(TradingStrategy, d["strategy"])
            strategy.template_id = "tpl_1"
            await session.commit()
    client.portal.call(switch_market)
    assert change(pairs).status_code == 409


def test_returns_only_current_execution_and_deduplicates_first_balance(client, monkeypatch):
    d = setup(client)
    strategy = _mk_strategy(client, d["leader"], "第二种策略", "tpl_2")
    second = _mk_group_with_strategy(client, d["leader"], [d["primary"]], "第二组", strategy)
    for row in apply(client, d, [d["node"], d["bob_primary"]], [d["group"], second])["pending"]:
        approve(client, d, row["request_id"])

    async def ledger():
        async with SessionLocal() as session:
            now = datetime.now() + timedelta(seconds=2)
            async def trade(group, node, magic, seconds, profit, balance, equity):
                dt = now + timedelta(seconds=seconds)
                task = GroupSignalTask(signal_id=str(magic), group_id=group,
                                       strategy_snapshot_json={"template_id": "tpl_2"})
                session.add(task)
                await session.flush()
                first_id = magic * 10
                dispatch = GroupTaskDispatch(task_id=task.task_id, signal_id=str(magic),
                    group_id=group, node_id=node, magic=magic, status="running",
                    deal=first_id, opened_at=dt, dispatched_at=dt, opening_balance=balance)
                session.add(dispatch)
                for index, entry in enumerate(("IN", "OUT")):
                    session.add(Deal(node_id=node, magic=0 if index and magic == 101 else magic,
                        deal_id=first_id + index, position_id=magic,
                        entry_type=entry, direction="BUY" if not index else "SELL", symbol="XAUUSD",
                        reason=5, price=2400, volume=1 if not index else .5,
                        net_profit=profit if index else 0, equity=equity, deal_time=int(dt.timestamp()) + index))
                await session.flush()
                return dispatch.id
            old = await trade(d["group"], d["node"], 100, -86400, 999, 5000, 5000)
            current = await trade(d["group"], d["node"], 101, 0, 10, 1000, 1000)
            await trade(second, d["node"], 102, 60, 20, 800, 1000)
            await trade(d["group"], d["bob_primary"], 103, 30, 30, 2000, 1000)
            await session.commit()
        assert await my_follow.own_dispatches(d["bob"]["id"], d["node"], d["group"], [old, current]) == {current}
    client.portal.call(ledger)
    client.portal.call(state.store.save_account, d["node"], {"positions": [
        {"ticket": 4001, "magic": 101, "volume": .5}, {"ticket": 4002, "magic": 100, "volume": .5},
    ]})
    data = client.get("/api/follow/me", headers=d["follower"]).json()
    assert data["summary"]["net_profit"] == 60
    assert data["summary"]["average_rate"] == 2
    assert data["summary"]["first_balance"] == 3000
    groups = {g["group_id"]: g for g in data["following"][0]["groups"]}
    assert groups[d["group"]]["magics"][d["node"]] == [101]
    assert [r["deal_id"] for r in groups[d["group"]]["history"]] == [1031, 1011]
    assert all(r["volume"] == .5 and r["status"] == "已止盈" for r in groups[d["group"]]["history"])
    assert groups[second]["last_sync"] > groups[d["group"]]["last_sync"]
    assert [p["ticket"] for n in groups[d["group"]]["my_nodes"] for p in n["positions"]] == [4001]
    from app.connections import manager
    from tests.test_follow_pause_dispatch import capture_sender
    sent = []
    monkeypatch.setitem(manager.nodes, d["node"], object())
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    url = f"/api/nodes/{d['node']}/follow-channels/close"
    body = {"market": "follow", "mode": "ticket", "ticket": 4001, "group_id": d["group"]}
    assert client.post(url, headers=d["follower"], json={**body, "group_id": second}).status_code == 400
    assert client.post(url, headers=d["follower"], json={**body, "ticket": 4002}).status_code == 400
    assert not sent
    assert client.post(url, headers=d["follower"], json=body).json()["closed"] == 1
    assert len(sent) == 1
    leave = {"leader_user_id": d["alice"]["id"], "node_ids": [d["node"]], "group_ids": [second], "close_tasks": False}
    kept = client.post("/api/follow/leave", headers=d["follower"], json=leave).json()
    assert kept["removed"] and not kept["closing"] and len(sent) == 1
    stopped = client.post("/api/follow/leave", headers=d["follower"],
                          json={**leave, "group_ids": [d["group"]], "close_tasks": True}).json()
    assert len(stopped["closing"]) == 1 and len(sent) == 2
    assert sent[-1][1]["magic"] == 101
