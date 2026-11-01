# 跟单市场分层 + 【AI 交易】Tab 统一实施方案

> **一句话目标**：把「跟单」拆成**两个互斥市场**——
> 【带单】Tab 只承载 `tpl_2` 趋势 / `tpl_3` 网格；
> 【AI 交易】Tab 新承载 `tpl_1` **AI 智能加仓策略**，以「机器人广场」形态让普通用户跟单。
> 两个市场**共用同一张关系表、同一条分发链路**，靠「分组绑定的策略模版」天然互斥。
>
> **本方案 = 原《带单市场过滤》方案 + 原《AI 交易 Tab 设计》方案 的合并版**，
> 已消解两份方案之间的口径冲突，并按核实结果修正了若干结论。
>
> ---
>
> ## 已确认的七条前置决策
>
> | # | 决策 | 含义 |
> |---|---|---|
> | 1 | **白名单过滤** | 带单市场只放 `tpl_2` + `tpl_3`；未来新增模版**默认不外露** |
> | 2 | **参数化「跟单市场」** | 不把 `tpl_1` 硬编码进各处判断，抽象成 `market` 参数；两个 Tab 复用同一套关系表与分发链路 |
> | 3 | **存量关系由管理员自行移除** | 不做自动迁移 / 自动删除；只保证「AI 分组不再被新跟单、不再下发信号」 |
> | 4 | **不控制手数** | 完全按带单员的手数执行，只做风险提示文案 |
> | 5 | **跟实时（不锁快照）** | 带单员改 AI 策略 → 正在跑的跟单任务**自动同步**（复用现有热推） |
> | 6 | **展示单位 = 主节点** | 一个「机器人」= 一位带单员的主节点；挂主节点所在、绑定 `tpl_1` 的分组 |
> | 7 | **两能力都要 + 显式开关** | 机器人准入 = `ai_strategy` **且** `follow_leader`；分组还需**显式勾选**「开放 AI 跟单」（默认关） |

---

## 0. 本次核实修正的四处结论

> 这四处与原两份方案的说法有出入，**已逐条回到代码验证**，此处为修正后的最终口径。

| # | 原结论 | 修正后 | 证据 |
|---|---|---|---|
| **M1** | 「AI 分组单槽互斥 → 跟单员之间互相挤占」 | **跟单员之间不竞争**。忙闲占位键是 `(group_id, node_id)`，每个跟单员节点各有**独立**单槽。真正的限制是「**同一跟单员节点**同时只跑一单」 | `group_dispatcher.py:715` `acquire_group_node_busy(group_id, node_id, ...)` |
| **M2** | 「带单员改参数立即影响在跑的跟单任务」 | **成立**。`tpl_1` 热推按 `strategy_id` 覆盖该策略**所有**非终态子任务（不分 owner / 是否跟单），跟单员节点自然被覆盖 | `group_persist.py:246-286` |
| **M3** | 「跟单员收不到自己节点的 WS 事件」 | **成立**。`event_visible` 只认 owner，AI tab 与带单 tab 同病 | `permissions.py:304-311` |
| **M4** | 「`adopt-followers` 会跨市场静默拉人」 | **成立且更严重**：它的来源筛选是 `is_leader_source_group`（owner + 含主节点），**完全不看模版**。带单员一旦开 AI 分组并触发继承，跟单员会被**静默**拉进 AI 分组 | `groups.py:402-415` |
| **M5** | 「模版判定用 `template_in_market`（白名单命中才算）」 | **实施中发现需要区分「模版已知但不在市场」与「模版未知」**。白名单式判定会把**未绑策略的分组**也判死，破坏既有行为（16 个既有测试失败）。改用 `template_out_of_market`：**只在模版已知时拒绝** | 实施验证：`test_follow_api.py` 16 → 0 失败 |

> **M4 是本方案必须修的硬边界**（详见 §5）。
>
> **M5 的取舍说明**：模版未知的分组（未绑策略 / 策略被删除）保持既有可跟单行为。
> 安全性由分发层兜底（`_candidate_groups` 见到未绑策略直接跳过，本就不分发），
> 跟单侧不必判死 —— 跟单员在跟一个「还没配策略的分组」是合法场景。

---

## 1. 现状核对（已核实的代码事实）

### 1.1 策略模版与分组绑定

| 项 | 值 | 位置 |
|---|---|---|
| AI 智能加仓策略 | `tpl_1` / `TEMPLATE_1_ID` / `AI_STRATEGY_TEMPLATE_ID` | `strategy_templates.py:103`、`permissions.py:88` |
| 趋势策略 | `tpl_2` / `TEMPLATE_2_ID` | `strategy_templates.py:105` |
| 网格策略 | `tpl_3` / `TEMPLATE_3_ID` | `strategy_templates.py:107` |
| 分组 → 策略 | **一对一**（`node_group.strategy_id` unique 索引） | `orm.py:272`、`db.py:104` |
| 分组 → 策略 owner | 必须与分组同 owner | `group_service.validate_strategy_binding` |

**关键推论**：分组通过 `strategy_id` 绑定**唯一一个**策略，
所以「这个分组属于哪个市场」= 「它绑定的那个策略的 `template_id` 属于哪个白名单」。
**一个分组只可能属于一个市场 → 两市场天然互斥。**

### 1.2 当前判定链路（没有模版维度 —— 要补的洞）

```76:85:backend/app/follow_rules.py
def followable_groups(
    groups: list[dict], leader_user_id: int, primary_node_id: Optional[str],
) -> list[dict]:
    """带单员名下所有可被跟单的分组（启用 + 含主节点）。"""
    if not primary_node_id:
        return []
    return [
        g for g in (groups or [])
        if isinstance(g, dict)
        and g.get("enabled", True)
        and is_leader_source_group(g, leader_user_id, primary_node_id)
    ]
```

它只判「启用 + 含主节点 + owner 匹配」，**不看策略模版** → `tpl_1` 分组现在会正常出现在
【带单】列表并可被 join。

### 1.3 五个必须同时收口的入口

过滤只做前端不够（前端是体验，后端才是边界）：

| # | 路径 | 现状 | 后果 |
|---|---|---|---|
| **A** | `GET /api/follow/leaders` | 不过滤模版 | AI 分组出现在带单列表 |
| **B** | `GET /api/follow/leaders/{id}` | 不过滤模版 | AI 分组出现在详情页可选列表 |
| **C** | `POST /api/follow/join` | 不过滤模版 | **能把节点加进 AI 分组**（写入漏洞根源） |
| **D** | `group_dispatcher._effective_with_follows` | 只判「带单员是否还有能力」 | **AI 分组里的跟单节点仍会收到信号**（唯一硬边界） |
| **E** | `POST /groups/{id}/adopt-followers` | 来源筛选**完全不看模版** | **跨市场静默拉人**（M4） |

> **D 与 E 是硬边界**：A/B/C 挡住入口，D 挡住存量下发，E 挡住跨市场污染。

### 1.4 分发层已有的降级机制（可复用）

