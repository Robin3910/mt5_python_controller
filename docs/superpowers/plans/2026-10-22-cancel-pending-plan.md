# 【主节点】卡片「删挂单」功能实施方案

> **一句话目标**：在**首页主节点卡片**上新增一个 `删挂单` 按钮，一次点击把该主节点上
> **剩余的全部挂单**清干净，**但绝不动已有持仓** —— 补上现有「平掉该节点全部」缺失的另一半能力。
>
> **关键顺序**：**先停策略 → 等策略收口 → 撤残余挂单 → 复扫校验**。
> 顺序颠倒会导致策略 runner 把刚撤掉的单补回来（详见 §2.2），这是整个方案的技术核心。

---

## 已确认的四条前置决策

| # | 决策 | 含义 |
|---|---|---|
| 1 | **默认撤光全部挂单** | 不做 `manual` / `strategy` 范围分层，一次清空（协议层仍预留 `scope` 字段） |
| 2 | **仅主节点卡片显示** | `n.is_primary === true` 才渲染按钮；无主节点时首页无此入口 |
| 3 | **自动停策略** | 撤策略挂单前自动停掉该节点全部在跑子任务，与现有 `close` 的 `target:'all'` 语义一致 |
| 4 | **不动持仓** | 与「平掉该节点全部」严格区分：那是"平仓 + 附带撤单"，本功能是"撤单 + 停策略" |

### 与现有能力的对称关系

| 动作 | 停策略 | 平持仓 | 撤挂单 | 入口 |
|---|---|---|---|---|
| `平掉该节点全部`（**现有**） | 是 | **是** | 是（附带） | 首页卡片底部 |
| `删挂单`（**本方案**） | 是 | **否** | 是（主体） | 首页主节点卡片底部 |

两者都能把账户清干净，区别是本功能**保留浮盈浮亏的持仓**不动。

---

## 0. 本次核实修正的两处结论

> 这两处与直觉不符，**已逐条回到代码验证**，此处为修正后的最终口径。

| # | 直觉结论 | 修正后 | 证据 |
|---|---|---|---|
| **M1** | 「系统里没有清挂单的入口」 | **有一个，但它强制连持仓一起平**。`mt5_client.close_all()` 第一步就是 `self._cancel_many(self.pending_orders())`，撤挂单只是「平仓」的附带动作，无法单独触发 | `node_client/mt5_client.py:1283-1305` |
| **M2** | 「前端拿不到挂单数据」 | **完全拿得到**。节点快照 `orders` 字段 → `ws_gateway._save_account` 存 Redis → `hub.applyWs` 整份塞进 `accounts[node_id]`。缺的只是前端 TS 类型声明 | `ws_gateway.py:305,325`、`hub.ts:476` |

**推论**：本方案**不需要任何新增数据采集链路**，纯粹是在既有数据上加一条命令 + 一个路由 + 一个按钮。
工作量的大头在**编排时序**（§3）和**与限价监听的互斥**（§6），不在取数。

---

## 1. 现状核对（已核实的代码事实）

### 1.1 现有平仓链路的四跳

```
HomeView.closeNodeAll(n)                          前端卡片底部按钮
  └─> hub.closeNode(node_id, {target:'all'})     frontend/src/stores/hub.ts:383
      └─> POST /api/nodes/{id}/close             backend/app/close.py:48
          ├─> _stop_strategies_on_flatten()       close.py:37 → group_dispatcher.stop_all_on_node
          │      └─> 节点: cmd="strategy_stop" → close_by_magic（撤该 magic 的挂单 + 平仓）
          └─> cmd="close" / close_target="all"   models.py:1242 build_close_command
                 └─> 节点: _do_close()           node_client/node_client.py:922
                      └─> mt5.close_all()        mt5_client.py:1283
                           ├─ _cancel_many(pending_orders())   ← 挂单在这里被撤
                           └─ _close_hedges_burst + _close_many ← 持仓在这里被平
```

`close_all()` 的第一步就是撤挂单，**所以「撤单」目前只是「平仓」的副产物**。
要单独撤单，必须新增一条不经过 `_close_hedges_burst` / `_close_many` 的独立命令。

### 1.2 节点侧已有的撤单能力（可直接复用）

| 能力 | 位置 | 可复用度 |
|---|---|---|
| 单张撤单 `cancel_order(ticket, max_retry=3)` | `mt5_client.py:723` | ✅ 含瞬时错误退避重试 |
| 批量撤单 `_cancel_many(orders)` | `mt5_client.py:854` | ✅ **正是我们要的语义**：先无退避连发，瞬时失败再带退避重试 |
| 读挂单 `pending_orders()` | `mt5_client.py:365` | ✅ 已过滤掉市价单（`orders_get` 里的瞬时态），只含真挂单 |
| 按 magic 筛选 `pending_orders_by_magic(magic)` | `mt5_client.py:398` | ✅ 为 `scope` 分层预留 |
| RPC 命令分发 `_do_cancel_pending` | `node_client.py:940` | ⚠️ 写死单张，需泛化 |

### 1.3 后端已有的 req_id 配对机制（`limit_watch` 私有）

```322:340:backend/app/limit_watch.py
async def cancel_pending_on_node(node_id: str, ticket: int) -> dict:
    """向节点下发撤挂单并等待回包；失败返回 success=False，不抛。"""
    req_id = uuid.uuid4().hex[:16]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending_cancels[req_id] = fut
    try:
        sent = await manager.send_to_node(node_id, build_cancel_command(req_id, ticket))
        if not sent:
            return {"success": False, "ticket": ticket, "error": "节点当前离线，无法撤销挂单"}
        try:
            payload = await asyncio.wait_for(fut, timeout=CANCEL_TIMEOUT)
```

`ws_gateway._session` 已有对应分发分支：

```286:288:backend/app/ws_gateway.py
        elif mtype == "cancel_pending_result":
            from . import limit_watch
            limit_watch.resolve_cancel(data.get("req_id"), data)
```

