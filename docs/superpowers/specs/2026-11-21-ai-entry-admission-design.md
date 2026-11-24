# 中控台「AI 策略（tpl_1）开仓准入过滤」实现方案

> 本文档只写方案、不写代码。
>
> **一句话定位**：在**中控台**的品种过滤配置里新增一块**独立于普通区间过滤**的
> 「AI 策略（tpl_1）开仓准入」，作用在 **AI 智能加仓策略分组的首单开仓信号**上：
> 方向不对 → 丢弃；价格不在允许区间 → 丢弃；缺价 → 直接拦截。
> 平仓（CLOSE）、已在跑的任务、`tpl_2` / `tpl_3`、normal 链路**一律不受影响**。

---

## 0. 已拍板的决策

| # | 决策 | 说明 |
|---|---|---|
| 1 | **配置落点采纳 B 方案** | 在 `config:filters` 的**每个品种条目下新增 `ai_entry` 子块**，与普通 `intervals` 并列、互不影响 |
| 2 | **判定层级放 L1（信号/分组级）** | 在自动路由规则匹配**之后**、建主任务**之前**做裁决；不拆成 L1 方向 + L2 区间两级 |
| 3 | **缺价直接拦截** | 取不到参考价 → 判定为「无法判定区间」→ 拦截丢弃，**不放行** |
| 4 | **auto 路由同样拦截** | 自动路由规则命中之后仍要过 AI 准入闸门；命中了但被拦 → 规则命中统计照常记，信号不下发 |

---

## 1. 背景与现状

### 1.1 现状：strategy 链路与中控台 filters 完全隔离

`tpl_1`（AI 智能加仓策略）严格走 `model=strategy` 链路（`webhook.process_signal` → `group_dispatcher.GroupDispatcher`），
与 normal 链路（`dispatcher.py`）**完全隔离**。`group_rules.py` 模块说明写得很明确：

```7:12:backend/app/group_rules.py
- 不读中控台 `config:filters`，不做币种准入、多区间方向过滤、持仓过滤；
- 不读节点按币种配置（follow_sync / follow_poll / lot_mode / poll_order）；
- 分发模式来自分组自身（sync / poll），与币种无关；
```

也就是说：**中控台那套「区间 + 允许方向」过滤，tpl_1 一条都不生效**。
normal 链路的区间判定在 `rules.interval_filter`，读的是 `config:filters`；
strategy 链路被显式设计成不读它。

### 1.2 现状：tpl_1 现有的完整过滤清单（按执行顺序）

| # | 层 | 过滤内容 | 是否涉及方向 | 是否涉及价格区间 |
|---|---|---|---|---|
| 1 | 解析/校验 | model 枚举合法、action/symbol 必填、未知 template_ids 拒收 | — | — |
| 2 | 幂等 | 5 秒窗口内同指纹信号抑制 | — | — |
| 3 | 分组定向 | `group_ids` / `template_ids` 点名（空 = 不限制） | — | — |
| 4 | 分组准入 | 分组启用 + 绑定策略 + 策略启用 + **策略品种 == 信号品种** | — | — |
| 5 | 策略级准入 | 至少一条启用中规则（`group_rules.entry_reject_reason`） | — | — |
| 6 | 路由模式 | 分组 `signal_source`：`direct` 直连 / `auto` 按规则 / 遗留 `center` 不收信号 | — | — |
| 7 | 自动路由规则 | 管理员配置的持久规则：`signal_types` / `timeframes` / **`directions`** / `group_ids`，priority 命中即占位；**只对 `auto` 分组生效** | ✅ | ❌ |
| 8 | 跟单市场准入 | tpl_1 属 AI 市场；`accept_ai_follow` 关闭 / 主人失去带单能力 → 不接单 | — | — |
| 9 | 暂停 | `ai_follow_paused`（整组不接新开仓）、`leader_paused`（该跟单员不收） | — | — |
| 10 | 节点可达 | 节点启用 + 当前在线 + `node_ids` 定向 | — | — |
| 11 | 并发互斥 | 组内同节点已有子任务 → 跳过（**tpl_1 不支持信号并发**） | — | — |
| 12 | **财经日历黑名单** | 用户级：高影响数据公布前后禁新开仓；手动急停优先；fail-open；跟单员按**自己**的设置 | — | — |
| 13 | 链路闸门 | 终端与券商断连 → 不下发 | — | — |
| 14 | **趋势风控** | 分组开启 `trend_risk_enabled`：按节点终端行情探针算趋势，**BUY 只允许多头、SELL 只允许空头** | ✅（行情判定） | ❌ |
| 15 | AI 跟单个性化 | `risk_mode=custom` 覆盖盈亏比；`run_mode=single/loop` | — | — |
| 16 | 节点侧加仓规则 | tpl_1 的逆势/顺势规则 `action`（all/buy/sell）**只约束加仓方向，不约束首单方向** | ✅（仅加仓） | ❌ |
| 17 | 信号质量 | 信号历史胜率；**铁律：只展示，不拦截** | — | — |

