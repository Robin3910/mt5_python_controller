# 指定价档位改造 Implementation Plan

> 日期：2026-11-02
> 配套设计：`docs/superpowers/specs/2026-11-02-price-level-design.md`
> 状态：**待执行**（用户后续下达执行指令）

---

## Global Constraints

1. **只做模版1（`tpl_1`）逆势规则的指定价档位**。顺势指定价不动（设计 §2 决策 3）。
2. **不引入 `tp_mode`**：指定价的止盈参考价只用于反推手数，出场一律走大部队阶梯止盈。
3. **不新写手数公式**：复用 `manual_scatter.compute_volume`。
4. **不改既有止盈止损口径**：2b（越价保留旧值）、价格事件市价平仓等既有语义全部保留。
5. **不破坏旧挂单 / 旧持仓**：`R1L2P{数字}` 按旧逻辑处理，新逻辑只认 `R1L2X`。
6. 每期结束必须跑全量单测（`node_client` + `backend`）。
7. 任何「已成交仓的止损」写入都属于不可逆动作，动手前必须走既有 `stop_side_decision` 判定。

---

## File Structure

| 文件 | 一期 | 二期 | 三期 | 四期 |
|---|:--:|:--:|:--:|:--:|
| `backend/app/models.py` | ✅ | | | |
| `backend/app/strategy_templates.py` | ✅ | | | |
| `frontend/src/api/types.ts` | ✅ | | | |
| `frontend/src/components/StrategyFormModal.vue` | ✅ | | | |
| `node_client/strategy_rules.py` | ✅ | ✅ | | |
| `node_client/signal_pl.py` | ✅ | | | ✅ |
| `node_client/manual_scatter.py` | ✅ | | | |
| `node_client/strategy_runner.py` | | ✅ | ✅ | ✅ |
| `node_client/strategy_reconcile.py` | | | | ✅ |
| `node_client/test_*.py` | ✅ | ✅ | ✅ | ✅ |

---

## Task 1: 后端 —— 指定价档位新增两个字段

**Files**: `backend/app/models.py`（`StrategyBatchLevel`）、`backend/app/strategy_templates.py`（`BatchLevel` + `normalize_rule`）

### Step 1: 加字段

`StrategyBatchLevel`：

```python
price_tp: float = Field(default=0.0, ge=0, description="指定价档位的止盈参考价：只用于反推手数，出场随大部队阶梯止盈")
price_sl: float = Field(default=0.0, ge=0, description="指定价档位的自有止损（绝对价位），0=不设")
```

`BatchLevel` 同名字段（dataclass，`asdict` 会自动带上）。

### Step 2: 归一化

`normalize_rule` 的 `batch_levels` 归一化里：`calc_type != 'price'` 时把 `price_tp` / `price_sl` 置 0（与「按 type 只保留该类型字段」的既有口径一致）。

### Step 3: 验证

- `python -c "import app.models"` 通过
- 后端既有测试通过

---

## Task 2: 前端 —— 指定价档位表单

**Files**: `frontend/src/api/types.ts`、`frontend/src/components/StrategyFormModal.vue`

### Step 1: 类型

`StrategyBatchLevel` 加 `price_tp?: number` / `price_sl?: number`。

### Step 2: 表单

`calc_type === 'price'` 分支下，追加两个 `field` 块：

- 「止盈参考价」绑定 `lv.price_tp`（number，`min=0`，`step=any`）
- 「自有止损」绑定 `lv.price_sl`（number，`min=0`，`step=any`）

### Step 3: 文案

改写 `FIELD_HELP.batch_price`，必须说清三件事：

1. 成交价正好等于指定价（挂单，成交不滑点）
2. 止盈参考价**只用于反推手数**，这一档不单独止盈，出场跟大部队阶梯止盈
3. 自有止损成交后锁定；与大部队共同止损并存，**取更近的**

### Step 4: 验证

