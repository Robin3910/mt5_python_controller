# 「我的」TAB 设计方案（设计稿，未实现）

> 本文档只设计、不写代码。
>
> **一句话定位**：「我的」是一个**个人门户页**，回答"我是谁 / 我拥有什么 / 我最近做了什么"，给常用入口提供快捷跳转。它**不是**功能页，**不进菜单注册表**，**所有登录用户都能进**。

---

## 1. 背景与现状

### 1.1 当前导航长什么样

顶部导航（`App.vue`）按 `auth.hasMenu(code)` 逐个渲染 `RouterLink`，所有菜单**硬编码在模板里**：

```100:129:frontend/src/App.vue
        <RouterLink v-if="auth.hasMenu('permissions')" to="/permissions">
```

现有 12 个菜单 + 1 个无菜单码的「配置」：

| 菜单码 | 路径 | 标题 | 可分配 |
|---|---|---|---|
| `home` | `/home` | 首页 | ✅ |
| `trading` | `/trading` | 交易 | ✅ |
| `dashboard` | `/` | 总览 | ✅ |
| `nodes` | `/nodes` | 节点 | ✅ |
| `trend` | `/trend` | 趋势面板 | ✅ |
| `groups` | `/groups` | 分组管理 | ✅ |
| `strategies` | `/strategies` | 策略管理 | ✅ |
| `audits` | `/audits` | 操作审计 | ✅ |
| `console` | `/console` | 中控台 | ❌ 仅管理员 |
| `client_versions` | `/client-versions` | 客户端版本 | ❌ 仅管理员 |
| `events` | `/events` | 事件 | ❌ 仅管理员 |
| `permissions` | `/permissions` | 用户权限 | ❌ 仅管理员 |
| （无） | `/config` | 配置 | — 所有人可进 |

### 1.2 「配置」页已经在做"个人设置"

`ConfigView.vue` 目前是个**混合页**：

| 内容 | 可见范围 | 后端 |
|---|---|---|
| 节点令牌 (NODE_TOKEN) + Webhook 鉴权开关 | 仅管理员 | `system_settings` |
| 修改密码 | 所有人 | `POST /api/change-password` |
| 双因素认证 (2FA) | 所有人 | `GET/POST /api/2fa/*` |

问题：
1. **语义混杂** —— 系统级运维（节点令牌）与个人安全设置（密码/2FA）挤在同一页；
2. **没有"我是谁"的落点** —— 顶栏只有一行 `display_name` + 「退出」按钮（`App.vue` 的 `topbar-user`）；
3. **名下资源无处可见** —— 普通用户要知道自己有几个节点/分组/策略，得分别翻三个页面。

### 1.3 后端已有的可复用能力

| 能力 | 位置 | 复用价值 |
|---|---|---|
| 按 owner 聚合名下资源 | `users_api._ownership` | ⭐ 直接给出 `{uid→node_ids}` / `{uid→分组数}` / `{uid→策略数}` |
| 数据归属判定 | `permissions.owns` / `owned_*_or_404` | ⭐ 「我的」天然继承现有隔离语义 |
| 当前用户信息 | `GET /api/me` → `auth.me` | 已有 `user_id / username / display_name / is_admin / roles / menus` |
| 我的操作审计 | `GET /api/audits` 内部已按 `operator == p.username` 过滤普通用户 | ⭐ 复用查询即可 |
| 2FA 状态 | `GET /api/2fa/status` | 直接用 |

**关键结论：后端数据几乎全部就位，缺的是"聚合到一个接口"。**

---

## 2. 设计目标

1. **一眼看清现状**：进「我的」不用点任何菜单就知道自己有什么；
2. **零权限摩擦**：所有登录用户都能进，管理员看全局、普通用户只看自己，**不新增菜单码、不新增 RBAC 开关**；
3. **数据零泄漏**：普通用户在「我的」里看不到别人的任何数据（复用 `permissions.owns`）；
4. **不重复造轮子**：密码 / 2FA 的**交互流程**仍在 `/config`，「我的」只放跳转按钮；
5. **一个接口拿全**：门户类页面做 N 次串行请求体验很差，聚合为 `GET /api/me/overview`；
6. **前端改动最小**：新增 1 个视图 + 1 个路由 + 1 处导航链接，其余为增量。

