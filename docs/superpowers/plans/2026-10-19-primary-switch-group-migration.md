# 主节点切换 → 交易页专属分组同步方案（设计稿，未实现）

> 本文档只设计、不写代码。**v2 修订**：在 v1 基础上引入"旧主节点仍有在跑任务"的前置校验 —— 出现阻塞时**直接拒绝**主节点切换，不提供任何强制绕过路径。**v3 修订**：v2 中预留的 `force` 兜底参数、`terminate_node_tasks`、`force_primary_switch` 审计全部移除 —— 唯一允许切换的场景是"旧主节点上没有任何在跑任务"。这是不可妥协的硬规则，避免把执行器与持仓追踪链串到错误的节点上。

---

## 1. 背景与现状

### 1.1 主节点（首页 / 节点页均可切换）

- 数据模型：`nodes.is_primary: bool`，按 `owner_user_id` 维度每用户/管理员最多一个主节点（唯一约束由 `node_service.set_primary` 维护）。
- 切换 API：`POST /api/nodes/{node_id}/primary` → `node_service.set_primary`：
  - 把同 owner 下其它节点的 `is_primary` 全部清掉；
  - 仅刷新节点本身的 Redis 缓存（并刷新同 owner 所有节点的 `nodes:*` 缓存）；
  - **不会** 触碰任何分组（`NodeGroup` / `NodeGroupMember`）。
- 前端：首页卡片菜单的「★ / ☆ 设置主节点 / 取消主节点」入口（`HomeView.vue#togglePrimary`），切换后只有 toast，没有跨页联动。

### 1.2 交易页「专属分组 / 策略」的生成机制

- 触发入口：`POST /api/trading/quick-trigger`（`trading_api.py#quick_trigger`）。
- 命名规则（`trading_api.py#_group_name`）：`{strategy_type}-{username}-{mt5_login}-{symbol}`，例如 `trend_market-alice-10023-XAUUSD`。
- 首次触发：
  1. 找到/创建专属 `TradingStrategy`（按名字 + owner 唯一）；
  2. 找到/创建专属 `NodeGroup`，并把 **当前主节点** `body.node_id` 加入成员；
  3. 走 `webhook.process_signal` 真正下信号。
- 后续触发：按同样名字复用策略 + 分组，调用 `_ensure_member` 兜底补成员。

### 1.3 当前痛点

- 用户在首页把 A 节点设为主节点后，到交易页触发若干次开仓 → 产生了一组以 `A` 为唯一成员的分组（含对应的策略）。
- 用户切回首页把主节点换成 B，再回交易页触发同样品种的开仓：
  - 现在的代码会按新名字（`btm-{user}-{B.login}-{symbol}`）找到/新建一个 **新的专属分组**；
  - 旧的以 `A` 为成员的分组 **仍然开着**（处于 `enabled=True` 状态），继续接收 webhook 信号；
  - 等于「一个主节点 / 一组分组」变成了「两个主节点（标记 + 旧的隐式成员）/ 两套分组同时在跑」。
- 同理，分组里的限价监听成员也以「旧主节点」为准，新主节点加入后无法接管触发源。

### 1.4 v2 新增痛点：旧主节点正在执行任务

`GroupTaskDispatch`（节点子任务账本）记录了每个主节点当前正在执行的策略信号下发时分配给该节点的 `magic` 魔术号与节点互斥占位。一个信号在某个分组里被下发到 A 节点后，A 节点的执行器会按 `magic` 持续监控持仓 / 加仓 / 平仓 —— 这个流程不能轻易换"主节点"承载：

| 子任务 status | 业务含义 | 在跑 / 不可打断 |
|---|---|---|
| `pending` / `sent` | 命令已下但首单尚未回报 | ✅ 在跑 |
| `opened` / `running` / `closing` | 节点正在按策略监控 / 加仓 / 收尾 | ✅ 在跑 |
| `stop_failed` / `detached` / `faulted` | 节点已停手但仍有 `magic` 残余持仓 | ✅ 在跑（不能换主节点） |
| `done` / `failed` / `skipped` / `offline` | 终态 | ❌ 已收口 |