```175:198:backend/app/group_dispatcher.py
    async def _effective_with_follows(
        self, group: dict, nodemap: dict[str, dict], online_ids: set[str],
        *, signal: Optional[TradingSignal] = None,
    ) -> list[str]:
        """本次分发的有效节点，串起 跟单员准入 与 节点定向（node_ids）。
        ...
        """
        if group_rules.has_followers(group) and not await self._leader_still_enabled(group):
            base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
        else:
            base = group_rules.effective_node_ids(group, nodemap, online_ids)
```

已有「带单员失去能力 → 退回 owner 口径」的降级路径。
本方案**沿用同一形状**，把「市场准入」纳入降级条件。

### 1.5 策略快照的取值路径

| 场景 | 取值方式 | 现成批量接口 |
|---|---|---|
| 列表 / 详情 / join（A/B/C） | `store.get_strategy(g["strategy_id"])` → `["template_id"]` | ✅ `follow_api._strategies_of` |
| 分发层（D） | `strategy["template_id"]`（`_candidate_groups` 已加载） | ✅ **零额外 IO** |
| 跨市场判定（E） | 同上 | ✅ 需在 `groups.py` 补一次批量预取 |

> **注意**：分组缓存 dict 里**没有** `template_id`，只有 `strategy_id`。

### 1.6 能力体系已就绪（决策 7 的零成本部分）

| 项 | 现状 |
|---|---|
| `CAP_AI_STRATEGY = "ai_strategy"` | 已注册（`permissions.py:73`） |
| `CAP_FOLLOW_LEADER = "follow_leader"` | 已注册（`permissions.py:76`） |
| `can_capability(p, code)` | 现成，管理员自动放行 |
| `can_be_leader(p)` | = `can_capability(p, CAP_FOLLOW_LEADER)` |

> 决策 7 的「两能力都要」= `can_capability(p, CAP_AI_STRATEGY) and can_be_leader(p)`，
> **零新增注册**。

### 1.7 关系表已完备（零新增表）

```296:321:backend/app/orm.py
class GroupFollowerNode(Base):
    """跟单成员：非主节点加入他人的带单源分组。
    ...
    """
```

字段完备（`follower_user_id` / `leader_user_id` / `leader_primary_node_id` 快照），
**无需新增任何列**。`leader_user_id` + 分组模版唯一定位市场。

### 1.8 `tpl_1` 的三条独有特性（AI tab 的设计前提）

| # | 特性 | 证据 | 设计影响 |
|---|---|---|---|
| **D1** | **不支持信号并发** | `supports_signal_concurrent` 只放行 `tpl_2`/`tpl_3` | AI 分组强制单槽：**同一跟单员节点**同时只跑一单（详见 M1） |
| **D2** | **有热推**（`tpl_2`/`tpl_3` 没有） | `rewrite_running_strategy_snapshots` + `push_strategy_updates` | 决策 5「跟实时」的机制**已存在且免费** |
| **D3** | **持续加仓**（逆势/顺势 + 分批档位） | `CounterTrendRule.batch_levels` / `TrendFollowRule` | 敞口**单调增长**；决策 4「不控手数」的风险量级 |

### 1.9 移出跟单的在跑任务语义（决策 7 补充）

```1020:1024:backend/app/models.py
class FollowLeaveRequest(BaseModel):
    leader_user_id: int = Field(ge=1)
    group_ids: list[str] = Field(min_length=1, max_length=50)
    node_ids: list[str] = Field(min_length=1, max_length=50)
    close_tasks: bool = False
```

`close_tasks` 默认 `False`，且已有提示文案
「存在进行中的任务，移出后不会停止，将继续按策略运行至结束」。

> **结论**：你确认的「在跑任务默认等待执行结束」= **现有默认行为，无需任何改动**。
> 任务执行由 node_client 负责，后端只解除跟单关系、**不再下发新信号**。

---

## 2. 设计：把「跟单市场」参数化

### 2.1 市场常量

```python
# backend/app/follow_rules.py（新增）

# 带单市场（普通跟单）的模版白名单：只放趋势策略与网格策略。
# AI 智能加仓策略（tpl_1）不在其中 —— 它归【AI 交易】Tab 承载。
# 白名单而非黑名单：未来新增模版默认不外露，需显式加入才进入带单市场。
FOLLOW_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_2_ID,   # tpl_2 趋势策略
    strategy_templates.TEMPLATE_3_ID,   # tpl_3 网格策略
})

# AI 交易市场：只放 AI 智能加仓策略。与带单市场互斥。
AI_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_1_ID,   # tpl_1 AI 智能加仓策略
})
```

> **依赖方向已核实**（防循环 import）：
> - `strategy_templates.py` 只 import 标准库 → 叶子模块，`follow_rules` 可直接 import，无环；
> - `follow_rules` → `group_rules` / `permissions`（都不反向依赖 `follow_rules`）→ 无环；
> - `group_dispatcher` 追加 `from . import follow_rules` → 无环。

### 2.2 纯函数（可单测）

```python
def template_in_market(template_id: object | None, market: frozenset[str]) -> bool:
    """该策略模版是否属于指定跟单市场；白名单命中才算。"""
    if not template_id:
        return False
    return str(template_id).strip().lower() in market


def market_for_template(template_id: object | None) -> frozenset[str]:
    """按分组绑定的模版决定它属于哪个跟单市场（两市场互斥、不重叠）。"""
    tid = str(template_id or "").strip().lower()
    if tid == strategy_templates.TEMPLATE_1_ID:
        return AI_MARKET_TEMPLATE_IDS
    if tid in (strategy_templates.TEMPLATE_2_ID, strategy_templates.TEMPLATE_3_ID):
        return FOLLOW_MARKET_TEMPLATE_IDS
    return frozenset()          # 未知模版：两个市场都不收（保守）
```

### 2.3 `followable_groups` 增加模版维度（A/B/C 的共同入口）

`followable_groups` 是 A/B/C 的**唯一**判定入口 —— 改这一个函数，三处一起生效。

```python
def followable_groups(
    groups: list[dict],
    leader_user_id: int,
    primary_node_id: Optional[str],
    *,
    market: frozenset[str] = FOLLOW_MARKET_TEMPLATE_IDS,
    strategy_templates_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """带单员名下所有可被跟单的分组（启用 + 含主节点 + 模版在市场白名单内）。

    market：跟单市场的模版白名单。默认 = 普通带单市场（tpl_2 + tpl_3）。
    strategy_templates_map：{strategy_id: template_id} 快照，由调用方批量预取
        （避免在循环里逐个查 Redis）。为 None 时跳过模版判定（仅供内部/测试用）。
    """
    if not primary_node_id:
        return []
    out = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        if not g.get("enabled", True):
            continue
        if not is_leader_source_group(g, leader_user_id, primary_node_id):
            continue
        sid = str(g.get("strategy_id") or "").strip()
        if not sid:
            continue                      # 未绑策略的分组没有可跟的模版
        if strategy_templates_map is not None:
            tid = strategy_templates_map.get(sid)
            if not template_in_market(tid, market):
                continue
        out.append(g)
    return out
```

