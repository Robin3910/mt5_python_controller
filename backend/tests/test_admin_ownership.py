"""双管理员个人交易隔离、旧归属预检与幂等修复（SQLite / fakeredis）。"""
from sqlalchemy import func, select

from app.admin_ownership_repair import repair
from app.db import SessionLocal
from app.orm import AuditLog, Node, NodeCredential, NodeGroup, SignalDispatch, TradingStrategy
from app.state import state
from tests.test_rbac_api import client, _login, _mk_group, _mk_node, _mk_strategy, _mk_user
from tests.test_group_api import _node_token, _wait_task_status
from tests.test_helpers import auth_headers


def test_two_admins_have_personal_nodes_and_keep_global_management(client):
    first = auth_headers(client)
    first_id = client.get('/api/me', headers=first).json()['user_id']
    roles = client.get('/api/roles', headers=first).json()
    rid = next(r['id'] for r in roles if r['code'] == 'admin')
    second_id = _mk_user(client, first, 'other_admin', role_ids=[rid])['id']
    user_id = _mk_user(client, first, 'trader')['id']
    second = _login(client, 'other_admin')
    a = _mk_node(client, second, 81101)
    payload = {'mt5_login': 81102, 'admin_owner_id': user_id}
    assert client.post('/api/nodes', headers=first, json=payload).status_code == 400
    b = client.post('/api/nodes', headers=first, json={**payload, 'admin_owner_id': second_id}).json()['node_id']
    assert client.get(f'/api/nodes/{a}', headers=second).json()['owner_user_id'] == first_id
    assert client.get(f'/api/nodes/{b}', headers=first).json()['owner_user_id'] == second_id
    for nid in [a, b]:
        assert not client.get(f'/api/nodes/{nid}', headers=second).json()['is_primary']
        assert client.post(f'/api/nodes/{nid}/primary', headers=first, json={'is_primary': True}).status_code == 200
    g = _mk_group(client, second, '同名组', [b]).json()
    assert g['owner_user_id'] == second_id
    assert _mk_group(client, first, '同名组', [a]).status_code == 201
    request = {'model': 'strategy', 'action': 'BUY', 'symbol': 'XAUUSD', 'volume': 0.01,
               'trading_primary_id': a, 'group_ids': [g['group_id']]}
    assert client.post('/api/console/manual-signal', headers=first, json=request).status_code == 403
    assert client.post('/api/console/manual-signal', headers=second, json={**request,'trading_primary_id': b}).status_code == 409
    assert client.post('/api/trading/quick-trigger', headers=first, json={
        'node_id': b, 'strategy_type': 'trend_market', 'symbol':'XAUUSD', 'volume':0.01}).status_code == 403
    sid = _mk_strategy(client, second, '模拟开仓')['strategy_id']
    assert client.patch(f"/api/groups/{g['group_id']}", headers=second, json={'strategy_id': sid}).status_code == 200
    with client.websocket_connect('/ws/node') as ws:
        ws.send_json({'type': 'auth', 'data': {'token': _node_token(client, first), 'mt5_login': 81102}})
        assert ws.receive_json()['type'] == 'auth_ok'
        sent = client.post('/api/console/manual-signal', headers=second, json={**request, 'trading_primary_id': b, 'node_ids': [b]})
        assert sent.status_code == 200 and sent.json()['targets'] == 1, sent.text
        cmd = ws.receive_json()
        assert cmd['cmd'] == 'strategy_start' and cmd['group_id'] == g['group_id']
        ws.send_json({'type': 'trade_result', 'data': {'signal_id': cmd['signal_id'], 'magic': cmd['magic'],
                      'symbol': 'XAUUSD', 'success': True, 'order': 123, 'price': 2300}})
        assert _wait_task_status(client, second, g['group_id'], 'running')['items'][0]['status'] == 'running'
        ws.send_json({'type': 'strategy_finished', 'data': {'task_id': cmd['task_id'], 'magic': cmd['magic'],
                      'status': 'done', 'reason': 'positions_cleared', 'total_orders': 1, 'total_volume': 0.01, 'realized_profit': 1}})
        assert _wait_task_status(client, second, g['group_id'], 'done')['items'][0]['status'] == 'done'
    assert client.get('/api/config/node-token', headers=second).status_code == 200
    r = client.put(f'/api/users/{second_id}/nodes', headers=first, json={'node_ids': []})
    assert r.status_code == 409 and r.json()['detail']['reason'] == 'confirm_required'
    assert client.put(f'/api/users/{second_id}/nodes', headers=first, json={'node_ids': [], 'confirm': True}).status_code == 200
    assert client.get(f'/api/nodes/{b}', headers=second).json()['owner_user_id'] == first_id
    assert not client.get(f'/api/nodes/{b}', headers=second).json()['is_primary']


