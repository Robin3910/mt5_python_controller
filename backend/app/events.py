"""Webhook 信号事件 API（仅管理员：信号历史是全局数据，含 normal 链路对全部节点的分发明细）。"""
from fastapi import APIRouter, Depends

from . import persist
from .deps import get_store, require_admin
from .models import PaginatedSignalEvents
from .permissions import Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/signals", response_model=PaginatedSignalEvents)
async def list_signal_events(
    page: int = 1,
    page_size: int = 20,
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """分页列出 Webhook 信号：原始参数 + 各节点后续处理明细。"""
    nodes = await store.all_nodes()
    node_names = {n["node_id"]: n.get("name") or n["node_id"] for n in nodes}
    return await persist.recent_webhook_events(page, page_size, node_names)
