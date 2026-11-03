# 信号管理中心 设计方案

日期：2026-11-03
状态：待确认
参考稿：`UI/H5.html`（首页 hero + 节点总览的视觉体系）
关联现状：`frontend/src/views/EventsView.vue`、`frontend/src/views/GroupSignalsView.vue`、`backend/app/events.py`、`backend/app/persist.py`、`backend/app/deal_stats.py`

---

## 0. 设计前的结论（先说清楚事实边界）

1. **`UI/H5.html` 原型里没有「信号」页面。** 原型只有 5 个顶部频道（首页 / 交易 / AI交易 / 带单 / 我的），首页是「标的趋势 + K线 + 节点总览」。
   本方案的视觉骨架 **完全复用原型首页的 hero / metrics / rows 三件套**（`stat-hero` + `stat-dist-bar` + `stat-hero-metrics` + 节点分组卡片），信号内容按原型语言重排。
2. **原型 JS bundle（`index-Ci3OJfTd.js`）本次未能拉取**（下载被环境策略拦截），因此 1:1 像素级比对暂缺；落地时以 H5 首页实测 CSS 变量为准（`--pf-*` 系列已落到 `frontend/src/styles.css`）。
3. **现有信号数据已经是「全量落库」的**，缺的不是数据，是 **统一口径的统计层 + 统一入口的展示层**。

---

## 1. 现状盘点

### 1.1 两条互斥的信号链路（必须同时纳入统计）

| 链路 | model | 入口 | 信号表 | 分发表 | 用途 |
|---|---|---|---|---|---|
| 按币种分发 | `normal` | `/webhook`（TradingView）、中控台手动 | `signal_history` | `signal_dispatch` | 一条信号 → N 个节点各一条明细 |
| 按分组分发 | `strategy` | 交易页弹窗、AI 策略、中控台手动 | `signal_history` + `group_signal_task` | `group_task_dispatch` | 一条信号 → M 个分组主任务 → 每任务 N 个节点子任务 |

两条链路通过 **`signal_id` 串联**，但 `signal_dispatch` 与 `group_task_dispatch` 是两张表、字段名不同源、状态机不同源。现有页面也是分开的：
- 【事件】页（`/events`，管理员）只看 `normal` 链路
- 【分组管理 → 信号】页（`/groups/:id/signals`）只看 `strategy` 链路

**这是当前最大的认知成本：管理员看不到「今天一共发了多少信号、成功率多少、卡在哪」。**

### 1.2 现有可用的数据字段（无需新增即可统计）

**`signal_history`（信号级）**
`signal_id` / `received_at` / `source_ip` / `raw_payload` / `action` / `symbol` / `volume` / `sl` / `tp` / `comment` / `parsed_ok` / `dispatch_mode` / `status` / `source` / `model`

- `source`：`tradingview` | `manual` | `limit_watch`（历史空值按 tradingview）
- `model`：`normal` | `strategy`（历史空值按 normal）
- `status`：`dispatching` | `rejected` | `duplicate` | `done` | `partial` | `failed`

**`signal_dispatch`（normal 分发型）**
`node_id` / `decided_vol` / `gate_result`(passed/skipped) / `skip_reason` / `status` / `retcode` / `order_ticket` / `deal` / `price` / `error` / `dispatched_at` / `finished_at` + 时延埋点 `sent_at` / `recv_at` / `order_at` / `filled_at` / `queued_ms` / `order_to_fill_ms`

**`group_signal_task`（strategy 主任务）**
`task_id` / `group_id` / `group_name` / `strategy_id` / `strategy_name` / `node_count` / `status` / `skip_reason` / `total_orders` / `total_volume` / `realized_profit` / `opened_at` / `finished_at`

**`group_task_dispatch`（strategy 节点子任务）**
状态/跳过原因/魔术号/持仓笔数/挂单笔数/累计手数/已实现盈亏 + 与 normal 同名的时延埋点列

**`group_task_event`（策略执行事件流）**
`event_type`（open / add_counter / grid_add / close_partial / close_all / error …）/ `price` / `volume` / `profit` / `message` / `detail_json`

**`mt5_deal`（成交明细）**
`node_id` / `position_id` / `symbol` / `entry_type` / `net_profit` / `magic` / `signal_id` / `task_id` / `group_id` / `source`(strategy/signal/manual/terminal) / `deal_time` / `server_time_offset`

### 1.3 已有的统计口径（必须复用，不要重写）

`backend/app/deal_stats.py` 是**成交统计口径的唯一实现**，已被订单历史页 / 节点概览 / 带单页共用：