### 2.1 非目标

- ❌ 不在「我的」里复刻密码 / 2FA 的完整交互（避免双向维护）
- ❌ 不给管理员在「我的」里提供"查看其他用户明细"的能力（越权风险，且 `/permissions` 已有）
- ❌ 不改 `MENU_REGISTRY` / `menus.ts`（见 §4）
- ❌ 不做"我的订单/我的信号流水"这类需要新建数据表的深功能（列入 §10 后续演进）

---

## 3. 核心决策：「我的」为什么不进菜单注册表

### 决策：**不进。** 与 `/config` 同构处理。

理由：

1. **菜单注册表的语义是"由角色菜单控制的功能页"**。`MENU_REGISTRY` 里每一条都对应一个需要 RBAC 开关的业务域，而「我的」是所有人的个人门户，**没有"关掉它"这个业务需求**。

2. **路由守卫天然支持免菜单码**。守卫逻辑是 `if (to.meta.menu && !auth.hasMenu(...))` —— `meta.menu` 为空时**只校验登录态**：

```118:121:frontend/src/router/index.ts
  if (to.meta.menu && !auth.hasMenu(to.meta.menu)) {
    const fallback = firstAllowedPath(auth.hasMenu)
    return fallback === to.path ? true : fallback
  }
```

3. **不进 `MENU_PATHS` 才不会污染回退链**。`MENU_PATHS` 的顺序是"用户没有任何菜单时往哪跳"的回退顺序；如果把 `me` 放进去，一个被剥掉全部菜单的用户会先落到「我的」而不是「配置」，而 `/config` 才是设计上的最终兜底（改密码 / 2FA 是任何账号都该有的能力）。

4. **避免"配置页被关掉"的历史教训外溢**。若把 `me` 加进注册表并 `assignable=True`，将来有人误把它取消勾选，普通用户就会失去改密码入口 —— 这类风险应该只有 `/config` 承担。

---

## 4. 导航位置与改动清单

### 4.1 放在哪：**「用户权限」之后、「配置」之前**

```
首页 | 交易 | 总览 | 节点 | 趋势面板 | 分组管理 | 策略管理
     | 中控台 | 客户端版本 | 事件 | 操作审计 | 用户权限
     | 我的 | 配置
```

理由：
- 「配置」是导航**末尾**（且内容混杂），「我的」紧随其后 → 形成"个人域集中在导航右侧"的稳定心智；
- 「我的」展示的头像/用户名与顶栏 `topbar-user` 位置接近，视觉上是一组；
- 「用户权限」之后 → 管理员看完全局权限，顺手看自己的个人信息，是自然的收尾动线。

### 4.2 前端改动清单（精确到文件）

| 文件 | 改动 | 说明 |
|---|---|---|
| `frontend/src/router/index.ts` | **新增 1 条路由** `{ path: '/me', name: 'me', component: () => import('@/views/ProfileView.vue') }`，**不加 `meta.menu`** | 守卫自动只校验登录态 |
| `frontend/src/App.vue` | **新增 1 个 `<RouterLink to="/me">`**（带人形 SVG 图标，样式与其余菜单一致），**不加 `v-if="auth.hasMenu(...)"`** | 整段导航已在 `v-if="authed"` 内，登录即可见 |
| `frontend/src/views/ProfileView.vue` | **新建** | 页面主体 |
| `frontend/src/stores/profile.ts` | **新建**（可选） | 缓存 overview，短时切页不重复请求 |
| `frontend/src/api/types.ts` | **新增** `ProfileOverview` / `MyAuditItem` 等类型 | 与后端模型一一对应 |
| `frontend/src/constants/menus.ts` | **不改** | 见 §3 |
| `backend/app/permissions.py` | **不改** | `MENU_REGISTRY` 不动 |

---

## 5. 页面信息架构

四块卡片，从上到下：**身份 → 资源 → 安全 → 操作**。管理员与普通用户**同一套结构**，靠数据自适应（不切两套模板）。

### 5.1 区块一：身份卡（顶部通栏）

