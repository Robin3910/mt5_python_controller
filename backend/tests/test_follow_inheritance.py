from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import follow_requests, follow_rules, follow_service, limit_watch
from app.connections import manager
from app.db import SessionLocal
from app.orm import AuditLog, FollowRequest, GroupFollowerNode, GroupTaskDispatch, NodeGroupMember
from app.state import state
from tests.test_follow_api import _mk_group_with_strategy, _mk_strategy, client
from tests.test_follow_pause_dispatch import capture_sender
from tests.test_follow_review import apply, approve, setup
from tests.test_limit_watch import _order


def target_group(client, d, name):
    sid = _mk_strategy(client, d["leader"], name, "tpl_2")
    return _mk_group_with_strategy(client, d["leader"], [d["primary"]], name, sid)


def sync(client, d, gid):
    response = client.post(f"/api/groups/{gid}/adopt-followers", headers=d["leader"])
    assert response.status_code == 200, response.text
    return response.json()["adopted"]


def limit_group(client, d):
    strategy = client.post("/api/strategies", headers=d["leader"], json={
        "template_id": "tpl_2", "name": "限价", "symbol": "XAUUSD",
        "rules": [{"type": 3, "status": 1, "entry_mode": "limit"}],
    })
    assert strategy.status_code == 201, strategy.text
    return _mk_group_with_strategy(client, d["leader"], [d["primary"]], "限价组", strategy.json()["strategy_id"])


@pytest.fixture
def watched_limit_group(client, monkeypatch):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    gid = limit_group(client, d)
    response = client.patch(f"/api/groups/{gid}", headers=d["leader"], json={
        "limit_watch_enabled": True, "limit_watch_keyword": "",
        "limit_watch_node_ids": [d["primary"]], "signal_concurrent_enabled": True,
    })
    assert response.is_success, response.text
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    for nid in (d["primary"], d["node"]):
        monkeypatch.setitem(manager.nodes, nid, object())
    cancel = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(limit_watch, "cancel_pending_on_node", cancel)
    return d, gid, sent, cancel


def start_targets(sent):
    return {nid for nid, msg in sent if msg["cmd"] == "strategy_start"}


def test_mt5_first_limit_order_dispatches_to_inherited_follower(client, watched_limit_group):
    d, gid, sent, cancel = watched_limit_group
    order = _order()
    cancel.return_value = {"success": False}
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [order])
    assert not sent
    assert client.portal.call(state.store.get_group, gid)["followers"] == []

    cancel.reset_mock()
    cancel.return_value = {"success": True}
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [order])

    starts = {nid: msg for nid, msg in sent if msg["cmd"] == "strategy_start"}
    assert starts.keys() == {d["primary"], d["node"]}
    assert len({msg["signal_id"] for msg in starts.values()}) == 1
    assert all(
        msg["group_id"] == gid
        and msg["entry"]["entry_price"] == order["price_open"]
        and msg["entry"]["stop_loss"] == order["sl"]
        for msg in starts.values()
    )
    assert len({msg["dispatch_id"] for msg in starts.values()}) == 2
    assert len({msg["magic"] for msg in starts.values()}) == 2

    sent.clear()
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [order])
    assert not sent
    cancel.assert_awaited_once_with(d["primary"], order["ticket"])
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [_order(ticket=1002)])
    assert start_targets(sent) == {d["primary"], d["node"]}

    async def verify_ledger():
        async with SessionLocal() as session:
            for nid, msg in starts.items():
                dispatch = await session.get(GroupTaskDispatch, msg["dispatch_id"])
                assert (dispatch.node_id, dispatch.magic, dispatch.status) == (nid, msg["magic"], "sent")
            relation = (await session.scalars(select(GroupFollowerNode).where(GroupFollowerNode.group_id == gid))).one()
            assert relation.node_id == d["node"]
            request = (await session.scalars(select(FollowRequest).where(FollowRequest.group_id == gid))).one()
            assert (request.source, request.status) == ("inherit", "approved")
            audit = (await session.scalars(select(AuditLog).where(AuditLog.action == "follow_request_auto_approve"))).one()
            assert (audit.operator, audit.target) == ("system", str(request.id))

    client.portal.call(verify_ledger)


def test_mt5_limit_watch_keeps_inherited_pause(client, watched_limit_group):
    d, gid, sent, _ = watched_limit_group
    response = client.post("/api/follow/me/state", headers=d["follower"], json={
        "relations": [{"group_id": d["group"], "node_id": d["node"]}], "paused": True,
    })
    assert response.is_success, response.text

    client.portal.call(limit_watch.handle_account_orders, d["primary"], [_order()])

    assert start_targets(sent) == {d["primary"]}
    followers = client.portal.call(state.store.get_group, gid)["followers"]
    assert [(f["node_id"], f["follow_state"]) for f in followers] == [(d["node"], "paused")]


