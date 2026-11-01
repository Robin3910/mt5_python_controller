# 【AI 交易】Tab 设计方案

> **一句话目标**：新增一个【AI 交易】Tab，把**同时持有 `ai_strategy` + `follow_leader` 两个能力**的客户
> 以「机器人」形态展示在机器人广场，让普通用户把自己的非主节点加入对方的 **AI 智能加仓策略（`tpl_1`）** 分组去跟单。
>
> **与【带单】Tab 的关系**：两个市场、两套模版白名单、**同一套关系表与分发链路**（见 §2.3）。
>
> **已确认的四条前置决策**：
>
> | # | 决策 | 含义 |
> |---|---|---|
> | 1 | **不控制手数** | 完全按带单员的手数执行，只做风险提示文案 |
> | 2 | **跟实时（不锁快照）** | 带单员改 AI 策略 → 正在跑的跟单任务**自动同步**（复用现有热推） |
> | 3 | **展示单位 = 主节点** | 一个「机器人」= 一位带单员的主节点；挂主节点所在、绑定 `tpl_1` 的分组 |
> | 4 | **两个能力都要** | `ai_strategy`（能配 AI 策略）**且** `follow_leader`（能对外接单）才进广场 |

---

## 1. 已核实的代码事实（设计前提）

### 1.1 三条与【带单】不同的硬差异

| # | 事实 | 证据 | 对设计的影响 |
|---|---|---|---|
| **D1** | **`tpl_1` 不支持信号并发** | `group_rules.supports_signal_concurrent` 只放行 `tpl_2`/`tpl_3` | AI 分组**强制单槽互斥**：跟单员节点上已有任务时，本次信号**直接跳过**（`busy`）。这是 AI 跟单最重要的体感差异，必须在 UI 明说 |
| **D2** | **`tpl_1` 有热推，`tpl_2`/`tpl_3` 没有** | `strategies._hot_push_running_snapshot` 对 `tpl_1` 调 `rewrite_running_strategy_snapshots` + `push_strategy_updates` | 决策 2「跟实时」的机制**已存在且免费**：带单员改 AI 策略 → 跟单员在跑任务被 `strategy_update` 实时改写。**不需要新代码** |
| **D3** | **`tpl_1` 是持续加仓型** | `max_allow_num` / `total_lot_limit` 是**批次累积额度** | 敞口随行情推移**单调增长**（逆势加仓）。决策 1「不控手数」下，跟单员风险完全跟随带单员 —— 文案必须写死这一点 |

### 1.2 D2 的完整链路（决策 2 的可行性证明）

```246:253:backend/app/group_persist.py
async def rewrite_running_strategy_snapshots(
    strategy_id: str, snapshot: dict,
) -> list[dict]:
    """重写该策略下非终态子任务所属主任务的规则快照，返回这些子任务。
    ...
    """
```

按 `strategy_id` 查该策略下**所有**非终态子任务（**不分 owner / 不分是否跟单**），
改写快照后逐个 WS 推 `strategy_update`。

> **含义**：`tpl_1` 的热推天然覆盖跟单员节点 —— 不需要为跟单关系做任何特殊处理。
> 反过来说，**这也意味着跟单员无法锁定风险参数**（决策 2 的必然结果，见 §7 风险）。

### 1.3 一个尚未实现、但 AI tab 会撞上的既有缺口

```304:312:backend/app/permissions.py
def event_visible(
    p: Principal, resource: Optional[tuple[str, str]], owner_user_id: object,
) -> bool:
    """该连接能否收到这条事件：管理员全收；普通用户只收自己名下资源的事件。"""
    if p.is_admin:
        return True
    if resource is None:
        return False
    return owns(p, owner_user_id)
```

跟单员**不是**分组 owner → 收不到自己节点的 `group_dispatch` / `strategy_progress` 事件。
这在带单 tab 已存在（设计稿 §4.4 提出、未实现），**AI tab 同样会撞上**。

**处理决策**：本期**不实现**事件可见性扩展（§9 二期），
改为「AI tab 的实时性由**轮询**提供，不依赖 WS 事件」—— 见 §6.4。

### 1.4 能力体系已就绪，无需新建

| 项 | 现状 |
|---|---|
| `CAP_AI_STRATEGY = "ai_strategy"` | 已注册（`permissions.py:73`） |
| `CAP_FOLLOW_LEADER = "follow_leader"` | 已注册（`permissions.py:76`） |
| `can_capability(p, code)` | 现成判定，管理员自动放行 |
| `Principal.capabilities` | `frozenset[str]`，已是能力并集 |