**问题**：这套 `req_id → Future` 机制被 `limit_watch` 私有持有。
新增批量撤单若再写一套，就是两份会各自漂移的重复代码 → **必须抽公共模块**（§4.4）。

### 1.4 停策略会顺带撤掉策略挂单

```515:542:backend/app/group_dispatcher.py
    async def stop_all_on_node(
        self, node_id: str, *, signal_id: str, reason: str = "manual_close_all",
    ) -> dict:
        """终止该节点上所有未收口的策略子任务（复用单任务 strategy_stop）。
        ...
        subs = await group_persist.active_subtasks_for_node(node_id)
        closing = 0
        forced = 0
        for sub in subs:
            if sub.get("status") in group_rules.SUBTASK_TERMINAL:
                continue
            outcome = await self._stop_subtask(sub, signal_id=signal_id, reason=reason)
            if outcome["status"] == "closing":
                closing += 1
            elif outcome["status"] == "forced":
                forced += 1
        ...
        return {
            "stopped": closing,
            "forced": forced,
            "total": closing + forced,
        }
```

节点侧收到 `strategy_stop` 后走 `_close_orphan_strategy` / `close_by_magic`
（`node_client.py:816-832`），**已带 `int(res.get("cancelled") or 0)` 的撤单计数**。

**推论**：停策略后，**策略自己的挂单已被撤掉一部分**。
但 runner 认知外的挂单（手工单、网格在途单、崩溃留下的孤儿单）**不会**被撤 → 必须补一次全量扫撤。

### 1.5 现有权限模型（直接照抄，无需新设计）

```48:60:backend/app/close.py
async def close_node(
    node_id: str,
    body: CloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_NODES, MENU_DASHBOARD)),
):
    """对单个节点下发平仓（全平 / 按品种 / 按订单）。
    ...
    """
    await owned_node_or_404(store, p, node_id)
```

`require_menu(MENU_NODES, MENU_DASHBOARD)` + `owned_node_or_404`
= **普通用户只能操作自己拥有的节点，管理员可操作任意节点**。本功能沿用。

### 1.6 首页按钮区的现有结构

```506:520:frontend/src/views/HomeView.vue
            <div class="dash-node-foot row between">
              <button
                v-if="acct(n.node_id)?.positions?.length"
                type="button"
                class="dash-link"
                @click="goNode(n.node_id)"
              >查看全部 {{ acct(n.node_id)!.positions.length }} 个持仓 →</button>
              <span v-else></span>
              <button
                type="button"
                class="btn-sm btn-ghost"
                :disabled="!acct(n.node_id)?.positions?.length"
                @click="closeNodeAll(n)"
              >平掉该节点全部</button>
            </div>
```

卡片底部是 `row between` 布局，左侧「查看全部持仓」+ 右侧单个按钮。
新增按钮后右侧变成按钮组，需调整为 `gap` 间距的两枚按钮。

### 1.7 现成的 Redis 锁模式（节点级互斥直接复用）

```162:167:backend/app/redis_store.py
    async def acquire_exec_lock(self, node_id: str, symbol: str, ttl_ms: int = 10000) -> bool:
        """获取 (节点, 品种) 执行锁；带 TTL，异常情况下可自动释放。"""
        return bool(
            await self.r.set(K_EXEC_LOCK.format(node_id, symbol), "1", nx=True, px=ttl_ms)
        )
```

`SET NX PX` 是本项目既有的锁实现方式，新增节点级锁沿用同一形状，不引入新范式。

---

## 2. 核心设计决策

### 2.1 语义定义：删挂单 = 停策略 + 全量撤单，不碰持仓

```
[0] 权限校验 → 节点在线检查
[1] 记录快照基线（撤单前的挂单清单）
[2] 停策略 → group_dispatcher.stop_all_on_node(reason="manual_cancel_pending")
[3] 等策略收口（轮询子任务数归零或超时）
[4] 全量撤单 → cmd="cancel_pending_all" → 节点扫 pending_orders() 逐张撤
[5] 复扫 → 若 remaining>0，等 2s 再撤一轮（最多 2 轮）
[6] 审计 + 返回汇总
```

### 2.2 为什么必须"先停策略再撤单"（唯一的硬技术点）

`strategy_runner` **自己维护挂单数量**，发现少了会立刻补：

```1268:1272:node_client/strategy_runner.py
        if res.get("pending"):
            self._pending_orders += 1
        ..._log(self.task_id, batch.index, "placed" if res.get("pending") else "filled", reason,
```

```1867:1891:node_client/strategy_runner.py
            reader = getattr(self._mt5, "pending_orders_by_magic", None)
            ...
            self._pending_orders = len(resting)
```

外加网格的 `_grid_pending_sells: set[int]`（`strategy_runner.py:208`）驱动重新挂格。

**若顺序颠倒，用户会看到**：

```
点「删挂单」→ 撤掉 12 张 → 3 秒后 runner 补回 12 张 → 以为功能坏了
```

因此**三段式顺序不可颠倒**，这是 §2.1 那张时序图的全部意义。

### 2.3 复扫的收敛判据：看**总数**，不看"是否还有策略单"

- `remaining == 0` → 收敛，报「已清空」
- `remaining` **稳定不变**（撤了又出来同样数量）→ 说明有外部在补单（如 `limit_watch` 监听重新分发），
  **停止复扫**，在结果里说明「撤单后系统又生成了 N 张挂单，可能是限价监听自动触发，请检查分组监听开关」

复扫最多 2 轮，避免无限循环。

### 2.4 为什么停策略后还要撤（不是冗余）

| 挂单来源 | 停策略会撤吗 |
|---|---|
| 策略托管挂单（runner 名下） | ✅ 会（`close_by_magic`） |
| 手工在 MT5 上挂的单（magic=0） | ❌ 不会 |
| 网格在途挂单 / 崩溃留下的孤儿单 | ❌ 不会（runner 已停，认知外） |
| `limit_watch` 刚生成的策略挂单 | ⚠️ 时序上可能落在停策略之后 |

