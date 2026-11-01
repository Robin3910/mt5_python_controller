# 信号盈亏控制 · 止盈止损热推生效（方案 A + 2b）

> 日期：2026-11-01
> 适用模版：`tpl_1`「AI智能加仓策略」
> 关联代码：`node_client/signal_pl.py`、`node_client/strategy_runner.py`、`node_client/strategy_reconcile.py`、`node_client/strategy_precheck.py`、`backend/app/strategies.py`、`frontend/src/components/StrategyFormModal.vue`、`frontend/src/views/GroupSignalsView.vue`、`frontend/src/api/types.ts`
> 上游文档：`docs/AI智能加仓策略动态调参方案.md`（本方案是它的补充；上游 §12 关键代码位置索引需在实施后同步）
> 修订：2026-11-01 第 2 版（builder check 后重写 §3.2 的判据，删除配置事件标记机制）

---

## 1. 目标与非目标

### 1.1 目标

1. **改 `float_pl_ratio.ratio` 热推后必须生效**：节点按新比例重算共同止损价并写回持仓。
2. **越价时保守处理**：新算出的价已落在现价错误侧时，该侧**保留旧值**、不强平、发出告警。保存时已在 precheck 层拦截（400），节点层是第二道防线。
3. **手动分散仓成交后，止损热推仍然生效**（止盈仍听手动仓权威，两侧互不干扰）。
4. **回执说人话**：改分档止盈金额、改盈亏比、改开关，都要在逐字段回执里出现，标明实际生效与否。

### 1.2 非目标（本次明确不做）

| 不做 | 原因 |
|---|---|
| 关掉开关就**摘掉**持仓上已有的 SL / TP | 「摘单」需要 `modify_position_sl` 增加「显式清零」通道（当前 `tp=None` 语义是「沿用现值」），属方案 B |
| 让 `lot_pl_tiers.tiers[1..]` 生效 | 节点只取批次 1（`tier0_pl_amount` / `lot_pl_amount`）。批次 2+ 目前是死配置，本次只在回执与前端文案里**如实标注**，不改行为 |
| 按**固定价位**设置止盈止损（不按金额反推） | 属方案 C |
| 保存前在表单里预览「重算后的止损价 / 止盈价」 | 需要 precheck 回传计算结果并新增一次往返；本次不做，预检仍只用于「能不能存」 |

---

## 2. 现状诊断（根因）

### 2.1 止损热推不生效 —— 一处短路

`node_client/strategy_runner.py :: _lot_sltp_sync`：

```python
sl_amount = None
if ratio_on and opened:          # ← opened = 仓位构成变了
    balance = await self._read_balance()
    ...
    sl_amount = signal_pl.float_pl_loss_amount(self._signal_pl, balance)
```

`opened` 由 `signal_pl.book_has_open(self._lot_sltp_sig, sig)` 得出 —— **只有仓位构成变化（新开仓 / 加仓）才为真**。

热推改 `ratio` 时，仓位构成没变 → `opened=False` → `sl_amount=None` → `plan_common_stops` 算出的 `plan["sl"]` 为 `None` → `_lot_sltp_modify` 走：

```python
send_sl = float(sl) if sl is not None else _as_float(pos.get("sl"), 0.0)
```

**原样写回旧止损价**。用户改了比例，仓位上的止损纹丝不动。

`apply_strategy_detailed` 虽然置了 `_lot_sltp_dirty = True` 让下一拍重算，但重算时止损金额分支被短路，等于没算。

### 2.2 越价当前会市价强平

同一个函数里：

```python
for plan in plans:
    if plan.get("crossed"):
        await self._void_manual_pending(...)
        ok = await self._lot_sltp_market_close(plan, positions)   # ← 市价平掉整个方向
```

`plan["crossed"]` 是 `_cross_side(side, sl, tp, market)` 的结果，语义是「算出来的目标价已经落在现价错误侧」。

这条路径在**两种完全不同**的场景下都会触发，必须区分：

| 场景 | 含义 | 2b 下的处置 |
|---|---|---|
| **价格事件**：目标价已经写在持仓上，现价冲过它 | 止盈**正常达成**，必须平仓 | **保持现状**（`_lot_sltp_check_crossed` 路径） |
| **配置事件**：改了配置，新算出的价就在现价错误侧 | 配置错误 / 目标被调得离现价太近 | **不写这一侧、保留旧值、不强平、告警** |

现状没有区分，这是本次要引入的核心区分。

### 2.3 手动分散仓成交后，止损完全没人管

```python
if self._manual_authority_tp() is not None:
    # 手动仓的止盈价是本魔术号所有仓位的止盈权威：
    # 不再让分档手数盈亏那套反推的共同止盈来改单，只对齐到手动仓的 TP
    await self._align_positions_to_manual_tp(positions, event)
else:
    lot_result = await self._lot_sltp_sync(event, positions)
```

手动仓在场时走的是「只对齐止盈」的分支，**共同止损这条线整个被跳过**。