### 2.4 分发层兜底（D：唯一硬边界）

```python
    async def _effective_with_follows(
        self, group: dict, nodemap: dict[str, dict], online_ids: set[str],
        *, signal: Optional[TradingSignal] = None,
        strategy: Optional[dict] = None,
    ) -> list[str]:
        """本次分发的有效节点，串起 跟单员准入 / 市场准入 / 节点定向。

        三层准入：
        1. 跟单员准入：带单员失去 follow_leader 能力 -> 退回 owner 口径；
        2. 市场准入：分组绑定的策略模版不在跟单市场白名单内
           -> 同样退回 owner 口径，跟单节点一律不接收新信号；
        3. 节点定向（node_ids）：带单关 = 只推主节点。
        """
        base = group_rules.effective_node_ids(group, nodemap, online_ids)
        if group_rules.has_followers(group):
            if not await self._leader_still_enabled(group):
                base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
            elif not follow_rules.template_in_market(
                (strategy or {}).get("template_id"),
                follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            ):
                # 该分组的策略模版不在带单市场内（AI 加仓等）：跟单节点不接收新信号
                base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
        if signal and getattr(signal, "node_ids", None):
            wanted = {str(n).strip().lower() for n in signal.node_ids if n}
            base = [nid for nid in base if nid.lower() in wanted]
        return base
```

**调用点**（`_dispatch_group` 已同时拿到 `group` 和 `strategy`，**零额外 IO**）：

```python
        effective = await self._effective_with_follows(
            group, nodemap, online_ids, signal=signal, strategy=strategy,
        )
```

> **注意**：上面的实现把 AI 分组判为「不在带单市场」→ 退回 owner 口径。
> **但 AI tab 的跟单员应当能收到 AI 分组的信号！**
> 所以实际实现必须**按市场分别处理**，见 §3.4。

### 2.5 【我的跟单】的原因文案

```python
def marketable_reason(
    group: dict,
    leader_has_capability: bool,
    primary_node_id: Optional[str],
    template_id: object | None = None,
    market: frozenset[str] = FOLLOW_MARKET_TEMPLATE_IDS,
) -> Optional[str]:
    """该分组当前是否还在指定市场；下架时返回人读原因，可跟单返回 None。"""
    if not leader_has_capability:
        return "该带单员已关闭带单，本分组不再下发新信号"
    if not group_contains_primary(group, primary_node_id):
        return "该带单员已更换主节点，本分组不再对外接单"
    if not template_in_market(template_id, market):
        return market_reject_reason(template_id, market)
    if not group.get("enabled", True):
        return "分组已禁用"
    return None
```

---

## 3. 【AI 交易】Tab 设计

### 3.1 页面定位

```
┌─ AI 交易 ────────────────────────────────────────────────┐
│ 跟随 AI 智能加仓机器人的加仓节奏。加仓参数由机器人主人掌握，│
│ 你的节点会用**自己的资金**执行他的加仓计划。               │
├──────────────────────────────────────────────────────────┤
│  左：机器人广场（列表）        右：详情 / 我的 AI 跟单     │
└──────────────────────────────────────────────────────────┘
```

### 3.2 与【带单】Tab 的对称关系

| 维度 | 【带单】Tab | 【AI 交易】Tab |
|---|---|---|
| 菜单码 | `follow` | `ai_trade`（新增） |
| 路径 | `/follow` | `/ai-trade` |
| 市场常量 | `FOLLOW_MARKET_TEMPLATE_IDS` | `AI_MARKET_TEMPLATE_IDS` |
| 白名单 | `tpl_2` + `tpl_3` | `tpl_1` |
| 机器人准入 | `follow_leader` | `ai_strategy` **且** `follow_leader`（决策 7） |
| 分组开关 | 无 | `accept_ai_follow`（默认关，决策 7） |
| 关系表 | `group_follower_node` | **同一张** |
| 分发链路 | `group_dispatcher` | **同一条** |
| 信号并发 | 支持 | **不支持**（D1） |
| 策略热推 | 无 | **有**（D2） |
| 跟单员控参 | 无 | 无（决策 4） |

### 3.3 菜单与能力注册（纯增量，零副作用）

**`backend/app/permissions.py`**：

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

```python
def can_host_ai_robot(p: Principal) -> bool:
    """能否作为【AI 交易】广场的机器人主人：需同时持有 ai_strategy 与 follow_leader。

    两个能力都要的语义：能配 AI 策略（ai_strategy）+ 愿意对外接单（follow_leader）。
    管理员直接放行（can_capability 内已含 is_admin 分支）。
    """
    return (can_capability(p, CAP_AI_STRATEGY)
            and can_capability(p, CAP_FOLLOW_LEADER))
```

> **`assignable=True`** —— 广场对所有登录用户开放；管理员通过**角色菜单**决定谁能看到这个 tab。
>
> **无需迁移**：`MENU_AI_TRADE` 因 `assignable=True` 自动进 `DEFAULT_USER_MENUS`，
> 且 `rbac_service.seed_rbac` 已有**幂等补齐**逻辑（存量 `user` 角色自动补上）。

### 3.4 ⚠️ 分发层的市场分派（**必须改，否则 AI 跟单收不到信号**）

§2.4 的实现有个**逻辑漏洞**：它把「不在带单市场」一律退回 owner 口径。
但 AI 分组的跟单员**应当**收到信号。

**正确做法**：分派依据「**该分组的模版属于哪个市场**」：

```python
# group_dispatcher._effective_with_follows 内
if group_rules.has_followers(group):
    if not await self._leader_still_enabled(group):
        base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
    else:
        # 该分组的模版决定它属于哪个跟单市场；
        # 两个市场互斥：AI 分组只认 AI 市场的跟单关系，带单分组只认带单市场的。
        group_market = follow_rules.market_for_template(
            (strategy or {}).get("template_id"),
        )
        allowed = _followers_in_market(group, group_market)
        if not allowed:
            base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
        else:
            base = [nid for nid in base if nid in allowed]
```

**关键设计问题**：分组缓存里的 `followers` 只记 `node_id`，**不记市场**。
如何判定「某个跟单关系属于哪个市场」？

**答案**：**不需要额外字段** —— 关系表里已经有 `group_id`，
而**分组只有一个模版** → 跟单关系的市场 = 它所属分组的模版的市场。
一个跟单关系行天然只属于一个市场。

所以 `followers` 里的每条关系，**只要它所在的分组在某个市场内，就属于那个市场**。
分发时的判定因此简化为：

> **分组在哪个市场 → 它的全部跟单关系就在哪个市场。**
> 跨市场污染**只可能**由「把 A 市场的关系写进 B 市场的分组」造成
> —— 这正是 E（`adopt-followers`）的锅，靠 §5 修掉即可。

**因此 §2.4 可以简化为**（比原方案更简单，且天然支持两个市场）：

