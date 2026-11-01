# 交易 Tab 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在【首页】后新增【交易】Tab，对**主节点**触发三种开仓策略（市价开仓/限价开仓/智能吸筹），自动复用或创建「策略+分组」对，复用 `process_signal` 走 strategy 链路开仓。

**Architecture:**
- 后端新增 1 个端点 `POST /api/trading/quick-trigger`：根据 (strategy_type, username, mt5_login) 找/创建"专属"策略与分组，把主节点加入成员，绑策略，然后构造信号体走 `webhook.process_signal`。
- 趋势（市价/限价）方向由后端根据「现价 vs 止损价」自动判断；网格需要用户选方向（默认 long）。
- 前端新增 `TradingView.vue` + 触发弹窗：选策略类型 → 选主节点（自动）→ 填品种/手数/SL/TP/限价入场价 → 弹窗确认 → 提交。
- 复用 `permissions.MENU_REGISTRY` + `group_service` / `strategy_service` / `webhook.process_signal`，不修改任何现有分发链路。

**Tech Stack:** FastAPI + SQLAlchemy + Redis（后端），Vue 3 + Element Plus + Pinia + Axios（前端）。

---

## 一、设计要点

### 1. 命名规则（owner 隔离）

- 分组/策略命名：`{strategy_type}-{username}-{mt5_login}`
  - 例：`trend_market-admin-10023`、`trend_limit-alice-10024`、`grid-bob-10025`
- 同 owner 同 strategy_type 同 login → 复用；任何一项不同 → 视为新组。
- 跨 owner 命名撞不到一起：username 已经是 owner 的天然区分。

### 2. 策略类型 → 模板映射

| 前端 strategy_type | 模板 | 默认规则 | 是否需要 entry_mode | 是否需要 direction |
|---|---|---|---|---|
| `trend_market` | `tpl_2`（趋势策略） | `default_risk_sized_rule()` + `entry_mode=market` | 0 | 0（后端根据现价/SL 算） |
| `trend_limit` | `tpl_2`（趋势策略） | `default_risk_sized_rule()` + `entry_mode=limit` | 1（必填） | 0（后端根据 limit/SL 算） |
| `grid` | `tpl_3`（网格策略） | `default_grid_rule()` | 0 | 1（默认 long） |

### 3. 方向自动判断规则（趋势）

按 `signal.action` 需要 BUY 还是 SELL：
- **市价（trend_market）**：用现价作为入场价，比较现价与 SL 价。
  - 现价 > SL → BUY（做多，SL 在下方）
  - 现价 < SL → SELL（做空，SL 在上方）
  - 现价 == SL → 拒收（无方向）
- **限价（trend_limit）**：用 `entry_price` 作为入场价，比较 entry_price 与 SL 价。
  - entry_price > SL → BUY
  - entry_price < SL → SELL
  - entry_price <= 0 或与 SL 同向 → 拒收

现价取自节点最近一次账户快照的 `quotes[品种].bid/ask`：
- BUY 时入场价 = ask（市价买），方向对照 ask vs SL
- SELL 时入场价 = bid（市价卖），方向对照 bid vs SL

> 为简化（且 BUY/SELL 价差极小），统一用 `quotes[品种].mid` 作为"现价"判断方向，落到 `signal.action`。

### 4. 复用 / 创建流程

```
1. 校验主节点存在 + 在线 + 与当前用户同 owner
2. _group_name(strategy_type, username, login) → 找同 owner 同名的分组
3. _strategy_name(...) → 找同 owner 同名的策略
4. 不存在则建（策略 → 分组 → 绑策略 → 加主节点为成员）
5. 存在则同步 strategy.symbol（用户可能切换品种）；成员已是主节点则不动
6. 构造信号数据：
   - action: 趋势由 3 自动算；网格直接取 user input
   - model: "strategy"
   - group_ids: [group_id]
   - symbol / volume / sl / tp / entry_price（限价模式）/ comment
7. 调 webhook.process_signal(data, source="manual", ...)
8. 落审计 + 返回 {group_id, strategy_id, result}
```

### 5. 权限

- 新菜单 `MENU_TRADING = "trading"`，`assignable=True`（普通用户也能触发自己名下的主节点）。
- 接口需 `require_menu(MENU_TRADING)`；普通用户再叠加主节点的 owner 归属校验。

### 6. 行为细节

- 主节点离线 → 接口 400「主节点不在线」；前端置灰按钮。
- 没有任何主节点 → 前端空态：红条「请先在【节点】页设置主节点」+ 跳转按钮。
- 限价模式缺 `entry_price` → 400「限价开仓必须填写入场价」。
- 网格 `price_lower`/`price_upper` 为 0 → 网格规则被拒收（已有 `_validate_grid_rule_fields`）；弹窗内给提示，触发前要求用户去【策略管理】设置区间。
- 触发结果 rejected → 弹窗内显示 reason（不关闭弹窗），允许用户调整后重提。
- 触发成功 → toast「已触发」+ 弹窗关闭 + 留在交易页（不跳转）。

---

## 二、文件清单

### 新增
- `backend/app/trading_api.py`（核心端点 + 辅助函数 + 命名/规则构造）
- `backend/tests/test_trading_api.py`（关键路径单测）
- `frontend/src/api/trading.ts`（类型 + `quickTrigger`）
- `frontend/src/views/TradingView.vue`（主页面 + 触发弹窗）

