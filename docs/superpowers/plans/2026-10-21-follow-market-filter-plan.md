# 带单市场过滤「AI 智能加仓策略」实施方案

> **一句话目标**：【带单】Tab 只做「市价开仓 / 现价开仓 / 网格策略」的跟单；
> 绑定 **AI 智能加仓策略（模版1 / `tpl_1`）** 的分组一律不进入带单市场，
> 留给后续独立的【AI 交易】Tab 专门承载。

> **已确认的三条前置决策**（本次方案据此展开）：
>
> | # | 决策 | 含义 |
> |---|---|---|
> | 1 | **白名单过滤** | 带单市场只放 `tpl_2`（趋势策略）+ `tpl_3`（网格策略）；未来新增模版**默认不外露** |
> | 2 | **参数化「跟单市场」** | 不把 `tpl_1` 硬编码进各处判断，而是把「市场」抽象成参数，【AI 交易】Tab 后续复用同一套关系表与分发链路 |
> | 3 | **存量关系由管理员自行移除** | 本方案**不做**自动迁移 / 自动删除；只保证「AI 分组不再被新跟单、不再下发信号」，存量行保留在表里等人工清理 |

---

## 1. 现状核对（已核实的代码事实）

### 1.1 策略模版与分组绑定关系

| 项 | 值 | 位置 |
|---|---|---|
| AI 智能加仓策略 | `tpl_1` / `TEMPLATE_1_ID` / `AI_STRATEGY_TEMPLATE_ID` | `strategy_templates.py:103`、`permissions.py:88` |
| 趋势策略（市价/限价开仓） | `tpl_2` / `TEMPLATE_2_ID` | `strategy_templates.py:105` |
| 网格策略 | `tpl_3` / `TEMPLATE_3_ID` | `strategy_templates.py:107` |
| 分组 → 策略 | **一对一**（`node_group.strategy_id` 有 unique 索引） | `orm.py:272`、`db.py:104` |
| 分组 → 策略 的 owner | 必须与分组同 owner | `group_service.validate_strategy_binding` |

**关键推论**：带单源分组 = 带单员名下「含其主节点」的分组，它通过 `strategy_id` 绑定**唯一一个**策略，
所以「这个分组能不能跟单」等价于「它绑定的那个策略的 `template_id` 是不是白名单里的模版」。

### 1.2 当前「带单市场」的判定链路（**没有模板维度 —— 这就是要补的洞**）

`follow_rules.followable_groups` 是唯一的可跟单分组判定：

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

它只判「启用 + 含主节点 + owner 匹配」，**不看策略模版**。
所以 `tpl_1` 的分组现在会正常出现在【带单】列表里、并可被 join —— 正是要修的地方。

### 1.3 四个必须同时收口的入口

过滤只做前端是不够的（前端只是体验，后端才是边界）。四条路径必须口径一致：

| # | 路径 | 现状 | 后果 |
|---|---|---|---|
| **A** | `GET /api/follow/leaders` → `followable_groups` | 不过滤模版 | **AI 分组出现在带单员列表** |
| **B** | `GET /api/follow/leaders/{id}` → `followable_groups` | 不过滤模版 | **AI 分组出现在详情页可选列表** |
| **C** | `POST /api/follow/join` → `followable_groups` | 不过滤模版 | **能把节点加进 AI 分组**（漏洞根源） |
| **D** | `group_dispatcher._effective_with_follows` | 只判「带单员是否还有能力」 | **AI 分组里的跟单节点仍会收到信号**（兜底必须补） |

> **D 是最关键的一条**：即使 A/B/C 都改了，只要存量关系还在，D 不过滤，
> 跟单节点依然会按 AI 加仓策略执行 —— 这正是决策 3（存量人工清理）成立的前提：
> D 负责「立刻停止下发」，人工清理负责「清掉关系本身」。

### 1.4 分发层已有的能力降级机制（可直接复用）

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
        if signal and getattr(signal, "node_ids", None):
            wanted = {str(n).strip().lower() for n in signal.node_ids if n}
            base = [nid for nid in base if nid.lower() in wanted]
        return base
