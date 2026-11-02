# 指定价档位改造方案：指定价加仓按「手动单同构」执行

> 日期：2026-11-02
> 适用模版：`tpl_1`「AI智能加仓策略」（规则 type=1 逆势加仓 / type=2 顺势加仓）
> 关联代码：`node_client/strategy_runner.py`、`node_client/strategy_rules.py`、`node_client/signal_pl.py`、`node_client/manual_scatter.py`、`node_client/strategy_reconcile.py`、`backend/app/models.py`、`backend/app/strategy_templates.py`、`frontend/src/components/StrategyFormModal.vue`、`frontend/src/api/types.ts`
> 关联文档：`docs/superpowers/specs/2026-11-02-hot-push-always-apply-design.md`（热推无条件采纳方案，见 §8 交叉点）

---

## 0. 一句话

> **「指定价」不再是一种间距口径，而是一笔有独立入场价、独立自有止损的加仓单：成交价正好等于指定价，出场随大部队阶梯止盈，止损有「自己的」和「大部队的」两道（取更近的），并且它只是加仓批次中的一环，不是最后一仓。**

---

## 1. 背景与现状

### 1.1 现状：指定价只是「间距的一种计算口径」

`calc_type=price` 在 `node_client/strategy_rules.py :: resolve_threshold` 里被折算成偏离点数：

- 逆势：`counter_limit_price` 取绝对价 → 预先挂一张限价（`strategy_runner.py :: _place_counter_limit`）
- 顺势：到价后市价成交（成交价会滑点，不等于指定价）

它**没有**自己的止盈 / 止损语义，全部跟着共同止盈止损走，且**不带任何自有止损**。

### 1.2 现状：手动单（`manual_scatter`）才是真正的「有自己 TP/SL 的加仓」

`node_client/manual_scatter.py`：

- 挂限价/止损单，成交价**正好等于**配置入场价（`pending_kind` 按现价在入场价哪一侧选单型）
- 自带止损（`sl_is_sendable` 校验方向与券商 `stops_level`）
- 成交后它的 TP 成为**全魔术号止盈权威**（`_align_positions_to_manual_tp`）
- 它是**最后一次加仓**（`add_closed_rule_types` → `ctx.add_closed` → 该规则不再加仓）

### 1.3 目标形态

| 维度 | 指定价档位（目标） | 手动分散仓（现状） |
|---|---|---|
| 入场 | 挂单，成交价 = 指定价 | 挂单，成交价 = 配置入场价 |
| 止盈 | **跟大部队阶梯止盈**（tier1） | 自己是权威，全魔术号对齐它 |
| 止损 | 自有止损 + 大部队共同止损，**取更近的** | 只有自己的 |
| 批次位置 | **批次中的一环**，不是最后一仓 | **最后一仓**（收口） |
| 手数 | 按 tier1 金额反推 | 按 tier1 金额反推 |
| 数量 | 一条规则可配多个档位 | 顺势/逆势各一条 |

---

## 2. 已拍板的决策（实现时不再讨论）

| # | 决策 | 落地含义 |
|---|---|---|
| 1 | 指定价的「止盈点」= **算手数的参考价** | 字段 `price_tp` 只参与手数反推与每手止盈距离；出场一律走大部队阶梯止盈。**不引入** `tp_mode` |
| 2 | 双止损冲突「**取更近的**」 | 指定价仓的 SL = `own_sl` 与 `common_sl` 中离现价更近者（多单取更高、空单取更低） |
| 3 | **顺势的指定价不用改** | 顺势档位保持现状（到价市价成交 + 原手数公式）。新逻辑只作用于逆势规则的指定价档位 |
| 4 | 手数按**阶梯止盈 1 金额反推** | `volume = (|tier1金额| − 存量到 price_tp 的预估盈亏) ÷ 每手止盈(entry→price_tp)`，复用 `manual_scatter.compute_volume` |
| 5 | 自有止损触发后按**新加权均价 + tier1 金额重算 TP/SL** | 新增「仓位减少也要重算 SL 金额」触发条件；TP 随加权均价移动 |
| 6 | 自有止损触发**不收口** | 只走掉一笔，剩余继续按新 TP/SL 跑 |
| 7 | 一条规则**允许多个**指定价档位 | 配置层支持多档；运行时同一规则同时最多一张在途挂单 |
| 8 | 指定价加仓**不是最后一仓** | 不置 `add_closed`；`total_lot_limit` 计入这一笔 |

