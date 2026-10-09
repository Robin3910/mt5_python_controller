"""面板审批、隔离凭证与守护的端到端回归（SQLite + fakeredis）。"""
import pathlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
PREFIX = "/api/node-dashboard"


@pytest.fixture
def client(monkeypatch):
    reset_test_db(_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(
        lambda cls, url=None: RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))))
    from app.main import app
    with TestClient(app) as c:
        yield c
    drop_test_db(_DB)


def user(client, admin, name="alice"):
    data = client.post("/api/users", headers=admin,
                       json={"username": name, "password": "pass1234"}).json()
    token = client.post("/api/login", json={"username": name, "password": "pass1234"}).json()["token"]
    return data["id"], {"Authorization": f"Bearer {token}"}


def enrolled(client, h, login=8801):
    response = client.post(f"{PREFIX}/enrollments", headers=h, json={"mt5_login": login})
    assert response.status_code == 201, response.text
    return response.json()


def assign(client, admin, uid, node_id):
    response = client.put(f"/api/users/{uid}/nodes", headers=admin, json={"node_ids": [node_id]})
    assert response.status_code == 200, response.text


def enable(client, admin, node_id):
    response = client.patch(f"/api/nodes/{node_id}", headers=admin, json={"enabled": True})
    assert response.status_code == 200, response.text
    return response.json()


def opened(client):
    admin = auth_headers(client)
    uid, h = user(client, admin)
    n = enrolled(client, h)
    assign(client, admin, uid, n["node_id"])
    assert enable(client, admin, n["node_id"])["approval_status"] == "approved"
    return admin, uid, h, n["node_id"]


@pytest.mark.parametrize("enable_first", [True, False])
def test_enrollment_requires_assignment_and_admin_intent(client, enable_first):
    admin = auth_headers(client)
    uid, h = user(client, admin)
    n = enrolled(client, h)
    nid = n["node_id"]
    assert n["owner_user_id"] is None and n["requested_by_user_id"] == uid
    assert n["approval_status"] == "pending" and n["enabled"] is False and n["legacy_allowed"] is False
    assert enrolled(client, h)["node_id"] == nid
    assert client.get(f"{PREFIX}/enrollments", headers=h).json()["items"][0]["node_id"] == nid
    if enable_first:
        data = enable(client, admin, nid)
        assert data["admin_enable_requested"] and not data["enabled"] and data["approval_status"] == "pending"
        assign(client, admin, uid, nid)
    else:
        assign(client, admin, uid, nid)
        assert client.patch(f"/api/nodes/{nid}", headers=h, json={"enabled": True}).status_code == 403
        assert client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid}).status_code == 403
        enable(client, admin, nid)
    data = client.get(f"/api/nodes/{nid}", headers=h).json()
    assert data["enabled"] and data["approval_status"] == "approved"


def test_admin_application_can_approve_without_assignment(client):
    admin = auth_headers(client)
    n = enrolled(client, admin)
    data = enable(client, admin, n["node_id"])
    assert data["owner_user_id"] is None and data["enabled"] and data["approval_status"] == "approved"


def test_other_user_cannot_claim_or_view_application(client):
    admin = auth_headers(client)
    _, alice = user(client, admin)
    _, bob = user(client, admin, "bob")
    n = enrolled(client, alice)
    assert client.post(f"{PREFIX}/enrollments", headers=bob, json={"mt5_login": 8801}).status_code == 409
    assert client.get(f"{PREFIX}/enrollments", headers=bob).json()["items"] == []
    assert client.post(f"{PREFIX}/actions", headers=bob, json={
        "request_id": "claim-other-node", "action": "view_log", "node_id": n["node_id"]}).status_code == 404


def test_scoped_credential_issue_verify_rotate_and_ws_isolation(client):
    admin, _, h, nid = opened(client)
    issue = client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid})
    assert issue.status_code == 200, issue.text
    secret = issue.json()
    assert secret["token"].startswith(f"ndv1.{nid}.")
    assert client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid}).status_code == 409
    assert client.post(f"{PREFIX}/credentials/verify", headers=h,
                       json={"node_id": nid, "token": secret["token"]}).json()["valid"]
    global_token = client.get("/api/config/node-token", headers=admin).json()["token"]
    for token, login in [(global_token, 8801), (secret["token"], 8802)]:
        with client.websocket_connect("/ws/node") as ws:
            ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": login}})
            assert ws.receive_json()["data"]["reason"] == "invalid_token"
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": secret["token"], "mt5_login": 8801}})
        assert ws.receive_json()["type"] == "auth_ok"
    assert client.post(f"{PREFIX}/credentials/rotate", headers=h, json={
        "node_id": nid, "expected_generation": secret["generation"] - 1}).status_code == 409
    rotate = client.post(f"{PREFIX}/credentials/rotate", headers=h, json={
        "node_id": nid, "expected_generation": secret["generation"]})
    assert rotate.status_code == 200, rotate.text
    assert rotate.json()["generation"] == secret["generation"] + 1
    assert client.post(f"{PREFIX}/credentials/verify", headers=h,
                       json={"node_id": nid, "token": secret["token"]}).json()["valid"] is False


