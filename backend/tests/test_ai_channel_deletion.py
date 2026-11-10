"""真实删除：自有节点、频道授权、交易阻塞及确认范围。"""
import time
import asyncio
import json
import pytest

from sqlalchemy import select

from app.connections import manager
from app.db import SessionLocal
from app.orm import AuditLog, DeletedNode, Node, NodeCredential
from app.state import state
from tests.test_ai_channel_review import apply, approve, setup
from tests.test_ai_follow_api import _assign, _login, _mk_ai_strategy, _mk_group, client


class OnlineNode:
    async def close(self, **kwargs):
        pass


def online(client, node_id, **changes):
    manager.nodes[node_id] = OnlineNode()
    account = {"positions": [], "orders": [], "books_readable": True, "updated_at": time.time(), **changes}
    client.portal.call(state.store.save_account, node_id, account)


def test_follower_deletes_own_node_and_old_client_cannot_auto_register(client):
    data = setup(client)
    online(client, data["b_follow"])
    async def runtime_marks():
        for key in (f"node:pending_stop:{data['b_follow']}", f"limit_watch:done:{data['b_follow']}:7",
                    f"cancel_pending:ticket:{data['b_follow']}:8"):
            await state.store.r.set(key, "1")
        await state.store.r.set("signal:poll:rotation:XAUUSD", json.dumps([data["b_follow"], data["b_primary"]]))
    client.portal.call(runtime_marks)
    preview = client.get(f"/api/nodes/{data['b_follow']}/deletion-preview", headers=data["bob_h"])
    assert preview.status_code == 200, preview.text
    response = client.request("DELETE", f"/api/nodes/{data['b_follow']}", headers=data["bob_h"], json={"version": preview.json()["version"]})
    assert response.status_code == 200, response.text
    async def check():
        from app.node_service import auto_register
        async with SessionLocal() as session:
            tombstone = await session.scalar(select(DeletedNode).where(DeletedNode.node_id == data["b_follow"]))
            assert tombstone
            assert await session.get(Node, data["b_follow"]) is None
            assert await session.get(NodeCredential, data["b_follow"]) is None
        assert not [key async for key in state.store.r.scan_iter(match=f"*{data['b_follow']}*")]
        assert json.loads(await state.store.r.get("signal:poll:rotation:XAUUSD")) == [data["b_primary"]]
        try:
            await auto_register(state.store, tombstone.mt5_login)
        except ValueError:
            return
        raise AssertionError("deleted node auto-registered")
    client.portal.call(check)


def test_channel_deletes_rejected_members_system_node(client):
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    client.post(f"/api/ai-channel/requests/{request_id}/reject", headers=data["host_h"], json={})
    online(client, data["b_follow"])
    scope = {"group_id": data["ai_group_id"]}
    preview = client.post("/api/ai-channel/members/deletion-preview", headers=data["host_h"], json=scope)
    assert preview.status_code == 200, preview.text
    response = client.post("/api/ai-channel/members/delete", headers=data["host_h"], json={
        **scope, "version": preview.json()["version"], "node_ids": preview.json()["node_ids"],
    })
    assert response.status_code == 200, response.text
    assert client.portal.call(state.store.get_node, data["b_follow"]) is None


def test_group_delete_with_one_open_position_is_atomic(client):
    data = setup(client)
    apply(client, data, [data["b_primary"], data["b_follow"]])
    online(client, data["b_primary"])
    online(client, data["b_follow"], positions=[{"ticket": 1, "magic": 0}])
    scope = {"group_id": data["ai_group_id"]}
    preview = client.post("/api/ai-channel/members/deletion-preview", headers=data["host_h"], json=scope).json()
    assert preview["blocked"]
    response = client.post("/api/ai-channel/members/delete", headers=data["host_h"], json={
        **scope, "version": preview["version"], "node_ids": preview["node_ids"],
    })
    assert response.status_code == 409, response.text
    assert all(client.portal.call(state.store.get_node, nid) for nid in preview["node_ids"])
    async def check_audit():
        async with SessionLocal() as session:
            entry = await session.scalar(select(AuditLog).where(AuditLog.action == "preview_ai_channel_deletion"))
            assert entry.result == "failed"
            assert entry.after_json["blocked"][0]["reasons"] == ["仍有持仓"]
    client.portal.call(check_audit)


