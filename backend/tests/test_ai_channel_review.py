"""AI 申请审核、迁移与频道操作的接口保证。"""
import asyncio

from sqlalchemy import delete, select

from app import ai_application_store, group_service
from app.db import SessionLocal
from app.orm import AiFollowApplication, AuditLog, GroupFollowerNode, SystemSetting
from app.state import state
from tests.test_ai_follow_api import CAP_AI, CAP_LEADER, _login, _setup, client


def setup(client):
    data = _setup(client, _login(client, "admin"), caps=[CAP_AI, CAP_LEADER])
    data["bob_h"] = _login(client, "bob")
    return data


def apply(client, data, nodes=None):
    response = client.post("/api/ai-follow/join", headers=data["bob_h"], json={
        "leader_user_id": data["host"]["id"], "group_ids": [data["ai_group_id"]],
        "node_ids": nodes or [data["b_follow"]],
    })
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, data, request_id):
    response = client.post(f"/api/ai-channel/requests/{request_id}/approve", headers=data["host_h"])
    assert response.status_code == 200, response.text
    return response.json()


def test_pending_is_physically_separate_and_approval_is_idempotent(client):
    data = setup(client)
    result = apply(client, data, [data["b_primary"], data["b_follow"]])
    assert result["joined"] == []
    request_id = result["pending"][0]["request_id"]
    assert client.get("/api/ai-follow/me", headers=data["bob_h"]).json()["following"] == []
    group = client.portal.call(state.store.get_group, data["ai_group_id"])
    assert group["followers"] == []
    approve(client, data, request_id)
    assert approve(client, data, request_id)["skipped"]
    group = client.portal.call(state.store.get_group, data["ai_group_id"])
    assert {row["node_id"] for row in group["followers"]} == {data["b_primary"], data["b_follow"]}


def test_duplicate_pending_reject_and_reapply(client):
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    assert apply(client, data)["pending"] == []
    response = client.post(f"/api/ai-channel/requests/{request_id}/reject", headers=data["host_h"], json={"reason": "暂不接入"})
    assert response.status_code == 200, response.text
    second = apply(client, data)["pending"][0]["request_id"]
    assert second != request_id
    approve(client, data, second)


def test_application_is_atomic_if_one_node_is_disabled(client):
    data = setup(client)
    request_id = apply(client, data, [data["b_primary"], data["b_follow"]])["pending"][0]["request_id"]
    async def disable():
        from app.orm import Node
        async with SessionLocal() as session:
            node = await session.get(Node, data["b_follow"])
            node.enabled = False
            await session.commit()
    client.portal.call(disable)
    response = client.post(f"/api/ai-channel/requests/{request_id}/approve", headers=data["host_h"])
    assert response.status_code == 409
    assert client.portal.call(state.store.get_group, data["ai_group_id"])["followers"] == []


def test_channel_owner_pause_does_not_override_follower_pause(client):
    data = setup(client)
    approve(client, data, apply(client, data)["pending"][0]["request_id"])
    payload = {"group_id": data["ai_group_id"], "paused": True}
    assert client.post("/api/ai-channel/members/state", headers=data["host_h"], json=payload).status_code == 200
    client.portal.call(group_service.warm_cache, state.store)
    assert client.portal.call(state.store.get_group, data["ai_group_id"])["followers"][0]["leader_paused"] is True
    assert client.post("/api/ai-follow/nodes/state", headers=data["bob_h"], json={
        "node_ids": [data["b_follow"]], "state": "paused",
    }).status_code == 200
    payload["paused"] = False
    assert client.post("/api/ai-channel/members/state", headers=data["host_h"], json=payload).status_code == 200
    group = client.portal.call(state.store.get_group, data["ai_group_id"])
    from app.follow_rules import group_for_follow_dispatch
    assert group_for_follow_dispatch(group, "tpl_1")["followers"] == []
    assert group["followers"][0]["follow_state"] == "paused"
    async def change_ownership():
        from app.orm import Node
        async with SessionLocal() as session:
            node = await session.get(Node, data["b_follow"])
            node.owner_user_id = data["host"]["id"]
            await session.commit()
    client.portal.call(change_ownership)
    payload["paused"] = True
    assert client.post("/api/ai-channel/members/state", headers=data["host_h"], json=payload).status_code == 409
    assert client.portal.call(state.store.get_group, data["ai_group_id"])["followers"][0]["leader_paused"] is False


