# 交易页【当前交易】tab 展示未成交挂单：技术方案

> 状态：待评审（未写代码）
> 关联页面：`frontend/src/views/TradingView.vue`
> 关联数据链路：`node_client/mt5_client.py` → `node_client/node_client.py` → `backend/app/ws_gateway.py` → `frontend/src/stores/hub.ts`

---

## 〇、一句话结论

**挂单数据后端早就有了，前端也早就有 `pendingOf()`，缺的只是「把它展示出来 + 给出按行撤单的手感」。**
本次改动 = 一处后端新端点（单张撤单）+ 一个前端聚合层 + 一个 UI 分区，**不动数据采集链路，不动节点协议**（单张撤单复用节点已有的 `cancel_pending` 命令）。

---

## 一、背景与目标

### 1.1 现状问题

【当前交易】tab 目前只渲染持仓（`positionBooks`），但同一节点的未成交挂单**在数据层已经完整存在**，却只被用于一个副作用计算（`pendingOf(symbol).length` 决定「删挂单」按钮是否可点）：

```1168:1216:frontend/src/views/TradingView.vue
        <div v-if="detailTab === 'positions'" class="tr-body">
          <p v-if="!positionBooks.length" class="tr-empty-line">暂无持仓</p>
          <section v-for="book in positionBooks" :key="book.symbol" class="tr-book">
```

用户视角的问题：

1. **限价开仓是「先挂单、后成交」**，成交前账户里没有任何持仓 → 用户在【当前交易】里看到的是空白，误以为单没发出去；
2. 挂单数量、方向、挂单价、SL/TP 全部不可见，只能切到首页/节点主页去看；
3. 只能「按品种批量删挂单」，不能逐张处理（比如只想撤掉其中一张）。

### 1.2 目标

| 编号 | 目标 | 验收 |
|---|---|---|
| G1 | 【当前交易】tab 内同时展示持仓与未成交挂单 | 有挂单时 tab 内可见挂单表格 |
| G2 | 挂单按品种分组、区分「策略单 / 手工单」、区分限价/止损单 | 视觉上能分辨 |
| G3 | 支持**逐张撤单**（行内操作） | 点单行「撤单」后该单从盘上消失 |
| G4 | 支持按品种批量撤单（已有能力，但需修正越界误撤，见 §6.2） | 撤 A 品种不影响 B 品种同魔术号的单 |
| G5 | 统计口径不污染持仓口径 | 顶部汇总的「持仓笔数/手数/浮动盈亏」仍只算持仓 |

### 1.3 明确的非目标（本期不做）

- **改挂单**（改挂单价 / 改 SL / 改 TP）：节点侧有 `modify_pending_order`，但后端**没有对应的 HTTP 端点**，需要新增 `cmd: modify_pending` 协议 + 节点改造，成本远高于收益，放到二期（§10）。
- **挂单历史 / 已撤挂单流水**：属于订单历史范畴，现有的【历史交易】tab 走的是 `deals`（成交）口径，不混。
- **挂单级别的盈亏试算**：挂单没有浮动盈亏概念，不做。

---

## 二、数据链路盘点（关键：大部分是现成的）

```
MT5 orders_get()
  └─ node_client/mt5_client.py :: pending_orders()          ← 已存在，含 pending_kind / price_current
       └─ node_client/node_client.py :: _pending_orders()    ← 已存在，随账户快照一起上报
            └─ WS "account" 消息 data.orders
                 └─ backend/app/ws_gateway.py :: _save_account_active()  ← 已存 snapshot["orders"]
                      ├─ Redis store.get_account(node_id)   → GET /api/nodes/{id}/account
                      └─ broadcast "account"                → WS 推前端
                           └─ frontend/src/stores/hub.ts :: applyWs('account') → hub.accounts[id].orders
                                └─ frontend/src/api/types.ts :: AccountSnapshot.orders?: PendingOrder[]
```

关键结论：

1. **后端零改动即可拿到挂单**：`AccountSnapshot.orders` 已经在类型定义里（`types.ts:266-267`），只是前端没用它渲染。
2. **刷新频率已满足**：交易页已有 5s 轮询 `hub.fetchNodeAccount`（`TradingView.vue:1043-1046` 的 `pollTimer`），挂单与持仓同一节奏刷新；WS 推送另有实时通道。
3. **节点协议无需改动**：单张撤单用的 `cmd: cancel_pending` 节点端已实现（`node_client.py:1395-1421`），服务端侧 `limit_watch.cancel_pending_on_node()` 已封装好 req_id 配对，只是**没有暴露成 HTTP 端点**给前端用。