```

已有「带单员失去能力 → 退回 owner 口径」的降级路径。
本次改造**沿用同一形状**，把「市场准入」也纳入降级条件。

### 1.5 策略快照的取值路径（过滤要用的 `template_id` 从哪来）

| 场景 | 取值方式 | 是否已有现成批量接口 |
|---|---|---|
| 列表 / 详情 / join（A/B/C） | `store.get_strategy(g["strategy_id"])` → `["template_id"]` | ✅ `follow_api._strategies_of` 已有批量封装 |
| 分发层（D） | `_candidate_groups` 已经把 `strategy` 加载出来了 | ✅ 直接用 `strategy["template_id"]`，**零额外 IO** |

> **注意**：`group` 缓存 dict 里**没有** `template_id` 字段，只有 `strategy_id`。
> 所以分发层要用 `strategy["template_id"]`（`_candidate_groups` 已经拿到了），
> 而不是在 `_effective_with_follows` 里再查一次 Redis。

---

## 2. 设计：把「跟单市场」参数化

### 2.1 新增一个市场概念

**决策 2 的落点**：不把 `tpl_1` 散落在各处 `if`，而是定义**市场** = 「允许被跟单的策略模版集合」。

```python
# backend/app/follow_rules.py（新增）

# 带单市场（普通跟单）的模版白名单：只放趋势策略与网格策略。
# AI 智能加仓策略（tpl_1）不在其中 —— 它归【AI 交易】Tab 承载。
# 白名单而非黑名单：未来新增模版默认不外露，需显式加入才进入带单市场。
FOLLOW_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_2_ID,   # tpl_2 趋势策略（市价/现价开仓）
    strategy_templates.TEMPLATE_3_ID,   # tpl_3 网格策略
})

# AI 交易市场（后续 Tab 用）：只放 AI 智能加仓策略。与带单市场互斥。
AI_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_1_ID,   # tpl_1 AI 智能加仓策略
})
```

> **依赖方向已核实**（防循环 import）：
> - `strategy_templates.py` 只 import 标准库 → 是**叶子模块**，`follow_rules` 可直接 import，无环；
> - `follow_rules` → `group_rules` / `permissions`（都不反向依赖 `follow_rules`）→ 无环；
> - `group_dispatcher` 追加 `from . import follow_rules` → `follow_rules` 不 import `group_dispatcher` → 无环；
> - `permissions.py` 保持「用字面量 `tpl_1` 而非 import」的既有约定不变（`follow_rules` 是**另一个**模块，不破坏该约定）。

### 2.2 白名单判定函数（纯函数，可单测）

```python
def template_in_market(template_id: object | None, market: frozenset[str]) -> bool:
    """该策略模版是否属于指定跟单市场；白名单命中才算。"""
    if not template_id:
        return False
    return str(template_id).strip().lower() in market


def market_reject_reason(template_id: object | None, market: frozenset[str]) -> Optional[str]:
    """该模版不属于市场时的人读原因（详情页置灰提示用）。"""
    if template_in_market(template_id, market):
        return None
    tid = str(template_id or "").strip().lower()
    label = strategy_templates.live_template_name(tid, None) or tid or "未知策略"
    if tid in AI_MARKET_TEMPLATE_IDS:
        return f"「{label}」不在普通跟单市场，请在【AI 交易】页跟单"
    return f"「{label}」暂不开放跟单"
```

### 2.3 `followable_groups` 增加模版维度（**A / B / C 的共同入口**）

`followable_groups` 是 A/B/C 三个接口的**唯一**判定入口 ——
只要改这一个函数，三处一起生效，这是本方案最重要的收敛点。

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

**调用方改造**（A/B/C 三处）：

```python
# 原来
followable = follow_rules.followable_groups(all_groups, uid, primary.get("node_id"))

