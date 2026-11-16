# 自动路由（信号中心 → 分组自动下发）设计方案

> 日期：2026-11-16
> 状态：**待评审，未写代码**
> 关联：`docs/superpowers/specs/2026-11-13-ai-signal-center-prototype-design.md`（§2.4 信号类型分类 / §6.1 旧页保留）
> 需求：在【路由】里配置「哪些分组接收哪些类型 & 周期的信号」，信号中心收到信号后**自动下发给对应分组**，不再人工点「下发」。

---

## 〇、先说结论（TL;DR）

**推荐方案 = 「路由规则表 + 复用现有 signal_source 语义 + 在 webhook 受理链路上做规则匹配」。**

不是新造一个「自动路由引擎」，而是把现在的**人工路由规则化**：

1. 新增一张 `signal_route_rule` 表：**一条规则 = 一组过滤条件 + 一组目标分组**；
2. 分组的 `signal_source` 语义**扩一个值** `auto`（规则自动）；
3. `group_dispatcher.dispatch()` 里，`direct` 分支**先跑一遍规则匹配**，命中规则的分组自动走 `_dispatch_group`，没命中的照旧排除；
4. 匹配条件只用**已在 `signal_history` 里的字段**：信号类型、周期、方向、品种 —— 全部是 TV 告警带来的**统计维度**，不新增解析负担。

**为什么不用现成的 `template_ids` / `group_ids` 定向字段**：
那两个是 **TV 告警自带**的（谁推的告警谁指定），而需求是**管理员在信号中心里配置**的。后者需要一个可 CRUD、可持久化的规则表，前者只是单次信号的瞬时定向。

---

## 一、现状盘点（先搞清楚「已经有什么」）

### 1.1 现有的三条信号分发路径

```350:360:backend/app/webhook.py
async def process_signal(
    data,
    *,
    source_ip: str | None,
    source: str,
    store: RedisStore,
    dispatcher: Dispatcher,
    group_dispatcher: GroupDispatcher | None = None,
    raw_payload: str | None = None,
    dedup_key: str | None = None,
) -> dict:
```

```415:437:backend/app/group_dispatcher.py
    async def dispatch(self, signal: TradingSignal, signal_id: str,
                       source_ip: Optional[str] = None,
                       raw_payload: Optional[str] = None,
                       source: str = "tradingview",
                       route_mode: str = "direct",
                       trigger_source: Optional[str] = None,
                       group_ids: Optional[list[str]] = None) -> dict:
```

三条路径：

| 路径 | 入口 | 分组从哪来 | 路由状态 |
|---|---|---|---|
| **A. 直连** | webhook / 手动触发 / 限价监听 | `_candidate_groups()` 按规则挑 | `route_state='auto'` |
| **B. 信号中心（人工）** | 管理员在【路由】面板选中 | 管理员点的 `group_ids` | `route_state='routed'` |
| **C. 待路由队列** | A 路径里 `center` 分组被摘出去 | —— | `route_state='pending'` |

**关键点**：`group.signal_source` 已经有 `direct` / `center` 两个值，语义是**「这个分组接不接 webhook 的自动触发」**，跟「接哪类信号」无关。现有 `center` 模式 = 「等人工路由」。

### 1.2 现有的人工路由链路（复用它，而不是重写）

```117:170:backend/app/signal_routing.py
async def route_signal(
    signal_id: str,
    *,
    group_ids: list[str],
    dispatcher: Any,
    ...
) -> dict:
    """把一条信号路由到指定的分组。"""
```

`route_signal` 已经把「把一条信号按参数下发给 N 个分组」这件事做完了，包括：
- **幂等**：同信号 + 同分组已有未收口任务 → 409 `RouteConflict`
- **参数覆盖**：volume / sl / tp
- **信号重建**：从 `raw_payload` 重新解析（限价开仓的 `entry_price` 只在原始载荷里）

**所以自动路由要做的事，是「在合适的时机、用正确的参数调 `route_signal`」**，而不是重写一遍下发逻辑。

### 1.3 现有的准入校验（自动路由必须复用，不能自己写一套）