### 2.1 现有字段 vs 缺口

| 字段 | 节点侧 | 后端透传 | 前端类型 | 说明 |
|---|---|---|---|---|
| `ticket` / `symbol` / `type` / `volume` / `price_open` / `sl` / `tp` / `magic` / `comment` / `time` | ✅ | ✅ | ✅ | 已满足展示 |
| `pending_kind`（`limit` / `stop`） | ✅ | ✅ | ❌ **缺** | 区分「限价挂单 / 突破挂单」，需补前端类型 |
| `price_current`（挂单当前市场价） | ✅ | ✅ | ❌ **缺** | 用于算「距现价 N 点」，属锦上添花，可二期 |

> `pending_kind` / `price_current` 后端是**原样透传**的（`ws_gateway.py` 里 `orders` 未做字段裁剪），所以只需前端补类型即可，**无需后端改动**。

### 2.2 需要新增的最小后端能力

| 能力 | 现状 | 缺口 |
|---|---|---|
| 批量撤单 | ✅ `POST /api/nodes/{id}/cancel-pending`，scope = `all` / `manual` / `strategy`(+magics) | 不支持按 ticket、不支持按 symbol 精确圈定 |
| 单张撤单 | 节点支持 `cmd: cancel_pending`；服务端 `limit_watch.cancel_pending_on_node()` 已封装 | **未暴露 HTTP 端点** |
| 改单 | 节点支持 `modify_pending_order` | 服务端、节点协议均缺 |

---

## 三、交互方案选型

| 方案 | 形态 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A. 同 tab 内分区** | 持仓区（上） + 挂单区（下），共用 tab | 一屏看全；不增加 tab 层级；与现有 `tr-book` 结构一致 | tab 内信息量变大 | ✅ **推荐** |
| B. tab 内二级子 tab | 【当前交易】→ 持仓 / 挂单 | 干净 | 多一次点击；两个口径被割裂，"这个品种有仓有单"要来回切 | 备选 |
| C. 新增独立 tab | 【挂单】 | 隔离最彻底 | tab 变多；与"当前交易"语义割裂；用户预期是"当前有什么" | 否 |

**推荐 A**，理由：
- 用户的真实心智是「我现在账户上有什么」，持仓和挂单都是「当前未终结的仓位」，拆开反而割裂；
- 挂单和持仓**按品种一一对应**，放在一起才能看出「XAUUSD 有 2 笔持仓 + 3 张挂单」；
- 前端已有 `tr-book`（品种卡片）+ `tr-magic-head`（魔术号分组）+ `tr-grid`（行）三件套，挂单区直接复用，视觉一致、零新样式负担。

### 3.1 推荐布局（最终形态）

```
┌─ tab: 当前交易  ⟨持仓 3 · 挂单 2⟩ ─────────────────────────────┐
│                                                                  │
│  [ 持仓区 ]  （现有 positionBooks，不改）                        │
│   XAUUSD   2 笔 · 0.03 手                    +12.30              │
│     魔术号 88888888  1 笔 · 0.01 手                +5.00           │
│       # 魔术号 方向 手数 开仓价 止盈/止损 浮动盈亏 操作            │
│       1  88888888  多  0.01  2400.00  2450/2380  +5.00  [平仓]  │
│                                                                  │
│  [ 未成交挂单 ]  ← 新增                                          │
│   XAUUSD   3 张 · 0.05 手                                        │
│     魔术号 88888888  2 张 · 0.03 手                              │
│       # 类型 方向 手数 挂单价 止盈/止损 挂单时间 操作              │
│       1  限价  多  0.02  2380.00  2450/2340  11-18 20:10 [撤单] │
│     手工单   1 张 · 0.02 手                                      │
│       # 类型 方向 手数 挂单价 止盈/止损 挂单时间 操作              │
│       2  限价  空  0.02  2450.00  —/—       11-18 19:02 [撤单] │
│                                                                  │
│   EURUSD   1 张 · 0.01 手     ← 有挂单但无持仓的品种也要出现       │
│                                                                  │
│  页脚：持仓 3 笔 · 挂单 4 张 · 合计手数 0.08 手                   │
└──────────────────────────────────────────────────────────────────┘
```

**关键布局决策**：

