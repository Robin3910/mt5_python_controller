# 止盈金额按「当前仓位命中档位」计算

> 日期：2026-11-04
> 适用模版：`tpl_1`「AI智能加仓策略」
> 关联代码：`node_client/signal_pl.py`、`node_client/manual_scatter.py`、`node_client/strategy_runner.py`、`node_client/strategy_rules.py`、`frontend/src/components/StrategyFormModal.vue`
> 关联文档：`docs/superpowers/specs/2026-11-02-price-level-design.md`（指定价档位设计；本文**取代**其中 §4.1「止盈分支的生命周期」）

---

## 0. 一句话

> **止盈金额不再恒等于「阶梯止盈批次 1 的金额」，而是按该方向当前总手数命中的档位取值；止盈价 = 加权均价 ± 命中档位金额 ÷ ((tick_value / tick_size) × 该方向总手数)。**

用户场景：阶梯配置 `0.05 手 → 100`、`0.1 手 → 200`。指定价成交前总手数 0.08 → 命中批次 1（100）；指定价成交后总手数 0.12 → 命中批次 2（200）→ **止盈按 200 反推**。

---

## 1. 背景与现状

### 1.1 现状：金额恒为 tier1

`signal_pl.lot_pl_amount`（`signal_pl.py`）只取 `tiers[0].pl_amount`，`plan_common_stops` 只吃一个标量 `tp_amount`，**两个方向共用同一个金额**。批次 2 及以后是**死配置**：配了不生效，改了没有任何反应。

### 1.2 现状：指定价档位的「绝对价止盈分支」

`2026-11-02-price-level-design.md` §4.1 设计了一套「按指定价算止盈」的分支：

- 指定价档位成交那一拍 → 该方向的止盈改用档位里配的绝对价 `price_tp`；
- 该分支价是**按 tier1 金额反推手数**得到的（`manual_scatter.price_level_volume`），
  因此「价格打到这里时全场合计盈亏 == tier1 金额」是一个隐含不变量；
- 分支是**一次性**的：其它类型加仓进来就撤销（`_revoke_price_tp_override_on_other_add`）；
- tier1 金额被改 → 分支作废（`_invalidate_price_tp_override_on_tier_change`）。

### 1.3 现状的问题

| # | 问题 | 现象 |
|---|---|---|
| 1 | 共同止盈金额恒为 tier1 | 手数从 0.08 涨到 0.12，止盈仍按 100 算，永远够不到 tier2 的 200 |
| 2 | 绝对价分支与档位金额彻底脱钩 | 改了 tier2 的 200，止盈价**一动不动**（分支价是绝对价，与档位金额无关） |
| 3 | 加仓撤销分支 → TP 从 200 跳回 100 | 同一套配置在不同仓位阶段给出不同的出场金额 |
| 4 | tier2+ 是死配置 | 前端能配 10 档，实际只有第 1 档生效，用户困惑 |
| 5 | 分支机制复杂且易错 | 一次性、过期判定、runtime 还原……都是为「凑出 tier1 金额」服务的副产物 |

---

## 2. 已拍板的决策

| # | 决策 | 落地含义 |
|---|---|---|
| 1 | **统一改造** | 所有共同止盈都按「当前手数命中档位」算金额，不只指定价路径 |
| 2 | **止盈价改为计算值** | `TP = W ± 命中金额 / (k × V)`；配置里的 `price_tp` **降级**为「反推手数的参考价」，不再是出场价 |
| 3 | **删掉绝对价分支整套机制** | `_price_tp_override_*` / `_align_positions_to_price_tp` / `_revoke_*` / `_invalidate_*` / runtime 字段全部移除 |
| 4 | **命中规则：从高到低，第一条 `min_lot ≤ 当前手数`** | 手数涨上去自动跳档，金额变大 |
| 5 | **每个方向独立按自己的手数选档** | 多空不混算；`close_action` 只平一侧时也按该侧自己的手数 |
| 6 | **门槛仍只看批次 1 的 `min_lot`** | 「够不够格算止盈」的闸门不变，与今天一致 |
| 7 | **指定价档位的手数按「成交前已有仓位的档位」开** | **不做**不动点迭代。成交后仓位变了 → 下一拍按新档位重算 TP（跳档） |