def test_daemon_token_cannot_login_and_survives_password_change(client):
    admin, uid, h, nid = opened(client)
    grant = client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid}).json()
    grant_body = {"grant_token": grant["grant_token"]}
    assert client.post(f"{PREFIX}/daemon-grants/check", json=grant_body).status_code == 200
    assert client.get("/api/me", headers={"Authorization": f"Bearer {grant['grant_token']}"}).status_code == 401
    assert client.post(f"{PREFIX}/daemon-grants/check", json={"grant_token": h["Authorization"].split()[1]}).status_code == 401
    client.post(f"/api/users/{uid}/reset-password", headers=admin, json={"new_password": "newpass99"})
    assert client.get("/api/me", headers=h).status_code == 401
    assert client.post(f"{PREFIX}/daemon-grants/check", json=grant_body).status_code == 200
    client.patch(f"/api/users/{uid}", headers=admin, json={"is_active": False})
    assert client.post(f"{PREFIX}/daemon-grants/check", json=grant_body).status_code == 403


def test_transfer_revokes_grant_and_scoped_secret(client):
    admin, _, h, nid = opened(client)
    secret = client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid}).json()
    grant = client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid}).json()
    bob_id, bob = user(client, admin, "bob")
    assign(client, admin, bob_id, nid)
    assert client.post(f"{PREFIX}/daemon-grants/check", json={"grant_token": grant["grant_token"]}).status_code == 403
    assert client.post(f"{PREFIX}/credentials/verify", headers=bob,
                       json={"node_id": nid, "token": secret["token"]}).json()["valid"] is False
    assert client.post(f"{PREFIX}/actions", headers=h, json={
        "request_id": "previous-owner-start", "action": "start", "node_id": nid}).status_code == 404
    assert client.get(f"{PREFIX}/enrollments", headers=h).json()["items"] == []
    assert client.post(f"{PREFIX}/enrollments", headers=h, json={"mt5_login": 8801}).status_code == 409
    assert client.post(f"{PREFIX}/enrollments", headers=bob, json={"mt5_login": 8801}).json()["node_id"] == nid
    for action in ["stop", "edit_env", "remove_instance", "view_log"]:
        assert client.post(f"{PREFIX}/actions", headers=h, json={
            "request_id": f"former-owner-{action}", "action": action, "node_id": nid}).status_code == 404


def test_action_intent_result_idempotent_redacted_and_actor_bound(client):
    admin, _, h, nid = opened(client)
    payload = {"request_id": "once-start-8801", "action": "start", "node_id": nid,
               "params": {"token": "top-secret", "password": "secret", "raw_config": {"NODE_TOKEN": "secret"}, "version": "1.0"}}
    a = client.post(f"{PREFIX}/actions", headers=h, json=payload)
    assert a.status_code == 200, a.text
    op = a.json()["operation_id"]
    assert client.post(f"{PREFIX}/actions", headers=h, json=payload).json()["operation_id"] == op
    assert client.post(f"{PREFIX}/actions", headers=h, json={**payload, "action": "stop"}).status_code == 409
    result = {"result": "ok", "params": {"token": "top-secret", "error": "exception secret"}}
    _, bob = user(client, admin, "bob")
    assert client.post(f"{PREFIX}/actions/{op}/result", headers=bob, json=result).status_code == 404
    assert client.post(f"{PREFIX}/actions/{op}/result", headers=h, json=result).status_code == 200
    assert client.post(f"{PREFIX}/actions/{op}/result", headers=admin, json=result).status_code == 200
    assert client.post(f"{PREFIX}/actions/{op}/result", headers=h, json=result).status_code == 200
    rows = client.get("/api/audits", headers=h, params={"category": "all", "page_size": 100}).json()["items"]
    row = next(x for x in rows if x["id"] == op)
    assert row["result"] == "ok" and "top-secret" not in str(row) and "raw_config" not in str(row)


