import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app import node_deletion_jobs, node_lifecycle, persist
from app.db import SessionLocal
from app.orm import AuditLog, DeletedNode, PendingNodeDeletion, SignalDispatch
from app.parser import TradingSignal
from app.state import state
from tests.test_ai_follow_api import _assign, _mk_user
from tests.test_api import _auth, _node_token, client


def report(ws, login, *, positions=None, orders=None, readable=True):
    ws.send_json({"type": "account", "data": {
        "account": {"login": login, "balance": 1000}, "books_ok": readable,
        "positions": positions or [], "orders": orders or [],
    }})
    ws.send_json({"type": "heartbeat", "data": {}})


def test_offline_deletion_restores_retries_and_waits_until_confirmed_flat(client):
    headers = _auth(client)
    login = 735001
    node_id = client.post("/api/nodes", headers=headers, json={"name": "待删除", "mt5_login": login}).json()["node_id"]
    url = f"/api/nodes/{node_id}"
    preview = client.get(f"{url}/deletion-preview", headers=headers).json()
    assert "节点离线" in preview["blocked"][0]["reasons"]
    for _ in range(2):
        response = client.request("DELETE", url, headers=headers, json={"version": preview["version"]})
        assert response.status_code == 202, response.text
        assert response.json() == {"status": "pending", "node_ids": [node_id]}
    assert client.get(url, headers=headers).json()["deletion_pending"] is True

    async def restore_and_check_dispatch():
        node_lifecycle.pending_deletions.clear()
        await node_deletion_jobs.restore_pending_deletions()
        node = await state.store.get_node(node_id)
        signal = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)
        normal = await state.dispatcher.try_open(node, signal, "normal", "symbol", {})
        await state.store.set_group_node_busy("group", node_id, "1", 3600)
        strategy = await state.group_dispatcher._dispatch_locked_node(
            {"node_id": node_id, "group_id": "group", "decided_vol": 0.1}, {},
        )
        assert normal["reason"] == strategy["reason"] == "节点等待删除"
        assert not await state.store.r.exists(f"group:node:busy:group:{node_id}")
        async with SessionLocal() as session:
            persist.add_manual_close(session, "unreported", node_id, "all")
            await session.commit()

    client.portal.call(restore_and_check_dispatch)
    token = _node_token(client, headers)
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": login}})
        assert ws.receive_json()["type"] == "auth_ok"

        report(ws, login, readable=False)
        assert ws.receive_json()["type"] == "pong"
        report(ws, login, positions=[{"ticket": 1}], orders=[{"ticket": 2}])
        first = ws.receive_json()
        assert first["cmd"] == "close" and first["close_target"] == "all"
        assert ws.receive_json()["type"] == "pong"

        report(ws, login)
        assert ws.receive_json()["type"] == "pong"
        assert client.get(url, headers=headers).json()["deletion_pending"] is True

        ws.send_json({"type": "trade_result", "data": {
            "signal_id": first["signal_id"], "action": "CLOSE", "success": False, "error": "market closed",
        }})
        report(ws, login, orders=[{"ticket": 2}])
        assert ws.receive_json()["type"] == "pong"
        assert client.get(url, headers=headers).json()["deletion_pending"] is True

        async def allow_retry():
            async with SessionLocal() as session:
                job = await session.get(PendingNodeDeletion, node_id)
                job.last_attempt_at = 0
                await session.commit()

        client.portal.call(allow_retry)
        report(ws, login, orders=[{"ticket": 2}])
        retry = ws.receive_json()
        assert retry["cmd"] == "close" and retry["signal_id"] != first["signal_id"]
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "trade_result", "data": {
            "signal_id": retry["signal_id"], "action": "CLOSE", "success": True,
        }})
        report(ws, login, orders=[{"ticket": 2}])
        assert ws.receive_json()["type"] == "pong"
        assert client.get(url, headers=headers).status_code == 200

        report(ws, login)
        with pytest.raises(WebSocketDisconnect) as disconnect:
            ws.receive_json()
        assert disconnect.value.code == 4401

    assert client.get(url, headers=headers).status_code == 404

    async def check_ledger():
        async with SessionLocal() as session:
            assert await session.get(PendingNodeDeletion, node_id) is None
            assert (await session.get(DeletedNode, login)).node_id == node_id
            records = (await session.scalars(select(SignalDispatch).where(SignalDispatch.node_id == node_id))).all()
            assert {row.signal_id: row.status for row in records} == {
                "unreported": "failed", first["signal_id"]: "failed", retry["signal_id"]: "done",
            }
            actions = await session.scalars(select(AuditLog.action).where(AuditLog.target == node_id))
            assert {"request_node_deletion", "close_for_node_deletion", "delete_node"} <= set(actions)

    client.portal.call(check_ledger)


def test_queued_deletion_is_cancelled_if_node_changes_owner(client, monkeypatch):
    from app import ws_gateway

    headers = _auth(client)
    owner = _mk_user(client, headers, "new-owner")
    login = 735002
    node_id = client.post("/api/nodes", headers=headers, json={"name": "重新分配", "mt5_login": login}).json()["node_id"]
    url = f"/api/nodes/{node_id}"
    preview = client.get(f"{url}/deletion-preview", headers=headers).json()
    assert client.request("DELETE", url, headers=headers, json={"version": preview["version"]}).status_code == 202
    assert _assign(client, headers, owner["id"], [node_id]).status_code == 200

    resumed = []

    async def resume(nid, ws):
        resumed.append(nid)

    monkeypatch.setattr(ws_gateway, "_resume_strategy_tasks", resume)
    token = _node_token(client, headers)
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": login}})
        assert ws.receive_json()["type"] == "auth_ok"
        report(ws, login, orders=[{"ticket": 2}])
        assert ws.receive_json()["type"] == "pong"
        assert resumed == [node_id]

    node = client.get(url, headers=headers).json()
    assert node["owner_user_id"] == owner["id"] and node["deletion_pending"] is False