### 1.3 三个直接结论

1. **财经日历（⑫）是节点级、按用户身份取设置、fail-open**。
2. **方向过滤只有两处、都不满足需求**：自动路由规则的方向（⑦）只对 `auto` 分组生效、且无价格区间；
   趋势风控（⑭）是「顺势才开」，是系统判定的行情趋势，不是用户配的「4000-4200 允许做多」。
3. **价格区间过滤在 strategy 链路上完全不存在** —— 这正是本次要补的拼图。

---

## 2. 总体架构

```
        Webhook / 手动触发 / 限价监听
                    │
                    ▼
        webhook.process_signal（解析、校验、5 秒幂等）
                    │
                    ▼
        group_dispatcher.dispatch()
                    │
                    ├── _candidate_groups(signal)
                    │      └─ 分组定向 / 启用 / 绑策略 / 品种匹配 / 策略准入
                    │
                    ├── 分流 direct / auto（遗留 center 剔除）
                    │
                    ├── _match_auto_groups(auto_groups, signal)   ← 规则命中统计照常记
                    │
                    ├── ★ L1：AI 开仓准入闸门（新增）
                    │      ├─ 只看 template_id == "tpl_1" 的分组
                    │      ├─ 方向总开关 → 区间裁决 → 缺价拦截
                    │      └─ 被拦分组从下发列表剔除（不建主任务）
                    │
                    ├── record_signal（status=dispatching / rejected）
                    │
                    └── 逐个 _dispatch_group → _dispatch_node
                           （节点级：并发锁 / 财经日历 / 链路 / 趋势风控 / 建子任务 / 下发）
```

**L1 闸门的位置是本方案的关键**，理由见 §6.1。

---

## 3. 数据模型

### 3.1 中控台配置：`config:filters` 的 `ai_entry` 子块

存储位置不变（Redis `config:filters`，JSON blob），**在每个品种条目下新增 `ai_entry` 子块**：

```jsonc
"XAUUSD": {
  "enabled": true,            // ① normal 链路的品种总开关（原有，不动）
  "allow_buy": true,          // ② normal 链路方向开关（原有，不动）
  "allow_sell": true,
  "dispatch_mode": "sync",
  "position_scope": "symbol",
  "default_action": "block",
  "lot_enabled": false,
  "lot": 0.01,
  "intervals": [ /* ... */ ], // ③ normal 链路的区间（原有，不动）

  "ai_entry": {               // ④ 新增：只作用于 tpl_1（AI 智能加仓）的开仓准入
    "enabled": false,         //    总开关，默认 false → 存量零行为变化
    "allow_buy": true,        //    方向总开关
    "allow_sell": false,
    "default_action": "block",//    价格不在任何区间内：block / pass
    "intervals": [
      { "low": 4000, "high": 4200, "allow": ["BUY"] }
    ]
  }
}
```

**用户场景落地后**（XAUUSD，`ai_entry` = `4000-4200 allow BUY`、`allow_sell=false`、`default_action=block`）：