---

## 3. 新口径

### 3.1 命中档位

```
命中档位 = 从最高档往低档找，第一条 min_lot ≤ 该方向当前总手数 的档位
金额 A   = |命中档位.pl_amount|
```

- 金额为 0 的档位跳过，继续往下找（与 `manual_scatter.match_target_pl` 既有口径一致）。
- **一条都没命中**（当前手数连批次 1 的门槛都没到）→ **回落批次 1**（最低档）。
  - 为什么不是「不算止盈」：指定价档位挂单时可能**一股仓位都没有**（首次开仓），
    这时手数必须按批次 1 的金额反推，否则第一档永远挂不出来。
  - 止盈侧不需要在这里挡「手数不够」——那是**批次 1 的 `min_lot` 门槛**的事（§3.3）。
  - 批次 1 的金额也是 0 时才真的没有金额可用（该方向不算止盈）。

### 3.2 止盈价

```
W = Σ(手数 × 开仓价) / Σ手数        （该方向全部参与止盈的持仓，含指定价仓）
k = tick_value / tick_size
V = 该方向总手数

BUY : TP = W + A / (k × V)
SELL: TP = W − A / (k × V)
```

`k × V` 是「每移动一个价格，全场盈亏变化多少」。所以 TP 就是「打到这个价时全场合计盈亏 == A」。

### 3.3 门槛

批次 1 的 `min_lot` 仍是「够不够格算止盈」的闸门：该方向总手数 < 批次1门槛 → 不算止盈（沿用今天的行为）。

> 注：命中档位用的 `min_lot` 与这个门槛是**两件事**：前者决定「用哪一档的金额」，后者决定「算不算」。

### 3.4 方向独立

多单 0.5 手、空单 0.05 手时：多单按 0.5 命中高档，空单按 0.05 命中低档（或不命中）。**不共用金额**。

---

## 4. 指定价档位的新行为

### 4.1 手数：按「成交前已有仓位」命中的档位

用户拍板：**这一档的手数只按它成交时已经有的仓位的档位开，成交加仓后再跳档重算**。

```
挂单时：
  存量方向手数 V0 = 该方向已有持仓手数之和（不含手动分散仓）
  目标金额 A0 = 命中档位(V0).pl_amount
  每手止盈 = |price_tp − 指定价| / tick_size × tick_value
  存量在 price_tp 时刻的预估盈亏 = Σ 各持仓到 price_tp 的盈亏
  这一档手数 = (A0 − 存量预估盈亏) ÷ 每手止盈      （向上取整到 step，夹 min/max）
```

**不做**不动点迭代（不按「成交后总手数」反推）——这样手数是**当场就能算死**的，
不需要预估自己有多大，语义也更直白。

### 4.2 成交后：跳档重算

成交那一拍总手数从 `V0` 变成 `V0 + 这一档手数`：

- 若 `V0 + 这一档手数` 命中了更高的档 → TP 金额跳档 → **该方向的止盈价重算**；
- 重算由既有的仓位构成签名（`signal_pl.position_book_signature`）触发，无需新机制。

例：`0.05 → 100`、`0.1 → 200`。成交前 0.08（命中 100）→ 这一档按 100 的口径开 →
成交后 0.12（命中 200）→ TP 按 200 重算。

### 4.3 `price_tp` 的新语义

| 项 | 旧 | 新 |
|---|---|---|
| 含义 | 出场绝对价（成交后接管该方向止盈） | **反推手数的参考价**（决定「每手止盈」这个距离） |
| 是否落在入场价盈利侧 | 必须（否则挂不了单） | **仍然必须**（手数公式依赖这个距离） |
| 是否参与出场价计算 | 是（就是出场价） | 否（出场价 = 加权均价 ± 命中金额/(k×V)） |

前端文案必须同步改写，否则用户会以为填的价会失效。

---

## 5. 代码改动

### 5.1 `node_client/signal_pl.py`

