"""策略持久化：MySQL/SQLite 为权威，Redis 作缓存。

策略基于内置模版创建：选择模版后复制默认规则到实例，并绑定品种。
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import StrategyCreate, StrategyUpdate
from .orm import TradingStrategy
from .redis_store import RedisStore
from .security import make_strategy_id
from . import strategy_templates as templates


def strategy_row_to_dict(row: TradingStrategy) -> dict:
    return {
        "strategy_id": row.strategy_id,
        "name": row.name,
        "template_id": row.template_id,
        "template_name": row.template_name,
        "symbol": row.symbol,
        "enabled": row.enabled,
        "rules": templates.normalize_rules(row.config_json or []),
        "remark": row.remark,
        "created_at": row.created_at.timestamp() if row.created_at else time.time(),
    }


async def warm_cache(store: RedisStore) -> int:
    async with SessionLocal() as s:
        rows = (await s.execute(select(TradingStrategy))).scalars().all()
    for row in rows:
        await store.cache_strategy(strategy_row_to_dict(row))
    return len(rows)


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


async def name_exists(name: str, exclude_strategy_id: Optional[str] = None) -> bool:
    async with SessionLocal() as s:
        stmt = select(TradingStrategy.strategy_id).where(TradingStrategy.name == name)
        if exclude_strategy_id:
            stmt = stmt.where(TradingStrategy.strategy_id != exclude_strategy_id)
        return (await s.execute(stmt)).first() is not None


async def create_strategy(store: RedisStore, payload: StrategyCreate) -> dict:
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
            )
        )
        await s.commit()
        row = await s.get(TradingStrategy, strategy_id)
        d = strategy_row_to_dict(row)
    await store.cache_strategy(d)
    return d


async def update_strategy(
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
        await s.commit()
        d = strategy_row_to_dict(row)
    await store.cache_strategy(d)
    return d


async def delete_strategy(store: RedisStore, strategy_id: str) -> bool:
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