| 输入 | 结果 | 依据 |
|---|---|---|
| SELL 信号 | **丢弃** | 方向总开关 `allow_sell=false` |
| BUY 信号，价 4100 | **放行** | 命中区间 [4000,4200]，该区间允许 BUY |
| BUY 信号，价 4300 | **丢弃** | 不在任何区间，`default_action=block` |
| BUY 信号，**无价** | **丢弃** | 缺价直接拦截（决策 3） |

### 3.2 归一化与容错（服务端强制）

归一化逻辑放在**服务端**（`ai_entry_guard.normalize_ai_entry`），不依赖前端 —— 这份配置直接决定真实交易。

| 情形 | 处理 |
|---|---|
| `ai_entry` 缺失 / 非字典 | 视为 `{enabled: false}` → **放行**（存量零影响） |
| `enabled` 缺失 | 视为 `false` |
| `default_action` 非法值 | 视为 `block`（保守） |
| `allow_buy` / `allow_sell` 缺失 | 视为 `true` |
| 单条区间 `low >= high` / 非数字 / `allow` 为空 | **该条区间被丢弃**（不参与判定），不影响其它区间与整个配置 |
| 全部区间被丢弃且 `enabled=true` | 退化为「只看方向总开关」，并在日志里 warn 一次 |

### 3.3 不需要改动的存储层

- `config:filters` 是 Redis JSON blob，新字段随存随取；
- **不新增数据库列**，不新增表；
- `group_signal_task.skip_code` / `signal_history.reject_code` 列已存在，够用。

---

## 4. 模块设计

### 4.1 新增 `backend/app/ai_entry_guard.py`

职责：**只做「AI 开仓准入」的判定与取价编排**，不含交易动作、不写 DB、不发指令。

```
# —— 配置归一（纯函数，无 IO）——
normalize_ai_entry(raw: object) -> dict
    # → {enabled, allow_buy, allow_sell, default_action, intervals:[{low,high,allow}]}
    # 缺省 → {"enabled": False, ...}（不启用）

# —— 完整准入判定（纯函数，无 IO）——
evaluate(action, symbol, price, ai_entry_cfg) -> (allowed: bool, reason: str|None)
    # 放行 → (True, None)；拦截 → (False, 人读原因)
    # 注：skip_code 不在判定层返回 —— 拒收编码由调用方按场景决定
    #     （分组级被拦 -> REJECT_AI_ENTRY；节点级明细 -> SKIP_AI_ENTRY，二期启用）

# —— 取参考价（编排层，唯一有 IO 的部分）——
resolve_price(signal, group, nodemap, accounts, online_ids) -> (price: float|None, source_desc: str)
    # 返回 (价格, 来源说明)；取不到 → (None, "无可用价格")
```

### 4.2 判定顺序（短路）

```
1. ai_entry.enabled == false          → 放行
2. action 不是 BUY/SELL               → 放行（CLOSE 不参与）
3. BUY  且 allow_buy == false         → 拦（方向总开关）
   SELL 且 allow_sell == false        → 拦（方向总开关）
4. price is None                      → 拦（无可用价格，无法判定区间）   ← 决策 3
5. 命中某区间 [low, high]             → 该区间 allow 不含本次方向 → 拦（文案写明命中区间）
6. 不在任何区间                        → default_action = block → 拦 / pass → 放行
```

> **第 4 步只在 `enabled=true` 时才会走到** —— 不启用时缺价完全不影响任何信号，
> 否则会让所有没配价的上报信号被误杀。这条是必须有的回归点（测试清单第 8 条）。

### 4.3 区间裁决与 normal 链路共用（防口径漂移）

`rules.py` 现在的 `interval_filter` 把三件事混在一起：方向总开关、缺价、区间裁决。
本方案**重构（不改行为）**，抽出纯函数供两条链路共用：