```
┌─────────────────────────────────────────────────────┐
│  (A)  张三                    [超管] [启用] [2FA 未绑定]  │
│      zhangsan                   角色：管理员            │
│                                                     │
│      创建于 2026-01-12   最近登录 2026-10-20 10:12    │
│      来自 1.2.3.4                                    │
│                                     [管理 2FA] [退出] │
└─────────────────────────────────────────────────────┘
```

- 左侧 **头像占位**：取 `display_name[0] || username[0]`，大写，圆形渐变背景（纯 CSS，无需图片资源）
- 中部：显示名 / 用户名；角色徽标（`roles[].name` 拼接，超管额外标 `超管`）；`is_active` 徽标
- 下部元信息：`created_at` / `last_login_at` / `last_login_ip`
- 右侧操作：**「管理 2FA」→ `/config`**，**「修改密码」→ `/config`**（用 `?focus=2fa` / `?focus=password` 定位锚点）
- 整卡点击 → 跳 `/config`（鼠标手型 + `title` 提示"点击前往个人设置"）

> `is_active` 对当前登录用户恒为 `true`（禁用即会话吊销），此处仍展示是为了与 `/permissions` 的用户列表保持字段一致、便于用户对照。

### 5.2 区块二：名下资源（核心区）

3 个计数器方块 + 1 个"进行中任务"方块，横排：

| 方块 | 主数字 | 副信息 | 点击跳转 |
|---|---|---|---|
| **节点** | 总数 | 在线 N / 离线 M | `/nodes` |
| **分组** | 总数 | 启用 N | `/groups` |
| **策略** | 总数 | 启用 N | `/strategies` |
| **进行中任务** | 活跃主任务数 | 有任务时可点"去终止" | `/groups` |

**下方折叠列表**：默认收起，展开显示最近 5 条明细 + 「查看全部 →」。

- 节点明细：节点名 / MT5 账号 / 在线状态点 / 是否主节点（★）
- 分组明细：分组名 / 模式（sync/poll）/ 成员数 / 在线成员数
- 策略明细：策略名 / 品种 / 模版名 / 启停

**管理员视角的差异**：管理员名下资源 = "管理员名下"（owner 为空的），同时在区块底部给一行**全局汇总**：`全站节点 N · 分组 M · 策略 K（其中我的 X / Y / Z）`，只给数字不给明细。

> 普通用户不会看到这行（数据隔离，且他们本就只有自己那份）。

### 5.3 区块三：安全设置（精简，只读 + 跳转）

只放**状态摘要**，把操作交给 `/config`：

| 项 | 展示 | 操作 |
|---|---|---|
| 双因素认证 | 徽标：已开启 / 已绑定未开启 / 未绑定 | 「去开启」/「重新绑定」/「更换密钥」→ `/config?focus=2fa` |
| 登录密码 | 「由你本人管理」+ 上次改密时间（若可取） | 「修改密码」→ `/config?focus=password` |
| 登录会话 | 「当前设备」一行（`UA` 简写 + 登录时间 + IP） | 无（本期不做踢出其它会话） |

> **不重复实现任何鉴权交互**：`/config` 里的改密需要旧密码校验、2FA 关闭需要密码 + 验证码 —— 这些逻辑留在原地。

### 5.4 区块四：我的最近操作

来源：`audit_api` 的查询，**过滤 `operator == 当前用户`**，倒序取 10 条。

表格列：时间 / 操作 / 目标 / 结果 / IP

- 分类筛选：`全部 / 中控台 / 节点 / 认证`（对应 `AUDIT_CATEGORIES = ("console","node","auth","system")`）
- 底部「查看全部」：
  - 管理员（`MENU_AUDITS`）→ `/audits`
  - 普通用户 → 就地展开成完整列表（调 `/api/me/audits` 分页），**不跳 `/audits`**（那条路由对他们是 403）

> 「我的」把 `GET /api/audits` 的 `require_menu(MENU_AUDITS)` **绕开**，但**保留了同等的数据隔离**：普通用户只查自己。这与 `audit_api` 内部 `operator = None if p.is_admin else p.username` 的既有逻辑一致 —— 唯一区别是 `MENU_AUDITS` 这个菜单开关对「我自己的操作」不再适用（合理：看自己的操作不涉及跨用户数据）。