- 窗口：`7d / 30d / 90d / all`
- 净盈亏：`net_profit = profit + swap + commission`，且只统计出场类 `OUT / INOUT / OUT_BY`
- 胜率：**按 `position_id` 聚合（持仓级）**，不是按成交笔数
- 收益率分母：**窗口内最早一笔成交的 equity**（不是期末、不是 max）
- 分日：UTC+8 自然日，且先做 `真实UTC = deal_time - server_time_offset` 换算
- 分日不在 SQL 里做（MySQL/SQLite 方言不同），Python 侧累加

**信号统计必须遵循同一套哲学**：口径集中在 `signal_stats.py` 一处，所有页面读同一份结果。

### 1.4 现有接口缺口

| 缺口 | 说明 |
|---|---|
| 无统计接口 | 只有 `GET /api/events/signals`（分页列表，管理员） |
| 无筛选 | `page` / `page_size` 仅此两项，无法按品种/来源/状态/节点/时间筛选 |
| 链路割裂 | `normal` 与 `strategy` 分表分页，无统一流 |
| 无跨节点聚合 | 没有「某节点收到多少信号 / 成功率」这类维度 |
| 无时延视图 | 时延埋点已落库但只在单条明细里显示，无分布统计 |
| 无可见性设计 | `events` 菜单 `assignable=False`（管理员专属），普通用户完全看不到自己的信号 |

---

## 2. 页面定位与信息架构

### 2.1 定位

**信号管理中心 = 管理员/用户的「信号全景台」**：一眼看清「信号从哪来、去了哪、成功没有、卡在哪、花了多久」。

**与现有页面的关系**：

| 决策 | 说明 |
|---|---|
| **升级而非新建菜单** | 复用现有 `events` 菜单 code（`MENU_EVENTS`，`assignable=False`），菜单名从「事件」改为「信号中心」。不新建 `signal_center` 菜单，避免权限体系膨胀与角色配置迁移 |
| **保留 `/events` 路径** | 路由 `/events` 保留，新增 `/signal-center` 别名；老链接不失效 |
| **内部分 5 个 Tab** | 概览 / 信号流 / 节点命中 / 策略任务 / 时延 |
| **旧【分组管理→信号】页保留** | 它是「按分组」视角的运维页（停止任务、看子任务），管理中心是「全局统计」视角，职责不同不合并 |

### 2.2 布局（对齐原型首页 hero 骨架）

```
┌───────────────────────────────────────────────────────────────┐
│ ① Hero 统计卡（.pf-hero）                                     │
│   左：大数字「今日信号 1,284」+ 图标                           │
│       + 状态 chips：受理 1,210 · 拒收 38 · 部分成功 36         │
│   中：状态分布条（done/partial/failed/rejected 占比）         │
│   下：4 个指标格（.pf-metrics）                                 │
│       成功率 94.2% | 目标节点 3.6/条 | 端到端 P50 82ms | 跳过 61 │
├───────────────────────────────────────────────────────────────┤
│ ② 频道切换（.pf-tabs）：概览 | 信号流 | 节点命中 | 策略任务 | 时延 │
├───────────────────────────────────────────────────────────────┤
│ ③ 筛选条（.pf-seg + .pf-input）                                │
│   时间窗 7d/30d/90d/all | 来源 | 链路 | 品种 | 状态 | 节点 | 搜索 │
├───────────────────────────────────────────────────────────────┤
│ ④ 内容区                                                        │
│   概览：图表网格（echarts）+ 排行列表                           │
│   信号流：.pf-rows 统一流（normal + strategy 混合，游标分页）   │
│   节点命中：节点分组卡片（原型首页节点卡样式）+ 每节点统计条    │
│   策略任务：主任务列表 + 展开子任务（复用 GroupSignalsView 组件）│
│   时延：两段耗时分布 + 端到端分位 + 慢节点榜                    │
└───────────────────────────────────────────────────────────────┘
```

### 2.3 各 Tab 视觉与内容

#### Tab 1 · 概览

| 区域 | 内容 | 原型依据 |
|---|---|---|
| Hero | 今日信号数 + 状态 chips + 分布条 + 4 指标格 | 首页 `stat-hero` / `stat-dist-bar` / `stat-hero-metrics` |
| 趋势图 | 近 30 天信号量柱状 + 成功率折线（双轴） | echarts（`ProfitBarChart` 已有范式） |
| 来源分布 | 环形图：TradingView / 手动触发 / 限价监听 | 原型「多空比」条形语汇 |
| 品种 Top | 横向条形：信号量 + 成功率 | 原型节点总览的分组卡片 |
| 拦截原因 Top | 横向条形：按 `skip_code` 归类计数 | 原型「节点总览」指标格语汇 |
| 链路对比 | normal vs strategy 两列：条数 / 成功率 / 平均目标数 | 原型 K线卡的次要信息行 |