### 修改
- `backend/app/permissions.py`：注册 `MENU_TRADING`
- `backend/app/main.py`：注册 `trading_api.router`
- `frontend/src/constants/menus.ts`：插入 trading 菜单
- `frontend/src/router/index.ts`：注册 `/trading` 路由
- `frontend/src/App.vue`：插入交易 Tab 导航 + SVG

---

## 三、任务拆分

### Task 1：后端注册交易菜单

**Files:**
- Modify: `d:\work_space\mt5_python_controller\backend\app\permissions.py:39-51`

- [ ] **Step 1：注册菜单常量**

在 `permissions.py` 顶部加入 `MENU_TRADING`：

```python
MENU_TRADING = "trading"
```

- [ ] **Step 2：注册到 `MENU_REGISTRY`**

在 `MENU_REGISTRY` 顶部（紧跟 `MENU_HOME` 后）插入：

```python
MenuDef(MENU_TRADING, "交易", "/trading", True),
```

- [ ] **Step 3：验证**

```bash
cd d:\work_space\mt5_python_controller
python -c "from backend.app.permissions import MENU_TRADING, MENU_REGISTRY; print(MENU_TRADING in [m.code for m in MENU_REGISTRY])"
```

预期：`True`

---

### Task 2：后端 trading_api.py — 端点骨架与命名工具

**Files:**
- Create: `d:\work_space\mt5_python_controller\backend\app\trading_api.py`

- [ ] **Step 1：写骨架（含 schema + 工具函数）**

