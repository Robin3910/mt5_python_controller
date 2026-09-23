"""系统级 key/value 配置（持久化）。

当前职责：
- 全局节点接入令牌 NODE_TOKEN —— 所有节点共享同一令牌（明文存 DB，
  Redis 作缓存，便于管理员在「账户设置」页查看/复制/重置）；
- Webhook token 校验开关 webhook_auth_enabled —— 配置页可关，落库后覆盖 ENABLE_AUTH；
- 趋势面板参数 trend_config —— 全后台共享一份（JSON 落库，不分节点/币种）；
- 客户端发布指针 client_release —— 当前发布的客户端版本及回滚落点。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from sqlalchemy import select

from . import trend_indicators
from .client_version import RELEASE_KEY as KEY_CLIENT_RELEASE
from .db import SessionLocal
from .orm import Node, SystemSetting
from .redis_store import RedisStore
from .security import gen_token
from .settings import settings

logger = logging.getLogger(__name__)

KEY_NODE_TOKEN = "node_token"
KEY_WEBHOOK_AUTH = "webhook_auth_enabled"
KEY_TREND_CONFIG = "trend_config"


async def _get_setting(key: str) -> Optional[SystemSetting]:
    async with SessionLocal() as s:
        row = await s.get(SystemSetting, key)
        return row


async def _upsert_setting(key: str, value: str) -> SystemSetting:
    async with SessionLocal() as s:
        row = await s.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key, value=value)
            s.add(row)
        else:
            row.value = value
        await s.commit()
        await s.refresh(row)
        return row


async def get_node_token(store: RedisStore) -> tuple[str, float]:
    """读取当前全局节点令牌，返回 (token, updated_at_epoch)。

    优先读 Redis 缓存；未命中再回源到 MySQL/SQLite，并刷新缓存。
    """
    cached = await store.get_node_token()
    if cached:
        row = await _get_setting(KEY_NODE_TOKEN)
        updated_at = row.updated_at.timestamp() if (row and row.updated_at) else 0.0
        return cached, updated_at
    row = await _get_setting(KEY_NODE_TOKEN)
    if row is None:
        return "", 0.0
    await store.set_node_token(row.value)
    return row.value, (row.updated_at.timestamp() if row.updated_at else time.time())


async def rotate_node_token(store: RedisStore) -> tuple[str, float]:
    """生成新的全局节点令牌，写库 + 刷新缓存，返回 (token, updated_at_epoch)。"""
    token = gen_token()
    row = await _upsert_setting(KEY_NODE_TOKEN, token)
    await store.set_node_token(token)
    return token, (row.updated_at.timestamp() if row.updated_at else time.time())


def _parse_webhook_auth(value: str | None) -> bool | None:
    if value == "1":
        return True
    if value == "0":
        return False
    return None


async def is_webhook_auth_enabled(store: RedisStore) -> bool:
    """Webhook 是否校验 AUTH_TOKEN。

    配置页保存过则以数据库为准（并走 Redis 缓存）；从未保存时用 ENABLE_AUTH，
    该环境变量未设置时默认为开启。
    """
    cached = await store.get_webhook_auth_enabled()
    if cached is not None:
        return cached
    row = await _get_setting(KEY_WEBHOOK_AUTH)
    enabled = _parse_webhook_auth(row.value if row else None)
    if enabled is None:
        # 未保存过不写入缓存，这样只改 ENABLE_AUTH 并重启即可生效。
        return bool(settings.enable_auth)
    await store.set_webhook_auth_enabled(enabled)
    return enabled


async def set_webhook_auth_enabled(store: RedisStore, enabled: bool) -> bool:
    """写入 Webhook token 校验开关，立即刷新缓存。"""
    await _upsert_setting(KEY_WEBHOOK_AUTH, "1" if enabled else "0")
    await store.set_webhook_auth_enabled(enabled)
    return enabled


async def ensure_node_token(store: RedisStore) -> str:
    """启动期幂等：若全局令牌不存在则生成一个；返回当前令牌（用于日志记录）。"""
    row = await _get_setting(KEY_NODE_TOKEN)
    if row and row.value:
        await store.set_node_token(row.value)
        return row.value
    token = gen_token()
    await _upsert_setting(KEY_NODE_TOKEN, token)
    await store.set_node_token(token)
    return token


async def _legacy_node_trend() -> dict | None:
    """兼容：若全局尚未落库，尝试从旧的 nodes.trend_json 取一份作种子。"""
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(Node).where(Node.trend_json.is_not(None)).limit(1)
            )
        ).scalar_one_or_none()
        if row is None or not isinstance(row.trend_json, dict):
            return None
        return dict(row.trend_json)


async def get_trend_config() -> dict:
    """读取全局趋势面板参数（规范化后的完整字典）。"""
    row = await _get_setting(KEY_TREND_CONFIG)
    raw: dict | None = None
    if row and row.value:
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            logger.warning("trend_config JSON 损坏，回落默认值")
    if raw is None:
        legacy = await _legacy_node_trend()
        if legacy is not None:
            # 一次性迁到全局键，之后不再读节点列
            normalized = trend_indicators.normalize_config(legacy)
            await _upsert_setting(
                KEY_TREND_CONFIG,
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            )
            return normalized
    return trend_indicators.normalize_config(raw)


async def set_trend_config(cfg: dict | None) -> dict:
    """写入全局趋势面板参数；越界值由 normalize_config 夹回。"""
    normalized = trend_indicators.normalize_config(cfg)
    await _upsert_setting(
        KEY_TREND_CONFIG,
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
    )
    return normalized


async def get_client_release() -> dict:
    """读取当前客户端发布指针；从未发布过返回空字典。"""
    row = await _get_setting(KEY_CLIENT_RELEASE)
    if not row or not row.value:
        return {}
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        logger.warning("client_release JSON 损坏，按「未发布」处理")
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def set_client_release(release: dict | None) -> dict:
    """写入客户端发布指针；传空表示撤销发布。"""
    payload = release or {}
    await _upsert_setting(
        KEY_CLIENT_RELEASE,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return payload