> **结论**：决策 4 的「两个能力都要」= `CAP_AI_STRATEGY in p.capabilities and CAP_FOLLOW_LEADER in p.capabilities`，
> **零新增注册**。这是本方案最省力的一点。

### 1.5 复用可行性核对

| 需复用的东西 | 现状 | 改动量 |
|---|---|---|
| `group_follower_node` 关系表 | 已存在，字段完备（`leader_user_id` / `follower_user_id` / `follower_primary_node_id` 快照） | **零** —— 表结构无需新增列 |
| `follow_service.add_following` / `remove_following` | 通用读写，不含模版假设 | **零** |
| `follow_api` 的 join / leave 接口 | 判定走 `followable_groups` | 加 `market` 参数即可（见 §3.2） |
| `group_dispatcher._effective_with_follows` | 带单员能力 + 市场准入 | 加市场参数即可（见 §3.3） |
| `OrderDialog` 跟单节点继承（`adopt-followers`） | 跨模版继承，**不带模版过滤** | ⚠️ 必须加模版约束，见 §5.2 |

> ⚠️ **`adopt-followers` 是一个真实的交叉污染点**（详见 §5.2）：
> 它会把带单员**其它模版分组**里的跟单节点继承到新 AI 分组。若不处理，
> 跟单员跟的网格分组会被自动拉进他的 AI 分组 —— **违反决策 3 的隔离**。

---

## 2. 总体设计

### 2.1 页面定位

```
┌─ AI 交易 ────────────────────────────────────────────────┐
│ 跟随 AI 智能加仓机器人的加仓节奏。加仓参数由机器人主人掌握，│
│ 你的节点会用**自己的资金**执行他的加仓计划。               │
├──────────────────────────────────────────────────────────┤
│  左：机器人广场（列表）        右：详情 / 我的 AI 跟单     │
└──────────────────────────────────────────────────────────┘
```

### 2.2 与【带单】Tab 的对称关系

| 维度 | 【带单】Tab | 【AI 交易】Tab |
|---|---|---|
| 菜单码 | `follow` | `ai_trade`（新增） |
| 路径 | `/follow` | `/ai-trade` |
| 市场常量 | `FOLLOW_MARKET_TEMPLATE_IDS` | `AI_MARKET_TEMPLATE_IDS`（已预留） |
| 白名单 | `tpl_2` + `tpl_3` | `tpl_1` |
| 机器人准入 | `follow_leader` | `ai_strategy` **且** `follow_leader` |
| 关系表 | `group_follower_node` | **同一张** |
| 分发链路 | `group_dispatcher` | **同一条**（仅市场常量不同） |
| 信号并发 | 支持 | **不支持**（D1，单槽互斥） |
| 策略热推 | 无 | **有**（D2，跟实时） |
| 跟单员控参 | 无 | 无（决策 1） |

### 2.3 为什么不新建一套关系表 / 分发链路

AI 跟单与普通跟单在数据层面**同构**：

```
group_follower_node{group_id, node_id, follower_user_id, leader_user_id, ...}
```

`leader_user_id` 已足够区分两个市场（同一 leader 的 `tpl_1` 分组走 AI 市场，
`tpl_2`/`tpl_3` 分组走带单市场）。**唯一需要区分的是「准入判定时的模版白名单」**，
而这正是上一份《带单市场过滤》方案里已经参数化的 `market` 参数。

> **因此 AI tab 的后端工作量比表面看起来小得多**：
> 新增一个菜单码 + 一个 `market` 常量 + 5 个接口复用 + 1 处分发兜底。

---

## 3. 后端设计

### 3.1 菜单与能力（纯注册，零副作用）

**文件**：`backend/app/permissions.py`

```python
MENU_AI_TRADE = "ai_trade"      # 紧跟 MENU_FOLLOW
```

```python
MENU_REGISTRY = (
    ...
    MenuDef(MENU_FOLLOW, "带单", "/follow", True),
    MenuDef(MENU_AI_TRADE, "AI 交易", "/ai-trade", True),   # 新增
    MenuDef(MENU_DASHBOARD, "总览", "/", True),
    ...
)
```

> **`assignable=True`** —— 跟单是默认人人可做，机器人广场对所有登录用户开放。
> 管理员通过**角色菜单**决定谁能看到这个 tab（如只给「付费客户」角色）。

**新增判定函数**：

