# 带单（跟单）功能设计方案

> 本文档只设计、不写代码。与 `2026-10-19-primary-switch-group-migration.md` 同规格：先把数据归属、硬约束、失败路径定死，再谈实现。
>
> **v3 修订（当前版本）**：
> - **v2 修正展示口径** —— 带单界面里出现的是**带单员及其主节点**（带单源），不是分组列表本身。用户点「跟单」后，选中自己的非主节点，加入**该主节点所在的那些分组**。
> - **v3 把「带单权限」从用户列改为角色能力** —— 完全对齐已有的 AI 智能加仓策略（`ai_strategy`）模式：管理员在【用户权限 → 角色】里勾选 `follow_leader` 能力，授予角色即生效。**不再有 `users.follow_enabled` 列，也不再有 `PATCH /api/users/{id}/follow` 接口。**
>
> **一句话目标**：持有 `follow_leader` 能力的用户在【交易】Tab 用主节点触发信号并打开「带单」开关时，信号下发到其主节点所在分组的**全部有效成员**（含跟单方加入的非主节点）。

---

## 1. 业务语义

### 1.1 核心定义

| 概念 | 定义 |
|---|---|
| **带单员（Leader）** | 持有 `follow_leader` **角色能力**的用户。**其主节点就是带单源**。 |
| **带单源** | 带单员名下的**主节点**（`nodes.is_primary = true`，每 owner 至多一个）。交易 Tab 的触发入口就是主节点，因此带单源只能是主节点。 |
| **可跟单分组** | 带单员名下、**成员中包含其主节点**、且当前真正能接收信号的分组。 |
| **跟单员（Follower）** | 把自己的**非主节点**加入别人可跟单分组的用户。**默认所有客户都可以跟单，无需任何权限。** |

**判定公式**（后端唯一口径，`follow_rules.py` 纯函数实现）：

```
可跟单分组(L) = { g ∈ node_group |
      normalize(g.owner_user_id) == L.id          # 带单员名下
   ∧ g.enabled = TRUE                              # 分组启用
   ∧ L.primary_node_id ∈ member_ids(g)            # 【核心】带单源必须是该主节点
   ∧ g.strategy_id 绑定且 strategy.enabled = TRUE   # 否则不接收信号，展示也没意义
}

可展示带单员 = { u ∈ users |
      u 持有 follow_leader 能力                     # 角色能力（能力并集）
   ∧ u.is_active = TRUE
   ∧ u 存在主节点                                 # 没有主节点就没有带单源
   ∧ 可跟单分组(u) ≠ ∅                            # 至少一个真正会响应的分组
   ∧ u ≠ 当前登录用户                             # 自己不跟自己
}
```

### 1.2 角色动作矩阵

| 角色 | 动作 | 结果 |
|---|---|---|
| **管理员** | 在【用户权限 → 角色】勾选 `follow_leader` | 持有该角色的用户，其主节点 + 主节点所在分组出现在所有客户的【带单】Tab |
| **带单员** | 交易 Tab 触发信号，「带单」= **开** | 信号下发到可跟单分组的**全部有效成员**（自有 + 跟单） |
| **带单员** | 交易 Tab 触发信号，「带单」= **关** | 信号**只下发给自己的主节点**，跟单成员完全不参与 |
| **跟单员** | 点带单员的「跟单」→ 选自己的非主节点 + 选分组 | 节点成为该分组的跟单成员，此后按对方策略执行 |
| **跟单员** | 在「我的跟单」里移出 | 该节点不再接收该分组新信号；是否平仓在跑任务由用户在弹窗里选 |
| **任何人** | 把主节点拿去跟单 | **禁止** |
| **任何人** | 跟自己的单 | 不展示（`可展示带单员` 已排除自己） |

### 1.3 五条硬规则（不可妥协）

1. **主节点不能跟单**。加入时校验 `nodes.is_primary`；跟单期间被设为主节点的节点自动移出跟单关系。
2. **跟单成员是只读关系**。分组所有者不能通过「编辑分组成员」增删跟单成员；跟单成员只能由跟单员自己（或管理员代操作）解除。
3. **跟单成员永不成为限价监听触发源**。`limit_watch_report` 对跟单成员恒为 `False` —— 否则跟单者在自己终端随手挂一张限价单，就会触发带单员的分组开仓。
4. **只有包含带单员主节点的分组才是带单源分组**。带单员名下可能有手工创建、不含主节点的分组，它们**不进入跟单市场**。
5. **能力撤销立即生效，但不强平**。带单员失去 `follow_leader` 能力后，其分组立刻从跟单市场下架，跟单节点不再接收新信号；**已在跑的任务继续跑到自然结束**。

---

## 2. 现状与影响面（已核实的代码事实）

### 2.1 能力（capability）体系 —— 本次直接复用，无需新建

项目已有完整的**菜单 + 能力**双轨授权，AI 智能加仓策略（`ai_strategy`）就是现成范例：

| 环节 | 实现 | 位置 |
|---|---|---|
| 能力注册表 | `CAPABILITY_REGISTRY: tuple[tuple[str, str]]` | `permissions.py` |
| 能力码常量 | `CAP_AI_STRATEGY = "ai_strategy"` | `permissions.py` |
| 判定 | `can_capability(p, code)` | `permissions.py` |
| 角色存储 | `sys_role.capabilities`（TEXT，JSON 数组字符串） | `orm.Role` |
| 解码 | `_decode_capabilities`（脏数据当空数组，不让鉴权整体失败） | `rbac_service.py` |
| 组装 Principal | `_principal_data` 里 `cap_rows` 查询 → `resolve_capabilities` | `rbac_service.py` |
| `Principal` 字段 | `capabilities: frozenset[str]` | `permissions.py` |
| 管理员角色 | 固定 `ALL_CAPABILITY_CODES`，`set_role_capabilities` 对 admin 抛错 | `rbac_service.py` |
| 迁移 | `_migrate_role_capabilities`（TEXT 无 DEFAULT，手工回填 `'[]'`） | `db.py` |
| 查询注册表 | `GET /api/capabilities` → `CapabilityOut` | `roles_api.py` |
| 授权接口 | `PUT /api/roles/{id}/capabilities` → `refresh_sessions` 踢 WS | `roles_api.py` |
| 写操作拦截范式 | `permissions.can_create_strategy` / `can_write_strategy` | `strategies.py` 三处写路由 |
| 前端判定 | `auth.hasCapability(code)` / `auth.canUseAiStrategy` | `stores/auth.ts` |
| 前端能力清单 | `CAPABILITIES` 常量数组（代码内对齐注册表） | `stores/rbac.ts` |
| 前端勾选 UI | 角色编辑弹窗的「能力」check-list | `PermissionView.vue` |

> **结论**：加一个 `follow_leader` 能力码 = 在注册表加一行 + 前端 `CAPABILITIES` 加一行 + 三个业务拦截点。**不需要新表、不需要新列、不需要新迁移。**

