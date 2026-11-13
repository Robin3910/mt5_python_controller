# AI 信号管理中心（原型复刻）：技术方案

> 日期：2026-11-13
> 分支：`v4_dev_reset`
> 关联文档：
> - `docs/superpowers/specs/2026-11-11-signal-center-perf-routing-design.md`（信号中心绩效 + 路由中枢）
> - `docs/superpowers/specs/2026-11-13-signal-quality-design.md`（信号质量分，已落地）
>
> 范围：把【我的】→【AI信号管理中心】做成原型复刻页；新增「马丁止盈分布」口径；
> 现有【绩效】【路由】功能**保留不删、但先隐藏**。

---

## 〇、本轮拍板口径（已确认，实现不得偏离）

| # | 议题 | 结论 |
|---|---|---|
| 1 | 马丁止盈档位定义 | **最终止盈那一刻的持仓笔数**（含首仓；1 单 = 只有首仓就止盈） |
| 2 | 多节点时的统计口径 | **按节点分开统计** —— 每个「信号 × 节点」是一个独立统计单元，不合并（**展示上合并成一条分布条**，见 §4.2 补充说明） |
| 3 | 止损信号 | **不进【止盈分布】**（止损仓没有止盈事件，不产生档位） |
| 4 | 质量分 | **以现有 `signal_quality.py` 的实现为准**，本方案不另起一套算法；等级用**绝对阈值** |
| 5 | 「覆盖周期」 | **不写分母**（只显示窗口内覆盖了几个周期，不显示 "8/8"） |
| 6 | 「今日触发信号」 | 按 **UTC+8 自然日**（对齐 `signal_stats.today_start_utc8`） |

---

## 〇之二、可靠性等级：绝对阈值（已拍板）

> **≥60 分 → 高（关注）｜50~60 分 → 中（观察）｜<50 分 → 低（暂不参与）**

**等级只是展示层的一层翻译**，**不参与打分** —— 打分口径只有
`signal_quality` 那一个（历史同类信号的信号级胜率 + 贝叶斯收缩）。

**为什么用绝对阈值而不是相对全局先验**：相对阈值会随全局先验漂移 ——
同一个 58 分上周算「高」这周算「中」，管理员对不上号，绝对阈值稳定、可解释、可复述。

实现落在 `signal_quality.level_for()`，阈值常量 `LEVEL_HIGH_MIN=60` / `LEVEL_MID_MIN=50`。

---

## 一、目标与范围

### 1.1 目标

在【我的】页新增/改造「AI信号管理中心」入口，进入后是一张**单页多区块**的看板，
1:1 复刻原型（线上灵光闪应用原型【我的】→【AI信号管理中心】）的布局与信息结构。

原型区块 → 我们的实现映射：

| 原型区块 | 我们的实现 | 状态 |
|---|---|---|
| 页头（标题 / 档位 / 更新时间 / 刷新） | 新建 `AiPageHeader.vue` | 新增 |
| 蓝色说明条 | 同上组件内 | 新增 |
| 4 个指标卡 | `AiKpiRow.vue` | 新增（口径见 §3.4） |
| 可靠性等级分布 | `QualityOverview.vue` | 新增（复用 `signal_quality`） |
| 质量分区间分布 | 同上 | 新增 |
| 信号类型质量对比（点击筛选） | `TypeQualityCompare.vue` | 新增（复用 `signal_perf.by_signal_type` + `signal_quality`） |
| 重点关注 · 质量 TOP 5 | `TopSignalList.vue` | 新增 |
| 周期质量总览（8 卡 + 最优信号） | `TimeframeQuality.vue` | 新增 |
| **马丁止盈分布** | `MartingaleTpDistribution.vue` | **新增（全新口径）** |
| 全部档位 · 重点信号 TOP 5 | `TopSignalList.vue`（复用，切档位时改标题） | 新增 |
| 信号明细表 + 筛选 + 分页 | `AiSignalTable.vue` | 新增 |
| 页脚说明 | 页脚一行 | 新增 |

### 1.2 保留但隐藏（不删）

| 功能 | 处理 |
|---|---|
| 【绩效】面板（`PerformancePanel.vue` + `QualityPriorTable.vue`） | 组件与接口**全部保留**，从新页的 tab 列表中移除 |
| 【路由】面板（`RoutingPanel.vue`） | 同上 |
| 旧 7-tab 页面（`SignalCenterView.vue`） | 整体搬迁为 `SignalCenterLegacyView.vue`，**不挂路由**，保留可恢复 |
| 后端 `/performance` `/routing` `/batch-route` 等接口 | **不动** |
| `signal_perf.py` / `signal_routing.py` | **不动**（新页复用 `signal_perf._aggregate` 的口径，不复制） |