---

## 3. 配置模型

### 3.1 字段

在 `StrategyBatchLevel`（后端 API 模型）/ `BatchLevel`（后端落库模板）/ `frontend/src/api/types.ts` 同步新增 2 个字段，`price` 语义升级：

| 字段 | 类型 | 含义 | 约束 |
|---|---|---|---|
| `price`（已有） | float | 指定价档位的**入场价**（绝对价位） | > 0 才算指定价档位 |
| `price_tp` | float | **止盈参考价**（只用于反推手数与每手止盈距离） | **必填 > 0**，否则该档不可用并回执说明 |
| `price_sl` | float | 该档**自有止损**（绝对价位） | 0 = 不设；必须落在入场价反方向 |

**不做**：`tp_mode`（决策 1）、`price_volume`（手数一律反推）。

### 3.2 归一化规则（`backend/app/strategy_templates.py :: normalize_rule`）

- `calc_type != 'price'` → 丢弃 `price_tp` / `price_sl`（与其它类型字段「按 type 只保留该类型字段」的既有口径一致）
- `calc_type == 'price'` 且 `price <= 0` → 该档不是指定价档位（退化为「无效档」，不参与判定）

### 3.3 前端表单（`frontend/src/components/StrategyFormModal.vue`）

`calc_type === 'price'` 分支下增加两个输入框：

| 输入 | 绑定 | 说明文案要点 |
|---|---|---|
| 止盈参考价 | `lv.price_tp` | 「**只用于反推这一档的手数**（按阶梯止盈 1 的目标金额）；这一档成交后**不单独止盈**，出场跟随大部队的阶梯止盈」 |
| 自有止损 | `lv.price_sl` | 「这一档自己的止损。成交后锁定不可改。与大部队的共同止损同时存在，**离现价更近的那道生效**」 |

改写 `FIELD_HELP.batch_price`（现文案是「本档的指定价位（绝对价格）。逆势时多单需低于参考价、空单需高于参考价」），补上：成交价正好等于指定价、止盈参考价的用途、自有止损的语义、顺势不做这些（决策 3）。

---

## 4. 节点侧设计

### 4.1 档位识别与取参（`node_client/strategy_rules.py`，纯函数）

```python
def is_price_level(level: dict) -> bool:
    """这一档是不是「指定价档位」：calc_type=price 且入场价 > 0。"""

def price_level_params(level: dict) -> dict:
    """指定价档位的出场参数：入场价 / 止盈参考价 / 自有止损。"""
```

`resolve_threshold` 的 price 分支**不再返回可用于偏离判定的阈值**（返回 `Threshold(0.0, CALC_PRICE, target_price=entry)`），保证「指定价档位永远不会以市价触发」。

### 4.2 挂单计划

新增计划对象（在 `strategy_rules.py` 或 `manual_scatter.py` 均可，倾向 `strategy_rules.py` 与 `AddDecision` 同居）：

```python
@dataclass
class PriceLevelPlan:
    rule_type: int
    level_index: int
    action: str            # BUY / SELL
    entry: float           # 入场价（= level.price）
    tp_ref: float          # 止盈参考价（= level.price_tp）
    own_sl: float          # 自有止损（= level.price_sl，0=不设）
    volume: float
    limit_kind: str        # "limit" | "stop"（由现价相对入场价决定）
    comment: str           # R1L2X
    direction: str
    base_price: float
    price: float           # 现价
    point: float
    base_volume: float
    lot_times: float
    extra_lot: float
    add_count: int
    limit_kind_name: str   # total_lot_limit / max_allow_num
    limit_value: int
```

改写点：`strategy_runner.py :: _sync_counter_limit` 里，`plan_counter_limit` 之后判定命中的档位是否为 `is_price_level` → 是则转成 `PriceLevelPlan`。

**手数反推（决策 4）** —— 复用 `manual_scatter.compute_volume`，不新写公式：

```python
volume = manual_scatter.compute_volume(
    cfg={"entry_price": entry, "take_profit": tp_ref,
         "volume": 0.0, "volume_locked": False},
    current_lot_size=manual_scatter.current_lot(book),
    lot_pl_tiers=rule.get("lot_pl_tiers"),
    tick_size=spec["tick_size"], tick_value=spec["tick_value"],
    volume_step=spec["volume_step"], volume_min=spec["volume_min"],
    volume_max=spec["volume_max"],
    projected_pl=manual_scatter.projected_profit(
        manual_scatter.exclude_manual(book), tp_ref, tick_size, tick_value,
    ),
)
```