定义（`group_rules.py`）：
- `SUBTASK_PENDING = ("pending", "sent")`
- `SUBTASK_RUNNING = ("opened", "running", "closing")`
- `SUBTASK_STUCK = ("stop_failed", "detached", "faulted")`
- `SUBTASK_HOLDS_LOCK = SUBTASK_PENDING + SUBTASK_RUNNING + SUBTASK_STUCK`
- `SUBTASK_TERMINAL = ("done", "failed", "skipped", "offline")`

**判定"任务在跑" = `GroupTaskDispatch.status IN SUBTASK_HOLDS_LOCK`**（即非终态）。

> 这条 v1 没有 —— v1 直接让旧主节点从分组成员里被踢出去，但**没有同步停掉** `GroupTaskDispatch` 的子任务：节点上的执行器并不知道"我不再是主节点了"，还在按原 magic 跑。如果同时新主节点 B 也通过同名分组收到新信号，B 节点会另起一个 magic、自己的持仓 —— 两套并发运行。
> 所以**前置条件：旧主节点的所有任务必须先收口，才能切换主节点**。

---

## 2. 设计目标

主节点切换是「同一账号下的运维动作」，业务语义是 **「这账号的主交易终端换了一台」**。因此期望：

1. **数据完整性优先**：旧主节点上**仍有在跑任务**（含持仓追踪 / 加仓监控 / 残仓未清）时，**拒绝切换主节点**，避免把任务链串到错误的执行器；
2. **语义一致**：旧主节点不再担任任何专属分组的成员 / 上报源；
3. **零数据丢失**：用户历史触发的策略、分组、信号记录保持不变（只是成员被换）；
4. **可逆可审计**：每一次成员替换都走库 + 审计，能看到「A→B」的迁移记录；
5. **策略可独立**：策略本身与节点绑定解耦（已有的策略可继续挂到任意分组上），切换主节点不重建策略；
6. **不破坏既有 API 契约**：现有 `quick-trigger` 的「按名字找 / 按 owner 隔离」逻辑保持不变；只在主节点切换这条路径上做联动。

---

## 3. 总体方案（高层）

> 主节点切换路径分成 **三阶段**（这是 v2 相对 v1 的最大变化）：
>
> 0. **阶段 0（前置）—— 阻塞检查**：在主节点回写 `is_primary` 之前，先校验「被替换 / 被取消的旧主节点」上是否仍有 `SUBTASK_HOLDS_LOCK` 的子任务。如果有，**直接拒绝**，并把阻塞明细返回给前端（让用户先去交易页 / 任务页结束这些任务）。
> 1. **阶段 1**：原 `set_primary` 内的 `is_primary` 翻转 + `cache_node` 刷新（事务 1，已 commit）。
> 2. **阶段 2（新增）**：紧跟其后调用 `group_service.migrate_primary_groups(old_id, new_id, owner)`，把「归属于这个 owner、且与主节点切换直接相关的专属分组」做成员替换。

三个关键判定：

1. **如何识别「旧主节点上仍有在跑任务」？**
   - 主体判据：`group_persist.active_subtasks_for_node(old_id)` 非空 → 阻塞；
   - 等价 SQL：`SELECT 1 FROM group_task_dispatch WHERE node_id=? AND status NOT IN (...)`；
   - `group_rules.SUBTASK_TERMINAL = ("done", "failed", "skipped", "offline")` 之外的 status 全部视为在跑。
2. **如何识别「与主节点切换相关的专属分组」？**
   - 命名匹配专属规则 `^(trend_market|trend_limit|grid)-.+-(\d+)-.+$`；
   - `owner_user_id` 匹配；
   - `remark.startswith("交易页自动创建")`；
   - 这部分与 v1 保持不变。