```python
    async def _effective_with_follows(
        self, group: dict, nodemap: dict[str, dict], online_ids: set[str],
        *, signal: Optional[TradingSignal] = None,
        strategy: Optional[dict] = None,
    ) -> list[str]:
        """本次分发的有效节点，串起 跟单员准入 / 市场准入 / 节点定向。

        三层准入：
        1. 跟单员准入：带单员失去 follow_leader 能力 -> 退回 owner 口径；
        2. 市场准入：分组绑定的模版决定该分组属于哪个跟单市场；
           未知模版（两个市场都不收）-> 退回 owner 口径，跟单节点不接收新信号；
        3. 节点定向（node_ids）：带单关 = 只推主节点。
        """
        base = group_rules.effective_node_ids(group, nodemap, online_ids)
        if group_rules.has_followers(group):
            if not await self._leader_still_enabled(group):
                base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
            elif not follow_rules.market_for_template(
                (strategy or {}).get("template_id"),
            ):
                # 未知模版：两个市场都不收，跟单节点不接收新信号（保守）
                base = group_rules.owner_effective_node_ids(group, nodemap, online_ids)
        if signal and getattr(signal, "node_ids", None):
            wanted = {str(n).strip().lower() for n in signal.node_ids if n}
            base = [nid for nid in base if nid.lower() in wanted]
        return base
```

> **这个版本比原《带单市场过滤》方案更优**：
> - AI 分组的跟单员**能**收到信号（AI tab 可用）；
> - 带单分组的跟单员**照常**收到信号（回归无破坏）；
> - 未知模版保守拒绝（安全默认）。
>
> **前提**：写入路径（A/B/C/E）已确保 **AI 分组里不会有带单市场的关系**、反之亦然。
> 写入侧由 §3.5 / §5 保证。

### 3.5 AI 跟单的写入路径（join）

**`POST /api/ai-follow/join`**：

```python
    # 校验机器人主人：两能力都要
    principal = await rbac_service.load_principal(store, leader_username)
    if not permissions.can_host_ai_robot(principal):
        raise HTTPException(400, "该用户未开放 AI 跟单")

    # 校验分组在 AI 市场 + 开放了 AI 跟单
    followable = {
        g["group_id"]: g for g in follow_rules.followable_groups(
            all_groups, body.leader_user_id, leader_primary.get("node_id"),
            market=follow_rules.AI_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
        if g.get("accept_ai_follow")          # 决策 7 的显式开关
    }
```

> **`accept_ai_follow` 必须在写入侧校验**，不能只在展示侧过滤 ——
> 否则用户可以绕过 UI 直接调 API 加入未开放的分组。

### 3.6 分组可见性开关（决策 7，AI tab 必做）

> **这是 AI tab 与带单 tab 最大的运维差异**。
> 带单暴露一个闲置分组影响有限；但 **AI 加仓是持续加仓** ——
> 暴露一个参数激进的老分组 = 把跟单员的钱放进一个可能持续加仓到爆仓的计划。**代价远高于带单。**

**`backend/app/orm.py`**：

```python
    # AI 跟单开关：是否对外开放 AI 跟单（默认关）
    accept_ai_follow: Mapped[bool] = mapped_column(Boolean, default=False)
```

**`backend/app/db.py`** —— 照抄 `trend_risk_enabled` 的现成范式（已核实，`db.py:115-125`）：

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

**`backend/app/group_service.py`** —— **⚠️ 必须同步分组缓存 dict（易漏点）**：

```python
        "accept_ai_follow": bool(getattr(row, "accept_ai_follow", False)),
```

> **为什么必须改**：`follow_rules` / AI 广场判定读的是 **Redis 里的分组缓存 dict**，
> 不是 ORM 行。漏了这一处 → 字段永远 `False` → 分组永远不进广场，
> 且**不报错**（`getattr` 默认值兜底，症状是「功能就是不生效」）。
> 参考同族字段 `signal_concurrent_enabled` / `trend_risk_enabled` / `limit_watch_enabled`
> 在 `group_service.py:52-54` 都有对应行。

**`backend/app/models.py`** —— `GroupUpdate` 加字段：

```python
    accept_ai_follow: Optional[bool] = None
```

**`backend/app/group_service.py`** —— `create_group` / `update_group` 落库：

```python
    # create_group
    accept_ai_follow=bool(payload.accept_ai_follow),
    # update_group
    if patch.accept_ai_follow is not None:
        row.accept_ai_follow = bool(patch.accept_ai_follow)
```

**判定串联**（AI 机器人准入的**第 5 个条件**）：

```
AI 机器人分组 = 含主节点 ∧ enabled ∧ 绑定 tpl_1 ∧ 策略启用 ∧ accept_ai_follow = TRUE
```

### 3.7 AI tab 的 API 路由（`/api/ai-follow`）

