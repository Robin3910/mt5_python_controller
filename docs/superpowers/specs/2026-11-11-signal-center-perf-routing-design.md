# 信号中心：绩效统计 + 路由中枢（设计文档）

> 日期：2026-11-11
> 分支：`v4_dev_reset`
> 范围：只针对 **AI 智能加仓（`tpl_1`）** 的信号级 / 持仓级胜率统计；信号中心从
> 「只读看板」升级为「信号路由中枢」。

---

## 〇、本轮拍板的决策点（已确认）

| # | 议题 | 结论 |
|---|---|---|
| 1 | 胜率口径 | **信号级 + 持仓级**两个层级。**不做档位级胜率** |
| 2 | 进行中的信号 | **不算输**，单列「进行中」，不进分母 |
| 3 | 同一信号多次路由到同一分组 | **拒绝**（409 + 返回已有进行中任务的 task_id） |
| 4 | 路由时是否允许改 sl/tp | **允许**，改动写进任务快照，可追溯 |
| 5 | 新增 `signal_entry_leg` 窄表 | **做**。把「每一次入场」展开成可聚合的行 |
| 6 | 分组 `signal_source` 默认值 | **默认 `direct`**（存量零改动）；`center` 需管理员显式开启 |
| 7 | 区间筛选用哪个价 | 节点账户快照的**最新报价**；不用持仓价（空仓取不到会误拦） |
| 8 | 首仓类型 vs 加仓原因 | **不统一**。两者来源不同，展示层并排呈现 |
| 9 | `type=smc-data` 是否本期做 | **后置**。独立表 + 独立面板 |
| 10 | 存量数据回填 | 周期/类型**不回填**；`signal_entry_leg` 的存量加仓档位**从 `deal.comment` + 快照反解回填** |

---

## 一、点数 / 指定价加仓没有周期，怎么统计

这是本设计里最容易做错的一环，先说清楚。

### 1.1 事实

`strategy_templates.BATCH_CALC_TYPES` 里八种计算方式，**只有一部分真正读 K 线**：

| calc_type | 是否依赖周期 | 周期字段的语义 |
|---|---|---|
| `point`（固定点数） | **否** | 字段存在但无意义 |
| `price`（指定绝对价） | **否** | 字段存在但无意义 |
| `atr` | 是 | 读多少根 K 线算 ATR |
| `range`（波幅） | 是 | 读已收盘 K 线的高低点总跨度 |
| `fvg` / `ob` / `pdz` / `fibo` | 是 | 周期用来**匹配 TV 推送的价位** |

也就是说：**`point` 与 `price` 的「周期」字段是配了但根本不读**（`normalize_batch_level`
无条件写入 `timeframe`，但节点侧 `CALC_BAR_TYPES` 只有 `atr` / `range`）。

### 1.2 结论：周期维度只对「有周期的入场」生效

**不做**「给 point / price 硬凑一个周期」的做法。那会把「点数间距」和「ATR 间距」混进
同一个周期桶，统计出的「M15 的胜率」一半是 ATR 口径、一半是点数口径，数字没有意义。

采用**两级语义**：

| 层级 | 字段 | 取值 | 说明 |
|---|---|---|---|
| L1 信号级周期 | `signal_history.timeframe` | `M15` / `H1` / … / `NULL` | **首仓信号**自带的周期，来自 TV 告警 `{{interval}}` |
| L2 入场级周期 | `signal_entry_leg.timeframe` | `M15` / … / `NULL` | **每一次入场**（首仓 + 每一档加仓）实际生效的周期 |

`signal_entry_leg.timeframe` 的写入规则：

```
calc_type ∈ {atr, range, fvg, ob, pdz, fibo} → 写入档位配置的 timeframe
calc_type ∈ {point, price}                   → 写 NULL（该入场不依赖周期）
```

### 1.3 统计时怎么用

- **按周期出胜率**：只用 **L1 信号级周期**（`signal_history.timeframe`）做分桶。
  这是「这条信号本身出现在哪个周期上」——与加仓方式无关，口径干净。
- **按入场原因出明细**：`signal_entry_leg` 逐行展示
  「入场原因 × 是否有周期 × 周期」，前端把 `point` / `price` 归到
  「无周期口径」一栏，**不参与任何按周期的胜率分桶**。
- **交叉分析**：`信号周期 × 首仓类型` 做交叉表（都取 L1 层级，天然一致）；
  `入场原因` 那一维单独出「入场笔数 / 已实现盈亏 / 胜率（持仓级）」，**不带周期**。