### 2.2 其它影响面

| 事实 | 位置 | 对本设计的影响 |
|---|---|---|
| 每个 owner 至多一个主节点；无主节点时交易页无法触发 | `node_service._clear_primary_for_owner` / `trading_api.quick_trigger` 的 `is_primary` 校验 | 带单源天然 = 主节点；无主节点的用户不展示 |
| 交易页触发按 `(strategy_type, username, login, symbol)` 找/建分组，成员 = 主节点 | `trading_api._group_name` / `quick_trigger` 的 `node_ids=[body.node_id]` | 可跟单分组主要是「交易页自动创建」的那批，实际都含主节点 |
| 主节点切换阶段 2 会把旧主节点替换成新主节点 | `group_service.migrate_primary_groups`（另一份设计稿） | 迁移后旧分组仍含新主节点 → 继续留在跟单市场（见 §8.2） |
| 分组成员必须与分组**同一所有者** | `group_service.validate_node_ids` → `permissions.foreign_node_ids` | 跟单天然跨 owner，**必须另立成员关系** |
| 编辑分组时 `node_ids` 是**整体替换**语义 | `group_service._replace_members` | 跟单关系若放同一张表，owner 编辑成员会误删别人的跟单节点 |
| 分组缓存 dict 里有 `members` | `group_service.group_row_to_dict` → `redis_store.cache_group` | 缓存结构需扩展 `followers` 字段 |
| 有效节点口径 = `member_ids` ∩ (启用 ∧ 在线) | `group_rules.effective_node_ids` | 需合并跟单成员，并额外排除主节点 |
| poll 轮转参与者 = `member_ids` | `GroupDispatcher._send_poll` → `group_rules.reconcile_rotation` | 跟单成员同样进轮转队列（排在 owner 成员之后） |
| 主节点切换有阻塞前置 + 阶段 2 迁移 | `node_service.set_primary` / `PrimarySwitchBlocked` | 「跟单节点升主节点」接进这条链路 |
| 后台 WS 按资源归属过滤事件 | `permissions.event_resource` + `rbac_service.resolve_owner` + `connections.broadcast_admin` | 跟单员收不到自己节点的 `group_dispatch` 事件，**必须扩展可见性** |
| 手动信号对普通用户收窄到本人分组 | `console_api._scope_groups` → `permissions.scope_group_ids` | 带单员无法定向别人的分组（安全边界，天然成立） |
| 信号定向字段 `group_ids` / `template_ids` | `parser.TradingSignal` + `webhook._validate_targeting` | 「只推主节点」需要一个**节点级定向**字段 `node_ids` |
| 去重指纹含 model/action/symbol/volume/sl/tp/定向 | `webhook.process_signal` 的 `fp` | 带单 / 不带单两条信号必须被区分，**指纹必须加 `node_ids`** |
| 交易页 `OrderDialog` 有 `carry` 开关 | `OrderDialog.vue` `defineModel('carry')`，每次打开重置 true | **当前是死开关**：父组件 `TradingView` 未绑定、代码路径未读取，需要接线 |
| `assign_nodes` 会把节点从所有分组摘出 | `group_service.remove_node_from_all_groups` | 节点转移归属 / 删除时**必须同步清理跟单关系**（易漏点） |

---

## 3. 数据模型

### 3.1 唯一新表：`group_follower_node`（跟单成员）

```python
class GroupFollowerNode(Base):
    """跟单成员：非主节点加入他人的带单源分组。

    与 node_group_member 严格分离：
    - node_group_member    = 分组所有者自己的节点（owner 可编辑、排序、开限价监听）；
    - group_follower_node  = 跟单员的节点（只读、不可被 owner 编辑、限价监听恒关）。
    """
    __tablename__ = "group_follower_node"
    __table_args__ = (
        UniqueConstraint("group_id", "node_id", name="uq_follow_group_node"),
    )

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)

    # 跟单员用户 ID（冗余自 nodes.owner_user_id，便于按人查询 / 审计）
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)

    # 带单源冗余快照，写入时取「分组 owner 的主节点」。
    # 只用于展示 / 审计 / 主节点切换后的存量排查，**不参与运行时判定**
    # （运行时一律按 group.owner_user_id 现算主节点，避免主节点切换后快照过期）。
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 组内轮询顺序；跟单成员统一排在 owner 成员之后
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

**不加 `UniqueConstraint("node_id")`（一个节点只能跟一个分组）？**
现有系统已支持「一个节点同时承接多个分组的任务」（并发占位是 `group_id + node_id` 维度，见 `redis_store.acquire_group_node_busy`）。技术上多组可行。首版不加唯一约束，但：
- 加入跟单时，若该节点已跟 N 个分组，前端提示「该节点已在 M 个分组中跟单」；
- 预留 `follow_max_groups_per_user` 配置（默认 3，超出拒绝），避免误操作把同一节点挂到很多分组。

### 3.2 不新增 `users` 列

**`users.follow_enabled` 已取消**。带单资格完全由 `Principal.capabilities` 里的 `follow_leader` 决定，链路见 §2.1。这比用户列更好：多角色用户自动取并集，管理员改角色即生效，`refresh_sessions` 会踢掉旧会话强制重连。

### 3.3 Redis 缓存结构

`group` 缓存 dict 增加并列字段：

```python
{
    "group_id": ..., "name": ..., ...,
    "members":   [{"node_id", "sort_order", "limit_watch_report"}],   # 不变
    "followers": [{"node_id", "follower_user_id", "sort_order"}],     # 新增
}
```

`group_service.warm_cache` 与 `group_row_to_dict` 同步改造。

### 3.4 迁移

- 新表由 `Base.metadata.create_all` 自动创建，**无需手写迁移**。
- `sys_role.capabilities` 列**已存在**（`_migrate_role_capabilities` 已上线），**无需迁移**。
- **存量数据零迁移**：新表为空，没有任何跟单关系，功能处于「无人跟单」的空转态。

---

## 4. 权限设计

### 4.1 能力码注册

```python
# permissions.py
CAP_AI_STRATEGY = "ai_strategy"
CAP_FOLLOW_LEADER = "follow_leader"   # 带单：可作为带单员对外接单

CAPABILITY_REGISTRY: tuple[tuple[str, str], ...] = (
    (CAP_AI_STRATEGY, "AI 智能加仓策略"),
    (CAP_FOLLOW_LEADER, "带单"),
)

def can_be_leader(p: Principal) -> bool:
    """能否作为带单员对外接单（仅能力，不看数据归属）。"""
    return can_capability(p, CAP_FOLLOW_LEADER)