@pytest.mark.parametrize("review", ("pending", "rejected", "removed"))
def test_mt5_limit_watch_respects_target_review(client, watched_limit_group, review):
    d, gid, sent, _ = watched_limit_group
    request_id = apply(client, d, groups=[gid])["pending"][0]["request_id"]
    if review == "rejected":
        response = client.post(f"/api/follow-channel/requests/{request_id}/reject", headers=d["leader"], json={"reason": "拒绝"})
        assert response.is_success, response.text
    elif review == "removed":
        approve(client, d, request_id)
        response = client.post("/api/follow-channel/members/remove", headers=d["leader"], json={"request_ids": [request_id]})
        assert response.is_success, response.text

    client.portal.call(limit_watch.handle_account_orders, d["primary"], [_order()])

    assert start_targets(sent) == {d["primary"]}
    assert client.portal.call(state.store.get_group, gid)["followers"] == []


def test_mt5_limit_watch_from_non_primary_does_not_inherit(client, watched_limit_group, monkeypatch):
    d, gid, sent, _ = watched_limit_group
    for group_id in (gid, d["group"]):
        response = client.patch(f"/api/groups/{group_id}", headers=d["leader"], json={
            "node_ids": [d["primary"], d["extra"]], "limit_watch_node_ids": [d["extra"]],
        })
        assert response.is_success, response.text
    monkeypatch.setitem(manager.nodes, d["extra"], object())

    client.portal.call(limit_watch.handle_account_orders, d["extra"], [_order()])

    assert start_targets(sent) == {d["primary"], d["extra"]}
    assert client.portal.call(state.store.get_group, gid)["followers"] == []


def test_mt5_limit_watch_rechecks_reporting_in_approval(client, watched_limit_group):
    d, gid, sent, cancel = watched_limit_group

    async def stop_reporting_during_cancel(*_):
        async with SessionLocal() as session:
            member = (await session.scalars(select(NodeGroupMember).where(NodeGroupMember.group_id == gid))).one()
            member.limit_watch_report = False
            await session.commit()
        return {"success": True}

    cancel.side_effect = stop_reporting_during_cancel
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [_order()])

    assert start_targets(sent) == {d["primary"]}
    assert client.portal.call(state.store.get_group, gid)["followers"] == []


def test_mt5_limit_watch_sync_failure_is_audited_and_dispatch_continues(client, watched_limit_group, monkeypatch):
    d, gid, sent, _ = watched_limit_group
    order = _order()
    with monkeypatch.context() as patch:
        patch.setattr(follow_service, "_refresh_groups", AsyncMock(side_effect=RuntimeError("cache unavailable")))
        client.portal.call(limit_watch.handle_account_orders, d["primary"], [order])

    assert start_targets(sent) == {d["primary"]}

    async def read_watch_audit():
        async with SessionLocal() as session:
            return (await session.scalars(select(AuditLog).where(AuditLog.action == limit_watch.AUDIT_ACTION))).one()

    audit = client.portal.call(read_watch_audit)
    assert audit.before_json["ticket"] == order["ticket"]
    assert audit.after_json["follow_sync_failed_group_ids"] == [gid]
    assert audit.after_json["result"]["status"] == "accepted"

    sent.clear()
    client.portal.call(limit_watch.handle_account_orders, d["primary"], [_order(ticket=1002)])
    assert start_targets(sent) == {d["primary"], d["node"]}


def test_first_limit_signal_after_concurrent_inheritance(client, monkeypatch):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    gid = limit_group(client, d)
    with ThreadPoolExecutor(2) as pool:
        calls = [pool.submit(sync, client, d, gid) for _ in range(2)]
        adopted = [row for call in calls for row in call.result()]
    assert len(adopted) == 1 and adopted[0]["status"] == "approved"
    channel = client.get("/api/follow-channel/requests?status=reviewed", headers=d["leader"]).json()
    assert channel["stats"]["pending_nodes"] == 0
    assert {r["group_id"] for r in channel["applicants"][0]["nodes"] if r["status"] == "approved"} == {gid, d["group"]}

    async def audit_entry():
        async with SessionLocal() as session:
            return (await session.scalars(select(AuditLog).where(AuditLog.action == "follow_request_auto_approve"))).one()
    entry = client.portal.call(audit_entry)
    assert entry.actor_user_id == d["alice"]["id"] and entry.params_json["source_group_ids"] == [d["group"]]
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    for nid in (d["primary"], d["node"]):
        monkeypatch.setitem(manager.nodes, nid, object())
    response = client.post("/api/console/manual-signal", headers=d["leader"], json={
        "model": "strategy", "trading_primary_id": d["primary"], "group_ids": [gid], "action": "BUY", "symbol": "XAUUSD",
        "entry_price": 2300, "stop_loss": 2290, "volume": 0.1,
    })
    assert response.status_code == 200, response.text
    assert {nid for nid, msg in sent if msg.get("cmd") == "strategy_start"} == {d["primary"], d["node"]}, response.text