3. **唯一允许的放行条件：旧主节点无任何在跑子任务**（`GroupTaskDispatch.status NOT IN SUBTASK_HOLDS_LOCK`）。
   - **不提供任何强制绕过 / force 路径**；管理员也要遵守同样的前置条件；
   - 遇到阻塞 → 用户必须先去交易页 / 任务页收口（或等待节点自身完成），再来重试；
   - 这条原则不接受"运维紧急"妥协，因为 force 强行切换会让旧主节点的执行器与新主节点的成员关系错位，引发更隐蔽的资损。

---

## 4. 详细设计

### 4.1 触发点：哪个函数里发起联动？

| 位置 | 优点 | 缺点 |
|------|------|------|
| (A) `nodes.set_primary_node` 路由层 | 紧贴 API，一处加完 | 把领域知识泄漏到路由层；测试需要 mock 整个 HTTP 层 |
| (B) `node_service.set_primary` 内 | 唯一入口（不论是 API 还是后台脚本），最自然 | 需要在 commit 后再做一次 DB+缓存写入，事务边界复杂 |
| (C) 独立服务 + 事件订阅（`events.py`） | 完全解耦 | 当前仓库没有稳定的事件总线，引入要额外搭桥 |

**v2 依然选 (B)，但拆为三阶段**：

| 阶段 | 函数 | 事务边界 | 失败处理 |
|------|------|----------|----------|
| 0 | `precheck_active_blocking(node_id, *, principal)` | 独立只读 session | 命中阻塞 → `HTTPException(409, detail={blocking_tasks: [...]})`，**不进入阶段 1** |
| 1 | `set_primary(...)` 原逻辑 | 事务 1（已有） | 保持原样 |
| 2 | `migrate_primary_groups(...)` | 事务 2 | best-effort，只记日志，不回滚阶段 1 |

阶段 0 **与阶段 1 不能合并**：阶段 0 的语义是"决策前置"，必须先看到阻塞列表再决定放不放行；强行合并会让阶段 1 的"主节点切换"成为不可逆动作（即使后续又回滚，已经下发的 status flip 也不好收拾）。

### 4.2 阶段 0：阻塞前置检查

**新增函数 `backend/app/node_service.py`**：

```python
async def precheck_active_blocking(
    store: RedisStore,
    *,
    target_node_id: str,         # 即将被替换 / 被取消的旧主节点
    next_node_id: Optional[str], # 即将成为新主节点的节点 id（设主时为新 id，取消时为 None）
) -> None:
    """前置校验：旧主节点上是否仍有在跑子任务；命中阻塞则拒绝放行。

    阻塞判据：
      - GroupTaskDispatch 中 node_id == target_node_id 且
        status NOT IN ("done", "failed", "skipped", "offline") → 阻塞
      - 节点当前在线且存在 status == "pending"/"sent" 的子任务 → 阻塞

    放行条件（唯一）：
      - 旧主节点无任何在跑子任务 → 放行
      - 任何其它情况（包括 force 请求、管理员运维请求）均不放行。

    Raises:
      PrimarySwitchBlocked：命中阻塞时抛出，前端拿到 409 + 阻塞明细。
    """
```

**新增「异常冒号」`HTTPException(409, ...)` 响应结构**：

```json
{
  "detail": "旧主节点仍有在跑任务，请先结束这些任务后再切换主节点",
  "code": "primary_switch_blocked",
  "blocking_tasks": [
    {
      "task_id": 12345,
      "group_id": "tr...",
      "group_name": "trend_market-alice-10023-XAUUSD",
      "symbol": "XAUUSD",
      "status": "running",
      "magic": 10010023,
      "opened_at": "2026-10-19T12:34:56",
      "position_count": 2,
      "pending_orders": 1
  ],
  "node_id": "old_node_id",
  "node_name": "..."
}
```

> 阻塞明细**最多返回 100 条**（前端有分页 + "全部"按钮即可，响应不要太重）。

### 4.3 阶段 1：`set_primary` 内的事务 1（保持原样 + 阶段 0 调用）

伪代码（**v2 在 commit 之前插入阶段 0 调用**）：