所以 §2.1 的 `[4]` 是必需的，不是"以防万一"。

### 2.5 挂单数徽标：点之前就知道有没有东西可删

卡片按钮上显示 `删挂单 (12)`，数据源是**已有的账户快照 `accounts[node_id].orders`**，零额外请求。
这比"点了才知道"好得多——尤其在 `scope=all` 下会连带停策略，用户需要有预期。

---

## 3. 后端编排（核心）

### 3.1 路由入口

**新建 `backend/app/pending_api.py`**（不塞进 `close.py`——两者语义不同，混在一起会让 `close.py` 的审计口径变浑）：

```
POST /api/nodes/{node_id}/cancel-pending
Body: { "scope": "all" }        // 本期只开放 all；协议层预留 manual/strategy
```

依赖与权限**照抄 `close.py`**（§1.5），无需新设计：

```python
@router.post("/nodes/{node_id}/cancel-pending")
async def cancel_pending_on_node_route(
    node_id: str,
    body: CancelPendingRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_NODES, MENU_DASHBOARD)),
):
    await owned_node_or_404(store, p, node_id)
    ...
```

在 `main.py` 注册（与既有 20+ 个 router 并列，`main.py:12-44`）：

```python
from . import (..., pending_api, ...)
```

### 3.2 编排六步（伪代码骨架）

```python
async def orchestrate_cancel_pending(node_id, scope, principal, ip) -> dict:
    # [0] 在线检查：离线直接 409，与 close 一致
    if not await manager.send_to_node(node_id, {"cmd": "ping_probe"}):  # 实际用 manager 状态判断
        raise HTTPException(409, "node offline")

    # [并发保护] 节点级 Redis 锁，防多标签页/多用户重复点击
    if not await store.acquire_cancel_pending_lock(node_id, ttl_s=30):
        raise HTTPException(409, "该节点正在执行删挂单操作")

    try:
        # [1] 快照基线（只用于审计 before 字段，不参与判断）
        prev = await store.get_account(node_id) or {}
        before_orders = list(prev.get("orders") or [])

        # [2] 停策略 —— 关键第一步
        signal_id = _cmd_id()
        strategies = await group_dispatcher.stop_all_on_node(
            node_id, signal_id=signal_id, reason="manual_cancel_pending",
        )

        # [3] 等策略收口：轮询子任务数归零或超时（建议 3s）
        await _wait_subtasks_settled(node_id, timeout_s=3.0)

        # [4]+[5] 撤单 + 复扫（最多 2 轮）
        result = {"cancelled": 0, "failed": [], "remaining": 0, "rounds": 0}
        for _ in range(2):
            payload = await node_rpc.request(
                node_id,
                build_cancel_all_command(scope=scope),
                expect_type="cancel_pending_all_result",
                timeout=CANCEL_TIMEOUT,   # 8s，与 limit_watch 一致
            )
            result["rounds"] += 1
            result["cancelled"] += int(payload.get("cancelled") or 0)
            result["failed"].extend(payload.get("failed") or [])
            result["remaining"] = int(payload.get("remaining") or 0)
            if result["remaining"] == 0:
                break
            await asyncio.sleep(2)   # 给 limit_watch 的自动重分发留时间

        # [6] 审计
        await persist.audit(
            principal.username, "cancel_pending_all", node_id,
            {"scope": scope}, "ok" if not result["failed"] else "partial",
            ip, category="node",
            before={"orders": before_orders},
            after={**result, "strategies": strategies, "limit_watch_active": ...},
        )
        return {"status": "ok", "node_id": node_id,
                "strategies": strategies, **result}
    finally:
        await store.release_cancel_pending_lock(node_id)
```

### 3.3 返回结构（前端据此渲染结果弹窗）

```json
{
  "status": "ok",
  "node_id": "node_xxx",
  "strategies": { "stopped": 3, "forced": 0, "total": 3 },
  "cancelled": 12,
  "failed": [
    { "ticket": 123456, "symbol": "XAUUSD", "error": "订单已被拒绝：价格已变" }
  ],
  "remaining": 0,
  "rounds": 1,
  "limit_watch_active": true
}
```

**`failed` 非空时前端升级为 warning 并列出明细**——
撤单失败很常见（刚成交、价格变动、broker 拒绝），**绝对不能吞掉**。

`limit_watch_active` 来自 `limit_watch.node_has_watch(groups, node_id)`（`limit_watch.py:171`），
让确认弹窗能提前提醒用户"这个节点的挂单正被自动监听消费"。

---

## 4. 协议层与节点客户端

### 4.1 新命令

```json
{ "cmd": "cancel_pending_all", "req_id": "c1a2b3c4", "scope": "all" }
```

### 4.2 `mt5_client.py`：薄封装（**复用 `_cancel_many`，不要重写撤单逻辑**）

```python
def cancel_pending_all(self, scope: str = "all", *, magics: set[int] | None = None) -> dict:
    """扫全部未成交挂单并批量撤销（人工「删挂单」落点）。

    scope: all（不过滤）/ manual（只撤 magic=0）/ strategy（只撤 magics 名下）
    撤单语义直接复用 _cancel_many —— 它已经实现了「先无退避连发、
    瞬时失败再带退避重试」，重写会丢掉这层保护。
    """
    self.ensure()
    orders = self.pending_orders()          # 已过滤市价单，只含真挂单
    if scope == "manual":
        orders = [o for o in orders if int(o.get("magic") or 0) == 0]
    elif scope == "strategy" and magics is not None:
        orders = [o for o in orders if int(o.get("magic") or 0) in magics]

    results = self._cancel_many(orders)
    ok = all(r.get("success") for r in results) if results else True
    return {
        "success": ok,
        "action": "CANCEL_ALL",
        "cancelled": _cancelled_count(results),
        "failed": [r for r in results if not r.get("success")],
        "results": results,
        "remaining": len(self.pending_orders()),   # 撤完再读一次，供复扫判断
    }
```