def test_admin_can_audit_unbound_local_inventory_without_granting_control(client):
    admin = auth_headers(client)
    _, h = user(client, admin)
    payload = {"request_id": "read-unbound-instance", "action": "view_status", "instance_id": "legacy-local"}
    response = client.post(f"{PREFIX}/actions", headers=admin, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["node_id"] is None and response.json()["mt5_login"] is None
    operation_id = response.json()["operation_id"]
    assert client.post(f"{PREFIX}/actions", headers=admin, json=payload).json()["operation_id"] == operation_id
    assert client.post(f"{PREFIX}/actions", headers=h, json=payload).status_code == 422
    assert client.post(f"{PREFIX}/actions", headers=admin, json={**payload, "action": "start"}).status_code == 422
    for action in ("update_client", "rollback_client", "replace_client"):
        local = {"request_id": f"local-{action}", "action": action, "instance_id": "legacy-local", "params": {"version": "1.1.0-20261006092656"}}
        updated = client.post(f"{PREFIX}/actions", headers=admin, json=local)
        assert updated.status_code == 200, updated.text
        assert updated.json()["node_id"] is None
        assert client.post(f"{PREFIX}/actions", headers=h, json=local).status_code == 422
    assert client.post(f"{PREFIX}/actions", headers=admin, json={**payload, "node_id": "unknown-node"}).status_code == 404
    rows = client.get("/api/audits", headers=admin, params={"category": "all", "page_size": 100}).json()["items"]
    row = next(item for item in rows if item["id"] == operation_id)
    assert row["action"] == "dashboard_view_status" and row["target"] == "legacy-local" and row["result"] == "pending"


def test_pending_prepare_allowed_but_start_and_guard_denied(client):
    admin = auth_headers(client)
    _, h = user(client, admin)
    nid = enrolled(client, h)["node_id"]
    for action in ["edit_config", "import_node", "update"]:
        assert client.post(f"{PREFIX}/actions", headers=h, json={
            "request_id": f"prepare-{action}", "action": action, "node_id": nid}).status_code == 200
    for action in ["start", "restart", "daemon_on"]:
        assert client.post(f"{PREFIX}/actions", headers=h, json={
            "request_id": f"deny-{action}", "action": action, "node_id": nid}).status_code == 404
    assert client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid}).status_code in (403, 404)


def test_daemon_events_have_strict_idempotent_audit(client):
    _, _, h, nid = opened(client)
    grant = client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid}).json()["grant_token"]
    body = {"grant_token": grant, "request_id": "auto-restart-8801", "phase": "intent"}
    a = client.post(f"{PREFIX}/daemon-grants/events", json=body)
    assert a.status_code == 200, a.text
    assert client.post(f"{PREFIX}/daemon-grants/events", json=body).json()["operation_id"] == a.json()["operation_id"]
    assert client.post(f"{PREFIX}/daemon-grants/events", json={**body, "phase": "result", "result": "ok"}).status_code == 200


def test_jwt_can_download_but_cannot_get_global_token(client):
    admin = auth_headers(client)
    _, h = user(client, admin)
    assert client.get("/api/client-versions/available", headers=h).status_code == 200
    assert client.get("/api/client-versions/current", headers=h).status_code == 204
    assert client.get("/api/config/node-token", headers=h).status_code == 403
    assert client.get("/api/client-versions/available").status_code == 401


def test_concurrent_first_issue_and_intent_are_atomic(client):
    _, _, h, nid = opened(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(f"{PREFIX}/credentials/issue", headers=h,
                                                        json={"node_id": nid}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    payload = {"request_id": "parallel-start-8801", "action": "start", "node_id": nid}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(f"{PREFIX}/actions", headers=h, json=payload), range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert responses[0].json()["operation_id"] == responses[1].json()["operation_id"]


def test_concurrent_assignment_and_enable_open_node(client):
    admin = auth_headers(client)
    uid, h = user(client, admin)
    nid = enrolled(client, h)["node_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(assign, client, admin, uid, nid), pool.submit(enable, client, admin, nid)]
        for job in jobs:
            job.result()
    node = client.get(f"/api/nodes/{nid}", headers=h).json()
    assert node["enabled"] and node["approval_status"] == "approved"


def test_first_issue_racing_transfer_cannot_damage_new_owner_credential(client):
    admin, _, h, nid = opened(client)
    bob_id, bob = user(client, admin, "bob")
    with ThreadPoolExecutor(max_workers=2) as pool:
        issue_job = pool.submit(client.post, f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid})
        transfer_job = pool.submit(assign, client, admin, bob_id, nid)
        issue = issue_job.result()
        transfer_job.result()
    assert issue.status_code in (200, 404), issue.text
    node = client.get(f"/api/nodes/{nid}", headers=bob).json()
    assert node["owner_user_id"] == bob_id and not node["has_credential"]
    bob_issue = client.post(f"{PREFIX}/credentials/issue", headers=bob, json={"node_id": nid})
    assert bob_issue.status_code == 200, bob_issue.text
    assert client.post(f"{PREFIX}/credentials/verify", headers=bob,
                       json={"node_id": nid, "token": bob_issue.json()["token"]}).json()["valid"]
    if issue.status_code == 200:
        assert client.post(f"{PREFIX}/credentials/verify", headers=bob,
                           json={"node_id": nid, "token": issue.json()["token"]}).json()["valid"] is False


def test_rotate_and_transfer_preserve_existing_ws(client):
    admin, _, h, nid = opened(client)
    secret = client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid}).json()
    bob_id, _ = user(client, admin, "bob")
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": secret["token"], "mt5_login": 8801}})
        assert ws.receive_json()["type"] == "auth_ok"
        assert client.post(f"{PREFIX}/credentials/rotate", headers=h, json={
            "node_id": nid, "expected_generation": secret["generation"]}).status_code == 200
        assign(client, admin, bob_id, nid)
        ws.send_json({"type": "heartbeat", "data": {}})
        assert ws.receive_json()["type"] in {"pong", "ping"}


