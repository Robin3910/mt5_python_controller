"""操作审计 API（需管理员鉴权）。"""
from fastapi import APIRouter, Depends, Query

from . import persist
from .deps import get_current_admin
from .models import PaginatedAudits

router = APIRouter(prefix="/api/audits", tags=["audits"])


@router.get("", response_model=PaginatedAudits)
async def list_audits(
    page: int = 1,
    page_size: int = 20,
    category: str | None = Query(
        default=None,
        description="可选过滤：console / node；缺省返回中控台+节点",
    ),
    _: str = Depends(get_current_admin),
):
    """分页列出操作审计（默认仅中控台与节点操作）。"""
    if category in ("console", "node"):
        cats = [category]
    else:
        cats = ["console", "node"]
    return await persist.recent_audits(page, page_size, cats)