#### Tab 2 · 信号流（统一流）

- 一行一条信号，**混排两条链路**，用 `pf-pill` 标注链路（币种 / 分组）
- 列：时间 · 来源药丸 · 动作 · 品种 · 手数 · 链路 · 目标节点数 · 成功/跳过/失败徽标 · 耗时
- 展开行（`.pf-collapse-body`）显示：
  - normal → 节点分发明细（节点 / 状态 / 决策手数 / 跳过原因 / MT5 retcode / 两段时延）
  - strategy → 分组主任务 + 节点子任务 + 子任务事件流
- **游标分页**：`(received_at, signal_id)` 复合游标，避免新信号插入时行号分页跳行/重复

#### Tab 3 · 节点命中

- 按 owner 分组（原型首页「管理员 / 风控」分组卡片），每组头显示 `在线 x/y`
- 每节点卡：状态点 · 节点名 · 徽章（主节点）
  - `收到 128 条 · 成功 121 · 跳过 7 · 失败 0`
  - `成功率 94.5% · P50 78ms`
  - 主拦截原因一行
- 点击 → 该节点的信号流水（复用 Tab 2 带 node_id 筛选）

#### Tab 4 · 策略任务

- `group_signal_task` 主任务列表 + 展开 `group_task_dispatch` 子任务
- **直接复用 `GroupSignalsView.vue` 的渲染逻辑**，抽成共享组件 `StrategyTaskTable.vue`，避免两页两套
- 追加统计列：主任务累计手数 / 已实现盈亏 / 平均耗时

#### Tab 5 · 时延

- **两段耗时分布**（节点内，同机可比）：节点排队 `queued_ms` / 券商往返 `order_to_fill_ms` → 直方图 + P50/P90/P99
- **端到端**（服务端时钟，跨节点可比）：`finished_at - sent_at` → 分位折线（按时间）
- **慢节点榜**：P90 最高的 10 个节点
- 明确标注：哪些数字可跨节点比较，哪些不可（见 §3.3）

---

## 3. 底层数据统计方案

### 3.1 三层架构

```
┌─ 事实层（已有，只读，不动）────────────────────────────┐
│ signal_history / signal_dispatch                        │
│ group_signal_task / group_task_dispatch / group_task_event │
│ mt5_deal                                                │
└────────────────────────────────────────────────────────┘
                          ↓
┌─ 计算层（新增 signal_stats.py，口径唯一实现）───────────┐
│ 统一索引 → 统一状态归一 → 统一跳过原因归类 → 统一时延口径   │
└────────────────────────────────────────────────────────┘
                          ↓
┌─ 服务层（新增 signal_center_api.py）────────────────────┐
│ 5 个读接口，全部走 signal_stats，无第二份口径            │
└────────────────────────────────────────────────────────┘
```

### 3.2 核心设计原则（写进代码注释，避免后续漂移）

1. **口径唯一实现**：`signal_stats.py` 是信号统计的唯一实现，所有页面读同一份结果。参照 `deal_stats.py` 的既有做法。
2. **两层计数分离**：
   - **信号级（signal-level）**：一条 `signal_history` = 1 个信号。指标：信号数、受理数、拒收数、去重数。
   - **分发型（dispatch-level）**：一条明细 = 1 次「信号→节点」投递尝试。指标：投递数、通过数、跳过数、失败数。
   - **两者不可混用**：成功率必须写清是「信号成功率」还是「节点投递成功率」。
3. **时间轴单一来源**：跨节点可比的时间只有服务端时钟（`sent_at` / `dispatched_at` / `finished_at`）。节点上报的 `recv_at`/`order_at`/`filled_at` 是节点本地钟，**只用于同机差值**（`queued_ms` / `order_to_fill_ms`），不得参与跨节点求和或端到端计算。
4. **时区与服务器偏移修正**：成交相关时间沿用 `deal_stats` 的 `真实UTC = deal_time - server_time_offset` + UTC+8 自然日归集。信号侧 `received_at` 是服务端本地 naive 时间，直接按服务端本地日切分即可（需在文档中写明）。
5. **禁止解析中文做统计**：跳过原因必须走结构化 `skip_code`（§3.4），中文 `skip_reason` 只用于人读展示。

### 3.3 指标定义表