def test_reviewed_applicant_rows_merge_batches_and_profile_counts_requests(client):
    data = setup(client)
    approve(client, data, apply(client, data)["pending"][0]["request_id"])
    approve(client, data, apply(client, data, [data["b_primary"]])["pending"][0]["request_id"])
    response = client.get("/api/ai-channel/requests", headers=data["host_h"], params={"status": "reviewed"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["stats"]["reviewed_requests"] == 2
    assert len(body["groups"][0]["requests"][0]["nodes"]) == 2
    profile = client.get("/api/ai-channel/profile", headers=data["host_h"])
    assert profile.status_code == 200, profile.text
    assert profile.json()["reviewed_requests"] == 2
    assert profile.json()["total_profit"] is None


def test_pending_node_cannot_resume_or_enter_dispatch_without_approval(client):
    data = setup(client)
    apply(client, data)
    response = client.post("/api/ai-follow/nodes/state", headers=data["bob_h"], json={
        "node_ids": [data["b_follow"]], "state": "running", "group_id": data["ai_group_id"],
    })
    assert response.status_code == 404
    assert client.portal.call(state.store.get_group, data["ai_group_id"])["followers"] == []


def test_ai_menu_only_can_read_owned_channels_but_cannot_close_normal_channel(client):
    data = setup(client)
    response = client.get(f"/api/nodes/{data['b_follow']}/follow-channels", headers=data["bob_h"])
    assert response.status_code == 200, response.text
    response = client.post(f"/api/nodes/{data['b_follow']}/follow-channels/close", headers=data["bob_h"], json={"market": "follow", "mode": "all"})
    assert response.status_code == 403


def test_group_disabled_cancels_pending_and_releases_application_slot(client):
    data = setup(client)
    apply(client, data)
    response = client.patch(f"/api/groups/{data['ai_group_id']}", headers=data["host_h"], json={"enabled": False})
    assert response.status_code == 200, response.text
    assert client.get("/api/ai-follow/me", headers=data["bob_h"]).json()["applications"][0]["status"] == "cancelled"
    client.patch(f"/api/groups/{data['ai_group_id']}", headers=data["host_h"], json={"enabled": True})
    assert len(apply(client, data)["pending"]) == 1


def test_concurrent_approvals_create_one_relation_and_success_audit(client):
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    async def run():
        from app.rbac_service import load_principal
        principal = await load_principal(state.store, data["host"]["username"])
        result = await asyncio.gather(*(ai_application_store.review(state.store, principal, request_id, approve=True) for _ in range(2)))
        async with SessionLocal() as session:
            assert len((await session.scalars(select(GroupFollowerNode))).all()) == 1
            audits = (await session.scalars(select(AuditLog).where(AuditLog.action == "ai_follow_request_approve"))).all()
            assert len(audits) == 1
        assert sum(bool(item.get("skipped")) for item in result) == 1
    client.portal.call(run)


def test_foreign_channel_and_requests_are_denied(client):
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    assert client.get("/api/ai-channel/profile", headers=data["bob_h"]).status_code == 403
    assert client.post(f"/api/ai-channel/requests/{request_id}/approve", headers=data["bob_h"]).status_code == 403
    assert client.post("/api/ai-channel/members/deletion-preview", headers=data["bob_h"], json={
        "group_id": data["ai_group_id"],
    }).status_code == 403


def test_primary_switch_keeps_approved_ai_relation(client):
    data = setup(client)
    approve(client, data, apply(client, data)["pending"][0]["request_id"])
    assert client.get(f"/api/nodes/{data['b_follow']}/followings", headers=_login(client, "admin")).json()["count"] == 0
    response = client.post(f"/api/nodes/{data['b_follow']}/primary", headers=_login(client, "admin"), json={"is_primary": True})
    assert response.status_code == 200, response.text
    assert client.portal.call(state.store.get_group, data["ai_group_id"])["followers"][0]["node_id"] == data["b_follow"]


def test_normal_market_never_dispatches_primary_follower_after_template_switch():
    from app.follow_rules import group_for_follow_dispatch
    group = {"followers": [{"node_id": "primary"}, {"node_id": "secondary"}]}
    nodes = {"primary": {"is_primary": True}, "secondary": {"is_primary": False}}
    assert group_for_follow_dispatch(group, "tpl_1", nodes)["followers"] == group["followers"]
    assert group_for_follow_dispatch(group, "tpl_2", nodes)["followers"] == [{"node_id": "secondary"}]


def test_legacy_migration_once_preserves_paused_state_and_audit(client):
    data = setup(client)
    async def seed():
        async with SessionLocal() as session:
            await session.execute(delete(SystemSetting).where(SystemSetting.key == ai_application_store.MIGRATION_KEY))
            session.add(GroupFollowerNode(group_id=data["ai_group_id"], node_id=data["b_follow"],
                follower_user_id=data["bob"]["id"], leader_user_id=data["host"]["id"],
                leader_primary_node_id=data["h_primary"], follow_state="paused"))
            await session.commit()
        await ai_application_store.migrate_legacy()
        await ai_application_store.migrate_legacy()
        await group_service.warm_cache(state.store)
    client.portal.call(seed)
    body = client.get("/api/ai-follow/me", headers=data["bob_h"]).json()
    assert body["following"][0]["review_status"] == "pending"
    assert len(body["applications"]) == 1
    approve(client, data, body["applications"][0]["request_id"])
    group = client.portal.call(state.store.get_group, data["ai_group_id"])
    assert group["followers"][0]["follow_state"] == "paused"
    async def audit_count():
        async with SessionLocal() as session:
            return len((await session.scalars(select(AuditLog).where(AuditLog.action == "ai_follow_legacy_review"))).all())
    assert client.portal.call(audit_count) == 2


def test_concurrent_submissions_create_one_pending_application(client):
    data = setup(client)
    async def submit_twice():
        from app.rbac_service import load_principal
        principal = await load_principal(state.store, "bob")
        args = (state.store, principal, data["host"]["id"], [data["ai_group_id"]], [data["b_follow"]])
        return await asyncio.gather(ai_application_store.submit(*args), ai_application_store.submit(*args))
    results = client.portal.call(submit_twice)
    assert sum(len(result["pending"]) for result in results) == 1


def test_approval_audit_failure_rolls_back_relations(client, monkeypatch):
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    original = ai_application_store.audit
    def fail(session, principal, action, *args, **kwargs):
        if action == "ai_follow_request_approve":
            raise RuntimeError("audit unavailable")
        return original(session, principal, action, *args, **kwargs)
    monkeypatch.setattr(ai_application_store, "audit", fail)
    async def run():
        from app.rbac_service import load_principal
        principal = await load_principal(state.store, data["host"]["username"])
        try:
            await ai_application_store.review(state.store, principal, request_id, approve=True)
        except RuntimeError:
            pass
        async with SessionLocal() as session:
            assert (await session.get(AiFollowApplication, request_id)).status == "pending"
            assert (await session.scalars(select(GroupFollowerNode))).all() == []
    client.portal.call(run)


def test_all_approval_ids_span_pages_and_search_limits_all_action(client):
    from tests.test_ai_follow_api import _mk_ai_strategy, _mk_group
    data = setup(client)
    first = apply(client, data)["pending"][0]["request_id"]
    strategy_id = _mk_ai_strategy(client, data["host_h"], "other-ai")
    group_id = _mk_group(client, data["host_h"], [data["h_primary"]], "other", strategy_id, True)
    result = client.post("/api/ai-follow/join", headers=data["bob_h"], json={
        "leader_user_id": data["host"]["id"], "group_ids": [group_id], "node_ids": [data["b_primary"]],
    })
    assert result.status_code == 200, result.text
    second = result.json()["pending"][0]["request_id"]
    all_results = client.get("/api/ai-channel/requests", headers=data["host_h"], params={"page_size": 1}).json()
    assert all_results["total"] == 2
    assert len(all_results["groups"]) == 1
    assert set(all_results["all_pending_request_ids"]) == {first, second}
    assert all_results["all_pending_node_count"] == 2
    filtered = client.get("/api/ai-channel/requests", headers=data["host_h"], params={"q": "other-ai", "page_size": 1}).json()
    assert filtered["all_pending_request_ids"] == [second]
    assert filtered["all_pending_node_count"] == 1
    response = client.post("/api/ai-channel/requests/approve-batch", headers=data["host_h"], json={"request_ids": all_results["all_pending_request_ids"]})
    assert response.status_code == 200, response.text
    assert len(response.json()["approved"]) == 2
    assert response.json()["failed"] == []


def test_removing_ai_menu_cancels_pending_and_prevents_approval(client):
    from tests.test_ai_follow_api import _bind_role, _make_role
    data = setup(client)
    request_id = apply(client, data)["pending"][0]["request_id"]
    admin_h = _login(client, "admin")
    role = _make_role(client, admin_h, "without_ai", "无 AI 菜单", ["follow"], [])
    _bind_role(client, admin_h, data["bob"]["id"], role["id"])
    response = client.post(f"/api/ai-channel/requests/{request_id}/approve", headers=data["host_h"])
    assert response.status_code == 409, response.text
    async def check():
        async with SessionLocal() as session:
            assert (await session.get(AiFollowApplication, request_id)).status == "cancelled"
            assert (await session.scalars(select(GroupFollowerNode))).all() == []
    client.portal.call(check)
