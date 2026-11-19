"""AI 跟单首单余额，按用户、分组和账户持久化。"""

import logging
from collections.abc import Iterable
from datetime import datetime
from math import isfinite

from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError

from .db import SessionLocal
from .orm import (
    AiFollowApplication, AiFollowApplicationNode, AiFollowFirstOpen, Deal, GroupSignalTask, GroupTaskDispatch, Node,
)
from .strategy_templates import TEMPLATE_1_ID

logger = logging.getLogger(__name__)
FILLED_RETCODES = (10009, 10010)


async def _earlier_open(session, dispatch: GroupTaskDispatch, user_id: int, deal_id: int):
    applications = select(AiFollowApplication.id).join(AiFollowApplicationNode,
        AiFollowApplicationNode.application_id == AiFollowApplication.id).where(
        AiFollowApplication.follower_user_id == user_id,
        AiFollowApplication.group_id == dispatch.group_id,
        AiFollowApplicationNode.node_id == dispatch.node_id,
    )
    legacy_join = select(func.min(AiFollowApplicationNode.joined_at)).join(AiFollowApplication,
        AiFollowApplication.id == AiFollowApplicationNode.application_id).where(
        AiFollowApplication.id.in_(applications), AiFollowApplication.source == "legacy",
    ).scalar_subquery()
    application_id = GroupTaskDispatch.follow_settings_json["application_id"].as_integer()
    reported_deal = case((GroupTaskDispatch.retcode.in_(FILLED_RETCODES), func.nullif(GroupTaskDispatch.deal, 0)))
    ticket = func.coalesce(reported_deal, Deal.deal_id)
    statement = (
        select(ticket)
        .select_from(GroupTaskDispatch)
        .join(GroupSignalTask, GroupSignalTask.task_id == GroupTaskDispatch.task_id)
        .outerjoin(Deal, and_(
            Deal.node_id == GroupTaskDispatch.node_id, Deal.magic == GroupTaskDispatch.magic,
            Deal.entry_type.in_(("IN", "INOUT")), Deal.volume > 0,
        ))
        .where(
            GroupTaskDispatch.group_id == dispatch.group_id,
            GroupTaskDispatch.node_id == dispatch.node_id,
            GroupTaskDispatch.id != dispatch.id,
            or_(application_id.in_(applications), and_(application_id.is_(None),
                GroupTaskDispatch.dispatched_at >= legacy_join,
                GroupSignalTask.strategy_snapshot_json["template_id"].as_string() == TEMPLATE_1_ID)),
            or_(ticket.between(1, deal_id - 1), and_(ticket.is_(None),
                GroupTaskDispatch.retcode.in_(FILLED_RETCODES),
                GroupTaskDispatch.opened_at < (dispatch.opened_at or datetime.now()))),
        )
        .order_by(ticket)
        .limit(1)
    )
    return (await session.execute(statement)).first()


async def record(node_id: str, dispatch_id: int, result: dict, balance: object) -> None:
    deal_id = result.get("deal")
    if (result.get("success") is not True or result.get("pending") or result.get("retcode") not in FILLED_RETCODES
            or type(deal_id) is not int or not 0 < deal_id <= 2**63 - 1):
        return
    try:
        value = float(balance) if type(balance) in (int, float) else None
    except OverflowError:
        value = None
    if value is not None and not isfinite(value):
        value = None
    await _record_confirmed_open(node_id, dispatch_id, deal_id, value)


async def _record_confirmed_open(node_id: str, dispatch_id: int, deal_id: int, value: float | None) -> None:
    try:
        async with SessionLocal() as session:
            row = (await session.execute(select(GroupTaskDispatch, AiFollowApplication.follower_user_id)
                .select_from(GroupTaskDispatch)
                .join(AiFollowApplication, and_(
                    AiFollowApplication.id == GroupTaskDispatch.follow_settings_json["application_id"].as_integer(),
                    AiFollowApplication.group_id == GroupTaskDispatch.group_id))
                .join(AiFollowApplicationNode, and_(AiFollowApplicationNode.application_id == AiFollowApplication.id,
                    AiFollowApplicationNode.node_id == GroupTaskDispatch.node_id))
                .where(GroupTaskDispatch.id == dispatch_id, GroupTaskDispatch.node_id == node_id))).one_or_none()
            if row is None:
                return
            dispatch, user_id = row
            if dispatch.deal not in (None, 0, deal_id):
                return
            # 按账户串行化首次写入，保证并发或重报时仍只保留一条首单。
            if await session.scalar(select(Node.node_id).where(Node.node_id == node_id).with_for_update()) is None:
                return
            key = (user_id, dispatch.group_id, node_id)
            saved = await session.get(AiFollowFirstOpen, key, with_for_update=True)
            if saved is None:
                earlier = await _earlier_open(session, dispatch, user_id, deal_id)
                session.add(AiFollowFirstOpen(follower_user_id=user_id, group_id=dispatch.group_id, node_id=node_id,
                    deal_id=deal_id if earlier is None else earlier[0], balance=value if earlier is None else None))
            elif saved.deal_id is not None and (
                deal_id < saved.deal_id or (deal_id == saved.deal_id and saved.balance is None)
            ):
                saved.deal_id, saved.balance = deal_id, value
            else:
                return
            await session.commit()
    except SQLAlchemyError:
        logger.warning("保存 AI 跟单首单余额失败 node=%s dispatch=%s", node_id, dispatch_id, exc_info=True)


async def record_unreported_opens(node_id: str, deal_ids: list[int]) -> None:
    """历史成交晚到时补记首单身份，避免把以后开仓的余额当成期初余额。"""
    try:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(GroupTaskDispatch.id, func.min(Deal.deal_id))
                .select_from(GroupTaskDispatch)
                .join(Deal, and_(Deal.node_id == GroupTaskDispatch.node_id, Deal.magic == GroupTaskDispatch.magic))
                .where(
                    Deal.node_id == node_id, Deal.deal_id.in_(deal_ids),
                    Deal.entry_type.in_(("IN", "INOUT")), Deal.volume > 0,
                    GroupTaskDispatch.follow_settings_json["application_id"].as_integer().is_not(None),
                    func.coalesce(GroupTaskDispatch.deal, 0) == 0,
                )
                .group_by(GroupTaskDispatch.id)
            )).all()
    except SQLAlchemyError:
        logger.warning("核对 AI 跟单首单成交失败 node=%s", node_id, exc_info=True)
        return
    for dispatch_id, deal_id in rows:
        await _record_confirmed_open(node_id, dispatch_id, deal_id, None)


async def list_first_opens(user_id: int, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], AiFollowFirstOpen]:
    pairs = set(pairs)
    if not pairs:
        return {}
    async with SessionLocal() as session:
        rows = (await session.scalars(select(AiFollowFirstOpen).where(
            AiFollowFirstOpen.follower_user_id == user_id,
            tuple_(AiFollowFirstOpen.group_id, AiFollowFirstOpen.node_id).in_(pairs),
        ))).all()
    return {(row.group_id, row.node_id): row for row in rows}


def total_balance(opens: Iterable[AiFollowFirstOpen | None]) -> float | None:
    balances = []
    for opening in opens:
        if opening is None or opening.balance is None:
            return None
        balances.append(opening.balance)
    total = sum(balances)
    return round(total, 2) if balances and isfinite(total) else None