按 `docs/AI智能加仓策略动态调参方案.md` §6.3，手动分散仓是本任务的**最后一次加仓**、开完即收口。也就是说这个策略跑到最后阶段，止损热推是完全哑的 —— 而这恰恰是用户最需要调止损的阶段。

### 2.4 顺带发现的三个缺陷

**(a) 预检提示文案里「现价 0」**

`signal_pl.plan_common_stops` 产出的 plan **没有 `market` 键**，但 `strategy_precheck.describe_crossed` 去读它：

```python
market = _as_float(plan.get("market"))     # → 0.0
```

结果：保存被拦时用户看到的提示是「多单按新配置算出的止盈价 3120.45，现价 **0** 已经越过…」。数字是错的，会让人完全无法判断该往哪调。

**(b) 分档止盈金额的变更在回执里一个字都不出现**

`strategy_reconcile.py` 定义了 `PL_TIERS_FIELD = "lot_pl_tiers"`，但**全文件没有任何地方使用它**。`_pl_ratio_field` 只处理 `float_pl_ratio` 的 `ratio` 与 `max_times`。

后果：改「批次 1 的盈亏金额」—— 这是**真正决定共同止盈价的那个字段**——热推后回执是空的，用户完全看不到这次改动的结论。

**(c) `_pl_ratio_field` 提前 return，多字段同时改只报一个**

`_pl_ratio_field` 在 `max_times` 变化时 `return`，`ratio` 的结论就被吞掉。同时改开关 + 比例 + 次数，回执只有一行。

---

## 3. 设计

### 3.1 方案 A：止损重算与「仓位是否变化」解耦

**核心**：把「止损配置是否变了」和「仓位构成是否变了」这两个正交的触发条件分开。

新增运行态指纹 `self._lot_sltp_sl_cfg_sig`，记录**上一次写入止损时所用的止损配置**：

```python
# 止损配置指纹：(开关, 比例绝对值)。
# 比例或开关变了就要重算止损，与仓位构成是否变化无关。
# 余额不进指纹：余额变化不重算止损是既有口径（见 §6 风险表）。
self._lot_sltp_sl_cfg_sig: Optional[tuple] = None
```

止盈侧对称地加一个 `self._lot_sltp_tp_cfg_sig`（`(开关, 批次1金额, 批次1门槛, 平仓方向)`），
用途见 §3.2.1.1。

在 `_lot_sltp_sync` 里：

```python
sl_cfg_sig = (ratio_on, abs(ratio))
sl_cfg_changed = sl_cfg_sig != self._lot_sltp_sl_cfg_sig
...
sl_amount = None
if ratio_on and (opened or sl_cfg_changed):
    balance = await self._read_balance()
    ...
    sl_amount = signal_pl.float_pl_loss_amount(self._signal_pl, balance)
```

**指纹在哪里更新 —— 只有一处**：本轮重算**跑完且没有失败**之后，在 `_lot_sltp_sync` 内更新。

绝不能在 `apply_strategy_detailed` 里更新 —— 那样热推到达时指纹就被改成新值，等 `_lot_sltp_sync` 真正跑到时 `sl_cfg_changed` 恒为 `False`，修复会**静默失效**（回执还照样说「已按新比例重算」）。

**为什么不简单地用「热推后强制重算」**：`apply_strategy_detailed` 拿到的是整份快照，不知道改了哪些字段。无条件强制重算会让**每次**热推都重算止损（包括改档位间距这种与止损无关的改动），并且破坏 resume 时「存量仓不重算止损」的语义。配置指纹能精确命中。

**resume 语义**：`_lot_sltp_seed_existing` 存在的意义就是压制「重连后第一轮不重算存量仓的止损」。

- `suppress_open = self._lot_sltp_seed_existing and sig and not self._lot_sltp_sig`
- 处置：`sl_cfg_changed` 触发的重算**额外要求 `not suppress_open`**。
- 即：重连后的第一轮不重算止损（与现状一致）；此后若热推改了比例，则正常重算。

**指纹初始值**：设为 `None`，因此首次重算时 `sl_cfg_changed=True` → 会算止损。这与现状一致（现状首次开仓 `opened=True` 也会算），不改变行为。resume 场景由 `suppress_open` 单独压制。

### 3.2 方案 2b：越价时保留旧值，不强平

#### 3.2.1 判据：比「持仓上已有的值」，而不是「这是不是一次配置事件」

第 1 版设计用了一个「配置事件标记」`_lot_sltp_cfg_pushed`（热推改到盈亏配置就置位，重算完清零）。**builder check 指出它有生命周期漏洞**：`_lot_sltp_sync` 有多个提前 return（无持仓、配置全关、读不到合约规格、读不到余额、5 秒重试闸门）根本走不到 plan 循环，标记清不掉；标记若不清，则「首次开仓即越价」这类**价格事件**会被永久降级成「保留旧值」，正常止盈触发被废掉。

**改为无状态判据**。对每一个方向（BUY / SELL）、每一侧（sl / tp），在 plan 循环里比较三个事实：