> 一句话：**周期是信号的属性，不是加仓的属性**。加仓的「入场原因」单独一维。

### 1.4 「入场原因」的完整词表

| 原因 | 类别 | 来源 | 周期 |
|---|---|---|---|
| `ob` | SMC | 档位配置 / TV 告警 | 有（匹配价位用） |
| `fvg` | SMC | 同上 | 有 |
| `pdz` | SMC | 同上 | 有 |
| `fibo` | SMC | 同上 | 有 |
| `atr` | 指标 | 档位配置 | 有 |
| `range` | 波幅 | 档位配置 | 有 |
| `point` | 点数 | 档位配置 | **无** |
| `price` | 指定价 | 档位配置 | **无** |
| `first` | 首仓 | TV 告警 | 取首仓信号的周期 |
| `manual` | 手动 | 中控台 / 限价监听 | 取信号周期 |

编码即数据（对齐 `signal_codes.py` 的既有范式）：**编码是数据，文字是展示**。

---

## 二、数据模型

### 2.1 `signal_history` 新增列

| 列 | 类型 | 说明 |
|---|---|---|
| `timeframe` | `VARCHAR(8)` NULL, INDEX | L1 信号级周期（`M15`/`H1`/…）。**缺就 NULL，绝不拒收信号** |
| `signal_type` | `VARCHAR(16)` NULL, INDEX | 首仓信号类型（`ob`/`fvg`/`bos`/…）。未知类型**保留原值小写**，不丢 |
| `route_state` | `VARCHAR(16)` NULL, INDEX | `pending` / `auto` / `routed` / `dropped` |
| `route_mode` | `VARCHAR(16)` NULL | `direct` / `center` |

`route_state` 语义：

- `pending`：已入信号中心，等待路由
- `auto`：直连模式已被分组自动消费
- `routed`：被信号中心选中下发过（**人工介入过**）
- `dropped`：被信号中心的筛选规则丢弃

> 一条信号既 `auto` 触发给 A 分组、又被人工 `routed` 给 B 分组时，
> `route_state = routed`（人工介入优先），`route_mode = center`。

### 2.2 `group_signal_task` 新增列

| 列 | 类型 | 说明 |
|---|---|---|
| `trigger_source` | `VARCHAR(16)` NULL, INDEX | `tv_webhook` / `manual` / `limit_watch` / `center` |
| `route_mode` | `VARCHAR(16)` NULL | `direct` / `center` |

`group_signal_task` 天然就是「信号 × 分组」的多对多表，**不需要新建路由表**。

### 2.3 新表 `signal_entry_leg`（每一次入场的权威账本）

```python
class SignalEntryLeg(Base):
    """信号入场明细：一条信号的一次入场（首仓 / 某一档加仓）。

    把「每一次入场都有他的原因」落成可聚合的窄行，避免每次统计都去解析
    group_signal_task.strategy_snapshot_json。
    """
    __tablename__ = "signal_entry_leg"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 入场序号：0 = 首仓；1+ = 第 N 档加仓
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # 入场原因：first / ob / fvg / pdz / fibo / atr / range / point / price
    reason: Mapped[str] = mapped_column(String(16), index=True)
    # 规则类型：1=逆势 2=顺势（首仓为 NULL）
    rule_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 档位号（首仓为 NULL）
    level_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 实际生效的周期；point / price 恒为 NULL（该入场不依赖周期）
    timeframe: Mapped[str | None] = mapped_column(String(8), index=True)
    # 该入场的手数与成交价（成交后回填）
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 已实现盈亏（持仓收口后回填，按该入场对应的持仓归集）
    realized_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

**为什么要有这张表**：
1. 「每一次入场都有原因」这句话需要一个**权威账本**——否则每次统计都要去解析
   JSON 快照，慢且脆；
2. 档位的**周期**只在策略快照里（JSON），SQL 无法 group by；
3. 未来要扩展「平均持仓时长」「最大回撤」时直接用它。

**写入时机**：
- 建主任务时（`group_persist.create_task`）按快照展开**首仓**与**各档加仓**的**计划**行；
- 节点回报成交事件时（`record_strategy_progress`）把 `detail.calc_type` /
  `detail.timeframe` / `detail.level_index` 回填到对应行，并写入手数与成交价；
- 成交入库后按 `magic` 回填 `realized_profit`。

### 2.4 `node_group` 新增列

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `signal_source` | `VARCHAR(16)` | `direct` | `direct`=TV/手动直连触发；`center`=只接受信号中心下发 |

**默认 `direct`** → 存量分组零行为变化，不破坏在跑任务。

---

## 三、胜率口径（必须一次定死）

### 3.1 两个层级

| 口径 | 分母 | 判定 | 用途 |
|---|---|---|---|
| **信号级胜率** | 已收口的信号数 | 该信号**全部持仓合计净盈亏 > 0** → 赢 | 「这类信号赚不赚钱」 |
| **持仓级胜率** | 已收口的持仓数 | 单个 `position_id` 的净盈亏 > 0 → 赢 | 与成交侧 `deal_stats` 口径一致 |

**不做档位级胜率**（本轮拍板）。

### 3.2 计算链路

```
signal_id
  → group_signal_task (signal_id)                  -- 该信号进了哪些分组
  → group_task_dispatch (task_id → magic)          -- 各节点各持一个魔术号
  → mt5_deal (magic)                               -- 逐笔成交
  → 按 position_id 聚合净盈亏                        -- 一个持仓可能分多次出场
  → 净盈亏 = profit + swap + commission