### 1.3 明确移除（本次不做的）

原型没有、用户也不需要的：概览（Hero + 图表）、信号流、节点命中、策略任务、时延。

对应组件文件（`SignalHero.vue` / `SignalOverviewCharts.vue` / `SignalStreamTable.vue` /
`NodeHitCards.vue` / `StrategyTaskPanel.vue` / `LatencyPanel.vue`）**保留在仓库**，
仅从新页引用中移除；旧 Legacy 视图仍引用它们。

---

## 二、口径设计

### 2.1 质量分：完全复用现有实现

**唯一实现**：`backend/app/signal_quality.py`。

| 项 | 口径 |
|---|---|
| 定义 | 历史同类信号（`signal_type` × `timeframe`）的**信号级胜率**（0~100） |
| 小样本 | 贝叶斯收缩 `k=10`；主桶 `n<5` 回退到类型桶，再回退全局 |
| 统计范围 | 只算 **AI 智能加仓（`tpl_1`）** 的任务 |
| 先验窗口 | 固定近 30 天全量，**不受信号中心筛选条影响** |

新页面**只消费** `signal_quality.get_prior(window)` 与
`signal_quality.score_for(type, tf, prior)`，**不重算、不改公式**（拍板 #4）。

> ⚠️ 一条实现纪律：质量分与绩效面板的胜率必须**逐位相等**（二者共用
> `signal_perf._aggregate`）。新页的「类型质量对比」「周期质量总览」里的均分，
> 也必须走 `signal_quality` 的桶分数，不能拿 `signal_perf` 的原始胜率冒充。

### 2.2 可靠性等级：高/中/低（**绝对阈值**，评分本身不变）

原型有「高 / 中 / 低」三档。等级按**绝对阈值**判定（已拍板）：

```
score >= 60  → 高（关注）
50 <= score < 60 → 中（观察）
score < 50   → 低（暂不参与）
score is None → 无（暂无历史）
```

> **为什么不用相对全局先验**：相对阈值会随全局先验漂移（同一个 58 分这周算「高」
> 下周算「中」），管理员对不上号。绝对阈值稳定、可解释、可复述。

- 阈值常量集中在 `signal_quality.py`（`LEVEL_HIGH_MIN = 60.0` / `LEVEL_MID_MIN = 50.0`），
  便于一处调整。
- **没有先验（`None`）与「分数是 0」必须区分**：`None` → 等级 `none`（暂无历史），
  `0` → 等级 `low`（历史同类信号全亏）。
- 等级与决策指导文案：

| 等级 | `level` | 决策指导 | 标签 |
|---|---|---|---|
| 高 | `high` | 关注 | 高 |
| 中 | `mid` | 观察 | 中 |
| 低 | `low` | 暂不参与 | 低 |
| — | `none` | 暂无历史 | 暂无 |

- 原型底部的固定说明文案**替换为真实口径文案**：
  「质量分 = 同类历史信号（类型×周期）的信号级胜率，小样本按贝叶斯收缩向全局回归；
  等级：60 分以上为高、50-60 分为中、50 分以下为低；仅供参考，不参与交易决策。」

### 2.3 马丁止盈档位（本方案唯一的全新口径）

#### 2.3.1 定义

> **马丁止盈档位 = 最终止盈那一刻，该魔术号下的持仓笔数**（含首仓）。
> 1 单 = 只有首仓就止盈；5 单 = 逆势加了 4 仓后止盈。

#### 2.3.2 统计单元（拍板 #2：按节点分开统计）

统计单元是 **(signal_id, node_id)** —— 一条信号被路由到 3 个节点，就是 3 个单元，
各自有自己的止盈档位，**不合并、不取最大值**。

理由：不同节点是不同 MT5 账户，手数/加仓节奏/止盈时点都不同；
把 3 个节点合成一个「档位」会产出一个两边都不认的数字。

#### 2.3.3 数据链路

```
group_task_dispatch (magic, node_id, task_id)
  → mt5_deal (magic)                        # 该节点该次执行的全部成交
      ├─ IN  成交 → position_id + deal_time  # 开仓时刻
      ├─ OUT 成交 reason=5 → deal_time       # 止盈事件（TP 时刻）
      └─ OUT 成交 reason=4 → 止损            # 不产生 TP 时刻
  → group_signal_task (task_id) → signal_id + template_id(tpl_1)
  → signal_entry_leg (task_id, seq, rule_type, order_ticket)
      └─ order_ticket → mt5_deal.order_id → position_id
```