```text
async def set_primary(
    store, node_id, is_primary, *,
    audit_principal, audit_ip,
):
    # ── 阶段 0：阻塞前置（新增）── 无 force 参数
    victim_ids = collect_victim_node_ids(node_id, is_primary)
    for vid in victim_ids:
        await precheck_active_blocking(
            store,
            target_node_id=vid,
            next_node_id=node_id if is_primary else None,
        )

    # ── 阶段 1：原有逻辑不变 ──
    async with SessionLocal() as s1:
        ...
        if is_primary:
            cleared_rows = await _clear_primary_for_owner(...)
            old_primary_id = cleared_rows[0].node_id if cleared_rows else None
            row.is_primary = True
            new_primary_id = node_id
        else:
            old_primary_id = node_id if row.is_primary else None
            new_primary_id = None
            row.is_primary = False
        ...
        await s1.commit()

    # 缓存刷新

    # ── 阶段 2：联动迁移专属分组成员（已有） ──
    if old_primary_id != new_primary_id:
        try:
            migrated = await group_service.migrate_primary_groups(...)
        except Exception as e:
            logger.exception(...)
```

要点：

- **阶段 0 不通过 → 直接抛 `PrimarySwitchBlocked`（HTTP 409），阶段 1 完全不会执行**；
- **不接受任何 force / 强制绕过**：路由层不解析 `force` 参数，服务端 `set_primary` 不接受 `force` 关键字；阻塞即 409，唯一放开条件是"无任何 in-flight 任务"；
- 阶段 0 仅在「设主 / 取消主」两个动作上做：单纯「已经是主节点、还点了同一态」时 `set_primary` 早已 short-circuit 返回，不进入 0。

### 4.4 路由层：`POST /api/nodes/{node_id}/primary`

`backend/app/nodes.py#set_primary_node` 修改：

```python
@router.post("/{node_id}/primary", response_model=NodeOut)
async def set_primary_node(
    node_id: str,
    body: NodePrimaryUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_NODES)),
):
    """设置 / 取消某节点的主节点标记。

    前置校验：被替换 / 被取消的旧主节点上仍有在跑任务（status ∉ SUBTASK_TERMINAL）
    时直接返回 409 + 阻塞明细，让用户先去交易页 / 任务页收口。
    路由不接受任何强制参数 —— 旧主节点没有 in-flight 任务是切换的唯一前置条件。
    """
    await owned_node_or_404(store, p, node_id)
    try:
        d = await node_service.set_primary(
            store, node_id, bool(body.is_primary),
            audit_principal=p, audit_ip=client_ip(request),
        )
    except node_service.PrimarySwitchBlocked as e:
        # 把结构化明细塞进响应头 + body，前端拿来弹详情
        raise HTTPException(
            status_code=409,
            detail={
                "code": e.code,
                "message": str(e),
                "blocking_tasks": e.blocking_tasks,
            },
        )
    if not d:
        raise HTTPException(status_code=404, detail="节点不存在")
    return d
```

### 4.5 阻塞时的处理（无 force 兜底）

#### 唯一路径：用户提前结束任务

阶段 0 直接抛 409，让用户在交易页 / 任务页通过 `strategy_stop` 信号（或操作员全平）让旧主节点的子任务进入终态后，再回来重试。

**不接受任何强制绕过**：

- 不提供 `force` query 参数、不提供管理员"先关立即切换"的内部 API；
- 不在阶段 0 通过后主动给旧主节点发 `strategy_stop` 然后继续切换 —— 因为那等于变相的 force，违反了"唯一允许切换的场景是旧主节点无 in-flight 任务"的硬规则；
- 紧急运维场景下，正确的做法是：**先到交易页 / 任务页用正常流程停止 / 平掉旧主节点的任务**，等子任务进入终态后再切主节点 —— 这条流程本来就是业务侧已有的能力（CLOSE 信号、`/api/.../all-close` 全平等），不会成为运维阻塞点。

#### 为什么不做 force 兜底