- `npm run build`（或项目既有前端校验命令）通过
- 表单可保存 / 回显

---

## Task 3: 节点纯逻辑 —— 档位识别 / 注释编解码 / 取更近的止损

**Files**: `node_client/strategy_rules.py`、`node_client/signal_pl.py`、`node_client/manual_scatter.py`

### Step 1: `strategy_rules.py`

新增：

```python
def is_price_level(level: dict) -> bool: ...
def price_level_params(level: dict) -> dict: ...
_PRICE_LEVEL_RE = re.compile(r"^R([12])L(\d+)X")
def parse_price_level(comment: object) -> Optional[tuple[int, int]]: ...
```

`resolve_threshold` 的 `CALC_PRICE` 分支：仍返回 `Threshold(0.0, CALC_PRICE, target_price=target)`（**不再返回可用于偏离判定的阈值**）→ 指定价档位永远不会以市价触发。

`decision_comment`：指定价档位输出 `R{type}L{level}X`（不带尾数偏离点数）。

`counter_pending_key`：`_COUNTER_PENDING_RE` 接受 `X`，并把 `X` 映射成 calc 名 `"price_level"`（与旧 `P` 区分）。

### Step 2: `signal_pl.py`

新增纯函数：

```python
def nearer_stop(side: str, own_sl: float, common_sl: float) -> float: ...
```

### Step 3: `manual_scatter.py`

可选薄封装 `price_level_volume(...)`，内部直接调 `compute_volume`，仅为单测可读性。

### Step 4: 单测

- `test_strategy_rules.py`：`is_price_level` / `price_level_params` / `parse_price_level` 编解码 / `decision_comment` 输出 `R1L2X` / `resolve_threshold` 对 price 档位不给可用阈值
- `test_signal_pl.py`：`nearer_stop` 四种组合
- `test_manual_scatter.py`：手数反推（浮盈抵扣 / 浮亏补上 / ≤0 走最小手数 / 步长取整）

### Step 5: 验证

- `python -m pytest node_client/test_strategy_rules.py node_client/test_signal_pl.py node_client/test_manual_scatter.py` 通过
- **执行器行为零变化**（一期不碰 `strategy_runner.py`）

---

## Task 4: 节点 —— 挂单与成交（二期）

**Files**: `node_client/strategy_runner.py`、`node_client/strategy_rules.py`

### Step 1: `PriceLevelPlan`

在 `strategy_rules.py` 定义（与 `AddDecision` 同居）：

```python
@dataclass
class PriceLevelPlan:
    rule_type: int
    level_index: int
    action: str
    entry: float
    tp_ref: float
    own_sl: float
    volume: float
    limit_kind: str        # "limit" | "stop"
    comment: str           # R1L2X
    direction: str
    base_price: float
    price: float
    point: float
    base_volume: float
    lot_times: float
    extra_lot: float
    add_count: int
    limit_kind_name: str
    limit_value: int
```

### Step 2: 计划组装 `_price_level_plan`

在 `_sync_counter_limit` 里，`plan_counter_limit` 之后：

1. 取命中档位 `level` 与 `level_index`
2. `is_price_level(level)` → 走 `_price_level_plan`，否则维持现状
3. 手数反推：`manual_scatter.compute_volume(...)`（`current_lot_size` 用 `manual_scatter.current_lot(book)`；`projected_pl` 用 `manual_scatter.projected_profit(manual_scatter.exclude_manual(book), tp_ref, ...)`）
4. 单型：`manual_scatter.pending_kind(action, 现价, entry)` → `limit` / `stop`；返回 `None`（现价压在入场价）→ 本拍跳过
5. 自有 SL：`manual_scatter.sl_is_sendable(action, own_sl, entry, stops_level, point)` 为真才带上，否则不带并说明

### Step 3: 挂出

复用 `_place_counter_limit` 的下单路径，但：