`compute_volume` 已实现「浮盈抵扣 / 浮亏补上 / ≤0 走最小手数 / 向上取整到步长」，与决策 4 完全一致。算不出（`tp_ref<=0` 或每手止盈 ≤ 0）→ 本档不可用，回执说明「止盈参考价缺失，无法反推手数」。

**单型**：`manual_scatter.pending_kind(action, 现价, entry)` → 现价在上方挂 `limit`、下方挂 `stop`；现价压在入场价上 → 两种都挂不上 → 本拍跳过（券商硬约束），等价格离开再挂。

**自有止损**：`manual_scatter.sl_is_sendable(action, own_sl, entry, stops_level, point)` 为真才带上；否则**不带 SL 挂单**（宁可少一道，也不能让整张单被券商拒 error 130），并回执说明。

**挂单不带 TP**（成交后由共同止盈统一写），与手动单一致。

### 4.3 备注编码

新增 `X` 字母标识指定价档位：

```
R{1|2}L{档位号}X        例：R1L2X = 逆势规则、第 3 档、指定价档位
```

```python
_PRICE_LEVEL_RE = re.compile(r"^R([12])L(\d+)X")

def parse_price_level(comment: object) -> Optional[tuple[int, int]]:
    """从持仓 / 挂单备注解析 (规则类型, 档位号)；不是指定价档位返回 None。"""
```

- `X` 与既有 `D/P/A/W/F/O/Z/B` 不冲突。
- `_CALC_COMMENT_CODE` 保持不变（`P` 仍是旧语义「间距口径=指定价」），新逻辑只认 `X`。
- **旧挂单 / 旧持仓的 `R1L2P{数字}` 仍按旧逻辑处理**，不迁移、不破坏。

既有 `parse_add_rule_type` 的正则 `^R([12])(?:L|[DPAW]|$)` **已经匹配 `R1L2X`**（`L` 分支），因此加仓计数、逆势分类不用改。

### 4.4 与现有逆势限价机制的复用

`_counter_resting_orders` / `_counter_pending_key` / `counter_order_key` / `_align_counter_order` / `_cancel_counter_orders` 都是「逆势挂单」通用机制，指定价档位挂单**全部复用**：

| 机制 | 复用点 |
|---|---|
| 在途单识别 | `_counter_resting_orders`（按 `parse_add_rule_type == COUNTER` 过滤，已含 `R1L2X`） |
| 身份比对 | `counter_pending_key` / `counter_order_key` —— 需把 `X` 映射成新 calc 名 `"price_level"`，与旧 `P` 区分 |
| 撤单 / 改价 / 限流 | `_cancel_counter_orders` / `_align_counter_order` / `_counter_reprice_limits` |
| 收口清理 | `_cleanup_pending_before_finish`（按魔术号撤残留挂单，确认覆盖新类型） |

**改价不适用**：指定价挂单的价格固定 = 入场价，不随行情调整。只有**身份变了**（手数因 tier1 变化 / 档位变了 / 自有 SL 变了）才撤单重挂。

### 4.5 批次衔接

- **占位**：指定价档位挂出后占用这一格；**在途未成交前本规则不再挂下一档**（沿用逆势「同时最多一张在途单」排他机制）。
- **成交后** `add_counts[COUNTER] += 1` → 进入下一格（`pick_batch_level` 按 `rule_adds + 1` 命中档位，序号语义不乱）。
- **不置 `add_closed`**（决策 8）：指定价成交**不会**让该规则收口，后续档位照常。
- **计入上限**：`total_lot_limit` / `max_allow_num` 包含这一笔（决策 8）。

### 4.6 成交识别

`_observe_counter_fills` 中对 `parse_price_level(comment)` 命中的持仓：

- 标记进 `self._price_level_tickets`（下称「组 B」）
- 置「仓位构成变化 / 仓位减少」脏标记 → 触发共同 TP/SL 重算（§4.8）
- 事件上报额外带：`entry_price / tp_ref / own_sl / volume_formula`（后台要能还原「按 tier1 金额反推手数」这条链路）

### 4.7 双止损：取更近的（决策 2）

现有共同止损是「一个方向一个 SL，写给该方向所有仓」（`signal_pl.plan_common_stops` → `plan["sl"]` → `_lot_sltp_modify` 逐仓写）。要支持组 B 单独一道 SL，把 plan 升级为**逐票覆盖**：