```python
# rules.py（重构，行为逐字节不变）
def interval_decision(action, price, intervals, default_action) -> tuple[bool, Optional[str]]
    # 只管：命中哪个区间 / 区间允不允许这个方向 / 区间外怎么办
    # 放行 → (True, None)；拦截 → (False, 人读原因)

def interval_filter(action, symbol, price, filters_cfg) -> tuple[bool, Optional[str]]
    # 保持现有签名与行为，改为调用 interval_decision
    #   + 保留 normal 自己的方向总开关与缺价文案
```

`ai_entry_guard.evaluate` 调用**同一个** `interval_decision`。
这样两条链路对「命中哪个区间 / 边界闭合 / 区间外默认动作」的口径**永远是同一份实现**。

**区间边界为闭区间**：`low <= price <= high` 两端都算命中（与 normal 一致）。
**多区间重叠**：按 `intervals` 数组顺序，**首个命中即定**（与 normal 一致）。

### 4.4 参考价来源（L1 的价格从哪来）

| 优先 | 来源 | 说明 |
|---|---|---|
| 1 | `signal.entry_price` / `signal.limit_price`（> 0 才算） | 信号自带价，零 IO、最准 |
| 2 | 分组 **owner 成员**的账户快照报价（`store.get_account(node_id).prices`） | 优先 **主节点**（`is_primary`，报价最有代表性）；其次第一个**在线** owner 成员；再次第一个启用成员 |
| 3 | — | **缺价 → 拦截**（决策 3） |

**不做行情探针**（`market_probe`）：探针要求节点在线且有 8 秒超时，放在分发热路径上不可接受。

### 4.5 批量取价（避免 N 次 IO）

一次信号可能命中多个分组。实现上：

1. 先算出所有候选分组各自需要的参考节点集合；
2. 一次性 `store.get_account(node_id)` 批量读（Redis，读的是节点每 5 秒上报一次的缓存快照）；
3. 各分组用自己的参考节点价格判定。

### 4.6 L1 的边界定义（重要）

**L1 = 分组级裁决**，即「同一个分组内的所有节点结论一致」，
不会出现「主节点放行、跟单节点被拦」的分歧。不同分组之间可以结论不同（各自用自己的报价源）。

> 若后续需要「整条信号全局一刀切」（所有分组共用一个价），
> 只需把取价改成全局单点 + 一个参考节点，改动很小。记录在此备查。

---

## 5. 接口设计

### 5.1 `PUT /api/config/filters`（`backend/app/config_api.py`）

现状是 `body: dict` 直接存，**无服务端校验**。新增校验：

- `ai_entry` 的每条区间：`low < high`、`low/high` 均为有限数、`allow` 非空且取值属于 `{BUY, SELL}`、`default_action ∈ {block, pass}`；
- **非法 → 400 拒收**，错误文案指明品种与第几条区间；
- normal 的 `intervals` **保持现状不校验**（不扩大改动面）；
- `ai_entry` 缺失时**不报错**（存量配置原样通过）。

### 5.2 `GET /api/config/filters`

无需改动 —— 直接返回 Redis 里的原始 JSON，新字段随存随取。
前端负责归一化展示（`parseFilterRules`）。

### 5.3 不新增接口

不需要新表、不需要新路由。审计沿用现有 `persist.audit(category="console")`（`set_filters` 已在记 before/after）。

---

## 6. 后端接入点

### 6.1 `backend/app/group_dispatcher.py` —— 位置 A：`dispatch()` 主流程

现状：

```560:610:backend/app/group_dispatcher.py
        matched, reasons = await self._candidate_groups(signal)
        # 两种分组模式：把 auto 分组从「无条件下发」里摘出去。
```

改为：

```
matched, reasons = await self._candidate_groups(signal)
  → 分流 direct / auto（遗留 center 剔除）
  → auto 规则匹配（auto_hit）                    ← 规则命中统计照常记
  → ★ L1 准入判定（对 direct_groups + auto_hit 逐个判定）
        · 只处理 template_id == "tpl_1" 的分组，其余原样放行
        · 被拦的分组从下发列表剔除
        · 被拦原因写入 reasons（分组级，人读）
        · 全部被拦 → 返回 mode="rejected" + reject_code="ai_entry_admission"
        · 部分被拦 → 正常下发放行分组，响应附带 filtered_groups
  → record_signal
  → 逐个 _dispatch_group
```