```

**语义边界（重要）**：

| 方向 | 需要什么 |
|---|---|
| **我能否带单给他人** | `follow_leader` 能力 |
| **我能否跟别人的单** | **什么都不需要** —— 只要登录 + 有【带单】菜单即可 |
| **别人能否看到我带单** | 由我的 `follow_leader` 能力决定（`can_be_leader(u)`） |

### 4.2 【带单】菜单（独立 Tab）

```python
MENU_FOLLOW = "follow"
MenuDef(MENU_FOLLOW, "带单", "/follow", True),   # assignable=True：默认开放给普通用户
```

- 插入 `MENU_REGISTRY` 紧跟 `MENU_TRADING`（导航顺序：首页 → 交易 → **带单** → 总览 → …）；
- 因为 `assignable=True`，内置 `user` 角色种子（`DEFAULT_USER_MENUS = ASSIGNABLE_MENU_CODES`）**自动包含**，存量用户升级后直接能看到 Tab；
- 前端同步改 `constants/menus.ts` 的 `MENU_PATHS` 与 `App.vue` 导航项 + SVG。

### 4.3 能力授予 / 撤销（管理员）

**唯一入口**：【用户权限 → 角色 Tab → 新建 / 编辑角色 → 能力勾选】，走已有的 `PUT /api/roles/{id}/capabilities`。

**撤销的连带影响**（无需任何额外代码，全部由运行时判定兜住）：

| 情况 | 行为 |
|---|---|
| 带单员失去能力 | 其分组立刻从跟单市场下架（`可展示带单员` 判定不再命中） |
| 已有跟单关系 | 跟单节点不再接收新信号；**已在跑任务跑完** |
| 需要清理数据吗 | **不需要**。关系保留在表里；重新授予能力后自动恢复。这与 `ai_strategy` 撤销后 AI 策略实例仍在的行为一致 |

**前置提示（前端）**：勾选 `follow_leader` 时，弹窗提示「授予该能力的用户，其主节点与主节点所在分组将出现在所有客户的带单界面中」。让管理员知道这个能力是**对外暴露**的，不是内部工具。

### 4.4 事件可见性（必须扩展，否则跟单页面是瞎的）

现状：`broadcast_admin` 通过 `event_resource(message) → ("group", gid) → resolve_owner → 分组所有者` 过滤。跟单员不是 owner，收不到自己节点的 `group_dispatch` / `strategy_progress` / `strategy_finished` / `account` 事件。

改造（保持现有结构，只扩一个维度）：

```python
# rbac_service
async def resolve_access(store, resource) -> tuple[Optional[int], set[int]]:
    """返回 (owner_user_id, follower_user_ids)；事件对该 follower 也可见。"""

# permissions
def event_visible(p, resource, owner, followers) -> bool:
    if p.is_admin: return True
    if owner is not None and owns(p, owner): return True
    return p.user_id in followers
```

`connections.broadcast_admin` 改一行调用。开销可接受：只在存在普通用户连接时计算，且 group 已在 Redis 缓存里。

---

## 5. 后端 API 设计

新增 `backend/app/follow_api.py`（路由）+ `follow_service.py`（持久化/缓存）+ `follow_rules.py`（纯规则，单测友好）。

### 5.1 带单员列表（主入口）

```
GET /api/follow/leaders?q=&only_following=
```

鉴权：`require_menu(MENU_FOLLOW)`。按 §1.1 的 `可展示带单员` 过滤。

**只投影聚合信息，不泄露对方节点明细** —— 跟单员需要判断的是「这单靠不靠谱」，不是「对面在跑哪几个号」：

```json
{
  "leaders": [{
    "leader_user_id": 7,
    "username": "alice",
    "display_name": "Alice",
    "primary_node": {
      "node_id": "n_primary",
      "name": "主节点A",
      "mt5_login": 10023,
      "status": "online",
      "enabled": true
    },
    "group_count": 3,
    "online_group_count": 2,
    "follower_node_count": 5,          // 全局：该带单员被多少个节点跟随
    "my_follow_group_count": 1,        // 我已跟其中 1 个分组
    "my_follow_node_count": 2,         // 我用了 2 个节点
    "joined_at": 1762000000            // 我首次跟单时间；未跟则为 null
  }]
}
```

`q` 模糊匹配 `username / display_name / primary_node.name / mt5_login`。

> **带单员能力的实时性**：`can_be_leader(leader_principal)` 需要按 leader 用户名组装 `Principal`。实现上用 `rbac_service.load_principal(store, username)`，**按用户批量预取一次**并做请求内缓存，不要在循环里逐个查 Redis。

### 5.2 带单员详情（点击「跟单」时加载）

```
GET /api/follow/leaders/{leader_user_id}
→ {
    "leader": { ...同 5.1 单条... },
    "groups": [{
      "group_id": "grp_x",
      "name": "trend_market-alice-10023-XAUUSD",
      "strategy_name": "XAUUSD 趋势策略",
      "strategy_template": "趋势策略",
      "symbol": "XAUUSD",
      "dispatch_mode": "sync",
      "signal_concurrent_enabled": true,
      "trend_risk_enabled": false,
      "enabled": true,
      "member_count": 1,               // owner 自有成员数
      "online_count": 2,               // 全组有效节点（含跟单员节点）
      "follower_count": 2,
      "follower_usernames": ["bob", "carol"],
      "active_task_count": 3,
      "signal_count": 128,
      "my_follow_node_ids": ["n_1"],   // 我在该组已跟的节点
      "my_follow_node_count": 1,
      "selectable": true,
      "unselectable_reason": null      // 见下
    }],
    "my_nodes": [                       // 我可用于跟单的节点（已剔除主节点）
      {"node_id": "n_1", "name": "节点B", "mt5_login": 20001, "status": "online", "enabled": true}
    ]
  }
```

`unselectable_reason` 取值：`null` / `"分组已禁用"` / `"已停止接收新信号（策略已停用）"` / `"我已在该分组"`。

> 主节点行由前端本地渲染为禁选（`"主节点不可跟单"`），服务端在 join 时二次校验 —— **前端只是体验，后端才是边界**。

### 5.3 我的跟单

```
GET /api/follow/me
→ {
    "following": [{
      "leader": { ...带单员摘要，同 5.1... },
      "groups": [{
        "group_id": ..., "group_name": ..., "symbol": ..., "strategy_name": ...,
        "marketable": true,                      // 带单员是否仍有 follow_leader 能力
        "unmarketable_reason": null,
        "active_task_count": 2,
        "my_nodes": [{
          "node_id": "n_1", "name": "节点B", "mt5_login": 20001, "status": "online",
          "joined_at": 1762000000,
          "my_active_task_count": 1,            // ★ 该节点在本分组的进行中任务数（移出弹窗用）
          "magic_open": true                    // 是否有未收口持仓
        }]
      }]
    }],
    "summary": { "following_count": 1, "follow_node_count": 2 }
  }
```

`marketable=false` 时带单员**已失去能力**，该分组不再下发新信号，但跟单员仍可自行「移出」清理。

### 5.4 批量加入跟单

```
POST /api/follow/join
body: { "leader_user_id": 7, "group_ids": ["grp_x"], "node_ids": ["n_1","n_2"] }
→ {
    "joined": [{ "group_id": "grp_x", "group_name": "...", "node_ids": ["n_1","n_2"] }],
    "skipped": [{ "node_id": "n_0", "reason": "主节点不能用于跟单" }],
    "leader_primary_node_id": "n_primary"
  }