| 反对方案 | 后果 |
|---------|------|
| force 后强制 `terminate_node_tasks` | 节点离线 / WS 抖动 → stop 没真的执行 → 主节点已切 / 旧节点还在跑 → 仓位追踪链串错 |
| force 后立刻阶段 1 翻转 | 同上，并新增"已 flip is_primary 但节点侧还在按旧 magic 加仓"的状态 |
| force 仅给管理员用 | 管理员失误造成的资损 ≠ 用户失误；事故定责更模糊；UX 上"force 能用"会让用户优先选 force 而非走标准收口路径 |
| 引入延迟异步 force（"后台异步停旧节点"） | 阶段 1 / 阶段 2 在异步任务完成前已 commit，行为退化成"普通切换 + 后台补偿"，与 force 等价，违反硬规则 |

> **结论**：阻塞时**直接拒绝**，让标准流程接管。子任务进入终态的语义是"执行器已确认收尾"，这是任何上层动作（切主节点 / 删除节点 / 转让节点）的基础前提。

### 4.6 阶段 2：成员迁移（v1 不变，列出要点）

新增（落在 `backend/app/group_service.py`）：

- `QUICK_TRIGGER_NAME_RE = re.compile(r"^(trend_market|trend_limit|grid)-.+-(\d+)-.+$")`
- `async def is_quick_trigger_group(group: dict) -> bool`
- `async def list_quick_trigger_groups_for_owner(session, *, owner_user_id, node_id: str) -> list[NodeGroup]`
- `async def migrate_primary_member(session, group_id, *, old_node_id, new_node_id, owner_user_id) -> Optional[dict]`
- `async def migrate_primary_groups(store, *, owner_user_id, old_node_id, new_node_id) -> list[str]`

事务 2 内：

1. 找出所有命中的 `NodeGroup`（详见 v1 §4.2）。
2. 对每个分组：
   - 在 `NodeGroupMember` 中删除 `node_id == old_id` 的行；
   - 检查 `new_id` 是否已经在成员列表里，没有则 append 到末尾；
   - 如果分组启用了限价监听 + 绑定了趋势策略 + 当前用户没显式传 `limit_watch_node_ids`，把 `new_id` 的 `limit_watch_report` 打开。
3. commit；事务 2 commit 成功后逐个 `store.cache_group(d)` 刷新 Redis。

### 4.7 取消主节点（is_primary=False）也要做阻塞检查吗？

**要**。取消主节点等同于"这账号当前没有主节点"，交易页的专属分组里残存一个不再被认作主节点的节点，逻辑上仍然是错位的 —— 但**更要紧**的是：旧主节点上的任务还在跑，**取消主节点不会自动停止任务**，等于"无主节点 + 有在跑任务"的孤儿状态。

- 取消时的 `precheck_active_blocking(target_node_id=被取消的旧主节点, next_node_id=None)`：
  - 与"切换主节点"使用同一套阻塞判据；
  - **不接受 force 绕过**；
  - 阶段 2 从命中分组里 **删除 `old_id`**，**不补**新节点；返回 `migrated_groups` 让前端提示。

### 4.8 前端改动

| 入口 | 改动 |
|------|------|
| `HomeView.vue#togglePrimary` | 调用前：如果 next=True 且原主节点非空，先发一个轻量查询 `GET /api/nodes/{old_primary}/active-tasks` 看是否阻塞；非阻塞才弹原确认；阻塞则**直接弹阻塞确认**（列出阻塞任务数 + 跳转链接），让用户去任务页清理后**重新尝试** —— **不提供跳过 / 强制**的入口。 |
| `HomeView.vue#togglePrimary`（响应处理） | 收到 409 → 打开 `ElMessageBox.alert`，标题「主节点切换被拦截」；明细显示「旧主节点仍有 N 条在跑任务」；列出每条 `group_name / status / magic / 持仓数 / 挂单数`；按钮「去任务页收口」「取消」；**不显示强制切换入口**。 |
| `TradingView.vue` | 不需要直接改动 —— `primaryNode` 因 `hub.fetchNodes` 更新而变化，UI 早就自然刷成新主节点。 |
| `GroupsView.vue` | 监听 `hub.nodes` 变化，若分组里的成员名单突然少了一个节点，弹一个 inline warning「本分组因主节点切换已自动移除旧主节点成员」。 |
| `NodesView.vue` | 主节点切换按钮与首页共用 `togglePrimary` 组件逻辑（统一前端行为）。 |