def test_foreign_node_and_stale_confirmation_are_rejected(client):
    data = setup(client)
    assert client.get(f"/api/nodes/{data['h_primary']}/deletion-preview", headers=data["bob_h"]).status_code == 404
    online(client, data["b_follow"])
    assert client.request("DELETE", f"/api/nodes/{data['b_follow']}", headers=data["bob_h"]).status_code == 422
    preview = client.get(f"/api/nodes/{data['b_follow']}/deletion-preview", headers=data["bob_h"]).json()
    assert client.patch(f"/api/nodes/{data['b_follow']}", headers=_login(client, "admin"), json={"name": "changed"}).status_code == 200
    response = client.request("DELETE", f"/api/nodes/{data['b_follow']}", headers=data["bob_h"], json={"version": preview["version"]})
    assert response.status_code == 409


@pytest.mark.parametrize("account", [
    {"orders": [{"ticket": 8}]}, {"books_readable": False}, {"updated_at": time.time() - 120},
])
def test_unknown_or_nonempty_books_block_deletion(client, account):
    data = setup(client)
    online(client, data["b_follow"], **account)
    preview = client.get(f"/api/nodes/{data['b_follow']}/deletion-preview", headers=data["bob_h"]).json()
    assert preview["blocked"]
    response = client.request("DELETE", f"/api/nodes/{data['b_follow']}", headers=data["bob_h"], json={"version": preview["version"]})
    assert response.status_code == 409


def test_queued_open_cannot_pass_a_committed_deletion(client, monkeypatch):
    from app import node_deletion
    from app.dispatcher import Dispatcher
    from app.parser import TradingSignal
    from app.rbac_service import load_principal
    data = setup(client)
    online(client, data["b_follow"])
    async def run():
        principal = await load_principal(state.store, "bob")
        node = await state.store.get_node(data["b_follow"])
        preview = await node_deletion.preview(state.store, principal, node_id=node["node_id"])
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = node_deletion._preview
        count = 0
        async def pause_under_lock(*args, **kwargs):
            nonlocal count
            count += 1
            result = await original(*args, **kwargs)
            if count == 2:
                entered.set()
                await proceed.wait()
            return result
        monkeypatch.setattr(node_deletion, "_preview", pause_under_lock)
        deletion = asyncio.create_task(node_deletion.execute(state.store, principal, preview["version"], [node["node_id"]], node_id=node["node_id"]))
        await entered.wait()
        opening = asyncio.create_task(Dispatcher(state.store).try_open(node, TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "during-delete", "symbol", {}))
        await asyncio.sleep(0)
        assert not opening.done()
        proceed.set()
        await deletion
        assert (await opening)["status"] == "skipped"
    client.portal.call(run)


def test_deleted_login_is_denied_even_if_redis_metadata_is_stale(client):
    from app import system_settings
    data = setup(client)
    online(client, data["b_follow"])
    before = client.portal.call(state.store.get_node, data["b_follow"])
    preview = client.get(f"/api/nodes/{data['b_follow']}/deletion-preview", headers=data["bob_h"]).json()
    assert client.request("DELETE", f"/api/nodes/{data['b_follow']}", headers=data["bob_h"], json={"version": preview["version"]}).status_code == 200
    client.portal.call(state.store.cache_node, before)
    token, _ = client.portal.call(system_settings.get_node_token, state.store)
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": before["mt5_login"]}})
        message = ws.receive_json()
        assert message["type"] == "auth_fail"
        assert message["data"]["reason"] == "node_deleted"


def test_preview_counts_other_robot_pending_and_requires_reconfirmation(client):
    data = setup(client)
    apply(client, data)
    online(client, data["b_follow"])
    scope = {"group_id": data["ai_group_id"]}
    before = client.post("/api/ai-channel/members/deletion-preview", headers=data["host_h"], json=scope).json()
    strategy_id = _mk_ai_strategy(client, data["host_h"], "second-ai")
    group_id = _mk_group(client, data["host_h"], [data["h_primary"]], "second", strategy_id, True)
    other = client.post("/api/ai-follow/join", headers=data["bob_h"], json={
        "leader_user_id": data["host"]["id"], "group_ids": [group_id], "node_ids": [data["b_follow"]],
    })
    assert other.status_code == 200, other.text
    preview = client.post("/api/ai-channel/members/deletion-preview", headers=data["host_h"], json=scope).json()
    assert preview["other_group_count"] == 1
    assert preview["binding_count"] == 2
    assert preview["cancelled_application_count"] == 2
    assert preview["version"] != before["version"]
    response = client.post("/api/ai-channel/members/delete", headers=data["host_h"], json={
        **scope, "version": before["version"], "node_ids": before["node_ids"],
    })
    assert response.status_code == 409
    response = client.post("/api/ai-channel/members/delete", headers=data["host_h"], json={
        **scope, "version": preview["version"], "node_ids": preview["node_ids"],
    })
    assert response.status_code == 200, response.text
    applications = client.get("/api/ai-follow/me", headers=data["bob_h"]).json()["applications"]
    assert len(applications) == 2
    assert all(row["status"] == "cancelled" for row in applications)