**`remaining` 在撤完后重新读一次 `pending_orders()`** —— 这是 §2.3 复扫判据的数据来源，
必须由节点侧给出权威值，不能由服务端拿旧快照猜。

### 4.3 `node_client.py`：新增分支与回包

在 `_handle` 的命令分发里加（`node_client.py:532` 之后）：

```python
elif cmd == "cancel_pending_all":
    await self._do_cancel_pending_all(ws, msg)
```

回包格式与既有 `cancel_pending_result` 风格一致（便于日志/解析统一）：

```python
async def _do_cancel_pending_all(self, ws, msg: dict) -> None:
    req_id = str(msg.get("req_id") or "")
    scope = str(msg.get("scope") or "all")
    data: dict = {"req_id": req_id, "scope": scope, "success": False,
                  "cancelled": 0, "failed": [], "remaining": 0, "results": []}
    try:
        res = dict(await self._exec(self.mt5.cancel_pending_all, scope) or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("cancel_pending_all failed: %s", e)
        data["error"] = str(e)
    else:
        data.update(res)
    await ws.send(json.dumps({"type": "cancel_pending_all_result", "data": data}))
    logger.info("cancel_pending_all result: %s", data)
```

### 4.4 `backend/app/node_rpc.py`（**新建**）：抽公共 req_id 配对

把 `limit_watch.py` 里私有的 `_pending_cancels` + `CANCEL_TIMEOUT` 提出来，
让**单张撤单**（`limit_watch`）与**批量撤单**（本方案）共用同一套配对逻辑：

```python
_pending: dict[str, asyncio.Future] = {}
DEFAULT_TIMEOUT = 8.0

def resolve(req_id: object, payload: dict) -> None:
    fut = _pending.pop(str(req_id or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(payload or {})

def discard(req_id: str) -> None:
    _pending.pop(req_id, None)

def reset_waiters() -> None:      # 测试用
    _pending.clear()

async def request(node_id: str, cmd: dict, *, expect_type: str,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """向节点下发命令并等对应回包；失败返回 success=False，**不抛**。"""
    req_id = uuid.uuid4().hex[:16]
    fut = asyncio.get_running_loop().create_future()
    _pending[req_id] = fut
    try:
        sent = await manager.send_to_node(node_id, {**cmd, "req_id": req_id})
        if not sent:
            return {"success": False, "error": "节点当前离线，无法执行"}
        try:
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "节点未在超时时间内确认"}
        return payload if isinstance(payload, dict) else {"success": False,
                                                          "error": "回包无法解析"}
    finally:
        discard(req_id)
```

`limit_watch.cancel_pending_on_node` 改为调用它（**行为不变，纯重构**），`ws_gateway` 增分支：

```python
elif mtype in ("cancel_pending_result", "cancel_pending_all_result"):
    node_rpc.resolve(data.get("req_id"), data)
```

> **注意**：合并分支后 `limit_watch.resolve_cancel` 的调用点要一起迁移，
> 否则同一个 req_id 会被两个 resolver 抢（都不会命中，但留下误导性死代码）。
> `limit_watch.resolve_cancel` / `discard_cancel` 保留为薄转发以免破坏既有测试。

---

## 5. 前端设计

### 5.1 类型与 store

`frontend/src/api/types.ts` 补挂单类型（数据**早已在快照里**，只是没声明）：

```ts
/** 未成交挂单：字段口径对齐节点侧 mt5_client.pending_orders() */
export interface PendingOrder {
  ticket: number
  symbol: string
  type: string          // BUY / SELL
  pending_kind: string  // limit / stop / stop_limit ...
  volume: number
  price_open: number
  sl: number
  tp: number
  magic: number
  comment: string
  time: number
}

export interface AccountSnapshot {
  // ...existing
  /** 未成交挂单：限价开仓在成交前只有它（M2：数据链路本来就通） */
  orders?: PendingOrder[]
}

export interface CancelPendingRequest { scope: 'all' | 'manual' | 'strategy' }

export interface CancelPendingResult {
  status: string
  node_id: string
  strategies: { stopped: number; forced: number; total: number }
  cancelled: number
  failed: Array<{ ticket: number; symbol: string; error: string }>
  remaining: number
  rounds: number
  limit_watch_active?: boolean
}
```

`frontend/src/stores/hub.ts` 加方法（与 `closeNode` 并列，`hub.ts:383`）：

```ts
// ---- 远程撤挂单 ----
async cancelPendingAll(nodeId: string, body: CancelPendingRequest): Promise<CancelPendingResult> {
  return (await api.post(`/api/nodes/${nodeId}/cancel-pending`, body)).data
},
```

### 5.2 按钮位置与视觉

放在卡片底部与「平掉该节点全部」并排（§1.6）：

```
┌─────────────────────────────────────────────┐
│ ● 主节点名称   [在线]  [★ 主节点]      ⋯    │
│ 12345678 @ Broker-Server                    │
│  净值        持仓        盈亏               │
│  ─────────────────────────────              │
│  查看全部 N 个持仓 →  [删挂单(12)] [平掉全部] │
└─────────────────────────────────────────────┘
```

| 项 | 决策 | 理由 |
|---|---|---|
| 位置 | 卡片底部操作区（**不进 `⋯` 菜单**） | 挂单清理是运维高频动作，要一眼可见 |
| 可见性 | **仅 `n.is_primary`**（决策 2） | 主节点是唯一会被分发策略信号的节点，也是挂单唯一堆积点 |
| 视觉 | 告警色（橙/黄），**与平仓的危险色（红）区分** | 两个都是破坏性操作，颜色必须分层，否则误点 |
| 文案 | `删挂单 (12)` 带数量徽标 | 点之前就知道有没有东西可删（§2.5） |

### 5.3 disabled 条件

