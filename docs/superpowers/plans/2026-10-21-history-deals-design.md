# 历史成交数据采集与落地设计方案

> 本方案解决一件事：**把 MT5 终端里的历史成交（deals）持续采集到服务端并持久化**，
> 再驱动两个业务场景：
> 1. 【交易】Tab →【订单历史】重做，以 MT5 真实成交为唯一数据源（替换现有的 `signal_dispatch` 派发明细）
> 2. 【带单】页 → 带单员绩效从 mock 假数据换成真实统计
>
> 按可独立验证的阶段排列。设计决策见第 10 节，每条决策都注明了取舍理由。

---

## 目标与决策

### 目标

- **采集**：节点定时上报 + 服务端按需拉取，双通道覆盖 MT5 历史成交
- **存储**：Redis 热数据 + MySQL 持久化，`deal_id` 唯一约束保证幂等
- **应用一**：【订单历史】改为以 MT5 真实成交为唯一数据源（替换当前的"信号派发"视图）——**不再保留派发视图**
- **应用二**：带单员四项统计全部来自真实数据，与订单历史页口径完全一致

### 三条关键决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | **推送为主，拉取为辅** | 推送覆盖日常；拉取只在补历史（节点首次上线/换机）时用，避免每次都拉全量 |
| 2 | **`deal_history` 是【订单历史】的唯一数据源**，完整替换 `signal_dispatch` 派发视图 | 派发视图记录的是"我方意图"，看不到真实成交价/盈亏/手续费，更漏掉手工单、止损止盈、强平。MT5 终端真实成交才是用户要看的"订单历史" |
| 3 | **统计口径唯一化**：带单员统计 = 主节点统计，共用一套聚合函数 | 两个页面胜率不一致会直接摧毁数据可信度 |

---

## 关键约束（动手前必读）

### 约束 A：替换而非并存

【订单历史】Tab 不再有"派发 / 成交"双视图切换，原 `/api/nodes/{id}/dispatches` 端点退出本期范围。

- 现有 `signal_dispatch` / `signal_history` / `limit_watch_log` 表**保留**（被信号页、限价监听页、风控审计共用），只是不再被【订单历史】页查询
- 现有 `fetchNodeDispatches` 三个调用点（`TradingView.vue` / `NodeDetailView.vue` / `UserStrategiesView.vue`）的"订单历史"段全部改成 `fetchNodeDeals`
- 移除路径：`signal_dispatch` 派发明细的相关注释、`historyTotal` 计数、对应分页器

### 约束 B：关联信息只能用于展示，不能用于过滤

`signal_dispatch.order_ticket` 存的是 `result.get("order")`（下单回执），`deal_history.order_id` 是 MT5 的 `deal.order`。部分成交/手工单场景两者可能对不上。

**因此【订单历史】直接以 `deal_history` 为唯一数据源，LEFT JOIN 只补展示字段**。绝不能因为 JOIN 不到就把成交行丢掉——那会让手工单重新消失。

### 约束 C：MT5 调用在专用单线程

```110:117:node_client/node_client.py
        # MT5 的 Python API 非线程安全：所有阻塞调用固定在这一个线程里排队，
        # 否则风控清仓与策略收口会并发对同一批持仓发单，后到的那笔必被拒
        self._mt5_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
```

采集历史成交必须走同一个 `_exec`，不能另开线程调 MT5。

### 约束 D：带单员统计只算主节点

带单员名下次要节点多是跟单账户，算进去等于拿别人的成绩。口径以 `is_primary=True` 的节点为准。

---

## 现状分析

### 【订单历史】Tab 的四个硬伤

当前链路：

```
TradingView.vue fetchHistory()
  → hub.fetchNodeDispatches(node_id, page, page_size)
  → GET /api/nodes/{id}/dispatches
  → persist.recent_dispatches()   [SignalDispatch LEFT JOIN SignalHistory]
```

| # | 问题 | 具体表现 |
|---|------|----------|
| 1 | **成交价不可信** | `price` 来自 `trade_result` 的下单回执，是下单价不是成交价。滑点、限价部分成交全对不上 |
| 2 | **完全没有盈亏** | 没有 profit / commission / swap 字段，用户无法判断这单赚还是亏 |
| 3 | **失败单混在里面** | 被券商拒单、被风控拦下的信号也占一行，但根本没产生成交 |
| 4 | **漏单** | 手工在 MT5 终端下的单、止损止盈触发、券商强平——完全不进 `signal_dispatch` |