```374:410:backend/app/group_dispatcher.py
        for group in groups:
            name = group.get("name") or group.get("group_id")
            if not group_rules.group_targeted(group, signal.group_ids):
                continue
            if not group.get("enabled", True):
                ...
            strategy_id = group.get("strategy_id")
            ...
            off_target = group_rules.template_reject_reason(strategy, signal.template_ids)
            ...
            if not group_rules.symbol_match(strategy.get("symbol"), signal.symbol):
                ...
            reject = group_rules.entry_reject_reason(...)
```

准入链 = 分组启用 + 绑定策略 + 策略启用 + 模版定向 + **品种匹配** + 策略开仓准入。

**品种匹配这条特别重要**：`group_rules.symbol_match` 要求**策略绑定的品种 == 信号的品种**。也就是说「哪些分组接收哪些信号」这个需求里，**品种维度已经被策略绑定隐式决定了**（不需要在规则里再配一次品种 —— 配了会和策略绑定打架）。

---

## 二、需求拆解（把模糊的话变成可实现的规则）

用户原话：「我在路由里面可以选择分组，接收哪些类型 & 周期的信号，然后信号中心接收到信号之后，就自动下发给对应的分组。」

拆成四个可配置维度：

| 维度 | 取值 | 数据来源 | 备注 |
|---|---|---|---|
| **目标分组** | N 个 group_id | 已有 | 多选 |
| **信号类型** | `ob` / `fvg` / `pdz` / `fibo` / … | `signal_history.signal_type` | 建议支持**业务分类**（结构分析 = ob+fvg+pdz） |
| **周期** | `M15` / `H1` / `D1` / … | `signal_history.timeframe` | 归一后词表 |
| **方向**（可选） | BUY / SELL | `signal_history.action` | 建议加上，很便宜 |
| **品种** | —— | —— | **建议不做**，见 §1.3 |

### 2.1 空值语义（必须先定死，否则规则行为不可预测）

我建议统一为**「空 = 不限制」**，与现有 `template_ids` / `group_ids` 的语义一致（`group_rules.template_reject_reason` 里 `if not template_ids: return None`）。

| 规则字段 | 空值的含义 |
|---|---|
| `signal_types` 空 | 不限制类型 → **所有类型都收** |
| `timeframes` 空 | 不限制周期 → **所有周期都收** |
| `directions` 空 | 不限制方向 |
| `group_ids` 空 | **无效规则**（没有目标分组的规则没有意义）→ 保存时直接拒 |

⚠️ **反直觉的点**：一条「只配了分组、没配类型/周期」的规则 = 收全部信号。这可能不是用户想要的（用户说「选择接收哪些类型&周期」，可能是想**必填**）。**这是个需要拍板的点**，见 §七。

---

## 三、方案设计

### 3.1 数据模型：新增 `signal_route_rule` 表

```python
class SignalRouteRule(Base):
    """自动路由规则：一条规则 = 一组过滤条件 → 一组目标分组。

    与「信号自带的 template_ids / group_ids 定向」是两回事：
    那两个是 **TV 告警自带**的（谁推的告警谁指定），单次有效、不持久化；
    本表是**管理员在信号中心里配置的持久规则**，对之后所有信号生效。
    """
    __tablename__ = "signal_route_rule"

    rule_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))              # 规则名，给人看
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # —— 过滤条件（空 = 不限制）——
    signal_types: Mapped[list | None] = mapped_column(JSON, nullable=True)   # ["ob","fvg",...]
    timeframes:   Mapped[list | None] = mapped_column(JSON, nullable=True)   # ["M15","H1"]
    directions:   Mapped[list | None] = mapped_column(JSON, nullable=True)   # ["BUY","SELL"]

    # —— 目标分组 ——
    group_ids: Mapped[list] = mapped_column(JSON)                # ["g1","g2"]

    # —— 参数覆盖（留空 = 沿用信号原值）——
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl:     Mapped[float | None] = mapped_column(Float, nullable=True)
    tp:     Mapped[float | None] = mapped_column(Float, nullable=True)

    # 优先级：多条规则同时命中时，**先命中的分组先处理**（见 §4.3）
    priority: Mapped[int] = mapped_column(Integer, default=100)

    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
```

**设计要点**：