| 条件 | 表现 | 理由 |
|---|---|---|
| 节点离线 | disabled，tooltip「节点离线，无法操作」 | 后端会 409，前端先挡一道 |
| `orders` 为空 | disabled，文案「无挂单」 | 无意义的空操作 |
| `orders === undefined` | disabled，tooltip「等待节点上报挂单数据」 | 老版本节点客户端没有这个字段 |
| 请求进行中 | disabled + loading | 防重复提交（服务端仍有 §3.2 的锁兜底） |

### 5.4 二次确认弹窗

用 `ElMessageBox`（比现有 `confirmAction` 承载信息量大；`confirmAction` 是二选一文本确认，装不下明细列表）：

```
⚠ 确认删除「主节点A」的全部挂单？

该节点当前有 12 张挂单（手工单 5 张 / 策略单 7 张）：
  • XAUUSD  BUY  0.10手  @3450.20   magic=0
  • EURUSD  SELL 0.50手  @1.0850    magic=0
  ...（最多列 20 张，超出显示「等共 12 条」）

· 只删除挂单，已有持仓不受影响。
· 将同时停止该节点上 3 个在跑策略任务（否则策略会自动补单）。
· 该节点属于开启限价监听的分组，删除后这些手工限价单不会再触发策略。

[取消]  [确认删除 12 张挂单]
```

最后一条由 `limit_watch_active` 控制显隐。
挂单明细与拆分统计**全部来自前端已有快照**，不需要额外请求。

### 5.5 结果反馈

| 情况 | 反馈 |
|---|---|
| 全成功 | `ElMessage.success('已删除 12 张挂单')` |
| 部分失败 | `ElMessage.warning('删除 12 张挂单，2 张失败')` + 展开失败明细（ticket / 品种 / 原因） |
| `remaining > 0` 且 `rounds == 2` | 额外提示「撤单后系统又生成了 N 张挂单，可能是限价监听自动触发，请检查分组监听开关」 |
| `strategies.forced > 0` | 额外提示「其中 N 个任务为强制停止」 |
| 409 节点锁 | 「该节点正在执行删挂单操作」 |

---

## 6. 边缘情况与风险

### 6.1 ⚠️ 「没有主节点就没有入口」（决策 2 的体验缺口）

按决策 2，未设主节点的账号在首页**完全没有**这个功能。

**认知上自洽**：主节点是唯一接收策略信号的节点，因此也是唯一会堆积挂单的地方。

**兜底方案**（低成本，建议做）：在首页已有的管理员横幅区补一句提示：

> 「尚未设置主节点，删除挂单入口在设置主节点后可用。」

不改变按钮可见性策略，只补一句说明，避免用户以为功能坏了。

### 6.2 ⚠️ 限价监听（`limit_watch`）的双向干扰 —— 最高风险项

`limit_watch.handle_account_orders` 在**每次账户快照后**扫描挂单，
对 magic=0 的手工限价单执行「撤单 → 转 strategy 信号 → 生成新策略挂单」（`limit_watch.py:348-364`）。

| 场景 | 现象 | 处理 |
|---|---|---|
| **A** 我们删掉了手工单 | 下一轮快照里订单已消失，limit_watch 无事可做 | ✅ 正常，无需处理 |
| **B** 我们删单时 limit_watch 正在处理同一张单 | limit_watch 的撤单 RPC 失败 → 记 `cancel_failed` 日志 → 用户看到红字误以为系统故障 | ⚠️ **需修**：撤单失败分支先查"该 ticket 是否已被人工批量撤掉"，若是则**静默跳过**，不记失败日志 |
| **C** 撤单完成后 limit_watch 又生成新策略单 | 撤了又出来同样数量 | ⚠️ **需修**：这正是 §2.3 复扫要检测的情况，检测到后停止复扫并提示用户 |

**建议在 Step [4] 撤单期间给该节点挂一个「人工撤单中」短标记**（Redis TTL ≈ 10s），
`limit_watch.handle_account_orders` 在此窗口内**跳过该节点**的挂单处理，避免两种逻辑打架：

```python
# handle_account_orders 开头
if await store.is_manual_cancel_pending(node_id):   # TTL 10s
    logger.info("limit_watch skip node %s: manual cancel in progress", node_id)
    return
```

配套 Redis key（沿用 `redis_store.py` 既有 K_ 常量风格）：

```python
K_CANCEL_PENDING_BUSY = "cancel_pending:busy:{}"       # 撤单中窗口（给 limit_watch 避让）
K_CANCEL_PENDING_LOCK = "cancel_pending:lock:{}"       # 节点级互斥锁（§3.2 并发保护）
```

### 6.3 对账副作用：子任务会被判为「已平仓」

撤掉策略挂单后，账户快照的判据是「持仓 ∪ 挂单」：

```387:391:backend/app/ws_gateway.py
    magics = _magics_of(positions) | _magics_of(orders or [])
    hits = _reconcile_hits.setdefault(node_id, {})
    for done in await group_persist.reconcile_node_positions(
        node_id, magics, hits=hits,
    ):
```

挂单没了、持仓被 `close_by_magic` 平了 → 该 magic 从快照消失 →
`reconcile_node_positions` 在连续 `RECONCILE_EMPTY_CONFIRM` 轮后收口，
写入 `finish_reason = "reconciled_no_position"`（`group_persist.py:910`）。

**这是符合预期的**（任务确实结束了），但**状态文案必须准确**：
不能让它显示成「任务正常完成」。

本方案通过 `stop_all_on_node(reason="manual_cancel_pending")` 留下 stop reason 痕迹
（`close.py` 的同类操作用的是 `manual_close_all`，本方案用 `manual_cancel_pending` 区分），
前端在展示收口原因时优先显示 stop reason。

### 6.4 老版本节点客户端兼容

老版本节点客户端不认识 `cancel_pending_all` → **不回包** → 8s 超时。