```python
# plan 新增字段
plan["sl_by_ticket"] = {ticket: sl_value, ...}   # 组 B 的逐票止损
```

取值规则（多单；空单对称）：

```python
def nearer_stop(side: str, own_sl: float, common_sl: float) -> float:
    """指定价仓该带哪道止损：取离现价更近的那道（多单取更高，空单取更低）。"""
    if own_sl <= 0:
        return common_sl
    if common_sl <= 0:
        return own_sl
    if side == "BUY":
        return max(own_sl, common_sl)
    return min(own_sl, common_sl)
```

写入语义：

- 组 B 票号取 `sl_by_ticket[ticket]`
- 判定为「自有 SL 更近」→ **不改**，保持 broker 上挂单时写进去的自有 SL → `sl_by_ticket[ticket] = None`（沿用现值）
- 判定为「共同 SL 更近」→ 改单覆盖成共同 SL
- 其余仓（组 A：首单 / 点数档 / ATR 档 / SMC 档）取 `plan["sl"]`

**副作用（需在回执与文档里说明）**：组 B 的仓在「共同 SL 更近」时被改写成共同 SL，之后共同 SL 重算时它跟着变；「自有 SL 更近」时它保持不动。

### 4.8 共同止盈 / 共同止损

**止盈（决策 1、8）**

- 指定价仓**不参与** `manual_scatter.exclude_manual`（那只排除 `M1/M2` 手动仓），所以它天然进入共同 TP 计算 → **阶梯止盈 tier1 先到时，它和大部队一起走**。
- 共同 TP = tier1 金额 → 距离（按该方向全部仓位含指定价仓的加权均价与总手数）。
- 后续任何加仓都会让仓位构成变化 → 共同 TP/SL 重算（现有 `_lot_sltp_sync` 按 `position_book_signature` 触发，无需新机制）。
- **在途指定价挂单不参与 TP**（不在持仓里），但**共同止盈/止损被越过、要市价平仓时，必须先撤掉在途指定价挂单**（否则它会在不该成交时成交）。现有 `_void_manual_pending(OFF_LADDER_TP)` 只处理手动单 → 同一收口点要扩展到指定价挂单。

**止损**

- 共同 SL 金额 = 盈亏比% × 余额 → 距离 → 按**含指定价仓**的总手数与加权均价反推（用户原话「一起计算」）。
- **副作用**：指定价档位通常在更不利的一侧，会把共同 SL 的加权均价拉低 → 共同 SL 被推远。回执里要写明「共同止损含指定价仓 X 手」。

**自有止损触发后的重算（决策 5、6）**

自有止损由 broker 平掉该持仓 → 下一拍仓位构成变化。需要补三件事：

1. **新增触发条件「仓位减少也要重算 SL 金额」**。现有 `_lot_sltp_sync` 只在「有新开仓 / 止损配置变了 / 余额变了」时才算 `sl_amount`：

```python
# 新增运行态：按方向记录上一轮总手数
self._lot_sltp_volume: dict[str, float] = {}   # side -> 上一轮总手数
volume_dropped = current_volume(side) < self._lot_sltp_volume.get(side, 0.0)
```

`volume_dropped` 与 `opened / sl_cfg_changed / balance_changed` 同级，触发重算并**重新读余额**（`_read_balance()`）——决策 5 的「按余额重算」。

2. **TP 随加权均价移动**：仓位少一笔 → 加权均价变 → 新 TP 变。写单前走现有 2b 判定（`signal_pl.stop_side_decision`）：新 TP 已落在现价错误一侧 → 视为价格事件 → 市价平掉该方向（既有口径，不新造）；界面上要能看到「本来要写成多少」（`plan["new_tp"]` 已保留）。

3. **不收口（决策 6）**：`_lot_sltp_sync` 返回 `"hit"`（已市价平掉穿过的方向）**不等于**任务收口；只有「全部持仓归零」才 `request_stop` + `_close_all`。自有止损只走掉一笔仓，剩余继续。

### 4.9 运行时 / 重启 / 热推