**为什么放在这里（`record_signal` 之前、建主任务之前）**：

1. **不产生 `group_signal_task` 记录** —— 避免信号中心任务列表被一堆「skipped」污染
   （与 `_candidate_groups` 里「品种不匹配」的处理完全同构）；
2. **落选原因进 `reasons`**，webhook 响应里能直接看到「XX 分组：AI 开仓准入：…」；
3. **在自动路由规则之后** —— 保证规则命中统计不失真（见 §6.2）。

### 6.2 auto 路由为什么也要拦、且必须放在规则匹配之后

| 环节 | auto 分组的行为 | 本次改动 |
|---|---|---|
| 自动路由规则匹配 | 规则命中 → 分组进入候选 | **不受影响**，规则照常匹配 |
| AI 开仓准入（L1） | 规则命中后，仍要做准入判定 | **被拦** → 该分组不下发 |
| 实际下发 | — | 只有通过前两关的分组才下发 |

**顺序不可颠倒**：如果把 L1 放在规则匹配之前，auto 规则的「命中 N 条 / 最近命中时间」就会失真，
管理员会以为规则没配好。所以顺序固定为 **规则匹配 → 准入 → 下发**。

被拦时：信号仍然入库、`targets=0`、响应里明确写出「哪个分组因为什么被拦」。
这与现有「auto 无规则命中」的处理完全同构（`已入库但未下发：…`），口径一致。

### 6.3 其余接入点：一律不动

| 位置 | 理由 |
|---|---|
| `_dispatch_close`（CLOSE 路径） | CLOSE 走「按活动子任务筛」的独立路径，**不经 `_candidate_groups`**，天然不受影响。平仓永远放行 —— 与财经日历黑名单同口径（数据公布期间把裸仓丢着不管，比错过几单危险得多） |
| 已在跑的任务 | L1 只作用于「新信号 → 新建主任务」；已在跑的子任务不终止、不强制平仓 |
| `tpl_2` / `tpl_3` 分组 | 判定前先检查 `strategy.template_id == "tpl_1"`，不是就整段跳过 |
| normal 链路（`dispatcher.py`） | 本需求只作用 strategy 链路；normal 链路继续读普通 `intervals`，行为不变 |
| 节点侧加仓 | tpl_1 的加仓方向由节点侧规则 `action` 控制，**一期明确不做**（见 §9 风险表） |

### 6.4 `backend/app/signal_codes.py`

新增两个编码（遵循既有「只增不改」规矩）：

| 常量 | 值 | 用途 | 人读标签 |
|---|---|---|---|
| `SKIP_AI_ENTRY` | `ai_entry` | 节点级明细（**预留**，二期启用 `group_task_dispatch` 用） | AI 开仓准入 |
| `REJECT_AI_ENTRY` | `ai_entry_admission` | 信号级 / 分组级拒收（`signal_history.reject_code`） | AI 开仓准入 |

同时：

- 加入 `SKIP_CODES` / `REJECT_CODES` 元组；
- 补 `SKIP_CODE_LABELS` / `REJECT_CODE_LABELS`；
- `_BACKFILL_RULES` 加一条 `("AI 开仓准入", ("AI 开仓准入", "ai_entry"))`（兼容历史文案回填，可选）。

> `REJECT_CODES` 那套是给 `signal_history.reject_code` 用的（信号级拒收）；
> 分组级被拦的原因走 `reasons` 字符串 + 响应体，不落 `group_task_dispatch`。

---

## 7. 前端设计

### 7.1 文件改动

| 文件 | 改动 |
|---|---|
| `frontend/src/api/types.ts` | 新增 `AiEntryRule` 接口；`SymbolFilterRule` 增加 `ai_entry?: AiEntryRule` |
| `frontend/src/utils/filterRules.ts` | `createEmptySymbolRule` 补 `ai_entry` 默认值；`normalizeSymbolRule` 解析 `ai_entry`；`serializeFilterRules` 序列化；`validateFilterRules` 校验 `ai_entry` 区间 |
| `frontend/src/components/FilterRulesEditor.vue` | **中控台模式（`mode="global"`）下**，每个品种卡片新增一节「AI 策略（tpl_1）开仓准入」 |

