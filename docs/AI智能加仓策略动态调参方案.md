# AI智能加仓策略 · 运行期动态调参技术方案

> 状态：**已实施**（P0-0 ~ P3 全部落地并通过测试）
> ⚠️ **v2 修订（2026-11-02）**：本文的「保存前拦截」与「缩容冻结」两条核心口径**已被推翻**。
> 最新口径见 **[`docs/superpowers/specs/2026-11-02-hot-push-always-apply-design.md`](superpowers/specs/2026-11-02-hot-push-always-apply-design.md)**：
> **配置无条件生效**（保存/下发零拦截），**节点自决是否继续加仓**（已超上限就停、等止盈止损）。
> 本文以下章节保留为历史设计记录；与 v2 冲突处以 v2 为准。
> 实施记录见 [§13 实施落地记录](#13-实施落地记录)
> 适用模版：`tpl_1`「AI智能加仓策略」（规则 type=1 逆势加仓 / type=2 顺势加仓）
> 关联代码：前端 `frontend/src/components/StrategyFormModal.vue`、`backend/app/strategies.py`、`backend/app/group_rules.py`、`backend/app/group_dispatcher.py`、`backend/app/group_persist.py`、`node_client/node_client.py`、`node_client/strategy_runner.py`、`node_client/strategy_rules.py`、`node_client/manual_scatter.py`、`node_client/signal_pl.py`

---

## 0. 已锁定的四条产品语义

> ⚠️ 下表是 **v1 口径**。v2 修订后：
> - 决策 1 → 「新值 ≥ 已开笔数才允许」**取消**；上限一律采纳，低于已开笔数时节点不再新增批次（回执 `no_effect` + `exec=no_more_adds`）。
> - 决策 2 → 「保存时就拦截」**取消**；改为节点侧保留旧值 + 告警事件。

本方案基于以下四条已确认的产品决策，后续所有设计围绕它们展开。
| # | 决策 | 影响的设计 |
|---|---|---|
| 1 | 上限类字段（`total_lot_limit` / `max_allow_num`）**是普通可改字段**：调大=扩容、调小=缩减剩余额度，**只要新值 ≥ 该规则已开笔数就允许**；低于已开笔数则**前端直接拒绝保存**（后端 400 兜底） | 无需 `frozen_limits`；节点侧 `rule_adds >= limit` 判定天然支持缩容；新增「已开笔数」数据链路供前端校验 |
| 2 | 改盈亏类参数若新算出的 SL/TP 已在现价错误侧 → **保持原 SL/TP**，且**保存时就拦截** | 需要「保存前预校验」，不能等到热推才处理 |
| 3 | 手动分散仓**本任务只开一次**；锁定粒度按生命周期：**挂单在盘上（`pending`）可改**（节点撤旧单重算重挂），**已成交（`filled`）才锁死** | 手动仓相关字段按 `manual_state` 决定进「锁定区」还是「可改区」 |
| 4 | 热推回执**逐字段** | 新增 `strategy_update_ack`，字段级结论落任务事件 |
| 5 | **手动分散仓是本任务的最后一次加仓**：某规则的手动仓一开（挂单已出），该规则**永久停止**新增批次（逆势 / 顺势都不再加） | 节点侧新增「加仓收口」闸门；`_manual_fired` 语义扩展为「该规则加仓已收口」；跨规则隔离（逆势锁逆势，顺势照常） |

---

## 1. 现状链路与问题诊断

### 1.1 现有热推链路（模版 1 加仓策略专有）

```
前端 PATCH /api/strategies/{id}
  └─ backend/app/strategies.py :: update_strategy
       └─ background_tasks → _hot_push_running_snapshot
            ├─ group_rules.strategy_rules_snapshot(strategy)      生成快照
            ├─ group_persist.rewrite_running_strategy_snapshots   改库内快照（保证断线重连 resume 也用新快照）
            └─ group_dispatcher.push_strategy_updates             WS 下发 strategy_update
                 └─ node_client.py :: _do_strategy_update
                      └─ StrategyRunner.apply_strategy(snapshot)
```

节点侧 `apply_strategy` 的当前行为是**整体替换快照**：

```python
# node_client/strategy_runner.py
def apply_strategy(self, snapshot: dict) -> bool:
    """热推规则快照：只接受仍走加仓路径的更新，拒绝运行中切网格 / 以损定量。"""
    if self._stopping or self._finished:
        return False
    # ... 模式一致性校验 ...
    self.strategy = snap
    self._signal_pl = signal_pl.prepare_runtime(
        signal_pl.extract_config(self.strategy.get("rules")),
    )
    self._lot_sltp_dirty = True
    self._metric_specs = None
    self._live_specs = None
    self._counter_resync = True
```

### 1.2 已加笔数与配置值解耦

上限判定用的是**实际已加笔数**，而不是「配置值 − 已加」，这是「扩容到 10 仓」能生效的根本原因：

```python
# node_client/strategy_rules.py :: _prepare_rule
rule_adds = ctx.rule_add_count(rule_type)
level, level_index = pick_batch_level(rule, rule_adds + 1)
if level is not None:
    if not action_matches(rule.get("batch_action") or rule.get("action"), ctx.direction):
        return None
    limit = _as_int(rule.get("total_lot_limit"), 0)
    if limit and rule_adds >= limit:
        return None
```

```python
# node_client/strategy_runner.py :: _ctx
counted = count_adds_by_rule(positions)
merged = {
    RULE_TYPE_COUNTER: max(self.add_counts.get(RULE_TYPE_COUNTER, 0), counted.get(RULE_TYPE_COUNTER, 0)),
    RULE_TYPE_TREND: max(self.add_counts.get(RULE_TYPE_TREND, 0), counted.get(RULE_TYPE_TREND, 0)),
}
```

### 1.3 三个核心问题

1. **能改但结果不是用户想要的**
   - 档位重切让第 N 笔加仓用到用户没填过的档位参数。
   - 缩容没有下限约束：把上限改成低于已开笔数会让节点进入「上限 < 已加」的非法状态，并使手动分散仓门槛被误解锁。

2. **改不动但没告诉任何人**
   - `apply_strategy` 拒绝时只打 `logger.warning`，后台与前端完全无感，用户以为改了其实没生效。

3. **改的动作本身产生强副作用**
   - 改 `lot_pl_tiers` → 下一拍重算共同止盈 → 现价已越过 → `signal_pl` 走 `_lot_sltp_market_close` 市价强平该方向，甚至收口任务。

**方案核心 = 把「热推即生效」改成「先校验、再判定、后应用、必回执」。**

---

## 2. 总体架构

```
┌─ 前端 StrategyFormModal ──────────────────────────────────┐
│  保存前本地预检：上限下限校验 / 锁定区字段 / 档位 extend 语义 │
│  （依据 runtime_hints.max_added_by_rule）                    │
└───────────────┬───────────────────────────────────────────┘
                │ PATCH /api/strategies/{id}
┌───────────────▼───────────────────────────────────────────┐
│ backend/app/strategies.py                                 │
│  ① 拉取在跑子任务 → 构造「候选快照」                        │
│  ② 库内校验：新上限 < 已开笔数 → 400（兜底）               │
│  ③ ★预校验：向在线节点发 strategy_precheck，收集阻断项       │
│     └─ 有阻断 → 400，返回逐字段原因（不落库、不下发）        │
│  ④ 无阻断 → 落库 + 生成 config_version                     │
│           + rewrite_running_strategy_snapshots             │
│  ⑤ 下发 strategy_update { config_version, snapshot }        │
└───────────────┬───────────────────────────────────────────┘
                │ WS
┌───────────────▼───────────────────────────────────────────┐
│ node_client.py → StrategyRunner.apply_strategy()          │
│  ① 版本门：config_version < applied_version → 丢弃          │
│  ② reconcile：逐字段判定 hot / limit / locked              │
│  ③ 应用 → 唤醒订阅 → 重建逆势限价                           │
│  ④ 回执 strategy_update_ack {applied, rejected, partial}    │
└───────────────┬───────────────────────────────────────────┘
                │
┌───────────────▼───────────────────────────────────────────┐
│ 后端落任务事件 → 前端 GroupSignalsView 实时展示              │
└───────────────────────────────────────────────────────────┘
```

---

## 3. 字段可变性矩阵

`apply_strategy` 不再整体替换，改为按字段 reconcile。

### 3.1 三档可变性

| 类别 | 字段 | 运行中行为 |
|---|---|---|
| **HOT**（直接生效） | `point`、`lot_times`、`extra_lot`、`action`、`batch_action`、`status`、`batch_levels`（间距 / 倍数 / 手 / calc_type / timeframe / range_*）、`manual_scatter.close_all_on_tp`、`float_pl_ratio`、`lot_pl_tiers`（受第 4 节拦截约束） | 下一拍判定即用新值 |
| **LIMIT**（上限类，可调大也可调小，但有下限） | `total_lot_limit`、`max_allow_num` | 调大 → 扩容，继续加仓；调小 → 缩减剩余额度，**只要新值 ≥ 已开笔数就允许**；低于已开笔数 → **前端拒绝保存**（见 4.6）。判定侧沿用现成的 `rule_adds >= limit` 逻辑，**无需新增冻结态**。⚠️ 该规则手动仓已开时 → **不再生效**（加仓已收口，见 6.3），回执标 `no_effect` |
| **LOCKED**（运行期拒绝） | 规则 `type`、规则所属模版、`manual_scatter` 的 `entry_price` / `take_profit` / `volume` / `volume_locked` / `stop_loss`、网格 / 以损定量全部字段 | 回执标 `locked`，前端提示需新建任务 |

> `LOCKED` 中的 `manual_scatter` 由决策 3 驱动。**锁定粒度按手动单生命周期区分**（详见 `技术实现方案.md` §18.54）：
> - `pending`（挂单在盘上）：**可改** `entry_price` / `take_profit` / `stop_loss` / `volume` —— 节点撤旧单、按新价重算手数再重挂；
> - `filled`（**已成交**）：该规则的手动分散仓整块锁死（不只是参数锁，`enabled` 也不能再改、不能重开）。
>
> 判定依据是 `runtime_hints.manual_state[rule_type]`，不再是「开过就锁」。

### 3.2 上限类字段的判定口径

节点侧上限判定本来就是「**实际已加笔数** ≥ 上限就不再加」，与配置值无关：

```python
# node_client/strategy_rules.py :: _prepare_rule
rule_adds = ctx.rule_add_count(rule_type)
limit = _as_int(rule.get("total_lot_limit"), 0)
if limit and rule_adds >= limit:
    return None
```

因此**缩容天然生效**且语义精确：

| 场景 | 上限 5 → 4，已开 3 笔 | 上限 5 → 2，已开 3 笔 | 上限 5 → 2，已开 5 笔 |
|---|---|---|---|
| 是否允许保存 | ✅ | ✅（4 ≥ 3） | ❌ **前端拒绝**（2 < 5） |
| 运行时效果 | 还能再开 1 笔（第 4 笔） | 还能再开 1 笔（第 4 笔） | — |

**关键收益**：不再需要 `frozen_limits` 机制，也**不再存在「缩容把手动分散仓门槛解锁」的反效果**——因为该反效果只发生在 `新上限 < 已开笔数` 时，而那个值现在直接被前端拒掉了（见 4.6）。

### 3.3 三类「拒绝」的区别

| 类型 | 触发条件 | 后端行为 | 节点行为 |
|---|---|---|---|
| `rejected` | LOCKED 字段被改 / 版本倒退 / 模式切换 / **新上限 < 已开笔数** | **保存前拦截**（HTTP 400） | 不应发生 |
| `no_effect` | 手动仓在场时改 `lot_pl_tiers`（被止盈权威覆盖） | 允许保存 | 回执标 `no_effect`，提示不生效原因 |
| `locked_runtime` | 手动仓已开过，改手动仓配置 | 允许保存（不影响存量仓），但回执标 `locked_runtime` + 前端黄色提示 | 存量手动仓 TP 仍为绝对权威，不动 |

---

## 4. 保存前预校验（决策 2 的落地）

### 4.1 为什么必须「保存前」拦截

共同 SL/TP 的计算依赖节点侧的**持仓、余额、合约规格、现价**：

```python
# node_client/signal_pl.py
def plan_common_stops(
    scoped, *, tp_amount=None, sl_amount=None, tp_min_lot=0.0,
    tick_size=0.0, tick_value=0.0, digits=0, bid=0.0, ask=0.0, fallback_price=0.0,
)
```

后端拿不到这些。因此**校验必须由节点 dry-run 执行**，后端只负责收集与聚合结论。

### 4.2 新增 `strategy_precheck` 协议

后端在 PATCH 落库**之前**，向每个有在跑任务的在线节点发预校验请求，等待回执（建议 3s 超时）。

请求：

```jsonc
// 后端 → 节点
{
  "cmd": "strategy_precheck",
  "task_id": 1234,
  "config_version": 42,
  "strategy": { /* 候选新快照 */ },
  "changed": ["rules[0].total_lot_limit", "rules[0].lot_pl_tiers"]
}
```

回执：

```jsonc
// 节点 → 后端
{
  "cmd": "strategy_precheck_ack",
  "task_id": 1234,
  "ok": false,
  "blocks": [
    {
      "field": "rules[0].lot_pl_tiers",
      "code": "sltp_crossed",
      "message": "按新的分档手数盈亏（批次1 目标 500），多单共同止盈算在 3120.45，现价 3135.20 已越过；保存后存量仓位会被平掉。请先调整目标金额或现价回归后重试。"
    }
  ],
  "runtime": {
    "add_counts": { "1": 3, "2": 1 },
    "position_count": 5,
    "manual_fired": [2],
    "counter_pending": true
  }
}
```

### 4.3 节点侧 precheck 判定清单

| 检查 | 规则 | 结果 |
|---|---|---|
| 版本门 | `config_version < applied_version` | `rejected: stale_version` |
| 模式一致 | 新快照仍是加仓路径 | `rejected: mode_switch` |
| LOCKED 字段 | `manual_scatter` 参数被改 且 该规则手动仓已开过 | `rejected: manual_locked` |
| LOCKED 字段 | 规则 `type` 变化 / 模版变化 | `rejected: type_locked` |
| **上限下限** | **新上限 < 该规则已开笔数** | **`rejected: limit_below_added`**（见 4.6） |
| **加仓已收口** | 该规则手动仓已开（`_manual_fired` 含该规则）且改动只影响「新增批次」的字段（上限 / 档位 / 间距 / 手数） | 不阻断，记 `no_effect`（见 6.3） |
| **SL/TP 越过** | 用新参数 dry-run 算共同 SL/TP，现价已在错误侧 | **`rejected: sltp_crossed`**（决策 2） |
| 档位自洽 | 档位数 == `batch_count`；`pos_from/pos_to` 连续；`total_lot_limit` ≥ 末档 `pos_to` | `rejected: batch_inconsistent` |

### 4.4 后端聚合规则

- 任一节点返回非空 `blocks` → **整次 PATCH 拒绝**（HTTP 400），返回所有节点的阻断原因，前端弹窗逐条展示。**不落库、不下发**。
- 节点离线 → 该节点不参与预校验，但响应里标注「有 N 个离线节点未校验，重连后生效，可能与新配置不一致」。
- 全部节点离线 → 允许保存（无法校验），但回执标 `unchecked`。

**上限下限校验（`limit_below_added`）走单独一条更早的路径**——它不依赖节点在线，用库内 `add_counts` 即可判定，因此：

- 后端在 PATCH 入口处**先**做这一条校验（库内数据，比等节点回执快且覆盖离线节点）；
- 命中即 400，不进入 precheck 流程；
- 节点侧 precheck 里再算一次（用**实时**已开笔数），防止库内数据滞后导致漏判。

### 4.5 dry-run 的实现要求

节点侧 precheck **不落任何交易动作**：

- 只读持仓、现价、余额、合约规格；
- 用 `signal_pl.plan_common_stops` + 新参数算出 `sl` / `tp`；
- 与 `bid` / `ask` 比较判断是否越过；
- 不调用 `modify_position_sl` / `close_positions`。

`float_pl_ratio` 的 `sl_amount` 依赖**账户余额**，`lot_pl_tiers` 的 `tp_amount` 依赖 `tiers[0].pl_amount`，两者都在节点侧计算。dry-run 与真实执行必须走**同一函数**，避免口径不一致。

### 4.6 上限下限校验：前端直接拒（决策 1 的落地）

**目标**：客户把上限改成低于已开笔数时（如已开 3 仓却改成 2 仓），**在前端就被拦住**，不让请求发到后端。

#### 4.6.1 校验依据：各规则已开笔数

判定是**按规则独立**的（逆势 `type=1` 与顺势 `type=2` 各算各的），所以校验也必须按规则分别做：

```python
# node_client/strategy_runner.py :: _ctx
counted = count_adds_by_rule(positions)
merged = {
    RULE_TYPE_COUNTER: max(self.add_counts.get(RULE_TYPE_COUNTER, 0), counted.get(RULE_TYPE_COUNTER, 0)),
    RULE_TYPE_TREND: max(self.add_counts.get(RULE_TYPE_TREND, 0), counted.get(RULE_TYPE_TREND, 0)),
}
```

节点已有 `add_counts`（按规则类型分开的已加笔数），且**上报报文里已经带了**：

```python
# node_client/strategy_runner.py :: _snapshot
return {
    "position_count": len(pos),
    "pending_orders": self._pending_orders,
    "add_count": self.add_count,
    "add_counts": dict(self.add_counts),   # ← 已有，按规则类型分开
    ...
}
```

但**后端落库时只取了合计的 `add_count`，丢掉了分规则的 `add_counts`**：

```python
# backend/app/models.py :: GroupTaskDispatch
    add_count: int = 0
```

```python
# backend/app/group_persist.py :: 上报落库
if _num(data.get("add_count")) is not None:
    row.add_count = int(data["add_count"])
# ↑ 没有对应的 add_counts 落库逻辑
```

→ **需要新增 `add_counts` 的落库与查询链路**（见第 5 节）。落库位置建议复用已有的 `runtime_json` 字段（非心跳事件才写），避免新增列：

```python
# backend/app/group_persist.py
runtime = data.get("runtime")
if event_type and event_type != "heartbeat" and isinstance(runtime, dict):
    row.runtime_json = runtime
```

但 `add_counts` 需要**心跳也刷新**（加仓是随时可能发生的），所以更稳妥的做法是新增一列 `add_counts_json`，落库逻辑与 `add_count` 并列。

#### 4.6.2 前端校验规则

策略编辑弹窗加载时，后端通过 `GET /api/strategies/{id}` 返回 `runtime_hints`：

```jsonc
{
  "strategy_id": "str_xxx",
  "runtime_hints": {
    "running_task_count": 2,
    "max_added_by_rule": {
      "1": 3,     // 逆势：各在跑任务里已开笔数的最大值
      "2": 0      // 顺势：已开 0 笔
    },
    "manual_fired": [2],   // 已开过手动分散仓的规则类型
    "manual_state": {      // 手动分散仓生命周期（键是规则类型字符串）
      "2": "filled"        // none / pending / filled / void
    },
    "nodes_offline": 1
  }
}
```

前端在用户修改「加仓笔数」输入框时**实时校验**：

| 输入 | 判定 | 交互 |
|---|---|---|
| 新值 ≥ 已开笔数 | ✅ 允许 | 正常保存；提示「保存后本任务还能再开 N 笔」 |
| 新值 < 已开笔数 | ❌ **拒绝** | 输入框标红 + 禁用保存按钮 + 明确文案 |

拒绝文案（示例）：

> 无法保存：逆势加仓已开 **3** 笔，新上限 **2** 笔低于已开笔数。
> 上限只能调大或缩减剩余额度，不能低于已开笔数。请填写 **≥ 3** 的值。

**多条规则各自校验**：逆势已开 3、顺势已开 0 时，改逆势上限到 2 被拒，改顺势上限到 0（不限）可以通过。

#### 4.6.3 多任务场景取「最大值」

同一策略可能同时有多个在跑任务（不同节点 / 不同信号），各自已开笔数可能不同（如节点 A 已开 3 笔、节点 B 已开 1 笔）。校验必须取**所有在跑任务的最大值**，否则会出现「按节点 A 拒了，但节点 B 其实还够」的误拒。

保守取最大值符合「宁可拦住也不能产生非法状态」的原则：任何节点的实际已开笔数超过新上限都会导致该节点后续状态不一致。

#### 4.6.4 后端兜底（前端校验不可信）

前端校验只是体验优化，**后端必须独立再校验一次**：

- PATCH 时若该策略有在跑任务，从库里取各规则的 `max_added_by_rule`；
- 若新上限 < 该值 → 返回 HTTP 400，`code = "limit_below_added"`，附上具体规则与数字；
- 离线节点的任务仍用库内最后上报的 `add_counts`（可能滞后），所以后端校验用**库内值**，节点侧 precheck 用**实时值**——两者取严。

#### 4.6.5 缩容后的运行时行为（合法缩容，无需特殊机制）

新上限 ≥ 已开笔数时，缩容**直接生效**，节点侧不需要任何新增逻辑：

- 判定侧现成的 `if limit and rule_adds >= limit: return None` 会自然停止加仓；
- **已开仓位保持不动**（不冻结、不平仓）；
- 逆势在途限价单会被撤掉：`plan_counter_limit` 返回 None → `_sync_counter_limit` 走「逆势加仓没有下一档」→ 撤单；
- **手动分散仓门槛自动跟随**：门槛是「已加笔数 ≥ 上限」，缩容后若已加笔数仍 < 新上限 → 门槛保持「未加完」，手动仓**不会**被误解锁；
- 若缩容后已加笔数恰好 == 新上限 → 门槛判「已加完」，手动仓可正常开启（这是正确语义：分批确实加满了）。

#### 4.6.6 `limit = 0` 的歧义

节点侧 `0` 表示**不限**（`if limit and rule_adds >= limit` —— 0 为假值，直接跳过限制）：

```python
# node_client/strategy_rules.py :: _prepare_rule
limit = _as_int(rule.get("total_lot_limit"), 0)
if limit and rule_adds >= limit:
    return None
```

所以「上限改成 0」在运行时是**解除限制**，不是「清零」。但从用户视角，0 显然不是合法的「加仓笔数」。

**处理**：
- 前端下限校验把 `0` 排除在「合法缩减值」之外——它不是缩减，是「不限」，走独立的确认提示；
- 输入 `0` 时提示「上限填 0 表示**不限制加仓笔数**，与『不加了』不同，请确认」；
- 后端校验 `limit = 0` 时按「不限」处理，不参与下限比较。

---

## 5. 数据模型与接口变更清单

| 层 | 变更 |
|---|---|
| 策略存储 | `rules[]` 增 `batch_edit_mode`（`append` / `redistribute`）；`strategy` 增 `config_version` |
| 快照 | `group_rules.strategy_rules_snapshot` 增 `config_version` |
| WS 命令 | 新增 `strategy_precheck` / `strategy_precheck_ack` / `strategy_update_ack` |
| 节点运行时 | 新增 `self._cfg`（生效快照）、`self._applied_version` |
| **节点上报** | ✅ **无需改动**：`_snapshot()` 已带 `add_counts`，`_emit_progress` 通过 `**self._snapshot(positions)` 自动带上（JSON 序列化后键为字符串 `"1"` / `"2"`） |
| **库表** | `group_task_dispatch` 增 `add_counts_json`（或复用 `runtime_json` 存 `add_counts`），落库各规则已开笔数 —— 这是**必需的新增落库** |
| **事件** | 新增 `detail.kind = "strategy_update"`，含 `fields[]` |
| **策略 API** | `PATCH` 增加 400 阻断响应结构 `{error, blocks:[{field, code, message, node_id, task_id}]}`；新增 `limit_below_added` 阻断码 |
| **策略 API** | `GET /api/strategies/{id}` 增 `runtime_hints`：`{running_task_count, max_added_by_rule, manual_fired, nodes_offline}` |

---

## 6. 三个专项设计

### 6.1 扩容：档位必须「尾追加」而非「重切」

**问题**：前端改批数 / 加仓笔数会重切区间 + 按 index 复用旧参数：

```typescript
// frontend/src/components/StrategyFormModal.vue
function rebuildBatchLevels(r: EditableRule): void {
  const limit = Math.max(BATCH_POS_START, Math.floor(Number(r.total_lot_limit) || BATCH_POS_START))
  r.total_lot_limit = limit
  const maxCount = limit - BATCH_POS_START + 1
  let count = Math.max(1, Math.floor(Number(r.batch_count) || 1))
  if (count > maxCount) count = maxCount
  r.batch_count = count
  const old = r.batch_levels
  r.batch_levels = splitBatchRanges(limit, count).map((range, i) => ({
    ...range, ...defaultLevelParams(i, old[i]),
  }))
}
```

举例：原 3 批覆盖 1–3，每批 1 笔；改成 10 笔 / 3 批 → 区间变成 1–4 / 5–7 / 8–10，**第 4 次加仓命中档位 #1（1–4）**，沿用 `old[0]` 的点 / 倍 / 手，而不是原设计里「第 4 笔该用的新档」。也就是说「扩到 10 仓」实际生效的是一套**用户没填过的档位**。

**方案**：

1. 拆成两个动作：
   - `改批数（均分）` — 用户想重新均分时用，但**必须先确认没有在跑任务**（有在跑任务则拒绝，提示「该策略有在跑任务，改批数会重切已有档位；请用『追加档位』」）。
   - `追加档位`（默认动作）— 原档位区间**完全不动**，只按新增笔数在尾部追加新档，新档参数由用户填写。
2. 前端新增 `batch_edit_mode: 'append' | 'redistribute'`，`redistribute` 在有在跑任务时禁用。
3. 节点侧依靠**绝对加仓序号锚定**保证「第 N 次加仓」永远落在用户指定的那一档：

```python
# node_client/strategy_rules.py
def pick_batch_level(rule: dict, next_position_no: int) -> tuple[Optional[dict], Optional[int]]:
    """按「本规则下一次加仓序号」命中分批档位；未启用或未命中返回 (None, None)。"""
    if not rule.get("batch_enabled"):
        return None, None
    for idx, level in enumerate(rule.get("batch_levels") or []):
        if not isinstance(level, dict):
            continue
        lo = _as_int(level.get("pos_from"), 0)
        hi = _as_int(level.get("pos_to"), 0)
        if lo <= next_position_no <= hi:
            return level, idx
    return None, None
```

只要 `pos_from` / `pos_to` 不被重切，锚定就是稳的。

4. 追加档位时，若新增档的 `pos_from` ≤ 已加笔数（说明用户想覆盖历史档），后端拒绝，提示「追加档位的起始加仓次数必须大于当前已加笔数」。

### 6.2 缩容：只允许缩到「已开笔数以上」

**语义**（决策 1 修订后）：

上限类字段是**普通可改字段**，缩容本身完全合法，唯一的硬约束是：

> **新上限 ≥ 该规则已开笔数**

| 场景 | 结果 |
|---|---|
| 已开 3 笔，上限 5 → 4 | ✅ 允许，还能再开 1 笔 |
| 已开 3 笔，上限 5 → 3 | ✅ 允许，不能再开（额度刚好用完），已开仓位不动 |
| 已开 3 笔，上限 5 → 2 | ❌ **前端直接拒绝保存**（2 < 3） |

**运行期行为**（合法缩容）：节点侧**不需要任何新增机制**，现成判定天然支持：

```python
# node_client/strategy_rules.py :: _prepare_rule
limit = _as_int(rule.get("total_lot_limit"), 0)
if limit and rule_adds >= limit:
    return None
```

- 停止新增加仓，**已开仓位保持不动**（不冻结、不平仓）；
- 逆势在途限价单自动撤掉：`plan_counter_limit` 返回 None → `_sync_counter_limit` 走「逆势加仓没有下一档」→ 撤单；
- 回执字段状态：`applied`，消息「上限已从 5 调整为 4，本任务还能再开 1 笔；已开仓位保持不动」。

**「手动仓门槛被解锁」的反效果已自动消除**：

原反效果来自「缩容后 `已加笔数 ≥ 新上限` → 门槛判『已加完』→ 多开一手手动仓」：

```python
# node_client/manual_scatter.py
if int(added_count or 0) < limit:
    return f"本规则分批加仓尚未加完（已加 {int(added_count or 0)} / {limit} 笔），手动分散仓暂不开仓"
return None
```

这个反效果**只发生在 `新上限 ≤ 已开笔数` 时**——而那个值现在被前端直接拒掉了（见 4.6），根本存不下来。

因此：
- **不需要** `frozen_limits` 机制；
- **不需要**给 `manual_blocked_reason` 加 `frozen` 入参；
- 合法缩容时手动仓门槛按新上限正常判定：已开 3 笔、上限缩到 4 → `3 < 4` → 门槛仍是「未加完」，手动仓**不会**被误解锁；已开 3 笔、上限缩到 3 → `3 >= 3` → 门槛判「已加完」，手动仓可开，这是**正确语义**（分批确实加满了）。

### 6.3 手动分散仓：一次性锁死（决策 3）

### 6.3 手动分散仓：一次性锁死 + 终止后续加仓（决策 3 + 决策 5）

#### 6.3.1 产品语义

**手动分散仓是本任务的最后一次加仓。** 一旦某条规则的手动分散仓已开（挂单已出），该规则的加仓批次就**永久结束**——不再有任何新的分批加仓、逆势限价或顺势加仓。

这是业务上的强约束：手动分散仓是收尾仓，它的存在意味着这个信号的风险敞口已经按设计铺完；之后再加仓会破坏手数结构（手动仓的手数是按「存量仓位到止盈时刻的预估亏损」反推算出来的，存量再加仓会把这个反推的基准改掉，导致手动仓止盈那一刻的净额对不上目标金额）。

#### 6.3.2 现状核查：节点侧**没有**这个闸门

当前 `_manual_fired` **只用于防止手动仓重复开仓**，不拦截加仓判定：

```python
# node_client/strategy_runner.py :: _maybe_open_manual_scatter
for watch in watches:
    rule_type = int(watch.get("rule_type") or 0)
    if rule_type in self._manual_fired:
        continue        # ← 只跳过「再开一次手动仓」，不影响加仓判定
```

而加仓判定走的是另一条路（`_on_event` → `evaluate` / `plan_counter_limit`），**完全不看 `_manual_fired`**：

```python
# node_client/strategy_runner.py :: _on_event
decision = evaluate(
    self.strategy.get("rules") or [], ctx,
    rule_types=(RULE_TYPE_TREND,),
)
...
await self._sync_counter_limit(event, positions, metrics, lives)
```

**后果**：手动分散仓开完后，逆势批次**仍会继续加**（逆势挂下一档限价、顺势照常判定），与「手动仓是最后一次加仓」的产品语义冲突。

#### 6.3.3 闸门设计：`_manual_fired` 作为「该规则加仓已收口」标记

**关键前提**：`_manual_fired` 是在**挂出手动限价单的那一刻**就写入的，不等成交：

```python
# node_client/strategy_runner.py :: _place_manual_pending
self._manual_pending[rule_type] = { ... }
self._manual_fired.add(rule_type)      # ← 挂单时就标记
```

这正好符合语义——手动仓一旦**挂出**，就代表「这是最后一次加仓」的决定已经做出，后续批次不应再启动。

只有一种情况会撤销该标记：挂单被撤 / 被券商拒绝，确认不在盘上：

```python
# node_client/strategy_runner.py :: _observe_manual_pending
if resting is not None and rule_type not in resting_by_rule:
    logger.info("task %s manual scatter M%s pending order gone without fill", ...)
    self._manual_pending.pop(rule_type, None)
    self._manual_fired.discard(rule_type)   # ← 撤销，恢复加仓资格
```

**闸门实现**（新增，节点侧）：

1. `_manual_fired` 的语义从「手动仓已开过（防重开）」扩展为「**该规则加仓已收口**（防再加仓）」——两者时机相同（都是挂单时），一个标记两用。

2. **顺势加仓**：在 `_on_event` 的市价判定前过滤掉已收口的规则。最干净的做法是在 `evaluate` 的调用处传入允许的规则类型：

```python
# node_client/strategy_runner.py :: _on_event（改造后）
allowed_trend = RULE_TYPE_TREND if RULE_TYPE_TREND not in self._manual_fired else None
decision = None
if allowed_trend is not None:
    decision = evaluate(
        self.strategy.get("rules") or [], ctx,
        rule_types=(RULE_TYPE_TREND,),
    )
```

3. **逆势加仓**：在 `_sync_counter_limit` 里，若逆势规则已收口 → **撤掉在途的逆势限价单**并返回：

```python
# node_client/strategy_runner.py :: _sync_counter_limit（改造后）
if RULE_TYPE_COUNTER in self._manual_fired:
    await self._cancel_counter_orders(resting, book, reason="手动分散仓已开，本任务加仓已收口")
    return
```

4. **跨规则隔离**：手动仓是**按规则**收口的——逆势的手动仓开了，只锁逆势，顺势照常。所以判断必须用 `rule_type in self._manual_fired` 精确匹配，不能用「只要有手动仓就全锁」。

> 注意与「手动分散仓门槛」的区分：门槛是「本规则分批加满才开手动仓」（`manual_blocked_reason`），方向是**先加满再开手动仓**；本闸门是「开了手动仓就再加不加」，方向是**开完就收口**。两者时序相反、互不冲突。

#### 6.3.4 重连一致性

`_manual_fired` 已经随 `runtime` 落库并参与重连恢复：

```python
# node_client/strategy_runner.py :: __init__
raw_fired = self._resume_runtime.get("manual_fired")
if isinstance(raw_fired, list):
    for item in raw_fired:
        self._manual_fired.add(int(item))
```

且持仓快照里带 `M1` / `M2` 备注的仓位会补记：

```python
# node_client/strategy_runner.py :: _seed_from_positions
self._manual_fired |= manual_scatter.fired_rule_types(positions)
```

所以重连后闸门自动恢复，**不会因为断线而重新加仓**。

#### 6.3.5 前端与文档同步

- 前端策略编辑弹窗：该规则手动仓已开过 → 除了 `manual_scatter` 区块只读，还要提示「本任务加仓已收口，不会再新增批次」。
- 回执：手动仓开完后，热推改上限类字段 → 回执标 `no_effect`，消息「该规则手动分散仓已开仓，本任务加仓已收口，调参不会新增批次」。
- `使用与部署文档.md` / `技术实现方案.md` 的模版1 说明需补一句：**手动分散仓是本任务最后一次加仓，开仓后不再新增任何批次**。

---

## 7. 节点侧 reconcile 与回执

### 7.1 `apply_strategy` 改造后流程

```
1. 版本门：config_version < applied_version → 丢弃 + 回执 stale
2. 模式门：加仓路径一致性 → 不一致则 rejected
3. 逐字段 diff old_snapshot vs new_snapshot，按可变性矩阵分类：
   HOT           → 采纳
   LIMIT         → 采纳（调大=扩容；调小=缩减剩余额度，合法性已在预校验阶段把关）
   LOCKED        → 标 locked（正常路径下不该出现，precheck 已拦）
4. 合成生效快照 self._cfg
5. 重置派生状态：_lot_sltp_dirty / _metric_specs / _live_specs / _counter_resync
   ★ 不动：add_counts / _manual_fired / _manual_open / _manual_tp_authority / _smc_used
6. 唤醒订阅
7. 回执 ack
```

> 上限类字段**不需要**额外的冻结态：判定侧现成的 `rule_adds >= limit` 已经天然支持缩容（见 3.2 / 6.2）。

### 7.2 配置版本化与「每拍不可变快照」

解决「apply 与判定交错」：现在 `self.strategy` 被热推直接替换，可能正在 `await place_market_order` 的那一拍前后读到不同值。

- `self._cfg` 存**生效快照**，`apply_strategy` 只在 `_cfg` 上做整体替换（原子赋值）。
- 每一拍开始时 `cfg = self._cfg`（不可变 dict 引用），整拍判定都用同一份。
- `_ctx()` / `evaluate()` / `plan_counter_limit()` 改从 `cfg` 取规则，不再读 `self.strategy`。

### 7.3 回执协议

```jsonc
{
  "cmd": "strategy_update_ack",
  "task_id": 1234,
  "config_version": 42,
  "result": "partial",              // applied | partial | rejected
  "fields": [
    { "field": "rules[0].total_lot_limit", "status": "applied",
      "old": 5, "new": 4,
      "message": "上限已从 5 调整为 4，本任务还能再开 1 笔；已开仓位保持不动" },
    { "field": "rules[0].batch_levels", "status": "applied",
      "message": "追加档位 #4（第 4–5 次加仓），间距 200 点 × 1.5" },
    { "field": "rules[1].manual_scatter.volume", "status": "locked",
      "message": "顺势手动分散仓已开仓，配置锁定，不影响已开仓位" },
    { "field": "rules[0].lot_pl_tiers", "status": "applied",
      "message": "共同止盈已按新目标金额重算，存量仓位改单在本拍生效" },
    { "field": "rules[0].total_lot_limit", "status": "no_effect",
      "old": 5, "new": 10,
      "message": "逆势手动分散仓已开仓，本任务加仓已收口，调参不会新增批次" }
  ]
}
```

### 7.4 后端落事件

- 回执写 `group_task_event`，`detail.kind = "strategy_update"`，`detail.fields` 为逐字段结论。
- 前端 `GroupSignalsView` 的任务事件流新增一类 `strategy_update`，渲染为：✅ 生效 / ⚠️ 部分生效（展开看逐字段）/ ❌ 被拒。
- 同时在策略编辑弹窗保存后弹「应用到 N 个在跑任务」的回执摘要。

---

## 8. 「更新无法生效」案例全集（核查表）

按「能否保存 / 能否生效」双列给出。这是本方案的重点交付物。

| # | 场景 | 保存 | 运行期实际效果 | 处置 |
|---|---|---|---|---|
| 1 | 已加 3 仓，上限 3 → 10（扩容） | ✅ | ✅ 继续加第 4 笔 | 档位需用 **append** 模式追加，否则第 4 笔用错档（见 6.1） |
| 2 | 已加 3 仓，上限 5 → 4（缩减剩余额度） | ✅ | ✅ 还能再开 1 笔，已开仓位不动 | 无需额外机制，现成判定天然支持（见 6.2） |
| 3 | 已加 3 仓，上限 5 → 3（额度刚好用完） | ✅ | ✅ 不再加仓，已开仓位不动 | 同上；手动仓门槛判「已加完」，可正常开启 |
| 4 | 已加 3 仓，上限 5 → **2**（低于已开笔数） | ❌ **前端直接拒** | — | 前端标红 + 禁用保存；后端 400 `limit_below_added` 兜底（见 4.6） |
| 5 | 已加 5 仓，上限 10 → 2 | ❌ **前端直接拒** | — | 同上 |
| 6 | 已加 3 仓，上限 5 → 0 | ✅ | ⚠️ 0 = **不限**，不是清零 | 前端加提示；后端校验 `limit = 0` 时按「不限」处理（见 4.6.5） |
| 7 | 缩容后手动分散仓门槛被「解锁」 | — | ✅ **反效果已消除** | 该反效果只在 `新上限 ≤ 已开笔数` 时出现，而那个值被前端拒掉了 |
| 8 | 手动分散仓已开完（已止盈 / 被平），还要补逆势批次 | ⚠️ | ❌ **完全无效** | 若该手动仓触发了「止盈联动清仓」→ 任务已 `request_stop` + `_close_all` 收口，此后所有热推被拒（`self._stopping or self._finished`）。**必须新建任务** |
| 9 | 手动分散仓已开完，未触发联动清仓，任务仍在跑，还要补逆势批次 | ⚠️ 可保存 | ❌ **不会加仓**（改后的 §6.3 闸门：手动仓开完该规则加仓已收口） | 手动仓是**本任务最后一次加仓**；逆势在途限价单会被撤掉。**要加仓必须新建任务** |
| 10 | 手动仓已开过 → 改 `manual_scatter` 的入场价 / 止盈 / 手数 | ❌ 400 | — | `rejected: manual_locked`，前端只读遮罩（见 6.3） |
| 11 | 改 `lot_pl_tiers` / `float_pl_ratio`，重算出的 SL/TP 已被现价越过 | ❌ 400 | — | 决策 2：`rejected: sltp_crossed`，保存前就拦（见第 4 节） |
| 12 | 改 `lot_pl_tiers` / `float_pl_ratio`，重算出的 SL/TP 仍在现价正确侧 | ✅ | ✅ 下一拍改单生效 | 次数型浮盈的已消耗次数**不复活**（热推沿用运行态 `remaining_times`） |
| 13 | 手动仓在场时改 `lot_pl_tiers` | ✅ | ⚠️ **不生效** | 手动仓 TP 是绝对权威（`_manual_authority_tp`），改单被对齐逻辑覆盖。回执标 `no_effect` |
| 14 | 改 ATR / 波幅的 `timeframe` / `range_*` | ✅ | ✅ 下一拍生效 | `_metric_specs` / `_live_specs` 置空重解析 |
| 15 | 改 SMC `calc_type` / 周期 | ✅ | ✅ 限价身份变化 → 撤单重挂 | 已用记录 `_smc_used` 不回收 |
| 16 | 改 `batch_count`（均分重切档位） | ⚠️ 有在跑任务时拒绝 | — | 提示改用「追加档位」；`redistribute` 模式在有在跑任务时禁用（见 6.1） |
| 17 | 改规则 `type`（1 ↔ 2）或模版 | ❌ 400 | — | LOCKED；`apply_strategy` 现有逻辑也会拒绝模式切换 |
| 18 | 网格 / 以损定量策略改任何字段 | ✅（落库） | ❌ 不影响在跑任务 | 后端 `_hot_push_running_snapshot` 对 tpl_2 / tpl_3 直接 return，任务快照冻结 |
| 19 | 节点离线时改配置 | ✅（离线节点不参与预校验） | ⚠️ 重连 resume 才用新快照；离线期间可能已加超新上限 | 库内快照已改（`rewrite_running_strategy_snapshots`），重连生效；回执标 `deferred` |
| 20 | 多任务场景，节点 A 已开 3 笔、节点 B 已开 1 笔，改上限到 2 | ❌ **前端直接拒** | — | 校验取**所有在跑任务的最大值**（3），宁可严不可松（见 4.6.3） |
| 21 | 启用手动分散仓时，前端显示的「加仓笔数」= `total_lot_limit + 1` | — | — | 改为「分批加仓笔数」与「手动分散仓 1 笔」分列，避免隐式换算导致误改 |
| 22 | 热推时节点正在 `await place_market_order` | ✅ | ⚠️ 本拍用旧值、下拍用新值 | 每拍取不可变 `self._cfg` 引用（见 7.2） |
| 23 | 改逆势限价档位后，改价节流窗口内 | ✅ | ⏳ 下一拍补 | 波幅自适应时死区 5s / 拉回 10s，回执不报「未生效」，等自然生效 |
| 24 | 改 `status` 关掉某规则 | ✅ | ✅ 停止该规则加仓，**不平仓** | 已有持仓继续由 `_lot_sltp_sync` 管 |
| 25 | 手动仓开完后，扩容上限（3 → 10） | ✅ | ❌ **不会加仓** | 加仓已收口（决策 5）；回执标 `no_effect`，提示要加仓需新建任务 |
| 26 | 手动仓开完后，改档位间距 / 手数 | ✅ | ❌ **不会加仓** | 同上；已开仓位不受影响 |
| 27 | 手动仓开完后，改 `lot_pl_tiers` / `float_pl_ratio` | ✅ | ✅ **仍生效** | 收口只拦「加仓」，不拦「改单」；止盈止损照常按新参数重算 |
| 28 | 手动仓挂单被撤 / 被券商拒绝（未成交） | ✅ | ✅ **加仓资格恢复** | `_observe_manual_pending` 里 `self._manual_fired.discard(rule_type)` 撤销标记（见 6.3.3） |
| 29 | 逆势手动仓开完，顺势规则仍启用 | ✅ | ✅ 顺势照常加仓 | 收口**按规则隔离**：逆势锁逆势，顺势不受影响（见 6.3.3 第 4 点） |

---

## 9. 前端呈现

- 策略编辑弹窗里显示该策略**当前在跑任务**的数量、各规则已开仓笔数、以及「本次保存会影响 N 个在跑任务」。
- **「加仓笔数」输入框实时下限校验**（决策 1 核心）：
  - 新值 < 该规则已开笔数 → 输入框标红、**禁用保存按钮**、显示具体数字对比；
  - 文案示例：「逆势加仓已开 **3** 笔，新上限 **2** 笔低于已开笔数。上限只能缩减剩余额度，不能低于已开笔数，请填写 ≥ **3** 的值。」
  - 新值 ≥ 已开笔数 → 允许保存，并提示「保存后本任务还能再开 N 笔」。
  - 校验**按规则独立**（逆势 / 顺势各算各的）。
- `加仓笔数` 输入框：改为「分批加仓笔数」与「手动分散仓 1 笔」分列显示，避免 `+1` 的隐式换算。
- 保存后回执展示：✅ 生效 / ⚠️ 部分生效（列出未生效项及原因）/ ❌ 被拒绝。
- 手动分散仓锁定区：只读 + 灰色遮罩 + 明确原因文案（见 6.3）。
- **加仓已收口提示**：某规则手动仓已开过 → 该规则的「加仓笔数」「档位」区块也置灰，提示：
  > 该规则的手动分散仓已开仓，**本任务加仓已收口，不会再新增任何批次**。如需继续加仓，请等待任务收口后新建任务。

---

## 10. 实施顺序

| 阶段 | 内容 | 独立可上线 |
|---|---|---|
| **P0-0** | **加仓收口闸门**（决策 5）：`_manual_fired` 语义扩展 → 顺势判定过滤 + 逆势限价撤单；跨规则隔离 | ✅ 独立可上线，修正与产品语义冲突的现状行为 |
| **P0-1** | `config_version` + 节点 `strategy_update_ack` 逐字段回执 + 后端落事件 | ✅ 先做到「看得见」 |
| **P0-2** | 前端 `rebuildBatchLevels` 拆 append / redistribute；`redistribute` 有在跑任务时禁用 | ✅ 直接修掉「扩到 10 仓用错档」 |
| **P0-3** | **「已开笔数」数据链路**：`add_counts` 落库 → `runtime_hints` API → 前端下限校验 + 后端 400 兜底 | ✅ 决策 1 的落地 |
| **P1-1** | `strategy_precheck` 协议 + 节点 dry-run + PATCH 400 阻断（`sltp_crossed` / `manual_locked` / `type_locked` / `batch_inconsistent`） | ✅ 决策 2 的落地 |
| **P1-2** | 前端锁定区只读遮罩 + `manual_fired` 提示 + 「加仓已收口」提示 | ✅ 决策 3 / 5 的落地 |
| **P2-1** | `self._cfg` 不可变快照 + `evaluate` / `plan_counter_limit` 改从 `cfg` 取规则 | 消除 apply / 判定交错 |
| **P2-2** | 档位 `pos_from` 锚定校验（追加档起始序号 > 已加笔数） | 兜底档位错位 |
| **P3** | 补 `remaining_times` 重置提示；`limit = 0` 语义提示；加仓笔数分列显示；同步 `使用与部署文档.md` / `技术实现方案.md` | 体验收尾 |

---

## 11. 待确认的两个遗留点

1. **`float_pl_ratio` 次数型监控（`monitor_mode = monitor_times`）**
   热推后已消费的次数**不会**被复活：节点沿用运行态的 `remaining_times`
   （见 `signal_pl.prepare_runtime(keep_remaining=...)`），只有监控方式在
   「循环 ↔ 指定次数」之间切换时才重新起算；回执会写明「已消耗的次数不复活」。
   → 已按「热推保持已消耗次数」实现，需求方如需改成「热推重置为上限」请再提。

2. **离线节点的预校验**
   离线节点无法参与 `strategy_precheck`。本方案按「允许保存 + 回执标 `unchecked` + 提示重连后生效」处理。
   → 需求方需确认：是否改为**只要有离线节点在跑就拒绝保存**（更保守）？

---

## 12. 关键代码位置索引

| 主题 | 位置 |
|---|---|
| 热推入口 | `backend/app/strategies.py :: _hot_push_running_snapshot` |
| 快照生成 | `backend/app/group_rules.py :: strategy_rules_snapshot` |
| 库内快照改写 | `backend/app/group_persist.py :: rewrite_running_strategy_snapshots` |
| WS 下发 | `backend/app/group_dispatcher.py :: push_strategy_updates` |
| 节点接收 | `node_client/node_client.py :: _do_strategy_update` |
| 节点应用 | `node_client/strategy_runner.py :: apply_strategy` |
| 加仓判定 | `node_client/strategy_rules.py :: _prepare_rule` / `plan_counter_limit` / `pick_batch_level` |
| 档位重建 | `frontend/src/components/StrategyFormModal.vue :: rebuildBatchLevels` |
| 档位增删（不重切区间） | `frontend/src/components/StrategyFormModal.vue :: addBatchLevel` / `removeBatchLevel` |
| 手动仓门槛 | `node_client/manual_scatter.py :: manual_blocked_reason` / `batch_add_limit` |
| 手动仓一次性 | `node_client/strategy_runner.py :: _manual_fired` / `_maybe_open_manual_scatter` |
| **加仓收口闸门**（决策 5） | `node_client/strategy_runner.py :: _manual_fired`（标记点 `_place_manual_pending`）→ 顺势过滤点 `_on_event` 的 `evaluate` 调用处；逆势撤单点 `_sync_counter_limit`；撤销点 `_observe_manual_pending` 的 `_manual_fired.discard` |
| 共同 SL/TP | `node_client/signal_pl.py :: plan_common_stops` / `prepare_runtime` |
| 逆势限价 | `node_client/strategy_runner.py :: _sync_counter_limit` / `_align_counter_order` |
| **加仓收口闸门**（新） | `node_client/manual_scatter.py :: add_closed_rule_types` / `is_add_closed`；`node_client/strategy_rules.py :: PositionCtx.add_closed` |
| **逐字段回执**（新） | `node_client/strategy_reconcile.py :: reconcile_snapshot` / `summarize`；`node_client/strategy_runner.py :: apply_strategy_detailed` / `send_update_ack` |
| **保存前预校验**（新） | `node_client/strategy_precheck.py :: check_sltp_crossed` / `check_limit_below_added` / `check_manual_locked` / `check_batch_anchor`；`backend/app/strategy_precheck.py :: check_subtasks` |
| **已开笔数链路**（新） | `backend/app/group_persist.py :: record_strategy_progress`（写 `add_counts_json`）→ `strategy_runtime_hints` → `backend/app/models.py :: StrategyOut.runtime_hints` → `frontend/src/api/types.ts` |
| **下限兜底**（新） | `backend/app/strategy_validate.py :: check_limit_below_added` / `summarize_blocks` |
| **前端呈现**（新） | `frontend/src/components/StrategyFormModal.vue :: runtimeBanner` / `appendBatchRanges` / `manualScatterLocked` / `limitFloor` |
| **信号盈亏热推**（新，见 `docs/superpowers/specs/2026-11-01-signal-pl-hot-push-design.md`） | 止损配置指纹 `node_client/strategy_runner.py :: _sl_cfg_sig` / `_lot_sltp_sl_cfg_sig`；逐侧越价判定 `node_client/signal_pl.py :: stop_side_decision` / `crossed_sides`；手动仓在场只写止损 `node_client/strategy_runner.py :: _lot_sltp_sync_sl`；回执补齐 `node_client/strategy_reconcile.py :: _pl_ratio_fields` / `_pl_tiers_fields` |

---

## 13. 实施落地记录

### 13.1 阶段完成情况

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0-0** | 加仓收口闸门（决策 5）：顺势判定过滤 + 逆势限价撤单 + 跨规则隔离 | ✅ 已落地 |
| **P0-1** | `config_version` + `strategy_update_ack` 逐字段回执 + 后端落事件 | ✅ 已落地 |
| **P0-2** | 前端档位拆 `append` / `redistribute`；有在跑任务时禁用 redistribute | ✅ 已落地 |
| **P0-3** | 已开笔数数据链路：`add_counts` 落库 → `runtime_hints` → 前端下限校验 + 后端 400 兜底 | ✅ 已落地 |
| **P1-1** | `strategy_precheck` 协议 + 节点 dry-run + PATCH 400 阻断 | ✅ 已落地 |
| **P1-2** | 前端锁定区只读遮罩 + 「加仓已收口」提示 | ✅ 已落地 |
| **P2-1** | 不可变快照 + 判定侧统一从快照取规则 | ✅ 已落地 |
| **P2-2** | 档位 `pos_from` 锚定校验 | ✅ 已落地 |
| **P3** | `remaining_times` 重置提示、`limit = 0` 语义提示、加仓笔数分列、文档同步 | ✅ 已落地 |

### 13.2 新增 / 修改的关键文件

**节点侧（`node_client/`）**

| 文件 | 说明 |
|---|---|
| `manual_scatter.py` | 新增 `add_closed_rule_types(fired)` / `is_add_closed(fired, rule_type)`，判定「该规则加仓已收口」 |
| `strategy_rules.py` | `PositionCtx` 增加 `closed_rules`；`_prepare_rule` 在收口规则上直接返回 `None` |
| `strategy_reconcile.py` | **新增**。纯函数 `reconcile_snapshot(old, new, added_by_rule, closed_rules, manual_fired)` → `(fields, effective_snapshot)`；`summarize()` → `applied` / `partial` / `rejected` |
| `strategy_precheck.py` | **新增**。纯校验：`check_sltp_crossed` / `check_limit_below_added` / `check_manual_locked` / `check_batch_anchor` |
| `strategy_runner.py` | `_add_closed` / `_closed_rule_types` / `_open_rule_types`；`_on_event` 过滤顺势加仓；`_sync_counter_limit` 撤掉收口规则的在途限价单；`_rules` 只读属性 + `_deep_copy_snapshot`；`apply_strategy_detailed` / `send_update_ack` / `precheck_strategy` |
| `node_client.py` | `_do_strategy_update` 改走 `apply_strategy_detailed` 并回 `strategy_update_ack`；新增 `_do_strategy_precheck` → `strategy_precheck_result` |

**后端侧（`backend/app/`）**

| 文件 | 说明 |
|---|---|
| `orm.py` / `db.py` | `TradingStrategy.config_version`（Integer，默认 0）；`GroupTaskDispatch.add_counts_json`（JSON）；配套迁移 `_migrate_strategy_config_version` / `_migrate_group_dispatch_add_counts` |
| `models.py` | `StrategyOut` 增加 `config_version: int` 与 `runtime_hints: Optional[dict]` |
| `group_rules.py` | `strategy_rules_snapshot` 带出 `config_version` |
| `group_dispatcher.py` | `build_strategy_update_command` 带出 `config_version` |
| `strategy_validate.py` | **新增**。后端下限兜底 `check_limit_below_added` / `summarize_blocks` |
| `strategy_precheck.py` | **新增**。RPC 协调器 `request` / `check_subtasks` / `resolve` |
| `group_persist.py` | `record_strategy_progress` 写 `add_counts_json`；新增 `record_strategy_update_ack`（落 `GroupTaskEvent(event_type="strategy_update")`）、`running_strategy_subtasks`、`strategy_runtime_hints` |
| `strategies.py` | `get_strategy` 附带 `runtime_hints`；`update_strategy` 做 `limit_below_added` 校验 + `_precheck_running_tasks`（400 阻断） |
| `ws_gateway.py` | 处理 `strategy_update_ack` / `strategy_precheck_result` |

**前端侧（`frontend/src/`）**

| 文件 | 说明 |
|---|---|
| `api/types.ts` | 新增 `StrategyRuntimeHints`；`StrategyOut` 增加 `config_version` / `runtime_hints` |
| `stores/hub.ts` | 新增 `fetchStrategyDetail(id)`，取带 `runtime_hints` 的详情 |
| `components/StrategyFormModal.vue` | `runtimeHints` / `addedByRule` / `hasRunningTask` / `limitFloor` / `manualScatterLocked` / `runtimeBanner`；`appendBatchRanges` + `batchEditMode()`（`append` vs `redistribute`）；锁定区只读遮罩；下限校验；`limit = 0` 语义提示；结构化 400 错误提示 |

### 13.3 验证结果

| 范围 | 结果 |
|---|---|
| `node_client` 单元测试 | 679 项全部通过 |
| `backend` 单元测试 | 881 项全部通过 |
| 合计 | **1560 项通过** |
| 前端 `npm run typecheck` | 通过（0 错误） |

### 13.4 遗留待确认

§11 的「离线节点预校验」仍按方案默认处理（离线节点不阻断保存），等待需求方确认后再调整。
§11 的「次数型浮盈」已按「热推保持已消耗次数」实现并写进回执。

---

## 14. v2 修订实施记录（2026-11-02 · 配置无条件热推）

设计文档：[`docs/superpowers/specs/2026-11-02-hot-push-always-apply-design.md`](superpowers/specs/2026-11-02-hot-push-always-apply-design.md)
实施计划：[`docs/superpowers/plans/2026-11-02-hot-push-always-apply-plan.md`](superpowers/plans/2026-11-02-hot-push-always-apply-plan.md)

### 14.1 口径变更

| v1 口径 | v2 口径 |
|---|---|
| 上限低于已开笔数 → 前端拒存 + 后端 400 `limit_below_added` | **一律保存、一律下发**；节点自行判断：已加 ≥ 上限 → 不再新增批次，回执 `no_effect` + `exec=no_more_adds` |
| 保存前向节点发 `strategy_precheck`，有阻断项 → 400 | **预检链路整体删除**（后端 / 节点 / WS 三处） |
| SL/TP 越价 → 保存时拒（400 `sltp_crossed`） | 节点侧保留旧值 + 告警事件；保存放行 |
| 上限类字段在节点侧被标 `frozen`、保留旧值 | **`frozen` 退役**；上限类字段**一律采纳新值** |
| 删除规则 → 保留旧规则并标 `locked` | **采纳删除**：该规则不再参与加仓判定；在途限价单撤掉；已开仓位不平仓 |
| 档位没覆盖下一次加仓 → 节点静默卡死 | 节点**沿用相邻/最后一档**兜底；服务端**自动补齐**档位到覆盖上限 |
| 热推后等价格触发才生效 | 热推后**下一拍立刻对齐**（扩容续挂下一档 / 缩容撤掉在途单） |
| 前端只显示「已开笔数」 | 新增 **`add_progress`**：当前第几批、还能再加几笔、下一批间距与手数 |

### 14.2 关键代码变更

**后端**

| 文件 | 变更 |
|---|---|
| `backend/app/strategies.py` | 删除 `limit_below_added` 400 分支与 `_precheck_running_tasks` 调用；PATCH 一律 200 |
| `backend/app/strategy_precheck.py` | **删除** |
| `backend/app/strategy_validate.py` | **删除** |
| `backend/app/ws_gateway.py` | 删除 `strategy_precheck_result` 分支与 import |
| `backend/app/group_rules.py` | 新增 `_ensure_batch_coverage`：档位自动补齐到覆盖 `[1, total_lot_limit]` |
| `backend/app/group_persist.py` | `record_strategy_progress` / `record_strategy_update_ack` 落 `add_progress`；`strategy_runtime_hints` 合并 `add_progress` |
| `backend/app/orm.py` / `backend/app/db.py` | `GroupTaskDispatch.add_progress_json` + 迁移 `_migrate_group_dispatch_add_progress` |

**节点侧**

| 文件 | 变更 |
|---|---|
| `node_client/strategy_reconcile.py` | 退役 `frozen`；上限类字段一律采纳新值；规则删除采纳；逐字段加 `exec` 结论（`will_add` / `no_more_adds` / `waiting_tp_sl` / `locked_by_manual`） |
| `node_client/strategy_rules.py` | `pick_batch_level` 命中失败改为兜底（沿用相邻/最后一档），新增 `batch_level_is_fallback`；`_prepare_rule` 同步调整 |
| `node_client/strategy_runner.py` | 新增 `_add_align_pending` / `_align_adds_after_update` / `_counter_no_next_reason` / `_add_progress` / `_level_at`；删除 `precheck_strategy` / `_precheck_sltp_crossed` |
| `node_client/strategy_precheck.py` / `node_client/test_strategy_precheck.py` | **删除** |
| `node_client/node_client.py` | 删除 `_do_strategy_precheck` |

**前端**

| 文件 | 变更 |
|---|---|
| `frontend/src/api/types.ts` | 新增 `StrategyAddProgress`；`StrategyRuntimeHints.add_progress`；`StrategyUpdateField.exec`；`GroupTaskEventDetail.progress` |
| `frontend/src/components/StrategyFormModal.vue` | 删除 `limitFloor` 拦截，改为黄色提示 `formWarnings`；`ensureBatchCoverage` 自动追加档位；`runtimeBanner` 展示加仓进度 |
| `frontend/src/views/GroupSignalsView.vue` | 回执渲染支持 `exec`；`no_effect` 文案改为「➖ 已保存，不再新增批次」；展示 `add_progress` |

### 14.3 验证结果

| 范围 | 结果 |
|---|---|
| `node_client` 单元测试 | **769 项全部通过** |
| `backend` 单元测试 | **1032 项全部通过** |
| 前端 `npm run build`（含 `vue-tsc --noEmit`） | 通过（0 错误） |