1. **品种维度合并**：挂单区与持仓区各自按 `symbol` 分组，但**品种集合取并集** —— 限价单未成交时该品种持仓为空，若只按持仓分组，这张单就"消失"了。这是本需求最容易踩的坑。
2. **魔术号分组**：挂单同样按 `magic` 分组（策略单 / 手工单 `magic=0` 单独归组），与持仓区的 `tr-magic-head` 视觉一致，用户能一眼看出"哪张单属于哪个策略"。
3. **挂单不参与持仓汇总**：顶部 `tr-sum` 的「当前持仓 / 合计手数 / 浮动盈亏」**口径不变**，只算 `positions`；挂单在页脚单独一行汇总（挂单无浮动盈亏，手数单列，不并入持仓手数）。

### 3.2 徽标口径

当前 tab 徽标是 `{{ totalPosCount }}`。改为分段显示：

- 持仓 `N` 笔 + 挂单 `M` 张，两者都 > 0 时显示 `N · M`；
- 用户能一眼看出"这个 tab 里既有仓也有单"。

---

## 四、前端详细设计

### 4.1 类型补齐（`frontend/src/api/types.ts`）

```ts
/** 未成交挂单：字段口径对齐节点侧 mt5_client.pending_orders() */
export interface PendingOrder {
  ticket: number
  symbol: string
  type: string
  volume: number
  price_open: number
  sl: number
  tp: number
  magic: number
  comment: string
  time: number
  /** 挂单类型：limit=限价（等回调）/ stop=突破（等突破）。节点上报，老节点可能缺失 */
  pending_kind?: 'limit' | 'stop' | string
  /** 挂单当前市场价；节点上报，老节点可能缺失 */
  price_current?: number
}
```

> 两个字段都是**可选**：老版本节点客户端上报的快照里没有，前端按 `undefined` 降级显示（类型列显示「挂单」）。

### 4.2 聚合层（`TradingView.vue`）

新增两个 computed，与现有 `positionBooks` 平级：

```ts
/** 全量未成交挂单（主节点） */
const allPending = computed<PendingOrder[]>(() => account.value?.orders || [])

/** 挂单按品种分组 */
interface PendingMagicGroup {
  magic: number
  rows: PendingOrder[]
  volume: number
}
interface PendingBook {
  symbol: string
  rows: PendingOrder[]
  groups: PendingMagicGroup[]
  volume: number
}
const pendingBooks = computed<PendingBook[]>(...)
```

分组规则（与 `positionBooks` 对称，但更简单）：

- 按 `symbol` 分桶，桶内按 `magic` 分组，`magic=0` 归为「手工单」（用现成的 `magicLabel(0)` → "手工单"）；
- 排序：品种按字母序；组内策略单按 magic 升序、手工单置尾（与持仓区 `positionBooks` 的 magic 排序规则一致）；
- 行排序：按 `ticket` 升序（同持仓区）。

### 4.3 渲染

在 `detailTab === 'positions'` 分支内，**持仓区之后**追加挂单区（`TradingView.vue:1214` 的 `tr-foot-note` 之前）：

- 复用 `tr-book` / `tr-magic-head` / `tr-grid` / `tr-line`；
- 新增一个列定义 `.tr-pend-grid`（8 列）：
  `#` / `类型` / `方向` / `手数` / `挂单价` / `止盈/止损` / `挂单时间` / `操作`
  （与 `.tr-pos-grid` 一样用 `grid-template-columns` 显式定义，`>720px` 断点行为保持一致）；
- 「类型」列：`pending_kind === 'stop'` → 标签「突破」，`limit` → 「限价」，缺失 → 「挂单」；
- 「方向」列复用现成的 `isBuySide(o.type)` + `fmtPosType(o.type)`（`type` 是 `BUY`/`SELL`，`fmtPosType` 已做小写归一）；
- 「挂单时间」复用 `fmtPosTime(o.time)`；
- 「操作」列：`撤单` 按钮（`tr-x` 样式，danger 语义），点击走 §5 的单张撤单。

空态：
- 持仓与挂单都为空 → `暂无持仓与挂单`（保留现有 `暂无持仓` 文案可改为"暂无持仓与挂单"）；
- 持仓为空但有挂单 → 持仓区显示 `暂无持仓`，**挂单区正常渲染**（这是限价开仓的典型状态）；
- 有持仓无挂单 → 挂单区显示 `暂无挂单`（一行 muted 小字，不占大块空间）。

### 4.4 页脚汇总

现有页脚：