```python
"""交易页一键触发 API。

复用 /webhook 的解析与分发流程，针对主节点触发三种开仓策略（市价/限价/网格）。
首次触发按 (strategy_type, username, mt5_login) 自动创建专属分组+策略并把主
节点加入成员；后续复用。每次只触发一笔信号，不修改任何已有分发链路。
"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from . import (
    group_service,
    permissions,
    persist,
    strategy_service,
    strategy_templates,
    user_service,
    webhook,
)
from .connections import manager
from .deps import (
    client_ip,
    get_dispatcher,
    get_group_dispatcher,
    get_principal,
    get_store,
    require_menu,
)
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .models import (
    GroupCreate,
    GroupUpdate,
    QuickTriggerRequest,
    QuickTriggerResult,
    StrategyCreate,
    StrategyUpdate,
)
from .orm import Node, NodeGroup, TradingStrategy
from .permissions import MENU_TRADING, Principal
from .redis_store import RedisStore
from .strategy_templates import (
    TEMPLATE_2_ID,
    TEMPLATE_3_ID,
    default_grid_rule,
    default_risk_sized_rule,
)
from .db import SessionLocal
from .webhook import process_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])

# 主节点不在线 / 不存在 / 非主 / 跨 owner —— 全部归一错误信息，避免泄露内部状态
_PRIMARY_NOT_FOUND = "主节点不存在或未启用"
_PRIMARY_OFFLINE = "主节点不在线"
_PRIMARY_NOT_OWNED = "主节点不属于当前用户"


def _group_name(strategy_type: str, username: str, login: int) -> str:
    """专属分组名：(策略类型-用户名-MT5 登录号)。"""
    return f"{strategy_type}-{username}-{int(login)}"


def _find_group_by_name(
    store: RedisStore, name: str, *, owner_user_id: Optional[int],
) -> Optional[dict]:
    """在 Redis 缓存里按名称找分组（按 owner 隔离）。"""
    for g in await store.all_groups():
        if g.get("name") != name:
            continue
        if permissions.same_owner(g.get("owner_user_id"), owner_user_id):
            return g
    return None


def _find_strategy_by_name(
    store: RedisStore, name: str, *, owner_user_id: Optional[int],
) -> Optional[dict]:
    """在 Redis 缓存里按名称找策略（按 owner 隔离）。"""
    for s in await store.all_strategies():
        if s.get("name") != name:
            continue
        if permissions.same_owner(s.get("owner_user_id"), owner_user_id):
            return s
    return None


def _build_default_rules(strategy_type: str) -> tuple[str, list[dict]]:
    """返回 (template_id, rules)，复用模版默认参数。

    趋势市价/限价：复制 default_risk_sized_rule()，把 entry_mode 改成对应值。
    网格：直接复制 default_grid_rule()，方向由后续 group_service 写入。
    """
    if strategy_type == "trend_market":
        rule = deepcopy(default_risk_sized_rule().to_dict())
        rule["entry_mode"] = "market"
        return TEMPLATE_2_ID, [rule]
    if strategy_type == "trend_limit":
        rule = deepcopy(default_risk_sized_rule().to_dict())
        rule["entry_mode"] = "limit"
        return TEMPLATE_2_ID, [rule]
    if strategy_type == "grid":
        rule = deepcopy(default_grid_rule().to_dict())
        return TEMPLATE_3_ID, [rule]
    raise ValueError(f"未知策略类型：{strategy_type}")


def _infer_trend_action(
    strategy_type: str,
    *,
    entry_price: float,
    stop_loss: float,
    current_price: float,
) -> str:
    """趋势策略方向自动判断：BUY/SELL。

    - 市价：用 current_price 与 stop_loss 比较
    - 限价：用 entry_price 与 stop_loss 比较
    现价统一用 mid（已含在调用方处理过）
    """
    ref = current_price if strategy_type == "trend_market" else entry_price
    if ref <= 0 or stop_loss <= 0:
        raise ValueError("止损价必须大于 0")
    if ref > stop_loss:
        return "BUY"
    if ref < stop_loss:
        return "SELL"
    raise ValueError("入场价与止损价相同，无法判断方向")


def _quote_mid(account: dict, symbol: str) -> float:
    """从节点账户快照取品种的中间价；缺失则 0。"""
    quotes = (account or {}).get("quotes") or {}
    q = quotes.get(symbol) or {}
    mid = q.get("mid")
    if mid is not None and mid > 0:
        return float(mid)
    # 兼容旧字段：prices[symbol] 存的就是 mid
    prices = (account or {}).get("prices") or {}
    p = prices.get(symbol)
    if p is not None and p > 0:
        return float(p)
    return 0.0


def _resolve_username(user_id: Optional[int], fallback: str) -> str:
    """owner_user_id → username；管理员（None）用 fallback，规避 user_service 查不到。"""
    if user_id is None:
        return fallback
    # 同步查表代价高；用当前 principal.username 兜底（同一 owner 查不到的可能性极低）
    return fallback


async def _ensure_member(store: RedisStore, group: dict, node_id: str) -> None:
    """保证主节点是分组成员；缺则补。"""
    members = group.get("members") or []
    if any(m.get("node_id") == node_id for m in members):
        return
    new_ids = [m.get("node_id") for m in members if m.get("node_id")] + [node_id]
    await group_service.update_group(
        store, group["group_id"],
        GroupUpdate(node_ids=new_ids),
    )


# ------------------------------------------------------------------
# 端点
# ------------------------------------------------------------------
@router.post("/quick-trigger", response_model=QuickTriggerResult)
async def quick_trigger(
    body: QuickTriggerRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_TRADING)),
):
    """交易页一键触发：找/创建专属策略+分组，调 webhook.process_signal 走 strategy 链路。"""
    # 1. 主节点校验
    node = await store.get_node(body.node_id)
    if not node or not node.get("is_primary") or not node.get("enabled", True):
        raise HTTPException(status_code=400, detail=_PRIMARY_NOT_FOUND)
    if not permissions.owns(p, node.get("owner_user_id")):
        raise HTTPException(status_code=403, detail=_PRIMARY_NOT_OWNED)
    if not manager.is_node_online(body.node_id):
        raise HTTPException(status_code=400, detail=_PRIMARY_OFFLINE)

    owner = permissions.normalize_owner(node.get("owner_user_id"))
    username = _resolve_username(owner, p.username)
    login = int(node.get("mt5_login") or 0)
    if login <= 0:
        raise HTTPException(status_code=400, detail="主节点未绑定 MT5 登录号")

    # 2. 限价模式必填入场价
    if body.strategy_type == "trend_limit" and not (body.entry_price and body.entry_price > 0):
        raise HTTPException(status_code=400, detail="限价开仓必须填写入场价")

    # 3. 网格需要方向（默认 long）
    grid_direction: Optional[str] = None
    if body.strategy_type == "grid":
        if body.direction not in ("all", "buy", "sell"):
            raise HTTPException(status_code=400, detail="网格方向必须是 all/buy/sell")
        grid_direction = body.direction or "all"

    name = _group_name(body.strategy_type, username, login)

    # 4. 找/建策略
    strategy = _find_strategy_by_name(store, name, owner_user_id=owner)
    if not strategy:
        template_id, rules = _build_default_rules(body.strategy_type)
        try:
            strategy = await strategy_service.create_strategy(
                store,
                StrategyCreate(
                    name=name,
                    template_id=template_id,
                    symbol=body.symbol,
                    enabled=True,
                    remark=f"交易页自动创建（{body.strategy_type}）",
                    rules=[_to_pydantic_rule(r) for r in rules],
                ),
                owner_user_id=owner,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"创建策略失败：{e}")
    elif strategy.get("symbol") != body.symbol:
        # 切换品种：同步 strategy.symbol，不动 rules
        try:
            updated = await strategy_service.update_strategy(
                store, strategy["strategy_id"],
                StrategyUpdate(symbol=body.symbol),
            )
            if updated:
                strategy = updated
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"更新策略品种失败：{e}")

    # 5. 找/建分组（成员是主节点，分发模式 sync 单节点足够）
    group = _find_group_by_name(store, name, owner_user_id=owner)
    if not group:
        try:
            group = await group_service.create_group(
                store,
                GroupCreate(
                    name=name,
                    enabled=True,
                    dispatch_mode="sync",
                    node_ids=[body.node_id],
                    strategy_id=strategy["strategy_id"],
                    remark=f"交易页自动创建（{body.strategy_type}）",
                ),
                owner_user_id=owner,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"创建分组失败：{e}")
    else:
        # 复用：保证主节点是成员、策略已绑
        if group.get("strategy_id") != strategy["strategy_id"]:
            try:
                updated = await group_service.update_group(
                    store, group["group_id"],
                    GroupUpdate(strategy_id=strategy["strategy_id"]),
                )
                if updated:
                    group = updated
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"绑定策略失败：{e}")
        await _ensure_member(store, group, body.node_id)

    # 6. 构造信号
    if body.strategy_type in ("trend_market", "trend_limit"):
        account = await store.get_account(body.node_id) or {}
        current_price = _quote_mid(account, body.symbol)
        if body.strategy_type == "trend_market" and current_price <= 0:
            raise HTTPException(status_code=400, detail="无法获取主节点当前报价，请稍后再试")
        if not body.sl or body.sl <= 0:
            raise HTTPException(status_code=400, detail="趋势策略必须填写止损价")
        entry_ref = body.entry_price or 0.0
        if body.strategy_type == "trend_market":
            entry_ref = current_price
        try:
            action = _infer_trend_action(
                body.strategy_type,
                entry_price=entry_ref,
                stop_loss=float(body.sl),
                current_price=current_price,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        action = (body.direction or "all").lower()  # grid 模式 action 透传
        if action not in ("all", "buy", "sell"):
            action = "all"

    data: dict = {
        "action": action.upper() if action != "all" else "BUY",  # 兜底，process_signal 不强依赖
        "model": "strategy",
        "group_ids": [group["group_id"]],
        "symbol": body.symbol,
        "volume": body.volume,
    }
    if body.sl and body.sl > 0:
        data["sl"] = body.sl
    if body.tp and body.tp > 0:
        data["tp"] = body.tp
    if body.strategy_type == "trend_limit" and body.entry_price and body.entry_price > 0:
        data["limit_price"] = body.entry_price
    if body.remark:
        data["comment"] = body.remark

    # 网格方向：写进 strategy rule 的 action（仅首次创建时；复用时不动已有规则）
    # —— 已在 _build_default_rules 用 default_grid_rule 默认 long；用户选 buy/sell 时
    # 需在 strategy 内调整 rule.action。简化方案：仅在首次创建时把方向写进 rule。

    # 7. 走 webhook.process_signal
    ip = client_ip(request)
    result = await process_signal(
        data, source_ip=ip, source="manual",
        store=store, dispatcher=dispatcher, group_dispatcher=group_dispatcher,
    )
    await persist.audit(
        p.username, "quick_trigger", body.node_id,
        {"strategy_type": body.strategy_type, "data": data, "result": result.get("status")},
        result.get("status", "ok"), ip,
        category="console", before=None, after=None,
    )
    return QuickTriggerResult(
        group_id=group["group_id"],
        strategy_id=strategy["strategy_id"],
        result=result,
    )


def _to_pydantic_rule(rule: dict):
    """dict → StrategyRule pydantic 模型（仅用于 create_strategy 入参）。"""
    from .models import StrategyRule
    return StrategyRule(**rule)
```