```python
def can_host_ai_robot(p: Principal) -> bool:
    """能否作为【AI 交易】广场的机器人主人：需同时持有 ai_strategy 与 follow_leader。

    两个能力都要的语义：能配 AI 策略（ai_strategy）+ 愿意对外接单（follow_leader）。
    管理员直接放行（can_capability 内已含 is_admin 分支）。
    """
    return (can_capability(p, CAP_AI_STRATEGY)
            and can_capability(p, CAP_FOLLOW_LEADER))
```

**文件**：`backend/app/db.py` —— **无需迁移**：
`MENU_AI_TRADE` 因 `assignable=True` 自动进 `DEFAULT_USER_MENUS`，
且 `rbac_service.seed_rbac` 已有**幂等补齐**逻辑（存量 `user` 角色会自动补上）。

**验证**：

```bash
python -c "from backend.app import permissions; print(permissions.MENU_AI_TRADE)"
python -c "from backend.app import permissions as p; print(p.can_host_ai_robot(p.Principal(1,'u',True,frozenset(),frozenset())))"
```

**回滚**：删除常量 + MenuDef 条目 + 判定函数

---

### 3.2 市场判定：复用 `market` 参数

**前提**：上一份《带单市场过滤》方案（`2026-10-21-follow-market-filter-plan.md`）已把
`followable_groups` 改造为带 `market` + `strategy_templates_map` 参数。

> **⚠️ 依赖说明**：若那份方案尚未实施，本节需先落地它 —— AI tab 的市场判定直接建立在该参数化之上。
> 若决定不实施那份方案，替代方案见 §9.1。

**新增常量**（若上一份方案已加，可直接复用）：

```python
# follow_rules.py
AI_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_1_ID,   # tpl_1 AI 智能加仓策略
})
```

**新增纯函数**：

```python
def ai_host_groups(
    groups: list[dict],
    leader_user_id: int,
    primary_node_id: Optional[str],
    strategy_templates_map: dict[str, str],
) -> list[dict]:
    """该用户可对外开放的 AI 机器人分组：含其主节点 + 绑定 tpl_1 + 启用。"""
    return followable_groups(
        groups, leader_user_id, primary_node_id,
        market=AI_MARKET_TEMPLATE_IDS,
        strategy_templates_map=strategy_templates_map,
    )
```

### 3.3 新的 API 路由（`/api/ai-follow`）

