"""带单申请、逐节点审批及频道成员控制的集成保证。"""
import asyncio

import pytest
from sqlalchemy import delete, select

from app import follow_requests, follow_service, group_service
from app.db import SessionLocal
from app.orm import AuditLog, FollowRequest, GroupFollowerNode, Node, SystemSetting
from app.state import state
from tests.test_follow_api import (
    _assign, _bind_role, _group_with_primary, _login, _mk_node, _mk_strategy, _mk_user, _setup_leader_and_follower, client,
)


def setup(client):
    admin = _login(client, "admin")
    alice, bob, primary, extra, bob_primary, node, group = _setup_leader_and_follower(client, admin)
    leader = _login(client, "alice")
    strategy = _mk_strategy(client, leader, "审核趋势", "tpl_2")
    response = client.patch(f"/api/groups/{group}", headers=leader, json={"strategy_id": strategy})
    assert response.status_code == 200, response.text
    return {"admin": admin, "leader": leader, "follower": _login(client, "bob"),
            "alice": alice, "bob": bob, "primary": primary, "extra": extra,
            "bob_primary": bob_primary, "node": node, "group": group, "strategy": strategy}


def apply(client, d, nodes=None, groups=None):
    response = client.post("/api/follow/join", headers=d["follower"], json={
        "leader_user_id": d["alice"]["id"], "group_ids": groups or [d["group"]], "node_ids": nodes or [d["node"]],
    })
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, d, request_id):
    response = client.post(f"/api/follow-channel/requests/{request_id}/approve", headers=d["leader"])
    assert response.status_code == 200, response.text
    return response.json()


def test_pending_is_separate_and_primary_nodes_are_allowed(client):
    d = setup(client)
    result = apply(client, d, [d["node"], d["bob_primary"]])
    assert result["joined"] == [] and len(result["pending"]) == 2
    assert client.portal.call(state.store.get_group, d["group"])["followers"] == []
    mine = client.get("/api/follow/me", headers=d["follower"]).json()
    assert mine["following"] == [] and mine["summary"]["pending_node_count"] == 2
    assert len(mine["applications"]) == 2
    approve(client, d, result["pending"][0]["request_id"])
    assert approve(client, d, result["pending"][0]["request_id"])["skipped"]
    assert len(client.portal.call(state.store.get_group, d["group"])["followers"]) == 1
    assert len(apply(client, d, [d["node"], d["bob_primary"]])["skipped"]) == 2


def test_reject_reapply_and_review_permissions(client):
    d = setup(client)
    request_id = apply(client, d)["pending"][0]["request_id"]
    assert client.post(f"/api/follow-channel/requests/{request_id}/approve", headers=d["follower"]).status_code == 403
    assert client.post(f"/api/follow-channel/requests/{request_id}/approve", headers=d["admin"]).status_code == 404
    response = client.post(f"/api/follow-channel/requests/{request_id}/reject", headers=d["leader"], json={"reason": "暂不接收"})
    assert response.status_code == 200
    assert client.get("/api/follow/me", headers=d["follower"]).json()["applications"][0]["reason"] == "暂不接收"
    new_id = apply(client, d)["pending"][0]["request_id"]
    assert new_id != request_id
    approve(client, d, new_id)


def test_batch_is_partial_and_revalidates_ownership(client):
    d = setup(client)
    pending = apply(client, d, [d["node"], d["bob_primary"]])["pending"]
    async def transfer():
        async with SessionLocal() as session:
            node = await session.get(Node, d["node"])
            node.owner_user_id = d["alice"]["id"]
            await session.commit()
    client.portal.call(transfer)
    response = client.post("/api/follow-channel/requests/approve-batch", headers=d["leader"],
                           json={"request_ids": [r["request_id"] for r in pending]})
    assert response.status_code == 200, response.text
    assert len(response.json()["approved"]) == len(response.json()["failed"]) == 1
    assert client.portal.call(state.store.get_group, d["group"])["followers"][0]["node_id"] == d["bob_primary"]


def test_channel_pause_remove_and_cache_recovery(client):
    d = setup(client)
    request_id = apply(client, d)["pending"][0]["request_id"]
    approve(client, d, request_id)
    assert client.post("/api/follow-channel/members/state", headers=d["leader"],
                       json={"request_ids": [request_id], "paused": True}).status_code == 200
    client.portal.call(group_service.warm_cache, state.store)
    group = client.portal.call(state.store.get_group, d["group"])
    from app.follow_rules import group_for_follow_dispatch
    assert group_for_follow_dispatch(group, "tpl_2")["followers"] == []
    client.portal.call(state.store.set_group_node_busy, d["group"], d["node"], "123", 120)
    response = client.post("/api/follow-channel/members/remove", headers=d["leader"], json={"request_ids": [request_id]})
    assert response.status_code == 200, response.text
    assert client.portal.call(state.store.get_group_node_busy, d["group"], d["node"]) == "123"
    assert client.portal.call(state.store.get_node, d["node"])
    assert client.portal.call(state.store.get_group, d["group"])["followers"] == []
    assert apply(client, d)["pending"]