```

鉴权：`require_menu(MENU_FOLLOW)`（**不需要** `follow_leader` —— 跟单是默认人人可做）。

校验顺序（任一不满足 → 4xx，`detail` 为人读原因）：

1. 带单员存在、`is_active`、**持有 `follow_leader` 能力**（否则 404，避免探测）；
2. 每个 `group_id` 必须属于该带单员**且包含其主节点**且 enabled 且绑定启用策略（否则 404）；
3. 每个 `node_id` 走 `owned_node_or_404`（归属校验，越权即 404）；
4. `node.is_primary` → 400「主节点不能用于跟单」；
5. `node.enabled` → 400「节点已停用」；
6. 已在该分组 → 幂等跳过（不报错，计入 `skipped`）；
7. 跟单数量上限（可配置，默认 3）。

写库（**一个分组一个事务**，避免部分失败）+ `store.cache_group(d)`，审计 `join_follow_group`（params 含 `leader_username`、`leader_primary_node_id`、`group_ids`、`node_ids`）。

### 5.5 批量移出跟单

```
POST /api/follow/leave
body: { "leader_user_id": 7, "group_ids": ["grp_x"], "node_ids": ["n_1"], "close_tasks": true }
→ {
    "removed": [{ "group_id": "grp_x", "node_ids": ["n_1"] }],
    "closing": [{ "node_id": "n_1", "dispatch_id": 123, "magic": 10010023 }],
    "forced":  [{ "node_id": "n_1", "reason": "节点离线，已强制收口，待重连补发平仓" }],
    "warning": null
  }
```

`close_tasks=true` 时：对每个 (group, node) 调 `group_dispatcher._stop_subtask`（**完全复用**现有 `close_subtask` 语义，不新写终止逻辑）—— 在线 → 下发 `strategy_stop` 并标 `closing`；离线 → `force_finish_subtasks` + `push_pending_stop` 排队等重连补发。

`close_tasks=false`：只删跟单关系，在跑任务继续跑到自然结束，返回体带 `warning`：「N 个任务不会停止，将继续按策略运行至结束」。

> **移出不需要带单员有 `follow_leader` 能力** —— 即使带单员已被撤销能力，跟单员也要能清理自己的节点。

审计 `leave_follow_group`，`params` 含 `close_tasks` 与被影响的 `dispatch_id` 列表。

### 5.6 分组侧的只读投影（分组管理页）

`GroupOut` 增加：

- `follower_count: int`
- `follower_summaries: list[{username, node_ids, joined_at}]`（仅 owner / 管理员可见）
- `my_follow_node_ids: list[str]`（当前请求者的跟单节点，便于标注哪些是「跟单 · bob」）

`update_group` 的成员编辑**忽略** `followers`；前端在成员列表把跟单成员以只读 chip 单独展示，不提供删除按钮。

### 5.7 节点级任务预查（复用 + 扩展）

现有 `GET /api/nodes/{node_id}/active-tasks` 返回该节点**全部分组**的在跑任务。移出确认框需要「该节点在**这一个分组**里」的任务数。

方案：**给现有接口加可选参数** `?group_id=`，不新增端点（保持 `/api/nodes/{id}/active-tasks` 契约向后兼容）。

---

## 6. 分发链路改造（核心）

### 6.1 有效节点合并跟单成员

```python
# group_rules.py
def participant_ids(group) -> list[str]:
    """owner 成员（按 sort_order）+ 跟单成员（按 sort_order），去重保序。"""

def effective_node_ids(group, nodemap, online_ids) -> list[str]:
    """有效节点 = participant_ids ∩ (存在 ∧ enabled ∧ 在线 ∧ 非主节点)。

    主节点过滤是双保险：加入跟单时已校验，节点后被设为主节点也会被剔除。
    """
```

`GroupDispatcher` 中所有用到 `group_rules.member_ids(group)` 的地方改用 `participant_ids(group)`：

- `_send_poll` 的轮转队列（`reconcile_rotation(order, participants)`）；
- `group_skip_reason` 的「无成员」判定；
- `limit_watch` 相关逻辑**不改**（跟单成员不进 `limit_watch_report_ids`）。

`store.save_group_rotation` 存的顺序里会混有跟单节点，`reconcile_rotation` 已能按 `participants` 收敛，无需额外迁移。

### 6.2 带单源分组的额外准入（分发期）

即使分组进了跟单市场，运行期还要再判一次「该分组此刻是否仍在带单」：

```python
# GroupDispatcher._dispatch_group
if group.get("followers") and not await follow_rules.group_still_leaded(store, group):
    # 带单员已失去 follow_leader 能力 → 本次只发给 owner 自己的成员
    effective = [n for n in effective if n in group_rules.member_ids(group)]
