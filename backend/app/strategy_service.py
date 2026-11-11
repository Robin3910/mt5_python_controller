"""策略持久化：MySQL/SQLite 为权威，Redis 作缓存。

策略基于内置模版创建：选择模版后复制默认规则到实例，并绑定品种。
策略归创建者所有（owner_user_id，空 = 管理员名下），名称在同一所有者内唯一。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import select

from . import permissions
from .db import SessionLocal
from .models import StrategyCreate, StrategyUpdate
from .orm import TradingStrategy
from .redis_store import RedisStore
from .security import make_strategy_id
from . import strategy_templates as templates

logger = logging.getLogger(__name__)


def strategy_row_to_dict(row: TradingStrategy) -> dict:
    return {
        "strategy_id": row.strategy_id,
        "name": row.name,
        "template_id": row.template_id,
        "template_name": templates.live_template_name(
            row.template_id, row.template_name
        ),
        "symbol": row.symbol,
        "enabled": row.enabled,
        "rules": templates.normalize_rules(row.config_json or []),
        "remark": row.remark,
        "config_version": int(row.config_version or 0),
        "owner_user_id": row.owner_user_id,
        "created_at": row.created_at.timestamp() if row.created_at else time.time(),
    }


async def warm_cache(store: RedisStore) -> int:
    async with SessionLocal() as s:
        rows = (await s.execute(select(TradingStrategy))).scalars().all()
    for row in rows:
        await store.cache_strategy(strategy_row_to_dict(row))
    return len(rows)


async def reload_strategy_cache(store: RedisStore, strategy_id: str) -> Optional[dict]:
    """重新读库并刷新 Redis 里的策略缓存；返回刷新后的策略（读不到返回 None）。

    后端直接改写 `config_json`（如指定价加仓用一次即摘除）后必须调这里：
    缓存是分发时读策略的权威入口（见 group_dispatcher 的 store.get_strategy），
    不刷新的话下一条信号还会带上刚被摘掉的指定价档位。
    """
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    try:
        async with SessionLocal() as s:
            row = await s.get(TradingStrategy, sid)
            if row is None:
                return None
            d = strategy_row_to_dict(row)
        await store.cache_strategy(d)
        return d
    except Exception as e:  # noqa: BLE001
        logger.warning("reload strategy cache failed %s: %s", sid, e)
        return None


def strip_price_levels_from_rules(
    rules: list[dict],
) -> tuple[list[dict], bool]:
    """把规则里的指定价档位摘掉（见 `templates.strip_price_levels`）。

    保存路径上的包装：返回值与底层纯函数一致（(新规则, 是否有改动)）。
    """
    return templates.strip_price_levels(rules)


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


async def name_exists(
    name: str,
    exclude_strategy_id: Optional[str] = None,
    *,
    owner_user_id: Optional[int] = None,
) -> bool:
    """策略名称在同一所有者内是否已被占用。"""
    owner = permissions.normalize_owner(owner_user_id)
    async with SessionLocal() as s:
        stmt = select(TradingStrategy.strategy_id).where(TradingStrategy.name == name)
        stmt = stmt.where(
            TradingStrategy.owner_user_id.is_(None) if owner is None
            else TradingStrategy.owner_user_id == owner
        )
        if exclude_strategy_id:
            stmt = stmt.where(TradingStrategy.strategy_id != exclude_strategy_id)
        return (await s.execute(stmt)).first() is not None


async def create_strategy(
    store: RedisStore, payload: StrategyCreate, *, owner_user_id: Optional[int] = None,
) -> dict:
    tpl = templates.get_template(payload.template_id)
    if not tpl:
        raise ValueError(f"策略模版不存在：{payload.template_id}")
    symbol = normalize_symbol(payload.symbol)
    if not symbol:
        raise ValueError("请填写绑定品种")
    strategy_id = make_strategy_id()
    if payload.rules is not None:
        rules = templates.normalize_rules([r.model_dump() for r in payload.rules])
        if not rules:
            raise ValueError("请至少配置一条规则")
    else:
        rules = templates.normalize_rules(tpl.get("rules") or [])
    bad = templates.validate_rules_for_template(tpl["template_id"], rules)
    if bad:
        raise ValueError(bad)
    async with SessionLocal() as s:
        s.add(
            TradingStrategy(
                strategy_id=strategy_id,
                name=payload.name.strip(),
                template_id=tpl["template_id"],
                template_name=tpl["name"],
                symbol=symbol,
                enabled=payload.enabled,
                config_json=rules,
                remark=(payload.remark or "").strip() or None,
                owner_user_id=permissions.normalize_owner(owner_user_id),
                # 配置版本从 1 起：首个热推的版本号是 2，节点侧版本门能正确比较
                config_version=1,
            )
        )
        await s.commit()
        row = await s.get(TradingStrategy, strategy_id)
        d = strategy_row_to_dict(row)
    await store.cache_strategy(d)
    return d


async def update_strategy(store: RedisStore, strategy_id: str, patch: StrategyUpdate) -> Optional[dict]:
    from .node_lifecycle import group_key, locked
    from .orm import NodeGroup
    async with SessionLocal() as session:
        group_ids = (await session.scalars(select(NodeGroup.group_id).where(NodeGroup.strategy_id == strategy_id))).all()
    async with locked(*(group_key(gid) for gid in group_ids)):
        return await _update_strategy(store, strategy_id, patch)


async def _update_strategy(
    store: RedisStore, strategy_id: str, patch: StrategyUpdate,
) -> Optional[dict]:
    async with SessionLocal() as s:
        row = await s.get(TradingStrategy, strategy_id)
        if not row:
            return None
        if patch.name is not None and patch.name.strip():
            row.name = patch.name.strip()
        if patch.symbol is not None:
            symbol = normalize_symbol(patch.symbol)
            if not symbol:
                raise ValueError("请填写绑定品种")
            row.symbol = symbol
        if patch.enabled is not None:
            row.enabled = patch.enabled
            if patch.enabled is False:
                from .ai_application_store import cancel_applications
                from .orm import NodeGroup
                group_ids = (await s.scalars(select(NodeGroup.group_id).where(NodeGroup.strategy_id == strategy_id))).all()
                await cancel_applications(s, "机器人策略已停用", group_ids=group_ids, pending_only=True)
                from .follow_requests import cancel
                await cancel(s, "带单策略已停用", group_ids=group_ids)

        if patch.remark is not None:
            row.remark = patch.remark.strip() or None
        if patch.rules is not None:
            rules = templates.normalize_rules(
                [r.model_dump() for r in patch.rules]
            )
            bad = templates.validate_rules_for_template(row.template_id, rules)
            if bad:
                raise ValueError(bad)
            row.config_json = rules
        # 配置版本 +1：热推时随快照下发，节点用它做版本门
        row.config_version = int(row.config_version or 0) + 1
        await s.commit()
        d = strategy_row_to_dict(row)
    await store.cache_strategy(d)
    return d


async def delete_strategy(store: RedisStore, strategy_id: str) -> bool:
    from .node_lifecycle import group_key, locked
    from .orm import NodeGroup
    async with SessionLocal() as session:
        group_ids = (await session.scalars(select(NodeGroup.group_id).where(NodeGroup.strategy_id == strategy_id))).all()
    async with locked(*(group_key(gid) for gid in group_ids)):
        return await _delete_strategy(store, strategy_id)


async def _delete_strategy(store: RedisStore, strategy_id: str) -> bool:
    from . import group_service

    async with SessionLocal() as s:
        row = await s.get(TradingStrategy, strategy_id)
        if not row:
            return False
        await s.delete(row)
        await s.commit()
    await store.delete_strategy(strategy_id)
    await group_service.clear_strategy_bindings(store, strategy_id)
    return True