1. **用 JSON 列存数组，不建关联表**。理由：过滤条件是「集合匹配」，没有多对多的实体语义；规则数量级是**几十条**（不是几千条）；JSON 列在 SQLite / MySQL 都够用，且避免 3 张关联表的 join。
2. **规则是「全局的」还是「按所有者的」？** 我建议**先做全局**（`owner_user_id` 预留但不强绑）。理由：分组本身有 owner，但**信号是系统级的**（TV 告警进的是平台），一条规则跨 owner 分组是常态（管理员给所有分组配一条规则）。owner 维度留字段，二期再决定是否启用。
3. **参数覆盖字段**：规则级覆盖让「同一批信号到不同分组用不同手数」不用配 N 条规则。但**风险**：覆盖 `sl` 会直接影响策略的开仓准入（`entry_reject_reason` 要求信号带止损价）。所以 `sl` 覆盖必须**在匹配之后、准入之前**生效，且要保证覆盖后的 sl 仍然合法。

### 3.2 分组的 `signal_source` 扩一个值：`auto`

```python
# signal_meta.py
ROUTE_MODE_DIRECT = "direct"   # 分组直连（TV / 手动直接触发）
ROUTE_MODE_CENTER = "center"   # 信号中心下发
ROUTE_MODE_AUTO   = "auto"     # 信号中心按规则自动下发   ← 新增
```

语义变成三档：

| `signal_source` | 行为 |
|---|---|
| `direct`（默认） | webhook 命中即下发（现状，**存量分组零行为变化**） |
| `center` | 不被自动触发，等管理员手工路由（现状） |
| `auto`（新增） | 被自动触发，但**只接收命中自动路由规则的信号**；没命中规则的信号不进这个分组 |

**为什么用 `auto` 而不是复用 `direct`**：
- `direct` = 「无条件接收所有品种匹配的信号」，这是**广播**语义；
- `auto` = 「有选择地接收」，这是**订阅**语义。

两者对同一条信号的处置完全不同，混在一个值里会让 `direct` 分组的行为变得不可预测（要么 direct 也跑规则 → 存量分组行为突变；要么 direct 不跑规则 → 那 `auto` 就没存在意义）。

**存量零行为变化**：新列有 `default="direct"`，老行不受影响；`normalize_signal_source` 认 `auto` 之前会把它回落成 `direct`，所以**必须同步改这个函数**（见 §5.2）。

### 3.3 匹配逻辑

```python
# signal_route_rules.py（新模块）

def rule_matches(rule: dict, signal: dict) -> bool:
    """规则是否命中这条信号。

    `signal` 是归一后的信号维度：{"signal_type","timeframe","action","symbol"}。
    空条件 = 不限制（与 template_ids / group_ids 的语义一致）。
    """
    types = rule.get("signal_types") or []
    if types:
        st = signal_meta.normalize_signal_type(signal.get("signal_type"))
        # 业务分类展开：规则里写「结构分析」要匹配 ob/fvg/pdz 三个码
        codes = set()
        for t in types:
            codes |= set(signal_meta.types_in_group(t)) or {t}
        if st not in codes:
            return False

    tfs = rule.get("timeframes") or []
    if tfs:
        tf = signal_meta.normalize_timeframe(signal.get("timeframe"))
        if tf not in tfs:
            return False

    dirs = rule.get("directions") or []
    if dirs:
        action = (signal.get("action") or "").upper()
        if action not in dirs:
            return False

    return True
```

**业务分类展开**（`signal_meta.types_in_group` 已在 `signal_ai_center` 用过）：
规则里写「结构分析」→ 展开成 `['ob','fvg','pdz']`；写 `ob` → 就只匹配 `ob`。
**这条很重要** —— 否则用户选「结构分析」，结果只收到 `ob` 信号。

### 3.4 执行时机：放在 `dispatch()` 的 direct 分支里

```python
# group_dispatcher.py，dispatch() 内，现有 direct 分支改造后

matched, reasons = await self._candidate_groups(signal)

direct_groups: list = []
center_groups: list = []
auto_groups:   list = []

for group, strategy in matched:
    src = signal_meta.normalize_signal_source(group.get("signal_source"))
    if src == "center":
        center_groups.append((group, strategy))
    elif src == "auto":
        auto_groups.append((group, strategy))
    else:
        direct_groups.append((group, strategy))

# ★ 新增：auto 分组走规则匹配，命中哪些分哪些
if auto_groups:
    hit, miss = await match_auto_groups(auto_groups, signal)
    direct_groups.extend(hit)
    # miss 的分组 → 这条信号对它们是「不匹配」，静默跳过
    #（不是 rejected，是「没选它」）
```