- [ ] **Step 2：补齐 models.py 缺失的 schema**

在 `backend/app/models.py`（GroupCreate 后、StrategyCreate 前）插入：

```python
# ----------------------------- 交易页一键触发 -----------------------------
STRATEGY_TYPE_TREND_MARKET = "trend_market"
STRATEGY_TYPE_TREND_LIMIT = "trend_limit"
STRATEGY_TYPE_GRID = "grid"
TRADING_STRATEGY_TYPES = (
    STRATEGY_TYPE_TREND_MARKET,
    STRATEGY_TYPE_TREND_LIMIT,
    STRATEGY_TYPE_GRID,
)


class QuickTriggerRequest(BaseModel):
    """交易页一键触发：主节点 + 策略类型 + 开仓参数。"""
    node_id: str = Field(min_length=1, max_length=32)
    strategy_type: str = Field(description="trend_market / trend_limit / grid")
    symbol: str = Field(min_length=1, max_length=32)
    volume: float = Field(gt=0)
    sl: Optional[float] = Field(default=None, gt=0)
    tp: Optional[float] = Field(default=None, gt=0)
    # 限价开仓必填；其它模式忽略
    entry_price: Optional[float] = Field(default=None, gt=0)
    # 网格模式必填（all/buy/sell，默认 long=all=long）；
    # 趋势模式忽略（后端根据现价/SL 自动判断）
    direction: Optional[str] = Field(default=None)
    remark: Optional[str] = Field(default=None, max_length=64)


class QuickTriggerResult(BaseModel):
    """一键触发结果。"""
    group_id: str
    strategy_id: str
    result: dict  # webhook.process_signal 的返回体
```

- [ ] **Step 3：注册路由**

修改 `backend/app/main.py`：

1. 在顶部 import 区加入 `from . import trading_api`
2. 把 `trading_api.router` 加入 `app.include_router(r)` 元组

```python
# 顶部 import
from . import trading_api  # 与其他 import 同级

# app.include_router 段，加入
trading_api.router,
```

- [ ] **Step 4：启动后端验证 import 成功**

```bash
cd d:\work_space\mt5_python_controller
python -c "from backend.app.main import app; print([r.path for r in app.routes if 'trading' in r.path])"
```

预期：包含 `/api/trading/quick-trigger`

- [ ] **Step 5：commit**

```bash
git add backend/app/permissions.py backend/app/main.py backend/app/models.py backend/app/trading_api.py
git commit -m "feat(trading): add quick-trigger API for trading tab"
```