def test_new_group_inheritance_requires_review(client):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    target = _group_with_primary(client, d["leader"], [d["primary"]], "新带单分组")
    response = client.post(f"/api/groups/{target}/adopt-followers", headers=d["leader"])
    assert response.status_code == 200, response.text
    assert response.json()["adopted"][0]["status"] == "pending"
    assert client.portal.call(state.store.get_group, target)["followers"] == []
    approve(client, d, response.json()["adopted"][0]["request_id"])
    assert len(client.portal.call(state.store.get_group, target)["followers"]) == 1


def test_following_survives_primary_promotion(client):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    response = client.post(f"/api/nodes/{d['node']}/primary", headers=d["admin"], json={"is_primary": True})
    assert response.status_code == 200, response.text
    assert client.portal.call(state.store.get_group, d["group"])["followers"][0]["node_id"] == d["node"]


def test_legacy_backfill_keeps_relations_and_is_idempotent(client):
    d = setup(client)
    async def seed():
        await follow_service.add_following(state.store, group_id=d["group"], node_id=d["node"],
            follower_user_id=d["bob"]["id"], leader_user_id=d["alice"]["id"], leader_primary_node_id=d["primary"])
        async with SessionLocal() as session:
            await session.execute(delete(SystemSetting).where(SystemSetting.key == follow_requests.MIGRATION_KEY))
            await session.commit()
        await follow_requests.migrate_legacy()
        await follow_requests.migrate_legacy()
        async with SessionLocal() as session:
            return (await session.scalars(select(FollowRequest))).all()
    rows = client.portal.call(seed)
    assert len(rows) == 1 and rows[0].status == "approved" and rows[0].source == "legacy"
    assert len(client.portal.call(state.store.get_group, d["group"])["followers"]) == 1


def test_channel_search_pagination_and_ui_audit(client):
    d = setup(client)
    apply(client, d, [d["node"], d["bob_primary"]])
    body = client.get("/api/follow-channel/requests", headers=d["leader"], params={"q": "bob", "page_size": 1}).json()
    assert body["total"] == 1 and len(body["applicants"][0]["nodes"]) == 2
    assert len(body["all_pending_request_ids"]) == 2
    assert client.get("/api/follow-channel/profile", headers=d["leader"]).json()["pending_nodes"] == 2
    other = _mk_user(client, d["admin"], "charlie")
    other_node = _mk_node(client, d["admin"], "charlie-follow", 92005, primary=False)
    assert _assign(client, d["admin"], other["id"], [other_node], True).status_code == 200
    apply(client, {**d, "follower": _login(client, "charlie"), "node": other_node})
    all_pages = client.get("/api/follow-channel/requests", headers=d["leader"], params={"page_size": 1}).json()
    assert all_pages["total"] == 2 and len(all_pages["applicants"]) == 1
    assert len(all_pages["all_pending_request_ids"]) == 3
    scoped = client.get("/api/follow-channel/requests", headers=d["leader"], params={"q": "bob", "page_size": 1}).json()
    reviewed = client.post("/api/follow-channel/requests/approve-batch", headers=d["leader"],
                           json={"request_ids": scoped["all_pending_request_ids"]})
    assert len(reviewed.json()["approved"]) == 2
    remaining = client.get("/api/follow-channel/requests", headers=d["leader"]).json()
    assert remaining["applicants"][0]["applicant_name"] == "charlie" and len(remaining["all_pending_request_ids"]) == 1
    assert client.post("/api/follow-channel/ui-events", headers=d["follower"], json={"action": "open_picker"}).status_code == 200
    async def audits():
        async with SessionLocal() as session:
            return (await session.scalars(select(AuditLog.action).where(AuditLog.category == "follow"))).all()
    actions = client.portal.call(audits)
    assert "follow_request_create" in actions and "follow_ui_open_picker" in actions