**为什么放在这里，而不是放在 webhook 层**：
- `dispatch()` 是**所有 strategy 信号的唯一入口**（webhook / 手动触发 / 限价监听 / 信号中心人工路由都走它）；
- 放这里 → 自动路由对**所有信号来源**生效，不只是 TV 告警；
- 放这里 → 天然复用了 `_candidate_groups` 的全部准入校验（品种匹配、策略准入、启用状态）；
- 放 webhook 层 → 手动触发和限价监听的信号就漏了。

**⚠️ 但有个坑**：`dispatch()` 走 direct 路径时，`signal.group_ids` 可能被 TV 告警指定过（`template_ids` / `group_ids` 定向）。这时候「自动路由规则」和「告警自带的定向」**谁优先**？见 §4.2。

### 3.5 幂等：自动路由的幂等比人工路由更重要

人工路由是「人点一下」，同一信号点两次会看到 409，人能理解。
自动路由是**系统每次都跑**，如果 webhook 重试 / 告警重发，就会**重复开仓**。

现有三道防线：

1. **webhook 层去重**（`store.seen_signal(fp)`）—— 指纹含 action/symbol/volume/sl/tp/signal_type/timeframe/template_ids/group_ids/node_ids。**同一条告警重发会被吞掉**。这是最外层的防线，够用。
2. **`route_signal` 的 `RouteConflict`** —— 同信号 + 同分组已有未收口任务 → 409。
3. **`_active_task_ids`** —— 查 `group_signal_task` 里 `status ∈ TASK_ACTIVE` 的行。

**但自动路由有个额外风险**：规则匹配是**纯函数**（同样的信号 + 同样的规则 → 同样的结果），所以重跑必然撞上 `RouteConflict`。**处理方式：自动路由路径下，`RouteConflict` 视为「成功（已处理过）」而不是错误** —— 否则每次重试都刷一条错误日志。

**建议**：自动路由走一个独立的 `auto_route_signal()` 包装，它捕获 `RouteConflict` 并返回 `status='skipped_duplicate'`，不计入失败。

### 3.6 状态标记：自动路由的 `route_state` 该是什么？

现在有四个值：`pending` / `auto` / `routed` / `dropped`。

| 场景 | 建议的 `route_state` | `route_mode` |
|---|---|---|
| direct 分组自动消费 | `auto` | `direct` |
| **auto 分组被规则命中并下发** | `auto`（复用！） | **`auto`** |
| center 分组等人工路由 | `pending` | `direct` |
| 人工路由下发 | `routed` | `center` |

**`route_state='auto'` 被复用**（direct 消费 和 规则消费 都是「自动」）：
「已直连」这个标签改成「已自动下发」，两种自动场景用同一个状态，靠 `route_mode` 区分（`direct` vs `auto`）。

⚠️ **但这会动到统计口径**：`signal_center_api` 和 `signal_stats` 里按 `route_state` 聚合的地方，「已直连」的条数会**变多**（含自动路由下发的）。需要确认现有统计口径能不能接受 —— 我倾向于**能**，因为两者都是「没人干预就下发了」。

如果不能接受，备选是新增 `route_state='auto_routed'`，代价是又多一个状态值要到处加（`signal_meta.ROUTE_STATES`、前端下拉、统计、标签）。

**建议**：复用 `auto`，把 `route_mode` 作为区分依据。少一个状态值。

### 3.7 参数覆盖：先不要做

规则表设计里我留了 `volume` / `sl` / `tp`，但**建议第一期不做**。

理由：
- 覆盖 `sl` 会**改变策略的开仓准入**（`entry_reject_reason` 要求信号带止损价）；
- 覆盖 `volume` 会影响 AI 加仓的风险金额反推（`signal_quality` 那套）；
- 现在的诉求是「自动下发给对应分组」，参数覆盖是**另一个诉求**（不同分组不同手数）。

**建议**：表结构预留字段，接口先不开放。等真的需要时再开 —— 但要在**文档里写清楚「预留未启用」**，免得以后有人以为有这功能。

---

## 四、几个关键设计决策（需要想清楚的坑）

### 4.1 规则匹配在 `_candidate_groups` 之后 —— 意味着「品种」维度被策略绑定决定了

`_candidate_groups` 已经要求「策略品种 == 信号品种」，所以一条规则里**不需要也不应该**配品种。

