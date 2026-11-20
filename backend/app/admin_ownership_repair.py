"""维护窗口内修复旧资产归属。默认只读；不由应用启动过程调用。"""
import argparse
import asyncio
import getpass
import json
from collections import Counter

from sqlalchemy import or_, select

from . import group_rules, group_service, node_service, rbac_service, strategy_service
from .db import SessionLocal
from .orm import (AiFollowApplication, AiFollowApplicationNode, AuditLog, FollowRequest,
                  GroupFollowerNode, GroupSignalTask, GroupTaskDispatch, Node, NodeCredential,
                  NodeGroup, NodeGroupMember, SignalDispatch, TradingStrategy)
from .redis_store import (K_CANCEL_PENDING_BUSY, K_CANCEL_PENDING_LOCK, K_EXEC_LOCK, K_GROUP_NODE_BUSY,
                          K_LIMIT_WATCH_LOCK, K_NODE_PENDING_STOP, RedisStore)


async def repair(session, store, *, apply_to=None, operator=None):
    # 执行时所有写入者须离线；预检和执行使用同一计算，执行不信任旧报告。
    async def rows(model, condition, key):
        stmt = select(model).where(condition).order_by(key)
        return list(await session.scalars(stmt.with_for_update() if apply_to is not None else stmt))

    owner = await rbac_service.admin_owner_id(session)
    nodes = await rows(Node, Node.owner_user_id.is_(None), Node.node_id)
    groups = await rows(NodeGroup, NodeGroup.owner_user_id.is_(None), NodeGroup.group_id)
    strategies = await rows(TradingStrategy, TradingStrategy.owner_user_id.is_(None), TradingStrategy.strategy_id)
    ns, gs, ss = [n.node_id for n in nodes], [g.group_id for g in groups], [s.strategy_id for s in strategies]
    blocks = []
    report = dict(admin_id=owner, nodes=ns, groups=gs, strategies=ss, credentials=[], followers=[], counts={'nodes': len(ns), 'groups': len(gs), 'strategies': len(ss)}, blockers=blocks)
    if apply_to is not None and apply_to != owner:
        blocks.append('目标管理员已变化')
    if not (ns or gs or ss):
        return report
    for model, items in ((NodeGroup, groups), (TradingStrategy, strategies)):
        names = Counter(item.name.casefold() for item in items)
        for name in await session.scalars(select(model.name).where(model.owner_user_id == owner)):
            if name.casefold() in names:
                names[name.casefold()] += 1
        blocks.extend(f'{model.__tablename__} 同名: {name}' for name, count in names.items() if count > 1)

    primary = [n.node_id for n in nodes if n.is_primary]
    primary += list(await session.scalars(select(Node.node_id).where(Node.owner_user_id == owner, Node.is_primary.is_(True))))
    if any(n.is_primary for n in nodes) and len(primary) > 1:
        blocks.append(f'主节点冲突: {primary}')
    pairs = (await session.execute(select(NodeGroupMember, Node, NodeGroup).outerjoin(Node, Node.node_id == NodeGroupMember.node_id)
             .outerjoin(NodeGroup, NodeGroup.group_id == NodeGroupMember.group_id)
             .where(or_(NodeGroupMember.node_id.in_(ns), NodeGroupMember.group_id.in_(gs))))).all()
    for member, node, group in pairs:
        if not node or not group or node.owner_user_id not in (None, owner) or group.owner_user_id not in (None, owner):
            blocks.append(f'成员归属不一致: {member.group_id}/{member.node_id}')
        elif group.enabled and node.is_primary and node.node_id in primary:
            blocks.append(f'可能新增带单源: {member.group_id}/{member.node_id}')
    for group, strategy in (await session.execute(select(NodeGroup, TradingStrategy).outerjoin(TradingStrategy,
            TradingStrategy.strategy_id == NodeGroup.strategy_id).where(NodeGroup.strategy_id.is_not(None),
            or_(NodeGroup.group_id.in_(gs), NodeGroup.strategy_id.in_(ss))))).all():
        if not strategy or group.owner_user_id not in (None, owner) or strategy.owner_user_id not in (None, owner):
            blocks.append(f'策略绑定归属不一致: {group.group_id}')
    for key, condition in (
        (GroupSignalTask.task_id, GroupSignalTask.group_id.in_(gs) & GroupSignalTask.status.in_(group_rules.TASK_ACTIVE)),
        (GroupTaskDispatch.id, or_(GroupTaskDispatch.node_id.in_(ns), GroupTaskDispatch.group_id.in_(gs)) &
         GroupTaskDispatch.status.notin_(group_rules.SUBTASK_TERMINAL)),
        (SignalDispatch.id, SignalDispatch.node_id.in_(ns) & SignalDispatch.status.in_(['pending', 'sent'])),
    ):
        blocks.extend(f'未收口 {key.class_.__tablename__}: {i}' for i in await session.scalars(select(key).where(condition)))
    for gid in gs:
        if await anext(store.r.scan_iter(match=K_GROUP_NODE_BUSY.format(gid, '*')), None) is not None:
            blocks.append(f'分组仍有执行占位: {gid}')
    for nid in ns:
        account = await store.get_account(nid) or {}
        if account.get('positions') or account.get('orders'):
            blocks.append(f'仍有持仓或挂单: {nid}')
        for pattern in (K_EXEC_LOCK.format(nid, '*'), K_LIMIT_WATCH_LOCK.format(nid, '*'), K_GROUP_NODE_BUSY.format('*', nid), K_NODE_PENDING_STOP.format(nid),
                        K_CANCEL_PENDING_LOCK.format(nid), K_CANCEL_PENDING_BUSY.format(nid)):
            if await anext(store.r.scan_iter(match=pattern), None) is not None:
                blocks.append(f'节点仍有运行态: {nid}')
                break
    credentials = await rows(NodeCredential, NodeCredential.node_id.in_(ns), NodeCredential.node_id)
    for cred in credentials:
        report['credentials'].append(cred.node_id)
        if cred.owner_user_id is not None or cred.token_sha256:
            blocks.append(f'须先核实并按既有流程处理专属凭据: {cred.node_id}')
    for item in await rows(GroupFollowerNode, or_(GroupFollowerNode.group_id.in_(gs), GroupFollowerNode.node_id.in_(ns)), GroupFollowerNode.id):
        report['followers'].append(item.id)
        blocks.append(f'须核对跟单与信号接收方: {item.group_id}/{item.node_id}')
    for model, condition in ((FollowRequest, or_(FollowRequest.node_id.in_(ns), FollowRequest.group_id.in_(gs), FollowRequest.leader_primary_node_id.in_(ns))),
                             (AiFollowApplication, or_(AiFollowApplication.group_id.in_(gs), AiFollowApplication.leader_primary_node_id.in_(ns),
                              AiFollowApplication.id.in_(select(AiFollowApplicationNode.application_id).where(AiFollowApplicationNode.node_id.in_(ns)))))):
        blocks.extend(f'须核对申请归属: {model.__tablename__}/{i}' for i in await session.scalars(select(model.id).where(condition, model.status.in_(['pending', 'approved', 'rejected']))))
    if apply_to is not None and not blocks:
        for row in [*nodes, *groups, *strategies, *credentials]:
            row.owner_user_id = owner
        session.add(AuditLog(operator=operator, action='backfill_admin_ownership', category='system', result='ok',
                             before_json={'owner_user_id': None}, after_json=report))
    return report


async def main(apply_to):
    store = RedisStore.from_url()
    try:
        await store.r.ping()
        async with SessionLocal.begin() as session:
            report = await repair(session, store, apply_to=apply_to, operator=getpass.getuser())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report['blockers']:
            return 2
        if apply_to is not None:
            async with SessionLocal() as session:
                for node in await session.scalars(select(Node)):
                    await store.cache_node(node_service.node_row_to_dict(node))
            for service in (strategy_service, group_service):
                await service.warm_cache(store)
        return 0
    finally:
        await store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-to', type=int, metavar='ADMIN_ID', help='备份并停写后，确认预检中的管理员 ID 才执行')
    raise SystemExit(asyncio.run(main(parser.parse_args().apply_to)))