```

`group_still_leaded(store, group)` 内部：`load_principal(store, owner_username)` → `can_be_leader(p)`。**加请求内缓存**，避免一条信号查 N 次 Redis。

其余跟单成员在分发中的行为：

| 场景 | 行为 |
|---|---|
| 趋势风控（`trend_risk_enabled`） | 对跟单成员**照常生效**（按节点自身行情探测） |
| 信号并发（`signal_concurrent_enabled`） | 对跟单成员**照常生效**；不支持的策略模版自动回落单槽互斥（现有逻辑） |
| 手数 | 沿用 `group_rules.resolve_volume(signal.volume)`，**不带单缩放**。风险提示写在 UI（见 §7.5） |
| 分组禁用 | 全体成员（含跟单成员）都不接收 |
| 跟单员节点离线 | 与普通成员一致：不接收本轮；poll 模式自动跳到下一个节点 |

### 6.3 CLOSE 的作用域（必须在评审时确认）

`CLOSE` 按 `group_ids` 定向（`GroupDispatcher._close_candidates`），不带 `group_ids` 时命中**全部分组**——带单上线后，这会连带终止跟单员节点上该品种的任务。

- **Webhook 来源**（`source=tradingview`）：沿用现有语义，CLOSE 是终止指令、命中范围内全平。**保持不变**，但在评审时显式记录这一放大效应。
- **后台手动触发**：`console_api._scope_groups` 已把普通用户收窄到本人分组，带单员点 CLOSE **不会**误伤跟单员。安全边界靠现有代码保证，无需改动。
- **跟单员自己平仓**：走【交易】页持仓平仓（`POST /api/nodes/{id}/close`）或节点详情页，不涉及分组 CLOSE。

### 6.4 `node_ids` 节点级定向（「只推主节点」的载体）

`TradingSignal` 新增 `node_ids: List[str] = field(default_factory=list)`，parser 新增 `_extract_node_ids`（复用 `_extract_id_list`，归一为小写去重）。

规则：

- `webhook._validate_targeting`：`node_ids` 非空且 `model != strategy` → 400（与 `group_ids` 同口径）；
- 去重指纹 `fp` **必须**追加 `','.join(signal.node_ids)`，否则「带单开」与「带单关」两条同参数信号会被判为重复、互相吞掉；
- `GroupDispatcher._dispatch_group`：在算出 `effective` 之后按 `node_ids` 收窄（`effective = [n for n in effective if n in node_ids]`，`node_ids` 为空 = 不收窄）；
- CLOSE 信号忽略 `node_ids`（终止指令按分组全量执行）。

---

## 7. 前端设计

### 7.1 带单员列表页 `FollowView.vue`（路由 `/follow`，`meta.menu = 'follow'`）

```
┌ 带单 ────────────────────────────────────────────────┐
│ 把你自己的非主节点加入带单员的主节点分组，接收他的信号。 │
│ [搜索：用户名 / 主节点名 / MT5 号]      [我的跟单 (2)] │
├───────────────────────────────────────────────────────┤
│ ┌ 带单员 ─────────────────────────────────────────┐   │
│ │ ● Alice                                        │   │
│ │   主节点：主节点A（MT5 10023 · 在线）             │   │
│ │   带单源分组 3 个 · 被 5 个节点跟随              │   │
│ │                                [ 跟单 ]  [ 已跟 ]│   │
│ └─────────────────────────────────────────────────┘   │
│ ┌ 带单员 ─────────────────────────────────────────┐   │
│ │ ○ Bob                                          │   │
│ │   主节点：主节点X（MT5 10025 · 离线）             │   │
│ │   带单源分组 1 个 · 尚无人跟随                    │   │
│ │                          [ 跟单 ] [带单源离线]  │   │
│ └─────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
```

**离线主节点的处理**：主节点离线的带单员**仍然展示**，但标注「带单源离线」，并在详情里提示「该带单员当前无法触发信号，跟单后暂不会有任务」。不隐藏 —— 带单员只是暂时没开机。

> **无 `follow_leader` 能力的用户看不到自己**。带单页对所有人可见，但只有持有能力的带单员会出现在列表里。

### 7.2 跟单弹窗（双区选择，单弹窗完成）

```
┌ 跟单 · Alice ─────────────────────────────────┐
│ 带单源：主节点A（MT5 10023 · 在线）              │
│ 他触发信号时，信号会下发给所选分组的全部成员       │
├ 分组（可多选）─────────────────────────────────┤
│ ☑ trend_market-alice-10023-XAUUSD              │
│    XAUUSD · 趋势策略 · 全员同步 · 已触发 128 次   │
│    成员 1 + 跟单 2 · 在线 2 · 进行中 3            │
│ ☐ trend_limit-alice-10023-XAUUSD               │
│    XAUUSD · 趋势策略 · 全员同步 · 已触发 42 次    │
│ ☐ grid-alice-10023-XAUUSD  [已停止接收新信号]    │
├ 我的节点（可多选）───────────────────────────────┤
│ ☑ 节点B（MT5 20001 · 在线）                      │
│ ☑ 节点C（MT5 20002 · 离线）                      │
│ ⊘ 主节点X（MT5 20000）  主节点不可跟单             │
├─────────────────────────────────────────────────┤
│ 已选：2 个节点 × 2 个分组                          │
│ ⚠ 加入后将按对方的策略与手数执行，移出不会自动平仓   │
│                            [取消]      [确认跟单]  │
└─────────────────────────────────────────────────┘
```

- 分组区：`selectable=false` 的行置灰 + tooltip 说明原因；
- 节点区：主节点行**禁用** + 行尾 tag「主节点不可跟单」，hover 提示「主节点需保持独立，跟单请用其它节点」；停用节点禁用 + tag「已停用」；
- 提交后 toast：`已加入 2 个节点到 2 个分组`；`skipped` 项逐条回显原因。

### 7.3 「我的跟单」抽屉

按带单员分组折叠（默认全部展开）：

```
▾ Alice · 主节点A（10023 在线）
    grid-alice-10023-XAUUSD           策略：网格
      节点B（20001）  进行中 1 个任务   10-20 14:30 加入   [移出]
      节点C（20002）  无任务            10-20 14:30 加入   [移出]
▸ Bob · 主节点X（10025 离线）
```

带单员已失去能力时，折叠头加灰标「该带单员已关闭带单」，分组行加提示但**保留移出入口**。

### 7.4 移出确认弹窗

| 该节点在该分组的任务数 | 弹窗内容 |
|---|---|
| 0 | 「确认把节点 X 移出分组「xxx」？移出后不再接收该分组的新信号。」 |
| N > 0 | 「节点 X 在该分组有 **N 个进行中的任务**。移出后：**」+ 单选：<br>· **平仓并关闭任务后移出**（默认选中，红字提示会按魔术号平掉该分组持仓）<br>· **保留任务，移出后不再接收新信号**（附 warning：任务不会停止，会继续按策略跑到结束） |

二次确认弹窗复用 `utils/confirm.ts#confirmAction`；带选项时用 `ElMessageBox` 自定义 HTML。

### 7.5 风险提示文案（必须写进加入确认框）

> 加入后将按该分组绑定的策略「XAUUSD 趋势策略」在你的节点上执行，**手数、止损、加仓规则均以带单员为准**。移出不会自动平仓。

### 7.6 空态

| 场景 | 文案 + 动作 |
|---|---|
| 当前没有带单员开放 | 「当前没有开放跟单的带单员」 |
| 跟单员没有非主节点 | 「你没有可用于跟单的节点（主节点不可跟单）」+「去节点页」 |
| 跟单员没有节点 | 「还没有节点」+「去申请节点」 |

> **不再有「你还没有跟单权限」空态** —— 跟单是默认人人可做（v2 的这条要删掉）。

### 7.7 用户权限页（管理员）

**在【角色 Tab】的角色编辑弹窗里**，`capabilities` check-list 增加一项（数据驱动，无需改模板）：

```
☑ 带单
  授予该能力的用户，其主节点与主节点所在分组
  将出现在所有客户的带单界面中
```

因为 `CAPABILITIES` 是前端常量数组，只需在 `stores/rbac.ts` 追加一项；`PermissionView.vue` 的 v-for 自动渲染。

**用户列表侧**：新增一列「能力」，展示该用户所有角色的能力并集（`capabilityNames`）：

```
┌────────┬──────────┬───────────────┬──────────────┐
│ 用户   │ 角色     │ 能力          │ 状态         │
├────────┼──────────┼───────────────┼──────────────┤
│ alice  │ 普通用户 │ 带单          │ 启用         │
│ bob    │ 普通用户 │ —             │ 启用         │
└────────┴──────────┴───────────────┴──────────────┘
```

> `UserOut` 当前**不含** `capabilities`（只有 `roles`）。需要后端在 `_to_user_out` 里补一个「所有启用角色的能力并集」字段，供这一列展示（纯展示，不影响鉴权 —— 鉴权仍走 `Principal`）。