def test_concurrent_duplicate_application_and_audit_rollback(client, monkeypatch):
    d = setup(client)
    async def concurrent():
        from app import rbac_service
        principal = await rbac_service.load_principal(state.store, "bob")
        return await asyncio.gather(*(follow_requests.submit(state.store, principal, d["alice"]["id"],
            [d["group"]], [d["node"]]) for _ in range(3)))
    results = client.portal.call(concurrent)
    assert sum(len(result["pending"]) for result in results) == 1
    request_id = next(row["request_id"] for result in results for row in result["pending"])
    original_audit = follow_requests.audit
    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(follow_requests, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(f"/api/follow-channel/requests/{request_id}/approve", headers=d["leader"])
    async def persisted():
        async with SessionLocal() as session:
            request = await session.get(FollowRequest, request_id)
            relations = (await session.scalars(select(GroupFollowerNode))).all()
            return request.status, relations
    assert client.portal.call(persisted) == ("pending", [])
    monkeypatch.setattr(follow_requests, "audit", original_audit)
    original_refresh = follow_service._refresh_groups
    async def fail_refresh(*args, **kwargs):
        raise RuntimeError("cache unavailable")
    monkeypatch.setattr(follow_service, "_refresh_groups", fail_refresh)
    with pytest.raises(RuntimeError, match="cache unavailable"):
        client.post(f"/api/follow-channel/requests/{request_id}/approve", headers=d["leader"])
    monkeypatch.setattr(follow_service, "_refresh_groups", original_refresh)
    assert approve(client, d, request_id)["skipped"]
    assert len(client.portal.call(state.store.get_group, d["group"])["followers"]) == 1


def test_source_switch_invalidates_pending_and_preserves_other_nodes(client):
    d = setup(client)
    apply(client, d)
    response = client.post(f"/api/nodes/{d['extra']}/primary", headers=d["admin"], json={"is_primary": True})
    assert response.status_code == 200, response.text
    application = client.get("/api/follow/me", headers=d["follower"]).json()["applications"][0]
    assert application["status"] == "cancelled" and "主节点已变更" in application["reason"]
    assert client.portal.call(state.store.get_node, d["node"])


def test_channel_operations_do_not_change_other_groups(client):
    d = setup(client)
    target = _group_with_primary(client, d["leader"], [d["primary"]], "另一个带单分组")
    pending = apply(client, d, groups=[d["group"], target])["pending"]
    for row in pending:
        approve(client, d, row["request_id"])
    selected = next(row for row in pending if row["group_id"] == d["group"])
    response = client.post("/api/follow-channel/members/state", headers=d["leader"],
                           json={"request_ids": [selected["request_id"]], "paused": True})
    assert response.status_code == 200
    assert client.portal.call(state.store.get_group, target)["followers"][0]["leader_paused"] is False
    assert client.post("/api/follow-channel/members/remove", headers=d["leader"],
                       json={"request_ids": [selected["request_id"]]}).status_code == 200
    assert client.portal.call(state.store.get_group, target)["followers"][0]["node_id"] == d["node"]


def test_disabled_group_cancels_pending_and_stops_running_count(client):
    d = setup(client)
    pending = apply(client, d, [d["node"], d["bob_primary"]])["pending"]
    approve(client, d, pending[0]["request_id"])
    response = client.patch(f"/api/groups/{d['group']}", headers=d["leader"], json={"enabled": False})
    assert response.status_code == 200
    profile = client.get("/api/follow-channel/profile", headers=d["leader"]).json()
    assert profile["pending_nodes"] == profile["following_nodes"] == 0
    mine = client.get("/api/follow/me", headers=d["follower"]).json()
    assert {row["status"] for row in mine["applications"]} == {"approved", "cancelled"}
    assert all(not row["marketable"] for row in mine["applications"])


def test_pending_follows_node_transfer_and_leader_permission_revocation(client):
    d = setup(client)
    pending = apply(client, d, [d["node"], d["bob_primary"]])["pending"]
    response = _assign(client, d["admin"], d["alice"]["id"], [d["primary"], d["extra"], d["node"]], True)
    assert response.status_code == 200, response.text
    async def statuses():
        async with SessionLocal() as session:
            return {row.node_id: row.status for row in (await session.scalars(select(FollowRequest))).all()}
    assert client.portal.call(statuses) == {d["node"]: "cancelled", d["bob_primary"]: "pending"}
    plain = next(role for role in client.get("/api/roles", headers=d["admin"]).json() if role["code"] == "plain")
    _bind_role(client, d["admin"], d["alice"]["id"], plain["id"])
    assert set(client.portal.call(statuses).values()) == {"cancelled"}
    assert client.post(f"/api/follow-channel/requests/{pending[1]['request_id']}/approve", headers=d["leader"]).status_code == 403


def test_removing_source_member_cancels_pending_application(client):
    d = setup(client)
    apply(client, d)
    response = client.patch(f"/api/groups/{d['group']}", headers=d["leader"], json={"node_ids": [d["extra"]]})
    assert response.status_code == 200, response.text
    row = client.get("/api/follow/me", headers=d["follower"]).json()["applications"][0]
    assert row["status"] == "cancelled" and "带单源成员" in row["reason"]