| 项 | 处理 |
|---|---|
| 超时文案 | 区分开：「节点未响应（可能客户端版本过低，不支持批量撤挂单）」，不要笼统说"超时" |
| 版本标记 | `node_client/version.py` 的 `BASE_VERSION = "1.1.0"` 提升到 `1.2.0`；`build_version.py` 自动追加时间戳 |
| 后台可见性 | 既有「客户端版本」页面（`client_version_api.py`）已能展示版本，可标红提示需升级 |

> **`remaining` 的老客户端兼容问题**：老客户端不回包 → `remaining` 拿不到 → 复扫无从判断。
> 超时即视为"撤单结果未知"，**不要**据此重发第二轮（可能重复撤已撤的单，虽然撤单幂等，
> 但会产生一堆噪音日志）。超时直接返回错误即可。

### 6.5 并发保护：同节点重复点击

同一节点连续两次点击 → 两次撤单并行 → 第二次拿到的是已撤掉的 ticket 列表 → 一堆失败噪音。

**服务端 Redis 节点级锁**（TTL 30s，`SET NX`，沿用 §1.7 既有范式）：

```python
async def acquire_cancel_pending_lock(self, node_id: str, ttl_s: int = 30) -> bool:
    return bool(await self.r.set(K_CANCEL_PENDING_LOCK.format(node_id), "1", nx=True, ex=ttl_s))

async def release_cancel_pending_lock(self, node_id: str) -> None:
    await self.r.delete(K_CANCEL_PENDING_LOCK.format(node_id))
```

前端按钮 loading 只挡单标签页；**多标签页 / 多用户并发必须靠服务端锁**。

### 6.6 边界场景表

| 场景 | 行为 | 说明 |
|---|---|---|
| 节点离线 | 409 `node offline` | 与 `close` 完全一致 |
| 节点无挂单 | 正常返回 `cancelled: 0`，前端给「该节点没有挂单」提示 | 不报错 |
| 快照里没有 `orders` 字段 | 前端 disabled；后端仍可执行（节点侧自己读） | 老客户端兜底 |
| 停策略时节点离线 | `stop_all_on_node` 返回 `forced`（强制收口） | 已有行为，`_stop_subtask` 处理 |
| 挂单刚成交就点了撤单 | 撤单失败「订单已成交」，计入 `failed` | 正常，提示即可 |
| 部分挂单被 broker 拒绝 | 计入 `failed`，带 `error` 明细 | **绝不吞掉** |
| 复扫 2 轮后仍有挂单 | 停止复扫，提示"系统又生成了挂单" | §2.3 |
| `scope=manual` 预留下游 | 协议层已支持，UI 本期不暴露 | 为二期"只删手工单"留口 |

---

## 7. 实施步骤

> 后端先行，每步可独立验证。

### 阶段 A：节点客户端（协议 + 执行）

| 步 | 文件 | 内容 |
|---|---|---|
| A1 | `node_client/mt5_client.py` | `cancel_pending_all(scope, magics?)`：复用 `_cancel_many`，返回含 `remaining` |
| A2 | `node_client/node_client.py` | `_handle` 加 `cancel_pending_all` 分支 + `_do_cancel_pending_all` + 回包 |
| A3 | `node_client/version.py` | `BASE_VERSION` `1.1.0` → `1.2.0` |

**A2 验证**（关键）：
```
1. 挂 3 张单 → 下发 cancel_pending_all(scope=all) → 3 张全撤、remaining=0
2. 无挂单 → cancelled=0、remaining=0、success=True（**不得抛异常**）
3. scope=manual → 只撤 magic=0，策略单保留
4. 部分撤单失败 → failed[] 带 ticket + error，success=False
```

**回滚**：删 A1/A2 分支即可；A3 的版本号回退不影响功能。

### 阶段 B：后端公共层

| 步 | 文件 | 内容 |
|---|---|---|
| B1 | `backend/app/node_rpc.py` | **新建**：`request()` / `resolve()` / `discard()` / `reset_waiters()` |
| B2 | `backend/app/ws_gateway.py` | `_session` 增 `cancel_pending_all_result` 分支（**与 `cancel_pending_result` 合并为一条**） |
| B3 | `backend/app/limit_watch.py` | `cancel_pending_on_node` 改调 `node_rpc.request`（行为不变）；`resolve_cancel` 变薄转发 |
| B4 | `backend/app/models.py` | `build_cancel_all_command()` + `CancelPendingRequest` + `CancelPendingResult` |
| B5 | `backend/app/redis_store.py` | 两个 K_ 常量 + `acquire/release_cancel_pending_lock` + `is_manual_cancel_pending` |

**B3 验证**（**最易出回归**）：限价监听既有用例全绿——`limit_watch` 换 RPC 实现后行为必须完全一致

**B5 验证**：`acquire` 两次同时调 → 第二次返回 `False`

**回滚**：撤销 B1~B5；`limit_watch` 回退到自带 `_pending_cancels`（git 单文件回滚即可）

### 阶段 C：后端编排 + 路由

| 步 | 文件 | 内容 |
|---|---|---|
| C1 | `backend/app/group_dispatcher.py` | `stop_all_on_node` 的 `reason` 参数已有（§1.4），**无需改动**，直接传 `manual_cancel_pending` |
| C2 | `backend/app/pending_api.py` | **新建**：路由 + §3.2 六步编排 + 审计 + 节点锁 |
| C3 | `backend/app/main.py` | 注册 `pending_api.router` |
| C4 | `backend/app/close.py` | `_stop_strategies_on_flatten` 保持不动；仅在需要时抽共用（**可选，非必需**） |

**C2 验证**（关键，按序）：
```
1. 在线节点 + 3 张手工单 + 0 策略任务 → cancelled=3、remaining=0、rounds=1
2. 在线节点 + 网格策略在跑（3 个子任务，各 2 张挂单）
   → strategies.total=3、cancelled=6、remaining=0（**验证停策略确实先发生了**）
3. 节点离线 → 409 node offline
4. 普通用户操作他人节点 → 404（owned_node_or_404）
5. 同节点并发两次请求 → 第二次 409「该节点正在执行删挂单操作」
6. 持仓 5 笔 + 挂单 3 张 → 操作后 **持仓仍为 5 笔**（**验证不动持仓**）
```