不需要新页面、不需要新路由。

### 4.9 后端响应 payload 扩展

- 正常 200：`NodeOut` + 响应头 `X-Migrated-Groups: <group_id>,<group_id>`（与 v1 一致，零侵入）。
- 阻塞 409：`{detail: {code, message, blocking_tasks: [...]}}`（v2 新增）。
- **不返回任何 force 相关的响应头 / 字段**。

---

## 5. 边界 / 风险

| 场景 | 行为 | 备注 |
|------|------|------|
| 旧主节点没有任何在跑任务 | 阶段 0 通过，正常切换 | **最常见**，v1 路径不变 |
| 旧主节点有 `pending`/`sent` 子任务 | 阶段 0 拒绝；返回 409 | 用户可在任务页 / 等首单回报 |
| 旧主节点有 `opened`/`running`/`closing` 子任务 | 阶段 0 拒绝；返回 409 | 必须先平仓 / 收口 |
| 旧主节点有 `stop_failed`/`detached`/`faulted`（残仓） | 阶段 0 拒绝；返回 409 | 残仓必须先处理掉 |
| 管理员要求 force 切换 | **接口不接受该参数 / 服务端不接受该路径** | 直接拒绝；不存在 force 路径 |
| 旧主节点不在任何专属分组里 | 命中为空，跳过迁移 | 正常情况 |
| 同一 owner 下同时有 2 个旧主节点标记（同 owner 多端并发） | `set_primary` 用 `_locked_node` 加行锁；并发安全 | 已实现 |
| 取消主节点时旧主节点仍有任务 | 阶段 0 拒绝；返回 409 | v2 新增；不接受 force |
| 新主节点已经在命中分组里 | 仅删除 `old_id`，不重复 append | v1 已有 |
| 命中分组里有用户手工加的非主节点成员 | **不影响** —— 我们只动 `old_node_id`，其它成员原样保留 | v1 已有 |
| 阶段 2 抛错（DB 写失败 / Redis 失败） | 只记日志，主节点切换仍返回 200 | v1 已有 |
| 跨用户转移节点 | `assign_node` 路径里已经把 `is_primary` 清掉了，不会触发主节点切换联动 | v1 已有 |
| 用户是管理员、给某个用户切了主节点 | 同样按 owner 维度迁移；多用户隔离不会被串 | v1 已有 |
| 分组当前已经 `enabled=False` | **仍然迁移成员**（成员关系和启用状态是独立的） | v1 已有 |
| `limit_watch_enabled=True` 且绑定的不是趋势策略 | 不强制打开 `limit_watch_report` | v1 已有 |
| 旧主节点触发了网格/趋势/市价三种类型 | 三种专属分组都会迁移 | v1 已有 |

---

## 6. 测试设计

### 6.1 单元测试（v3 移除 force 相关 case）

`tests/test_primary_migration.py`：