| 指标 | 定义 | 来源 | 备注 |
|---|---|---|---|
| 信号总数 | `COUNT(signal_history)` | 事实层 | 按窗口/筛选条件 |
| 受理数 | `status IN (dispatching, done, partial, failed)` | signal_history | 真正进入分发的 |
| 拒收数 | `status = rejected` | signal_history | 分发前被拒（无匹配分组 / 品种未配置） |
| 去重数 | `status = duplicate` | signal_history | 5 秒幂等抑制 |
| 信号成功率 | `COUNT(status=done) / COUNT(受理)` | signal_history | 注意：全部跳过时状态也是 `done`（见 `persist._aggregate_signal_status`），需与 dispatch 级区分 |
| 投递总数 | `COUNT(dispatch ∪ subtask)` | 两张分发表 UNION | |
| 节点投递成功率 | `(done) / (done+failed+skipped)` | 分发表 | 排除 pending/sent |
| 目标节点数（均值） | 投递数 / 信号数 | 计算 | 「一条信号平均打几个节点」 |
| 拦截率 | `skipped / 投递总数` | 分发表 | |
| 端到端 P50/P90/P99 | `finished_at - sent_at` 的分位数 | **仅服务端时钟** | 跨节点可比 |
| 节点排队 P50/P90 | `queued_ms` 分位数 | 节点内 | 不可跨节点求和 |
| 券商往返 P50/P90 | `order_to_fill_ms` 分位数 | 节点内 | 不可跨节点求和 |
| 策略成交转化 | `group_signal_task.total_orders` 分布 | strategy 链路 | |
| 策略已实现盈亏 | `group_signal_task.realized_profit` 汇总 | strategy 链路 | **注意口径与 `deal_stats` 不同**：这是任务收口时节点上报的累计值，与 `mt5_deal` 逐笔汇总可能不等，**不可与带单页盈亏混用**，需在 UI 上分开标注 |

> ⚠️ `group_signal_task.realized_profit` 与 `deal_stats` 的净盈亏是**两套口径**。管理中心里两者要分区展示并明确标注来源，绝不做加总。

### 3.4 跳过原因分类器（`skip_code`）

现状：跳过原因全部是中文自由文本散落在各处（`rules.py` / `group_rules.py` / `dispatcher.py`），直接正则解析会脆。

**方案：新增结构化列 + 历史回填兜底**

1. **新增列**（ORM + `db.py` 迁移，与既有 `_migrate_*` 同风格）：
   - `signal_dispatch.skip_code VARCHAR(24) NULL`
   - `group_task_dispatch.skip_code VARCHAR(24) NULL`
   - `signal_history.reject_code VARCHAR(24) NULL`（拒收原因）
2. **写入点补码**：所有 `_skip()` / 拒收分支已有语义化分支，直接补 `code`，例如：

   | skip_code | 含义 | 现有文案示例 |
   |---|---|---|
   | `config_missing` | 配置缺失 | 节点未配置：XAUUSD未在节点按币种配置中，拒收 |
   | `login_mismatch` | 登录号不一致 | MT5 登录号与节点绑定不符 |
   | `risk_trend` | 趋势风控拦截 | 趋势风控：信号 BUY，当前趋势为<多/空>…已拦截 |
   | `risk_position` | 持仓过滤 | 持仓过滤：已持有同品种XAUUSD仓位(N笔)… |
   | `risk_interval` | 区间方向过滤 | 方向与配置区间不符 |
   | `conflict_busy` | 资源冲突 | 并发控制：XAUUSD已有在途订单未完成 / 节点忙 |
   | `unavailable` | 目标不可用 | 分组无有效节点 / 节点离线 / 分组已禁用 |
   | `admission_rule` | 准入不通过 | 策略没有启用中的规则 / 网格方向不接受 SELL |
   | `admission_param` | 参数缺失 | 以损定量需要止损价 / 网格需要合法区间 |
   | `mismatch_scope` | 定向不匹配 | 品种不匹配 / 模版不匹配 |
   | `error_mt5` | 执行失败 | MT5 retcode != 0 / error 非空 |
   | `unknown` | 其它 | 兜底 |

3. **历史回填**：用正则分类器对存量数据回填 `skip_code`（一次性脚本，写 `backfill` 审计），分类器与枚举定义放同一模块，避免两处漂移。
4. **新增列不影响交易链路**：均为可空列，写入失败不阻塞交易（参照现有埋点「坏了不该影响交易链路」的原则）。

### 3.5 统一索引层（`signal_stats.py` 的核心）

两条链路的表结构不同，需要一层「归一化视图」，把两种分发明细抹平成同一形状再统计：