| 事实 | 来源 |
|---|---|
| `new_crossed`：新算出的这一侧，是否已被现价越过 | `plan["crossed_sides"]` |
| `old_crossed`：持仓上**已有**的这一侧，是否已被现价越过 | 逐笔持仓的实际 `sl` / `tp` |
| `has_old`：持仓上这一侧**有没有**值 | 逐笔持仓的实际 `sl` / `tp` 是否 > 0 |

判定表：

| `new_crossed` | `old_crossed` | `has_old` | 结论 | 理由 |
|---|---|---|---|---|
| 否 | — | — | 写入新值 | 正常路径 |
| 是 | **是** | 是 | **市价平仓** | 价格事件：旧目标已被现价越过，券商侧的止损止盈本来就会打掉它。**保持现状** |
| 是 | 否 | **是** | **保留旧值 + 告警** | 配置事件：旧目标仍然有效（还没被越过），新目标不成立。2b 的核心 |
| 是 | 否 | 否 | **市价平仓** | 开仓即越价：没有旧值可保留，与现状一致 |

**这个判据为什么不需要「是否配置事件」**：「旧目标已被越过」本身就是价格事件的定义；「旧目标有效、新目标越界」本身就只能是配置改出来的。两者天然区分开，无需任何标记。

**重复执行是安全的**：保留旧值后，持仓上的旧值不变，`old_crossed` 仍为 `False` → 下一轮（若因加仓再次进 plan 循环）仍然判「保留旧值」→ 不会因为「重复触发」而误强平。等价格真的越过旧目标时，`old_crossed` 变 `True` → 市价平仓 —— 这是正确的止损行为。

**不依赖 `config_version` 或任何跨拍状态**。

#### 3.2.1.1 2b 只针对「配置改动」，不加仓

实施时发现的一个必须补的口径：上面的判定表是**纯函数**的——只看持仓上的旧值，
不看这一轮是「配置改动」还是「加仓」。但这两者的语义不同：

| 场景 | 越价时该做什么 | 原因 |
|---|---|---|
| **配置改动**（热推改比例 / 金额） | 保留旧值 + 告警 | 目标被调得离现价太近，是配置问题 |
| **加仓后重算** | 市价平仓（既有口径不变） | 加仓后重算出的止盈落到现价之下，说明行情已经走过去了——这是价格事件的延伸，不是配置问题 |

不加这个区分的话，加仓路径会被误伤：加仓后重算出的止盈价格落在现价错误一侧时，
本该平仓却变成「保留旧止盈」，等于把止盈废了。

处置：新增两个配置指纹（止损 `(开关, |比例|)`、止盈 `(开关, 批次1金额, 批次1门槛, 平仓方向)`），
`allow_keep = 止损指纹变了 or 止盈指纹变了`。指纹变了 → 走 2b；只有加仓 → 走原有平仓逻辑。

这个区分与「纯函数判据」不冲突：指纹只决定**要不要**允许 2b，具体**怎么处置**
（保留 vs 平仓）仍然由「持仓上的旧值」决定。

#### 3.2.2 「保留旧值」怎么表达

`plan["sl"] = None` / `plan["tp"] = None` 即「沿用持仓上的现值」—— 这是现成的语义：

```python
# _lot_sltp_modify
send_sl = float(sl) if sl is not None else _as_float(pos.get("sl"), 0.0)
send_tp = float(tp) if tp is not None else None
```

且 `modify_position_sl(ticket, sl, tp=None)` 的 `tp=None` 明确是「沿用持仓上的现值」（`mt5_client.py` 有注释）。

`stops_match(pos, None, None)` 返回 `True` → 两侧都被保留时该笔持仓直接跳过，不会发出无意义的改单。

**逐笔处理**：某一笔持仓这一侧本来就没有值（`sl == 0`）而新值越价 → 「没有旧值可保留」→ 它会拿到 `sl = 0`（等于没有止损）。

**这是有意的取舍**：该仓位保持「无止损」现状，并**在告警里点名**（见 §3.2.5）。理由：券商本来就拒收落在现价错误侧的止损，写进去只会拿到一次拒单；把它平掉则超出了「改参数」的语义。保存时的 precheck 400 已经拦住了绝大多数此类情况（见 §3.2.6）。

#### 3.2.3 落价记录必须跟着改

`self._lot_sltp_levels[side] = (sl, tp, digits)` 是给**价格事件**路径（`_lot_sltp_check_crossed`）用的：判断「现价是否冲过了已经落地的目标价」。

2b 下这一侧被拒绝了，**没有写进券商**。如果 `levels` 里记的是新算出来的（被拒的）价，价格事件路径会用错误的价去判「冲过了」→ 市价平仓。

处置：被保留的那一侧，`levels` 里记**持仓上的实际值**（券商侧真正的目标价）。

**同一方向多笔持仓的实际值可能不同**（首单在开仓时写、后续加仓各写各的；`_align_positions_to_manual_tp` 只动止盈）。`levels` 按方向只有一个值，因此取**最容易先被触发**的那个（保守）：

- BUY：`tp = min(各笔 tp)`、`sl = max(各笔 sl)`
- SELL：`tp = max(各笔 tp)`、`sl = min(各笔 sl)`

（`cross_level` 的判定是「现价越过该值」，取最容易被越过的那一个才能第一时间发现。）