**TP 时刻**：`magic` 下所有 `entry_type ∈ (OUT, INOUT, OUT_BY)` 且 `reason == 5`
的成交里，**最早**的那个 `deal_time`。
（`reason=5` 是 MT5 `DEAL_REASON_TAKE_PROFIT`；`reason=4` 是止损。）

**TP 时刻仍在场的持仓**：该 `magic` 下 `position_id` 满足
「首个 IN 成交时间 ≤ TP 时刻」且「最后一个 OUT 成交时间 ≥ TP 时刻（同一秒算在场）」。

**逆势链限定**：只统计「首仓（`seq=0`）+ 逆势加仓（`rule_type=1`）」这两类入场对应的持仓。
- `signal_entry_leg.rule_type = 1` → 逆势加仓（对应用户的「AI 逆势加仓加了几仓」）
- `signal_entry_leg.rule_type = 2` → 顺势加仓 → **不计入档位**
- `seq = 0`（首仓）→ 计入（档位 1 = 只有首仓）

**排除项**（明确不产生档位）：
- 止损收口的信号（`reason=4`）→ 不进止盈分布（拍板 #3）
- 仍在持仓中（尚无止盈成交）→ `tp_level = null`，前端显示「持仓中」，**不进分布分母**
- 非 `tpl_1` 的任务 → 不统计

#### 2.3.4 分布分母

分布的分母 = **已止盈的统计单元数**（有 TP 时刻的 (signal, node) 单元数）。
原型的「全部信号 97」= 窗口内信号总数，两者**不是同一口径**，UI 上必须分别标注
（原型把它们放在同一区域，容易误读 —— 我们的实现把「已止盈」写在分布条旁，
「全部信号」写在档位按钮组的标题处）。

#### 2.3.5 新增模块 `backend/app/signal_tp_levels.py`

**唯一实现**，职责三件事（与 `signal_perf` / `signal_quality` 同范式）：

| 函数 | 职责 |
|---|---|
| `_tp_moments(task_ids)` | task_id → magic → TP 时刻（最早 `reason=5` 出场成交时间） |
| `_positions_by_magic(task_ids)` | magic → position_id → (首个 IN 时间, 最后一个 OUT 时间) |
| `_leg_positions(task_ids)` | task_id → position_id → `rule_type` / `seq`（经 `order_ticket → order_id` 映射） |
| `tp_levels(task_ids)` | 汇总成**统计单元列表**：`{signal_id, node_id, task_id, magic, tp_level, tp_at}` |

复用现有工具：`signal_perf._signal_task_map`（判定 `tpl_1`）、
`signal_perf._entry_legs`（入场腿明细，含 `order_ticket`）。

**不落库**：档位是**派生计算**，不新增任何列、不加迁移。
（理由同 `signal_quality`：先验是活的；落库会给交易热路径加写入。）

### 2.4 「信号类型」的**业务分类**聚合（原型是按分类展示的）

原型是 6 个**业务分类**，我们是**结构码**。加一层**分类映射**（后端 `signal_meta`）：

| 原型分类 | 我们的 `signal_type` |
|---|---|
| 结构分析 | `ob` / `fvg` / `pdz` |
| 斐波那契回踩 | `fibo` |
| 突破信号 | `bos` |
| 趋势跟踪 | `swing` |
| 反转信号 | `choch` |
| 量价背离 | `mm` |

未匹配的码归「其它」并显示原码（遵循 `signal_meta`「未知类型保留原值」原则）。

**「信号类型质量对比」按业务分类聚合**（结构分析 = ob + fvg + pdz **合一张卡**），
每张卡带 `types` 字段列出它包含的类型码。

**联动筛选按分类展开成 `IN (...)`** —— 只取一个码会把另外两个漏掉：
`type_group=结构分析` → `signal_type IN ('ob','fvg','pdz')`。

### 2.5 「信号名称」的拼装（无落库字段）

原型有「大结构看涨OB回踩」这类可读名。库里没有该字段，**前端拼装**：

```
信号名 = 方向词(看涨/看跌) + 结构类型中文标签 + 动作词
例：看涨 · 订单块 OB 回踩
```

「周期质量总览」里的「最优：xxx」复用同一函数。

---

## 三、指标卡口径