> **不复用 `/api/follow/*` 的路由**，理由：
> ① 审计 `category` 可区分（`follow` vs `ai_follow`）；
> ② 未来 AI 跟单要独立限流 / 独立统计时切开更容易；
> ③ 两个 tab 的 `unselectable_reason` 文案完全不同。
> **但内部完全复用 `follow_service` 的读写与 `follow_rules` 的判定。**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai-follow/robots` | 机器人广场列表 |
| GET | `/api/ai-follow/robots/{leader_user_id}` | 机器人详情（可跟分组 + 我的可跟节点） |
| GET | `/api/ai-follow/me` | 我的 AI 跟单 |
| POST | `/api/ai-follow/join` | 加入 AI 跟单 |
| POST | `/api/ai-follow/leave` | 移出 AI 跟单 |

**机器人列表**：

```python
@router.get("/robots", response_model=AiRobotListResponse)
async def list_ai_robots(
    q: str = "",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiRobotListResponse:
    """AI 机器人广场：持 ai_strategy + follow_leader + 有主节点 + 至少一个已开放 AI 跟单的 tpl_1 分组。"""
    users = await user_service.list_users()
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
        groups = [
            g for g in follow_rules.followable_groups(
                all_groups, uid, primary["node_id"],
                market=follow_rules.AI_MARKET_TEMPLATE_IDS,
                strategy_templates_map=tmpl_map,
            )
            if g.get("accept_ai_follow")
        ]
        if not groups:
            continue          # 没有已开放 AI 跟单的分组 -> 不进广场
        out.append({...})
```

**`_ai_host_ids_with_capability` 照抄 `_leader_ids_with_capability` 的现成范式**
（`follow_api.py:114-126`，已核实）—— 同样注意「每个用户查一次 Redis」的 N+1 已存在，
AI 侧沿用同一形状，不引入新的性能特征：

```python
async def _ai_host_ids_with_capability(store: RedisStore, users: list) -> set[int]:
    """持有 ai_strategy 且 follow_leader 的用户 ID（AI 机器人主人）。

    镜像 follow_api._leader_ids_with_capability：只取启用的用户，逐个 load_principal。
    """
    out: set[int] = set()
    for u in users:
        if not u.is_active:
            continue
        principal = await rbac_service.load_principal(store, u.username)
        if principal and permissions.can_host_ai_robot(principal):
            out.add(u.id)
    return out
```

> **能力判定复用**：`can_host_ai_robot` 已封装「两能力都要」，
> 管理员由 `can_capability` 内的 `is_admin` 分支自动放行。

**机器人详情**（与 `get_leader_detail` 同构，差异）：

```python
        detail_groups.append({
            ...
            # AI 特有字段（决策 4 的对价：必须公开风险参数）
            "risk_amount": _first_rule_value(strategy, "risk_amount"),
            "max_allow_num": _first_rule_value(strategy, "max_allow_num"),
            "total_lot_limit": _first_rule_value(strategy, "total_lot_limit"),
            "entry_mode": _first_rule_value(strategy, "entry_mode"),
            # AI 分组强制单槽
            "signal_concurrent_enabled": False,
            "concurrent_unavailable_reason": "AI 智能加仓策略不支持信号并发，同一节点同时只跑一单",
        })
```

> **`risk_amount` / `max_allow_num` / `total_lot_limit` 必须公开**（决策 4 的对价）：
> 跟单员不控手数，就必须能**事先看清**带单员的风险参数，否则是盲跟。
> 这些是带单员自己的策略配置，公开它不泄露节点 / 账户信息。

**移出**：`leave` 直接复用 `follow_api._stop_following_subtasks`，
`close_tasks` 默认 `False`（= 决策：在跑任务等待执行结束，**现有默认行为**）。

### 3.8 前端设计

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

**`frontend/src/App.vue`**：导航项 `{ path: '/ai-trade', label: 'AI 交易', icon: '🤖' }`。

**`AiTradeView.vue`** —— 机器人广场：

```
┌ AI 交易 ─────────────────────────────────────────────────┐
│ ⚠ AI 加仓策略会持续累积仓位，风险随行情推移增长。          │
│   你的节点将用自己的资金执行机器人的加仓计划。              │
├───────────────────────────────────────────────────────────┤
│ ┌ 机器人广场 ──────────────────────────────────────────┐  │
│ │ ● Alice   主节点：主节点A（MT5 10023 · 在线）         │  │
│ │   AI 分组 2 个 · 被 5 个节点跟随 · 加仓中 2 个         │  │
│ │   批次上限 3 · 总手数上限 0.5                        │  │
│ └──────────────────────────────────────────────────────┘  │
│ ┌ Bob     主节点：主节点X（MT5 10025 · 离线）            │  │
│ │   AI 分组 1 个 · 尚无人跟随                            │  │
│ └──────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

**卡片字段**：

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

> **`max_allow_num` / `total_lot_limit` 在列表卡片就展示**（决策 4 的对价）：
> 用户选机器人时第一眼就要能判断风险量级，不能点进详情才知道。

**跟单弹窗（双区选择）**：

```
┌ 跟单 · Alice ─────────────────────────────────┐
│ 机器人：主节点A（MT5 10023 · 在线）             │
├ 可开放的 AI 分组（可多选）───────────────────┤
│ ☑ ai-Alice-10023-XAUUSD                        │
│    XAUUSD · AI智能加仓策略 · 单槽执行            │
│    批次上限 3 · 总手数上限 0.5 · 手数不缩放     │
│ ☐ ai-Alice-10023-EURUSD                         │
│    ⚠ 本分组未开放 AI 跟单（机器人主人未开启）   │
├ 我的节点（可多选）─────────────────────────────┤
│ ☑ 节点B（MT5 20001 · 在线）                     │
│ ⊘ 主节点X（MT5 20000）  主节点不可跟单          │
├─────────────────────────────────────────────────┤
│ ⚠ 加入后将按机器人的加仓参数执行，手数不缩放；   │
│   机器人主人修改策略会实时同步到你的在跑任务；   │
│   移出后不自动平仓，在跑任务会继续运行至结束。   │
│                          [取消]      [确认跟单] │
└─────────────────────────────────────────────────┘
```

**三条 AI 特有的风险提示（`FollowView` 无对应文案）**：

1. `手数不缩放` —— 你将按机器人设定的完整手数执行；
2. `策略实时同步` —— 机器人主人修改加仓参数会**立即作用到你正在跑的任务**，你无法锁定；
3. `移出不自动平仓` —— 在跑任务继续运行至结束（决策 7）。

**我的 AI 跟单**（结构与带单 tab 一致，外加轮询）：

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

> **为什么轮询不用 WS**：M3 —— `event_visible` 只认 owner，跟单员收不到自己节点的事件。
> 带单 tab 同样如此，一期统一用轮询（§8 二期再考虑事件可见性扩展）。

**分组行额外展示**：

```
ai-Alice-10023-XAUUSD
  批次上限 3 · 总手数上限 0.5      当前在跑 1 单（3/3 批次已用）
  节点B（20001）  进行中 1 个任务    [移出]
```

> **`批次已用 / 批次上限`** 是 AI 跟单员最需要盯的指标 —— 它决定还剩多少加仓空间（风险敞口）。

### 3.9 类型与 API 层

**`frontend/src/api/types.ts`**：新增 `AiRobotSummary` / `AiRobotDetailResponse` / `AiMeResponse`
（`FollowGroupDetail` 上加 §3.7 的 AI 字段）。

**`frontend/src/api/aiFollow.ts`**：`fetchAiRobots` / `fetchAiRobotDetail` /
`fetchMyAiFollowings` / `joinAiFollow` / `leaveAiFollow`（形状与 `follow.ts` 完全一致）。

---

## 4. 【带单】Tab 的对应改动

> AI tab 的市场过滤**依赖**这里的参数化，所以必须先做。

### 4.1 详情页的置灰展示（B）

改造后 AI 分组会从带单详情页消失 —— 符合决策 3 的「软边界」，
但用户会困惑「我之前跟的 AI 分组去哪了」。

**建议**：详情接口**额外**返回「已下架分组」列表，标灰不可选：

```python
# FollowDetailOut 新增字段
class FollowDetailOut(BaseModel):
    leader: FollowLeaderSummary
    groups: list[FollowGroupDetail]        # 可选的（市场内）
    hidden_groups: list[FollowGroupDetail]  # 新增：不在市场内的（AI 分组），置灰
    my_nodes: list[dict] = Field(default_factory=list)
```

`hidden_groups` 的 `selectable=False`，`unselectable_reason` = 市场原因文案。

> **取舍**：这让接口契约变复杂。**最小实现**是 AI 分组直接不返回，
> 【我的跟单】里保留存量条目 + 原因文案。
> **本方案按「加 hidden_groups」设计** —— 它直接支撑决策 3（人工清理需要看得见），
> 且前端改动只是多渲染一个折叠区。

### 4.2 前端

**`frontend/src/views/FollowView.vue`**：

```typescript
const selectableGroups = computed(() => detail.value?.groups ?? [])
const hiddenGroups = computed(() => detail.value?.hidden_groups ?? [])
```

```vue
<!-- 可跟单分组 -->
<h4 class="sec-title">可跟单分组（{{ selectableGroups.length }}）</h4>
<ul class="pick-list">
  <li v-for="g in selectableGroups" :key="g.group_id" class="pick-item"> ... </li>
</ul>

<!-- 不在本市场（AI 加仓等）：仅提示，不可选 -->
<template v-if="hiddenGroups.length">
  <h4 class="sec-title">
    不在普通跟单市场（{{ hiddenGroups.length }}）
    <span class="muted" style="font-weight:400">AI 加仓策略请到【AI 交易】页跟单</span>
  </h4>
  <ul class="pick-list">
    <li v-for="g in hiddenGroups" :key="g.group_id" class="pick-item disabled">
      <span class="pick-name">{{ g.name }}</span>
      <span class="muted">{{ g.strategy_name }}</span>
      <span class="tag amber">{{ g.unselectable_reason }}</span>
    </li>
  </ul>
</template>
```

**【我的跟单】的存量条目** —— 当前代码写死了「该带单员已关闭带单」，需改成读实际值：

```vue
<span v-if="!f.marketable" class="tag amber">{{ f.unmarketable_reason || '该带单员已关闭带单' }}</span>
```

**`frontend/src/api/types.ts`**：

```typescript
export interface FollowDetailResponse {
  leader: FollowLeaderSummary
  groups: FollowGroupDetail[]
  /** 不在普通跟单市场内的分组（AI 加仓策略等）：仅提示，不可选 */
  hidden_groups?: FollowGroupDetail[]
  my_nodes: FollowNodeBrief[]
}
```

---

## 5. ⚠️ 必修：跨市场静默污染（M4）

### 5.1 问题

`POST /groups/{id}/adopt-followers` 的来源筛选是：

```402:415:backend/app/groups.py
    all_groups = await store.all_groups()
    # 用 is_leader_source_group（owner + 含主节点）而非 followable_groups，
    # 后者带 enabled 过滤 —— 继承与「该分组是否对外接单」无关，故不过滤启用状态。
    sources = [
        g for g in (all_groups or [])
        if isinstance(g, dict)
        and str(g.get("group_id") or "") != str(group_id)
        and follow_rules.is_leader_source_group(g, owner, primary)
```

**完全不看模版**。后果：

> 机器人主人 A 既开带单（网格分组 G_grid，跟单员 B 已加入）又开 AI 分组 G_ai。
> A 在 G_ai 上触发继承 → **B 的节点被静默拉进 G_ai**，
> B 就此在毫不知情的情况下开始用自己的钱执行 A 的 AI 加仓计划。
> **这违反决策 6（展示单位=主节点）与用户「不控手数」的知情前提。**

### 5.2 修复

来源筛选**按目标分组的模版决定继承哪个市场的跟单节点**：

```python
    all_groups = await store.all_groups()
    # 跨市场隔离：只继承**与目标分组同市场**的来源分组的跟单节点。
    # 目标分组的模版决定它属于哪个跟单市场（两个市场互斥）。
    target_tid = tmpl_map.get(str(target.get("strategy_id") or ""), "")
    market = follow_rules.market_for_template(target_tid)
    if not market:
        # 目标分组未绑策略 / 未知模版：无从判定市场，不做继承（保守）
        sources = []
    else:
        sources = [
            g for g in (all_groups or [])
            if isinstance(g, dict)
            and str(g.get("group_id") or "") != str(group_id)
            and follow_rules.is_leader_source_group(g, owner, primary)
            and follow_rules.template_in_market(
                tmpl_map.get(str(g.get("strategy_id") or "")), market,
            )
        ]
```

**需要预取 `tmpl_map`**（`groups.py` 里新增一次批量读取）：

```python
    strategies = await follow_service._strategies_of(all_groups, store)  # 或复用 follow_api 的
    tmpl_map = {sid: str(st.get("template_id") or "") for sid, st in strategies.items()}
```

> **注意**：`follow_api._strategies_of(groups, store)` 的参数顺序是 **(groups, store)**，别写反。

### 5.3 附带修掉一个同源问题

原实现**忽略 `enabled`**（继承计划文档「决策 1」故意为之）。
但**跨市场继承**必须收窄。两种语义要分开：

| 场景 | 语义 | 理由 |
|---|---|---|
| **同市场继承**（网格 → 网格） | 保持现有「忽略 enabled」，**不破坏已上线行为** | 继承与「该分组是否对外接单」无关 |
| **跨市场**（带单 ↔ AI） | **不允许** | 由 `market` 收窄天然排除 |

> 上面的实现**只按模版收窄，不动 enabled 语义** —— 同市场继承行为完全不变。

### 5.4 其它潜在跨市场入口

| 入口 | 是否会跨市场 | 说明 |
|---|---|---|
| `adopt-followers` | **会** | 必修，见 §5.2 |
| `POST /api/follow/join` | 不会 | 收窄到带单市场后，AI 分组写不进来 |
| `POST /api/ai-follow/join` | 不会 | 收窄到 AI 市场 + `accept_ai_follow` 校验 |
| 分组成员编辑（owner 加自己的节点） | 不会 | owner 成员与跟单成员是**两张表**，互斥 |
| `remove_node_followings`（节点生命周期） | 无关 | 按 `node_id` 全表删，**不分市场** —— 正确（一个节点不能既是带单跟单又是 AI 跟单而互不知情） |

---

## 6. 实施步骤

> 后端先行，每步可独立验证。

### 阶段 A：注册（纯增量，零副作用）

| 步 | 文件 | 内容 |
|---|---|---|
| A1 | `permissions.py` | `MENU_AI_TRADE` + MenuDef + `can_host_ai_robot()` |
| A2 | `menus.ts` / `router/index.ts` / `App.vue` | 菜单路径 / 路由 / 导航项 |
| A3 | `orm.py` + `db.py` | `NodeGroup.accept_ai_follow` 列 + 幂等迁移 |
| A4 | `group_service.py` + `models.py` | 缓存 dict 加字段 + `GroupUpdate` 加字段 + 落库 |

**A3 验证**：`SELECT accept_ai_follow FROM node_group LIMIT 1` → 0
**A4 验证**（**最易漏**）：改分组开关后，`GET /api/groups/{id}` 返回的 `accept_ai_follow` 跟随变化
（若恒为 `False`，说明缓存 dict 那行没加）

**回滚**：删列 / 删常量（前端菜单项可留空页）

### 阶段 B：市场参数化（AI tab 的前置）

| 步 | 文件 | 内容 |
|---|---|---|
| B1 | `follow_rules.py` | `FOLLOW_MARKET_TEMPLATE_IDS` / `AI_MARKET_TEMPLATE_IDS` + `template_in_market()` + `market_for_template()` + `market_reject_reason()` |
| B2 | `follow_rules.py` | `followable_groups` 加 `market` + `strategy_templates_map`；`marketable_reason` 加 `template_id` + `market` |
| B3 | `follow_api.py` | 新增 `_template_ids_of` 转换 + **3 处调用点**接入（A/B/C） |
| B4 | `follow_api.py` | 【我的跟单】原因文案（`list_my_followings`） |
| B5 | `group_dispatcher.py` | `_effective_with_follows` 加 `strategy` 参数 + 市场准入降级分支（§3.4 简化版） |
| B6 | `groups.py` | **修 `adopt-followers` 跨市场污染**（§5.2） |

**B5 验证**（关键）：
```
1. tpl_1 分组 + AI 跟单节点 → 触发 → AI 跟单节点**正常**产生子任务
2. tpl_2 分组 + 带单跟单节点 → 触发 → 跟单节点**正常**产生子任务（回归）
3. 未知模版分组 + 跟单节点 → 触发 → 跟单节点**不**产生子任务（保守）
```

**B6 验证**（关键）：
```
1. 带单员有网格分组（跟单员B已加入）+ AI 分组 → 在 AI 分组触发继承
   → B 的节点**不**被拉进 AI 分组
2. 带单员有网格分组 + 另一个网格分组 → 在后者触发继承
   → B 的节点**仍然**被继承（**不破坏既有行为**）
```

**回滚**：撤销 B1~B6

### 阶段 C：AI tab 后端

| 步 | 文件 | 内容 |
|---|---|---|
| C1 | `models.py` | `AiRobotSummary` / `AiRobotDetailResponse` / `AiMeResponse` |
| C2 | `follow_api.py` | 抽出 `_strategies_of` / `_template_ids_of` / `_primary_of` 供复用（改为模块级可导入） |
| C3 | **`ai_follow_api.py`（新建）** | 5 个路由，全部复用 `follow_service` / `follow_rules` |
| C4 | `main.py` | 注册 `ai_follow_api.router` |
| C5 | `groups.py` / `GroupsView.vue` | 分组编辑弹窗加 `accept_ai_follow` 开关（**仅 `tpl_1` 分组显示**） |

**C3 验证**：
```bash
curl http://<host>/api/ai-follow/robots -H "Authorization: Bearer <token>"
# 只含 ai_strategy + follow_leader 都有、且有已开放 AI 跟单的 tpl_1 分组的用户
```

### 阶段 D：前端

| 步 | 文件 | 内容 |
|---|---|---|
| D1 | `api/types.ts` | AI 类型 + `hidden_groups?` |
| D2 | `api/aiFollow.ts` | 5 个请求函数 |
| D3 | `views/AiTradeView.vue` | 广场 + 详情 + 我的跟单（复制 `FollowView` 结构后改差异点） |
| D4 | `views/FollowView.vue` | 详情页分组区拆分 + 我的跟单灰标读实际原因 |

**D3 验证**：`npm run build` 无类型错误；浏览器里完整走一遍加入 / 移出

### 阶段 E：测试

| 文件 | 用例 | 期望 |
|---|---|---|
| `test_follow_api.py` | 带单员有 `tpl_1` 分组 → `GET /api/follow/leaders` | 该分组不计入 `followable_group_count` |
| | 带单员**只有** `tpl_1` 分组 | 该带单员**不出现在**列表 |
| | `POST /api/follow/join` 加入 `tpl_1` 分组 | 400，被拒 |
| | `GET /api/follow/leaders/{id}` | `groups` 不含 AI 分组；`hidden_groups` 含它且 `selectable=False` |
| | `GET /api/follow/me` 中 AI 分组的存量关系 | 仍返回，`marketable=false`，原因指向【AI 交易】 |
| | `tpl_2` / `tpl_3` 分组 | 行为完全不变（回归） |
| `test_ai_follow_api.py`（新建） | 只有 `ai_strategy` 无 `follow_leader` | 不出现在广场 |
| | 只有 `follow_leader` 无 `ai_strategy` | 不出现在广场 |
| | 两能力都有，但无 `tpl_1` 分组 | 不出现在广场 |
| | 两能力都有，`tpl_1` 分组但 `accept_ai_follow=false` | 不出现在广场 |
| | `tpl_1` 分组 + `accept_ai_follow=true` | 出现在广场 |
| | AI 分组加入跟单 | 200；`tpl_2` 分组加入 AI 跟单 → 400 |
| | 带单 tab 加入 `tpl_1` 分组 | 400（市场互斥） |
| | `tpl_1` 热推 | 跟单员在跑任务被 `strategy_update` 改写（决策 5 核心验证） |
| `test_group_dispatch_follow.py` | AI 分组 + AI 跟单节点 → 触发 | 跟单节点**正常**产生子任务 |
| | 带单分组 + 带单跟单节点 → 触发 | 跟单节点**正常**产生子任务（回归） |
| | 未知模版分组 + 跟单节点 → 触发 | 跟单节点**不**产生子任务（保守） |
| | 带单节点设为 AI 跟单节点 | `remove_node_followings` 清掉关系 |
| `test_groups.py` | `adopt-followers`：AI 分组不继承网格分组的跟单节点 | ✅ 跨市场隔离生效 |
| | `adopt-followers`：网格分组仍继承另一网格分组的跟单节点 | ✅ 同市场仍继承 |
| 回归 | `test_follow_api.py` / `test_group_dispatch_follow.py` / `test_group_dispatch.py` / `test_group_api.py` | 全绿 |

---

## 7. 改造后的完整判定链路

```
                    ┌──────────────────────────────────────┐
                    │  分组 G（owner + 含主节点）           │
                    └──────────────┬───────────────────────┘
                                   │ strategy_id → strategy → template_id
                                   ▼
                    ┌──────────────────────────────────────┐
                    │ template_id → market_for_template()    │
                    │  tpl_1        → AI 市场                │
                    │  tpl_2/tpl_3  → 带单市场               │
                    │  其它/未绑    → 空集（保守）           │
                    └──────────────┬───────────────────────┘
                                   │
    ┌──────────────┬───────────────┼───────────────┬──────────────┐
    ▼              ▼               ▼               ▼              ▼
【带单】列表/详情  join 写入      AI 广场/详情     AI join 写入   分发层
(A/B)            (C)            (AI)             (AI)          (D)
followable_      followable_    followable_      followable_   _effective_
groups           groups         groups +         groups +      with_follows
(FOLLOW market)  (FOLLOW)       accept_ai_       accept_ai_    (按分组模版
→ AI 分组不出现  → AI 写不进     follow=TRUE      follow=TRUE   分派市场)
→ 进 hidden_      (400)         → 不进广场       → AI 写不进
  groups 置灰
```

**一致性保证**：A/B/C 与 AI 侧共享 `followable_groups`（唯一判定入口）；
D 用同一份 `market_for_template`；E 用同一份白名单收窄。
**改一次白名单，五个入口同时生效。**

---

## 8. 边界与风险

| 场景 | 行为 | 说明 |
|---|---|---|
| **AI 分组里的存量跟单关系** | 关系保留；**不再收到新信号**（D 兜底）；【我的跟单】里可见 + 原因提示 | 决策 3：人工清理 |
| **AI 分组已被禁用** | 既不在 `groups` 也不在 `hidden_groups` | 见「已知不完美」 |
| **带单员只有 AI 分组** | 该带单员**不出现在**【带单】列表 | 正确 —— 他在带单市场没有可跟的东西 |
| **带单员有 AI + 趋势/网格分组** | 带单 tab 只展示后者；AI 分组进 `hidden_groups`；AI tab 只展示前者 | 正确 |
| **带单员失去 `follow_leader`** | 立刻下架；关系保留；**在跑任务跑完**（决策 7） | `_leader_still_enabled` 降级路径 |
| **带单员失去 `ai_strategy`** | AI 广场下架；关系保留；在跑任务跑完 | `can_host_ai_robot` 判定 |
| **AI 分组的 CLOSE 信号** | 仍按 `group_ids` 终止子任务 | CLOSE 是终止指令，不受市场准入影响 |
| **AI 分组的 owner 自己** | 不受影响 —— 降级只影响跟单成员 | 关键：过滤的是「跟单」，不是「分组」 |
| **策略被删除（`strategy_id` 悬空）** | 不在 `tmpl_map` → 判为不在市场 → 不可跟 | 保守安全 |
| **未来新增模版 `tpl_4`** | 默认**不**进入任何市场（白名单） | 需显式加入 |
| **一个节点同时是带单跟单 + AI 跟单** | 允许（不同分组） | 节点可以是多个分组的跟单员 |

### 已知取舍

1. **被禁用的 AI 分组在带单详情页完全不可见** —— `followable_groups` 统一带 `enabled` 过滤。
   若要「禁用的 AI 分组也提示去哪跟」，需另写一个不看 `enabled` 的变体。**当前不写**
   （禁用的分组本就不该展示，收益有限）。
2. **`marketable_reason` 的判断顺序** —— 市场判定放在「能力 / 主节点」之后、「分组已禁用」之前。
   若某 AI 分组同时「换了主节点」和「是 AI 策略」，显示主节点原因（更根本）。
3. **存量关系不自动清理** —— 决策 3 明确「自行移除」。
   分发层兜底（D）保证这些关系**不会产生任何交易**。
4. **跟单员收不到实时 WS 事件**（M3）—— 改用 45s 轮询。
5. **AI tab 的统计指标是占位** —— 收益 / 胜率 / 加仓次数当前无数据源，
   先返回 0，后续由历史订单数据驱动（与 `FollowView` 的 mock 同源）。

---

## 9. 文件清单

**修改**

| 文件 | 改动 | 规模 |
|---|---|---|
| `backend/app/permissions.py` | `MENU_AI_TRADE` + MenuDef + `can_host_ai_robot()` | 小 |
| `backend/app/follow_rules.py` | 市场常量 + 3 个纯函数 + `followable_groups` 加模版维度 + `marketable_reason` 加模版维度 | 中 |
| `backend/app/follow_api.py` | `_template_ids_of` + 3 处调用点接入 + `hidden_groups` + 【我的跟单】原因 | 中 |
| `backend/app/group_dispatcher.py` | `_effective_with_follows` 加 `strategy` 参数 + 市场准入降级分支 | 小 |
| `backend/app/groups.py` | **修 `adopt-followers` 跨市场污染** | 小 |
| `backend/app/orm.py` | `NodeGroup.accept_ai_follow` 列 | 极小 |
| `backend/app/db.py` | `_migrate_node_group_accept_ai_follow` | 极小 |
| `backend/app/group_service.py` | 缓存 dict + create/update 落库 | 小 |
| `backend/app/models.py` | `GroupUpdate.accept_ai_follow` + `FollowDetailOut.hidden_groups` + AI 模型 | 中 |
| `backend/app/main.py` | 注册 `ai_follow_api.router` | 极小 |
| `frontend/src/constants/menus.ts` | `ai_trade` 菜单路径 | 极小 |
| `frontend/src/router/index.ts` | `/ai-trade` 路由 | 极小 |
| `frontend/src/App.vue` | 导航项 | 极小 |
| `frontend/src/api/types.ts` | AI 类型 + `hidden_groups?` | 小 |
| `frontend/src/api/aiFollow.ts` | **新建** 5 个请求函数 | 小 |
| `frontend/src/views/AiTradeView.vue` | **新建** 广场 + 详情 + 我的跟单 | 大 |
| `frontend/src/views/FollowView.vue` | 详情页分组区拆分 + 我的跟单灰标 | 中 |
| `frontend/src/views/GroupsView.vue` | `accept_ai_follow` 开关 | 小 |

**新增**

| 文件 | 内容 |
|---|---|
| `backend/app/ai_follow_api.py` | AI tab 的 5 个路由 |
| `backend/tests/test_ai_follow_api.py` | AI tab 用例 |
| `backend/tests/test_follow_api.py` | 追加 6 个用例 |
| `backend/tests/test_group_dispatch_follow.py` | 追加 3 个用例 |
| `backend/tests/test_groups.py` | 追加 2 个用例 |

**不改动**（已核查，确认无需动）

- `backend/app/follow_service.py` —— 关系读写不变（`close_tasks=False` 已是默认）
- `backend/app/strategy_templates.py` —— 模版定义不变
- `backend/app/strategies.py` —— 策略热推只作用于 `tpl_1`，与市场准入**正交**
- `backend/app/ws_gateway.py` / `connections.py` —— 事件可见性扩展属二期
- `backend/app/node_client`（客户端侧）—— 任务执行逻辑不变

---

## 10. 待你确认的剩余两点

### 10.1 机器人主人失去能力后的处理（已按你的决策定稿，但请确认措辞）

本方案沿用带单语义：

| 情况 | 处理 |
|---|---|
| 失去 `follow_leader` | 立刻下架；关系保留；**在跑任务跑完**（node_client 负责执行到底） |
| 失去 `ai_strategy` | AI 广场下架；关系保留；在跑任务跑完 |
| 关闭 `accept_ai_follow` | 立刻从广场消失；**已有跟单关系保留**；在跑任务跑完 |

> **「已有跟单关系保留」**是关键：关闭开关只是「不再对外接新单」，
> 不是「把已跟的人踢走」。若你希望关闭开关就终止全部在跑任务，需要额外实现。

### 10.2 二期候选

| 项 | 说明 |
|---|---|
| 事件可见性扩展 | `resolve_owner` → `resolve_access`（带 followers），让跟单员收到自己节点事件，替代轮询 |
| 跟单员手数缩放 | 决策 4 的反面：若客户投诉风险不可控，加 `group_follower_node.lot_scale` |
| AI 跟单统计 | 收益 / 胜率 / 加仓次数，由历史订单数据驱动 |
| 分组级 AI 跟单上限 | 限制单个机器人能容纳的跟单节点数（防过度集中） |
| 灰度开关 | `system_settings.ai_follow_feature`，与带单的 `follow_feature` 同范式 |
| 存量关系清理脚本 | 导出 `tpl_1` 分组下的 `group_follower_node` 行给管理员确认后删除 |