正常写入路径下所有持仓被写成同一个共同价，取极值无差别。

#### 3.2.4 两侧要分别判

`_cross_side` 只能返回一个越价侧（TP 优先）。但 2b 下两侧要**分别**决定留还是不留 —— 例如新 TP 越价（保留旧 TP）但新 SL 正常（应该写入）。

在 `signal_pl.py` 新增：

```python
def crossed_sides(side, sl, tp, market) -> set[str]:
    """两侧各自是否已被现价越过。返回 {"tp"} / {"sl"} / {"tp","sl"} / 空集。"""
```

`_cross_side` 改为基于它实现（保持「TP 优先」的既有语义，向后兼容）。

`plan_common_stops` 的 plan 增补两个键：

```python
"crossed_sides": [...],   # 新增：两侧各自是否越价
"market": market,         # 新增：修 §2.4(a) 的「现价 0」
```

`"crossed"` 键保留（`describe_lot_stops` 与既有测试在用）。

#### 3.2.5 告警从哪来

「保留旧值」这个结论是在**下一拍的重算里**得出的，而热推回执（`strategy_update_ack`）在 `apply_strategy_detailed` 里**同步**就发走了 —— 回执里放不下这个结论。

所以：

- **保存时**（同步、用户可见）：precheck 已经 400 拦截 —— 这是用户能立刻看到的反馈。
- **节点侧**（异步、事后可见）：发一条 `signal_pl` 进度事件，`detail` 里带 `status: "kept"`，消息写清「新的止损价 X 已在现价 Y 之外，本次未写入，保留旧止损价 Z」。

这个分工与现状一致：`_lot_sltp_fail` 也是走进度事件报错的。

**`detail` 的形状**：复用现有的 `signal_pl.lot_stops_detail(plan)`，在其上增加：

```python
"status": "kept",          # "written" | "kept" | "crossed_closed"
"kept_side": "sl",         # 哪一侧被保留
"kept_reason": "新的止损价 4060 已在现价 4050 之外，本次未写入，保留旧止损价 4000",
"market": 4050.0,
```

`status` 取值：

| 值 | 含义 |
|---|---|
| `written` | 新值已写入（正常） |
| `kept` | 该侧越价被拒，保留旧值（2b） |
| `crossed_closed` | 价格事件，市价平仓 |

**前端渲染**：`GroupSignalsView.vue` 新增 `detail.kind === 'signal_pl'` 分支，按 `status` 显示「✅ 已写入 / ⚠️ 保留旧值 / 🔒 已平仓」。

> 注：节点侧本来就有两处产出 `kind: "signal_pl"` 的 detail（`lot_stops_detail` 与 `trigger_detail`），键名不重叠。前端分支按「键存在则显示」的方式兼容两种形状。`api/types.ts` 的 `GroupTaskEventDetail.kind` 联合类型缺 `'signal_pl'`，需补上。

#### 3.2.6 预检与运行时必须一致（重要）

`strategy_precheck.check_sltp_crossed` 现在**总是**检查共同止盈。但 §3.3 之后，手动仓在场时节点**根本不会写共同止盈**（只对齐手动仓的止盈）。

不修的话：手动仓成交后（也就是加仓收口、用户最需要调止损的阶段），precheck 仍会拿共同止盈去判越价并 400 拦截用户 —— 拦的是一个**永远不会被写入**的目标价。

处置：**precheck 镜像运行时的实际行为**。

- 节点侧 `precheck_strategy` 把「手动止盈权威是否在场」告诉 `check_sltp_crossed`（新增参数 `manual_authority: bool`）。
- `manual_authority=True` 时**只检查止损那一侧**（共同止盈根本不写，不该拦）；止损仍要检查（§3.3 之后止损照常写）。
- 实现上等价于：此时 `tp_amount=None` → `plan["tp"]` 恒为 `None` → 只有 `sl` 可能越价。
- `manual_authority=True` 且盈亏比也未启用 → 两侧都不写 → 无需检查 → 直接放行。

后端 `strategies.py` 无需改动（预检请求本来就发到节点，由节点判定）。

### 3.3 手动分散仓在场时的止损同步

把「写共同止损」从 `_lot_sltp_sync` 里拆出来，成为可单独调用的部分。

拆法：

- `_lot_sltp_sync(event, positions)` —— 保持现有职责（TP + SL + 越价处置 + 落价记录）。
- 新增 `_lot_sltp_sync_sl(event, positions)` —— **只算、只写止损**：算 plan 时 `tp_amount=None`（因此 `plan["tp"]` 恒为 `None`，不会碰止盈），止损金额照常算；越价按 §3.2.1 的判定表处置；**不写 `_lot_sltp_levels` 的 tp**。

调用点：

```python
if self._manual_authority_tp() is not None:
    await self._align_positions_to_manual_tp(positions, event)     # 止盈：手动仓权威
    await self._lot_sltp_sync_sl(event, positions)                 # 止损：仍然按配置算
else:
    lot_result = await self._lot_sltp_sync(event, positions)
```

要点：