| 场景 | 处理 |
|---|---|
| 在途指定价挂单 | 进 runtime（新增 `_price_pending`：`rule_type / level_index / entry / own_sl / volume / kind / ticket`），与 `_manual_pending` 同规格 |
| 节点重启 | 靠 runtime 还原在途单，避免重复挂（重复挂 = 双倍手数）；成交后的组 B 仓靠 `R1L2X` 识别，自有 SL 直接读 broker 持仓上的值，配置里的 `price_sl` 只做交叉校验 |
| 热推改 `price` / `price_tp` / `price_sl`（在途未成交） | 撤旧单 → 重算手数 → 重挂（口径同手动单 `OFF_REPLACED`）；回执说明 |
| 热推改（已成交） | **自有 SL 锁定不改**；`price_tp` 改动不影响已成交仓（手数已算死） |
| 热推改 tier1 金额 | tier1 变了 → 在途指定价挂单手数要重算 → 撤单重挂（对齐 `manual_scatter.tiers_target_changed`） |
| 阶梯止盈/止损越过要市价平仓 | 先撤在途指定价挂单，再市价平仓 |
| 任务收口 / 全平 | 撤掉残留的指定价挂单（`_cleanup_pending_before_finish` 已按魔术号撤残留挂单，确认覆盖新类型） |

### 4.10 热推回执（`node_client/strategy_reconcile.py`）

新增指定价档位字段的结论：

| 情形 | 状态 | 文案要点 |
|---|---|---|
| 在途未成交 | `applied` | 「原挂单会撤销并按新参数重挂」 |
| 已成交 | `locked` | 「自有止损已锁定，不影响已开仓位」 |