### 5.5 空状态

普通用户可能什么都没有。约定：

| 情况 | 展示 |
|---|---|
| 0 节点 0 分组 0 策略 | 资源区显示插画位空态 + 「请联系管理员分配节点后再使用」 |
| 0 条审计 | 「暂无操作记录」 |
| 未开 2FA | 安全区**温和提示**（非报错色）：「建议开启双因素认证」+ 按钮 |

---

## 6. 后端设计

### 6.1 新增 `GET /api/me/overview`

**鉴权**：`Depends(get_principal)` —— **不要**加 `require_menu`、**不要**加 `require_admin`。所有登录用户可调。

**返回结构**（`ProfileOverview`）：

| 字段 | 类型 | 数据来源 |
|---|---|---|
| `user` | `ProfileUser` | `GET /api/me` 现有字段 + `created_at` |
| `resources.nodes` | `{ total, online, mine_total?, recent: NodeOut[≤5] }` | `store.all_nodes()` + `permissions.owns` |
| `resources.groups` | `{ total, enabled, recent: GroupOut[≤5] }` | `store.all_groups()` + `permissions.owns` |
| `resources.strategies` | `{ total, enabled, recent: StrategyOut[≤5] }` | `store.all_strategies()` + `permissions.owns` |
| `resources.active_tasks` | `int` | 汇总在跑子任务所属的主任务数 |
| `security.twofa` | `{ enabled, bound, pending_setup }` | 复用 `twofa` 模块的状态查询 |
| `security.last_password_change` | `number \| null` | 审计 `category=auth` + `action=change_password` 倒序首条 |
| `recent_audits` | `AuditRecord[≤10]` | `persist.recent_audits(..., operator=p.username)` |
| `global_summary` | `{ nodes, groups, strategies, users }` **仅 `is_admin` 时返回，否则 `null`** | 全量计数 |

**实现要点**：
1. `visible(p, items)` 已经封装了归属过滤，**直接复用**，不要手写 if；
2. `recent` 的排序：节点按 `last_seen` 倒序，分组按 `created_at` 倒序，策略按 `created_at` 倒序；
3. `active_tasks` 必须走 `group_persist` 的在跑任务判定（`SUBTASK_HOLDS_LOCK`，非终态），**不要**用 `active_task_count` 字段简单相加（有重复计数风险）；
4. 审计的 `params` / `raw_payload` 可能很大，门户只取前 5 个关键字段（`before`/`after` **不返回**）。

### 6.2 新增 `GET /api/me/audits`

**鉴权**：`get_principal`。查询参数：`page` / `page_size`（默认 20）/ `category`。

**固定** `operator = p.username`（管理员也只查自己，这是"我的审计"接口）。

### 6.3 扩展 `MeOut`（非破坏性）

| 新增字段 | 类型 | 来源 | 备注 |
|---|---|---|---|
| `created_at` | `number \| null` | `User.created_at` | ORM 已有，零成本 |
| `last_login_at` | `number \| null` | Redis | 需新增写入 |
| `last_login_ip` | `string \| null` | Redis | 需新增写入 |

**`last_login_*` 的实现**：在 `auth.py` 的登录成功分支（`login` 与 `login_2fa` 各一处）写 Redis：

```
key: auth:last_login:<username>
value: {"at": <epoch>, "ip": "<ip>"}
TTL: 90 天（超出即视为无记录，字段返回 null）
```

**取舍说明**：
- 选 Redis 而非 `users` 表新增列 → 不动 schema、不需迁移，回滚只需删 key；
- TTL 90 天 → 「最近登录」本就是短时效信息，长期无意义；
- 若运维需要永久记录，可改从审计 `category=auth` + `action=login` 倒序首条推导（**降级方案，见 §6.5**）。

### 6.4 不需要改的（明确划界）

- ❌ `permissions.py` 的 `MENU_REGISTRY` / `DEFAULT_USER_MENUS`
- ❌ `roles_api.py` / `sys_role_menu` 表
- ❌ 任何 `require_menu` 拦截 —— 「我的」的所有新接口只要求登录
- ❌ 现有接口的响应契约（所有改动均为**新增**或**可选新增字段**）