- **止损与止盈互不干扰**：手动仓权威只管止盈，止损始终来自 `float_pl_ratio`。这不违反「手动仓止盈是权威」—— 权威说的是止盈。
- `_lot_sltp_sync_sl` 内部的持仓过滤与 `_lot_sltp_sync` 保持一致（`manual_scatter.exclude_manual` + `filter_lot_side`）—— 手动仓自身的仓位不参与共同止损的计算，与现状一致。
- **触发时机**：`_lot_sltp_sync_sl` 自带节流 —— 只在「止损配置指纹变了」或「持仓构成变了」时才真正计算（否则每 tick 都读一次账户余额）。`stops_match` 进一步抑制无变化的写单。
- **不写 `_lot_sltp_levels`**：手动分支不走 `_lot_sltp_check_crossed`（那是价格事件路径），写了也没人读，是死状态。副作用：手动分支里没有「现价越过止损 → 市价平仓」的兜底 —— 但止损已经成功写进券商，券商的止损单会自己打掉这笔仓，不需要节点兜底。见 §6 风险表。

### 3.4 回执：补齐信号盈亏相关的字段结论

`strategy_reconcile.py` 改动：

**`_pl_ratio_field` 改为返回列表**（修 §2.4(c) 的提前 return），逐字段给结论：

| 字段 | 结论消息要点 |
|---|---|
| `enabled` | 开关变化；**并说明关闭后已有持仓的止损价保持不变（不会自动摘除）** |
| `ratio` | 已有；补上「共同止损价已按新比例重算」 |
| `max_times` | 已有（次数不复活） |

**新增 `_pl_tiers_field`**，同样返回列表：

| 字段 | 结论消息要点 |
|---|---|
| `enabled` | 开关变化；**并说明关闭后已有持仓的止盈价保持不变（不会自动摘除）** |
| `tiers[0].pl_amount` | 批次 1 盈亏金额变化 —— 这是**实际生效的**止盈金额 |
| `tiers[0].min_lot` | 批次 1 手数门槛变化 —— 影响「总手数达到多少才写止盈」 |
| `close_action` | 平仓方向变化 —— 影响改单覆盖哪些方向 |
| `tiers[1..]` | **明确标注「当前版本只有批次 1 参与共同止盈计算，这些批次暂不生效」** |

**去重**：信号盈亏配置在逆势 / 顺势两条规则上写的是同一份。回执里**只报一次**，字段名沿用既有格式（不含规则下标），如 `rules[float_pl_ratio].ratio`、`rules[lot_pl_tiers].tiers[0].pl_amount`。

**`kept` 状态不进入 reconcile**：reconcile 是纯配置比对，没有行情数据，判不出「越价」。`kept` 只由运行时产生（§3.2.5 的进度事件），因此：

- `summarize()` **不新增** `kept` 分支（那会是死代码）。
- 前端 `strategyUpdateFieldLabel` **不新增** `kept`（那个映射只用于热推回执的字段状态）。
- `kept` 的渲染放在新增的 `signal_pl` 详情分支里（§3.2.5）。

### 3.5 预检提示文案修正

`signal_pl.plan_common_stops` 的 plan 补 `"market": market`（§3.2.4）。这一处修完，`strategy_precheck.describe_crossed` 里那句「现价 {}」就会显示真实价格。

**顺带**：`describe_crossed` 目前只按 `crossed` 字段区分止盈 / 止损。两侧同时越价时应把两侧都写出来。改为读 `crossed_sides`。

### 3.6 前端

| 位置 | 改动 |
|---|---|
| `StrategyFormModal.vue :: FIELD_HELP.signal_pl` | 「止损只在新开仓时重算」→ 改为「止损在**新开仓**与**配置热推**后重算；报价波动不重算」 |
| `StrategyFormModal.vue :: FIELD_HELP.signal_float_pl` | 「只在新开一单时重算。」→ 同步更新 |
| `StrategyFormModal.vue :: FIELD_HELP.signal_lot_pl` | 补一句「更高批次当前不参与共同止盈计算」 |
| `StrategyFormModal.vue` 信号盈亏控制区块 | 盈亏比 / 分档金额旁加一行 muted 说明，标注**实际生效的是批次 1**、**关闭开关不会摘掉已有止盈止损** |
| `GroupSignalsView.vue :: detailRows` | 新增 `detail.kind === 'signal_pl'` 分支：方向 / 止盈 / 止损 / 现价 / `status` 文案 / `kept_reason` |
| `GroupSignalsView.vue :: eventTag` | 新增 `signal_pl: { cls: 'blue', text: '止盈止损' }`（注意函数名是 `eventTag`，不是 `eventLabel`） |
| `api/types.ts` | `GroupTaskEventDetail.kind` 联合类型补 `'signal_pl'`；补 `status` / `kept_side` / `kept_reason` / `market` 字段 |

### 3.7 端到端时序