| 用例 | 输入 | 期望 |
|------|------|------|
| `test_blocking_running_tasks_reject` | owner=A，GroupTaskDispatch 有 status=running 的子任务 | 抛 `PrimarySwitchBlocked`，返回 blocking_tasks 明细 |
| `test_blocking_stuck_tasks_reject` | owner=A，GroupTaskDispatch 有 status=stop_failed 的子任务 | 同上 |
| `test_no_blocking_pass_transition` | owner=A，GroupTaskDispatch 只有 done/failed 跳过 | 正常切换，命中组返回 |
| `test_no_force_parameter_accepted` | 路由层调用 `set_primary(... force=True)` / URL `?force=true` | **抛 ValueError** / 404，**接口不接受 force** |
| `test_admin_no_force_bypass` | principal=admin，旧主节点有任务 | 仍然 409，**不**因 role 而 bypass |
| `test_cancel_primary_blocked` | owner=A，取消主节点且旧主节点有 running 子任务 | 抛 `PrimarySwitchBlocked` |
| `test_migrate_basic_replace` | owner=alice, 旧主=A, 新主=B, 一个 `trend_market-alice-10023-XAUUSD` 分组成员只有 A | A 被移除，B 被加入 |
| `test_migrate_new_already_member` | 同上但 B 已在成员里 | 仅删除 A |
| `test_migrate_legacy_group_skipped` | 有一个 `manual-group-alice` 名字不符合规则 | 不动 |
| `test_migrate_unset_primary` | 把当前主节点取消 | 命中分组里删除该成员 |
| `test_migrate_no_owner_groups` | owner=alice 没触发过交易页 | 命中为空，无副作用 |
| `test_migrate_limit_watch_report_default` | 趋势策略 + `limit_watch_enabled=True` | 新主节点的 `limit_watch_report=True` |
| `test_migrate_grid_unchanged` | 网格策略 | `limit_watch_report=False` |
| `test_migrate_error_isolated` | 阶段 2 内部抛错 | 主节点切换仍返回 200 |

### 6.2 集成测试

`tests/test_primary_switch_e2e.py`：

- **正常路径**：
  1. 通过 `httpx.AsyncClient` 调 `POST /api/nodes/{A}/primary {is_primary: True}`；
  2. `POST /api/trading/quick-trigger` 拿到 group_id；
  3. 让子任务进入终态（mock 节点回报 status=done）；
  4. `POST /api/nodes/{B}/primary {is_primary: True}`；
  5. 断言：`GET /api/groups` 返回的 group 成员里 **A 已不在 / B 已在**；审计表多了一行 `migrate_primary_member`。
- **阻塞路径**：
  1. 同上 1、2；
  2. **不**让子任务进入终态；
  3. `POST /api/nodes/{B}/primary` 期望返回 409 + `blocking_tasks: [{...running...}]`；
  4. 断言：`A` 的 `is_primary` 仍是 True（**没改**）；
  5. **额外断言**：尝试 `POST /api/nodes/{B}/primary?force=true`（admin token）依然 409，**不存在 force 旁路**。
- **(无强制路径)**：v3 不再写强制路径测试。

### 6.3 手工回归脚本

`scripts/smoke_primary_switch.py`：

1. 起 sqlite + 节点 A、B；
2. 注册 + 登录 → 设 A 为主 → 触发一次 trend_market；
3. 不收口任务，尝试 `hub.setNodePrimary(B)` → 期望 409 + blocking_tasks；
4. 强制调用 `POST /api/nodes/B/primary?force=true` → 期望 400（不接受参数）；
5. 让子任务进入终态，再次 `hub.setNodePrimary(B)` → 期望 200；分组列表断言 [B]。

---

## 7. 迁移 / 灰度

- 不需要数据迁移脚本 —— 历史分组的成员关系保持不动；
- 历史数据如果已经出现"两个旧主节点在同一个专属分组里"（v1 升级前留下的脏数据），阶段 2 会按规则清理一次；
- **灰度开关 `PRIMARY_BLOCKING_ENABLED`**（默认 False → 阶段 0 不生效，按 v1 行为跑；True → 启用 v2/v3 全量校验、强制力等同前者，不再有 force 路径）；
- 灰度顺序：
     1. 上线 `migrate_primary_groups`（v1）→ `PRIMARY_BLOCKING_ENABLED=False` 全量跑；
     2. 打开 `PRIMARY_BLOCKING_ENABLED=True` → 用户开始遇到 409，必须先收口再切；
     3. 观察 1~2 周 → 文档收敛。

---

## 8. 待确认 / 待评审项