### 7.8 交易 Tab 带单开关接线（重点）

**现状**：`OrderDialog.vue` 里 `carry` 每次打开被重置为 `true`，但父组件 `TradingView` 没绑 `v-model:carry`，`triggerMarketStrategy` / `triggerGridStrategy` 也不读它——是一个死开关。

**接线方案**：

1. `TradingView.vue` 持有 `const carry = ref(true)`，传给 `<OrderDialog v-model:carry="carry" …>`；
2. 两条提交路径都按 `carry` 决定 payload：
   - **市价 / 限价**（`OrderDialog.triggerMarketStrategy`）→ `hub.triggerManualSignal` → `POST /api/console/manual-signal`：
     ```ts
     payload.model = 'strategy'
     payload.group_ids = [currentGroup.group_id]
     payload.node_ids = carry ? [] : [primaryNodeId]   // ← 带单关 = 只推主节点
     ```
   - **网格**（`TradingView.submit` → `quickTrigger`）→ `POST /api/trading/quick-trigger`：body 增加 `carry: boolean`（默认 `true`），后端在构造 signal 时：
     ```python
     data["group_ids"] = [group["group_id"]]
     if not body.carry:
         data["node_ids"] = [body.node_id]   # body.node_id 已经是主节点
     ```
3. 按钮文案随开关变化，给用户明确预期：
   - 带单开：`触发 · 本次将下发给 5 个节点（含 2 个跟单节点）`
   - 带单关：`触发 · 仅下发到主节点`
4. 开关旁加副说明（复用现有被注释掉的 `.od-carry-sub`）：带单开「信号同时推给跟单者」，带单关「仅推给自己的主节点」；
5. **无 `follow_leader` 能力的用户**：开关仍可用（他本来就没有跟单者），但开关旁提示「你还没有带单权限，跟单者不会收到信号」。

---

## 8. 主节点联动

### 8.1 跟单员侧：跟单节点升主节点

| 场景 | 行为 |
|---|---|
| 跟单节点被设为**主节点** | `node_service.set_primary` 阶段 0 已有阻塞检查（`active_subtasks_for_node` 覆盖该节点在**所有**分组**含带单分组**的任务）—— 有在跑任务仍然 409 拒绝，语义一致；阶段 2 新增 `follow_service.remove_primary_followings(node_id)`，把该节点从所有跟单关系中移出（幂等、可重复调用） |
| **取消**主节点 | 无需处理（取消后重新具备跟单资格，但不会自动重新加入） |

### 8.2 带单员侧：带单员切换主节点（对跟单市场的影响）

这是必须依赖主节点切换设计稿阶段 2 的边界：

| 情况 | 行为 |
|---|---|
| 已实现 `group_service.migrate_primary_groups`（把旧主节点替换成新主节点于交易页专属分组） | 旧分组迁移后**仍包含新主节点** → 继续留在跟单市场，跟单关系**不失效**，跟单员无感 ✅ |
| 未实现 / 迁移失败 | 旧分组不再含主节点 → **从跟单市场下架**；跟单员节点仍留在该分组里，但带单员已不再通过它触发信号，跟单关系成为「静默僵尸」⚠️ |

**处理方案（不落库现算）**：详情 / 我的跟单接口里，对每个跟单分组校验「分组 owner 的当前主节点是否在成员里」，不在则返回 `marketable: false` + `unmarketable_reason: "该带单员已更换主节点，本分组不再对外接单"`；前端灰显 + 保留「移出」入口。

### 8.3 节点删除 / 归属转移

`users_api.assign_nodes` 与 `node_service.delete_node` 在调用 `remove_node_from_all_groups` 之后，**必须**调用 `follow_service.remove_node_followings(node_id)` —— **这是最容易被漏掉的清理点**，单测需覆盖。

---

## 9. 审计与可观测

新增审计动作（`category="follow"`，AuditView 加一列标签）：

| action | target | params |
|---|---|---|
| `join_follow_group` | group_id | `{leader_username, leader_primary_node_id, group_ids, node_ids}` |
| `leave_follow_group` | group_id | `{group_ids, node_ids, close_tasks, dispatch_ids}` |

> **能力授予 / 撤销不需要新增审计动作** —— `roles_api.set_role_capabilities` 已经写了 `set_role_capabilities` 审计，`after_json` 里含完整能力列表。这正是复用能力体系的红利。

WS 事件：新增 `follow_membership_changed`（`{group_id, node_id, action, follower_username}`），带单页与分组页据此实时刷新。

---

## 10. 边界与风险

| 场景 | 行为 | 备注 |
|---|---|---|
| 带单员**没有主节点** | 不出现在带单列表 | 授予能力时前端提示 |
| 带单员主节点**离线** | 展示，但标灰「带单源离线」 | 跟单员可加，跟单后暂不会有任务 |
| 带单员名下分组**不含其主节点**（手工创建） | 不进入跟单市场 | 规则 4 |
| 带单员分组**未绑策略 / 策略停用** | 不进入跟单市场 | 避免展示不会响应的组 |
| **带单员失去 `follow_leader` 能力** | 分组立刻下架；跟单节点不再收新信号；**已在跑任务跑完** | 关系保留，重新授权后自动恢复 |
| 带单员被禁用（`is_active=false`） | 不展示；已在跑任务跑完 | |
| 带单员切换主节点 | 见 §8.2 | |
| 跟单员节点离线 | 不接收本轮；poll 模式自动跳到下一个节点 | |
| 分组被带单员删除 | 级联删除 `group_follower_node`（`group_service.delete_group` 扩展） | |
| 带单开关中途切换时的并发信号 | 不加锁。语义是「本次触发的信号按触发时刻的开关走」 | 交易页是单人手动操作，概率极低 |
| 一个节点同时跟多个分组 | 允许（受 `follow_max_groups_per_user` 限制），加入时提示 | |
| `poll` 模式下跟单成员参与轮转 | 参与，排在 owner 成员之后 | |
| 限价监听 | 跟单成员**永不**作为触发源 | 规则 3 |
| 跟单员手动 CLOSE 误伤带单员 | 不可能：普通用户 `group_ids` 被 `_scope_groups` 收窄到本人分组 | |
| Webhook 无定向 CLOSE | 会终止包括带单分组在内的全部匹配子任务 | 既有语义，评审时确认 |
| 同一节点被两个跟单员指向同一分组 | 不可能（节点归属唯一） | |
| 大量带单员时列表性能 | 需批量预取带单员 `Principal`（避免 N 次 Redis 查询）+ 分组计数批量查询 | 见 §5.1 |

---

## 11. 测试设计

### 11.1 `tests/test_follow_rules.py`（纯规则）