**如果用户配了品种**（比如规则里想限定只收 BTCUSDT）：
- 方案 A：不支持品种维度，UI 上不提供这个选项 → 简单，不会和策略绑定打架
- 方案 B：支持，但在准入时**追加**一道品种检查 → 规则比策略更窄，可能导致「规则说收 BTC，但策略绑的是 XAU」→ 永远不命中 → 用户困惑

**建议**：**方案 A，不做品种维度**。理由：策略绑定品种是**已有的、用户已经配过的东西**，规则再配一遍是重复配置且必然冲突。

### 4.2 规则 vs 告警自带定向：谁优先？

场景：TV 告警自带 `group_ids=['g1']`（告警方指定只发给 g1），同时有一条 auto 规则命中 `g2`。

| 方案 | 行为 | 评价 |
|---|---|---|
| **交集** | 只发 `g1 ∩ 规则命中集` | 语义最安全：告警方的显式指定是**收窄**，规则也是收窄，取交集 = 「两个条件都满足才发」 |
| 规则优先 | 规则命中的全发，忽略告警定向 | 危险：告警方明确指定了 g1，却被发到 g2 |
| 告警优先 | 告警指定了就只发告警指定的 | 同样危险：规则白配了 |

**建议：交集**。理由：两者都是**收窄**语义（都只缩小范围），交集是唯一不违反任一方意图的组合。

实现上很自然：`_candidate_groups` 已经用 `group_rules.group_targeted(group, signal.group_ids)` 做了告警定向过滤，**规则匹配加在它之后**就是天然的交集。

⚠️ 但注意 `_candidate_groups` 的 `group_targeted` 语义是「`group_ids` 空 = 全部视为被点名」→ 告警不带定向时，规则匹配是唯一的收窄。✅

### 4.3 多条规则同时命中同一个分组：会不会重复开仓？

**会**，如果不做处理。

例子：
- 规则 R1：类型=[ob]，目标=[g1, g2]
- 规则 R2：周期=[M15]，目标=[g1]

一条 `ob` + `M15` 的信号 → R1 命中 g1,g2；R2 命中 g1 → g1 被下发**两次**。

**必须去重**。方案：
- **按 `priority` 排序，命中即占位**：一个分组被某条规则命中后，从后续规则的候选集里移除。
- 这样 `priority` 小的规则**优先占用**分组，参数覆盖也跟随第一条命中的规则。

**建议**：实现「**命中即占位**」。附带的好处：`priority` 有了明确语义 —— 「高优先级的规则先挑走分组，低优先级的只吃剩下的」。

### 4.4 一条规则命中多个分组 → 每个分组独立走准入

匹配规则只回答「这个分组要不要收这条信号」，**不回答「这个分组能不能收」**。

准入（策略品种、策略准入、启用状态、节点在线）仍然由 `_candidate_groups` / `_dispatch_group` 负责。

**顺序很重要**：规则匹配放在 `_candidate_groups` **之后** → 准入已经过了，只剩「要不要它」这一个判断。反过来（先匹配规则再跑准入）也行，但**逻辑上更容易搞混**。

推荐顺序：
```
_candidate_groups(signal)          # 准入：启用/策略/品种/模版
  → 按 signal_source 分桶            # direct / center / auto
    → auto 桶跑规则匹配              # 收窄：命中即占位
      → 合并回 direct_groups
        → _dispatch_group(...)       # 选节点、建任务、下发
```

### 4.5 auto 分组没命中规则时，信号的状态是什么？

场景：auto 分组 g1 没命中任何规则，但 direct 分组 g2 命中了准入并下发了。
→ 信号 `route_state='auto'`，正常。

场景：auto 分组 g1 没命中规则，**且没有任何 direct 分组**。
→ 现状代码会走 `if not direct_groups:` 分支 → 如果有 center 分组就 pending，否则 **rejected**。

**这里要改**：auto 分组存在但没命中规则时，**不应该 rejected**（rejected 意味着「信号有问题」，但这条信号本身是好的，只是没有分组要它）。

**建议**：区分「没有候选分组」和「候选分组都不匹配规则」两种情况：
- 前者 → `rejected`（现状）
- 后者 → **`route_state='auto'` / 正常入库，但 `targets=0`**，理由写「已入库，无分组匹配自动路由规则」