def test_strict_intent_failure_returns_503_without_success(client, monkeypatch):
    _, _, h, nid = opened(client)
    from app import dashboard_audit
    from sqlalchemy.exc import OperationalError
    async def fail(*args, **kwargs):
        raise OperationalError("audit", {}, Exception("unavailable"))
    monkeypatch.setattr(dashboard_audit, "begin_operation", fail)
    r = client.post(f"{PREFIX}/actions", headers=h, json={
        "request_id": "strict-failure-8801", "action": "start", "node_id": nid})
    assert r.status_code == 503


def test_approval_and_assignment_roll_back_when_audit_fails(client, monkeypatch):
    admin = auth_headers(client)
    uid, h = user(client, admin)
    n = enrolled(client, h)
    nid = n["node_id"]
    from app import dashboard_audit
    from sqlalchemy.exc import OperationalError
    async def fail(*args, **kwargs):
        raise OperationalError("audit", {}, Exception("unavailable"))
    original = dashboard_audit.begin_operation
    monkeypatch.setattr(dashboard_audit, "begin_operation", fail)
    assert client.patch(f"/api/nodes/{nid}", headers=admin, json={"enabled": True}).status_code == 503
    data = client.get(f"/api/nodes/{nid}", headers=admin).json()
    assert not data["admin_enable_requested"] and not data["enabled"]
    monkeypatch.setattr(dashboard_audit, "begin_operation", original)
    enable(client, admin, nid)
    monkeypatch.setattr(dashboard_audit, "begin_operation", fail)
    assert client.put(f"/api/users/{uid}/nodes", headers=admin, json={"node_ids": [nid]}).status_code == 503
    data = client.get(f"/api/nodes/{nid}", headers=admin).json()
    assert data["owner_user_id"] is None and data["approval_status"] == "pending" and not data["enabled"]


def test_daemon_history_replay_after_transfer_keeps_original_actor(client):
    admin, _, h, nid = opened(client)
    grant = client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid}).json()
    bob_id, bob = user(client, admin, "bob")
    assign(client, admin, bob_id, nid)
    body = {"grant_operation_id": grant["grant_operation_id"], "request_id": "offline-restart-8801", "phase": "intent"}
    assert client.post(f"{PREFIX}/daemon-events/replay", headers=bob, json=body).status_code == 404
    intent = client.post(f"{PREFIX}/daemon-events/replay", headers=h, json=body)
    assert intent.status_code == 200, intent.text
    op = intent.json()["operation_id"]
    assert client.post(f"{PREFIX}/daemon-events/replay", headers=h, json=body).json()["operation_id"] == op
    assert client.post(f"{PREFIX}/daemon-events/replay", headers=admin, json={**body, "phase": "result", "result": "ok"}).status_code == 200
    rows = client.get("/api/audits", headers=admin, params={"category": "all", "page_size": 100}).json()["items"]
    assert next(row for row in rows if row["id"] == op)["operator"] == "alice"