```

**信号级**：该信号所有 `position_id` 的净盈亏求和 > 0 → 赢。
**持仓级**：每个 `position_id` 单独判胜负。

### 3.3 三个必须显式声明的口径陷阱

1. **同一信号可能在多个分组、多个节点上各开一份仓**。
   信号级胜率**按信号号计一次**（不按节点重复计数）；持仓级胜率按 `position_id` 计，
   每个节点各持一份仓就是各自独立的持仓 —— 这是持仓级的自然语义。
2. **未收口的信号（还在跑）不算输**，单列「进行中」，不进分母。
3. **`group_signal_task.realized_profit`（任务收口累计值） vs 逐笔 `deal.net_profit`
   是两套口径，不可加总**（现有 `signal_stats` 模块 docstring 已明确警告）。
   本模块**必须走逐笔 deal 聚合**，因为要按 position 判胜负、且要过滤进行中。

### 3.4 「已收口」的判定

信号已收口 = 该信号下所有 `group_signal_task` 都不在 `TASK_ACTIVE` 里
（`pending` / `dispatching` / `running`）。

- 全部收口且有成交 → 进分母
- 全部收口但**零成交**（被拒收 / 全部跳过）→ **不进分母**（没开仓就没胜负）
- 任一未收口 → 归「进行中」

---

## 四、信号中心路由中枢

### 4.1 双模式并存

```
                   ┌──────────────────────────────────────┐
TV alert ──┐      │        信号中心（inbox 层）           │
手动触发 ──┼─────►│  落库 + 打标(周期/类型/方向) + 筛选   │
限价监听 ──┘      │  状态: pending → routed / dropped    │
                   └───────────────┬──────────────────────┘
                                   │ 按分组 signal_source 决定
                    ┌──────────────┴──────────────┐
                 direct                      center
            （立刻分组开仓）              （只被选中才开仓）
                    └──────────────┬──────────────┘
                                   ▼
                            分组 → 节点 → 成交
```

**路由策略挂在分组上，不是全局开关。** 现有分组默认 `direct`，行为不变。

### 4.2 三种触发方式统一到一个模型

| 方式 | 入口 | `signal_history.source` | `group_signal_task.trigger_source` | `route_mode` |
|---|---|---|---|---|
| TV 直接发过来 | `/webhook` | `tradingview` | `tv_webhook` | `direct` |
| 手动触发 | `/api/console/manual-signal` | `manual` | `manual` | `direct` |
| 信号中心下发 | `/api/signal-center/signals/{id}/route` | （沿用原 source） | `center` | `center` |

**信号中心下发时不新建信号**，而是**复用原信号的参数**再分发一次 —— 所以一条信号
可能对应多个 `group_signal_task`。

### 4.3 新增 API

```
POST /api/signal-center/signals/{signal_id}/route
  body: { group_ids: [...], volume?, sl?, tp?, note? }
  → 用该信号的原始参数构造 strategy 信号，走 GroupDispatcher 下发
  → 写 group_signal_task.trigger_source = "center", route_mode = "center"

POST /api/signal-center/signals/batch-route
  body: { signal_ids: [...] | filters: {...}, group_ids: [...], ... }