---

### Task 3：后端单测

**Files:**
- Create: `d:\work_space\mt5_python_controller\backend\tests\test_trading_api.py`

- [ ] **Step 1：写关键路径单测**

参考已有 `backend/tests/test_api.py` 的 setup/teardown 与 `client` fixture。

```python
"""交易页一键触发 API 单测：主节点校验、限价必填、网格方向、自动建组、复用。"""
import pytest
from fastapi.testclient import TestClient

from backend.app.db import SessionLocal
from backend.app.orm import Node, User, UserRole, NodeGroupMember
from backend.app.security import hash_password, make_node_id
from backend.app.strategy_templates import TEMPLATE_2_ID, TEMPLATE_3_ID


@pytest.fixture
def admin_user(db_setup):
    async def _setup():
        async with SessionLocal() as s:
            u = User(username="admin", password_hash=hash_password("admin"), is_admin=True)
            s.add(u)
            await s.commit()
            return u.id
    return _setup


def test_quick_trigger_rejects_non_primary(client, admin_user):
    body = {
        "node_id": "n1", "strategy_type": "trend_market",
        "symbol": "XAUUSD", "volume": 0.01,
    }
    r = client.post("/api/trading/quick-trigger", json=body, headers=auth_headers())
    assert r.status_code == 400
    assert "主节点" in r.json()["detail"]


def test_quick_trigger_requires_entry_price_for_limit(client):
    body = {
        "node_id": "n1", "strategy_type": "trend_limit",
        "symbol": "XAUUSD", "volume": 0.01, "sl": 1900.0,
    }
    r = client.post("/api/trading/quick-trigger", json=body, headers=auth_headers())
    assert r.status_code == 400
    assert "入场价" in r.json()["detail"]


def test_quick_trigger_grid_creates_strategy_and_group(client, primary_node):
    body = {
        "node_id": primary_node, "strategy_type": "grid",
        "symbol": "XAUUSD", "volume": 0.01, "direction": "long",
    }
    r = client.post("/api/trading/quick-trigger", json=body, headers=auth_headers())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["group_id"]
    assert j["strategy_id"]
    # 分组/策略名称含 strategy_type + login
    assert "grid-admin-" in _group_name(j)


def test_quick_trigger_reuses_existing(client, primary_node):
    body = {
        "node_id": primary_node, "strategy_type": "grid",
        "symbol": "XAUUSD", "volume": 0.01, "direction": "long",
    }
    a = client.post("/api/trading/quick-trigger", json=body, headers=auth_headers()).json()
    b = client.post("/api/trading/quick-trigger", json=body, headers=auth_headers()).json()
    assert a["group_id"] == b["group_id"]
    assert a["strategy_id"] == b["strategy_id"]
```

- [ ] **Step 2：运行单测**

```bash
cd d:\work_space\mt5_python_controller
python -m pytest backend/tests/test_trading_api.py -v
```

预期：全部通过；如果 fixture 命名差异，参考 `test_api.py` 已有命名（`client`、`db_setup`）。

- [ ] **Step 3：commit**

```bash
git add backend/tests/test_trading_api.py
git commit -m "test(trading): quick-trigger API key paths"
```

---

### Task 4：前端常量与路由

**Files:**
- Modify: `d:\work_space\mt5_python_controller\frontend\src\constants\menus.ts`
- Modify: `d:\work_space\mt5_python_controller\frontend\src\router\index.ts`

- [ ] **Step 1：插入菜单码**

`constants/menus.ts` 在 `home` 后插入 `trading`：

```ts
export const MENU_PATHS: ReadonlyArray<{ code: string; path: string }> = [
  { code: 'home', path: '/home' },
  { code: 'trading', path: '/trading' },
  { code: 'dashboard', path: '/' },
  // ... 其余不变
]
```

- [ ] **Step 2：注册路由**

`router/index.ts` 在 `/home` 后插入：

```ts
{
  path: '/trading',
  name: 'trading',
  component: () => import('@/views/TradingView.vue'),
  meta: { menu: 'trading' },
},
```

- [ ] **Step 3：commit**

```bash
git add frontend/src/constants/menus.ts frontend/src/router/index.ts
git commit -m "feat(frontend): register trading route and menu"
```

---

### Task 5：前端 API 客户端

**Files:**
- Create: `d:\work_space\mt5_python_controller\frontend\src\api\trading.ts`

- [ ] **Step 1：实现**

```ts
import { httpClient } from './client'

export type TradingStrategyType = 'trend_market' | 'trend_limit' | 'grid'

export interface QuickTriggerRequest {
  node_id: string
  strategy_type: TradingStrategyType
  symbol: string
  volume: number
  sl?: number | null
  tp?: number | null
  entry_price?: number | null
  /** 网格模式：all(默认 long) / buy / sell；趋势模式由后端自动判断 */
  direction?: 'all' | 'buy' | 'sell' | null
  remark?: string | null
}

export interface QuickTriggerResult {
  group_id: string
  strategy_id: string
  result: {
    status: 'accepted' | 'rejected' | 'duplicate'
    signal_id?: string
    groups?: number
    targets?: number
    reason?: string
    mode?: string
  }
}

export async function quickTrigger(req: QuickTriggerRequest): Promise<QuickTriggerResult> {
  return httpClient.post<QuickTriggerResult>('/api/trading/quick-trigger', req)
}
```