### 6.5 降级方案（若不想动登录流程）

若认为"在登录路径写 Redis"侵入性太强，`last_login_*` 可完全省略 —— 从 `GET /api/me/audits` 里筛 `category=auth` + `action=login` + `result=ok` 的首条推导出时间，IP 取该行审计的 `ip`。

**代价**：仅覆盖**本审计表保留窗口内**的登录；且登录审计本身存在写入延迟。

**建议**：先按 §6.3 实现（Redis 方案，语义准确）；若上线后觉得不值得，降级到 §6.5（删掉登录写入即可，无 schema 负担）。

---

## 7. 前端设计

### 7.1 目录

| 文件 | 职责 |
|---|---|
| `views/ProfileView.vue` | 页面主体（四个区块） |
| `stores/profile.ts` | overview 状态 + 缓存 + `fetchOverview(force?)` |
| `api/types.ts` | 新增 `ProfileOverview` / `ProfileUser` / `ResourceSummary` / `RecentAudit` |
| （沿用）`stores/auth.ts` | `me` 已在路由守卫时拉过，**不重复请求** |

### 7.2 数据流

```
ProfileView mounted
  → profile.fetchOverview()
      → if (缓存未过期 && auth.me 未变) 直接返回
      → else GET /api/me/overview
  → 并行（可选）GET /api/me/audits?page=1
```

**缓存策略**：进程内缓存 60 秒。理由：用户可能频繁在「我的」和「节点」之间切换，资源计数不必每次重算；60 秒足够让"刚做的操作"反映出来。

**失效时机**：`auth.token` 变化 / `logout` 时清空（与 `auth.setToken` 联动）。

### 7.3 组件拆分建议

页面较长，建议抽 3 个子组件保持可读性：

| 组件 | 内容 |
|---|---|
| `ProfileIdentityCard.vue` | 区块一 + 区块三（安全摘要） |
| `ProfileResourceStats.vue` | 区块二（计数器 + 折叠列表） |
| `ProfileRecentAudits.vue` | 区块四 |

均放在 `components/profile/` 下，`<script setup lang="ts">`，通过 props 接收数据、自身不发请求（单一数据源在 store）。

### 7.4 样式与响应式

- **复用现有全局样式**：`card` / `card-pad` / `h1` / `row` / `muted` / `tag` / `btn-primary` / `btn-ghost` / `pill` / `tabs` —— **不引入任何新的 CSS 变量**
- **玻璃质感**：区块容器沿用 `.card` + 现有 backdrop 效果，与总览页 / 节点页一致
- **响应式**（对齐现有 768px 断点）：
  - 桌面：身份卡横向排布，资源计数 4 列横排
  - 平板：资源计数 2×2
  - 移动：身份卡纵向堆叠；资源计数 2×2；明细列表复用现有 `.list-cards` + `.mobile-only` / `.desktop-only` 双模板模式
- **无障碍**：每个跳转方块用 `<button>` 或带 `role="link"` 的可聚焦元素，带 `aria-label`；折叠列表用 `<details>` 或显式 `aria-expanded`

### 7.5 加载与错误态

- 首次加载：骨架屏（沿用现有 `.muted` 占位样式，不引新组件）
- 拉取失败：不整页报错，**每个区块独立降级** —— 区块内显示「加载失败，重试」+ 小重试按钮；其他区块照常渲染
- 空数据：见 §5.5

---

## 8. 安全要点

| 风险 | 防护 |
|---|---|
| 普通用户越权看到他人资源 | 所有过滤走 `permissions.owns` / `visible`，**禁止**在 overview 里返回 `owner_username` 之外的他人信息 |
| `recent` 明细泄漏敏感信息 | 分组/策略的 `recent` 只取展示必需字段，**不返回** `members` 全量、`rules` 全量 |
| 审计信息泄漏 | `operator` 固定为自己；`params` / `before` / `after` **不返回** |
| 门户成为探测面 | 接口只接受 `page` / `page_size` / `category` 三个参数，`page_size` 设上限（如 100）；**不接受**任意 `operator` 查询参数 |
| `last_login_ip` 属敏感信息 | 仅本人可见（接口只返回 `p` 自己的） |