| 指标卡 | 口径 | 来源 |
|---|---|---|
| **今日触发信号** | UTC+8 自然日（`today_start_utc8()`）内 `signal_history` 的条数 | `signal_stats` |
| **覆盖周期** | 窗口内 `signal_history.timeframe` 去重后的周期数（**不写分母**，拍板 #5） | `signal_stats` |
| **高可靠信号** | 窗口内**逐条信号**的质量分等级为「高」的条数 | `signal_quality` 逐条打分 |
| **平均信号质量** | 窗口内**有可用先验**的信号的质量分算术平均（无先验的信号不参与，也不拉低均值） | `signal_quality` 逐条打分 |

> 「高可靠信号」「平均信号质量」需要**逐条信号**打 score → 复用
> `signal_quality.quality_map(signals)`（已实现，按 `(type, tf)` 去重计算）。

---

## 四、后端接口设计

挂在现有 `APIRouter(prefix="/api/signal-center")` 下，仍 `require_admin`。

### 4.1 `GET /api/signal-center/ai/overview`

返回原型上半屏（指标卡 + 质量总览 + 类型对比 + 周期总览 + TOP5）。

```jsonc
{
  "window": "30d",
  "updated_at": 1731000000,
  "kpi": {
    "today_signals": 48,
    "timeframes_covered": 8,
    "high_reliability": 9,
    "avg_quality": 77.5,
    "signals_total": 97,
    "signals_scored": 92          // 有可用先验的条数（分母说明用）
  },
  "reliability": [                // 等级分布
    { "level": "high", "label": "高", "count": 9,  "pct": 9.3,  "advice": "关注" },
    { "level": "mid",  "label": "中", "count": 81, "pct": 83.5, "advice": "观察" },
    { "level": "low",  "label": "低", "count": 7,  "pct": 7.2,  "advice": "暂不参与" }
  ],
  "score_buckets": [              // 质量分区间分布（区间固定，与原型一致）
    { "key": "gte90", "label": "90 分以上", "count": 2,  "pct": 2 },
    { "key": "80_89", "label": "80 - 89 分", "count": 31, "pct": 32 },
    { "key": "70_79", "label": "70 - 79 分", "count": 57, "pct": 59 },
    { "key": "lt70",  "label": "70 分以下",  "count": 7,  "pct": 7 }
  ],
  "by_type": [                    // 信号类型质量对比
    { "key": "ob", "label": "结构分析", "count": 14, "avg_score": 85.8,
      "high_count": 8, "win_rate": 62.0, "n": 14 }
  ],
  "by_timeframe": [               // 周期质量总览
    { "key": "H4", "label": "4 小时", "count": 19, "avg_score": 78.6,
      "level": "mid", "advice": "观察",
      "best_signal": { "signal_id": "abc", "name": "看涨 · 订单块 OB 回踩", "score": 93 } }
  ],
  "top_signals": [ /* 质量 TOP 5，字段同明细行 */ ],
  "scope": "质量分 = 同类历史信号（类型×周期）的信号级胜率；等级按与全局先验的差距判定"
}
```

**实现要点**：

- `by_type` / `by_timeframe` 的 `avg_score` 用 `signal_quality` 的**桶分数**
  （`get_prior()['buckets']` 中 `signal_type` 匹配 / `timeframe` 匹配后的 `score` 加权平均），
  **不得**用 `signal_perf` 的原始胜率冒充。
- `count` = 窗口内该类型/周期的**信号条数**（分母是信号数，不是桶样本数）。
- `by_timeframe.best_signal` = 该周期内质量分最高的信号（`score` 降序取 1）。
- `top_signals` 按 `score` 降序取 5；**同分按 `received_at` 倒序**（新信号优先）。

### 4.2 `GET /api/signal-center/ai/tp-distribution`

马丁止盈分布（拍板 #1/#2/#3）。

```jsonc
{
  "window": "30d",
  "total_signals": 97,          // 窗口内信号总数（原型「全部信号」）
  "closed_tp_units": 78,        // 已止盈的统计单元数（分布分母）
  "by_node": [                  // 各节点分开统计（拍板 #2）—— **接口保留，界面不展示**
    {
      "node_id": "7-904093",
      "node_name": "主账户 A",
      "units": 42,
      "buckets": [
        { "level": 1, "label": "1单止盈", "count": 9,  "pct": 21.4 },
        { "level": 2, "label": "2单止盈", "count": 10, "pct": 23.8 }
      ]
    }
  ],
  "overall": {                  // 各节点之和（**界面画的就是这一条**）
    "units": 78,
    "buckets": [ { "level": 1, "count": 19, "pct": 24.4 } ]
  },
  "scope": "档位 = 最终止盈那一刻的持仓笔数（含首仓）；各账户分别统计后汇总展示；止损与持仓中不计入"
}
```