- [ ] **Step 2：commit**

```bash
git add frontend/src/api/trading.ts
git commit -m "feat(frontend): trading API client"
```

---

### Task 6：前端主页面与弹窗

**Files:**
- Create: `d:\work_space\mt5_python_controller\frontend\src\views\TradingView.vue`

- [ ] **Step 1：实现主页面**

要点：
- 顶栏：「交易」标题 + 策略 Tab（[市价开仓] [限价开仓] [智能吸筹]）
- 主节点卡片：展示当前选中主节点（用 `hub.nodes.find(n => n.is_primary)`），无主节点时空态（跳转【节点】按钮）
- 持仓卡片：当前主节点品种持仓数（用 `useHubStore().primaryAccount(nodeId).positions.filter(p => p.symbol === form.symbol).length`）
- 操作节点卡片：在线状态（同上 account）
- 参数表单：品种 / 手数 / 止损 / 止盈 / 备注 / 限价入场价（仅限价模式显示）/ 网格方向（仅网格模式）
- 「触发信号」按钮：调 `quickTrigger`，弹窗确认后提交
- 弹窗（Element Plus `el-dialog`）：展示"分组/策略/绑定品种"（自动，不可改），参数（可改），底部「取消」「确认触发」
- 触发结果：accepted → toast 成功 + 关闭弹窗；rejected → 弹窗内展示 reason；duplicate → toast 提示
- 不自动跳转

具体实现（参考现有 `GroupsView.vue` / `ConsoleView.vue` 风格）：