> **不复用 `/api/follow/*` 的路由** —— 独立前缀的理由：
> ① 审计 `category` 可区分（`follow` vs `ai_follow`）；
> ② 未来 AI 跟单若要独立限流 / 独立统计，切开更容易；
> ③ 两个 tab 的 `unselectable_reason` 文案完全不同。
> **但内部完全复用 `follow_service` 的读写与 `follow_rules` 的判定。**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai-follow/robots` | 机器人广场列表 |
| GET | `/api/ai-follow/robots/{leader_user_id}` | 机器人详情（可跟分组 + 我的可跟节点） |
| GET | `/api/ai-follow/me` | 我的 AI 跟单 |
| POST | `/api/ai-follow/join` | 加入 AI 跟单 |
| POST | `/api/ai-follow/leave` | 移出 AI 跟单（复用 `_stop_following_subtasks`） |

**3.3.1 机器人列表**

```python
@router.get("/robots", response_model=AiRobotListResponse)
async def list_ai_robots(
    q: str = "",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiRobotListResponse:
    """AI 机器人广场：持 ai_strategy + follow_leader + 有主节点 + 至少一个 tpl_1 分组。

    机器人卡片需要展示「加仓强度」这类可信度指标 —— 本版沿用带单页的做法，
    统计字段先返回 0 占位，后续由历史订单数据驱动（与 FollowView 的 mock 同源）。
    """
    users = await user_service.list_users()
    # 批量预取能力：避免循环里逐个 load_principal
    host_ids = await _ai_host_ids_with_capability(store, users)

    all_groups = await store.all_groups()
    strategies = await follow_api._strategies_of(all_groups, store)
    tmpl_map = follow_api._template_ids_of(strategies)

    all_nodes = await store.all_nodes()
    ...
    for uid in host_ids:
        primary = follow_api._primary_of(all_nodes, uid)
        if not primary:
            continue
        groups = follow_rules.ai_host_groups(all_groups, uid, primary["node_id"], tmpl_map)
        if not groups:
            continue          # 只有 tpl_2/tpl_3 分组 -> 不是 AI 机器人
        out.append({... "ai_group_count": len(groups), ...})
```

> **注意**：机器人主节点的 AI 策略分组若**全部被禁用 / 全是 tpl_2**，
> 该用户**不出现**在广场 —— 与带单 tab「至少一个可跟分组」的口径一致。

**3.3.2 机器人详情**

与 `follow_api.get_leader_detail` 同构，差异：

```python
detail_groups.append({
    ...
    # AI 特有字段
    "risk_amount": _first_rule_value(strategy, "risk_amount"),       # 风险金额
    "max_allow_num": _first_rule_value(strategy, "max_allow_num"),   # 最大加仓次数
    "total_lot_limit": _first_rule_value(strategy, "total_lot_limit"),  # 总手数上限
    "entry_mode": _first_rule_value(strategy, "entry_mode"),         # 市价/限价
    # AI 分组强制单槽：并发能力恒为 False，前端据此提示
    "signal_concurrent_enabled": False,
    "concurrent_unavailable_reason": "AI 智能加仓策略不支持信号并发，同一节点同时只跑一单",
})
```

> **`risk_amount` / `total_lot_limit` 是必须公开的**（决策 1「不控手数」的对价）：
> 跟单员不控手数，就必须能**事先看清**带单员的风险参数，否则是盲跟。
> 这两个字段来自策略的 rules 快照，是**带单员自己的配置**，公开它不泄露节点/账户信息。

**3.3.3 加入 / 移出**

```python
@router.post("/join", response_model=FollowJoinResponse)
async def join_ai_follow(body: FollowJoinRequest, request: Request, ...):
    """把当前用户的非主节点加入某机器人的 tpl_1 分组。

    与 follow_api.join_follow 的唯一差异：market 传 AI_MARKET_TEMPLATE_IDS。
    校验顺序、幂等语义、审计结构完全一致。
    """
    # ... 校验 leader 有 ai_strategy + follow_leader ...
    followable = {
        g["group_id"]: g for g in follow_rules.ai_host_groups(
            all_groups, body.leader_user_id, leader_primary["node_id"], tmpl_map,
        )
    }
```

**移出**：`leave` 直接复用 `follow_api._stop_following_subtasks`（终止在跑子任务的唯一入口）。

---

### 3.4 分发层：AI 市场准入（兜底）

**文件**：`backend/app/group_dispatcher.py`

与上一份方案一致，`_effective_with_follows` 的市场准入参数化后，
AI 分组走 `AI_MARKET_TEMPLATE_IDS` 白名单判定：

```python
# 语义：带单 tab 的跟单节点只在 FOLLOW_MARKET 内生效；
#       AI tab 的跟单节点只在 AI_MARKET 内生效。
#       同一分组只绑定一个策略、一个模版，因此市场判定是**唯一且互斥**的。
def _market_for_template(template_id: object | None) -> frozenset[str]:
    """按分组绑定的模版决定走哪个跟单市场（两个市场互斥，不重叠）。"""
    tid = str(template_id or "").strip().lower()
    if tid == strategy_templates.TEMPLATE_1_ID:
        return follow_rules.AI_MARKET_TEMPLATE_IDS
    if tid in (strategy_templates.TEMPLATE_2_ID, strategy_templates.TEMPLATE_3_ID):
        return follow_rules.FOLLOW_MARKET_TEMPLATE_IDS
    return frozenset()          # 未知模版：两个市场都不收（保守）
```

> **互斥性保证**：两个白名单不相交，`tpl_1` 不在带单市场内、`tpl_2`/`tpl_3` 不在 AI 场内。
> 所以「一份分组只会被一个市场接纳」，**不需要额外的归属判断**。

---

## 4. 分组可见性开关（一期必做，不可省）

> **这是本方案与带单 tab 最大的运维差异**。
> 带单 tab 是「市价/限价/网格」—— 暴露一个闲置分组影响有限。
> 但 **AI 加仓策略是持续加仓**：暴露一个参数激进的老分组 =
> 把跟单员的钱放进一个可能持续加仓到爆仓的计划里。**代价远高于带单 tab。**

```python
# node_group 新增列（字段位置：与 trend_risk_enabled 同组）
accept_ai_follow: Mapped[bool] = mapped_column(Boolean, default=False)
```

- 默认 **False**（不主动开放，避免历史分组被误暴露）；
- 机器人主人需**显式勾选**「本分组开放 AI 跟单」。

**迁移照抄 `trend_risk_enabled` 的现成范式**（已核实，`db.py:115-125`）：

```python
def _migrate_node_group_accept_ai_follow(sync_conn) -> None:
    """node_group.accept_ai_follow：是否开放 AI 跟单（默认关闭）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    if "accept_ai_follow" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(text(f"ALTER TABLE node_group ADD COLUMN accept_ai_follow {col_type}"))
```

并在 `db.init_schema` 的 `run_sync(...)` 链（`db.py:359-377`）末尾追加一行。

**⚠️ 必须同步分组缓存 dict**（易漏点）：

```python
# group_service.group_row_to_dict 的返回 dict 里追加一行（与 trend_risk_enabled 并列）
"accept_ai_follow": bool(getattr(row, "accept_ai_follow", False)),
```

> **为什么必须改**：`follow_rules` / AI 广场判定读的是 **Redis 里的分组缓存 dict**，
> 不是 ORM 行。漏了这一处 → 字段永远是 `False` → 机器人主人的分组永远不进广场，
> 且**不报错**（`getattr` 有默认值兜底，症状是「功能就是不生效」）。
> 参考 `signal_concurrent_enabled` / `trend_risk_enabled` / `limit_watch_enabled`
> 三个同族字段，它们在 `group_row_to_dict` 里都有对应行。

**判定串联**（AI 机器人准入的**第 5 个条件**）：

```
AI 机器人分组 = 含主节点 ∧ enabled ∧ 绑定 tpl_1 ∧ 策略启用 ∧ accept_ai_follow = TRUE
```

> 带单 tab 暂不做此开关（沿用现状），AI tab 必做 —— 见 §7 风险表。

---

## 5. 两个必须处理的交叉污染点

### 5.1 事件可见性（本期不改，改用轮询）

见 §1.3：`event_visible` 不认跟单员。**处理方式**：

- AI tab 的「我的 AI 跟单」**轮询** `GET /api/ai-follow/me`（30~60s）；
- 不依赖 WS 的 `group_dispatch` / `strategy_progress`；
- **代价**：跟单员看不到实时逐笔加仓推送。**一期可接受**（带单 tab 同样如此）。

### 5.2 ⚠️ `adopt-followers` 会跨市场污染（**必须修**）

**问题**：`follow-api` 的 `adopt-followers`（跟单节点继承）会扫描带单员名下**所有**带单源分组，
把已有跟单节点搬进新分组。**它不区分模版** —— 后果：

> 机器人主人 A 既开带单（网格分组 G_grid，跟单员 B 已加入）又开 AI 分组 G_ai。
> A 触发 G_ai 时触发继承 → **B 的节点被自动拉进 G_ai**，B 就此在毫不知情的情况下
> 开始用自己的钱执行 A 的 AI 加仓计划。**这违反决策 3，且是静默的。**

**修复**：`adopt_followers` 的来源筛选**按调用方的市场**收窄。

```python
# groups.py: adopt_followers 路由
# 原来：follow_rules.followable_groups(all_groups, owner, primary)   # 不分模版

# 改为：按目标分组的模版决定继承哪个市场的跟单节点
target_tid = tmpl_map.get(str(target.get("strategy_id") or ""), "")
market = follow_rules.market_for_template(target_tid)   # 复用 §3.4 的函数
sources = [
    g for g in all_groups
    if str(g.get("group_id")) != group_id
    and follow_rules.is_leader_source_group(g, owner, primary)
    and g.get("enabled", True)                                  # 保留原语义
    and follow_rules.template_in_market(
        tmpl_map.get(str(g.get("strategy_id") or "")), market,
    )
]
```

> **附带修复了另一个同源 bug**：原实现的来源筛选忽略了 `enabled`（继承计划文档「决策 1」故意为之），
> 但**跨市场继承**必须收窄。两处语义要分开：
> - 同市场继承（网格 → 网格）：保持现有「忽略 enabled」语义，不破坏已上线行为；
> - 跨市场（带单 ↔ AI）：**不允许**，由 `market` 收窄天然排除。

### 5.3 主节点切换 / 节点生命周期

**完全复用**带单 tab 已有的清理链路（`node_service.set_primary` → `remove_node_followings`、
`users_api.assign_nodes` / `node_service.delete_node` → `remove_node_followings`）。
AI 关系存在同一张表里，**这些清理天然覆盖 AI 关系，无需额外代码**。

> **验证点**：`remove_node_followings` 按 `node_id` 全表删，不分市场 —— 正确（一个节点不能既是带单跟单又是 AI 跟单而互不知情）。

> ⚠️ **2026-10-24 更新**：`set_primary` 的清理路径已被阶段 0 的**硬拦截**前置 ——
> 节点仍在跟单时返回 409（`code=primary_switch_following_blocked`），不再静默清掉。
> `remove_node_followings` 退化为兜底（正常路径走不到）。AI 关系同表同规则，
> 跟单与 AI 跟单都会被这一道拦截挡住。

---

## 6. 前端设计

### 6.1 路由与导航

**`frontend/src/constants/menus.ts`**：

```typescript
{ code: 'follow', path: '/follow' },
{ code: 'ai_trade', path: '/ai-trade' },      // 新增，紧跟带单
```

**`frontend/src/router/index.ts`**：

```typescript
{
  path: '/ai-trade',
  name: 'AiTrade',
  component: () => import('@/views/AiTradeView.vue'),
  meta: { menu: 'ai_trade', title: 'AI 交易', requiresAuth: true },
},
```

**`frontend/src/App.vue`**：导航项 `{ path: '/ai-trade', label: 'AI 交易', icon: '🤖' }`（紧随「带单」）。

### 6.2 `AiTradeView.vue` —— 机器人广场

结构与 `FollowView.vue` 同构（左列表 + 右详情），差异在卡片内容：

```
┌ AI 交易 ─────────────────────────────────────────────────┐
│ ⚠ AI 加仓策略会持续累积仓位，风险随行情推移增长。          │
│   你的节点将用自己的资金执行机器人的加仓计划。              │
├───────────────────────────────────────────────────────────┤
│ ┌ 机器人广场 ──────────────────────────────────────────┐  │
│ │ ● Alice   主节点：主节点A（MT5 10023 · 在线）         │  │
│ │   AI 分组 2 个 · 被 5 个节点跟随 · 加仓中 2 个         │  │
│ │   风险额度：3 批次 / 总手数上限 0.5                   │  │
│ └──────────────────────────────────────────────────────┘  │
│ ┌ Bob     主节点：主节点X（MT5 10025 · 离线）            │  │
│ │   AI 分组 1 个 · 尚无人跟随                            │  │
│ └──────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

**卡片字段**（`/api/ai-follow/robots` 返回）：

```typescript
export interface AiRobotSummary {
  leader_user_id: number
  username: string
  display_name: string | null
  primary_node: FollowNodeBrief | null
  /** 可开放的 tpl_1 分组数（含 accept_ai_follow 开关） */
  ai_group_count: number
  follower_node_count: number
  my_follow_group_count: number
  my_follow_node_count: number
  /** 机器人对外公开的风险上限（取各分组最大值，让用户一眼看到风险量级） */
  max_allow_num: number | null
  total_lot_limit: number | null
  marketable: boolean
}
```

> **`max_allow_num` / `total_lot_limit` 在列表卡片就展示**（决策 1 的对价）：
> 用户在选机器人时第一眼就要能判断风险量级，不能点进详情才知道。

### 6.3 跟单弹窗（双区选择）

```
┌ 跟单 · Alice ─────────────────────────────────┐
│ 机器人：主节点A（MT5 10023 · 在线）             │
├ 可开放的 AI 分组（可多选）───────────────────┤
│ ☑ ai-Alice-10023-XAUUSD                        │
│    XAUUSD · AI智能加仓策略 · 单槽执行 · 已触发  │
│    56 次                                           │
│    批次上限 3 · 总手数上限 0.5 · 手数不缩放     │
│ ☐ ai-Alice-10023-EURUSD                         │
│    ⚠ 本分组未开放 AI 跟单（机器人主人未开启）   │
├ 我的节点（可多选）─────────────────────────────┤
│ ☑ 节点B（MT5 20001 · 在线）                     │
│ ☑ 节点C（MT5 20002 · 离线）                     │
│ ⊘ 主节点X（MT5 20000）  主节点不可跟单          │
├─────────────────────────────────────────────────┤
│ 已选：2 个节点 × 1 个分组                        │
│ ⚠ 加入后将按机器人的加仓参数执行，手数不缩放；   │
│   机器人主人修改策略会实时同步到你的在跑任务；   │
│   移出不会自动平仓。                             │
│                          [取消]      [确认跟单] │
└─────────────────────────────────────────────────┘
```

**两条 AI 特有的风险提示（必须写进确认框，`FollowView` 无对应文案）**：

1. `手数不缩放` —— 你将按机器人设定的完整手数执行；
2. `策略实时同步` —— 机器人主人修改加仓参数会**立即作用到你正在跑的任务**，你无法锁定。

### 6.4 我的 AI 跟单

结构与带单 tab 的「我的跟单」一致（按机器人聚合 → 分组 → 节点 → 移出），
外加**轮询**：

```typescript
// 每 45s 轮询一次（页面可见时才轮询）
let timer: number | undefined
onMounted(() => {
  void loadRobots(); void loadMine()
  timer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void loadMine()
  }, 45_000)
})
onUnmounted(() => window.clearInterval(timer))
```

**分组行额外展示**：

```
ai-Alice-10023-XAUUSD
  批次上限 3 · 总手数上限 0.5      当前在跑 1 单（3/3 批次已用）
  节点B（20001）  进行中 1 个任务    [移出]
```

> **`批次已用 / 批次上限`** 是 AI 跟单员最需要盯的指标 —— 它决定还剩多少加仓空间（风险敞口）。

### 6.5 类型与 API 层

**`frontend/src/api/types.ts`**：新增 `AiRobotSummary` / `AiRobotDetailResponse` / `AiMeResponse`
（`FollowGroupDetail` 上加 §3.3.2 的 AI 字段）。

**`frontend/src/api/aiFollow.ts`**：`fetchAiRobots` / `fetchAiRobotDetail` /
`fetchMyAiFollowings` / `joinAiFollow` / `leaveAiFollow`（形状与 `follow.ts` 完全一致）。

---

## 7. 风险表（AI 加仓特有）

| 风险 | 等级 | 处理 |
|---|---|---|
| **带单员改参数立即影响在跑的跟单任务**（D2） | **高** | 决策 2 已确认接受。文案写死 + `strategy_update` 是既有机制。**不做技术拦截** |
| **手数不缩放 → 跟单员用自己钱扛带单员的激进加仓** | **高** | 决策 1 已确认接受。对价：列表卡片 + 详情 + 确认框**三处**强制展示 `max_allow_num` / `total_lot_limit` |
| **AI 分组单槽互斥，跟单员频繁被 busy 跳过**（D1） | 中 | 详情页每行显示「单槽执行」tag + 说明；不是 bug，是 AI 策略的固有特性 |
| **闲置 / 实验性 AI 分组被误暴露** | **高** | §4 的 `accept_ai_follow` 开关，**默认关闭、显式开启** |
| 跟单员收不到实时 WS 事件 | 中 | §5.1 改轮询（45s）；二期再考虑事件可见性扩展 |
| `adopt-followers` 跨市场静默拉人（D2 交叉污染） | **高** | §5.2 必修：来源按目标分组的 market 收窄 |
| 跟单员节点离线 → 错过加仓批次 | 中 | 与带单 tab 一致：本次跳过，下次轮转；`poll` 模式自动跳下一个节点 |
| 两个市场用同一张关系表 → 数据混淆 | 低 | `leader_user_id` + 分组模版唯一定位市场；分组只绑一个模版，**天然互斥** |

---

## 8. 实施步骤

> 后端先行，每步可独立验证。

### 阶段 A：注册（纯增量，零副作用）

| 步 | 文件 | 内容 |
|---|---|---|
| A1 | `permissions.py` | `MENU_AI_TRADE` + MenuDef + `can_host_ai_robot()` |
| A2 | `frontend/constants/menus.ts`、`router/index.ts`、`App.vue` | 菜单路径 / 路由 / 导航项 |
| A3 | `orm.py` + `db.py` | `NodeGroup.accept_ai_follow` 列 + 幂等迁移 |

**A3 验证**：`SELECT accept_ai_follow FROM node_group LIMIT 1` → 0
**回滚**：删列 / 删常量（前端菜单项可留空页）

### 阶段 B：市场参数化（**依赖上一份方案**）

| 步 | 文件 | 内容 |
|---|---|---|
| B1 | `follow_rules.py` | `AI_MARKET_TEMPLATE_IDS` + `market_for_template()` + `template_in_market()` |
| B2 | `follow_api.py` | `followable_groups` 加 `market` 参数 + 三处调用点接入 |
| B3 | `group_dispatcher.py` | `_effective_with_follows` 市场准入兜底 |

**B3 验证**（关键）：
```
1. tpl_1 分组 + 跟单节点 → 触发 → 跟单节点**不**产生子任务（带单 tab 视角）
2. tpl_2 分组 + 跟单节点 → 触发 → 跟单节点正常产生子任务（回归）
```

**回滚**：撤销 B1~B3

### 阶段 C：AI tab 后端

| 步 | 文件 | 内容 |
|---|---|---|
| C1 | `models.py` | `AiRobotSummary` / `AiRobotDetailResponse` / `AiMeResponse` |
| C2 | `follow_api.py` | 抽出 `_strategies_of` 复用 + `_template_ids_of` + `_primary_of` 复用 |
| C3 | **`ai_follow_api.py`（新建）** | 5 个路由，全部复用 `follow_service` / `follow_rules` |
| C4 | `main.py` | 注册 `ai_follow_api.router` |
| C5 | `groups.py` | **修 `adopt-followers` 跨市场污染**（§5.2） |

**C3 验证**：
```bash
curl http://<host>/api/ai-follow/robots -H "Authorization: Bearer <token>"
# 只含 ai_strategy + follow_leader 都有、且有 tpl_1 分组且已开放跟单的用户
```

### 阶段 D：前端

| 步 | 文件 | 内容 |
|---|---|---|
| D1 | `api/types.ts` | AI 类型 |
| D2 | `api/aiFollow.ts` | 5 个请求函数 |
| D3 | `views/AiTradeView.vue` | 广场 + 详情 + 我的跟单（复制 `FollowView` 结构后改差异点） |
| D4 | `GroupsView.vue` | 分组编辑弹窗加 `accept_ai_follow` 开关（仅 `tpl_1` 分组显示） |

**D4 验证**：机器人主人勾选后，该分组出现在 AI 广场；取消勾选即消失

### 阶段 E：测试

| 用例 | 期望 |
|---|---|
| 只有 `ai_strategy` 无 `follow_leader` | 不出现在广场 |
| 只有 `follow_leader` 无 `ai_strategy` | 不出现在广场 |
| 两能力都有，但无 `tpl_1` 分组 | 不出现在广场 |
| 两能力都有，`tpl_1` 分组但 `accept_ai_follow=false` | 不出现在广场 |
| `tpl_1` 分组 + `accept_ai_follow=true` | 出现在广场 |
| AI 分组加入跟单 | 200；`tpl_2` 分组加入 AI 跟单 → 400（市场互斥） |
| 带单 tab 加入 `tpl_1` 分组 | 400（带单市场过滤生效） |
| `adopt-followers`：AI 分组不继承网格分组的跟单节点 | ✅ 跨市场隔离生效 |
| `adopt-followers`：AI 分组继承另一 AI 分组的跟单节点 | ✅ 同市场仍继承（不破坏既有行为） |
| `tpl_1` 热推 | 跟单员在跑任务被 `strategy_update` 改写（决策 2 的核心验证） |
| 带单节点设为 AI 跟单节点 | `remove_node_followings` 清掉关系（不因市场不同而漏清） |
| 回归：`test_group_dispatch_follow.py` | 全绿 |

---

## 9. 待确认与二期

### 9.1 ⚠️ 依赖上一份方案

本方案的市场判定**建立在**《带单市场过滤》方案的 `market` 参数化之上。
若那份方案不实施，替代路径：

- 在 `followable_groups` 内部**硬编码** `tpl_1` 排除 + 新增 `ai_only_groups()`；
- 代价：AI tab 与带单 tab 的判定逻辑**各写一份**，将来易分裂（两份都漏改的风险）。

> **建议**：先落地带单市场过滤（那份方案步骤 1~3），再实施 AI tab。

### 9.2 二期候选

| 项 | 说明 |
|---|---|
| 事件可见性扩展 | `resolve_owner` → `resolve_access`（带 followers），让跟单员收到自己节点事件，替代轮询 |
| 跟单员手数缩放 | 决策 1 的反面：若客户投诉风险不可控，加 `group_follower_node.lot_scale` |
| AI 跟单统计 | 复用带单页的收益 / 胜率 / 跟单人数（当前 mock） |
| 分组级 AI 跟单上限 | 限制单个机器人能容纳的跟单节点数（防过度集中） |
| 灰度开关 | `system_settings.ai_follow_feature`，与带单的 `follow_feature` 同范式 |

### 9.3 需你再确认的两点

1. **`accept_ai_follow` 默认值**：本方案定 **False（默认不开放）**。
   若你希望「授权了 AI 策略能力的客户，其 `tpl_1` 分组自动进广场」，则默认 True —— 但需接受历史实验分组被暴露的风险。
2. **机器人主人失去能力后的处理**：本方案沿用带单语义 —— **立刻下架 + 关系保留 + 在跑任务跑完**。
   是否需要对 AI 场景更硬（如直接终止在跑任务）？