> **拍板 #2 是「怎么数」，不是「怎么显示」**（2026-11-13 补充）：
> 统计单元仍是「信号 × 节点」，各节点各自算档位（不合并、不取最大值）；
> 但**界面只画一条合并后的分布条**，不按账户拆成多条。
> 理由：一个模块里堆 4 条分布条，横向对比反而看不清整体形态。
> `by_node` 字段**保留在接口里** —— 将来要看「哪个账户马丁加得深」时不用改后端。

- `level` 范围固定 `1..5`（AI 智能加仓逆势链最多 5 档）；>5 的异常值归入 `level=5` 并记日志。
- 查询参数：`?node_id=` 可只看某个节点；`?level=` 只看某档。

### 4.3 `GET /api/signal-center/ai/signals`

信号明细表（原型下半屏）。

- 复用 `signal_stats.SignalFilters`（窗口 / 品种 / 方向 / 周期 / 类型 / 路由状态…）
- 新增筛选参数：
  - `tp_level`（`1..5` / `0`=未止盈 / 不传=不限）
  - `node_id`（只看某节点）
  - `type_group`（按**业务分类**筛 → 展开成 `signal_type IN (...)`）
- **页码分页**（不是游标）：原型是「第 1 / 10 页 + 上一页/下一页 + 每页 10/20/50 条」。

```jsonc
{
  "window": "30d",
  "total": 97,
  "page": 1,
  "page_size": 10,
  "items": [
    {
      "signal_id": "...",
      "received_at": 1793000000,
      "timeframe": "H4",
      "timeframe_label": "4 小时",
      "signal_type": "ob",
      "signal_type_label": "订单块 OB",
      "type_group": "结构分析",
      "name": "看涨 · 订单块 OB 回踩",
      "action": "BUY",
      "action_label": "多",
      "symbol": "BTCUSDT",
      "tp_level": 3,            // null = 持仓中 / 未止盈
      "tp_level_label": "3单止盈",
      "tp_state": "tp",         // tp / sl / running / none
      "node_id": "7-904093",    // 按节点分开统计 → 明细行也带节点
      "node_name": "主账户 A",
      "quality": { /* SignalQuality，见 signal_quality 既有结构 */ },
      "level": "high",          // high / mid / low
      "advice": "关注"
    }
  ]
}
```

**排序**：默认 `received_at` 倒序；支持 `sort=score`（质量分降序，同分按时间倒序）。

**分页实现**：`signal_history` 走 `offset/limit`（`signal_stats._fetch_signals` 已支持
`limit/offset/order_desc`）；止盈档位是派生数据 → **先取信号分页，再对当页信号计算档位**，
不把止盈条件下推到 SQL（否则要 join 成交明细，慢且脆）。

---

## 五、前端设计

### 5.1 页面结构

```
views/AiSignalCenterView.vue                 （新页面，路由 /events、/signal-center）
└─ components/signal-center/ai/
   ├─ AiPageHeader.vue          标题 / 副标题（当前档位 · 更新时间）/ 刷新 / 说明条
   ├─ AiKpiRow.vue              4 个指标卡
   ├─ QualityOverview.vue       可靠性等级分布 + 质量分区间分布 + 口径说明
   ├─ TypeQualityCompare.vue    类型质量对比（点击 → 联动筛选）
   ├─ TimeframeQuality.vue      周期质量总览（点击 → 联动筛选）
   ├─ TopSignalList.vue         质量 TOP 5 / 档位 TOP 5
   ├─ MartingaleTpDistribution.vue  马丁止盈分布 + 档位筛选 chips
   └─ AiSignalTable.vue         明细表 + 筛选行 + 分页
```

### 5.2 联动状态

页面级用一个 `ref` 承载**三个联动筛选**：

```ts
const linked = ref<{
  timeframe: string | null      // 点击周期卡
  type: string | null           // 点击类型卡
  tp_level: number | null       // 点击止盈档位
}>({ timeframe: null, type: null, tp_level: null })
```

- 任一变化 → 重置 `page = 1` → 重拉 `/ai/signals`
- 「马丁止盈分布」选中档位时，下方 TOP5 列表标题切到 `1单止盈 · 重点信号 TOP 5`，
  并把该档位写进 `linked.tp_level`（原型行为）
- 「查看档位」chips（`全部 (97)` / `1单止盈 (19)` …）与档位按钮组**双向同步**

### 5.3 明细分页