POST /api/signal-center/signals/{signal_id}/drop
  → route_state = "dropped"
```

**路由时的参数处理**：
- 默认沿用信号原始 `symbol` / `action` / `sl` / `tp` / `volume`
- 允许覆盖 `volume`（不同分组手数不同）
- 允许覆盖 `sl` / `tp`（AI 加仓按风险金额反推手数，sl 是关键输入）
- 覆盖后写进 `group_signal_task.payload_json`，可追溯

**幂等**：同一 `signal_id` + 同一 `group_id` 已有未收口任务 → **拒绝**（409）。

### 4.4 筛选条件扩展

在现有 `SignalFilters` 上新增：

| 参数 | 语义 | 作用范围 |
|---|---|---|
| `timeframe` | 周期 | 统计 + 路由筛选 |
| `signal_type` | 首仓类型 | 统计 + 路由筛选 |
| `direction` | 多空 BUY/SELL | **路由筛选**（整合中控台） |
| `price_from` / `price_to` | 价格区间 | **路由筛选**（整合中控台） |

**「区间」有两层语义，必须分开呈现**：
1. **粗筛**：复用中控台该品种的 `intervals` + `allow_buy/allow_sell`（`rules.interval_filter`），
   价格取**节点账户快照的最新报价**（`rules.pick_price` 优先 `prices` 表）；
2. **细选**：前端给一个自选的 `price_from`/`price_to` 窗口做二次收窄（可选，不配置不生效）。

---

## 五、TV 指标改造

### 5.1 告警消息体契约

**一个结构类型 = 一个警报**（TV 没有「结构类型」的占位符）。

Pine 侧实际发出的载荷（`buildSignalPayload` 的产出）：

```json
{
  "action": "BUY",
  "symbol": "EURUSD",
  "volume": 0.1,
  "tf": "M15",
  "signal_type": "ob",
  "model": "strategy",
  "template_ids": ["tpl_1"],
  "token": "<AUTH_TOKEN>"
}
```

带止损止盈时（`ATR 倍数 > 0`）会在 `volume` 之后插入 `sl` / `tp`：

```json
{
  "action": "BUY", "symbol": "EURUSD", "volume": 0.1,
  "sl": 1.08432, "tp": 1.09168,
  "tf": "M15", "signal_type": "ob", "model": "strategy",
  "template_ids": ["tpl_1"], "token": "<AUTH_TOKEN>"
}
```

> `sl` / `tp` 是**可选**的：倍数配 0 时不带这两个字段，由策略自行计算止损止盈。
> `model` 固定 `strategy`（走分组链路）；`template_ids` 决定哪些分组能收到。

**服务端字段名兼容**（照抄 `parser.py` 既有别名归一风格）：
- 周期：`tf` / `timeframe` / `interval`
- 类型：`signal_type` / `stype` / `smc_type`

### 5.2 安全性

- 新字段**纯可选**，缺了照样能正常下单
- **不进 `validate_signal`** —— 缺周期/类型绝不 400
- **进幂等指纹** —— 同参数但不同结构类型的信号不能互相吞掉，
  否则同根 K 上的第二个结构点连信号中心都进不去，`signal_type` 胜率会系统性失真
  （详见 §6.1）

### 5.3 Pine 脚本改造点

`tv_pinescript/smc.pine` 目前是 `indicator`（只画图 + `alertcondition`），
`alertcondition` 的消息体是**固定文案**，无法携带动态 JSON。

改造方式：**保持 `indicator`**（不新建 `strategy` 版本 —— 那会让用户改图表类型，
既有告警全部要重配），在 `indicator` 里追加 `alert()` 区块。按 Pine 约束，
`indicator` 脚本可以用 `alert()` 传自定义 JSON —— 这正是现有 `smc-data` 推送的
做法（`smc.pine` 尾部区块）。

**改造清单**：
1. 新增输入组 `Signal Center Webhook`：开关 / token / 默认手数 / 目标模版 /
   止损止盈 ATR 倍数 / ATR 周期 / 是否推开仓信号；
2. 新增两个辅助函数：
   - `resolveTfName(tf)`：空串表示「用图表自己的周期」，否则归一传入的周期
     （FVG 是在 `fairValueGapsTimeframeInput` 上检测的，周期可能与图表不同 ——
     **`tf` 必须跟着信号本身的周期走**，不能一律用图表周期）；
   - `buildSignalPayload(action, signalType, tfName, sl, tp)`：拼装动态 JSON；
3. 新增「SIGNAL CENTER WEBHOOK ALERTS」区块，结构点触发时各自发一条：
   | 触发条件 | action | signal_type | tf |
   |---|---|---|---|
   | `bullishOBRetest` / `internalBullishOBRetest` | BUY | `ob` | 图表周期 |
   | `bearishOBRetest` / `internalBearishOBRetest` | SELL | `ob` | 图表周期 |
   | `bullishFairValueGap` | BUY | `fvg` | FVG 周期 |
   | `bearishFairValueGap` | SELL | `fvg` | FVG 周期 |
   | `bullishPDZRetest` | BUY | `pdz` | 图表周期 |
   | `bearishPDZRetest` | SELL | `pdz` | 图表周期 |
   | 5 个 Fibo 档位的「回踩」 | BUY | `fibo` | 图表周期 |
   | 5 个 Fibo 档位的「反弹」 | SELL | `fibo` | 图表周期 |
4. `sl` / `tp`：`ATR 倍数 = 0` 时**不带这两个字段**（由策略自行计算止损止盈），
   >0 时按 `close ± ATR × 倍数` 算；
5. **保留原 `alertcondition`**（不影响既有 TV 警报配置，用户不迁移也能用）；
6. **保留原 `smc-data` 推送**（每根 K 收盘必发，供中控台挑价位），不动。

### 5.3.1 铁律：图上没展示的结构点，一律不推

**规则**：用户在图上关掉的结构点，其信号**既不推给信号中心，TV 侧也不弹警报**。

| 结构类型 | 展示开关 → 推送开关 |
|---|---|
| OB（大结构） | `showSwingOrderBlocksInput` |
| OB（小结构） | `showInternalOrderBlocksInput` |
| FVG | `showFairValueGapsInput` |
| PDZ | `showPremiumDiscountZonesInput` |
| Fibo 整块 | `showFiboInput` |
| Fibo 各档位 | `showFib236Input` / `showFib382Input` / `showFib500Input` / `showFib618Input` / `showFib786Input` |

**为什么**：否则「图上看到的」和「实际下单的」是两套东西 —— 用户没在看的 OB
却发起了真实交易，统计侧也会莫名其妙多出一批根本没在看的信号。

**实现要点**：
- 检测层（`detectOBRetest` / `drawFairValueGaps` / `detectPDZRetest` / `drawFiboZones`）
  **本来就被这些开关挡住了**（上游隐式副作用）；
- 但推送层（`alert()` 与 `alertcondition()`）**再显式写一遍**同样的门控，
  让「不展示就不推」这条规则**写在推送处** —— 上游以后重构
  （比如把 OB 检测挪到别处），这条规则不会悄悄失效；
- OB 大/小结构**分开门控**：只开了 Swing OB 时，小结构 OB 回踩不应推送
  （早期实现用 `or` 把两者合并，会误推）；
- Fibo 档位开关**逐档生效**：没画出来的那条 Fibo 线，触达了也不推。

> 本规则与 `smc-data`（支撑阻力快照）的既有行为一致 —— 那里本来就有
> `if showSwingOrderBlocksInput and ...` 这类按展示开关裁剪 payload 的写法。

**频率的关键坑**：本区块用 `alert.freq_all`（**不是** `alert.freq_once_per_bar_close`），
整块被 `barstate.isconfirmed` 门控。

> `alert.freq_once_per_bar_close` **每根 K 只允许发一条** —— 同一根 K 上同时触发
> 多个结构点（例如既 OB 回踩又 FVG 出现）时，只有第一条会被送出去，其余静默丢失。
> `freq_all` + `isconfirmed` 等价于「只在收盘那一轮触发」，且互不吞。

**TV 侧只有一个告警选项**：TV 把脚本里所有 `alert()` 归成一项
「Any alert() function call」。所以 `smc-data` 快照与开仓信号**共用一个告警**，
后端靠 `type` 字段分流（`type=smc-data` 走价位匹配；带 `action` + `signal_type`
的走信号分发）。

> **注意**：`smc.pine` 是一个完整指标脚本，本次只做**新增**：
> 追加「开仓信号告警」区块，不改既有绘图 / 告警逻辑。

---

## 六、必须提前说明的风险

### 6.1 幂等指纹与信号类型的关系

现有幂等指纹（`webhook.process_signal`）由
`model:action:symbol:volume:sl:tp:template_ids:group_ids:node_ids` 构成。

**必须把 `signal_type` 与 `timeframe` 加进指纹**（与 §5.2 一致，这里拍板改动）。

理由（这是拍板后**修正**过的结论，早先的「不改」会漏数据）：

- Pine 侧一条结构点告警的 `sl` / `tp` 是**按 ATR 算出来的**，同一根 K 上
  「OB 回踩」与「FVG 出现」如果 ATR 没变，算出来的 `sl` / `tp` **完全相同**，
  `action` / `symbol` / `volume` 也相同 —— **指纹一模一样**；
- 不加区分，第二条信号会被 5 秒幂等**静默吞掉**，它连信号中心都进不去；
- 于是 `signal_type` 维度上，同一根 K 触发的第二个结构类型**永远统计不到**，
  胜率分母被系统性低估 —— 这是「统计失真」，比「重复下单」严重得多。

**不改**的部分：
- 5 秒幂等窗口本身不变；
- 同类型同参数连发**仍然**被吞掉（本就该是重复）。

即：指纹 = `model:action:symbol:volume:sl:tp:signal_type:timeframe:template_ids:group_ids:node_ids`。
`signal_type` / `timeframe` 缺失时为空串 —— 存量行为（不带这两个字段的普通
TV 告警）**逐字节不变**，老链路的幂等语义零影响。

### 6.2 改分发行为是高风险动作

`process_signal` 现在是「解析即分发」。加了 `center` 模式后，
**中心模式下 TV 信号不会自动开仓** —— 管理员误把生产分组切成 `center`，
交易会静默停止。

**必须**：
- 分组切 `center` 时**二次确认 + 审计留痕**；
- 面板上明确显示「该分组当前不从 TV 直连」。

### 6.3 `signal_entry_leg` 的展开时机

加仓档位的周期来自**开仓时的策略快照**（快照冻结设计）—— 这是历史真值。
但要写进注释：**改策略不影响已开任务的统计口径**。

### 6.4 两套盈亏口径不可加总

`group_signal_task.realized_profit` vs 逐笔 `deal.net_profit`。
本模块**必须走逐笔 deal 聚合**，不要图省事用 `realized_profit`。
一旦混用，胜率会和成交侧页面对不上。

---

## 七、分期落地

### 一期：统计底座（不动交易行为）

1. `backend/app/signal_meta.py`（新）—— 周期 / 类型 / 入场原因注册表
2. `backend/app/parser.py` —— `TradingSignal` 加 `timeframe` / `signal_type`
3. `backend/app/orm.py` + `db.py` —— 新列 + `signal_entry_leg` 表 + 迁移函数
4. `backend/app/persist.py` —— 落库周期 / 类型
5. `backend/app/group_persist.py` —— 建主任务时展开 `signal_entry_leg`
6. `backend/app/signal_perf.py`（新）—— 信号级 / 持仓级胜率统计
7. `backend/app/signal_center_api.py` —— 绩效接口
8. 前端 —— 信号中心加「绩效」tab

### 二期：路由中枢（改变分发行为）

9. `orm.py` / `db.py` —— `node_group.signal_source`；`signal_history` 路由列
10. `webhook.py` —— `process_signal` 支持 `center` 分支（只落库不下发）
11. `group_dispatcher.py` —— 按分组 `signal_source` 决定是否消费
12. `signal_center_api.py` —— route / batch-route / drop 接口 + 方向 / 区间筛选
13. 前端 —— 「待路由」队列 + 筛选器 + 批量下发

### 三期：TV 指标改造 + 文档

14. `tv_pinescript/smc.pine` —— 告警带 `signal_type` / `tf`
15. `WEBHOOK.md` / `使用与部署文档.md` / `技术实现方案.md` 更新

---

## 八、验收标准

1. TV 告警带 `signal_type=ob` 与 `tf=M15` → 信号中心信号流里能看到这两个字段
2. 缺 `tf` / `signal_type` 的告警**照常下单**，信号中心显示「未标注」
3. 绩效 tab：按周期、按信号类型的信号级与持仓级胜率，且与成交侧持仓级胜率**对得上**
4. 点数 / 指定价加仓的入场行，`timeframe` 为 NULL，**不出现在任何按周期的分桶里**
5. 分组切 `center` 后，TV 信号**不再自动开仓**，只在信号中心被选中下发时才开仓
6. 同一信号重复路由到同一分组 → 409