- `可跟单分组`：owner 匹配 + 含主节点 + enabled + 策略启用，四条判据逐个反例；
- `可展示带单员`：`follow_leader` 能力 ∧ `is_active` ∧ 有主节点 ∧ 分组非空 ∧ 非自己；
- `effective_node_ids` 合并 owner 成员 + 跟单成员，顺序正确；
- 跟单成员若是主节点 / 停用 / 离线，一律被剔除；
- `participant_ids` 去重；
- 跟单成员不进 `limit_watch_report_ids`。

### 11.2 `tests/test_follow_api.py`

| 用例 | 期望 |
|---|---|
| **无 `follow_leader` 能力的用户**，不出现在带单员列表 | 列表为空 |
| 有能力但无主节点的用户 | 不出现在列表 |
| 有能力、有主节点、但分组不含主节点 | 不出现在列表 |
| **撤销能力后**查带单员列表 | 该用户消失 |
| **撤销能力后**查 `GET /api/follow/me` | 跟单关系仍在，但 `marketable=false` |
| **撤销能力后**下发信号 | 该分组只命中 owner 成员 |
| **无能力的普通用户可以正常加入跟单** | 200（跟单不需要能力） |
| **无能力的用户可以正常移出**（含平仓） | 200 + `closing`（清理不需要能力） |
| 普通用户进入带单员详情 | 200，分组投影不含对方节点明细 |
| 加入**主节点** | 400「主节点不能用于跟单」 |
| 加入他人节点 | 404（归属校验，不泄露存在性） |
| 加入不含带单员主节点的分组 | 404 |
| 加入已禁用 / 策略停用的分组 | 404 |
| 重复加入同一节点 | 幂等（进 `skipped`），不报错 |
| 移出 + `close_tasks=true`，节点在线 | 返回 `closing`，子任务标 `closing` |
| 移出 + `close_tasks=true`，节点离线 | 返回 `forced`，`push_pending_stop` 入队 |
| 移出 + `close_tasks=false` | 只删关系，返回 warning |
| 带单员切换主节点后查旧分组 | `marketable=false` + 原因 |

### 11.3 `tests/test_group_dispatch_follow.py`

- 同步模式：跟单成员收到 `strategy_start`，各自独立 magic；
- 轮询模式：跟单成员在轮转队列中按顺序被选中；
- 跟单成员是该分组唯一在线成员 → 正常承接；
- **带单员失去能力后**：该分组只发给 owner 成员；
- `node_ids` 定向：只推主节点 → 跟单成员**不**产生子任务；
- 去重指纹：`group_ids` 相同、`node_ids` 不同的两条信号**都**被接受；
- CLOSE：按 group_ids 定向时终止该分组全部子任务（含跟单节点的）。

### 11.4 集成 / 回归

- **能力链路**：`test_rbac_api.py` 增加「角色授予 `follow_leader` → 用户 `Principal.capabilities` 含该码 → `can_be_leader` 为真 → `refresh_sessions` 踢掉旧连接」；
- **能力撤销**：撤销后同一用户 `can_be_leader` 为假，带单列表不再含他；
- **管理员角色**：仍固定拥有全部能力，`set_role_capabilities` 对 admin 抛 400；
- **未知能力码**：`PUT /api/roles/{id}/capabilities` 传 `follow_xxx` → 400「能力不存在」；
- **内置 user 角色种子**：`MENU_FOLLOW` 因 `assignable=True` 自动进 `DEFAULT_USER_MENUS`；
- **主节点联动（跟单员侧）**：跟单节点有在跑任务时设主 → 409；无任务时设主 → 跟单关系自动清除。
- **节点归属转移**：`assign_nodes` 收回跟单节点 → 跟单关系被清理（防脏数据）。
- **分组删除**：`delete_group` 级联清理跟单行。
- **事件可见性**：新增「跟单员能收到自己在带单分组的 `group_dispatch`」。

### 11.5 手工 smoke

`scripts/smoke_follow.py`：

1. 建 alice / bob 两用户；建角色 `带单员`（勾 `follow_leader`）与 `普通`（不勾）；alice 绑前者、bob 绑后者；
2. 断言：`GET /api/follow/leaders` 对 alice 返回 alice，对 bob 返回空；
3. alice 触发一次 `trend_market`（带单关）→ 断言只有 alice 主节点有子任务；
4. bob 进入带单页 → 断言能看到「Alice · 主节点A」卡片；
5. bob 点跟单 → 选自己的非主节点 + alice 的分组 → 断言加入成功（bob 无能力也能加）；
6. alice 触发一次（带单开）→ 断言 bob 的节点产生子任务、magic 不同；
7. bob 移出（close_tasks=true）→ 断言子任务 closing → done，跟单关系消失；
8. 管理员撤销 alice 角色的 `follow_leader` → 断言 bob 的带单列表不再含 alice；
9. alice 切换主节点 → 断言 §8.2 行为符合预期。

---

## 12. 灰度与迁移

- **无数据迁移、无新列**：新表由 `create_all` 建；能力列已存在。
- **存量用户可见性**：`MENU_FOLLOW` 因 `assignable=True` 会自动进内置 `user` 角色的菜单种子 —— 但 `seed_rbac` 只在**角色首次创建**时写菜单，存量 `user` 角色不会自动补上。**需要在 `seed_rbac` 里补一段「给内置 user 角色补齐新增的 `DEFAULT_USER_MENUS`」的幂等逻辑**（与 `_migrate_*` 同思路）。
- **能力默认关闭**：`follow_leader` 不在任何角色上默认勾选（沿用 `ai_strategy` 的做法 —— 新建角色的 `capabilities` 默认 `[]`）。存量角色迁移后 `capabilities='[]'`，因此**上线即零带单员**，需管理员显式授权。
- **全局功能开关**：`system_setting.follow_feature`（`"1"/"0"`，默认 `"0"`）。关闭时：带单页返回空态「功能未开放」；分发链路忽略全部跟单成员（退化为现状）；加入 / 移出接口 503。上线顺序：**先发代码（开关关）→ 灰度开 → 观察 → 全量**。
- **历史分组自动进入市场的风险**：一旦某用户被授予能力，其主节点所在的**全部历史分组**（含闲置的、实验性的）会立刻出现在所有人面前。可接受，但：
  - 授权时前端在确认框里展示「你名下有 N 个带单源分组将全部开放」；
  - 提供分组级开关 `PATCH /api/follow/groups/{gid}/visibility`（**建议二期**，避免把实验性分组暴露出去）。

---

## 13. 文件清单

**新增**

- `backend/app/follow_service.py`（持久化 + 缓存 + 预热）
- `backend/app/follow_rules.py`（纯规则：可跟单分组 / 可展示带单员 判定）
- `backend/app/follow_api.py`（路由）
- `backend/tests/test_follow_rules.py`、`test_follow_api.py`、`test_group_dispatch_follow.py`
- `scripts/smoke_follow.py`
- `frontend/src/views/FollowView.vue`
- `frontend/src/api/follow.ts`（类型 + 请求）
- `frontend/src/components/FollowJoinDialog.vue`（双区选择弹窗）、`FollowMyPanel.vue`（我的跟单 + 移出）