- 每页条数 `10 / 20 / 50`（分段控件，默认 10 —— 与原型一致）
- `上一页 / 下一页` + `第 N / M 页`
- 翻页只重拉明细表，不重拉上半屏

### 5.4 【我的】页入口改造

`frontend/src/views/MineView.vue` 的 `ALL_MODULES`：

| 字段 | 现值 | 改为 |
|---|---|---|
| `label` | `信号中心` | `AI信号管理中心` |
| `description` | `信号全链路统计与时延分析` | `TV报警接入 · 周期统计 · 质量评估 · 决策指导` |
| `icon` | 铃铛 path | 原型的 `radar` 雷达图标 path |
| `path` | `/events` | 不变（`/events` 与 `/signal-center` 都指向新页） |
| `code` | `events` | 不变（沿用后端菜单码，避免权限迁移） |

### 5.5 样式

- **沿用 `styles.css` 的 `pf-` 设计令牌**，不引入新色板：
  卡片底 `#101828`、边框 `#1C2740`、次级底 `#0C1220`、边框2 `#22304A`、
  主蓝 `#2563EB`、绿 `#22C55E`、红 `#F6465D`、琥珀 `#F59E0B`
- 新增 `ai-` 前缀样式段（等级分布条、区间分布柱、类型质量卡、周期卡、止盈档位条）
- 复用现有 `.pf-pill`（含 `blue/green/amber/red/grey` 语义色）、`.pf-seg`、`.pf-card`、`.pf-section-head`
- 页面容器保持 `max-width: 1200px`（与【我的】页一致）；原型是 1080px，
  **不为像素对齐压窄内容** —— 表格列多，1200 更实用

---

## 六、迁移与隐藏

### 6.1 旧页保留

1. 新建 `frontend/src/views/SignalCenterLegacyView.vue`：把现有 `SignalCenterView.vue`
   的内容**整体搬过去**（7 个 tab + `SignalFilters` + 全部旧面板引用），**不挂路由**；
   文件头注释写明「未来恢复：把 `/events` 路由指回本组件即可」。
2. `frontend/src/views/SignalCenterView.vue` **改写为新页面**（原型复刻）。
   - 理由：路由 `/events`、`/signal-center` 已指向它，改写即完成切换；
   - 或者反过来（新文件 + 改路由）—— 二选一，**推荐改写现有文件**，
     避免同一份代码两个名字、后续改路由忘了同步。
3. `SignalCenterLegacyView.vue` 的组件引用全部保留（`SignalHero` / `SignalOverviewCharts` /
   `SignalStreamTable` / `NodeHitCards` / `StrategyTaskPanel` / `LatencyPanel` /
   `PerformancePanel` / `RoutingPanel`）—— **不删任何文件**。

### 6.2 后端

- `/performance`、`/quality`、`/routing`、`/batch-route`、`/signals/{id}/route`、
  `/signals/{id}/drop` —— **全部保留，行为不变**
- 新增 3 个 `/ai/*` 接口（§四），与旧接口并存

---

## 七、实施清单

### 后端

| 文件 | 改动 |
|---|---|
| `backend/app/signal_quality.py` | 新增**绝对阈值**等级判定（`LEVEL_HIGH_MIN=60` / `LEVEL_MID_MIN=50` / `level_for()` / `advice_for()` / `level_label()` / `score_levels()`）；**打分逻辑一行不动** |
| `backend/app/signal_ai_center.py` | **新增**：AI 信号管理中心上半屏口径（指标卡 / 等级分布 / 区间分布 / 类型对比 / 周期总览 / TOP5 / 明细分页） |
| `backend/app/signal_tp_levels.py` | **新增**：马丁止盈档位唯一实现（§2.3.5） |
| `backend/app/signal_center_api.py` | 新增 3 个 `/ai/*` 接口 + Pydantic 模型 |
| `backend/app/signal_meta.py` | 新增 `signal_type_group()`（业务分类映射）/ `action_label()` / `signal_name()`（信号名拼装） |
| `backend/app/signal_stats.py` | **不改**（明细分页在 `signal_ai_center` 里做，不动共享筛选语义） |
| `backend/app/orm.py` / `db.py` | **不改**（全部派生计算，无迁移） |

### 前端

