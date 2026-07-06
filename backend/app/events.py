"""Webhook 信号事件 API（需管理员鉴权）。"""
from fastapi import APIRouter, Depends

from . import persist
from .deps import get_current_admin, get_store
from .models import PaginatedSignalEvents
from .redis_store import RedisStore

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/signals", response_model=PaginatedSignalEvents)
async def list_signal_events(
    page: int = 1,
    page_size: int = 20,
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """分页列出 Webhook 信号：原始参数 + 各节点后续处理明细。"""
    nodes = await store.all_nodes()
    node_names = {n["node_id"]: n.get("name") or n["node_id"] for n in nodes}
    return await persist.recent_webhook_events(page, page_size, node_names)