1. **是否需要在路由层做轻量预查**（`GET /api/nodes/{old_primary}/active-tasks`）让前端在确认弹窗前就能展示阻塞？
   - **建议**：增加只读接口，前端在弹「确认设置为主节点？」之前先拉一次，命中阻塞就把按钮文案变成「旧主节点有 N 条在跑任务，无法切换」并禁用提交；非命中才进入原确认弹窗。
2. **阶段 0 是否要支持「等待收口」的轮询 / SSE 推送**？比如阻塞发生后前端提示「正在等待任务收口，X 秒后自动重试」—— 减小用户重复点按钮的成本。
   - **首版建议不做**（增加复杂度，且依赖节点 WS 状态）。保留扩展位即可。
3. **`limit_watch_report` 联动**保持 v1 方案（趋势策略 + 限价监听开启 + 未显式指定上报名单 → 默认打开）？
   - 保持不变。
4. **响应头 `X-Migrated-Groups` vs 新字段**？
   - 首版保持响应头（v1）。
5. **多端并发切换主节点**？
   - 已经在 `_locked_node` 行锁内解决，阶段 2 的 `migrate_primary_groups` 也对 owner 加 `SELECT ... FOR UPDATE` 行锁。
6. **(已 v3 移除)** ~~force / 强制切换路径~~：明确不做。

---

## 9. 落地步骤（评审通过后按顺序实施）

1. `backend/app/group_persist.py` / `node_service.py`：暴露 `SUBTASK_TERMINAL` 常量或专用查询函数 `active_dispatches_for_node(node_id)`（已有 `active_subtasks_for_node`，**复用即可**，v1 已存在）；
2. `backend/app/node_service.py`：新增 `precheck_active_blocking(...)` + 异常类 `PrimarySwitchBlocked`；
3. `backend/app/node_service.py#set_primary`：在 commit 之前插入阶段 0 调用；**不新增 force 参数**；事务 1 之后事务 2 调用保持 v1；
4. `backend/app/nodes.py#set_primary_node`：解析 409 → 抛出带明细的 HTTPException；**不解析、不接受 force query 参数**；
5. `backend/app/nodes.py`：新增 `GET /api/nodes/{node_id}/active-tasks` 给前端做轻量预查；
6. `backend/app/group_service.py`：实现 v1 的 `migrate_primary_member` / `migrate_primary_groups` / `is_quick_trigger_group`；
7. `backend/app/dashboard_audit.py`：新增 `OperationType.migrate_primary_member`；
8. 前端 `HomeView.vue#togglePrimary`：阻塞前轻量预查 / 409 弹窗 / **不提供强制入口**；
9. 前端 `GroupsView.vue`：监听主节点变化 + 弹 inline warning（可选）；
10. 单测 + 集成测试 + smoke 脚本；
11. 灰度开关 → 全量。

---

## 10. v1 → v2 → v3 变更要点（速览）

| 章节 | v1 | v2 | v3 |
|------|----|----|----|
| §3 流程 | 2 阶段 | 3 阶段 | 3 阶段（与 v2 同） |
| §4.1 触发点 | 阶段 2 在事务 1 后调用 | 阶段 0 在事务 1 commit 前调用 | 同 v2 |
| §4.5 force | 不存在 | 新增 `force=True` + `terminate_node_tasks` + 审计 | **整体移除**：唯一放行条件是"旧主节点无任何 in-flight 任务" |
| §4.7 取消主节点 | 阶段 2 仅删除成员 | 走阶段 0 + force 也适用 | 走阶段 0，**不接受 force** |
| §5 边界 | 旧主节点有任务 → "阶段 2 迁移成员" | 旧主节点有任务 → 拒绝切换（除非 force） | 旧主节点有任务 → **直接拒绝**，无任何绕路 |
| §7 灰度 | 无灰度开关 | `PRIMARY_BLOCKING_ENABLED` + `PRIMARY_FORCE_ENABLED` | 仅 `PRIMARY_BLOCKING_ENABLED` |
| §9 落地步骤 | v1 全部 | 新增 force 相关步骤 | 移除 force 相关步骤（`terminate_node_tasks`、`force_primary_switch` 审计、force query 解析） |