| 文件 | 改动 |
|---|---|
| `frontend/src/views/SignalCenterView.vue` | **改写**为原型复刻页 |
| `frontend/src/views/SignalCenterLegacyView.vue` | **新增**：旧 7-tab 页整体搬迁（不挂路由） |
| `frontend/src/views/MineView.vue` | 入口卡片改名 / 描述 / 雷达图标 |
| `frontend/src/components/signal-center/ai/AiPageHeader.vue` | **新增**：页头 + 口径说明条 |
| `frontend/src/components/signal-center/ai/AiKpiRow.vue` | **新增**：4 个指标卡 |
| `frontend/src/components/signal-center/ai/QualityOverview.vue` | **新增**：等级分布 + 区间分布 |
| `frontend/src/components/signal-center/ai/TypeQualityCompare.vue` | **新增**：类型质量对比（点击 → 联动筛选） |
| `frontend/src/components/signal-center/ai/TimeframeQuality.vue` | **新增**：周期质量总览（点击 → 联动筛选） |
| `frontend/src/components/signal-center/ai/TopSignalList.vue` | **新增**：质量 TOP5 / 档位 TOP5（复用） |
| `frontend/src/components/signal-center/ai/MartingaleTpDistribution.vue` | **新增**：止盈分布 + 档位 chips + 分节点分布条 |
| `frontend/src/components/signal-center/ai/AiSignalTable.vue` | **新增**：明细表 + 筛选提示 + 页码分页 |
| `frontend/src/api/types.ts` | 新增 `AiSignalOverview` / `AiTpDistribution` / `AiSignalRow` / `AiSignalList` 等类型 |
| `frontend/src/stores/hub.ts` | 新增 `fetchAiSignalOverview` / `fetchAiTpDistribution` / `fetchAiSignals` |
| `frontend/src/constants/signalCenter.ts` | **新增**：等级色带 / 标签 / 时间与档位格式化 |
| `frontend/src/styles.css` | 新增 `ai-` 样式段（KPI / 分布条 / 类型卡 / 周期卡 / 止盈分布 / 明细表 / 分页） |

> **旧的 `QualityBadge.vue` / `PerfBucketTable.vue` / `QualityPriorTable.vue` /
> `PerformancePanel.vue` / `RoutingPanel.vue` 一个都没删** ——
> 它们由 `SignalCenterLegacyView.vue` 引用，未来恢复【绩效】【路由】时直接可用。

### 测试

| 文件 | 覆盖 |
|---|---|
| `backend/tests/test_signal_tp_levels.py` | TP 时刻取最早 `reason=5`；同秒多笔出场算在场；止损不产生档位；持仓中 `tp_level=None`；部分平仓只算在场的那笔；**按节点分开统计**（同信号两节点各算各的）；非 `tpl_1` 排除；分布分母只算已止盈 |
| `backend/tests/test_signal_ai_center.py` | 空库零值结构；覆盖周期只给个数；平均质量只算有先验的；等级分布与逐条判定一致；**业务分类聚合**（结构分析 = ob+fvg+pdz）与**分类过滤**；页码分页切片正确 + 越界夹取 |
| `backend/tests/test_signal_quality.py`（扩） | **绝对阈值**等级判定的边界（60 / 59.99 / 50 / 49.99 / 0 / None）；`None` 与 0 的区分；`advice` / `label` 文案映射；`types_in_group` 展开；`signal_type_group` / `signal_name` |
| `backend/tests/test_signal_center_api.py`（扩） | `/ai/overview`、`/ai/tp-distribution`、`/ai/signals` 的响应结构与空数据兜底；管理员可见性；页码分页字段；非法参数 422 |

---

## 八、验收标准

1. 【我的】页出现「AI信号管理中心」卡片（描述 = TV报警接入 · 周期统计 · 质量评估 · 决策指导），
   点击进入原型复刻页；旧 7-tab 页与【绩效】【路由】功能**仍可通过 Legacy 组件访问**，未被删除
2. 指标卡四个数字口径正确：「今日触发信号」按 UTC+8 自然日；「覆盖周期」只显示数字无分母；
   「高可靠信号」= 等级为高的信号条数；「平均信号质量」= 有先验信号的质量分均值
3. 质量分布图与绩效面板**同源**：同一桶的胜率逐位相等（有单测钉死）
4. 马丁止盈档位 = 最终止盈那一刻的持仓笔数；同一信号在两个节点上档位不同 → **分别统计**，
   不合并
5. 止损收口的信号**不出现**在止盈分布里；持仓中的信号档位显示「持仓中」且不进分母
6. 点击类型卡 / 周期卡 / 止盈档位 → 明细表联动筛选；分页 10/20/50 可切换，页码正确
7. 后端 `pytest` 全绿、前端 `vue-tsc --noEmit` + `vite build` 通过

---

## 九、风险与明确不做