- 价格 = `entry`（固定，不改价）
- SL = 自有 SL（可能为 `None`）
- TP = `None`
- comment = `R1L2X`
- 事件类型沿用 `cnt_limit`，detail 里带 `price_level` 标记与手数公式

### Step 4: 身份与撤单

`counter_order_key` 对指定价档位返回 `(volume, level_index, "price_level", "")` —— 身份变了才撤单重挂；价格不变 → 不改价。

### Step 5: 成交识别

`_observe_counter_fills`：对 `parse_price_level(comment)` 命中的持仓：

- 记入 `self._price_level_tickets`
- 触发「仓位构成变化」脏标记
- 事件 detail 带 `entry_price / tp_ref / own_sl / volume_formula`

### Step 6: runtime 存取

新增 `self._price_pending`（结构对齐 `_manual_pending`：`rule_type / level_index / entry / own_sl / volume / kind / ticket`），写入 `_manual_runtime_payload` 等价位置，重启时还原。

### Step 7: 撤单路径

- `_cleanup_pending_before_finish`（收口）：确认按魔术号撤残留挂单覆盖 `R1L2X`
- 阶梯止盈先到 → 市价平仓前：在 `_void_manual_pending(OFF_LADDER_TP)` 同一收口点，撤掉在途指定价挂单

### Step 8: 单测

`test_strategy_runner.py`：

- 命中指定价档位 → 挂出（价格 = 入场价、带自有 SL、无 TP、comment = `R1L2X`）
- 现价压在入场价上 → 本拍不挂
- 自有 SL 不合法 → 不带 SL 挂单
- 成交识别 → 组 B 标记 + `add_counts` 递增 + 不置 `add_closed`
- 重启恢复：不重复挂单

### Step 9: 验证

`python -m pytest node_client/` 全通过。

---

## Task 5: 节点 —— 止盈联动（三期）

**Files**: `node_client/strategy_runner.py`

### Step 1: 确认纳入

指定价仓（comment `R1L2X`）**不**被 `manual_scatter.exclude_manual` 排除（那只排 `M1/M2`）→ 天然进入共同 TP 计算。加一条单测锁住这个前提。

### Step 2: 成交后重算

组 B 成交 → 仓位构成变化 → `_lot_sltp_sync` 现有 `position_book_signature` 机制触发共同 TP/SL 重算。补一条测试确认共同 TP 已包含组 B 手数。

### Step 3: 后续加仓再计算

后续任意加仓（点位档 / ATR 档 / 另一指定价档）→ 签名变化 → 共同 TP/SL 重算，组 B 的 TP 同步移动。补测试。

### Step 4: 阶梯止盈先到

在途指定价挂单要在市价平仓前被撤掉。补测试。

### Step 5: 单测与验证

`python -m pytest node_client/` 全通过。

---

## Task 6: 节点 —— 双止损与重算（四期）

**Files**: `node_client/signal_pl.py`、`node_client/strategy_runner.py`、`node_client/strategy_reconcile.py`

### Step 1: `sl_by_ticket`

`signal_pl.plan_common_stops` 增加输出：`plan["sl_by_ticket"] = {ticket: sl_value}`。

规则：对每个组 B 持仓 → `nearer_stop(side, own_sl, common_sl)`；判定为「自有 SL 更近」→ 记 `None`（沿用 broker 上的自有 SL）；「共同 SL 更近」→ 记共同 SL。

`own_sl` 来源：broker 持仓上的 `sl` 字段（挂单时写进去的）。

### Step 2: 逐票写入

`_lot_sltp_modify`：组 B 票号取 `sl_by_ticket[ticket]`（`None` → 沿用持仓现值）；其余仓取 `plan["sl"]`。

### Step 3: 「仓位减少也重算」

`_lot_sltp_sync`：

```python
self._lot_sltp_volume: dict[str, float] = {}   # side -> 上一轮总手数
volume_dropped = current_volume(side) < self._lot_sltp_volume.get(side, 0.0)
```