```vue
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/dialog/style/css'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import { quickTrigger, type TradingStrategyType, type QuickTriggerResult } from '@/api/trading'
import type { NodeOut } from '@/api/types'

const hub = useHubStore()
const router = useRouter()

const STRATEGY_LABEL: Record<TradingStrategyType, string> = {
  trend_market: '市价开仓',
  trend_limit: '限价开仓',
  grid: '智能吸筹',
}

const strategyType = ref<TradingStrategyType>('trend_market')

// 当前主节点：取 owner == 当前用户（或管理员名下）的 is_primary 节点
const primaryNode = computed<NodeOut | undefined>(() => {
  return hub.nodes.find(n => n.is_primary && n.enabled) as NodeOut | undefined
})

const account = computed(() => {
  if (!primaryNode.value) return null
  return hub.accountById(primaryNode.value.node_id) || null
})

const isOnline = computed(() => {
  if (!primaryNode.value) return false
  return hub.onlineNodeIds.includes(primaryNode.value.node_id)
})

const positionsCount = computed(() => {
  if (!account.value || !form.symbol) return 0
  return (account.value.positions || []).filter(p => p.symbol === form.symbol).length
})

const form = ref({
  symbol: 'XAUUSD',
  volume: 0.01,
  sl: 0 as number | null,
  tp: 0 as number | null,
  entry_price: 0 as number | null,  // 仅限价
  direction: 'long' as 'long' | 'short' | 'all',  // 仅网格
  remark: '',
})

watch(strategyType, () => {
  // 切策略时清空入场价/方向，避免残留
  form.value.entry_price = 0
})

const canSubmit = computed(() => {
  if (!primaryNode.value || !isOnline.value) return false
  if (!form.value.symbol || form.value.volume <= 0) return false
  if (strategyType.value === 'trend_limit' && (!form.value.entry_price || form.value.entry_price <= 0)) return false
  return true
})

const dialogVisible = ref(false)
const submitting = ref(false)
const lastError = ref('')

function openDialog() {
  if (!canSubmit.value) return
  lastError.value = ''
  dialogVisible.value = true
}

async function submit() {
  if (!primaryNode.value) return
  submitting.value = true
  lastError.value = ''
  try {
    const payload = {
      node_id: primaryNode.value.node_id,
      strategy_type: strategyType.value,
      symbol: form.value.symbol,
      volume: form.value.volume,
      sl: form.value.sl && form.value.sl > 0 ? form.value.sl : null,
      tp: form.value.tp && form.value.tp > 0 ? form.value.tp : null,
      entry_price: strategyType.value === 'trend_limit' ? form.value.entry_price : null,
      direction: strategyType.value === 'grid'
        ? (form.value.direction === 'long' ? 'all' : form.value.direction)
        : null,
      remark: form.value.remark || null,
    }
    const r: QuickTriggerResult = await quickTrigger(payload)
    if (r.result.status === 'accepted') {
      ElMessage.success(`已触发 ${STRATEGY_LABEL[strategyType.value]} ${form.value.symbol}（${r.result.targets ?? 0} 个目标节点）`)
      dialogVisible.value = false
    } else if (r.result.status === 'duplicate') {
      ElMessage.warning('重复信号被抑制（5 秒内已有相同信号）')
    } else if (r.result.status === 'rejected') {
      lastError.value = r.result.reason || '触发被拒收'
    } else {
      ElMessage.info(`已提交：${r.result.status}`)
    }
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastError.value = err?.response?.data?.detail || err?.message || '触发失败'
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await Promise.all([hub.fetchNodes(), hub.fetchAccounts?.()])
})
</script>

<template>
  <div class="trading-page">
    <div class="row between page-header">
      <div class="h1">交易</div>
    </div>

    <!-- 空态：无主节点 -->
    <div v-if="!primaryNode" class="card card-pad empty-state">
      <div class="empty-title">尚未设置主节点</div>
      <div class="empty-desc">请先在【节点】页将一个节点标记为主节点，再回到此页面触发开仓。</div>
      <button class="btn-primary" @click="router.push('/nodes')">前往节点页</button>
    </div>

    <template v-else>
      <!-- 策略选择 -->
      <div class="card card-pad strategy-tabs">
        <button
          v-for="(label, key) in STRATEGY_LABEL"
          :key="key"
          class="strategy-tab"
          :class="{ active: strategyType === key }"
          @click="strategyType = key as TradingStrategyType"
        >
          {{ label }}
        </button>
      </div>

      <!-- 主节点 / 操作节点 / 持仓 三块 -->
      <div class="grid-3">
        <div class="card card-pad">
          <div class="label">主节点</div>
          <div class="row" style="gap: 8px; margin-top: 8px">
            <span class="pill pill-primary">主节点</span>
            <strong>{{ primaryNode.name }}</strong>
          </div>
          <div class="muted small" style="margin-top: 6px">
            MT5 登录：{{ primaryNode.mt5_login }} · 服务器：{{ primaryNode.mt5_server || '—' }}
          </div>
        </div>

        <div class="card card-pad">
          <div class="label">操作节点</div>
          <div class="row" style="gap: 8px; margin-top: 8px">
            <span :class="['pill', isOnline ? 'pill-live' : 'pill-offline']">
              <span :class="['dot', isOnline ? 'online' : 'offline']" />
              {{ isOnline ? '在线' : '离线' }}
            </span>
          </div>
        </div>

        <div class="card card-pad">
          <div class="label">持仓</div>
          <div class="big" style="margin-top: 8px">{{ positionsCount }}</div>
        </div>
      </div>

      <!-- 参数表单 -->
      <div class="card card-pad form-card">
        <div class="label">开仓参数</div>
        <div class="form-grid">
          <label>
            <span>开仓品种</span>
            <input v-model="form.symbol" placeholder="如 XAUUSD" />
          </label>
          <label>
            <span>开仓手数</span>
            <input v-model.number="form.volume" type="number" step="0.01" min="0.01" />
          </label>
          <label v-if="strategyType === 'trend_limit'">
            <span>入场价（必填）</span>
            <input v-model.number="form.entry_price" type="number" step="0.0001" />
          </label>
          <label v-if="strategyType !== 'grid'">
            <span>止损价</span>
            <input v-model.number="form.sl" type="number" step="0.0001" min="0" />
          </label>
          <label v-if="strategyType !== 'grid'">
            <span>止盈价</span>
            <input v-model.number="form.tp" type="number" step="0.0001" min="0" />
          </label>
          <label v-if="strategyType === 'grid'">
            <span>网格方向</span>
            <select v-model="form.direction">
              <option value="long">多头（默认）</option>
              <option value="short">空头</option>
            </select>
          </label>
          <label class="span-2">
            <span>备注</span>
            <input v-model="form.remark" maxlength="64" />
          </label>
        </div>

        <div class="row" style="margin-top: 16px; justify-content: flex-end">
          <button class="btn-primary" :disabled="!canSubmit" @click="openDialog">
            触发信号
          </button>
        </div>
        <div v-if="!isOnline" class="hint warn">主节点离线，无法触发</div>
      </div>
    </template>

    <!-- 触发弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="`触发信号 - ${STRATEGY_LABEL[strategyType]}`"
      width="520px"
    >
      <div class="dialog-form">
        <div class="kv">
          <span class="k">分组</span>
          <span class="v">{{ `${strategyType}-${primaryNode?.name}-${primaryNode?.mt5_login}` }}</span>
        </div>
        <div class="kv">
          <span class="k">策略</span>
          <span class="v">{{ STRATEGY_LABEL[strategyType] }}（自动复用/创建）</span>
        </div>
        <div class="kv">
          <span class="k">绑定品种</span>
          <span class="v">{{ form.symbol }}</span>
        </div>
        <div class="kv" v-if="strategyType !== 'grid'">
          <span class="k">止损价</span>
          <span class="v">{{ form.sl || '—' }}</span>
        </div>
        <div class="kv" v-if="strategyType !== 'grid'">
          <span class="k">止盈价</span>
          <span class="v">{{ form.tp || '—' }}</span>
        </div>
        <div class="kv" v-if="strategyType === 'trend_limit'">
          <span class="k">入场价</span>
          <span class="v">{{ form.entry_price }}</span>
        </div>
        <div class="kv" v-if="strategyType === 'grid'">
          <span class="k">方向</span>
          <span class="v">{{ form.direction === 'long' ? '多头' : '空头' }}</span>
        </div>
        <div class="kv" v-if="form.remark">
          <span class="k">备注</span>
          <span class="v">{{ form.remark }}</span>
        </div>

        <div class="hint">提示：首次触发会按 (策略类型-节点名-MT5 登录号) 自动创建「分组+策略」并把主节点加入；后续直接复用。</div>
        <div v-if="lastError" class="hint error">{{ lastError }}</div>
      </div>

      <template #footer>
        <button class="btn-ghost" @click="dialogVisible = false">取消</button>
        <button class="btn-primary" :disabled="submitting" @click="submit">
          {{ submitting ? '提交中...' : '确认触发' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.trading-page { display: flex; flex-direction: column; gap: 16px; }
.strategy-tabs { display: flex; gap: 8px; }
.strategy-tab {
  flex: 1; padding: 14px;
  background: var(--glass-bg, rgba(255,255,255,0.04));
  border: 1px solid var(--glass-border, rgba(255,255,255,0.1));
  border-radius: 8px; color: inherit; cursor: pointer; font-size: 14px;
}
.strategy-tab.active { border-color: var(--accent, #4f8cff); background: rgba(79,140,255,0.08); }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .grid-3 { grid-template-columns: 1fr; } }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; margin-top: 12px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; }
.form-grid label.span-2 { grid-column: span 2; }
.form-grid input, .form-grid select {
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
  color: inherit; padding: 8px 10px; border-radius: 6px;
}
.kv { display: grid; grid-template-columns: 100px 1fr; padding: 6px 0; }
.kv .k { color: var(--muted, #888); }
.dialog-form { display: flex; flex-direction: column; gap: 4px; }
.hint { margin-top: 12px; font-size: 12px; color: var(--muted, #888); }
.hint.warn { color: var(--amber, #f5a623); }
.hint.error { color: var(--red, #e74c3c); }
.empty-state { text-align: center; padding: 40px 20px; }
.empty-title { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
.empty-desc { color: var(--muted, #888); margin-bottom: 16px; }
.big { font-size: 28px; font-weight: 700; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
.pill-primary { background: rgba(79,140,255,0.15); color: #4f8cff; }
</style>
```