| 项 | 说明 |
|---|---|
| 止盈档位不落库 | 派生计算，随成交回填而变化；不保证「历史某一刻是几档」。一期接受 |
| 明细分页用 offset | 实时写入会让 offset 漂移（信号流用游标）。本表是**历史信号**且写入频率低，漂移可接受；不引入第二套分页语义 |
| 止盈筛选不下推到 SQL | 分页后计算，跨页口径一致；代价是筛「1单止盈」时翻页会扫全量（单窗口内量级可接受） |
| 质量分等级阈值是相对值 | 绝对阈值（≥85）在胜率型分数上不可用；相对阈值随全局先验漂移 —— 已在 §2.2 显式声明 |
| 不做档位级胜率 | 与既有决策一致（`signal_perf` 不做档位级胜率）；本方案只做**档位分布**，不做档位胜率 |
| 质量分不参与交易决策 | 与 `signal_quality` 铁律 2 一致 |
| 不做人工调分 / 快照分落库 | 二期 |
| 不做回填历史止盈档位 | 存量数据若 `signal_entry_leg` 缺失则无档位（显示「持仓中/无数据」），不猜 |

---

## 十、待拍板（若无异议按默认值执行）

| # | 议题 | 默认值 |
|---|---|---|
| 1 | 等级判定方式 | **绝对阈值：≥60 高 / 50-60 中 / <50 低（已拍板）** |
| 2 | 止盈档位上限 | 1~5（>5 归 5 并告警） |
| 3 | 明细分页默认每页 | 10 条（与原型一致） |
| 4 | 新页面文件策略 | 改写 `SignalCenterView.vue`，旧页搬 `SignalCenterLegacyView.vue`（不挂路由） |
| 5 | 周期上限 | 只展示窗口内**实际出现过**的周期，不做固定 8 格 |

---

## 十一、实施落地情况（已完成）

| 项 | 状态 | 位置 |
|---|---|---|
| 质量分等级（绝对阈值） | ✅ | `backend/app/signal_quality.py::level_for` / `advice_for` / `level_label` / `score_levels` |
| 业务分类映射 | ✅ | `backend/app/signal_meta.py::signal_type_group` / `types_in_group` / `signal_name` / `action_label` |
| 马丁止盈档位模块 | ✅ | `backend/app/signal_tp_levels.py`（新增，唯一实现） |
| AI 中心上半屏口径 | ✅ | `backend/app/signal_ai_center.py`（新增） |
| 三个 `/ai/*` 接口 | ✅ | `backend/app/signal_center_api.py` |
| 前端类型 | ✅ | `frontend/src/api/types.ts` |
| store 取数 | ✅ | `frontend/src/stores/hub.ts::fetchAiSignalOverview` / `fetchAiTpDistribution` / `fetchAiSignals` |
| 展示常量 | ✅ | `frontend/src/constants/signalCenter.ts`（新增） |
| 新页面 | ✅ | `frontend/src/views/SignalCenterView.vue`（改写） |
| 旧页保留 | ✅ | `frontend/src/views/SignalCenterLegacyView.vue`（新增，**不挂路由**） |
| 8 个新组件 | ✅ | `frontend/src/components/signal-center/ai/*` |
| 样式 | ✅ | `frontend/src/styles.css` 的 `ai-` 段 |
| 【我的】入口 | ✅ | `frontend/src/views/MineView.vue`（AI信号管理中心 / 雷达图标） |

### 验证结果

- 后端全量：**1615 passed**（含新增 46 条）
- 前端：`vue-tsc --noEmit` 通过、`vite build` 通过、`verify-build` 58/58 chunk 校验通过

### 实施中发现并修正的三个坑

1. **业务分类聚合 vs 单码筛选**：原型「结构分析」= ob + fvg + pdz。
   最初按单码筛选，点了「结构分析」只出 ob —— 改成按分类展开成 `IN (...)`
   （`signal_meta.types_in_group`），前端传 `type_group` 而非 `signal_type`。
2. **平均信号质量的「有先验」含全局兜底**：`signal_quality` 的回退链最后一级是
   全局桶，所以只要全局有样本，**任何**信号都查得到分。
   真正无分的是**全局先验也空**的情况。指标卡的分母按这个口径算。
3. **止盈档位计数口径**：「最终止盈那一刻的持仓笔数」是**字面口径** ——
   该魔术号下 TP 时刻仍在场的**全部**持仓数。
   逆向链（首仓 + `rule_type=1`）的持仓数另行记为 `chain_positions`，
   **仅供展示**，不参与档位计算（否则口径与拍板定义不一致）。
