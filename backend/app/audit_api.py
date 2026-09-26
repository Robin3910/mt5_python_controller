"""操作审计 API（需「操作审计」菜单；普通用户只看本人操作，管理员看全部）。"""
from fastapi import APIRouter, Depends, Query

from . import persist
from .deps import require_menu
from .models import PaginatedAudits
from .permissions import MENU_AUDITS, Principal

router = APIRouter(prefix="/api/audits", tags=["audits"])

AUDIT_CATEGORIES = ("console", "node", "auth", "system")
# 缺省只列中控台 + 节点（与既有行为一致）；all 列出全部分类
_DEFAULT_CATEGORIES = ["console", "node"]


@router.get("", response_model=PaginatedAudits)
async def list_audits(
    page: int = 1,
    page_size: int = 20,
    category: str | None = Query(
        default=None,
        description="可选过滤：console / node / auth / system / all；缺省返回中控台+节点",
    ),
    p: Principal = Depends(require_menu(MENU_AUDITS)),
):
    """分页列出操作审计（未知分类按缺省处理）。"""
    if category in AUDIT_CATEGORIES:
        cats = [category]
    elif category == "all":
        cats = list(AUDIT_CATEGORIES)
    else:
        cats = list(_DEFAULT_CATEGORIES)
    operator = None if p.is_admin else p.username
    return await persist.recent_audits(page, page_size, cats, operator=operator)