⚠️ 这是**必须改的**，否则新装的 auto 分组会让人看到一堆莫名 rejected。

---

## 五、落地改动清单

### 5.1 后端

| 文件 | 改动 | 类型 |
|---|---|---|
| `backend/app/orm.py` | 新增 `SignalRouteRule` 表 | 新表 |
| `backend/app/db.py` | 迁移（`init_db` 的建表流程 + alembic 迁移） | 迁移 |
| `backend/app/signal_meta.py` | `ROUTE_MODE_AUTO = "auto"`；`normalize_signal_source` 认 `auto`；`ROUTE_MODE_LABELS` 加文案 | 改 |
| `backend/app/signal_route_rules.py` | **新增**：规则 CRUD + `rule_matches()` + `match_auto_groups()` | 新模块 |
| `backend/app/group_dispatcher.py` | `dispatch()` 里加 auto 桶 + 规则匹配 + 命中即占位 | 改 |
| `backend/app/signal_routing.py` | 新增 `auto_route_signal()` 包装（吞 `RouteConflict`）；或直接复用 `route_signal` | 改/加 |
| `backend/app/groups.py` + `models.py` | `GroupCreate` / `GroupUpdate` 的 `signal_source` 接受 `auto`；`_validate_signal_source` 放行 | 改 |
| `backend/app/group_service.py` | `group_row_to_dict` 带出 `signal_source`（已有）；确认 `auto` 归一 | 微调 |
| `backend/app/signal_center_api.py` | 规则 CRUD 接口（`GET/POST/PUT/DELETE /api/signal-center/route-rules`）；`/ai/signals` 支持按规则来源过滤（可选） | 加 |
| `backend/app/signal_stats.py` | `route_state` 统计口径确认（§3.6） | 确认 |

### 5.2 前端

| 文件 | 改动 |
|---|---|
| `frontend/src/components/signal-center/RoutingPanel.vue` | **新增「自动路由规则」区块**：规则列表 + 新建/编辑表单（目标分组多选 + 类型多选 + 周期多选 + 方向） |
| `frontend/src/components/signal-center/RouteRuleEditor.vue` | **新增**：规则编辑弹窗 |
| `frontend/src/views/GroupsView.vue` | `SIGNAL_SOURCE_TAGS` 加第三个选项「自动（按规则）」；`FIELD_HELP.signal_source` 补说明 |
| `frontend/src/api/types.ts` | `SignalRouteRule` 类型 |
| `frontend/src/stores/hub.ts` | 规则 CRUD 的 store 方法 |
| `frontend/src/constants/signalCenter.ts` | 周期/类型/业务分类的选项表（前端已有类似，抽取复用） |

### 5.3 测试

| 文件 | 覆盖 |
|---|---|
| `backend/tests/test_signal_route_rules.py` | 匹配逻辑：类型/周期/方向、空值语义、业务分类展开、多规则命中即占位、priority 排序 |
| `backend/tests/test_group_dispatcher.py`（扩） | auto 分组命中规则→下发；未命中→不入库为 rejected；auto + direct 混存；交集语义（告警定向 ∩ 规则） |
| `backend/tests/test_signal_routing.py`（扩） | 自动路由幂等：重放同信号不重复开仓 |
| `backend/tests/test_signal_center_api.py`（扩） | 规则 CRUD 接口；权限；空规则拒收 |

---

## 六、UX 设计（【路由】面板长什么样）

现有【路由】面板是「**选信号 → 选分组 → 点下发**」，是**批处理**。

新的自动路由是「**配规则 → 之后全自动**」，是**配置**。

**建议：两个 tab 并列，不混在一起。**

```
┌─ 路由 ──────────────────────────────────────────┐
│  [ 手动下发 ]  [ 自动路由规则 ]                    │  ← tab 切换
├─────────────────────────────────────────────────┤
│  自动路由规则 tab：                                │
│  ┌───────────────────────────────────────────┐  │
│  │ ● 规则1  结构分析 → 黄金组、加密组          │  │
│  │   类型: 结构分析  周期: 全部  方向: 全部     │  │
│  │   命中 142 条 · 最近命中 2 分钟前      [编辑] │  │
│  ├───────────────────────────────────────────┤  │
│  │ ○ 规则2  M15 + BUY → 外汇组                │  │
│  │   ...                                  [编辑] │  │
│  └───────────────────────────────────────────┘  │
│                            [ + 新建规则 ]          │
└─────────────────────────────────────────────────┘
```