**为什么不保留"派发视图"作为诊断视图**：派发明细本来就是"我方意图 + 对方回执"的日志，归属信号页和审计页更合理（"信号事件"页已有这些数据），不属于"订单历史"Tab。诊断场景可以另外开"信号明细"Tab 或继续走节点详情页/信号事件页，不必挤在【订单历史】里。

### 【带单】页统计全是 mock

```69:87:frontend/src/views/FollowView.vue
// TODO: 后续由历史订单数据驱动。存储结构：_MOCK_STATS[leaderId] = stats
// @ts-ignore
const _statsDb: Record<number, LeaderStats> = {}

function getMockStats(leaderId: number): LeaderStats {
  // 用 leaderId 的哈希模拟不同带单员数据，后续接入真实统计时替换为 store 查询
  const base = (leaderId * 7 + 13) % 100
```

四项统计的真实来源应该是：

| 展示项 | 真实来源 | 来自订单数据？ |
|--------|----------|----------------|
| 累计收益 | Σ(net_profit) 出场成交 | ✅ 是 |
| 收益率 | 累计收益 ÷ 区间期初权益 | ✅ 是 |
| 胜率 | 盈利持仓数 ÷ 总持仓数（按 position_id 聚合） | ✅ 是 |
| 跟单人数 | COUNT(DISTINCT follower_user_id) | ❌ **来自跟单关系表** |

> **「跟单人数」与订单数据无关**，可独立上线，不必等 deals。

---

## 一、采集链路

### 1.1 整体架构

```
┌─────────────────┐      WebSocket       ┌─────────────────┐
│   node_client   │ ────────────────────▶ │   ws_gateway    │
│  (MT5 Terminal) │  deals_report        │                 │
│                 │                       └────────┬────────┘
│  ① 定时采集      │                                │
│  ② 响应拉取      │◀──────────────────────┐        │
└─────────────────┘  history_deals_pull  │        │
                                           │        ▼
                                    ┌──────┴──────────────┐
                                    │  deal_persist.py    │
                                    │  （幂等入库）        │
                                    └──────┬──────────────┘
                                           │
                                    ┌──────┴───────┐
                                    ▼              ▼
                             ┌──────────┐  ┌────────────┐
                             │  Redis   │  │   MySQL    │
                             │ (热数据)  │  │ deal_history│
                             └──────────┘  └────────────┘
```

### 1.2 推送模式（日常主力）

**触发时机**：与账户快照同周期（默认 30s），在 `_reporter` 循环里顺带采集。

```python
# node_client.py _reporter 循环内新增
async def _collect_new_deals(self) -> list[dict]:
    """采集自上次同步点之后的新成交（走专用 MT5 单线程）。"""
    cursor = await self._exec(self.mt5.deals_cursor)  # 本地持久化的同步点
    deals = await self._exec(self.mt5.history_deals_since, cursor["ts"], cursor["deal_id"])
    return deals
```

**增量游标**：`(last_deal_time, last_deal_id)` 二元组。只用时间戳会在同一秒内多笔成交时漏单，只用 deal_id 会在换账户时串号。

**本地持久化**：游标写入本地文件，节点重启后从断点继续。

### 1.3 拉取模式（补历史）

服务端在节点首次接入、或管理端手动触发时下发：

```json
{
  "cmd": "history_deals_pull",
  "data": {
    "from_time": 1697700000.0,
    "to_time": 1697800000.0,
    "chunk_size": 2000,
    "req_id": "uuid-xxx"
  }
}
```

节点按 `chunk_size` 分批回传，**避免一次拉几个月数据把 MT5 调用卡死**（与 `PROBE_BAR_CAPS` 同样的考虑）。

### 1.4 WebSocket 消息定义

| 消息 | 方向 | 说明 |
|------|------|------|
| `deals_report` | 节点→服务端 | 定时上报增量成交 + 账户快照 |
| `history_deals_pull` | 服务端→节点 | 请求补历史（分批） |
| `history_deals_result` | 节点→服务端 | 分批返回结果 |
| `deals_sync_state` | 节点→服务端 | 同步进度（供管理端监控） |

**`deals_report` 数据格式**：

```json
{
  "type": "deals_report",
  "data": {
    "cursor": {"ts": 1790000000.0, "deal_id": 5500123456},
    "deals": [
      {
        "deal_id": 5500123456,
        "order_id": 5500987654,
        "position_id": 4400112233,
        "symbol": "EURUSD",
        "type": 1,
        "entry": 1,
        "reason": 0,
        "volume": 0.1,
        "price": 1.0872,
        "profit": 15.20,
        "swap": -0.85,
        "commission": -0.50,
        "magic": 20240615,
        "comment": "close",
        "time": 1790000000.0
      }
    ],
    "account": {"balance": 10000.0, "equity": 10250.0}
  }
}
```