| 改动 | 说明 |
|---|---|
| 新增 `tier_amount_for_lot(tiers, lot)` | 按手数命中档位 → `\|pl_amount\|`；从高到低，第一条 `min_lot ≤ lot`；没命中**回落批次 1**（实现放在 `manual_scatter`，这边转发） |
| 新增 `lot_pl_target_for_lot(cfg, lot)` | 整份配置入口（内部取 `RULE_LOT_PL_TIERS` 后调上面那个） |
| 新增 `lot_pl_tiers_enabled(cfg)` | 分档止盈是否启用（替代 `lot_pl_amount(...) is not None` 的「有没有东西可写」判断） |
| `plan_common_stops` | 入参 `tp_amount` → `tp_tiers`；**函数内部按每个方向自己的手数选金额**；输出 `plan["amount"]` = 命中金额，`plan["tier_index"]` = 命中的档位下标 |
| `opening_stops` | 入参 `tp_amount` → `tp_tiers`；`tp_override` 参数删除 |
| `lot_pl_cfg_sig` | 指纹从 `(enabled, tier1金额, tier1门槛, close_action)` 扩成**全部档位**的 `(min_lot, pl_amount)` —— 否则改 tier2 不触发重算 |
| `lot_pl_amount` | 保留：仍表示「批次 1 的金额」，用于「分档止盈是否配了金额」的判断与首单兜底 |

### 5.2 `node_client/manual_scatter.py`

| 改动 | 说明 |
|---|---|
| `match_target_pl` | **不动**：手动分散仓路径保留「匹配不到就返回 None（不开仓）」的原口径。分档匹配的**归属地**移到本模块（`tier_pl_amount_for_lot` / `tier_amount_for_lot`），`signal_pl` 那边转发过来，两边同源 |
| `price_level_volume` | 新增 `current_lot` 入参；目标金额从 `tier0_pl_amount` 改为**按 `current_lot` 命中的档位** |
| `compute_volume`（手动分散仓） | **不动**：手动分散仓的「固定批次 1」口径是 §18.52 明确拍板的，与本次改造无关 |
| `tiers_target_changed` | **不动**（它服务于手动分散仓撤单重挂）；其 docstring 里「批次 1 是真正生效的那个」要改口径说明 |

### 5.3 `node_client/strategy_runner.py`

**删除**（绝对价分支整套）：

- `self._price_tp_override_by_side` / `self._price_tp_override_tier`
- `_price_tp_override()` / `_price_tp_override_side()` / `_clear_price_tp_override()`
- `_invalidate_price_tp_override_on_tier_change()`
- `_align_positions_to_price_tp()`（含主循环里的调用点）
- `_prune_price_tp_override()` / `_revoke_price_tp_override_on_other_add()`（含 `_add_position` 里的调用点）
- `_observe_price_level_fills` 里立分支的那段
- runtime 字段 `price_tp_override` / `price_tp_override_tier`（含重连还原逻辑）

**修改**：

- `_lot_sltp_sync`：改传 `tp_tiers`；「有没有东西可写」的判断改用 `lot_pl_tiers_enabled`
- `_lot_sltp_sync_sl`：改传 `tp_tiers`（这一路 tp 恒为 None）
- `_stops_on_open`：改传 `tp_tiers`；删 `tp_override`
- `_price_level_plan`：目标金额改为按**该方向存量手数**命中的档位
- `_merge_entry_stops`：不变（首单没有存量手数 → 命中批次 1，与今天一致）

**保留**：

- `_price_level_tp_ref` / `_price_level_sl_tp_refs`：仍用于恢复自有止损；`tp_ref` 降级为「参考价」，仅作诊断
- `_price_filled[ticket]["tp_ref"]`：保留记录，注释改口径

### 5.4 `frontend/src/components/StrategyFormModal.vue`