**设计要点**：
1. **规则卡片上显示「命中统计」** —— 用户配完规则需要立刻知道它有没有在生效（配错了「一条都没命中」是最容易发生的问题）。
2. **「最近命中时间」** —— 一条规则 0 命中，可能是条件太窄，也可能是根本没来这种信号，要能区分。
3. **手动下发 tab 保留** —— auto 规则和 center 人工路由**并存**是合理的：规则管常规的，手动管例外的。
4. **规则改动要能立即生效** —— 不需要重启服务（规则从 DB/Redis 读，带短 TTL 缓存）。

---

## 七、需要拍板的问题

| # | 议题 | 我的建议 | 影响 |
|---|---|---|---|
| 1 | **规则里的类型/周期为空 = 收全部，还是 = 无效规则？** | 收全部（与 `template_ids` 语义一致） | 建议保存时**提示**「未选条件 = 接收全部信号」 |
| 2 | **规则要支持品种维度吗？** | **不支持**（策略绑定品种已隐式决定） | 支持的话会与策略绑定冲突 |
| 3 | **多条规则命中同一分组时？** | 命中即占位，`priority` 小的先挑走 | 不去重会重复开仓 |
| 4 | **自动路由下发的 `route_state`？** | 复用 `auto`，用 `route_mode='auto'` 区分 | 新增状态值会污染统计口径 |
| 5 | **规则是全局的还是按所有者的？** | 一期**全局**，`owner_user_id` 预留 | 按所有者要做跨 owner 分组的可见性问题 |
| 6 | **规则参数覆盖（volume/sl/tp）要不要？** | 一期**不做**，字段预留 | 做的话要处理 sl 覆盖与开仓准入的冲突 |
| 7 | **规则改了要不要审核/灰度？** | 一期**不做**（先能跑起来） | 生产环境误配的后果需要评估 |

---

## 八、风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| **规则配错导致漏单** | 类型/周期写窄了，信号静默不匹配 | 规则卡上显示命中数 + 最近命中时间；提供「试运行」（只算不执行） |
| **规则配错导致爆单** | 条件写宽了（比如类型为空），信号大量下发 | 「命中统计」让异常立刻可见；分组侧还有 `signal_concurrent_enabled` 等闸 |
| **重复开仓** | 规则匹配是纯函数，重试必然重跑 | webhook 层指纹去重 + `RouteConflict` 视为「已处理」 |
| **存量分组行为变化** | 新增 `auto` 模式 | `signal_source` 默认 `direct`；老行不变；`normalize_signal_source` 认 `auto` |
| **统计口径变化** | 「已直连」条数会变多（`route_state='auto'` 复用） | 上线前核对 `signal_stats` 的聚合；必要时用 `route_mode` 拆分 |
| **规则匹配的 CPU 开销** | 每条信号要查规则表 | 规则数少（几十条），内存缓存 + Redis 缓存 + 短 TTL 足够 |

---

## 九、建议的实施顺序

1. **一期（最小可用）**：规则表 + CRUD 接口 + `auto` 模式 + 匹配 + 命中即占位 + 命中统计。
   —— 这一期就能满足「配好规则、自动下发」的核心诉求。
2. **二期**：试运行模式（只匹配不执行，看命中什么）、规则命中明细追溯（哪些信号被哪条规则命中）、告警（规则 N 分钟没命中）。
3. **三期**：参数覆盖、按质量分阈值路由（接 `signal_quality` 的分数，比如「只路由质量分 ≥ 70 的信号」——**这个需求迟早会有，`signal_quality` 已经有了**）。

---

## 十、附：为什么「先不写代码」是对的

这个需求看起来是「加个配置项」，但实际上要动**分发链路的核心**：
- 改 `dispatch()` 的分支（影响所有信号来源）；
- 改 `route_state` 语义（影响所有统计）；
- 加一张新表（影响迁移）；
- 引入新的幂等语义（影响重复下单的安全性）。

这些都在**交易热路径**上。上面 §七 的 7 个问题里，**任何一条拍错都会导致「信号该下的没下」或「不该下的下了」** —— 两个都是真金白银的错误。所以先对齐口径，再动手。