> **`account` 必须带**：收益率的分母（期初权益）依赖它，见 3.2。

### 1.5 MT5 客户端新增方法

`node_client/mt5_client.py` 需要补充（现有 `_history_deals` 是内部方法且只返回元组）：

```python
def history_deals_since(self, since_ts: float, after_deal_id: int = 0) -> list[dict]:
    """取 (since_ts, ∞) 内且 deal_id > after_deal_id 的成交，标准化字段返回。

    二元组过滤避免同秒多笔漏单：时间戳取宽窗口，deal_id 做精确去重。
    """
```

复用现有的 `mt5_book_rows` 模式——**读失败必须抛错，不能返回空列表**，否则同步游标会被误推进，永久丢数据。这是本项目反复强调的模式（见 `positions()` 的注释）。

---

## 二、数据模型

### 2.1 MySQL `deal_history`

```sql
CREATE TABLE deal_history (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    node_id       VARCHAR(32)  NOT NULL,   -- 节点
    mt5_login     INT          NOT NULL,   -- 冗余：便于按账号直接查

    -- MT5 原始标识
    deal_id       BIGINT       NOT NULL,   -- 成交唯一 ID
    order_id      BIGINT       NOT NULL DEFAULT 0,
    position_id   BIGINT       NOT NULL DEFAULT 0,   -- 持仓标识，胜率聚合的分组键
    magic         INT          NOT NULL DEFAULT 0,

    -- 交易信息
    symbol        VARCHAR(32)  NOT NULL,
    direction     VARCHAR(4)   NOT NULL,   -- BUY / SELL
    entry_type    VARCHAR(8)   NOT NULL,   -- IN / OUT / INOUT / OUT_BY
    reason        INT          NOT NULL DEFAULT 0,   -- 平仓原因（止损/止盈/手动）
    volume        FLOAT        NOT NULL,
    price         FLOAT        NOT NULL,

    -- 财务
    profit        FLOAT        NOT NULL DEFAULT 0,
    swap          FLOAT        NOT NULL DEFAULT 0,
    commission    FLOAT        NOT NULL DEFAULT 0,
    net_profit    FLOAT        NOT NULL DEFAULT 0,   -- profit+swap+commission（应用层算）

    -- 冗余账户快照（收益率分母）
    balance       FLOAT        NOT NULL DEFAULT 0,
    equity        FLOAT        NOT NULL DEFAULT 0,

    -- 关联（可空，查不到就留空）
    source        VARCHAR(16)  NOT NULL DEFAULT 'terminal',
    signal_id     VARCHAR(64),
    task_id       INT,
    group_id      VARCHAR(32),

    comment       VARCHAR(128) NOT NULL DEFAULT '',
    deal_time     DATETIME     NOT NULL,
    deal_time_ts  FLOAT        NOT NULL,
    backfill      TINYINT(1)   NOT NULL DEFAULT 0,   -- 是否为补历史导入
    synced_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_deal (deal_id),
    KEY ix_node_time    (node_id, deal_time_ts DESC),
    KEY ix_node_symbol  (node_id, symbol, deal_time_ts DESC),
    KEY ix_node_pos     (node_id, position_id),
    KEY ix_magic        (magic),
    KEY ix_signal       (signal_id),
    KEY ix_task         (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> **`uq_deal` 用 `deal_id` 全局唯一而非 `(node_id, deal_id)`**：MT5 的 deal_id 在单个账户内唯一，但不同账户可能撞号。加 node_id 组合更安全。**若确认全局唯一可去掉 node_id 维度**，减少一次索引查找。

### 2.2 MySQL `deal_sync_state`

```sql
CREATE TABLE deal_sync_state (
    node_id              VARCHAR(32) PRIMARY KEY,
    mt5_login            INT    NOT NULL,
    last_synced_time     FLOAT  NOT NULL DEFAULT 0,
    last_synced_deal_id  BIGINT NOT NULL DEFAULT 0,
    deals_synced         INT    NOT NULL DEFAULT 0,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

### 2.3 Redis

| Key | 类型 | TTL | 说明 |
|-----|------|-----|------|
| `deals:node:{node_id}` | Stream | 7d | 实时增量（回放 / 补写） |
| `deals:meta:{node_id}` | Hash | - | 同步游标 |
| `deals:lag:{node_id}` | String | 1h | 同步延迟（秒），告警用 |
| `stats:leader:{user_id}` | Hash(JSON) | 15min | **带单员预聚合绩效**（4 个窗口） |

### 2.4 数据写入策略

```
收到 deals_report
    ↓
Redis Stream 写入（立即，毫秒级）
    ↓
异步批量消费（每 60s，500 条/批）
    ↓
INSERT IGNORE INTO deal_history  ← uq_deal 保证幂等
    ↓
关联回填（signal_id / task_id / source）
```

**关联回填策略**（`source` 字段判定）：

| source | 判定依据 | 优先级 |
|--------|----------|--------|
| `strategy` | `magic` → `group_task_dispatch.magic` | 1 |
| `signal` | `order_id` → `signal_dispatch.order_ticket` | 2 |
| `manual` | `comment` 含 `close` / 手工平仓 API | 3 |
| `terminal` | 以上都无 → 用户直接在终端操作 | 4（兜底） |

> **回填失败的行照样入库**，`source='terminal'`、`signal_id=NULL`。查不到关联不代表这单不存在。

---

## 三、应用一：【订单历史】Tab（整体重做）

### 3.1 改造定位：直接换成成交明细，无双视图

```
┌─ 历史订单 ────────────────────────────────────────────┐
│  成交明细（唯一视图）                                    │
│    时间│品种│方向│开/平│手数│成交价│盈亏│手续费│...    │
│                                                       │
│  顶部：统计条（净盈亏 / 胜率 / 笔数，随筛选联动）         │
│  顶部：筛选器（时间 / 品种 / 方向 / 开平 / 来源）         │
│  底部：分页器                                            │
└───────────────────────────────────────────────────────┘
```

**UI 上没有任何 segmented 切换**，Tab 里就是成交明细本身。用户点【历史订单】想看的就是"我到底赚了多少"，成交明细才是答案。

**原派发数据去哪了**：见约束 A——`signal_dispatch` 表保留，信号页 / 限价监听页 / 审计页继续用，只是【订单历史】不再查它。

### 3.2 成交明细字段

| 列 | 来源 | 渲染要点 |
|----|------|----------|
| 时间 | `deal_time` | 本地时区，精确到秒 |
| 品种 | `symbol` | 等宽字体 |
| 方向 | `direction` | 复用现有 `.dir-tag` |
| **开/平** | `entry_type` | `开仓`/`平仓`/`双向`/`对冲平仓` |
| 手数 | `volume` | 右对齐 |
| 成交价 | `price` | **真实成交价** |
| **盈亏** | `net_profit` | 正绿负红 |
| **手续费** | `commission` | |
| **隔夜利息** | `swap` | |
| 订单号 / 成交号 | `order_id` / `deal_id` | 唯一性标识 |
| 来源 | `source` | `信号`/`策略`/`手工`/`终端` |

**方向陷阱**：一笔盈利的多单平仓，MT5 里 `type=SELL`（卖出平仓）。用户看到"SELL 平仓"会误以为又开了一单空仓。

**因此「方向」与「开/平」必须并排显示**，开仓行按 BUY 绿 / SELL 红，平仓行的方向色按"平掉的那个持仓的方向"取。

### 3.3 持仓级聚合（胜率的关键）

**一笔持仓可能分多次出场**（分批止盈 + 移动止损）。按 deal 计数会把一笔持仓算成多笔，胜率失真。

```sql
-- 按 position_id 聚合的持仓级损益
SELECT
    node_id,
    position_id,
    MAX(symbol) AS symbol,
    SUM(CASE WHEN entry_type IN ('OUT','INOUT','OUT_BY') THEN net_profit ELSE 0 END) AS net_profit,
    MIN(deal_time_ts) AS open_ts,
    MAX(deal_time_ts) AS close_ts,
    COUNT(*) AS deal_count
FROM deal_history
WHERE node_id = ? AND position_id > 0 AND deal_time_ts >= ?
GROUP BY node_id, position_id
```

胜率 = `net_profit > 0` 的持仓数 ÷ 总持仓数。

### 3.4 接口设计

#### 节点成交明细分页

```
GET /api/nodes/{node_id}/deals
```

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页（默认 50，最大 200） |
| `from_time` / `to_time` | 时间范围 |
| `symbol` / `direction` / `entry_type` / `source` | 筛选 |
| `keyword` | 模糊匹配订单号/成交号/备注 |

Response：

```json
{
  "items": [
    {
      "deal_id": 5500123456,
      "order_id": 5500987654,
      "position_id": 4400112233,
      "symbol": "EURUSD",
      "direction": "SELL",
      "entry_type": "OUT",
      "entry_label": "平仓",
      "volume": 0.1,
      "price": 1.0872,
      "profit": 15.20, "swap": -0.85, "commission": -0.50,
      "net_profit": 13.85,
      "source": "strategy",
      "source_label": "策略",
      "signal_id": "sig_20261021_0930",
      "task_id": 10231,
      "strategy_name": "网格吸筹-EURUSD",
      "deal_time": 1790000000.0
    }
  ],
  "total": 128,
  "page": 1,
  "page_size": 50,
  "summary": {
    "deal_count": 128,
    "position_count": 127,
    "net_profit": 1234.56,
    "win_positions": 74, "loss_positions": 53,
    "win_rate": 58.27,
    "commission_total": -32.10,
    "swap_total": -8.40
  }
}
```

> **`summary` 与 `items` 用同一 WHERE 条件**，前端直接展示，不自己算。

#### 分页稳定性

按 `(deal_time_ts DESC, deal_id DESC)` **复合排序**，不用 offset 分页。数据量大时 offset 会漂移（同秒成交排序不稳导致跨页重复）。

#### 统计接口（图表用）

```
GET /api/nodes/{node_id}/deals/stats?bucket=day|week|month
```

返回时间序列 + 分品种汇总。

### 3.5 前端改造

| 文件 | 改动 |
|------|------|
| `api/types.ts` | 新增 `DealRecord` / `DealSummary` / `PaginatedNodeDeals`；`NodeDispatchRecord` 保留（信号页仍在用） |
| `api/deals.ts`（新建） | `fetchNodeDeals()` / `fetchNodeDealStats()` |
| `stores/hub.ts` | 新增 `fetchNodeDeals()`；**`fetchNodeDispatches` 保留**（信号页等其它调用点仍需） |
| `components/DealsTable.vue`（新建） | 成交明细表格 + 筛选器 + 统计条 |
| `TradingView.vue` | 历史订单段**整体替换**为 `DealsTable`；删除 `historyItems` / `historyStatusClass` 等派发专用逻辑 |
| `NodeDetailView.vue` | 同上 |
| `UserStrategiesView.vue` | 同上 |

**三个页面复用同一个 `DealsTable` 组件**，只传 `nodeId`，避免三处重复实现。

**需删除的派发专用代码**（每个页面各一份）：

| 符号 | 用途 | 处置 |
|------|------|------|
| `historyStatusClass()` | status 着色（filled/pending/rejected） | 删除，成交明细无此状态语义 |
| `fetchHistory()` | 调 `fetchNodeDispatches` | 改名 `fetchDeals()`，改调新端点 |
| `historyItems` / `historyTotal` | 派发行数据 | 换成成交数据，类型换 `DealRecord` |
| `it.retcode` / `it.error` 列 | 错误码与错误文案 | **删除**（成交即成功，失败的单压根不在这里） |
| 「状态」列 | done/pending/failed | **删除**，改为「来源」列（信号/策略/手工/终端） |

> **「状态」列删除是个信息损失**，但方向是对的：用户点【历史订单】不该看到一堆"失败/跳过"的行——那些单根本没成交，不属于订单历史。要排查派发结果，去信号事件页。

---

## 四、应用二：【带单】页

### 4.1 统计口径（唯一权威）

**带单员绩效 = 主节点绩效 = `node_summary(node_id)`**，同一套函数，不是两套实现。

| 指标 | 公式 |
|------|------|
| 累计收益 | Σ(net_profit)，entry ∈ {OUT, INOUT, OUT_BY} |
| 收益率 | 累计收益 ÷ 区间期初权益 × 100% |
| 胜率 | net_profit>0 的持仓数 ÷ 总平仓持仓数（**按 position_id 聚合**） |
| 总交易笔数 | COUNT(DISTINCT position_id) |
| 跟单人数 | COUNT(DISTINCT follower_user_id)（**跟单关系表，非订单数据**） |

**手工单要不要计入？** 要。手工单也是本人真实交易，计入才完整——这正是引入 deal 数据的价值，比只看系统派发信号更全面。

### 4.2 期初权益怎么取（收益率的分母）

| 方案 | 问题 |
|------|------|
| 期末权益做分母 | 收益率随入金变化失真 |
| 固定基准 | 基准可能已很久，口径不清 |
| **区间期初权益** | ✅ 推荐，窗口语义清晰 |

**实现**：节点上报 deals 时带 `account` 快照，服务端把 `equity` 冗余进每条 deal。

```
期初权益 = 窗口内第一条成交（按时间升序）的 equity
```

**边界情况**：

| 情况 | 处理 |
|------|------|
| 无任何成交 | 收益率 = 0（**不返回 null**，避免前端 "NaN%"） |
| 期初权益 ≤ 0 | 收益率 = 0，`profit_rate_valid=false` |
| 区间内无成交 | 收益率 = 0 |

### 4.3 时间窗口

**可切换，默认「近 30 天」**。列表顶部放全局切换器，所有带单员同一窗口（否则没法横向比较）。

| 窗口 | 参数 |
|------|------|
| 近 7 天 | `7d` |
| 近 30 天 | `30d`（默认） |
| 近 90 天 | `90d` |
| 全部 | `all` |

卡片文案随之变化：`累计收益（近30天） +$1,234.56`

### 4.4 性能：绝不能 N+1

带单员列表有 N 个带单员，逐个查 DB 会卡死 SQLite。

```
后台任务（每 5 分钟）
    1. 扫描所有有 follow_leader 能力的用户的全部主节点
    2. 批量聚合 deal_history → 四个窗口的绩效
    3. 写 Redis stats:leader:{user_id}，TTL 15min
         ↓
GET /api/follow/leaders
    MGET stats:leader:{uid}  ← 一次拿全部
    Redis 未命中 → 返回 data_available=false（不回落查 DB）
```

**关键点**：
- TTL 15min > 刷新周期 5min，容忍任务偶发失败
- **未命中不回落查 DB**，保证接口延迟恒定
- 四个窗口一次性算好，切换窗口是纯读 Redis

### 4.5 接口设计

#### 带单员列表（改造现有端点）

```
GET /api/follow/leaders?q=&window=30d
```

```python
class LeaderPerformance(BaseModel):
    """带单员绩效（由历史成交聚合）。"""
    total_profit: float = 0.0
    profit_rate: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    gross_volume: float = 0.0
    commission_total: float = 0.0
    data_available: bool = False   # 节点未上线/无成交/同步未开
    updated_at: Optional[float] = None
    base_equity: float = 0.0        # 期初权益，便于排查异常
    profit_rate_valid: bool = True
```

`FollowLeaderSummary` 增加 `performance` 与 `perf_window`。

#### 绩效详情（下钻）

```
GET /api/follow/leaders/{leader_user_id}/performance?window=30d
```

返回 `performance` + `by_symbol` + `timeline` + `recent_deals`。

### 4.6 权限与数据可见性

**这是本方案最需谨慎的一点。**

| 数据 | 可见范围 | 理由 |
|------|----------|------|
| 聚合绩效（收益/胜率/笔数） | **所有登录用户** | 带单广场是公开的，绩效就是给潜在跟单者看的 |
| 分品种 / 时间线聚合 | 所有登录用户 | 维度更细，结论同样可公开 |
| **成交明细**（价位/手数/备注/订单号） | **仅本人 + 管理员** | 含具体价位，可能泄露策略参数 |
| 主节点名 / MT5 登录号 | 已公开（现状如此） | 保持 |

后端在 `performance` 端点校验 `p.user_id == leader_user_id or p.is_admin`，否则剥离 `recent_deals`。**前端非本人视角不渲染该区块。**

### 4.7 前端改造

`FollowView.vue`：

| 位置 | 改造 |
|------|------|
| `getMockStats()` | **删除** |
| `currentStats` computed | 读 `detail.leader.performance` |
| `leaderStatsMap` computed | 遍历 `leaders` 取 `l.performance` |
| `ZERO_STATS` | 保留（无数据占位） |
| 列表 4 项统计 | 真实值 + 窗口切换器 |
| 「跟单人数」 | 改用真实 `follower_user_count`（**可独立上线**） |

**「跟单人数」先行**是个便宜的改动：它不依赖 deals，能马上把 mock 换成真实值。

### 4.8 数据新鲜度

deals 是准实时（同步间隔 + 批量落库延迟），绩效必然滞后，**UI 必须标注**：

```
数据更新于 21:05（3 分钟前）
⚠ 该带单员数据同步延迟，可能未反映最新交易
```

预聚合任务需检查节点同步状态，滞后严重时标记 `stale=true`。

---

## 五、后端模块划分

```
backend/app/
├── deal_models.py     # ORM: DealHistory / DealSyncState
├── deal_store.py      # Redis 读写 + 预聚合任务
├── deal_persist.py    # 幂等入库 + 关联回填
├── deal_stats.py      # 统计口径唯一实现
└── deals_api.py       # REST 端点
```

**`deal_stats.py` 是口径唯一入口**：

```python
async def position_agg(node_id, from_ts, to_ts)   # 持仓级聚合（胜率分母）
async def node_summary(node_id, from_ts, to_ts)    # 节点级汇总
async def leader_batch(node_ids, from_ts, to_ts)   # 批量（预聚合用）
```

「带单员统计」直接调 `node_summary(主节点)`，**不另写一份**。

---

## 六、实施计划

各阶段可独立验证、独立回滚。

| 阶段 | 内容 | 前置 |
|------|------|------|
| **P0** | node_client 采集（`history_deals_since` + 游标持久化 + `deals_report`） | — |
| **P1** | 后端入库（ORM + 幂等写入 + WS 分派 + Redis） | P0 |
| **P2** | `deal_stats.py` 统计口径 + 节点级成交 API | P1 |
| **P3** | 【订单历史】Tab 重做为成交明细（三页面同步替换） | P2 |
| **P4** | 带单员预聚合任务 + Redis 统计缓存 | P2 |
| **P5** | 【带单】页替换 mock（绩效 + 跟单人数） | P4 |
| **P6** | 带单员绩效下钻（分品种/曲线/明细）+ 新鲜度提示 | P5 |

**P3 与 P5 可并行**（前后端分离）。

### 各阶段验证与回滚

| 阶段 | 验证方法 | 回滚方式 |
|------|----------|----------|
| P0 | 节点日志打印每轮采集笔数；重启后游标不丢 | 关闭 `DEALS_SYNC_ENABLED` |
| P1 | 手工构造重复 `deals_report`，确认库中无重复行 | 停 Redis 消费；表 DROP 即可 |
| P2 | 同一节点用两套口径手工核对胜率 | 端点不发布，无外部影响 |
| P3 | 手工在终端下一单，确认列表可见且 `source=terminal` | 把 `DealsTable` 的数据源切回 `fetchNodeDispatches`（**保留旧方法与旧类型一个月**） |
| P4 | 手动触发预聚合，确认 Redis key 生成 | 停定时任务；列表回落 `data_available=false` |
| P5 | 造几笔已知盈亏的成交，核对四项统计 | 恢复 `getMockStats`（**保留函数一个月**） |
| P6 | 非本人访问确认看不到 `recent_deals` | 下钻入口加开关 |

> **P5 的回滚要点**：替换 mock 时**保留 `getMockStats` 函数一个月**。统计口径万一有争议，能立刻切回去。

---

## 七、验收标准

### 【订单历史】Tab

- [ ] Tab 内**无任何视图切换控件**，只有成交明细
- [ ] 成交明细展示真实成交价、盈亏、手续费、隔夜利息
- [ ] **手工在 MT5 终端下的单也可见**，`source=terminal`
- [ ] 「状态」列已移除（不再有 failed/skipped 行）
- [ ] 统计条随筛选条件联动
- [ ] 分批出场只算一次胜率
- [ ] 分页稳定（复合排序，不重复不遗漏）
- [ ] 移动端卡片同步展示盈亏
- [ ] 三个页面（交易/节点详情/策略）均已切换，无残留派发字段

### 【带单】页

- [ ] `getMockStats` 已删除，无 mock 值残留
- [ ] 跟单人数来自真实跟单关系（distinct follower_user_id）
- [ ] 累计收益 / 收益率 / 胜率来自主节点真实成交
- [ ] 窗口切换即时生效（读 Redis，不查 DB）
- [ ] 列表接口 P95 < 300ms
- [ ] 非本人/非管理员看不到成交明细
- [ ] 显示数据更新时间，滞后有提示

### 一致性

- [ ] **同一节点在【订单历史】页与【带单】页的胜率、净盈亏完全一致**（同窗口下）

---

## 八、风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| MT5 历史查询慢/卡死 | 节点无响应 | 分批拉取（`chunk_size`）；走专用单线程；`PROBE_BAR_CAPS` 同理 |
| 读失败返回空列表 | **游标误推进，永久丢数据** | 读失败抛错（复用 `mt5_book_rows` 模式），绝不当空处理 |
| `deal_id` 跨账户撞号 | 唯一约束误判 | 唯一键用 `(node_id, deal_id)` 组合 |
| deals 未覆盖全部节点 | 部分带单员无数据 | `data_available=false` 占位，**UI 明示「暂无数据」而非显示 0** |
| 主节点频繁换绑 | 绩效历史断层 | 以 node_id 为准；`updated_at` 反映新鲜度 |
| 节点离线数天后补传 | 单次写入量大 | 500/批入库；`backfill=1` 标记，不计入实时统计 |
| 收益率分母受出入金影响 | 数值失真 | 标注统计窗口与期初权益；后续接出入金流水精细化 |
| 净持仓账户 INOUT | 开/平语义模糊 | `entry_label` 显示「双向」，盈亏直接用 MT5 的 profit |
| N+1 查询 | 接口超时 | 预聚合 + Redis MGET，**未命中不回落 DB** |

---

## 九、配置项

**node_client（.env）**

```ini
DEALS_SYNC_ENABLED=true
DEALS_SYNC_INTERVAL=60          # 秒
DEALS_BATCH_SIZE=500
DEALS_CURSOR_FILE=./deals_cursor.json
```

**backend（settings.py）**

```python
DEALS_REDIS_TTL       = 7 * 86400    # 热数据保留
DEALS_REDIS_STREAM_MAX = 100_000      # Stream 长度上限
DEALS_WRITE_BATCH     = 500          # 入库批大小
DEALS_AGG_INTERVAL    = 300          # 预聚合周期（秒）
DEALS_AGG_TTL         = 900          # 统计缓存 TTL（秒）
DEALS_STALE_THRESHOLD = 1800         # 同步延迟告警（秒）
DEALS_DEFAULT_WINDOW  = "30d"
```

---

## 十、设计决策记录

| # | 决策 | 取舍 |
|---|------|------|
| 1 | 推送为主，拉取为辅 | 拉取全量历史在 MT5 上很慢且占终端资源；推送覆盖日常，拉取只在补历史时用 |
| 2 | `deal_id` 唯一键含 node_id | 若确认 MT5 deal_id 全局唯一可简化，但组合键更保险，代价仅一次索引查找 |
| 3 | 【订单历史】**完全替换**派发视图，不做双视图 | 派发明细属于"信号/审计"语义，放在"订单历史"Tab 里是错位。诊断需求另走信号事件页。双视图会让用户在两个都叫"历史订单"的表格间困惑，且两份数据行数对不上（成交数 ≠ 派发数） |
| 4 | 关联信息只展示不过滤 | 宁可关联不上，也不能丢成交 |
| 5 | 胜率按 `position_id` 聚合 | 比按 deal 计数准，但 SQL 复杂一层。分批止盈场景下差别巨大 |
| 6 | 收益率用区间期初权益 | 需要在 deal 表冗余 equity。比分两次查余额简单 |
| 7 | 带单员统计只算主节点 | 少算了他名下跟单账户的成绩，但口径清晰、不会重复计算 |
| 8 | 预聚合 + Redis，未命中不回落 DB | 数据可能滞后最多 15 分钟，但接口延迟恒定 |
| 9 | 四窗口一次算好 | 预聚合任务稍重，但切换窗口零延迟 |
| 10 | 成交明细限本人+管理员 | 聚合公开、明细不公开，平衡宣传需求与策略隐私 |
| 11 | 复合排序分页而非 offset | 深翻页更快更稳 |
| 12 | `backfill` 标记区分补历史 | 补传数据不干扰实时统计口径 |

---

## 附录：相关文件

| 文件 | 说明 |
|------|------|
| `node_client/node_client.py` | 节点主入口，`_reporter` 循环（第 464 行）、`_exec` 单线程（第 250 行） |
| `node_client/mt5_client.py` | MT5 封装，`_history_deals`（第 1029 行）、`mt5_book_rows`（第 137 行） |
| `backend/app/ws_gateway.py` | WS 消息分派（`_session` 第 240-280 行） |
| `backend/app/redis_store.py` | Redis key 管理 |
| `backend/app/orm.py` | ORM 模型 |
| `backend/app/persist.py` | `recent_dispatches`（第 357 行）——**本期不再被订单历史页调用**，但信号页仍用 |
| `backend/app/nodes.py` | `/api/nodes/{id}/dispatches`（第 144 行）——**保留端点**（其它调用点仍需），新增 `/api/nodes/{id}/deals` |
| `backend/app/follow_api.py` | 带单员列表/详情组装（第 180-190、258-274 行） |
| `frontend/src/views/TradingView.vue` | 【交易】Tab 历史订单（第 366-470、1047-1136 行）——**P3 需整体替换** |
| `frontend/src/views/NodeDetailView.vue` | 节点详情页历史订单（第 436-460 行）——**P3 需整体替换** |
| `frontend/src/views/UserStrategiesView.vue` | 策略页历史订单（第 215-235 行）——**P3 需整体替换** |
| `frontend/src/views/FollowView.vue` | 【带单】页 mock 统计（第 60-108 行） |
| `frontend/src/stores/hub.ts` | `fetchNodeDispatches`（第 110-122 行）——**保留**，新增 `fetchNodeDeals` |