```
用户改 ratio
  │
  ├─ 保存：backend PATCH
  │    ├─ check_limit_below_added（库内兜底）→ 400
  │    └─ _precheck_running_tasks → 节点 strategy_precheck
  │         └─ check_sltp_crossed：
  │              ├─ 手动止盈权威在场？→ 只查止损那一侧（§3.2.6）
  │              └─ 新算的 SL/TP 越过现价？
  │                   ├─ 越过 → 400 {error: "sltp_crossed", blocks: [...]}
  │                   │         （文案里的「现价」此时已是真实价格）
  │                   └─ 没越过 → 放行
  │
  ├─ 落库 + config_version++ + rewrite_running_strategy_snapshots
  │
  └─ WS strategy_update
       └─ node apply_strategy_detailed
            ├─ reconcile → 逐字段结论（applied / locked / ...）
            ├─ 置 _lot_sltp_dirty = True
            └─ 回 strategy_update_ack
                 │   （此时还没算价，不知道会不会越价）
                 └─ 下一拍 TICK/IDLE → _lot_sltp_sync
                      ├─ sl_cfg_changed（指纹）→ 重算止损 ← 方案 A 的修复点
                      ├─ 逐侧判定（§3.2.1 判定表）
                      │    ├─ 新值没越价        → 写入
                      │    ├─ 旧值已被越过      → 市价平仓（价格事件）
                      │    ├─ 旧值有效 + 新值越价 → 保留旧值 + 告警（2b）
                      │    └─ 没有旧值 + 新价越价 → 市价平仓（开仓即越价）
                      ├─ 更新指纹（仅在本轮无失败时）
                      └─ 手动仓在场 → _align_positions_to_manual_tp
                                     + _lot_sltp_sync_sl（只写止损）
```

---

## 4. 影响面

| 层 | 文件 | 改动性质 |
|---|---|---|
| 节点 | `node_client/signal_pl.py` | 新增 `crossed_sides`；plan 增补 `crossed_sides` / `market`；`_cross_side` 基于新函数实现；`lot_stops_detail` 增 `status` / `kept_side` / `kept_reason` |
| 节点 | `node_client/strategy_runner.py` | 新增 `_lot_sltp_sl_cfg_sig`；改 `_lot_sltp_sync` 的止损分支与逐侧越价处置；新增 `_lot_sltp_sync_sl`；落价记录取极值 |
| 节点 | `node_client/strategy_reconcile.py` | `_pl_ratio_field` 改返回列表；新增 `_pl_tiers_field`；去重 |
| 节点 | `node_client/strategy_precheck.py` | `describe_crossed` 读 `crossed_sides`；`check_sltp_crossed` 增 `manual_authority` 参数 |
| 后端 | 无逻辑改动 | 预检链路已就位（`strategies.py :: _precheck_running_tasks`），仅受益于文案修正与 precheck 侧的对称修正（后者在节点侧） |
| 前端 | `StrategyFormModal.vue` | 3 处 help 文案 + 1 处区块说明 |
| 前端 | `GroupSignalsView.vue` | `signal_pl` 详情分支 + 事件标签 |
| 前端 | `api/types.ts` | `kind` 联合类型补 `'signal_pl'`；补 `status` / `kept_side` / `kept_reason` / `market` |

---

## 5. 测试计划

### 5.1 节点侧新增（`node_client/test_strategy_runner.py`）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 热推改 `ratio`，仓位构成不变 | 持仓 `sl` 按新比例重算 |
| 2 | 热推前后 `sl` 距离之比 | 与「比例变化倍数」一致 |
| 3 | 热推改与止损无关的字段（`batch_levels` 间距） | 持仓 `sl` **不变**（指纹未命中） |
| 4 | 新开仓 / 加仓后止损仍按当前比例重算 | 与用例 1 互不干扰（`opened` 路径仍有效） |
| 5 | 热推把止损比例调大 → 新止损价落在现价错误侧，且旧止损价仍有效 | **不强平**、持仓 `sl` 保留旧值、发 `signal_pl` 告警且 `detail.status == "kept"` |
| 6 | 同上，但**旧止损价也已被现价越过** | **市价平仓**（价格事件，回归现状） |
| 7 | 热推后**再触发一次加仓**（重回 plan 循环） | 仍然保留旧值，**不**误强平 |
| 8 | 首次开仓即越价（没有旧值可保留） | 市价平仓（回归现状） |
| 9 | 手动分散仓成交后热推改 `ratio` | 持仓 `sl` 按新比例重算；`tp` 仍等于手动仓止盈价 |
| 10 | 手动仓在场时止损越价 | 保留旧止损、告警、**不动止盈** |
| 11 | 关闭 `float_pl_ratio.enabled` | 持仓 `sl` 保留旧值（不摘除） |
| 12 | 关闭 `lot_pl_tiers.enabled` | 持仓 `tp` 保留旧值 |
| 13 | 新 TP 越价但新 SL 正常 | `tp` 保留旧值、`sl` 已更新 |
| 14 | 越价后价格事件路径用持仓实际价 | `_lot_sltp_levels` 记的是旧值，不会因被拒的新价而误平仓 |
| 15 | 某笔持仓这一侧本来就没有值 | 该笔保持无止损；告警文案点名说明 |
| 16 | 同一方向多笔持仓止盈价不同 | `_lot_sltp_levels` 取「最容易先被触发」的那个（BUY 取最小 tp） |
| 17 | **加仓后重算的止盈落在现价错误一侧** | 仍市价平仓（2b 只针对配置改动，不改加仓口径） |
| 18 | **热推改批次1金额** | 共同止盈按新金额重算 |
| 19 | 热推改止盈配置后止盈越价 | 保留旧止盈 + 告警，止损照常写 |