def test_legacy_global_credential_is_unchanged_until_explicit_issue(client):
    admin = auth_headers(client)
    uid, h = user(client, admin)
    n = client.post("/api/nodes", headers=admin, json={"mt5_login": 9901}).json()
    nid = n["node_id"]
    assign(client, admin, uid, nid)
    # 模拟升级前已分配、但尚无独立凭证记录的存量节点。
    sync_engine = create_engine(f"sqlite:///{_DB.as_posix()}")
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM node_credentials WHERE node_id = :node_id"), {"node_id": nid})
    sync_engine.dispose()
    grant = client.post(f"{PREFIX}/daemon-grants", headers=h, json={"node_id": nid})
    assert grant.status_code == 200, grant.text
    assert client.post(f"{PREFIX}/daemon-grants/check", json={"grant_token": grant.json()["grant_token"]}).status_code == 200
    data = client.get(f"/api/nodes/{nid}", headers=h).json()
    assert data["legacy_allowed"] and not data["has_credential"]
    assert client.post(f"{PREFIX}/enrollments", headers=h, json={"mt5_login": 9901}).json()["node_id"] == nid
    global_token = client.get("/api/config/node-token", headers=admin).json()["token"]
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": global_token, "mt5_login": 9901}})
        assert ws.receive_json()["type"] == "auth_ok"
    issue = client.post(f"{PREFIX}/credentials/issue", headers=h, json={"node_id": nid})
    assert issue.status_code == 200
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": global_token, "mt5_login": 9901}})
        assert ws.receive_json()["data"]["reason"] == "invalid_token"


def test_migration_keeps_legacy_rows_and_is_idempotent(tmp_path):
    from app.db import _migrate_dashboard_columns
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE nodes (node_id VARCHAR(32) PRIMARY KEY, enabled BOOLEAN)"))
        conn.execute(text("INSERT INTO nodes VALUES ('legacy', 0)"))
        conn.execute(text("CREATE TABLE audit_log (id INTEGER PRIMARY KEY)"))
        _migrate_dashboard_columns(conn)
        _migrate_dashboard_columns(conn)
        row = conn.execute(text("SELECT enabled, approval_status FROM nodes")).one()
        assert row == (0, "approved")
        assert "token_hash" not in {c["name"] for c in inspect(conn).get_columns("nodes")}
    engine.dispose()


def test_credentials_migration_backfills_snapshot_and_copies_iteration_table(tmp_path):
    from app.db import _migrate_node_credentials
    engine = create_engine(f"sqlite:///{tmp_path / 'credentials.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE nodes (node_id VARCHAR(32) PRIMARY KEY, owner_user_id INTEGER)"))
        conn.execute(text("INSERT INTO nodes VALUES ('plural', 7), ('singular', 4)"))
        definition = "(node_id VARCHAR(32) PRIMARY KEY, token_sha256 VARCHAR(64), generation INTEGER, legacy_allowed BOOLEAN)"
        conn.execute(text(f"CREATE TABLE node_credentials {definition}"))
        conn.execute(text(f"CREATE TABLE node_credential {definition}"))
        conn.execute(text("INSERT INTO node_credentials VALUES ('plural', NULL, 3, 1)"))
        conn.execute(text("INSERT INTO node_credential VALUES ('singular', NULL, 2, 0)"))
        _migrate_node_credentials(conn)
        _migrate_node_credentials(conn)
        assert conn.execute(text("SELECT node_id, owner_user_id FROM node_credentials ORDER BY node_id")).all() == [
            ("plural", 7), ("singular", 4)]
        assert conn.execute(text("SELECT count(*) FROM node_credential")).scalar_one() == 1
        conn.execute(text("UPDATE nodes SET owner_user_id=99 WHERE node_id='plural'"))
        _migrate_node_credentials(conn)
        assert conn.execute(text("SELECT owner_user_id FROM node_credentials WHERE node_id='plural'")).scalar_one() == 7
    engine.dispose()


def test_delete_enrolled_node_removes_it_from_the_list(client):
    admin = auth_headers(client)
    _, headers = user(client, admin)
    node = enrolled(client, headers)
    response = client.delete(f"/api/nodes/{node['node_id']}", headers=admin)
    assert response.status_code == 200, response.text
    listed = [item["node_id"] for item in client.get("/api/nodes", headers=admin).json()]
    assert node["node_id"] not in listed


def test_delete_purges_cache_when_ledger_row_is_gone(client):
    """账本行丢失后缓存仍在时，删除要把列表里的节点清掉。"""
    admin = auth_headers(client)
    created = client.post("/api/nodes", headers=admin, json={"name": "probe", "mt5_login": 90000001})
    assert created.status_code == 201, created.text
    node_id = created.json()["node_id"]
    with sqlite3.connect(_DB) as conn:
        conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
        conn.commit()
    assert any(item["node_id"] == node_id for item in client.get("/api/nodes", headers=admin).json())
    response = client.delete(f"/api/nodes/{node_id}", headers=admin)
    assert response.status_code == 200, response.text
    assert all(item["node_id"] != node_id for item in client.get("/api/nodes", headers=admin).json())