```python
# 归一化后的分发记录（内部结构，非 API 契约）
{
  "signal_id": str,
  "task_id": int | None,       # strategy 链路有
  "node_id": str,
  "group_id": str | None,      # strategy 链路有
  "model": "normal" | "strategy",
  "symbol": str | None,
  "action": str | None,        # BUY / SELL / CLOSE
  "dispatch_status": str,      # pending/sent/done/failed/skipped/offline
  "gate_result": str,          # passed / skipped
  "skip_code": str | None,
  "skip_reason": str | None,   # 人读
  "decided_vol": float | None,
  "order_ticket": int | None,
  "error": str | None,
  "sent_at": float | None,     # 服务端时钟
  "finished_at": float | None, # 服务端时钟
  "queued_ms": float | None,   # 节点内
  "order_to_fill_ms": float | None, # 节点内
}
```

**SQL 侧用 UNION ALL 把两张表拉平成一个 CTE**，一次查询出全部维度的聚合基础：

```sql
WITH dispatch_all AS (
  SELECT signal_id, node_id, 'normal' AS model, symbol, action,
         status AS dispatch_status, gate_result, skip_code, decided_vol,
         sent_at, finished_at, queued_ms, order_to_fill_ms
  FROM signal_dispatch
  WHERE sent_at >= :from_ts
  UNION ALL
  SELECT signal_id, node_id, 'strategy' AS model, symbol, action,
         status, 'passed', skip_code, decided_vol,
         sent_at, finished_at, queued_ms, order_to_fill_ms
  FROM group_task_dispatch
  WHERE sent_at >= :from_ts
)
```

> 注意：`signal_dispatch` 有 `action` 列（`signal_history.action`），`group_task_dispatch` 没有，需要从 `group_signal_task.action` 带出 —— 这会多一次 join。**替代方案**：给 `group_task_dispatch` 冗余一列 `action`（与 `symbol` 一样属于「冗余存一份便于统计」的设计，项目里已有此先例，见 `group_task_dispatch.symbol`）。**推荐冗余**，避免每次统计都 join 主任务表。

### 3.6 聚合策略（分阶段，不要一上来就上物化表）

**规模估算**（需在实施前用生产数据校准）：
- 假设 200 条信号/天 × 平均 5 个目标节点 = 1000 条分发明细/天
- 30 天窗口 ≈ 6000 条信号 / 30000 条明细 → **SQL 直接 group by 完全够用**（配合 `(sent_at)` 索引，毫秒级）

**Phase 1（本期实施）**：
- 直接 SQL 聚合明细表，**不建物化汇总表**
- 加 **Redis 缓存**：key `sigstat:{principal}:{window}:{filters_hash}`，TTL 60s（统计口径不要求实时，60s 足够）
- 写时**不碰**统计表 → 交易链路零侵入
- 唯一需要的写入改动：`skip_code` 列 + 冗余 `action` 列（可空、可失败、可后补）

**Phase 2（数据量 > 100 万行时再评估）**：
- 新增 `signal_stat_daily` 日汇总表：
  ```
  (day, source, model, symbol, node_id, group_id,
   signal_count, accepted_count, rejected_count,
   dispatch_count, passed_count, skipped_count, failed_count,
   skip_code_counts JSON,      -- 各拦截原因计数
   lat_count, lat_sum, lat_max, queued_sum, fill_sum)
  ```
- 写时增量：不在交易请求内做，而是 **后台批处理**（Redis Stream / 内存队列 + 每 5s 批量 upsert），交易请求只 `put` 一次
- 读策略：近 7 天读明细实时算（保证最新），7 天以上读日汇总表

### 3.7 时延口径的严谨性（这块最容易出错，单独说）

`latency_marks.py` 的文件头已经写明：**节点本地时钟与服务端时钟不同源，跨机绝对时刻不可比**。因此：

| 指标 | 计算式 | 可比性 | 展示要求 |
|---|---|---|---|
| 端到端时延 | `finished_at - sent_at` | ✅ 跨节点可比（都是服务端钟） | 主指标，Hero 与时延 Tab 主体 |
| 节点排队 | `queued_ms` = `order_at - recv_at` | ⚠️ 仅节点内可比 | 分节点展示，**禁止求和** |
| 券商往返 | `order_to_fill_ms` = `filled_at - order_at` | ⚠️ 仅节点内可比 | 分节点展示 |
| 发包 → 节点收到 | `recv_at - sent_at` | ❌ 跨机不可比 | **本期不展示**；若要展示需节点上报时钟偏移量，或用服务端 WS ping/pong RTT 近似 |

