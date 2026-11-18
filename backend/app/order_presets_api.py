"""开仓参数预设 API。

路由前缀 `/api/me/order-presets`（与 `/api/me/news` 同为「me 作用域」风格）。

**权限**：登录即可读写**本人**的预设 —— 它是纯个人的交易偏好（跟单员/跟单节点
都控不了别人的手数与止损），不是某个业务模块的附属功能，所以不限菜单码。

**归属**：写死 `user_id = p.user_id`，**不读 body 里的 user_id**（防越权）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from . import order_presets, persist, strategy_templates
from .deps import client_ip, get_principal, get_store
from .permissions import Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/me/order-presets", tags=["order-presets"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 老用户种子回填：从已有的策略实例推一份预设出来
# ---------------------------------------------------------------------------
#
# 背景：本次上线前，老用户已经在【策略配置】里存过策略实例（分组 + 绑定的
# 策略规则），那些参数就是他实际在用的默认值。为了让老用户开箱即用（第一次
# 打开交易页就能看到填好的参数，而不是重新配一遍），这里在**读取时**做一次
# 一次性回填。
#
# 口径：
#   - 只对**已存在的具体品种**回填，不生成 '*' 通用模板；
#   - 只回填本人名下、且分组名符合 `{strategy_type}-{用户名}-{mt5登录号}-{品种}`
#     约定的分组（与前端 / 后端 `_group_name` 规则一致）；
#   - 回填是 best-effort：失败只记日志，不影响接口返回。

# 分组名里的策略类型前缀（与前端 dialogGroupName 一致）
_GROUP_NAME_TYPE_PREFIX = {
    "trend_market": "trend_market",
    "trend_limit": "trend_limit",
    "grid": "grid",
}


def _parse_group_name(name: str) -> tuple[str, str] | None:
    """从分组名解析出 (strategy_type, symbol)；不符合约定返回 None。

    分组名格式：`{strategy_type}-{username}-{mt5_login}-{symbol}`。
    symbol 自身不含 '-'，所以从右往左切 3 段即可。
    """
    text = str(name or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) < 4:
        return None
    symbol = parts[-1].strip().upper()
    stype = parts[0].strip().lower()
    if stype not in _GROUP_NAME_TYPE_PREFIX or not symbol:
        return None
    return stype, symbol


def _seed_payload_from_strategy(strategy: dict, stype: str) -> dict:
    """从策略实例的规则里推出预设字段。"""
    rules = strategy.get("rules") or []
    payload: dict = {}
    if stype == "grid":
        rule = strategy_templates.pick_grid_rule(rules)
        if not rule:
            return {}
        payload["grid_mode"] = rule.get("grid_mode")
        payload["trailing_up"] = bool(rule.get("trailing_up"))
        # 网格的「以损定量」= 试算预算，落在 assist_max_loss
        payload["max_allow_loss"] = rule.get("assist_max_loss")
        return payload
    rule = strategy_templates.pick_risk_sized_rule(rules)
    if not rule:
        return {}
    payload["risk_amount"] = rule.get("risk_amount")
    payload["rr_ratio"] = rule.get("rr_ratio")
    payload["base_ratio"] = rule.get("base_ratio")
    payload["breakeven_times"] = rule.get("breakeven_times")
    return payload


async def _seed_presets_from_strategies(
    p: Principal, store: RedisStore, existing: list[order_presets.OrderPreset],
) -> list[order_presets.OrderPreset]:
    """从本人名下的策略实例回填缺失的预设。返回回填后的完整列表。"""
    have = {(x.symbol, x.strategy_type) for x in existing}
    try:
        groups = await store.all_groups()
        strategies = await store.all_strategies()
    except Exception as exc:  # noqa: BLE001
        logger.warning("order preset seed: read strategies failed: %s", exc)
        return existing

    by_id = {s.get("strategy_id"): s for s in strategies if s.get("strategy_id")}
    # 只看本人名下的分组。管理员名下的分组 owner_user_id 为空（NULL），
    # 那不属于任何用户 —— 不参与回填（否则会把管理员的策略灌给每个用户）。
    mine_groups = [g for g in groups if g.get("owner_user_id") == p.user_id]
    for g in mine_groups:
        parsed = _parse_group_name(g.get("name") or "")
        if not parsed:
            continue
        stype, symbol = parsed
        if (symbol, stype) in have:
            continue
        sid = g.get("strategy_id")
        strategy = by_id.get(sid) if sid else None
        if not strategy:
            continue
        payload = _seed_payload_from_strategy(strategy, stype)
        if not payload:
            continue
        payload["symbol"] = symbol
        payload["strategy_type"] = stype
        try:
            saved = await order_presets.save_preset(p.user_id, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("order preset seed: save failed for %s/%s: %s", symbol, stype, exc)
            continue
        have.add((symbol, stype))
        existing.append(saved)
    existing.sort(key=lambda x: (x.symbol != order_presets.WILDCARD_SYMBOL, x.symbol, x.strategy_type))
    return existing


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@router.get("")
async def list_my_presets(
    p: Principal = Depends(get_principal),
    store: RedisStore = Depends(get_store),
):
    """我的开仓参数预设列表。

    读取时顺带做一次老用户种子回填（best-effort，失败不影响返回）。
    """
    presets = await order_presets.list_presets(p.user_id)
    presets = await _seed_presets_from_strategies(p, store, presets)
    return {"presets": [x.to_dict() for x in presets]}


@router.put("")
async def put_my_preset(
    body: dict,
    request: Request,
    p: Principal = Depends(get_principal),
):
    """保存一条预设（upsert）。

    字段级「传什么改什么」：不传的字段保持原值；整份传则是全量覆盖。
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体格式不正确")

    symbol = order_presets.normalize_symbol(body.get("symbol"))
    strategy_type = order_presets.normalize_strategy_type(body.get("strategy_type"))
    payload = dict(body)
    payload["symbol"] = symbol
    payload["strategy_type"] = strategy_type

    before: Optional[order_presets.OrderPreset] = await order_presets.get_preset(
        p.user_id, symbol, strategy_type,
    )
    saved = await order_presets.save_preset(p.user_id, payload)

    await persist.audit(
        p.username, "save_order_preset", f"{symbol}/{strategy_type}", None, "ok",
        client_ip(request), category="system",
        before=before.to_dict() if before else None,
        after=saved.to_dict(),
    )
    return {"preset": saved.to_dict()}


@router.delete("/{symbol}/{strategy_type}")
async def delete_my_preset(
    symbol: str,
    strategy_type: str,
    request: Request,
    p: Principal = Depends(get_principal),
):
    """删除一条预设。"""
    sym = order_presets.normalize_symbol(symbol)
    st = order_presets.normalize_strategy_type(strategy_type)
    before: Optional[order_presets.OrderPreset] = await order_presets.get_preset(
        p.user_id, sym, st,
    )
    ok = await order_presets.delete_preset(p.user_id, sym, st)
    await persist.audit(
        p.username, "delete_order_preset", f"{sym}/{st}", None,
        "ok" if ok else "not_found", client_ip(request), category="system",
        before=before.to_dict() if before else None, after=None,
    )
    return {"ok": ok}