`volume_dropped` 与 `opened / sl_cfg_changed / balance_changed` 同级 → 触发重算并**重新读余额**（`_read_balance()`）。

### Step 4: 自有 SL 触发后的 TP/SL 重算

TP 随新加权均价移动（`plan_common_stops` 已按仓位构成算），写单前走 `stop_side_decision` 既有判定；新 TP 越价 → 价格事件 → 市价平掉该方向（既有口径）。

**不收口**：自有 SL 只走掉一笔；`_lot_sltp_sync` 返回 `"hit"` ≠ 任务收口，只有全部持仓归零才 `request_stop` + `_close_all`。

### Step 5: 热推结论

`strategy_reconcile.py`：指定价档位字段

- 在途未成交 → `applied` + 「原挂单会撤销并按新参数重挂」
- 已成交 → `locked` + 「自有止损已锁定，不影响已开仓位」

需要新的入参（例如 `price_locked: set[tuple[int, int]]` = (rule_type, level_index) 集合），由 `strategy_runner` 随 `apply_strategy_detailed` 传入。

### Step 6: 单测

- `test_signal_pl.py`：`sl_by_ticket` 输出
- `test_balance_sl_sync.py`：自有 SL 触发后按余额 + tier1 重算 TP/SL；`volume_dropped` 触发重算；不收口
- `test_strategy_reconcile.py`：在途 applied / 已成交 locked

### Step 7: 验证

`python -m pytest node_client/ backend/tests/` 全通过。

---

## Task 7: 交叉点处理（与热推无条件采纳方案）

**Files**: `node_client/strategy_rules.py`、`node_client/strategy_reconcile.py`

### Step 1: 档位兜底跳过指定价档位

`pick_batch_level` / `_fallback_batch_level`：兜底落点必须是「间距型」档位（`is_price_level(level)` 为假）；最近的间距型档位不存在 → 返回 `(None, None)`（不加仓）。

### Step 2: 已成交自有 SL 锁定（与「字段值一律采纳」的例外）

指定价档位的 `price_sl` / `price_tp` 走「已成交即锁定」分支；未成交（在途）仍可改 → 撤单重挂。

### Step 3: 单测

- `test_strategy_rules.py`：兜底不落到指定价档位
- `test_strategy_reconcile.py`：已成交锁定 / 在途可改

---

## Task 8: 文档同步与全量回归

**Files**: `docs/技术实现方案.md`、`使用与部署文档.md`

### Step 1: 文档

在技术实现方案里补一节「指定价档位」，写清：

- 指定价 = 一笔带自有止损的加仓单，成交价 = 指定价
- 止盈参考价只用于算手数
- 双止损取更近的
- 自有 SL 触发后按余额 + tier1 重算 TP/SL，不收口
- 指定价档位不是最后一仓

### Step 2: 全量回归

```
python -m pytest node_client/ backend/tests/ -q
```

前端：`npm run build`（或项目既有命令）通过。

---

## Review Focus

评审时重点看这几处：

1. **`resolve_threshold` 对 price 档位不给可用阈值** —— 否则指定价档位会同时被「市价触发」和「挂单」两条路径命中，手数会被算两次。
2. **`counter_pending_key` 的 `X` 映射** —— 映射错了会导致撤单重挂判成「身份没变」，改了参数却不动挂单。
3. **`sl_by_ticket` 里 `None` 的语义** —— `None` = 沿用持仓现值（保持 broker 上的自有 SL），不是「不设止损」。这两种语义混淆会导致自有 SL 被清掉。
4. **`volume_dropped` 的手数基准** —— 必须按**方向**比，不是按总数比；多空混合时总数会误判。
5. **兜底跳过指定价档位** —— 否则用户会收到一张没预期的指定价挂单。
6. **热推锁定的例外分支** —— 已在成交的指定价仓的 `price_sl` 不能被热推改掉。