**回滚**：C2/C3 撤销（前端按钮同时隐藏）

### 阶段 D：限价监听互斥（§6.2）

| 步 | 文件 | 内容 |
|---|---|---|
| D1 | `backend/app/limit_watch.py` | `handle_account_orders` 开头检查「人工撤单中」窗口，是则跳过 |
| D2 | `backend/app/limit_watch.py` | 撤单失败分支：查该 ticket 是否被人工撤掉，是则**静默跳过**不记 `cancel_failed` |

**D2 验证**（关键）：
```
1. 人工删挂单命中 limit_watch 正在处理的那张单
   → limit_watch **不**产生 cancel_failed 日志（**用户看不到误报**）
2. 正常限价单触发 → 仍正常撤单 + 分发（**不破坏既有行为**）
```

**回滚**：D1/D2 撤销（仅损失"日志噪音"和"并发打架"，不损失功能正确性）

### 阶段 E：前端

| 步 | 文件 | 内容 |
|---|---|---|
| E1 | `frontend/src/api/types.ts` | `PendingOrder` + `AccountSnapshot.orders` + `CancelPendingRequest/Result` |
| E2 | `frontend/src/stores/hub.ts` | `cancelPendingAll()` |
| E3 | `frontend/src/views/HomeView.vue` | 按钮（仅 `is_primary`）+ 徽标 + disabled 四态 |
| E4 | `frontend/src/views/HomeView.vue` | `ElMessageBox` 确认弹窗（明细列表 + 三条提示） |
| E5 | `frontend/src/views/HomeView.vue` | 结果反馈（五分支，§5.5）+ 无主节点提示（§6.1） |

**E3 验证**：
```
1. 主节点卡片 → 按钮显示，徽标 = 快照 orders.length
2. 非主节点卡片 → 按钮**不出现**
3. 无主节点 → 首页无按钮 + 横幅提示
4. 离线 / 无挂单 / orders 缺失 / 请求中 → 四种 disabled 各自生效
```

**E4 验证**：确认弹窗的挂单明细条数 = 实际 orders 数；三条提示按条件显隐

**E5 验证**：构造 `failed` 非空的响应 → warning + 明细展开，不被吞掉

---

## 8. 改造后的完整链路

```
                          ┌────────────────────────────────┐
                          │  首页 HomeView                 │
                          │  主节点卡片 [删挂单 (12)]       │
                          └───────────────┬────────────────┘
                                          │ confirmAction 明细确认
                                          ▼
                          ┌────────────────────────────────┐
                          │  hub.cancelPendingAll()        │
                          │  POST /api/nodes/{id}/cancel-pending
                          └───────────────┬────────────────┘
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │  pending_api.orchestrate_cancel_pending()       │
                │  require_menu(NODES|DASHBOARD) + owned_node    │
                │  + 节点在线 + Redis 节点锁                       │
                └──┬──────────────┬───────────────┬───────────────┘
                   │              │               │
      [2] 停策略   │   [4]+[5] 撤单+复扫        │  [6] 审计
                   ▼              ▼               ▼
        group_dispatcher     node_rpc.request   persist.audit
        .stop_all_on_node   → cmd=cancel_       (category="node")
        (reason=            _pending_all
        manual_cancel_      (最多 2 轮，
         pending)           remaining>0 才重发)
                   │              │
                   ▼              ▼
              节点 strategy_stop   节点 cancel_pending_all
                   │              │
                   ▼              ▼
              close_by_magic    mt5.cancel_pending_all()
              （撤策略自己的单）  └─ _cancel_many(pending_orders())
                   │                  └─ remaining = 再读一次
                   ▼
              ┌──────────────────────────────────────────┐
              │ limit_watch.handle_account_orders()      │
              │ 【§6.2】人工撤单中窗口 → 跳过该节点        │
              └──────────────────────────────────────────┘
```

**一致性保证**：停策略与撤单走**两条独立命令**，但由服务端单次编排串起来；
`req_id` 配对只有 `node_rpc` 一处实现（`limit_watch` 复用），
节点级互斥只有 `redis_store` 一处实现（沿用 `acquire_exec_lock` 范式）。

---

## 9. 边界与风险汇总

| 场景 | 行为 | 说明 |
|---|---|---|
| **策略补单竞态** | 靠「先停策略」根治 | §2.2，顺序不可颠倒 |
| **限价监听自动重分发** | 复扫检测到「撤了又出来」→ 停止复扫 + 提示 | §2.3 / §6.2-C |
| **limit_watch 撤单误报** | 人工撤单短路，静默跳过 | §6.2-B |
| **对账把子任务判为 done** | 符合预期，但文案要显示 stop reason | §6.3 |
| **老客户端不回包** | 超时返回错误，**不重发** | §6.4 |
| **并发点击** | 服务端 Redis 节点锁 409 | §6.5 |
| **撤单失败（已成交/价格变动）** | 计入 `failed` 带原因，前端 warning | 绝不吞掉 |
| **无主节点** | 首页无入口 + 横幅提示 | §6.1，认知自洽 |
| **`remaining` 拿不到（老客户端）** | 超时即"结果未知"，不重试 | §6.4 |
| **scope 预留下游** | 协议层支持 manual/strategy，UI 本期不暴露 | 二期留口 |

### 已知取舍

1. **复扫最多 2 轮** —— 不做无限收敛。理由：外部补单（limit_watch）无法靠重试解决，
   重试到第三次也只是浪费 2 秒。宁可停下并告知用户，也不无限循环。
2. **`cancel_pending_all` 不复用 `close` 命令** —— 宁可加一条命令，
   也不在 `close_target` 上加 `"pending"` 变体。理由：`close` 的三条分支
   （ticket / symbol / all）语义已经饱满，混入撤单会让 `_close_detail` 的文案分叉爆炸。