**修改**

| 文件 | 改动 | 规模 |
|---|---|---|
| `backend/app/permissions.py` | `CAP_FOLLOW_LEADER` + 注册表加一行 + `can_be_leader()`；`MENU_FOLLOW` + 注册；`event_visible` 扩展 follower 维度 | 小 |
| `backend/app/rbac_service.py` | `resolve_owner` → `resolve_access`（带 followers） | 小 |
| `backend/app/connections.py` | `broadcast_admin` 用新签名 | 极小 |
| `backend/app/orm.py` | 新增 `GroupFollowerNode`（**无新列**） | 小 |
| `backend/app/db.py` | **无需迁移**（新表 create_all 自动建） | 无 |
| `backend/app/rbac_service.py`（`seed_rbac`） | 补齐内置 `user` 角色的 `DEFAULT_USER_MENUS` 幂等逻辑 | 小 |
| `backend/app/group_rules.py` | `participant_ids` / `effective_node_ids` 合并跟单成员 + 主节点过滤 | 中 |
| `backend/app/group_service.py` | 缓存带 `followers`；`delete_group` 级联；`update_group` 忽略 followers | 中 |
| `backend/app/group_dispatcher.py` | `_send_poll` / `group_skip_reason` 用 `participant_ids`；`node_ids` 定向；带单员能力判定 | 中 |
| `backend/app/parser.py` | `TradingSignal.node_ids` + `_extract_node_ids` | 小 |
| `backend/app/webhook.py` | `_validate_targeting` 支持 `node_ids`；**去重指纹加 `node_ids`** | 小 |
| `backend/app/models.py` | `GroupOut` 增字段；`ManualSignalRequest.node_ids`；`QuickTriggerRequest.carry`；`UserOut.capabilities` | 中 |
| `backend/app/trading_api.py` | `quick_trigger` 按 `carry` 生成 `node_ids` | 小 |
| `backend/app/users_api.py` | **不需要 follow 接口**（能力走角色）；`assign_nodes` 清理跟单关系 | 极小 |
| `backend/app/node_service.py` | `set_primary` 阶段 2 清跟单；`delete_node` 清跟单 | 小 |
| `backend/app/main.py` | 注册 `follow_api.router` | 极小 |
| `backend/app/system_settings.py` | `follow_feature` 全局开关 | 极小 |
| `frontend/src/stores/rbac.ts` | `CAPABILITIES` 加 `follow_leader` 一项 | 极小 |
| `frontend/src/stores/auth.ts` | `canBeFollowLeader` getter | 极小 |
| `frontend/src/constants/menus.ts` | 新增 `follow` | 极小 |
| `frontend/src/router/index.ts` | `/follow` 路由 | 极小 |
| `frontend/src/App.vue` | 导航项 + SVG | 小 |
| `frontend/src/api/types.ts` | 前端类型（`UserOut.capabilities`） | 小 |
| `frontend/src/views/PermissionView.vue` | 用户列表「能力」列 | 小 |
| `frontend/src/views/TradingView.vue` | `carry` 状态 + payload 接线 | 中 |
| `frontend/src/components/OrderDialog.vue` | `v-model:carry` + 按钮文案 | 小 |
| `frontend/src/views/GroupsView.vue` | 跟单成员只读 chip | 小 |

> **对比 v2 的显著简化**：删掉了 `users.follow_enabled` 列、`_migrate_user_follow_enabled`、`_migrate_group_follower_node`、`PATCH/GET /api/users/{id}/follow` 两个接口、以及用户列表的跟单开关列 —— 全部由角色能力替代。

---

## 14. 落地步骤

1. **能力注册**：`permissions.py` 加 `CAP_FOLLOW_LEADER` + `can_be_leader()`；前端 `CAPABILITIES` 加一项；`test_permissions.py` 补用例。**此时能力已可授予，但无任何业务效果。**
2. **菜单注册**：`MENU_FOLLOW` + 前端路由 / 导航 / `menus.ts`；`seed_rbac` 补齐内置 user 角色菜单。**此时带单 Tab 可见但为空页面。**
3. `orm.py`：`GroupFollowerNode`（空表上线）。
4. `follow_rules.py` 纯规则（含「可跟单分组 / 可展示带单员」判定）+ 单测。
5. `group_service` 缓存带 `followers` + `warm_cache`；`group_rules.participant_ids` / `effective_node_ids` 改造 + 单测。**此时分发逻辑已支持 followers，但还没人加入 → 零行为变化。**
6. `group_dispatcher` 改造（poll 参与者、skip 判定、带单员能力判定）。
7. `follow_service.py` + `follow_api.py`（只读接口：带单员列表 / 详情 / 我的跟单）。
8. 加入 / 移出接口 + 审计 + WS 事件。
9. `node_ids` 定向（parser / webhook 指纹 / group_dispatcher / models）+ 交易页带单开关接线。
10. 主节点联动（跟单员侧清理 + 带单员侧 `marketable` 判定）+ `assign_nodes` / `delete_node` 清理。
11. 事件可见性扩展（`resolve_access` / `event_visible` / `broadcast_admin`）。
12. 带单页 UI 全量 + 用户权限页「能力」列。
13. `follow_feature` 全局开关 + 配置页入口。
14. 全量测试 + smoke → 灰度开。

---

## 15. 待确认项

1. **分组级「是否接受跟单」开关**是否首版就要？（能避免把实验性 / 闲置分组暴露给别人，见 §12）建议二期。
2. **跟单员是否需要手数缩放 / 上限**？目前按带单员信号手数执行，带单员若设 1 手，跟单员可能承受不起。建议二期做 `group_follower_node.lot_cap`（为空 = 不限制）。
3. **撤销带单员能力时，是否要一并终止跟单员节点上的在跑任务？** 当前方案是「不终止，让它跑完」。若业务要求更硬，可加 `terminate=true` 二次确认参数（复用 `_stop_subtask`）。
4. **CLOSE 作用域放大**（§6.3）是否需要为带单场景收窄？例如「CLOSE 只终止带单员自己节点的子任务，跟单员节点需自行平仓」。
5. **一个节点是否应限制只能跟一个分组**？当前设计允许多个（受上限配置约束）。
6. **管理员名下分组（`owner_user_id IS NULL`）是否进入跟单市场**？当前方案：**不进入** —— 带单源必须是「某用户的主节点」，而管理员名下分组的主节点属于 `owner_user_id IS NULL` 集合，与「带单员 = 持 `follow_leader` 能力的用户」口径冲突。建议明确排除。
7. **带单员主节点离线时是否还允许跟单**？当前方案：展示但标灰，允许跟单（主节点只是暂时没开机）。
8. **内置「普通用户」角色是否应默认含 `follow_leader`？** 当前方案：**不含**（与 `ai_strategy` 一致，能力默认关闭，由管理员显式授权）。若希望「所有客户都能带单」，改为在 `seed_rbac` 里给内置 user 角色默认勾上。