### 5.2 节点侧新增（`node_client/test_signal_pl.py` / `test_strategy_precheck.py`）

| # | 用例 | 断言 |
|---|---|---|
| 17 | `crossed_sides` 两侧同时越价 | 返回 `{"tp","sl"}` |
| 18 | `plan_common_stops` 产出带 `market` | `plan["market"]` 等于传入的现价 |
| 19 | `describe_crossed` 的文案 | 含真实现价，不含「现价 0」 |
| 20 | 两侧同时越价时的文案 | 止盈与止损都写出来 |
| 21 | `check_sltp_crossed(manual_authority=True)` | 只报止损侧的阻断项，止盈越价不阻断 |

### 5.3 节点侧新增（`node_client/test_strategy_reconcile.py`）

| # | 用例 | 断言 |
|---|---|---|
| 22 | 改 `lot_pl_tiers.tiers[0].pl_amount` | 回执出现该字段，status=applied |
| 23 | 改 `lot_pl_tiers.tiers[1].pl_amount` | 回执出现该字段，**消息标明「当前版本不生效」** |
| 24 | 改 `float_pl_ratio.enabled` | 回执出现该字段，消息含「不会自动摘除」 |
| 25 | 同时改 `enabled` + `ratio` + `max_times` | 回执出现**三条**（不再被提前 return 吞掉） |
| 26 | 改 `lot_pl_tiers.close_action` / `min_lot` | 回执各自出现 |
| 27 | 逆势 / 顺势两条规则带同一份盈亏配置 | 回执**只报一次**（去重） |

### 5.4 回归（不改期望值，只确认不破）

- `test_signal_lot_pl_crossed_price_market_closes` —— 价格事件的越价强平必须保留（用例 6 同理）。
- `test_signal_stop_recalculates_on_open_not_on_close` —— 「平仓不重算止损」的既有语义保留。
- `test_signal_float_pl_writes_stop_not_market_close` —— 浮亏本身不平仓。
- `test_signal_lot_pl_writes_batch1_stops_and_ignores_price_ticks` —— 报价波动不平仓。
- `test_signal_take_profit_starts_only_after_min_lot` —— 手数门槛语义不变。
- `test_hot_push_keeps_consumed_times` / `test_hot_push_switching_to_times_restarts_count` —— 次数不复活。
- 全部 `node_client` / `backend` 单测（当前 679 + 881 项）。

### 5.5 前端

- `npm run typecheck` 通过（0 错误）。

---

## 6. 风险与已知限制

| 风险 / 限制 | 说明 | 缓解 |
|---|---|---|
| **热推到生效有最多 5 秒延迟** | 热推置 `_lot_sltp_dirty` 后走 `WAKE` 分支（`WAKE` 只做逆势限价重同步并 `return False`），止损重算发生在**下一条 TICK/IDLE** 事件。行情活跃时 ~0.5s，行情静止时最多 5s（`idle_interval`） | 可接受；回执措辞用「下一拍生效」而非「立即生效」 |
| **余额变化不触发止损重算** | `sl_amount = \|ratio\| × balance / 100`，但余额不进配置指纹 | 保持既有口径；未来可单独做 |
| **2b 的「整条改动拒绝」只在保存时做** | 节点侧不能整条拒绝热推 —— 否则库里是新配置、节点跑旧配置，多节点会分叉 | 节点侧降级为「该侧保留旧值 + 告警」，与 MT5 券商拒单行为一致 |
| **越价告警是事后事件，不是回执** | 回执在 `apply_strategy_detailed` 同步发出，那时还没算价 | 保存时的 precheck 400 才是同步反馈；告警走 `signal_pl` 进度事件 |
| **某笔持仓本来没有止损，越价时保持无止损** | 「没有旧值可保留」 | 告警文案点名说明；保存时 precheck 已拦住绝大多数此类情况 |
| **手动分支没有「现价越过止损 → 市价平仓」的兜底** | `_lot_sltp_check_crossed` 不在手动分支 | 止损已成功写进券商，券商止损单会自行触发；节点兜底只用于「券商拒收」的情况 |
| **「关开关不摘单」仍是已知限制** | 关闭 `float_pl_ratio` 后持仓上的止损价保留 | 回执与前端文案如实写明；摘单属方案 B |
| **批次 2+ 仍是死配置** | 节点只取批次 1 | 回执与前端文案如实标注；不在本次改变行为 |
| **多节点场景的余额差异** | 各节点读自己的账户余额，止损金额可能不同 | 属既有特性（账户级盈亏比本就如此） |

---

## 7. 实施顺序