**实现约束**：`signal_stats` 只输出三类指标，端到端用服务端钟，节点内两段单独分组；任何跨节点聚合这两段的代码路径都不允许存在。

**分位数算法**：SQLite 与 MySQL 均无内置 `PERCENTILE_CONT`。项目现有做法是在 Python 侧累加（`deal_stats.daily_net_profit`）。此处同理：明细量级 3 万行/窗口，拉到 Python 侧排序取分位完全可行。

### 3.8 时间窗口与分日

- 窗口沿用 `deal_stats.WINDOWS`：`7d / 30d / 90d / all`（**不另立一套**，保持全站一致）
- 分日：信号侧 `received_at` 是服务端本地 naive 时间 → 按服务端本地自然日切分；成交侧沿用 `deal_stats` 的 UTC+8 + `server_time_offset` 修正
- **两者不可相加**（时区口径不同）：「每日信号量」按服务端日，「每日盈亏」按 UTC+8 日。若同一图里同时出现，需在图注中说明，或统一把信号侧也换算到 UTC+8（推荐，统一）

### 3.9 权限与数据可见性

| 角色 | 可见范围 |
|---|---|
| 管理员 | 全量（所有节点 / 分组 / 信号） |
| 普通用户 | 仅自己名下节点/分组相关的信号 |

实现要点：
- `signal_history` 本身**没有 owner 列**。可见性只能通过下游推导：
  - normal 链路：`signal_dispatch.node_id` → `nodes.owner_user_id`
  - strategy 链路：`group_signal_task.group_id` → `node_group.owner_user_id`
- 两种方案：
  - **A（查询时过滤）**：用 `EXISTS (SELECT 1 FROM nodes WHERE node_id = d.node_id AND owner_user_id = :uid)` 子查询。可行，但明细量级大时慢。
  - **B（冗余 owner）**：给 `signal_history` / `signal_dispatch` / `group_signal_task` 各加一列 `owner_user_id`，写入时快照。查询直接 `WHERE owner_user_id = :uid`。**推荐 B**，与项目里 `_OWNER_TABLES` 的既有做法一致（`db.py` 已有 owner 列迁移的范式）。
- 注意：`normal` 链路的 Webhook 信号是**全局配置发出**的（TradingView → 全部节点），对所有用户可见是合理的；过滤应作用在「节点明细」层，而不是信号层。**建议的最终规则**：
  - 信号级列表：普通用户只看**自己触发的**（`source=manual` 且 owner=自己）或**命中了自己节点**的
  - 分发明细级：只列自己名下节点的

### 3.10 幂等与一致性

- 「重复信号」（5 秒指纹去重）目前在 `webhook.py` 内存层处理，**未落库**（`signal_history.status` 里也没有 `duplicate` 值，`EventsView` 的 `signalTag` 里虽有 `duplicate` 但实际不会命中）。
  - **决策**：管理中心要统计「被去重抑制了多少次」→ 需要把去重事件落库。方案：新增 `signal_dedupe` 表，或在 `signal_history` 加 `duplicate_of` 列。
  - **推荐**：新增轻量表 `signal_dedupe(id, signal_id, fingerprint, hit_signal_id, created_at)`，只落被抑制的记录。
  - **非目标**：本期可以先不落，把「去重数」显示为「—（未采集）」，避免为统计而改交易热路径。

---

## 4. 接口设计

新增 `backend/app/signal_center_api.py`，前缀 `/api/signal-center`，权限 `require_menu(MENU_EVENTS)`（管理员）+ 用户级过滤。

### 4.1 `GET /api/signal-center/overview`

```jsonc
{
  "window": "30d",
  "from_ts": 1730000000,
  "totals": {
    "signals": 1284, "accepted": 1210, "rejected": 38, "duplicate": 12,
    "dispatch_total": 4620, "passed": 4380, "skipped": 201, "failed": 39,
    "signal_success_rate": 94.2,     // 信号级
    "dispatch_success_rate": 94.8,   // 分发级
    "avg_targets_per_signal": 3.6
  },
  "status_distribution": [{"status": "done", "count": 1150}],
  "by_source": [{"source": "tradingview", "count": 900, "success_rate": 95.1}],
  "by_model": [{"model": "normal", "count": 700}, {"model": "strategy", "count": 584}],
  "by_symbol": [{"symbol": "XAUUSD", "count": 320, "success_rate": 93.8}],
  "skip_reasons": [{"code": "risk_position", "label": "持仓过滤", "count": 88}],
  "daily": [{"date": "11-01", "signals": 40, "accepted": 38, "failed": 2}],
  "updated_at": 1730000000
}
```