def test_backfill_preflight_blockers_and_replay(client):
    h = auth_headers(client)
    uid = client.get('/api/me', headers=h).json()['user_id']
    nid = _mk_node(client, h, 81201)
    sid = _mk_strategy(client, h, 'legacy')['strategy_id']
    gid = _mk_group(client, h, 'legacy', [], sid).json()['group_id']

    async def check():
        store = state.store
        async with SessionLocal.begin() as s:
            for model, key in [(Node, nid), (NodeGroup, gid), (TradingStrategy, sid)]:
                (await s.get(model, key)).owner_user_id = None
            s.add(NodeCredential(node_id=nid, legacy_allowed=False, token_sha256='test-scoped-token-hash'))
        async with SessionLocal.begin() as s:
            report = await repair(s, store, apply_to=uid, operator='test')
            assert report['blockers'] and report['credentials'] == [nid]
            assert (await s.get(NodeGroup, gid)).owner_user_id is None
            cred = await s.get(NodeCredential, nid)
            assert cred.token_sha256 == 'test-scoped-token-hash'
            cred.token_sha256 = None
        async with SessionLocal.begin() as s:
            active = SignalDispatch(signal_id='waiting', node_id=nid, status='sent')
            s.add(active)
            await s.flush()
            refused = await repair(s, store, apply_to=uid, operator='test')
            assert any('signal_dispatch' in reason for reason in refused['blockers'])
            assert (await s.get(Node, nid)).owner_user_id is None
            active.status = 'done'
        async with SessionLocal.begin() as s:
            existing = Node(node_id='explicit', mt5_login=81202, name='existing primary', owner_user_id=uid, is_primary=True)
            s.add(existing)
            legacy = await s.get(Node, nid)
            legacy.is_primary = True
            result = await repair(s, store, apply_to=uid, operator='test')
            assert any('主节点冲突' in message for message in result['blockers'])
            assert legacy.owner_user_id is None and existing.is_primary
            legacy.is_primary = False
        async with SessionLocal.begin() as s:
            preview = await repair(s, store)
            assert not preview['blockers'] and preview['nodes'] == [nid] and preview['strategies'] == [sid]
            assert (await s.get(Node, nid)).owner_user_id is None
            assert (await repair(s, store, apply_to=uid+1, operator='test'))['blockers']
        async with SessionLocal.begin() as s:
            assert not (await repair(s, store, apply_to=uid, operator='test'))['blockers']
        async with SessionLocal.begin() as s:
            for model, key in [(Node,nid), (NodeCredential,nid), (NodeGroup,gid), (TradingStrategy,sid)]:
                assert (await s.get(model,key)).owner_user_id == uid
            assert not (await s.get(Node,nid)).is_primary
            assert not (await repair(s, store, apply_to=uid, operator='test'))['nodes']
            assert await s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'backfill_admin_ownership')) == 1
    client.portal.call(check)


def test_admin_can_follow_both_markets_as_a_personal_account(client):
    from tests.test_ai_follow_api import _setup, _mk_trend_strategy, _mk_group as market_group
    h = auth_headers(client)
    host = _setup(client, h, caps=['follow_leader', 'ai_strategy'])
    nid = _mk_node(client, h, 81301)
    trend = _mk_trend_strategy(client, host['host_h'], '模拟带单')
    gid = market_group(client, host['host_h'], [host['h_primary']], '模拟带单', trend)
    for market, channel, group in [('ai-follow', 'ai-channel', host['ai_group_id']), ('follow', 'follow-channel', gid)]:
        body = {'leader_user_id': host['host']['id'], 'group_ids': [group], 'node_ids': [nid]}
        denied = client.post(f'/api/{market}/join', headers=h, json={**body, 'node_ids': [host['h_extra']]})
        assert denied.status_code in (400, 403, 404) or (denied.status_code == 200 and not denied.json()['pending'])
        r = client.post(f'/api/{market}/join', headers=h, json=body)
        assert r.status_code == 200 and len(r.json()['pending']) == 1, r.text
        rid = r.json()['pending'][0]['request_id']
        assert client.post(f'/api/{channel}/requests/{rid}/approve', headers=host['host_h']).status_code == 200
        assert client.get(f'/api/{market}/me', headers=h).json()['following']
        assert client.post(f'/api/{market}/leave', headers=h, json=body).status_code == 200
        assert not client.get(f'/api/{market}/me', headers=h).json()['following']
