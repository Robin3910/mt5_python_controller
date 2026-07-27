"""策略管理 API（需管理员鉴权）。

提供策略模版列表，以及策略实例的新建 / 查询 / 更新 / 删除。
新建时选择模版，复制默认规则，并绑定品种。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist, strategy_service, strategy_templates
from .deps import client_ip, get_current_admin, get_store
from .models import (
    StrategyCreate,
    StrategyOut,
    StrategyRule,
    StrategyTemplateOut,
    StrategyUpdate,
)
from .redis_store import RedisStore

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _strategy_audit_snapshot(d: dict | None) -> dict | None:
    if not d:
        return None
    return {
        "strategy_id": d.get("strategy_id"),
        "name": d.get("name"),
        "template_id": d.get("template_id"),
        "template_name": d.get("template_name"),
        "symbol": d.get("symbol"),
        "enabled": d.get("enabled", True),
        "remark": d.get("remark"),
        "rules": d.get("rules") or [],
    }


def _to_strategy_out(d: dict) -> StrategyOut:
    rules = [StrategyRule(**r) for r in (d.get("rules") or [])]
    return StrategyOut(
        strategy_id=d["strategy_id"],
        name=d["name"],
        template_id=d["template_id"],
        template_name=d.get("template_name") or d["template_id"],
        symbol=d["symbol"],
        enabled=d.get("enabled", True),
        rules=rules,
        remark=d.get("remark"),
        created_at=d.get("created_at", 0),
    )


@router.get("/templates", response_model=list[StrategyTemplateOut])
async def list_strategy_templates(_: str = Depends(get_current_admin)):
    """可用策略模版列表（只读）。"""
    out: list[StrategyTemplateOut] = []
    for t in strategy_templates.list_templates():
        out.append(
            StrategyTemplateOut(
                template_id=t["template_id"],
                name=t["name"],
                description=t.get("description") or "",
                rules=[StrategyRule(**r) for r in (t.get("rules") or [])],
            )
        )
    return out


@router.get("", response_model=list[StrategyOut])
async def list_strategies(
    q: str | None = None,
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """策略列表；可选 q 按名称 / 品种模糊搜索。"""
    items = await store.all_strategies()
    if q and (term := q.strip()):
        needle = term.lower()
        items = [
            s for s in items
            if needle in (s.get("name") or "").lower()
            or needle in (s.get("symbol") or "").lower()
        ]
    items.sort(key=lambda s: (s.get("created_at", 0), s.get("name") or ""))
    return [_to_strategy_out(s) for s in items]


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(
    body: StrategyCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    name = body.name.strip()
    if await strategy_service.name_exists(name):
        raise HTTPException(status_code=409, detail=f"策略名称已存在：{name}")
    try:
        d = await strategy_service.create_strategy(store, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await persist.audit(
        admin, "create_strategy", d["strategy_id"], None, "ok", client_ip(request),
        category="console", before=None, after=_strategy_audit_snapshot(d),
    )
    return _to_strategy_out(d)


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: str,
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    d = await store.get_strategy(strategy_id)
    if not d:
        raise HTTPException(status_code=404, detail="strategy not found")
    return _to_strategy_out(d)


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: str,
    body: StrategyUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    if body.name is not None and (name := body.name.strip()):
        if await strategy_service.name_exists(name, exclude_strategy_id=strategy_id):
            raise HTTPException(status_code=409, detail=f"策略名称已存在：{name}")
    before = _strategy_audit_snapshot(await store.get_strategy(strategy_id))
    try:
        d = await strategy_service.update_strategy(store, strategy_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d:
        raise HTTPException(status_code=404, detail="strategy not found")
    await persist.audit(
        admin, "update_strategy", strategy_id, body.model_dump(exclude_none=True), "ok",
        client_ip(request),
        category="console", before=before, after=_strategy_audit_snapshot(d),
    )
    return _to_strategy_out(d)


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    before = _strategy_audit_snapshot(await store.get_strategy(strategy_id))
    ok = await strategy_service.delete_strategy(store, strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="strategy not found")
    await persist.audit(
        admin, "delete_strategy", strategy_id, None, "ok", client_ip(request),
        category="console", before=before, after=None,
    )
    return {"status": "deleted", "strategy_id": strategy_id}
