"""策略管理 API。

提供策略模版列表，以及策略实例的新建 / 查询 / 更新 / 删除。
新建时选择模版，复制默认规则，并绑定品种。

普通用户只能看 / 改自己创建的策略；写操作需要「策略管理」或「分组管理」菜单
（分组页里可以直接新建、编辑策略）。

AI 智能加仓策略（模版1 / tpl_1）另需 ai_strategy 能力（见 permissions），
未获授权的角色即使能进这个页面，也无法新建 / 编辑 / 删除该模版的实例。
"""
import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from . import (
    group_persist,
    group_rules,
    permissions,
    persist,
    rbac_service,
    strategy_service,
    strategy_templates,
)
from .deps import (
    client_ip,
    get_current_admin,
    get_principal,
    get_store,
    owned_strategy_or_404,
    require_menu,
)
from .state import state
from .models import (
    StrategyCreate,
    StrategyOut,
    StrategyRule,
    StrategyTemplateOut,
    StrategyUpdate,
)
from .permissions import MENU_GROUPS, MENU_STRATEGIES, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
logger = logging.getLogger(__name__)

# 热推不得拖住 PATCH：改快照 / WS 下发各自封顶，超时只打日志。
HOT_PUSH_REWRITE_TIMEOUT = 3.0


async def _hot_push_running_snapshot(strategy: dict) -> None:
    """PATCH 落库后 best-effort 改写进行中快照并 WS 下发；失败/超时不挡保存。

    仅加仓策略（模版1）热推：网格 / 趋势策略的任务从下发那一刻起冻结快照，
    整个运行周期内不受后续修改影响，新信号才用新配置。
    """
    sid = str((strategy or {}).get("strategy_id") or "")
    tid = str((strategy or {}).get("template_id") or "").strip().lower()
    if tid in (strategy_templates.TEMPLATE_2_ID, strategy_templates.TEMPLATE_3_ID):
        logger.info(
            "strategy %s snapshot frozen for running tasks (template %s)", sid, tid,
        )
        return
    try:
        snapshot = group_rules.strategy_rules_snapshot(strategy)
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy snapshot for hot-push failed %s: %s", sid, e)
        return
    if not snapshot or not sid:
        return
    try:
        subs = await asyncio.wait_for(
            group_persist.rewrite_running_strategy_snapshots(sid, snapshot),
            timeout=HOT_PUSH_REWRITE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("rewrite running snapshots timed out %s", sid)
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("rewrite running snapshots failed %s: %s", sid, e)
        return
    if not subs:
        return
    engine = state.group_dispatcher
    if engine is None:
        logger.warning("hot-push skipped %s: group dispatcher not ready", sid)
        return
    try:
        sent = await engine.push_strategy_updates(subs, snapshot)
        logger.info(
            "strategy %s hot-pushed to %d/%d running subtask(s)",
            sid, sent, len(subs),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("push strategy_update failed %s: %s", sid, e)
        return
    # 指定价加仓**一次生效**：这次热推把带指定价的配置（v1）交付给了节点，
    # 节点照它执行；配置这边立刻把指定价档位摘掉（v2），客户在网页上看到
    # 的是摘除后的配置，后续信号也不再带指定价。
    #
    # 热推路径必须也挂摘除：客户保存时任务已经在跑，指定价是随
    # `strategy_update` 热推交付的，不走新任务下发。只挂分发路径的话，
    # 热推交付的指定价会一直留在配置里（这就是「重新打开页面还在」的成因）。
    #
    # 摘除只在**确实推出去**（sent > 0）时做：没推给任何节点就不算交付，
    # 配置原样留着，客户下次保存还能带上这一档。
    if sent > 0:
        try:
            # 用分发器自己的 store：它就是分发时读策略的那个缓存层
            # （state.store 在 lifespan 之前可能是 None，热推不该依赖它）。
            await group_persist.consume_price_levels_with_cache(
                engine.store, sid, notify=True,
            )
        except Exception as e:  # noqa: BLE001
            # 摘除失败不影响热推：节点已经拿到配置在执行了，
            # 配置摘不掉只是下一次还会再带一次指定价。
            logger.warning("consume price levels after hot-push failed %s: %s", sid, e)


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
        "owner_user_id": d.get("owner_user_id"),
    }


def _deny_ai_strategy() -> None:
    """AI 智能加仓策略未授权：与菜单无权同样的 403 口径，只是文案说明原因。"""
    raise HTTPException(
        status_code=403,
        detail="AI 智能加仓策略需管理员授权后才能使用",
    )


def _to_strategy_out(
    d: dict, owner_names: dict[int, str] | None = None,
    runtime_hints: dict | None = None,
) -> StrategyOut:
    rules = [StrategyRule(**r) for r in (d.get("rules") or [])]
    owner = permissions.normalize_owner(d.get("owner_user_id"))
    return StrategyOut(
        strategy_id=d["strategy_id"],
        name=d["name"],
        template_id=d["template_id"],
        template_name=strategy_templates.live_template_name(
            d.get("template_id"), d.get("template_name")
        ) or d["template_id"],
        symbol=d["symbol"],
        enabled=d.get("enabled", True),
        rules=rules,
        remark=d.get("remark"),
        created_at=d.get("created_at", 0),
        config_version=int(d.get("config_version") or 0),
        runtime_hints=runtime_hints,
        owner_user_id=owner,
        owner_username=(owner_names or {}).get(owner) if owner is not None else None,
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
    p: Principal = Depends(get_principal),
):
    """策略列表；可选 q 按名称 / 品种模糊搜索。普通用户只看本人策略。"""
    items = permissions.visible(p, await store.all_strategies())
    if q and (term := q.strip()):
        needle = term.lower()
        items = [
            s for s in items
            if needle in (s.get("name") or "").lower()
            or needle in (s.get("symbol") or "").lower()
        ]
    items.sort(key=lambda s: (s.get("created_at", 0), s.get("name") or ""))
    names = await rbac_service.owner_names_for(p)
    out: list[StrategyOut] = []
    for s in items:
        try:
            out.append(_to_strategy_out(s, names))
        except Exception as e:  # noqa: BLE001
            logger.warning("skip invalid strategy %s: %s", (s or {}).get("strategy_id"), e)
    return out


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(
    body: StrategyCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_STRATEGIES, MENU_GROUPS)),
):
    """新建策略：归创建者所有（管理员建的归管理员名下），名称在同一所有者内唯一。"""
    if not permissions.can_create_strategy(p, body.template_id):
        _deny_ai_strategy()
    owner = permissions.owner_for_new(p)
    name = body.name.strip()
    if await strategy_service.name_exists(name, owner_user_id=owner):
        raise HTTPException(status_code=409, detail=f"策略名称已存在：{name}")
    try:
        d = await strategy_service.create_strategy(store, body, owner_user_id=owner)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await persist.audit(
        p.username, "create_strategy", d["strategy_id"], None, "ok", client_ip(request),
        category="console", before=None, after=_strategy_audit_snapshot(d),
    )
    return _to_strategy_out(d, await rbac_service.owner_names_for(p))


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: str,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    d = await owned_strategy_or_404(store, p, strategy_id)
    # 运行态提示：前端据此做「上限不能低于已开笔数」等保存前校验
    hints = await group_persist.strategy_runtime_hints(strategy_id)
    return _to_strategy_out(d, await rbac_service.owner_names_for(p), hints)


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: str,
    body: StrategyUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_STRATEGIES, MENU_GROUPS)),
):
    current = await owned_strategy_or_404(store, p, strategy_id)
    # 菜单与归属已由依赖和 owned_* 把关，这里只补 AI 加仓的能力校验。
    # StrategyUpdate 不含 template_id，模版在创建后不可改，故只需看当前实例。
    if permissions.is_ai_strategy_template(current.get("template_id")) and not (
        permissions.can_capability(p, permissions.CAP_AI_STRATEGY)
    ):
        _deny_ai_strategy()
    if body.name is not None and (name := body.name.strip()):
        if await strategy_service.name_exists(
            name, exclude_strategy_id=strategy_id, owner_user_id=current.get("owner_user_id"),
        ):
            raise HTTPException(status_code=409, detail=f"策略名称已存在：{name}")
    # 配置无条件热推：保存路径上不再做任何拦截。
    # 上限低于已开笔数、手动仓已成交、SL/TP 越过现价……这些都不再阻止保存——
    # 字段值一律采纳并热推到节点，「加不加 / 怎么加」由节点自身状态决定，
    # 结论通过逐字段回执（strategy_update_ack）告诉用户。
    before = _strategy_audit_snapshot(current)
    try:
        d = await strategy_service.update_strategy(store, strategy_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d:
        raise HTTPException(status_code=404, detail="strategy not found")
    await persist.audit(
        p.username, "update_strategy", strategy_id, body.model_dump(exclude_none=True), "ok",
        client_ip(request),
        category="console", before=before, after=_strategy_audit_snapshot(d),
    )
    # 先把 HTTP 200 发出去，避免改快照 / 节点 WS 卡住时前端 15s 超时误报保存失败。
    background_tasks.add_task(_hot_push_running_snapshot, d)
    try:
        return _to_strategy_out(d, await rbac_service.owner_names_for(p))
    except Exception as e:  # noqa: BLE001
        logger.exception("strategy out serialize failed %s: %s", strategy_id, e)
        raise HTTPException(status_code=500, detail="策略已保存，但回读失败，请刷新列表") from e


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_STRATEGIES, MENU_GROUPS)),
):
    before = _strategy_audit_snapshot(await owned_strategy_or_404(store, p, strategy_id))
    if permissions.is_ai_strategy_template(before.get("template_id")) and not (
        permissions.can_capability(p, permissions.CAP_AI_STRATEGY)
    ):
        _deny_ai_strategy()
    ok = await strategy_service.delete_strategy(store, strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="strategy not found")
    await persist.audit(
        p.username, "delete_strategy", strategy_id, None, "ok", client_ip(request),
        category="console", before=before, after=None,
    )
    return {"status": "deleted", "strategy_id": strategy_id}