```
当前持仓 {{ totalPosCount }} 笔 · 合计手数 {{ totalPosVolume.toFixed(2) }} · 浮动盈亏 {{ fmtSigned(totalPosPnl) }}
```

改为：

```
当前持仓 3 笔 · 合计手数 0.03 手 · 浮动盈亏 +12.30
未成交挂单 4 张 · 合计手数 0.05 手
```

**挂单手数不并入持仓手数**（并入会让「合计手数」变成两个口径的混合，失去意义）。

---

## 五、撤单能力设计

### 5.1 单张撤单（新增后端端点）

复用节点已有的单张撤单命令，只补一层 HTTP 皮：

```
POST /api/nodes/{node_id}/pending-orders/{ticket}/cancel
→ 服务端：owned_node_or_404 鉴权 → 节点在线检查
→ node_rpc.request(node_id, {"cmd":"cancel_pending","req_id":...,"ticket":...})
→ 节点执行 mt5.cancel_order(ticket)
→ 回包 {"success":bool,"ticket":int,"error":str?}
```

实现落点：`backend/app/pending_api.py`（与批量撤单同文件，天然共享鉴权 / 节点在线检查 / 审计）。

**必须复用** `limit_watch.cancel_pending_on_node()` 的两个配套动作，否则会出现误报：

1. `store.note_manual_cancelled_ticket(node_id, ticket)` —— 告诉限价监听"这张单是人工撤的"，避免限价监听把"单已不存在"记成 `cancel_failed`；
2. 撤单成功后触发一次账户快照刷新（或依赖既有 5s 轮询，见 §6.3）。

> 命名建议用 `POST /api/nodes/{id}/pending-orders/{ticket}/cancel`（REST 风格，与既有 `/cancel-pending` 并存不冲突）。若要更省事，也可扩展既有 `CancelPendingRequest` 加 `tickets: number[]`，但那会让一个端点同时承担「批量按范围」和「精确指定」两种语义，不如独立端点清晰。

### 5.2 前端行内撤单交互

```
用户点「撤单」
  → ElMessageBox 确认（确认文案含：品种 / 方向 / 类型 / 手数 / 挂单价 / 挂单时间 / 订单号）
  → hub.cancelPendingOrder(node_id, ticket)
  → 成功：ElMessage.success('已撤销挂单 #12345')，刷新账户快照
  → 失败：ElMessage.error(detail)
```

- 按钮在请求期间显示 `…` 并禁用（复用 `actionBusy` / `isClosing` 那套 busy 语义）；
- 节点离线时按钮禁用，title 提示「节点离线，无法撤单」（与 `HomeNodeView.vue` 的 `offlineReason()` 一致）。

### 5.3 批量撤单（复用 + 修正）

现有 `cancelSymbolPending(symbol)` 走 `hub.cancelPendingAll(node_id, { scope: 'strategy', magics })`。

**已存在的问题（必须修）**：节点侧 `cancel_pending_all(scope='strategy', magics)` **只按魔术号过滤，没有品种维度**。而交易页 `cancelSymbolPending` 传入的是"该品种挂单上的魔术号集合" → **同一魔术号在其它品种上的挂单会被一起撤掉**。交易页是"按品种"语义，首页是"按频道/节点"语义，两者混在一处，属于越界误撤。

修正方案（择一，建议 A）：

- **方案 A（推荐，后端加维度）**：扩展 `build_cancel_all_command` / 节点 `cancel_pending_all`，增加 `symbols` 过滤参数；交易页传 `{ scope: 'strategy', magics, symbols: [symbol] }`。
  - 代价：需同步升级**节点客户端**（老节点不认 `symbols`，会忽略 → 退化为当前行为，需在服务端对老版本节点降级并提示）。
- **方案 B（后端零改动，前端精确化）**：交易页的批量撤单**按 ticket 列表**逐张调用新的单张撤单端点。
  - 优点：后端与节点**完全不用动**，语义精确到张，不可能越界。
  - 缺点：N 张单 = N 次 RPC，撤单多时有延迟（可并发 + 汇总结果，与 `closeSymbolProfit` 逐张平仓的现有写法一致）。
  - 手工单（`magic=0`）当前被显式排除（提示"请到节点主页删除全部挂单"），方案 B 下可以**顺带支持手工单按张撤**，体验更好。

> 建议本期采用 **方案 B**，把"策略单/手工单一视同仁按张撤"作为增强；方案 A 作为后续把 `symbols` 维度下沉到节点协议时的优化。

---