### 4.2 `GET /api/signal-center/stream`

统一信号流（normal + strategy 混排），**游标分页**：

```
GET /api/signal-center/stream?cursor=<base64>&limit=20
    &window=30d&source=&model=&symbol=&status=&node_id=&group_id=&keyword=
```

```jsonc
{
  "items": [{
    "signal_id": "...", "received_at": 1730000000,
    "source": "tradingview", "model": "normal",
    "action": "BUY", "symbol": "XAUUSD", "volume": 0.1,
    "status": "done", "reject_code": null, "reject_reason": null,
    "targets": 5, "done": 4, "skipped": 1, "failed": 0,
    "elapsed_ms": 82,
    "group_task": null,            // strategy 链路：主任务摘要
    "dispatches": [ /* 明细，normal 链路 */ ]
  }],
  "next_cursor": "eyJ0cyI6MTczMDAwMDAwMCwiaWQiOiJzaWcifQ"
}
```

### 4.3 `GET /api/signal-center/nodes`

```jsonc
{
  "items": [{
    "node_id": "7-904093", "node_name": "主账户 A", "owner_user_id": 1,
    "is_primary": true, "status": "online",
    "signal_count": 128, "done": 121, "skipped": 7, "failed": 0,
    "success_rate": 94.5,
    "latency_p50_ms": 78, "latency_p90_ms": 210,
    "top_skip_code": "risk_position"
  }]
}
```

### 4.4 `GET /api/signal-center/latency`

```jsonc
{
  "window": "30d",
  "end_to_end": { "p50": 82, "p90": 240, "p99": 610, "samples": 4380 },
  "queued_ms":      { "p50": 12, "p90": 40, "scope": "node_local" },
  "order_to_fill_ms": { "p50": 68, "p90": 190, "scope": "node_local" },
  "slow_nodes": [{ "node_id": "...", "p90": 900 }],
  "daily": [{ "date": "11-01", "p50": 78, "p90": 220 }],
  "note": "queued_ms / order_to_fill_ms 为节点内耗时，不可跨节点比较或求和"
}
```

### 4.5 `GET /api/signal-center/tasks`

`group_signal_task` 主任务 + 子任务，参数与 `GET /api/groups/{id}/signals` 对齐，**额外支持跨分组查询**（`group_id` 可选）。

### 4.6 现有接口的兼容改造

`GET /api/events/signals` 保持不变（前端旧页面仍在用），新增筛选参数可选增量：
`source` / `model` / `symbol` / `status` / `node_id` / `from_ts` / `to_ts` / `keyword` —— 让【事件】页也能用上筛选器，避免两套列表逻辑。

---

## 5. 前端方案

### 5.1 页面与组件

```
frontend/src/views/SignalCenterView.vue        # 新页面（5 Tab）
frontend/src/components/signal-center/
  ├── SignalHero.vue            # hero 统计卡（.pf-hero）
  ├── SignalFilters.vue         # 筛选条（.pf-seg + .pf-input）
  ├── SignalStreamTable.vue     # 统一信号流（.pf-rows + 展开）
  ├── SignalOverviewCharts.vue  # echarts 图表组
  ├── NodeHitCards.vue          # 节点命中卡（原型节点分组卡）
  └── LatencyPanel.vue          # 时延面板
frontend/src/components/StrategyTaskTable.vue   # 从 GroupSignalsView 抽出的共享表格
```

### 5.2 需要新增的样式（`styles.css`，`pf-` 前缀）

参照原型首页实测类名补齐：

| 新类 | 对应原型 | 用途 |
|---|---|---|
| `.pf-hero` | `stat-hero` | 大数字 + chips + 分布条 + 指标格 |
| `.pf-hero-value` / `.pf-hero-label` | `stat-value hero` / `stat-label hero` | |
| `.pf-hero-chips` / `.pf-hero-chip` | `stat-status-chips` / `stat-chip` | 状态药丸组 |
| `.pf-dist-bar` / `.pf-dist-seg` | `stat-dist-bar` / `dist` | 状态分布条 |
| `.pf-metrics` / `.pf-metric` | `stat-hero-metrics` / `stat-metric` | **已有** `.pf-metrics`/`.pf-metric`，需补图标位 |
| `.pf-owner-group` / `.pf-owner-head` | 原型节点分组卡 | 节点按 owner 分组 |
| `.pf-chart` | — | echarts 容器（与原型 K 线卡留白一致） |

### 5.3 图表选型