- `FIELD_HELP.batch_price_tp`：改写为「**参考止盈价**：用于反推这一档的手数（每手止盈 = |参考价 − 指定价|）；成交后该方向的止盈按**当前总手数命中的档位金额**计算，不是这个价」
- `FIELD_HELP.signal_lot_pl`：改写为「分档金额按**当前总手数**命中档位：从最高档往下找第一条「总手数 ≥ 门槛」的档，用它的金额反推止盈价」
- 字段标题：`calc_type === 'price'` 时的「止盈价」改成「**参考止盈价**」
- `frontend/src/api/types.ts`：`amount` 的注释从「批次1盈亏金额」改成「生效的止盈金额（当前总手数命中的那一档）」；补 `tier_index`
- `frontend/src/views/GroupSignalsView.vue`：详情行从「批次1金额」改成「命中批次金额」，并带上命中的批次号

---

## 6. 重算触发

| 事件 | 是否重算 | 机制 |
|---|---|---|
| 仓位构成变化（开仓 / 加仓 / 减仓） | ✅ | `position_book_signature` 变化 → `_lot_sltp_sync` 重算 |
| **跨档跳变**（手数跨过某档 `min_lot`） | ✅ | 仓位构成变化的副产品 |
| 热推改任意档位的 `min_lot` / `pl_amount` | ✅ | `lot_pl_cfg_sig` 扩成全档位后自动生效 |
| 热推改 `close_action` | ✅ | 既有指纹项 |
| 客户入金 | ❌ | 只影响止损侧，止盈与余额无关（既有语义） |
| 价格波动 | ❌ | 止盈价不跟报价动；只有「现价冲过已落下的 TP」才平仓 |

---

## 7. 风险与边界

| # | 风险 | 处理 |
|---|---|---|
| 1 | **跳档时 TP 金额跳变** → 新 TP 可能落到现价错误一侧 | 走既有 2b：配置改动类「保留旧值 + 告警」；加仓类「市价平掉该方向」（既有语义，不新造） |
| 2 | 手数**减少**（自有止损被打掉）→ 可能从高档掉回低档 → TP 变近 | 同上；`_price_sl_dropped` 已触发重算 |
| 3 | `close_action` 只平一侧 | 命中档位按该侧自己的手数选，不混算 |
| 4 | 手动分散仓在场 | 它的 TP 仍是权威，不动（`_manual_authority_tp`） |
| 5 | 命中档金额为 0 | 跳过该档继续往下找；一条都没命中 → 回落批次 1；批次 1 金额也是 0 → 该方向不算止盈 |
| 6 | `price_tp` 语义变更 | **产品层面的语义变更**，前端文案必须同步（§5.4） |
| 7 | 存量配置里 tier2+ 是死配置 | 本次改造**让它们生效** —— 老配置会立刻改变行为（这是本次改造的目的） |
| 8 | 指定价档位挂单在手数变化后要撤单重挂 | `_sync_price_level` 每拍重算计划并比对 `volume` → 变了就撤单重挂（既有机制） |

---

## 8. 单测清单

| 文件 | 覆盖点 |
|---|---|
| `node_client/test_signal_pl.py` | 命中档位（从高到低）；方向独立；`plan_common_stops` 逐方向选金额；`lot_pl_cfg_sig` 覆盖全档位；删掉 `tp_override` 相关的旧用例 |
| `node_client/test_manual_scatter.py` | `match_target_pl` 返回绝对值；`price_level_volume` 按 `current_lot` 选档 |
| `node_client/test_strategy_runner.py` | 0.08 → 0.12 用 200 重算 TP；跨档跳变；改 tier2 生效；方向独立；删掉绝对价分支的旧用例 |
| `node_client/test_balance_sl_sync.py` | 自有 SL 被打掉 → 掉档 → TP 重算 |

---

## 9. 与既有文档的关系

- `docs/superpowers/specs/2026-11-02-price-level-design.md` §4.1「止盈分支的生命周期」**整体作废**，由本文取代；该文档其余部分（自有止损取更近、批次衔接、runtime 还原）继续有效。
- `技术实现方案.md` §18.45 / §18.48 / §18.52 里「批次 1 的金额是真正生效的那个」的表述需要修订 —— 改为「按当前手数命中的档位」。
- `docs/superpowers/specs/2026-11-01-signal-pl-hot-push-design.md` 里「批次 2+ 是死配置」的结论**不再成立**。