---

## 9. 实施步骤

| # | 步骤 | 产出 | 依赖 |
|---|---|---|---|
| 1 | 后端：`MeOut` 扩字段 + 登录写 `last_login` Redis | `auth.py` / `models.py` | — |
| 2 | 后端：`GET /api/me/overview` | 新增（建议放 `users_api.py` 或新 `profile_api.py`） | 1 |
| 3 | 后端：`GET /api/me/audits` | 同上 | 2 |
| 4 | 前端：`types.ts` 类型 + `stores/profile.ts` | — | 2 |
| 5 | 前端：`ProfileView.vue` **先只做区块一 + 区块二** | 最小可用版本 | 4 |
| 6 | 前端：路由 `/me` + `App.vue` 导航链接 | 可访问 | 5 |
| 7 | 前端：补区块三（安全摘要）+ 区块四（我的操作） | 完整版 | 5, 6 |
| 8 | 前端：抽 3 个子组件 + 移动端适配 | 可维护 | 7 |
| 9 | 后端单测：`overview` 的归属隔离（普通用户看不到他人） | `tests/` | 2 |

**先做 5+6 拿到可用版本**，再迭代 7/8 —— 这样即便后端字段调整，前端也能先跑起来验证布局。

---

## 10. 后续演进（本期不做）

| 想法 | 价值 | 前置条件 |
|---|---|---|
| 「我的信号流水」 | 查自己名下分组的历史信号 | 需复用 `group_signals` 的 owner 过滤，或由 `overview` 下钻 |
| 「我的收益」 | 名下分组的已实现盈亏汇总 | 需聚合 `GroupTaskDispatch.realized_profit`，注意管理员口径 |
| 「我的告警」 | 节点离线 / 风控触发汇总 | 需新建告警表 |
| 「登录会话管理」 | 查看并踢出其它设备会话 | 需后端记录每会话 JWT `jti` |
| 「通知偏好」 | 自定义 Webhook / 站内通知 | 需新建偏好表 |

---

## 11. 验收标准

- [ ] 所有登录用户（含零菜单的极端角色）都能进入 `/me`，且不 403
- [ ] 路由不挂 `meta.menu`，`MENU_REGISTRY` / `menus.ts` **零改动**
- [ ] 无任何菜单权限的用户，登录后仍能通过「我的」进入并改密码（改密码入口在 `/config`，导航可达）
- [ ] 普通用户看到的节点/分组/策略数量**恰好等于**其名下资源，与 `/nodes`、`/groups`、`/strategies` 页面显示一致
- [ ] 普通用户**看不到**任何他人用户名、他人资源明细、他人审计
- [ ] 管理员能看到「全站汇总」数字行，且汇总与 `/permissions` 页面统计一致
- [ ] `active_tasks` 数字与 `/groups` 页面的进行中任务数一致（无重复计数）
- [ ] 「我的操作」中普通用户只看到自己的行
- [ ] 移动端（≤768px）无横向滚动，四个区块均可读可用
- [ ] 单个区块接口失败不影响其余区块渲染

---

## 12. 决策记录（ADR 摘要）

| # | 决策 | 备选 | 选择理由 |
|---|---|---|---|
| 1 | 「我的」不进 `MENU_REGISTRY` | 注册为 `assignable=False` 的菜单码 | 门户页无"关闭"需求；避免占用 RBAC 语义；与 `/config` 一致 |
| 2 | 不进 `MENU_PATHS` | 加到 `menus.ts` 末尾 | 保住「配置」作为最终兜底的回退链 |
| 3 | 聚合为 `GET /api/me/overview` | 前端并发调 5 个现有接口 | 门户类页面避免请求瀑布；现有接口多为 `require_menu`，普通用户调不到 |
| 4 | `/me/audits` 独立接口 | 复用 `/api/audits` | `/api/audits` 需 `MENU_AUDITS`，普通用户被拦 |
| 5 | 密码 / 2FA 只跳转不重做 | 把交互搬进「我的」 | 鉴权交互复杂且已在 `/config` 验证过，双份维护必然发散 |
| 6 | `last_login` 存 Redis | `users` 表加列 | 不动 schema、无迁移、可随时回退 |