项目已依赖 `echarts ^6.1.0`，`ProfitBarChart.vue` / `TrendPanel.vue` 有现成范式：
- 概览趋势：柱 + 折线双轴（`grid` + `series`）
- 来源分布：环形图（`pie`）
- 品种/原因 Top：横向条形（`bar` + `yAxis.type='category'`）
- 时延分位：`line` + `areaStyle`

统一色板取原型语义色：`#22C55E` 成功 / `#F59E0B` 警告 / `#F6465D` 失败 / `#2563EB` 主色 / `#A78BFA` 次要。

### 5.4 移动端

原型是移动优先（`max-w-[1200px]` 容器 + 窄屏堆叠）：
- Hero 指标格：4 列 → 2 列（≤640px）→ 1 列（≤420px），与 `.pf-metrics` 既有断点一致
- 信号流：桌面 `.pf-rows` 表格 → 卡片列表（沿用 `EventsView` 的 `mobile-only` / `desktop-only` 套路）
- 图表：高度压缩为 160px，横轴标签抽稀

---

## 6. 实施拆分（建议顺序）

| 阶段 | 内容 | 产出 | 风险 |
|---|---|---|---|
| **P0 口径落地** | `signal_stats.py` 骨架 + `skip_code` 枚举定义 + `signal_dispatch.skip_code` / `group_task_dispatch.skip_code` / 冗余 `action` 列 + `db.py` 迁移 | 只读统计可用，无 UI | 低（纯新增可空列） |
| **P1 统计 API** | `signal_center_api.py` 5 个接口 + Redis 缓存 + 归一化 UNION 查询 | 可用 curl 验证 | 中（聚合正确性） |
| **P2 概览页** | `SignalCenterView.vue` Tab1 + hero + 图表 + 筛选条 | 第一个可见成果 | 低 |
| **P3 信号流** | Tab2 统一流 + 游标分页 + 展开明细 | 替代【事件】页主体 | 中（分页一致性） |
| **P4 节点命中 / 策略任务** | Tab3 + Tab4（含 `StrategyTaskTable` 抽取） | 覆盖两条链路 | 中（组件抽取回归） |
| **P5 时延** | Tab5 + 口径注释 + 不可比提示 | 观测能力 | 中（口径正确性） |
| **P6 可见性** | `owner_user_id` 冗余列 + 用户级过滤 | 普通用户可用 | 中（迁移 + 权限回归） |
| **P7（可选）** | `signal_dedupe` 落库 / `signal_stat_daily` 物化表 | 数据规模 > 100 万行时 | 高，暂不做 |

---

## 7. 需要确认的决策点

1. **是否升级【事件】菜单为「信号中心」**（复用 `events` code，不新建菜单），还是**新建 `signal_center` 菜单**？后者需要角色配置迁移。
2. **普通用户是否可见信号管理中心**？涉及 `owner_user_id` 冗余与权限模型（P6 阶段）。
3. **「去重抑制次数」是否本期统计**？需要新增 `signal_dedupe` 落库（会轻微触及交易热路径），还是先显示「—（未采集）」。
4. **`group_signal_task.realized_profit` 与 `deal_stats` 净盈亏两套口径**：确认在 UI 上分区展示并标注来源，不做加总。
5. **分日时区**：信号侧是否统一换算到 UTC+8（与成交侧对齐）？默认建议：是。
6. **是否需要「导出 CSV / 报表」**？如需，追加 P8（走 `deal_stats` 同款口径）。

---

## 8. 附录：涉及文件清单（预估）

**后端新增**
- `backend/app/signal_stats.py`（统计口径唯一实现）
- `backend/app/signal_center_api.py`（5 个读接口）
- `backend/app/signal_codes.py`（skip_code / reject_code 枚举 + 标签 + 历史回填分类器）

**后端改动**
- `backend/app/orm.py`：`skip_code` × 2、`group_task_dispatch.action`、`owner_user_id` × 3
- `backend/app/db.py`：对应 `_migrate_*` 函数
- `backend/app/rules.py` / `group_rules.py` / `dispatcher.py` / `group_dispatcher.py`：写入点补 `skip_code`

**前端新增**
- `frontend/src/views/SignalCenterView.vue`
- `frontend/src/components/signal-center/*`（6 个组件）

**前端改动**
- `frontend/src/styles.css`：`.pf-hero` 系列
- `frontend/src/api/types.ts`：新响应类型
- `frontend/src/stores/hub.ts`：`fetchSignalCenter*` 系列
- `frontend/src/router/index.ts`：`/signal-center` 路由
- `frontend/src/views/MineView.vue`：菜单入口文案
- `frontend/src/views/GroupSignalsView.vue`：抽 `StrategyTaskTable`