def test_pending_inheritance_reuses_request_and_combines_pauses(client):
    d = setup(client)
    source2 = target_group(client, d, "第二来源")
    gid = target_group(client, d, "新组")
    for row in apply(client, d, nodes=[d["node"], d["bob_primary"]])["pending"]:
        approve(client, d, row["request_id"])
    second = apply(client, d, groups=[source2])["pending"][0]["request_id"]
    approve(client, d, second)
    assert client.post("/api/follow/me/state", headers=d["follower"], json={
        "relations": [{"group_id": d["group"], "node_id": d["node"]}], "paused": True,
    }).status_code == 200
    assert client.post("/api/follow-channel/members/state", headers=d["leader"],
                       json={"request_ids": [second], "paused": True}).status_code == 200
    pending = apply(client, d, nodes=[d["node"], d["bob_primary"]], groups=[gid])["pending"]
    inherited = next(row["request_id"] for row in pending if row["node_id"] == d["node"])

    async def simulate_pre_upgrade():
        async with SessionLocal() as session:
            (await session.get(FollowRequest, inherited)).source = "inherit"
            await session.commit()
    client.portal.call(simulate_pre_upgrade)
    client.portal.call(state.store.delete_strategy, d["strategy"])
    client.portal.call(state.store.delete_strategy, client.portal.call(state.store.get_group, gid)["strategy_id"])
    assert [r["request_id"] for r in sync(client, d, gid)] == [inherited]
    group = client.portal.call(state.store.get_group, gid)
    assert [(r["node_id"], r["follow_state"], r["leader_paused"]) for r in group["followers"]] == [(d["node"], "paused", True)]
    assert follow_rules.group_for_follow_dispatch(group, "tpl_2")["followers"] == []
    channel = client.get("/api/follow-channel/requests", headers=d["leader"]).json()
    assert channel["all_pending_request_ids"] == [next(r["request_id"] for r in pending if r["node_id"] == d["bob_primary"])]


def test_sync_respects_rejection_removal_and_leave(client):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    for action in ("reject", "remove", "leave"):
        gid = target_group(client, d, action)
        req = apply(client, d, groups=[gid])["pending"][0]["request_id"]
        if action != "reject":
            approve(client, d, req)
        if action == "reject":
            assert client.post(f"/api/follow-channel/requests/{req}/reject", headers=d["leader"], json={"reason": "拒绝"}).is_success
        elif action == "remove":
            assert client.post("/api/follow-channel/members/remove", headers=d["leader"], json={"request_ids": [req]}).is_success
        else:
            assert client.post("/api/follow/leave", headers=d["follower"], json={
                "leader_user_id": d["alice"]["id"], "group_ids": [gid], "node_ids": [d["node"]], "close_tasks": False,
            }).is_success
        assert sync(client, d, gid) == []
        assert client.portal.call(state.store.get_group, gid)["followers"] == []
        assert apply(client, d, groups=[gid])["pending"]


def test_inheritance_rolls_back_with_audit_and_repairs_cache_on_retry(client, monkeypatch):
    d = setup(client)
    approve(client, d, apply(client, d)["pending"][0]["request_id"])
    gid = target_group(client, d, "重试")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    async def fail_refresh(*args, **kwargs):
        raise RuntimeError("cache unavailable")

    async def target_rows():
        async with SessionLocal() as session:
            return [(await session.scalars(select(model.id).where(model.group_id == gid))).all()
                    for model in (FollowRequest, GroupFollowerNode)]

    with monkeypatch.context() as patch:
        patch.setattr(follow_requests, "audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            sync(client, d, gid)
    assert client.portal.call(target_rows) == [[], []]
    with monkeypatch.context() as patch:
        patch.setattr(follow_service, "_refresh_groups", fail_refresh)
        with pytest.raises(RuntimeError, match="cache unavailable"):
            sync(client, d, gid)
    assert client.portal.call(state.store.get_group, gid)["followers"] == []
    assert sync(client, d, gid) == []
    assert [r["node_id"] for r in client.portal.call(state.store.get_group, gid)["followers"]] == [d["node"]]