## 六、边界与风险

### 6.1 口径边界

| 项 | 规则 |
|---|---|
| 挂单是否计入持仓笔数 | **否**。`totalPosCount` / `totalPosVolume` / `totalPosPnl` 保持只算 `positions` |
| 挂单手数是否并入"合计手数" | **否**。挂单手数单列一行汇总 |
| 挂单浮动盈亏 | 无此概念，不展示 |
| 网格 tab 的挂单计数 | 不动（`gridTaskPendingCount` 是任务维度的在途挂单数，与账户级挂单表不同口径） |
| 资产 tab 的品种分布 | 不动（只按持仓手数占比） |

### 6.2 已存在的越界风险（见 §5.3）

交易页"按品种删挂单"当前会误伤同魔术号其它品种的挂单。本期随单张撤单能力一起修正。

### 6.3 数据新鲜度与降级

| 场景 | 表现 | 处理 |
|---|---|---|
| 节点离线 | 挂单列表显示**最后一次快照**（可能已过期） | 页面上标注「节点离线，数据可能已过期」；撤单按钮禁用 |
| 快照降级（`books_ok=false`，终端与券商断联） | 后端保留上次快照并跳过对账 | 沿用后端语义，前端不额外处理；但挂单表不应显示为"已撤干净"，所以**撤单后不主动清空本地数据**，等新快照覆盖 |
| 老版本节点客户端 | 上报里没有 `orders` 字段 | `account.orders` 为 `undefined` → 挂单区显示「该节点客户端未上报挂单」而不是「暂无挂单」（**这两个含义完全不同**，不能混） |
| 挂单刚成交 | 快照还没更新 | 撤单按钮点了会返回 `order not found` 类错误 → 前端提示"该单已不在盘上"，并主动刷新一次账户快照 |
| `magic=0` | 手工单 | 单独归组展示「手工单」；撤单按钮同样可用（方案 B） |

### 6.4 其它风险

- **渲染规模**：网格策略可能在单品种挂几十张单 → 挂单区行数膨胀。建议对挂单区做**折叠默认收起**（有挂单时默认展开？我觉得默认展开更符合"当前交易"的语义），至少保证表格容器可滚动（`max-height` + `overflow:auto`），避免撑爆整页。
- **性能**：挂单表是纯 computed 派生，无额外请求；5s 轮询已存在，不新增网络开销。

---

## 七、后端改动清单

| # | 文件 | 改动 | 量级 |
|---|---|---|---|
| B1 | `backend/app/pending_api.py` | 新增 `POST /api/nodes/{node_id}/pending-orders/{ticket}/cancel`：鉴权 → 节点在线检查 → `limit_watch.cancel_pending_on_node()` → `note_manual_cancelled_ticket` → 触发账户快照刷新 | 中（~50 行） |
| B2 | `backend/app/models.py` | 如需定义请求/响应模型则加（可用裸 dict，省事） | 小 |
| B3 | `backend/app/pending_api.py` | 审计日志：人工撤单记 `manual_cancel_pending_ticket` | 小 |

**不需要改**：`node_client/*`（单张撤单命令已有）、`backend/app/ws_gateway.py`（透传已有）、`backend/app/models.py` 的 `build_cancel_all_command`（方案 B 不用）。

---

## 八、前端改动清单

| # | 文件 | 改动 |
|---|---|---|
| F1 | `frontend/src/api/types.ts` | `PendingOrder` 补 `pending_kind?` / `price_current?` |
| F2 | `frontend/src/stores/hub.ts` | 新增 `cancelPendingOrder(id, ticket)`（POST 新端点），返回 `{success, ticket, error?}` |
| F3 | `frontend/src/views/TradingView.vue` | 新增 `allPending` / `pendingBooks` 聚合；`pendingCount` 统计；行内撤单函数 `cancelPendingRow(order)`；busy 态 |
| F4 | `frontend/src/views/TradingView.vue` | 模板：tab 徽标改分段显示；挂单区渲染（复用 `tr-book`/`tr-magic-head`/`tr-grid`） |
| F5 | `frontend/src/views/TradingView.vue` | 样式：新增 `.tr-pend-grid` 列宽定义 + 挂单区滚动容器 |
| F6 | `frontend/src/views/TradingView.vue` | 修正 `cancelSymbolPending` 的越界语义（改为按张撤 / 或保留并加品种维度） |

---

## 九、实施步骤（建议顺序）