| 阶段 | 内容 | 独立可上线 |
|---|---|---|
| **P1** | `signal_pl.crossed_sides` + plan 增补 `crossed_sides` / `market`；`describe_crossed` 两侧文案 | ✅ 纯修文案与工具函数，零行为变化 |
| **P2** | 方案 A：止损配置指纹 + `_lot_sltp_sync` 的止损重算分支（**含 resume 压制**） | ✅ 核心修复，独立可上线 |
| **P3** | 2b：逐侧判定 + 保留旧值 + 落价记录取极值 + `kept` 告警事件 | ✅ 与 P2 同批上线（两者共用 `_lot_sltp_sync` 的改动） |
| **P4** | 手动仓在场时的 `_lot_sltp_sync_sl`；**同时**把 `check_sltp_crossed` 的 `manual_authority` 参数补上（§3.2.6，两者必须同批） | ✅ 必须成对上线，否则手动仓阶段会出现误拦 |
| **P5** | 回执补齐（`_pl_tiers_field` / 列表化 / 去重） | ✅ 独立可上线 |
| **P6** | 前端：help 文案、区块说明、`signal_pl` 详情分支、事件标签、`api/types.ts` | ✅ 独立可上线 |

---

## 8. Builder check 的问题与处置记录

| # | 问题 | 处置 |
|---|---|---|
| 1 | precheck 仍检查共同止盈，但手动仓在场时它根本不会被写入 → 误拦 | **已解决**（§3.2.6）：precheck 增 `manual_authority`，与运行时对齐 |
| 2 | 指纹更新位置自相矛盾（§3.1 vs §3.7）→ 会让修复静默失效 | **已解决**（§3.1）：只在 `_lot_sltp_sync` 跑完且无失败时更新；时序图已改 |
| 3 | 重连后第一轮会不会因指纹为 `None` 而重算存量仓止损 | **已解决**（§3.1）：`sl_cfg_changed` 额外要求 `not suppress_open` |
| 4 | 配置推送当轮新开的仓位，「保留旧值」= 没有止损 | **已解决**（§3.2.2）：有意保留现状 + 告警点名；列为已知限制 |
| 5 | `kept` 是死代码（reconcile 产不出） | **已解决**（§3.4 / §3.2.5）：`kept` 只走运行时 `signal_pl` 事件；`summarize` 与 `strategyUpdateFieldLabel` 均不加 `kept` |
| 6 | `_lot_sltp_sync_sl` 何时触发；谁读手动分支的 `_lot_sltp_levels` | **已解决**（§3.3）：自带指纹 + 持仓签名节流；手动分支不写 `_lot_sltp_levels`（无人读） |
| 7 | reconcile 出一个合并结论还是逐字段；两条规则是否重复报 | **已解决**（§3.4）：逐字段列表 + 去重（只报一次） |
| 8 | 同一方向多笔持仓的实际值不同，`levels` 记哪个 | **已解决**（§3.2.3）：取「最容易先被触发」的极值 |
| 9 | 券商拒单（非越价）算不算「保留旧值」 | **已解决**：不算。券商拒单仍走现有的 `error` + 5 秒重试路径；**重试期间不更新指纹**，所以改动会被继续重试 |
| 10 | 配置事件标记的生命周期漏洞 | **已解决**（§3.2.1）：删除标记机制，改为无状态的「比持仓上已有的值」判据 |

> 另修正：前端函数名是 `eventTag`（不是 `eventLabel`）；`api/types.ts` 的 `kind` 联合类型缺 `'signal_pl'`；节点侧已有两处 `kind: "signal_pl"` 的不同形状 detail，前端分支需兼容。

---

## 9. 关键代码位置索引（实施后更新）

| 主题 | 位置 |
|---|---|
| 止损配置指纹 | `node_client/strategy_runner.py :: _lot_sltp_sl_cfg_sig`（属性）/ `_sl_cfg_sig`（方法） |
| 止盈配置指纹 | `node_client/strategy_runner.py :: _lot_sltp_tp_cfg_sig` / `signal_pl.py :: lot_pl_cfg_sig` |
| 止损重算分支 | `node_client/strategy_runner.py :: _lot_sltp_sync`（`sl_cfg_changed`） |
| 2b 逐侧判定 | `node_client/strategy_runner.py :: _lot_sltp_apply_plans` + `signal_pl.py :: stop_side_decision` |
| 只写止损 | `node_client/strategy_runner.py :: _lot_sltp_sync_sl` |
| 两侧越价判定 | `node_client/signal_pl.py :: crossed_sides` / `side_crossed` |
| plan 补 market / crossed_sides | `node_client/signal_pl.py :: plan_common_stops` |
| 回执补齐 | `node_client/strategy_reconcile.py :: _pl_tiers_fields` / `_pl_ratio_fields` |
| 预检文案与对称修正 | `node_client/strategy_precheck.py :: describe_crossed` / `check_sltp_crossed`（`manual_authority`） |
| 告警事件 | `node_client/signal_pl.py :: lot_stops_detail`（`status` / `kept_side` / `kept_reason` / `new_sl` / `new_tp`） |
| 新增测试 | `node_client/test_signal_pl_hot_push.py`（19 条） |