### 7.2 数据结构（TypeScript）

```ts
export interface AiEntryRule {
  /** 总开关，默认 false（存量零影响） */
  enabled: boolean
  allow_buy: boolean
  allow_sell: boolean
  default_action: DefaultFilterAction
  intervals: FilterInterval[]
}
```

### 7.3 UI 结构

**位置**：中控台模式（`mode="global"`）下，在每个品种卡片里，紧跟普通「价格区间」区块之后，新增一节「AI 策略（tpl_1）开仓准入」。

**区块内容**（复用现有区间编辑 UI 与样式，只是数据源换成 `rule.ai_entry.intervals`）：

- 总开关（默认关）
- 允许做多 / 允许做空（两个开关）
- 默认动作：不在任何区间时 → 拦截 / 放行
- 区间列表：每条含下限、上限、允许方向（做多 / 做空），可增删改

**节点详情页（`mode="node"`）不显示这一节** —— 节点不涉及 tpl_1 准入。

### 7.4 前端校验规则

- `low < high`，两侧均为有效数字；
- 每条区间至少勾一个方向；
- `default_action` 只能是 `block` / `pass`。

### 7.5 中控台页面说明文案

在中控台页面底部说明文字补一句：

> 「AI 策略（tpl_1）开仓准入」只作用于 **AI 智能加仓分组的首仓信号**；
> 平仓与已在跑的任务不受影响。

---

## 8. 可观测性

| 场景 | 落库位置 | 内容 |
|---|---|---|
| 部分分组被拦 | `signal_history`（status=dispatching，targets 减少） | 响应附 `filtered_groups: [{group_id, group_name, reason, code}]` |
| 全部分组被拦 | `signal_history`（status=**rejected**，reject_code=`ai_entry_admission`，route_state=dropped） | reason 写明全部落选原因 |
| auto 规则命中但被准入拦 | `signal_route_rule_hit` **照常记录** | 响应 `auto_rule_hits` 照常返回；额外说明被准入拦下 |
| 信号中心统计 | 按 `reject_code` 聚合 | 「AI 开仓准入」成为一个可见的拒收原因分类 |

### 8.1 拦截文案样例

沿用 normal 链路的可读风格（可直接给运营看）：

```
AI 开仓准入：方向总开关：该品种已禁止接收做空(SELL)信号
AI 开仓准入：价格 4150.5 命中区间 [4000,4200]，该区间仅允许BUY，SELL 被拦截
AI 开仓准入：价格 4300 不在任何配置区间内，默认动作拦截(block)
AI 开仓准入：XAUUSD 无可用价格（节点未上报报价且无持仓价），无法判定区间，BUY 被拦截
```

---

## 9. 文件清单

### 新增（1 个）

- `backend/app/ai_entry_guard.py` —— 归一化 + 判定 + 取价编排

### 修改（后端 4 个）

- `backend/app/group_dispatcher.py` —— 接入 L1 准入闸门
- `backend/app/rules.py` —— 抽出 `interval_decision` 共用纯函数（**行为不变**）
- `backend/app/signal_codes.py` —— 新增 skip/reject code + 标签 + 关键词回填
- `backend/app/config_api.py` —— 保存时校验 `ai_entry`

### 修改（前端 3 个）

- `frontend/src/api/types.ts`
- `frontend/src/utils/filterRules.ts`
- `frontend/src/components/FilterRulesEditor.vue`

### 新增测试（1 个文件）

- `backend/tests/test_ai_entry_guard.py`

### 文档

- 本设计文档
- 更新《技术实现方案.md》的分发链路章节（新增 L1 闸门一节）

---

## 10. 测试清单

### 10.1 纯函数单测（`test_ai_entry_guard.py`，无 IO）