1. **S1**：`types.ts` 补字段 + `hub.ts` 加 `cancelPendingOrder`（前端能发出请求）
2. **S2**：后端新端点 B1（含鉴权、在线检查、人工撤单标记、审计）
3. **S3**：前端挂单区渲染（纯展示，不含撤单）→ 先让用户"看得见"
4. **S4**：前端行内撤单 + 确认弹窗 + 错误处理
5. **S5**：修正批量撤单语义（方案 B：按张撤 + 支持手工单）
6. **S6**：空态 / 降级文案（老节点、节点离线、无挂单三种空态分别措辞）
7. **S7**：回归（见 §十）

---

## 十、测试与验收

### 10.1 前端验收清单（手工）

| # | 场景 | 期望 |
|---|---|---|
| T1 | 主节点有持仓 + 有挂单 | 【当前交易】同时出现持仓区与挂单区，各自按品种分组 |
| T2 | 主节点**只有挂单**（限价开仓未成交） | 挂单区正常显示；持仓区显示「暂无持仓」 |
| T3 | 挂单所属品种**没有任何持仓** | 该品种依然出现在挂单区 |
| T4 | 挂单含策略单（magic≠0）与手工单（magic=0） | 分组标题分别显示「魔术号 xxx」与「手工单」 |
| T5 | 挂单 `pending_kind` 为 stop | 类型列显示「突破」，limit 显示「限价」 |
| T6 | 点行内「撤单」 | 确认弹窗 → 撤单成功提示 → 该行在下一次快照后消失 |
| T7 | 撤单时节点离线 | 按钮禁用 + 提示 |
| T8 | 撤一张已被成交/已撤的单 | 友好错误提示，且**不出现**「撤单失败」的红色误报 |
| T9 | 老节点（无 `orders` 字段） | 挂单区显示「该节点客户端未上报挂单」，不显示「暂无挂单」 |
| T10 | 撤 A 品种挂单 | B 品种同魔术号的挂单**不受影响**（回归 §6.2 的越界问题） |
| T11 | 顶部汇总 | 「当前持仓 / 合计手数 / 浮动盈亏」三个数字与改造前**逐位一致**（挂单不污染口径） |
| T12 | 大量挂单（>30 张） | 挂单区可滚动，页面不撑爆 |

### 10.2 后端验收

| # | 场景 | 期望 |
|---|---|---|
| B-T1 | 有权限用户撤自己的节点挂单 | 200 + `{success:true}`，节点侧该 ticket 消失 |
| B-T2 | 撤别人的节点 | 404（`owned_node_or_404`） |
| B-T3 | 节点离线时撤单 | 409 / 明确错误文案，不 500 |
| B-T4 | 撤单后读 Redis 账户快照 | `orders` 中该 ticket 消失（依赖节点下一次快照，最多 5s） |
| B-T5 | 人工撤单后，限价监听对同一 ticket 的处理 | 记为"已被人工撤"而非 `cancel_failed` |

---

## 十一、后续（不在本期）

| 项 | 价值 | 成本 |
|---|---|---|
| 改挂单（价 / SL / TP） | 用户可自助微调挂单 | 需新增 `cmd: modify_pending` 协议 + 节点改造 + 后端端点 |
| 挂单按距现价排序 / 距离显示 | 判断成交紧迫度 | 低（`price_current` 已有） |
| 挂单历史（已成交/已撤的入场单） | 完整审计 | 中（需后端落库） |
| 批量撤单下沉 `symbols` 维度到节点协议 | 减少 RPC 次数 | 中（需节点版本升级） |
| 挂单区列配置（用户自定义列） | 灵活性 | 低 |

---

## 十二、风险登记表

| 风险 | 影响 | 缓解 |
|---|---|---|
| 挂单口径与持仓口径混淆，导致「合计手数」误导 | 高 | 页脚分两行；顶部汇总口径不变；代码注释固化 |
| 按品种删挂单越界误撤 | 高 | 本期修正（方案 B）；单张撤单为默认路径 |
| 老节点无 `orders` 字段被显示成"无挂单" | 中 | 区分「未上报」与「无挂单」两种空态 |
| 节点离线时展示过期挂单误导用户 | 中 | 显式标注离线；禁用撤单 |
| 挂单量大导致页面卡顿 | 低 | 滚动容器 + 行数上限（>200 行折叠） |
| 撤单与限价监听同时动同一张单 | 中 | 复用 `note_manual_cancelled_ticket` + `cancel_pending:busy` 窗口 |