# 改为（先批量预取 template_id，再传进去）
tmpl_map = _template_ids_of(await _strategies_of(all_groups, store))   # {strategy_id: template_id}
followable = follow_rules.followable_groups(
    all_groups, uid, primary.get("node_id"),
    market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
    strategy_templates_map=tmpl_map,
)
```

### 2.4 分发层兜底（D：**AI 分组的跟单节点不再收到信号**）

这是本方案**唯一的硬边界**。改 `group_dispatcher._effective_with_follows`：

```python
    async def _effective_with_follows(
        self, group: dict, nodemap: dict[str, dict], online_ids: set[str],
        *, signal: Optional[TradingSignal] = None,
        strategy: Optional[dict] = None,
    ) -> list[str]:
        """本次分发的有效节点，串起 跟单员准入 / 节点定向（node_ids）。

        三层准入：
        1. 跟单员准入：带单员失去 follow_leader 能力 -> 退回 owner 口径；
        2. 市场准入：分组绑定的策略模版不在跟单市场白名单内（如 AI 智能加仓策略）
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

**调用点**：`_dispatch_group` 已经同时拿到了 `group` 和 `strategy`：

```577:580:backend/app/group_dispatcher.py
        mode = group_rules.normalize_dispatch_mode(group.get("dispatch_mode"))
        effective = await self._effective_with_follows(
            group, nodemap, online_ids, signal=signal,
        )
```

→ 补一个 `strategy=strategy` 参数即可，**零额外 IO**（`strategy` 已在 `_candidate_groups` 里加载）。

### 2.5 已有跟单关系的展示（【我的跟单】）

`GET /api/follow/me` 走的是 `marketable_reason`（不是 `followable_groups`），
所以 **AI 分组里的存量跟单关系仍会出现在【我的跟单】里**（决策 3：关系保留，等人工清理）。

需要补的是**原因说明** —— 让用户知道「为什么这个分组不跟了、去哪跟」：

```python
def marketable_reason(
    group: dict,
    leader_has_capability: bool,
    primary_node_id: Optional[str],
    template_id: object | None = None,
    market: frozenset[str] = FOLLOW_MARKET_TEMPLATE_IDS,
) -> Optional[str]:
    """该分组当前是否还在带单市场；下架时返回人读原因，可跟单返回 None。"""
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

### 2.6 详情页的置灰展示（B）

`GET /api/follow/leaders/{id}` 现在只返回**可跟**的分组（`followable_groups` 的结果）。
改造后 AI 分组会直接从列表里消失 —— 这符合决策 3 的「软边界」，
但用户会困惑「我之前跟的 AI 分组去哪了」。

**建议**：详情接口**额外**返回「已下架分组」列表（AI 分组），标灰不可选：

```python
# FollowDetailOut 新增字段
class FollowDetailOut(BaseModel):
    leader: FollowLeaderSummary
    groups: list[FollowGroupDetail]        # 可选的（市场内）
    hidden_groups: list[FollowGroupDetail]  # 新增：不在市场内的（AI 分组），置灰
    my_nodes: list[dict] = Field(default_factory=list)
```

`hidden_groups` 的 `selectable=False`，`unselectable_reason` = `market_reject_reason(...)` 的文案。

> **取舍说明**：这会让接口契约变复杂。**如果不想要这个复杂度**，
> 最小实现是：AI 分组直接不返回（当前 `followable_groups` 的行为），
> 【我的跟单】里保留存量条目 + 原因文案。**本方案按「加 hidden_groups」设计**，
> 因为它直接支撑决策 3（人工清理需要看得见），且前端改动只是多渲染一个折叠区。

---

## 3. 前端改动

### 3.1 `FollowView.vue` —— 详情页的分组区

当前详情页**只渲染 `detail.groups`**（都是可跟的）。改造后需要渲染两个区：

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

### 3.2 `FollowView.vue` —— 【我的跟单】的存量条目

`myFollowing` 里的分组已经带了 `unmarketable_reason`，前端只需把已有的
「该带单员已关闭带单」灰标扩展为**通用的原因展示**：

```vue
<span v-if="!f.marketable" class="tag amber">{{ f.unmarketable_reason || '该带单员已关闭带单' }}</span>
```

（当前代码写死了「该带单员已关闭带单」，需改成读 `unmarketable_reason` 的实际值。）

### 3.3 类型定义

`frontend/src/api/types.ts`：

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

## 4. 实施步骤（按可独立验证的顺序排列）

> 每步末尾标注「验证方法」和「回滚方式」。后端先行，前端随后。

### 步骤 1：后端 —— 市场常量与纯函数（无副作用）

**文件**：`backend/app/follow_rules.py`

1. 顶部加 `from . import strategy_templates`（叶子模块，无循环依赖风险）；
2. 新增 `FOLLOW_MARKET_TEMPLATE_IDS` / `AI_MARKET_TEMPLATE_IDS` 两个常量；
3. 新增 `template_in_market()` / `market_reject_reason()` 两个纯函数；
4. 改 `followable_groups`：加 `market` + `strategy_templates_map` 两个关键字参数；
5. 改 `marketable_reason`：加 `template_id` + `market` 两个关键字参数。

**验证**：

```bash
python -c "from backend.app import follow_rules; print(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS)"
# 预期输出：frozenset({'tpl_2', 'tpl_3'})
```

**回滚**：删除常量与两个新函数，恢复两个原函数签名（默认值保证旧调用仍可运行）

---

### 步骤 2：后端 —— `follow_api` 三处调用点接入市场判定

**文件**：`backend/app/follow_api.py`

2.1 **复用**已有的批量封装 `_strategies_of`（返回 `{strategy_id: strategy_dict}`），
从中取 `template_id` 即可 —— **不新增 helper**：

```python
# 已有（follow_api._strategies_of）：一次读 Redis，不在循环里逐个查。
# 缺失的策略（已删除）不在返回里 —— 调用方据此把分组判为不可跟。
async def _strategies_of(groups: list[dict], store: RedisStore) -> dict[str, dict]:
    ...

# 新增一个极薄的转换（放在 follow_api 内，与 _strategies_of 紧邻）
def _template_ids_of(strategies: dict[str, dict]) -> dict[str, str]:
    """{strategy_id: strategy} -> {strategy_id: template_id}。"""
    return {sid: str(st.get("template_id") or "") for sid, st in strategies.items()}
```

> **注意**：`_strategies_of(groups, store)` 的参数顺序是 **(groups, store)**，
> 调用时别写成 (store, groups)。

2.2 `list_follow_leaders`（A）：

```python
    all_groups = await store.all_groups()
    strategies = await _strategies_of(all_groups, store)
    tmpl_map = _template_ids_of(strategies)
    ...
    followable = follow_rules.followable_groups(
        all_groups, uid, primary.get("node_id"),
        market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
        strategy_templates_map=tmpl_map,
    )
```

2.3 `get_leader_detail`（B）：同上改造 `followable` 计算；并新增 `hidden_groups` 计算：

```python
    # 市场内（可跟）
    followable = follow_rules.followable_groups(
        all_groups, leader_user_id, primary.get("node_id"),
        market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
        strategy_templates_map=tmpl_map,
    )
    # 市场外（AI 加仓等）：仅提示
    hidden = [
        g for g in follow_rules.followable_groups(
            all_groups, leader_user_id, primary.get("node_id"),
            market=follow_rules.AI_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
        if g["group_id"] not in {f["group_id"] for f in followable}
    ]
```

> **注意**：`followable_groups` 内部还带 `enabled` 过滤 —— 已被禁用的分组既不在 `followable` 也不在 `hidden`，
> 这是可接受的（禁用分组本来就不该展示）。但**市场外的 AI 分组若被禁用，会完全不可见** —— 见 §6 边界说明。

2.4 `join_follow`（C）：

```python
    followable = {
        g["group_id"]: g for g in follow_rules.followable_groups(
            all_groups, body.leader_user_id, leader_primary.get("node_id"),
            market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
    }
```

> **这一处是安全关键**：它是**唯一**的写入路径。改完这里，AI 分组就**写不进**跟单关系了。
> 注意 `join_follow` 里已经有一处策略存在性检查（`g["strategy_id"] not in strategies`），
> 改造后这行变成冗余（市场判定已隐含策略必须存在），可保留但不再承担安全职责。

**验证**：

```bash
# 1. AI 分组不出现在列表
curl http://<host>/api/follow/leaders -H "Authorization: Bearer <token>"
# 预期：AI 分组的带单员，其 followable_group_count 减少

# 2. 直接尝试加入 AI 分组 -> 必须被拒
curl -X POST http://<host>/api/follow/join -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"leader_user_id":7,"group_ids":["<AI分组ID>"],"node_ids":["<我的节点ID>"]}'
# 预期：400「没有可加入的分组（可能已全部跟过或已失效）」
```

**回滚**：撤销 2.1~2.4 四处改动（`_template_ids_of` 转换函数可一并删除）

---

### 步骤 3：后端 —— 分发层兜底（D：唯一硬边界）

**文件**：`backend/app/group_dispatcher.py`

3.1 改 `_effective_with_follows` 签名，加 `strategy` 参数 + 市场准入分支（见 §2.4 代码）；
3.2 改调用点 `_dispatch_group`：

```python
        effective = await self._effective_with_follows(
            group, nodemap, online_ids, signal=signal, strategy=strategy,
        )
```

3.3 补 import：`from . import follow_rules`（若尚未 import）。

**验证**（**最关键的一步**）：

```bash
# 场景：alice 有 AI(tpl_1) 分组，bob 的节点已加入该分组的跟单关系
# 1. alice 触发一次信号（AI 加仓策略分组）
# 2. 断言 bob 的节点**没有**产生子任务
# 3. 断言 alice 自己的主节点**正常**产生子任务
```

**回滚**：撤销 3.1~3.3

---

### 步骤 4：后端 —— 【我的跟单】的原因文案

**文件**：`backend/app/follow_api.py`（`list_my_followings`）

4.1 预取市场内分组的模版（复用 `_strategies_of` + `_template_ids_of`）：

```python
    strategies = await _strategies_of(list(all_groups.values()), store)
    tmpl_map = _template_ids_of(strategies)
```

4.2 传 `template_id` 给 `marketable_reason`：

```python
            reason = follow_rules.marketable_reason(
                g, has_cap, primary.get("node_id") if primary else None,
                template_id=tmpl_map.get(str(g.get("strategy_id") or "")),
                market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            )
```

**验证**：AI 分组的存量跟单在【我的跟单】里显示
「「AI智能加仓策略」不在普通跟单市场，请在【AI 交易】页跟单」

**回滚**：撤销 4.1~4.2

---

### 步骤 5：后端 —— 模型与契约

**文件**：`backend/app/models.py`

5.1 `FollowDetailOut` 新增字段：

```python
class FollowDetailOut(BaseModel):
    leader: FollowLeaderSummary
    groups: list[FollowGroupDetail]
    hidden_groups: list[FollowGroupDetail] = Field(default_factory=list)
    my_nodes: list[dict] = Field(default_factory=list)
```

**验证**：`python -c "from backend.app.models import FollowDetailOut; print('ok')"`

**回滚**：删除该字段

---

### 步骤 6：前端 —— 详情页与我的跟单

**文件**：`frontend/src/views/FollowView.vue`

6.1 加计算属性：

```typescript
const selectableGroups = computed(() => detail.value?.groups ?? [])
const hiddenGroups = computed(() => detail.value?.hidden_groups ?? [])
```

6.2 模板：分组区拆成「可跟单分组」+「不在普通跟单市场」两块（见 §3.1）；
6.3 模板：【我的跟单】的灰标改为读 `unmarketable_reason` 实际值（见 §3.2）。

**文件**：`frontend/src/api/types.ts`

6.4 `FollowDetailResponse` 加 `hidden_groups?: FollowGroupDetail[]`（见 §3.3）。

**验证**：`npm run build` 无类型错误；浏览器里：
- 带单员有 AI 分组 → 详情页出现置灰的「不在普通跟单市场」区；
- 加入按钮对 AI 分组不可用；
- 【我的跟单】里 AI 分组显示原因灰标。

**回滚**：撤销 6.1~6.4

---

### 步骤 7：测试

**文件**：`backend/tests/test_follow_api.py`（追加）

| 用例 | 期望 |
|---|---|
| 带单员有 `tpl_1` 分组 → `GET /api/follow/leaders` | 该分组不计入 `followable_group_count` |
| 带单员**只有** `tpl_1` 分组 | 该带单员**不出现在**列表（`followable_groups` 为空） |
| `POST /api/follow/join` 加入 `tpl_1` 分组 | 400，被拒 |
| `GET /api/follow/leaders/{id}` | `groups` 不含 AI 分组；`hidden_groups` 含它且 `selectable=False` |
| `GET /api/follow/me` 中 AI 分组的存量关系 | 仍返回，`marketable=false`，`unmarketable_reason` 指向【AI 交易】 |
| `tpl_2` / `tpl_3` 分组 | 行为完全不变（回归） |

**文件**：`backend/tests/test_group_dispatch_follow.py`（追加）

| 用例 | 期望 |
|---|---|
| AI 分组 + 跟单节点在组 → 触发信号 | **跟单节点不产生子任务**；owner 主节点正常 |
| `tpl_2` 分组 + 跟单节点在组 → 触发信号 | 跟单节点**正常产生**子任务（回归） |
| AI 分组但**无**跟单成员 | 行为不变（owner 正常开仓） |
| 分发时 `strategy` 为 None | 按「不在市场」处理（保守：跟单节点不发信号） |

> **最后一条是安全默认**：宁可漏发也不误发。`strategy` 实际不会为 None
> （`_candidate_groups` 保证每个 matched 分组都有 strategy），但兜底要有。

**回归必跑**：

```bash
pytest backend/tests/test_follow_api.py -v
pytest backend/tests/test_group_dispatch_follow.py -v
pytest backend/tests/test_group_dispatch.py -v
pytest backend/tests/test_group_api.py -v
```

**回滚**：删除新增用例

---

## 5. 改造后的完整判定链路

```
                     ┌─────────────────────────────────────┐
                     │  分组 G（带单源：owner + 含主节点）   │
                     └──────────────┬──────────────────────┘
                                    │ strategy_id → 策略 → template_id
                                    ▼
                     ┌─────────────────────────────────────┐
                     │ template_id ∈ FOLLOW_MARKET_...?      │
                     │  tpl_2 / tpl_3  →  是                │
                     │  tpl_1 (AI)     →  否                │
                     └──────────────┬──────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
   【带单】列表/详情            join 写入                  分发层兜底
   (A/B) followable_groups    (C) followable_groups      (D) _effective_with_follows
   → AI 分组不出现              → AI 分组写入被拒           → AI 分组：跟单节点
   → 进 hidden_groups 置灰        (400)                      一律不接收新信号
                                                                （退回 owner 口径）
```

**一致性保证**：A/B/C 共享 `followable_groups`（唯一判定入口），D 用同一份白名单常量。
改一次白名单，四处同时生效。

---

## 6. 边界与风险

| 场景 | 行为 | 说明 |
|---|---|---|
| **AI 分组里的存量跟单关系** | 关系保留；**不再收到新信号**（D 兜底）；【我的跟单】里可见 + 原因提示 | 决策 3：人工清理 |
| **AI 分组已被禁用** | 既不在 `groups` 也不在 `hidden_groups`（`followable_groups` 带 `enabled` 过滤） | 见下方「已知不完美」 |
| **带单员只有 AI 分组** | 该带单员**不出现在**【带单】列表（`followable_groups` 为空） | 正确 —— 他在普通带单市场没有可跟的东西 |
| **带单员有 AI + 趋势/网格分组** | 只展示后者；AI 分组进 `hidden_groups` | 正确 |
| **AI 分组的 CLOSE 信号** | 仍按 `group_ids` 终止子任务 | CLOSE 是终止指令，不受市场准入影响（既有语义） |
| **AI 分组的 owner 自己** | 不受影响 —— 降级只影响**跟单成员**，owner 成员照常 | 关键：过滤的是「跟单」，不是「分组」 |
| **策略被删除（`strategy_id` 悬空）** | 不在 `tmpl_map` → 判为不在市场 → 不可跟 | 保守安全 |
| **未来新增模版 `tpl_4`** | 默认**不**进入带单市场（白名单） | 决策 1：需显式加入 |
| **【AI 交易】Tab 后续接入** | 复用 `FOLLOW_MARKET_TEMPLATE_IDS` / `AI_MARKET_TEMPLATE_IDS` + 同一张 `group_follower_node` 表 + 同一套分发链路 | 决策 2 已预留 |

### 已知不完美（明确取舍）

1. **被禁用的 AI 分组在详情页完全不可见** —— 因为 `followable_groups` 统一带 `enabled` 过滤。
   若希望「禁用的 AI 分组也要看到并提示去哪跟」，需要把 `hidden_groups` 的计算改为
   **不看 `enabled`**（即另写一个 `all_leader_source_groups` 变体）。当前选择不写，
   因为禁用的分组本就不该展示，收益有限。

2. **`marketable_reason` 的判断顺序** —— 市场判定放在「能力 / 主节点」之后、「分组已禁用」之前。
   若某个 AI 分组同时「带单员换了主节点」和「是 AI 策略」，会显示主节点的原因而非市场原因。
   这是可接受的（两个原因都成立，主节点更根本）。

3. **存量关系不自动清理** —— 决策 3 已明确「自行移除」。若后续想自动化，
   可加一个一次性脚本：把 `tpl_1` 分组下的 `group_follower_node` 行导出给管理员确认后删除。
   **本方案不实现**，但分发层兜底（D）保证这些关系**不会产生任何交易**。

---

## 7. 文件清单

**修改**

| 文件 | 改动 | 规模 |
|---|---|---|
| `backend/app/follow_rules.py` | 市场常量 + 2 个纯函数 + `followable_groups` 加模版维度 + `marketable_reason` 加模版维度 | 中 |
| `backend/app/follow_api.py` | 复用 `_strategies_of` + 新增 `_template_ids_of` 转换 + 3 处调用点接入 + `hidden_groups` 计算 + 【我的跟单】原因 | 中 |
| `backend/app/group_dispatcher.py` | `_effective_with_follows` 加 `strategy` 参数 + 市场准入降级分支 | 小 |
| `backend/app/models.py` | `FollowDetailOut` 加 `hidden_groups` | 极小 |
| `frontend/src/api/types.ts` | `FollowDetailResponse` 加 `hidden_groups?` | 极小 |
| `frontend/src/views/FollowView.vue` | 详情页分组区拆分 + 我的跟单灰标读实际原因 | 中 |

**新增**

| 文件 | 内容 |
|---|---|
| `backend/tests/test_follow_api.py` | 追加 6 个用例（见步骤 7） |
| `backend/tests/test_group_dispatch_follow.py` | 追加 4 个用例（见步骤 7） |

**不改动**（已核查，确认无需动）

- `backend/app/permissions.py` —— 能力体系与本次无关
- `backend/app/orm.py` —— 无新表、无新列
- `backend/app/follow_service.py` —— 跟单关系读写不变
- `backend/app/group_service.py` —— 分组缓存结构不变
- `backend/app/parser.py` / `webhook.py` —— 信号定向字段不变
- `backend/app/strategies.py` —— 策略热推只作用于 `tpl_1`，**与市场准入正交**（正交性见 §8）