| # | 用例 | 期望 |
|---|---|---|
| 1 | `ai_entry` 缺失 / `enabled=false` | 放行 |
| 2 | `allow_sell=false` + SELL | 拦（方向总开关） |
| 3 | BUY + 价格命中 [4000,4200]（allow=BUY） | 放行 |
| 4 | SELL + 价格命中同区间（allow=BUY） | 拦，文案含区间 |
| 5 | BUY + 价格 4300（区间外，`default_action=block`） | 拦 |
| 6 | BUY + 价格 4300 + `default_action=pass` | 放行 |
| 7 | 价格 `None` + `enabled=true` | 拦（缺价） |
| 8 | 价格 `None` + `enabled=false` | **放行**（关键回归点） |
| 9 | CLOSE | 放行 |
| 10 | 非法区间（`low>=high` / `allow` 空） | 该区间被丢弃，不影响其它区间 |
| 11 | 区间边界闭合：`price == low`、`price == high` | 都算命中 |
| 12 | 多区间重叠 | 首个命中即定 |
| 13 | 非法 `default_action` | 退化为 `block` |
| 14 | `normalize_ai_entry` 对非字典输入 | 返回 `enabled=False` 的默认结构 |

### 10.2 接入层测试

| # | 用例 | 期望 |
|---|---|---|
| 15 | tpl_1 分组 + SELL 信号 | 分组被拦、**无 `group_signal_task` 产生**、`targets=0` |
| 16 | **auto 分组**规则命中 + 被准入拦 | 规则命中统计照常记录、但**不下发** |
| 17 | direct 分组 + auto 分组混合，部分被拦 | 放行的那组正常下发，被拦的剔除 |
| 18 | tpl_2 / tpl_3 分组 | **完全不受 `ai_entry` 影响** |
| 19 | CLOSE 信号 | 不走准入，任务正常终止 |
| 20 | 被拦分组的响应 | `reason` 含人读原因，`reject_code == "ai_entry_admission"` |
| 21 | `config_api` 保存非法 `ai_entry`（`low>=high`） | **400** |
| 22 | `config_api` 保存含 `ai_entry` 的 filters | 落库后再读，`ai_entry` 原样保留（不被归一化吞掉） |

### 10.3 回归

| # | 用例 | 期望 |
|---|---|---|
| 23 | normal 链路全套既有测试 | `rules.interval_filter` 重构后**行为逐字节一致** |
| 24 | 存量 filters 配置（无 `ai_entry`） | tpl_1 分组行为**零变化** |

---

## 11. 边界与风险

| 风险 | 说明 | 处置 |
|---|---|---|
| **存量行为突变** | `ai_entry` 默认 `enabled=false` | 已有配置不写该字段 → 归一化为不启用 → 零影响。单测 #24 兜底 |
| **报价源差异** | 不同券商 XAUUSD / XAUUSDm 报价不同，边界上结论可能不一 | 分组级裁决已把分歧限制在分组内；文案写明判定用的参考价 |
| **主节点离线** | 主节点不在线时退到其它 owner 成员报价 | 仍取不到 → 缺价 → 拦截（决策 3）。文案明确写出「无可用价格」 |
| **配置粒度** | 全局配置对所有 tpl_1 分组一刀切 | 一期不做分组级豁免；但判定函数设计成「分组级调用」，二期加豁免只需在调用处加一个条件 |
| **加仓未覆盖** | L1 只管首仓，tpl_1 的加仓方向仍由节点侧规则 `action` 控制 | 一期明确不做（二期若做，应下沉节点侧，不能塞进中控台） |
| **auto 统计失真** | L1 若放在规则匹配前，规则命中数会失真 | 顺序固定为「规则匹配 → 准入 → 下发」，单测 #16 守住 |
| **价格与下单时刻偏差** | 判定用快照价（节点每 5 秒上报一次），实际成交价可能已变 | 区间过滤的固有特性，与 normal 链路一致。文案用「判定价」措辞而非「成交价」 |
| **判定层异常** | `ai_entry_guard` 抛异常 | **fail-open 放行**并打日志告警（与财经日历一致）：判定层故障不能静默变成「永不开单」 |