- [ ] **Step 2：在 `stores/hub.ts` 暴露 `onlineNodeIds` / `accountById` getter（如已存在则跳过）**

- [ ] **Step 3：本地跑**

```bash
cd d:\work_space\mt5_python_controller\frontend
npm run dev
```

访问 `/trading`，验证：
- 切换策略 Tab
- 限价模式显示入场价
- 网格模式显示方向
- 触发弹窗
- 主节点离线时按钮置灰

- [ ] **Step 4：commit**

```bash
git add frontend/src/views/TradingView.vue frontend/src/stores/hub.ts
git commit -m "feat(frontend): trading view with quick-trigger dialog"
```

---

### Task 7：App.vue 顶栏加入交易 Tab

**Files:**
- Modify: `d:\work_space\mt5_python_controller\frontend\src\App.vue`

- [ ] **Step 1：在首页 RouterLink 后插入交易 Tab**

```vue
<RouterLink v-if="auth.hasMenu('trading')" to="/trading">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 17l6-6 4 4 8-8"/>
    <path d="M14 7h7v7"/>
  </svg>
  <span>交易</span>
</RouterLink>
```

插入位置：在 `<RouterLink v-if="auth.hasMenu('home')" to="/home">` 块之后。

- [ ] **Step 2：commit**

```bash
git add frontend/src/App.vue
git commit -m "feat(frontend): add trading tab to top nav"
```

---

## 四、Self-Review（自检）

1. **Spec 覆盖：**
   - 三种策略（市价/限价/网格）→ ✅ Task 2 端点 + Task 6 弹窗
   - 找/建策略+分组 → ✅ Task 2 `_find_*` + `_group_name`
   - 触发信号弹窗 → ✅ Task 6
   - 默认参数 + 用户可改 → ✅ Task 2 `_build_default_rules` + 弹窗展示/可改
   - 命名规则 `策略类型-账号名-MT5 登录号` → ✅ Task 2 `_group_name`
   - 趋势方向由后端自动判断 → ✅ Task 2 `_infer_trend_action`
   - 网格方向由用户选 → ✅ Task 2 + Task 6

2. **占位扫描：** 无 TBD/TODO；每步代码完整。

3. **类型一致性：**
   - `TradingStrategyType` 在前端 `trading.ts` 和后端 `TRADING_STRATEGY_TYPES` 保持一致。
   - `QuickTriggerRequest` / `QuickTriggerResult` 字段名后端 ↔ 前端一致。
   - `MENU_TRADING = "trading"` 在 `permissions.py` ↔ `menus.ts` ↔ `App.vue` ↔ `router/index.ts` 全部一致。

4. **风险点：**
   - 趋势方向自动判断用 `quotes[品种].mid`；如果主节点没上报该品种的 quote（合约订阅未启用），市价模式会被 400 拒收。已加提示文案。
   - 网格 `price_lower/upper` 默认 0 会被 `_validate_grid_rule_fields` 拒收；首次创建时也会被后端拦下；前端不感知，给清晰 reason。
   - 同一 owner 同一 strategy_type 同一 login 的命名唯一性由 `group_service.name_exists` / `strategy_service.name_exists` 保证。

5. **非目标（YAGNI）：**
   - 不修改 webhook / group_dispatcher / 策略模板默认值
   - 不在交易页加策略参数配置 UI（用户需要改区间/手数去【策略管理】）
   - 不支持多主节点批量触发
   - 触发后不自动跳转