---

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/models.py` `StrategyBatchLevel` | 加 `price_tp` / `price_sl` |
| `backend/app/strategy_templates.py` `BatchLevel` + `normalize_rule` | 同上；`calc_type != 'price'` 时丢弃这两个字段 |
| `frontend/src/api/types.ts` | 加两字段 |
| `frontend/src/components/StrategyFormModal.vue` | `calc_type === 'price'` 分支加「止盈参考价」「自有止损」输入；改写 `FIELD_HELP.batch_price` |
| `node_client/strategy_rules.py` | `is_price_level` / `price_level_params` / `parse_price_level` / `PriceLevelPlan`；`resolve_threshold` 的 price 分支不再给可用阈值；`decision_comment` 对指定价档位输出 `R1L2X`；`counter_pending_key` 支持 `X` |
| `node_client/manual_scatter.py` | 无需新公式（复用 `compute_volume` / `pending_kind` / `sl_is_sendable`）；可选加 `price_level_volume` 薄封装便于单测 |
| `node_client/signal_pl.py` | `plan_common_stops` 增加 `sl_by_ticket` 逐票覆盖；`nearer_stop` 纯函数 |
| `node_client/strategy_runner.py` | 主体：`_price_level_plan` / `_place_price_level_pending` / `_observe_price_fills` / `_price_level_sl_overrides`；`_lot_sltp_sync` 增加 `volume_dropped` 触发重算 + 重读余额；撤单收口路径纳入指定价挂单；runtime 存取 |
| `node_client/strategy_reconcile.py` | 新字段热推结论（在途 = applied，已成交 = locked） |

---

## 6. 分期（每期可独立验证）

### 一期 · 配置 + 纯逻辑（不触碰执行器主流程）

- 后端 / 前端字段与文案
- `strategy_rules.py`：`is_price_level` / `price_level_params` / `parse_price_level` / `PriceLevelPlan` / `decision_comment` 输出 `R1L2X`
- `signal_pl.py`：`nearer_stop` 纯函数
- 单测：档位识别、注释编解码、`nearer_stop`、手数反推（复用 `compute_volume` 的口径）

**验收**：指定价档位能被识别、能编解码注释、能算出「取更近的止损」，执行器行为零变化。

### 二期 · 挂单与成交

- `_sync_counter_limit` 里转 `PriceLevelPlan` 并挂出（自带 SL、手数反推、票数 `limit`/`stop`）
- 在途 runtime 存取 + 重启恢复
- `_observe_counter_fills` 识别 `R1L2X` → 组 B 标记 + 事件上报
- `_cleanup_pending_before_finish` / 阶梯止盈收口路径纳入指定价挂单撤销

**验收**：指定价档位能挂出并在成交时被识别；重启不重复挂单；收口不残留挂单。

### 三期 · 止盈联动

- 组 B 纳入共同 TP（确认 `exclude_manual` 不误排）
- 成交后 / 后续加仓触发共同 TP/SL 重算
- 阶梯止盈先到时撤在途指定价挂单

**验收**：指定价仓与大部队同价止盈；后续加仓后 TP 同步移动。

### 四期 · 双止损与重算

- `plan_common_stops` 输出 `sl_by_ticket`；`_lot_sltp_modify` 支持逐票覆盖
- 自有 SL 更近 → 不改（沿用 broker 上的自有 SL）；共同 SL 更近 → 改单覆盖
- `_lot_sltp_sync` 增加 `volume_dropped` 触发 + 重读余额 → 自有 SL 触发后按新加权均价 + tier1 重算 TP/SL
- `strategy_reconcile` 新字段结论（已成交锁定）

**验收**：两道止损取更近的生效；自有 SL 被打掉后剩余仓位按新 TP/SL 继续跑且不收口。

---

## 7. 单测清单

| 文件 | 覆盖点 |
|---|---|
| `node_client/test_strategy_rules.py` | `is_price_level` 识别；`price_level_params` 取参；`parse_price_level` 编解码；`decision_comment` 输出 `R1L2X`；`resolve_threshold` 对 price 档位不给可用阈值 |
| `node_client/test_manual_scatter.py` | 手数反推（浮盈抵扣 / 浮亏补上 / ≤0 走最小手数 / 步长取整）；`pending_kind` 选单型；`sl_is_sendable` 拒单保护 |
| `node_client/test_signal_pl.py` | `nearer_stop` 四种组合；`sl_by_ticket` 输出 |
| `node_client/test_balance_sl_sync.py` | 自有 SL 触发后按余额 + tier1 重算 TP/SL；`volume_dropped` 触发重算；不收口 |
| `node_client/test_strategy_runner.py` | 挂单 → 成交 → 纳入共同 TP → 后续加仓再计算；收口撤单；重启恢复不重复挂 |
| `node_client/test_strategy_reconcile.py` | 指定价字段热推结论（在途 applied / 已成交 locked） |

---

## 8. 与工作区未提交改动的交叉点

当前工作区有一批「热推无条件采纳 + 档位兜底」的未提交改动（`strategy_rules.pick_batch_level` 已带兜底、`strategy_reconcile` 已改成字段值一律采纳）。本方案与它有两处耦合，实现时必须处理：

### 8.1 档位兜底不能落到指定价档位

`pick_batch_level` 的兜底（「档位没覆盖下一次加仓 → 沿用最接近的档位」）若落到**指定价档位**，会挂出一张用户没预期的指定价单（用户以为只是加一仓普通加仓，实际挂了一张带自有止损的指定价单）。

处理：兜底路径**跳过指定价档位**（兜底只允许落到「间距型」档位）；若最近的间距型档位也不存在，则返回 `(None, None)`（不加仓）。若仍要落到指定价档位，则必须在事件说明里显式标注「沿用了档位#K（指定价档位）」——**优先选择跳过**。

### 8.2 「字段值一律采纳」与「已成交自有 SL 锁定」冲突

热推无条件采纳方案的核心是「字段值永远采纳用户填的值」。但指定价档位**已成交后的自有 SL** 若照此一律采纳，会改掉已开仓的止损 —— 与决策 5/6（自有 SL 触发后重算、不收口）冲突，且改已开仓止损属于不可逆动作。

处理：指定价档位的 `price_sl`（以及 `price_tp`）走「**已成交即锁定**」的例外分支，与手动分散仓的 `manual_locked` 同规格；未成交（在途）时仍可改 → 撤单重挂。

---

## 9. 边界与已知取舍

| 取舍 | 说明 |
|---|---|
| 同时最多一张在途挂单 | 一条规则配多个指定价档位时，档位之间**串行**：前一档成交后才进入下一格。理由：逆势挂单排他是既有机制，复用成本最低，且保证 `pick_batch_level` 的序号语义不漂移 |
| 指定价挂单不带 TP | 成交前无法确定共同 TP（会随仓位构成变动），与手动单一致，成交后由共同止盈统一写 |
| 共同 SL 被推远 | 指定价仓在不利侧 → 共同 SL 距离变长。这是「指定价仓一起计算」的必然结果（用户明确要求），需在回执中可见 |
| 顺势指定价不变 | 决策 3：顺势仍是到价市价成交，成交价可能与指定价有滑点差异 |
| 旧单不迁移 | 既有 `R1L2P{数字}` 挂单 / 持仓按旧逻辑处理，新逻辑只认 `R1L2X` |
| 自有 SL 与共同 SL 不可同时存在于一个持仓 | MT5 一个持仓只有一个 SL。「两道止损」由「取更近的」+ 节点侧模拟实现，不是终端原生支持 |