def test_transferred_node_is_removed_from_old_channel_deletion_scope(client):
    data = setup(client)
    request_id = apply(client, data, [data["b_follow"], data["b_primary"]])["pending"][0]["request_id"]
    approve(client, data, request_id)
    response = _assign(client, _login(client, "admin"), data["host"]["id"],
                       [data["h_primary"], data["h_extra"], data["b_follow"]], confirm=True)
    assert response.status_code == 200, response.text
    scope = {"group_id": data["ai_group_id"]}
    preview = client.post("/api/ai-channel/members/deletion-preview", headers=data["host_h"], json=scope)
    assert preview.status_code == 200, preview.text
    assert preview.json()["node_ids"] == [data["b_primary"]]
    assert client.portal.call(state.store.get_node, data["b_follow"])


def test_handshake_started_before_deletion_cannot_register_after_commit(client, monkeypatch):
    from app import node_credentials, node_deletion, system_settings, ws_gateway
    from app.rbac_service import load_principal
    data = setup(client)
    online(client, data["b_follow"])
    async def run():
        principal = await load_principal(state.store, "bob")
        node = await state.store.get_node(data["b_follow"])
        token, _ = await system_settings.get_node_token(state.store)
        preview = await node_deletion.preview(state.store, principal, node_id=node["node_id"])
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = node_credentials.legacy_allowed
        async def pause_handshake(node_id):
            entered.set()
            await proceed.wait()
            return await original(node_id)
        monkeypatch.setattr(node_credentials, "legacy_allowed", pause_handshake)
        class Handshake:
            scope = {}
            messages = []
            async def accept(self):
                pass
            async def receive_json(self):
                return {"type": "auth", "data": {"token": token, "mt5_login": node["mt5_login"]}}
            async def send_json(self, message):
                self.messages.append(message)
            async def close(self, **kwargs):
                pass
        socket = Handshake()
        authentication = asyncio.create_task(ws_gateway.node_ws(socket))
        await entered.wait()
        await node_deletion.execute(state.store, principal, preview["version"], preview["node_ids"], node_id=node["node_id"])
        proceed.set()
        await authentication
        assert socket.messages[-1] == {"type": "auth_fail", "data": {"reason": "node_deleted"}}
        assert not manager.is_node_online(node["node_id"])
        assert await state.store.get_node(node["node_id"]) is None
    client.portal.call(run)


def test_reenrolled_account_requires_scoped_token_and_keeps_all_old_nodes_denied(client):
    from app import node_credentials
    data = setup(client)
    node = client.portal.call(state.store.get_node, data["b_follow"])
    online(client, node["node_id"])
    preview = client.get(f"/api/nodes/{node['node_id']}/deletion-preview", headers=data["bob_h"]).json()
    assert client.request("DELETE", f"/api/nodes/{node['node_id']}", headers=data["bob_h"], json={"version": preview["version"]}).status_code == 200
    admin_h = _login(client, "admin")
    response = client.post("/api/nodes", headers=admin_h, json={"name": "reenrolled", "mt5_login": node["mt5_login"]})
    assert response.status_code == 201, response.text
    new_id = response.json()["node_id"]
    assert client.portal.call(node_credentials.deleted_login_denied, node["mt5_login"], new_id) is False
    assert client.portal.call(node_credentials.legacy_allowed, new_id) is False
    online(client, new_id)
    preview = client.get(f"/api/nodes/{new_id}/deletion-preview", headers=admin_h).json()
    assert client.request("DELETE", f"/api/nodes/{new_id}", headers=admin_h, json={"version": preview["version"]}).status_code == 200
    for old_id in (node["node_id"], new_id):
        assert client.portal.call(node_credentials.deleted_login_denied, node["mt5_login"], old_id) is True