3. **编排逻辑放新文件而非 `close.py`** —— `close.py` 是平仓域，
   塞进撤单会让它的审计 action 语义变浑。代价是多一个 router 文件（可接受）。
4. **按钮仅在主节点** —— 按决策 2。但这也意味着**总览页 / 节点详情页没有此功能**。
   若后续要在别处补，是纯前端复用（store 方法已通用），后端零改动。
5. **等策略收口只等 3s** —— 节点侧 `close_by_magic` 若遇瞬时错误会重试（最多 3 次），
   3s 是「够用即止」。超时后继续走撤单——反正 Step [4] 是全量扫撤，**兜底**。

---

## 10. 文件清单

**修改**

| 文件 | 改动 | 规模 |
|---|---|---|
| `node_client/mt5_client.py` | `cancel_pending_all()`（复用 `_cancel_many`） | 小 |
| `node_client/node_client.py` | `_handle` 分支 + `_do_cancel_pending_all` + 回包 | 小 |
| `node_client/version.py` | `BASE_VERSION` 1.1.0 → 1.2.0 | 极小 |
| `backend/app/ws_gateway.py` | 新回包类型分发（与现分支合并） | 极小 |
| `backend/app/limit_watch.py` | 换用 `node_rpc` + 人工撤单窗口避让 + 误报短路 | 中 |
| `backend/app/models.py` | `build_cancel_all_command` + 2 个请求/响应模型 | 小 |
| `backend/app/redis_store.py` | 2 个 K_ 常量 + 2 个锁方法 | 小 |
| `backend/app/main.py` | 注册 `pending_api.router` | 极小 |
| `frontend/src/api/types.ts` | `PendingOrder` + `AccountSnapshot.orders` + 2 个类型 | 小 |
| `frontend/src/stores/hub.ts` | `cancelPendingAll()` | 极小 |
| `frontend/src/views/HomeView.vue` | 按钮 + 徽标 + 4 态 disabled + 确认弹窗 + 结果反馈 | 中 |

**新增**

| 文件 | 内容 |
|---|---|
| `backend/app/node_rpc.py` | 通用 req_id 配对的「发命令 + 等回包」 |
| `backend/app/pending_api.py` | 路由 + 六步编排 + 审计 + 节点锁 |
| `backend/tests/test_pending_api.py` | 撤单接口用例（§7 阶段 C 的 6 条） |
| `backend/tests/test_node_rpc.py` | RPC 配对：超时 / 离线 / 正常回包 |
| `backend/tests/test_limit_watch.py` | 追加 2 条（人工撤单避让 + 误报短路） |

**不改动**（已核查，确认无需动）

- `backend/app/close.py` —— 平仓链路独立；`_stop_strategies_on_flatten` 保持原样
- `backend/app/group_dispatcher.py` —— `stop_all_on_node` 的 `reason` 参数**已存在**（§1.4），直接传参即可
- `backend/app/group_persist.py` —— `reconcile_node_positions` 判据「持仓 ∪ 挂单」无需改
- `backend/app/ws_gateway.py` 的 `_save_account` —— `orders` 已在快照里（M2）
- `backend/app/persist.py` —— `persist.audit(category="node")` 已满足，无需新表
- `backend/app/client_version.py` —— 版本机制已有，只改 `version.py` 的常量
- `frontend/src/views/NodesView.vue` / `NodeDetailView.vue` —— 本期不挂入口（§9 取舍 4）

---

## 11. 测试清单

| 层级 | 文件 | 覆盖 |
|---|---|---|
| 节点 | `node_client/test_node_client.py` | 批量撤单成功 / 部分失败 / 空挂单 / `scope=manual` 过滤 |
| 节点 | `node_client/test_mt5_close.py` | `cancel_pending_all` 的 `_cancel_many` 复用与 `remaining` 复读 |
| 后端 | `backend/tests/test_pending_api.py` | §7 阶段 C 的 6 条（含**持仓不受影响**） |
| 后端 | `backend/tests/test_node_rpc.py` | 回包配对 / 超时 / 节点离线 |
| 后端 | `backend/tests/test_limit_watch.py` | 人工撤单窗口避让 / 误报短路 / **既有行为不回归** |
| 手工 | 首页 | 主节点显示 + 非主节点不显示 + 4 态 disabled + 结果五分支 |

**必跑的回归**：
- `backend/tests/test_limit_watch.py` 全绿（B3 是纯重构，**行为必须完全一致**）
- `backend/tests/test_node_client.py` 全绿（A2 加了新的命令分支）
- 任一策略链路测试（停策略是既有能力，但 `reason` 值变了，确认无按 reason 过滤的断言）

---

## 12. 待确认

| # | 问题 | 当前默认 | 影响 |
|---|---|---|---|
| 1 | 等策略收口的 3s 超时是否够？ | 3s | 偏短则撤单要多跑一轮（幂等，安全）；偏长则用户等得久。**可先按 3s 上线，观察日志** |
| 2 | 复扫 2 轮是否够？ | 2 轮 | 偏少则漏掉 limit_watch 的延迟重分发（会提示用户）；偏多只是多花 4s |
| 3 | 撤单失败明细是否需要落库（供事后追查）？ | **只落审计流**，不建新表 | 若要独立查询入口，需新增 `manual_cancel_log` 表 + 列表页（**建议二期**） |
| 4 | 总览页 / 节点详情页是否也要挂入口？ | **本期不做** | 后端已通用（store 方法 + 单节点路由），二期纯前端改动 |
| 5 | 是否要 `scope=manual`（「只删我手动挂的」）的 UI？ | **本期不做** | 协议层已支持；二期加一个下拉或次级按钮即可 |

---

**文档版本**：v1（2026-10-22）
**状态**：待实施
**关联**：`close.py` 平仓链路 · `limit_watch.py` 限价监听 · `group_dispatcher.stop_all_on_node` 停策略