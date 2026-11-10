"""AI 跟单配置随子任务冻结，单次跟单与任务终态同事务收口。"""
from sqlalchemy import and_, select

from . import group_rules, strategy_templates
from .orm import GroupFollowerNode, GroupSignalTask, GroupTaskDispatch


async def prepare_dispatch(session, task_id, group_id, node_id):
    task = await session.get(GroupSignalTask, task_id)
    if not task or (task.strategy_snapshot_json or {}).get("template_id") != strategy_templates.TEMPLATE_1_ID:
        return None, None, None
    relation = await session.scalar(select(GroupFollowerNode).where(
        GroupFollowerNode.group_id == group_id, GroupFollowerNode.node_id == node_id,
    ).with_for_update())
    if not relation:
        return None, None, None
    settings = relation.follow_settings_json
    if relation.follow_state != "running" or relation.leader_paused:
        return relation, settings, "跟单已暂停或停止"
    if settings and settings.get("run_mode") == "single" and relation.single_dispatch_id is not None:
        return relation, settings, "单次跟单已启动一轮策略"
    return relation, settings, None


async def complete_single_rounds(session, task_id):
    rows = (await session.execute(select(GroupFollowerNode, GroupTaskDispatch).join(
        GroupTaskDispatch, and_(GroupFollowerNode.group_id == GroupTaskDispatch.group_id,
            GroupFollowerNode.node_id == GroupTaskDispatch.node_id,
            GroupFollowerNode.single_dispatch_id == GroupTaskDispatch.id),
    ).where(GroupTaskDispatch.task_id == task_id,
            GroupTaskDispatch.status.in_(group_rules.SUBTASK_TERMINAL)).with_for_update())).all()
    for relation, dispatch in rows:
        settings = dispatch.follow_settings_json or {}
        if settings.get("application_id") != (relation.follow_settings_json or {}).get("application_id"):
            continue
        if dispatch.opened_at is None and not dispatch.total_orders:
            relation.single_dispatch_id = None
        elif relation.follow_state != "stopped":
            from .ai_application_store import audit
            relation.follow_state = "stopped"
            audit(session, None, "ai_follow_single_complete", relation.id,
                  after={"group_id": relation.group_id, "node_id": relation.node_id, "dispatch_id": dispatch.id})