---

## 12. 分期

### 一期（本次范围）

后端 4 文件 + 新模块 + 前端 3 文件 + 单测。含：

- 中控台 `ai_entry` 配置（存 / 读 / 校验 / 编辑）；
- L1 准入闸门（方向 + 区间 + 缺价拦截）；
- auto 路由同样拦截（且在规则匹配之后）；
- `SKIP_AI_ENTRY` / `REJECT_AI_ENTRY` 编码与信号中心可见性。

### 二期（可选）

- 分组级豁免开关（内部测试分组不受全局准入约束）；
- `price_missing_action: block|pass` 可配（当前固定 `block`）；
- 节点级明细（`SKIP_AI_ENTRY` 真正启用 `group_task_dispatch`）；
- 加仓级区间准入（需下沉节点侧，**另立需求**）；
- 全信号全局单一参考价（分组间也一致）。

### 三期（可选）

- 准入决策的「试算」预览：管理员在保存前能模拟某条历史信号会不会被拦。

---

## 13. 验收标准

- [ ] 中控台品种卡片出现「AI 策略（tpl_1）开仓准入」区块，**默认关闭**，存量配置**行为零变化**
- [ ] XAUUSD 配置 `4000-4200 allow BUY` + `allow_sell=false` + `default_action=block` 后：
  - SELL 信号被丢弃，节点**未收到**任何指令
  - BUY 信号且判定价在 [4000,4200] 内 → 正常下发
  - BUY 信号且判定价在区间外 → 被丢弃
  - BUY 信号**缺价** → 被丢弃（`reject_code == "ai_entry_admission"`）
- [ ] **auto 路由模式下同样拦截**，且 auto 规则的命中统计**不被影响**
- [ ] CLOSE 信号**不受影响**（平仓正常下发）
- [ ] 已在跑的任务**不终止**
- [ ] `tpl_2` / `tpl_3` 分组**不受影响**
- [ ] 非法 `ai_entry` 配置保存时返回 **400** 并指明品种与第几条区间
- [ ] 被拦分组**不产生** `group_signal_task` 记录（信号中心任务列表不被污染）
- [ ] 拦截原因在 webhook 响应与信号中心里都是**人读中文**，不是「其它」

---

## 14. 决策记录（ADR 摘要）

| # | 决策 | 备选 | 选择理由 |
|---|---|---|---|
| 1 | 配置落点 = 品种条目下 `ai_entry` 子块（B 方案） | A 复用普通 intervals / C 独立表 | 与「按品种配置」心智一致；与 normal 准入互不串台；存量零变化；不用新表 |
| 2 | 判定层级 = L1（信号/分组级），一次裁决 | L1 方向 + L2 区间两级 / 纯节点级 | 一条信号对一组分组统一裁决，语义清晰、落选原因好归因；避免同组内主节点放行、跟单节点被拦的分歧 |
| 3 | 缺价 = 直接拦截 | 缺价放行 | 用户拍板；与 normal 链路一致。区间过滤拿不到价就放行 = 配置形同虚设 |
| 4 | auto 路由同样拦截，且放在规则匹配**之后** | 放在规则匹配之前 / 不拦 | 用户拍板「我希望拦截」；放在之后可保住规则命中统计不失真 |
| 5 | 区间裁决抽成共用纯函数 `interval_decision` | 复制一份到 group_dispatcher | 两条链路口径永不漂移；normal 链路行为不变 |
| 6 | 不做行情探针取价 | 探针兜底 | 探针要节点在线 + 8 秒超时，放在分发热路径上不可接受 |
| 7 | 加仓不做区间准入 | 一并做 | 加仓发生在节点进程内，须下沉节点侧；塞进中控台会形成「两套价格、两个真相」 |
| 8 | 判定层异常 fail-open | fail-closed | 与财经日历一致：判定层故障不能静默变成「永不开单」 |
