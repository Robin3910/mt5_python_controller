# 波幅纳入当前 K 线（加仓间距实时自适应）— 技术方案 v2


| 项目   | 说明                                                                |
| ---- | ----------------------------------------------------------------- |
| 文档版本 | v2.0（2026-10-29，替代 v1.0「跨周期波幅自适应」方案）                              |
| 编写日期 | 2026-10-29                                                        |
| 适用范围 | 模版1（`tpl_1`）分批加仓 · **逆势加仓**的「波幅（range）」间距档位                       |
| 技术栈  | Python 3.10+（node_client / backend）+ Vue 3 + TypeScript（frontend） |
| 关联文档 | [技术实现方案.md](./技术实现方案.md) §18 设计决策确认                               |
| 前一版  | [波幅自适应加仓技术方案.md](./波幅自适应加仓技术方案.md)（v1.0，跨周期参考方案，已被本文取代）           |


---



## 目录

1. [背景与目标](#1-背景与目标)
2. [核心洞察：为什么不需要跨周期](#2-核心洞察为什么不需要跨周期)
3. [设计概览](#3-设计概览)
4. [关键实现细节](#4-关键实现细节)
5. [配置模型与接口契约](#5-配置模型与接口契约)
6. [运行时状态与时序](#6-运行时状态与时序)
7. [风险、边界与明确不做的事](#7-风险边界与明确不做的事)
8. [测试方案](#8-测试方案)
9. [分阶段落地计划](#9-分阶段落地计划)
10. [与主文档的衔接](#10-与主文档的衔接)

---



## 1. 背景与目标



### 1.1 场景

逆势分批加仓，档位按 `M1 → M5 → M15 → M30` 排 4 次，每档间距用该档周期的「波幅」计算
（`calc_type = range`）。

典型行情：

1. 前期平稳。M1 档先触发、成交（这一档符合预期）。
2. 随后市场**急速下跌**。M5 档若仍用「14 根已收盘 M5」的波幅做间距，这个间距反映的是
  **过去 70 分钟**的平稳行情，远小于当下的实际跌幅。
3. 结果：M5 档的限价单挂在离现价很近的位置，急跌途中立刻被成交 → **仓位加在半山腰上**。
  M15 / M30 同理，一档接一档在下跌中位成交。



### 1.2 目标

急跌时，让**大周期档位的间距自动纳入当前这根 K 线已经走出的真实波幅**，从而：

- 大周期档位的加仓点位被推到行情**更深处**，避免在半山腰连续接刀；
- 行情恢复平稳后，间距自动回落到原始基线，不改变原有加仓节奏；
- 全过程可解释：每笔加仓单的后台「开单原因」能还原「基线波幅 → 实时波幅 → 最终生效间距」。



### 1.3 非目标

- 不改变「点数 / 指定价 / ATR / SMC 价位」四种间距的计算方式；
- 不改变加仓手数、批次切分、上限等任何其它规则；
- 不做「急跌兜底加仓」等反向能力（见 §7.4）。

---



## 2. 核心洞察：为什么不需要跨周期

v1.0 方案的做法是：M5 档的间距参考「M1 的实时波幅」，M30 档参考「M1/M5/M15 的最大波幅」。
这需要跨周期搬运数据、多套缓存、一套回落状态机。

**但这套机制是冗余的。** 原因是 K 线的时间嵌套性。

### 2.1 已收盘部分的嵌套性

M5 的 14 根已收盘覆盖 70 分钟，M1 的 14 根已收盘覆盖 14 分钟。前者的时间窗口**包含**后者，
所以 M5 窗口的最高点不低于 M1 窗口的最高点、最低点不低于 M1 窗口的最低点：

$$
\text{span}(M5,14) \;\ge\; \text{span}(M1,14)
$$



### 2.2 实时部分的嵌套性

当前这根 M5 K 线的时间区间 `[10:00, 10:30)` **包含**当前这根 M1 K 线 `[10:03, 10:04)`。
M5 这根的 high 必然 ≥ M1 这根的 high，low 必然 ≤：

$$
\text{live\_span}(M5) \;\ge\; \text{live\_span}(M1)
$$



### 2.3 结论

对 M5 档位取「自身周期的已收盘包络」与「自身周期的实时波幅」的较大值，
就已经**支配了所有更小周期能提供的任何参考值**：

$$
\max\big(\text{span}(M5,14),\ \text{live\_span}(M5)\big)
\;\ge\;
\max_{TF' \le M5}\big(\text{span}(TF',14),\ \text{live\_span}(TF')\big)
$$

> **一句话**：当前这根大周期 K 线，在几何上已经「包住」了所有小周期正在发生的事。
> 不需要跨周期搬运数据 —— 只要把「大周期当前这根」纳入统计即可。

因此 v1.0 的跨周期参考集合、`range_adapt_ratio` 门槛、`range_adapt_decay` 回落、
维持期冻结、参考缓存，**全部不需要**。

---



## 3. 设计概览



### 3.1 口径

```
baseline(TF) = high_low_span(14 根已收盘 TF)        # 现有逻辑，完全不动
live(TF)    = 当前未收盘 TF 这根的 high - low       # 新增
effective   = max(baseline, live)                    # 正常情况
effective   = baseline                                # live > baseline × cap 时（数据异常，忽略实时值）
```

- `max` 保证**只放大不缩小**：参考值小于基线时自动不生效，无需额外的门槛参数。
- `cap` 是**数据异常的安全网**，不是间距上限：实时波幅超 `基线 × cap` 说明读数异常，
  此时忽略实时值、退回基线。真实急跌的倍数（几十~几百倍）远小于默认的 1000 倍，不会被误伤。
- **不需要回落机制** —— bar 收盘会自然交接（§4.3）。



### 3.2 三个口径的关系


| 概念  | 名称          | 定义                                         | 用途         |
| --- | ----------- | ------------------------------------------ | ---------- |
| 基线  | `baseline`  | `high_low_span(14 根已收盘 TF)`                | 稳定基准，作为下限  |
| 实时  | `live`      | 当前未收盘 TF 根的 `high - low`                   | 捕捉正在发生的急跌  |
| 有效  | `effective` | `max(baseline, live)`（实时超 `基线 × cap` 判为异常时退回基线） | 实际用于计算偏离点数 |




### 3.3 与 v1.0 的对比


|              | v1.0（跨周期参考）              | v2.0（含当前 K 线）              |
| ------------ | ------------------------ | -------------------------- |
| 需要读的周期       | 每档 自身 + 所有更小周期           | 每档**仅自身**                  |
| 每个周期的 MT5 调用 | 2 次（已收盘 + 实时）            | **1 次**（`pos=0, count=16`） |
| 新增纯函数        | `adaptive_range` + 回落状态机 | **无**（一个 `max`）            |
| 跨拍状态         | 需要（存 effective + ts）     | **不需要**                    |
| 新增配置字段       | 4 个                      | **3 个**                    |
| 有效波幅         | ≥ 跨周期参考                  | **≥ 跨周期参考**（§2 证明）         |


改造量约为 v1.0 的 **1/3**。

---



## 4. 关键实现细节



### 4.1 一次读取同时拿到基线与实时

**这是本方案最关键的一处设计，也是最容易做错的地方。**

现有 `closed_bars()` 从 `pos=1` 起取，刻意跳过 index 0 的未收盘 K 线：

```1283:1292:d:\work_space\mt5_python_controller\node_client\mt5_client.py
    def closed_bars(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        """取最近 count 根**已收盘** K 线，按时间升序返回。

        从 index 1 起取，跳过 index 0 的当前未收盘 K 线——ATR / 波幅这类统计要的
        是稳定值，掺进走势未定的当前 K 线会让阈值在同一根 K 线内来回跳。
        取不到时返回空列表，由调用方决定跳过判定。
        """
```

如果**基线走旧缓存、实时单独读**，会出大问题：

> 暴跌那根 M5 收盘的瞬间：实时归零、而基线还停留在旧缓存（TTL 最长 300 秒）
> → `effective` 从 200 塌回 50 → 限价单被猛地拉回来 → **直接成交在最低点**。

**解法：一次 MT5 调用同时拿到两者。**

```
copy_rates_from_pos(symbol, M5, 0, 16)
  ↑          ↑   ↑
  |          |   └── 16 根：index 0 = 当前未收盘，index 1..15 = 已收盘
  |          └────── 周期
  └───────────────── 从当前这根开始取
```

即把 `pos=1` 改成 `pos=0`、`count` 加 1，一次调用即可切出：

- `bars[0]` → 实时根
- `bars[1..14]` → 14 根已收盘 → **波幅基线**
- `bars[1..15]` → 15 根已收盘 → **ATR**（ATR 需要前一根收盘价，故多取一根）

> **索引关系核对**：`pos=0` 起取时，index 1 是刚收盘的那根，其前一根是 index 2（不是 index 0）。
> 因此 ATR 必须在 `bars[1:16]` 这 15 根已收盘上计算，与现有 `pos=1, count=15` 的结果**完全一致**，
> ATR 的口径与结果不受本方案影响。

**代价**：基线缓存 TTL 也被迫变短（300 秒 → 刷新间隔数秒），MT5 读取次数上升。
这是**用终端压力换一致性**，必须在意（§7.5）。

### 4.2 两条读取路径

不同指标 / 不同档位对「实时」的需求不同，因此保留两条路径：


| 路径                   | 读法                   | TTL                  | 用途                     |
| -------------------- | -------------------- | -------------------- | ---------------------- |
| 现状路径 `bar_metric`    | `pos=1, count=15/14` | `min(周期秒, 300)`      | ATR 档位；未开启实时的波幅档位      |
| 新路径 `bar_range_pair` | `pos=0, count=16`    | `range_live_refresh` | 开启实时的波幅档位（同时产出基线 + 实时） |


同一周期若同时被 ATR 档位与「开实时」的波幅档位使用，会读两次。这是可接受的取舍 ——
开实时的档位数量通常只有 1~3 个。

### 4.3 bar 收盘会自然交接（为什么不需要回落机制）

这是本方案最优雅的地方：


| 时刻                | 已收盘窗口    | 当前实时根      | `effective` |
| ----------------- | -------- | ---------- | ----------- |
| 暴跌中，M5 这根走到 200 点 | span=50  | live=200   | **200**     |
| 这根 M5 收盘的瞬间       | span=200 | live=0（新根） | **200**     |


`effective` 在 bar 边界**连续**，不会塌陷。因此：

- **不需要** `decay` 回落参数；
- **不需要** 维持期 / 冻结状态；
- **不需要** 跨拍状态（`effective` 完全由当前这一次读取算出）；
- `effective` 单调不减（bar 收盘只是把 live 换成基线，总量不变）→ 不会反复改价。



### 4.4 改价限流

实时纳入后，限价单价格随行情持续变化。现有改价死区只有 1 个最小变动单位：

```970:976:d:\work_space\mt5_python_controller\node_client\strategy_runner.py
    def _stops_differ(self, left: float, right: float, point: float) -> bool:
        tol = point if point > 0 else 1e-9
        return abs(_as_float(left) - _as_float(right)) >= tol
```

慢跌时会退化成每拍发一次 `modify_pending_order`。建议两层约束：

**（1）时间限流**：只有距上次改价超过 `range_live_refresh` 秒，才有资格改单。

**（2）方向不对称的价格死区**：


| 方向              | 语义       | 死区                           | 间隔                       |
| --------------- | -------- | ---------------------------- | ------------------------ |
| 限价被推**远**（远离现价） | 保护性动作，急  | `max(point, effective × 1%)` | `range_live_refresh`     |
| 限价被拉**回**（靠近现价） | 暴跌已结束，不急 | `max(point, effective × 3%)` | `2 × range_live_refresh` |


「推远」用小死区保证急跌时能及时躲开；「拉回」用大死区避免限价单在震荡中来回被拽。

### 4.5 限价不可挂时仍降级市价

自适应只把限价推远，**无法保证在暴跌中不接刀**。市场单边直下时，任何限价最终都会被穿过。
现有降级路径不变：

- `_counter_price_block(plan) == "side"`（挂单价穿到现价一侧）→ `_counter_market_fallback()` 改市价；
- `"stops"`（券商最小距离不足）→ 同样降级市价。

这一点必须明确告知使用者：自适应解决的是「阈值太小导致连续多档在下跌中位成交」，
**不是**「完全躲过下跌」。

---



## 5. 配置模型与接口契约



### 5.1 新增字段


| 字段                   | 类型      | 默认值     | 说明                                |
| -------------------- | ------- | ------- | --------------------------------- |
| `range_include_live` | `bool`  | `false` | 波幅间距是否纳入**当前未收盘 K 线**的实时波幅（仅逆势可用） |
| `range_live_cap`     | `float` | `1000.0` | **数据异常**的判定阈值：实时波幅超过「基线 × 该值」时忽略实时值、退回基线 |
| `range_live_refresh` | `int`   | `5`     | 重检间隔（秒）：刷新波幅 + 允许改单的最小间隔          |

> **`range_live_cap` 的语义（重要）**
>
> 它**不是**「生效间距的上限」。原因：真实急跌时，实时波幅相对基线的倍数本来就可能很大
> （平稳时基线 30 点，暴跌 6000 点就是 200 倍）。若把它当成上限，正常急跌会被裁掉，
> 「跟随更大波幅」就失效了 —— 而这正是本方案要解决的问题。
>
> 所以它的语义是**数据异常的安全网**：实时波幅超过 `基线 × cap` 时，判定为读数异常
> （接口返回错乱、复权跳变等），**忽略实时值、直接退回基线**。
> 之所以不裁剪成中间值，是因为中间值既不保守（间距仍小于真实波幅）也不激进（仍大于基线），
> 是最坏的选择；退回基线至少是可解释的保守行为。
>
> 默认 1000 倍，正常行情永远碰不到。真碰上了后台会标出「实时波幅被判为异常」，
> 把这个值调大即可。

字段挂在**分批档位**（`batch_levels[]` 的每一项）上，而不是规则上 —— 不同档位的周期与
敏感度需求不同。

### 5.2 顺势强制关闭

`normalize_batch_level()` 当前不感知规则类型，需要在 `_normalize_add_on_rule()` 里，
拿到 `rule_type` 后对顺势（`type=2`）的档位强制把 `range_include_live` 置为 `False`：

```python
# _normalize_add_on_rule(rule_type, raw, ...) 内，构造 batch_levels 之后
if rule_type == RULE_TYPE_TREND:
    for level in levels:
        level["range_include_live"] = False
```

前端在顺势规则下**不展示**该开关，表单提交时也不带上；服务端再兜底一次，
保证落库数据与运行行为一致。

> **为什么顺势必须关闭**：放大间距对逆势的含义是「挂得更远、不在半山腰接刀」，
> 对顺势的含义是「进场更晚、错过最快的段」。顺势加仓追求的是趁势跟进，
> 放大阈值等于主动放弃动量，得不偿失。



### 5.3 M1 档默认不启用（重要）

用户的原始设想是「M1 的限价单大概率会先被打到，然后大周期档位用 M1 的波幅调整」。
要让 M1 档**先成交**，M1 档就**不能**纳入实时波幅 —— 否则暴跌时 M1 档的间距同样被撑开，
第一笔加仓可能直接不成交，整段行情都不加仓。

因此：`range_include_live` **默认关闭，且建议只在 M5 及以上周期开启。**


| 档位  | 建议      | 理由                  |
| --- | ------- | ------------------- |
| M1  | **不开启** | 保持原样先成交，作为「探针」档位    |
| M5  | 开启      | 急跌时推开限价，避免第二笔在半山腰成交 |
| M15 | 开启      | 同上                  |
| M30 | 开启      | 同上                  |




### 5.4 向后兼容

- 老配置没有这 3 个字段 → 规范化后补默认值 `False / 1000.0 / 5`；
- `range_include_live = False` 时，判定路径与现在**完全一致**（同一个 `distance`、同一个 `points`），
读取路径也不变（走 `pos=1` 的现状路径），**不产生任何额外 MT5 读取**。



### 5.5 前端表单

在 `StrategyFormModal.vue` 的档位编辑区，`calc_type === 'range'` 且规则 `type === 1`（逆势）时，
在「K 线周期」下方追加：


| 控件         | 控件类型          | 绑定字段                    | 校验                               |
| ---------- | ------------- | ----------------------- | -------------------------------- |
| 纳入当前 K 线波幅 | 开关（TagSelect） | `lv.range_include_live` | 仅 `calc_type === 'range'` 且逆势时可用 |
| 上限倍数       | 数字输入          | `lv.range_live_cap`     | `> 0`，默认 1000.0                  |
| 重检间隔（秒）    | 数字输入          | `lv.range_live_refresh` | `≥ 1`，默认 5                       |


帮助文案建议：

- `batch_timeframe` 追加：「开启「纳入当前 K 线波幅」后，本档间距会额外算上当前这根未走完的
K 线已经走出的高低差，急跌时自动把限价推远，避免在下跌中位加仓。」
- 新增 `range_include_live`：「开启后，本档间距 = max(14 根已收盘 K 线的高低点总跨度, 当前这根 K 线的实时高低差)。
行情平稳时与原逻辑一致。仅逆势加仓可用；建议 M1 档不开启，让第一笔加仓先成交。」

---



## 6. 运行时状态与时序



### 6.1 一次急跌的完整时间线

假设档位：`M1`（第 1 次，不开实时）、`M5`（第 2 次，开实时）、`M15`（第 3 次，开实时）、
`M30`（第 4 次，开实时），全部 `calc_type=range`。


| 时刻  | 行情                 | M5 基线 | M5 实时 | M5 `effective` | M5 档限价单位置      | 结果                |
| --- | ------------------ | ----- | ----- | -------------- | -------------- | ----------------- |
| T0  | 平稳                 | 30    | 5     | 30             | 锚点 − 30 点      | M1 档成交，M5 档按原逻辑挂出 |
| T1  | 急跌开始，M5 这根走到 80 点  | 30    | 80    | **80**         | 锚点 − 80 点（推远）  | M5 限价被急跌避开        |
| T2  | 急跌加速，M5 这根走到 200 点 | 30    | 200   | **200**        | 锚点 − 200 点（推远） | 限价持续被推远           |
| T3  | 这根 M5 收盘，新根走出 10 点 | 200   | 10    | **200**（交接）    | 锚点 − 200 点     | 交接平滑，不塌陷          |
| T4  | 恢复平稳，新根走出 3 点      | 200   | 3     | **200**        | 锚点 − 200 点     | 仍等新基线消化完旧大波动      |
| T5  | 再过 14 根 M5 后基线回落   | 30    | 3     | **30**         | 锚点 − 30 点      | 完全回到原逻辑           |


> T4 的现象值得说明：暴跌那根 M5 收盘后，它被并入「14 根已收盘」窗口，
> 基线自然变大，所以间距不会立刻缩回原位 —— 要等 14 根 M5 之后这根才被挤出窗口。
> 这是**符合预期的**：那一段行情确实走过那么大的波幅。



### 6.2 状态位置


| 状态             | 存放位置                                   | 生命周期        |
| -------------- | -------------------------------------- | ----------- |
| 基线缓存（现状路径）     | `MarketHub._metrics`                   | 进程级，跨任务共享   |
| 基线 + 实时缓存（新路径） | `MarketHub._range_pairs`               | 进程级，跨任务共享   |
| 改价限流状态（上次改价时间） | `StrategyRunner._counter_modify_state` | 任务级，随任务结束销毁 |


**没有「自适应状态」** —— 这是 v2.0 相对 v1.0 的最大简化。

### 6.3 与限价单机制的交互

逆势加仓走「预先挂一张限价」，`_sync_counter_limit()` 每拍重算阈值：

- 间距变大 → 挂单价变远 → 命中 `_align_counter_order()` 的改价分支（受 §4.4 死区 + 限流约束）；
- 若市场继续急跌、限价被穿到现价一侧 → `_counter_price_block() == "side"` →
`_counter_market_fallback()` 改市价成交。

---



## 7. 风险、边界与明确不做的事



### 7.1 有效波幅只增不减

`effective ≥ baseline` 恒成立，因此自适应**只会让间距变疏、不会变密**。
好处是不会因为误判而多开仓；代价是急跌中可能少加仓。这是有意的取舍。

### 7.2 加仓不可逆

自适应只影响**尚未触发**的档位。一旦某档成交，间距变化不回头，也不改变已成交仓位。

### 7.3 顺势与逆势语义相反

已在 §5.2 处理：顺势强制关闭。

### 7.4 明确不做的事


| 不做项             | 原因                                    |
| --------------- | ------------------------------------- |
| 急跌兜底加仓（至少加 N 笔） | 接受「整段不加仓」的后果；兜底会引入新的语义与风险             |
| ATR 口径纳入实时      | ATR 是逐根均值，尺度大、上升慢，急跌时变化远不如波幅显著；本次只做波幅 |
| 跨周期参考集合         | 已由嵌套性证明冗余（§2）                         |
| 回落 / 维持期状态机     | bar 收盘天然完成交接（§4.3）                    |
| 多品种联动 / 相关性判断   | 超出本次范围                                |
| 历史回测框架          | 本次以「影子模式」观察替代（§9 阶段 3）                |




### 7.5 性能

单任务最坏情况（M5 / M15 / M30 三档开实时 + M1 档不开）：


| 周期  | 现状路径读取     | 新路径读取（`pos=0, count=16`） |
| --- | ---------- | ------------------------ |
| M1  | 每 60 秒 1 次 | —                        |
| M5  | —          | 每 5 秒 1 次                |
| M15 | —          | 每 5 秒 1 次                |
| M30 | —          | 每 5 秒 1 次                |


合计约 **0.6 次/秒**（约 36 次/分钟）。若把 M1 也开实时，则再加 0.2 次/秒。

`copy_rates_from_pos` 是同步阻塞调用，由 `self._exec` 丢到线程池执行，不阻塞事件循环；
缓存命中时完全不触碰终端。若终端压力偏高，优先**调大** `range_live_refresh`（5 → 10 秒），
而不是关闭功能。

### 7.6 数据一致性

实时波幅依赖「当前未收盘 K 线」。不同券商的 K 线聚合方式（时区、边界）可能不同，
同一台终端上自洽即可；换节点后可能有轻微差异，与现有 ATR / 波幅的跨节点差异同源。

---



## 8. 测试方案



### 8.1 `node_client/test_bar_metrics.py`（新增用例）

```python
def test_live_span_uses_current_bar():
    assert bm.live_span({"high": 10.0, "low": 8.0}) == 2.0
    assert bm.live_span(None) == 0.0
    assert bm.live_span({"high": 0.0, "low": 0.0}) == 0.0


def test_split_window_separates_live_from_closed():
    """MT5 返回按时间升序，最后一根才是当前正在走的这根。"""
    bars = [{"high": 11, "low": 8}, {"high": 12, "low": 9}, {"high": 20, "low": 10}]
    live, closed = bm.split_window(bars)
    assert live == {"high": 20, "low": 10}
    assert closed == [{"high": 11, "low": 8}, {"high": 12, "low": 9}]


def test_range_pair_returns_baseline_and_live():
    """基线取 14 根已收盘的总跨度；实时取当前这根的高低差。"""
    bars = [{"high": 11.0, "low": 8.0}] * 14                    # 已收盘：包络 3
    bars += [{"high": 20.0, "low": 10.0}]                      # 实时根：10
    baseline, live = bm.range_pair(bars)
    assert baseline == 3.0
    assert live == 10.0


def test_range_pair_baseline_ignores_extra_bars():
    """已收盘超过 14 根时只取最近 14 根。"""
    bars = [{"high": 500.0, "low": 500.0}]                      # 更老的一根，不该进窗口
    bars += [{"high": 11.0, "low": 8.0}] * 14
    bars += [{"high": 100.0, "low": 0.0}]                      # 实时根
    baseline, _ = bm.range_pair(bars)
    assert baseline == 3.0


def test_effective_range_takes_live_when_larger():
    effective, info = bm.effective_range(baseline=30.0, live=200.0, cap=1000.0)
    assert effective == 200.0
    assert info["adapted"] is True


def test_effective_range_keeps_baseline_when_live_is_smaller():
    effective, info = bm.effective_range(baseline=30.0, live=10.0, cap=1000.0)
    assert effective == 30.0
    assert info["adapted"] is False


def test_effective_range_ignores_anomalous_live():
    """实时波幅超上限（数据异常）时忽略实时值、退回基线，而不是裁成中间值。"""
    effective, info = bm.effective_range(baseline=30.0, live=50000.0, cap=1000.0)
    assert effective == 30.0
    assert info["clamped"] is True


def test_effective_range_keeps_real_crash_uncapped():
    """真实急跌（200 倍）不会被裁剪。"""
    effective, info = bm.effective_range(baseline=30.0, live=6000.0, cap=1000.0)
    assert effective == 6000.0
    assert info["clamped"] is False


def test_effective_range_hands_over_at_bar_close():
    """bar 收盘瞬间：实时归零、基线并入这根，有效波幅连续不塌陷。"""
    before = bm.effective_range(baseline=30.0, live=200.0, cap=1000.0)[0]
    after = bm.effective_range(baseline=200.0, live=0.0, cap=1000.0)[0]
    assert before == after == 200.0
```



### 8.2 `node_client/test_strategy_rules.py`（新增用例）

```python
def test_range_level_with_live_uses_live_span():
    """开启纳入实时后，间距取 max(基线, 实时)。"""
    rule = batched_counter_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 4, "calc_type": "range", "timeframe": "M5",
        "range_include_live": True, "range_live_cap": 1000.0, "range_live_refresh": 5,
        "lot_times": 1.1, "extra_lot": 0,
    }])
    ctx = PositionCtx(
        direction="BUY", position_count=1, base_volume=0.1, base_price=2400.0,
        price=2390.0, point=0.01, add_counts={RULE_TYPE_COUNTER: 0},
    )
    ctx.bar_metrics = {metric_key("range", "M5"): 30.0}
    ctx.bar_live = {metric_key("range", "M5"): 200.0}
    threshold = resolve_threshold(RULE_TYPE_COUNTER, rule["batch_levels"][0], ctx)
    assert threshold.metric_value == 200.0
    assert threshold.adapt["baseline"] == 30.0
    assert threshold.adapt["live"] == 200.0
    assert threshold.adapt["adapted"] is True


def test_range_level_without_live_keeps_baseline():
    """未开启时行为与现在完全一致。"""
    rule = batched_counter_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 4, "calc_type": "range", "timeframe": "M5",
        "lot_times": 1.1, "extra_lot": 0,
    }])
    ctx = PositionCtx(
        direction="BUY", position_count=1, base_volume=0.1, base_price=2400.0,
        price=2390.0, point=0.01, add_counts={RULE_TYPE_COUNTER: 0},
    )
    ctx.bar_metrics = {metric_key("range", "M5"): 30.0}
    threshold = resolve_threshold(RULE_TYPE_COUNTER, rule["batch_levels"][0], ctx)
    assert threshold.metric_value == 30.0
    assert threshold.adapt is None
```



### 8.3 `node_client/test_strategy_runner.py`（新增用例）

```python
def test_counter_limit_is_repriced_only_after_refresh_interval():
    """实时放大时，限价改价受重检间隔约束，不会每拍都改。"""
    ...


def test_counter_limit_reprice_dead_zone_is_direction_asymmetric():
    """推远用小死区，拉回用大死区。"""
    ...
```



### 8.4 回归要求

- `node_client/test_bar_metrics.py`、`test_strategy_rules.py`、`test_strategy_runner.py`、
`test_market_hub.py` 全绿；
- `backend/tests/test_strategy_api.py`、`test_grid_sizing.py` 全绿；
- **ATR 口径必须零变化**：改造后 `bar_metric("atr", tf)` 的数值与改造前逐值一致
（见 §4.1 的索引关系核对）；
- 关闭 `range_include_live`（默认）时，节点行为与上线前**逐拍一致**（用回放快照对比）。



### 8.5 影子模式验证

节点保留一个调试开关：只计算并上报实时波幅与 `effective`，**不改阈值**。观察一段时间后核对：

- 急跌时 `effective` 是否及时抬升；
- bar 收盘交接时是否平滑无跳变；
- 有多少次「本来会成交但被推远」的限价单。

---



## 9. 分阶段落地计划


| 阶段  | 内容                                                                                                                   | 验收标准                        |
| --- | -------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| 1   | `bar_metrics` 新增 `live_span` / `split_window` / `range_pair` / `effective_range`；`mt5_client.recent_bars()`（`pos=0`） | 单测通过；能稳定取到当前未收盘 K 线         |
| 2   | `market_hub.bar_range_pair()`（新路径 + 短 TTL 缓存）                                                                        | 缓存命中时完全不触碰终端；TTL = 刷新间隔     |
| 3   | 影子模式：接入读取链路但不改阈值，只上报                                                                                                 | 影子数据能还原「基线 → 实时 → 有效」，行为零变化 |
| 4   | 接入 `resolve_threshold` / `AddDecision` / `gap_text` / `decision_detail`；`strategy_runner` 传 `bar_live`               | 后台「开单原因」能显示自适应明细            |
| 5   | 限价改价的死区 + 时间限流（§4.4）                                                                                                 | 慢跌时改单频率 ≤ 1/刷新间隔            |
| 6   | 后端配置字段 + 规范化 + 顺势强制关闭；前端表单与校验                                                                                        | 老配置零行为变化；顺势无法开启             |
| 7   | 灰度：按策略 / 按分组逐个开启，观察后再全量                                                                                              | 急跌中「半山腰成交」的次数显著下降，且无误触发     |


---



## 10. 与主文档的衔接

实施完成后，需要在 [技术实现方案.md](./技术实现方案.md) 的 **§18 设计决策确认**
里补一条新条目（编号接在现有最新的 `§18.51 波幅改为高低点总跨度` 之后，取 `§18.52`），
格式与现有条目一致：

> ### 18.52 逆势分批的波幅纳入当前 K 线（2026-10-29）
>
> 逆势分批的「波幅」档位在急跌中若仍只用本周期 14 根已收盘 K 线的高低点总跨度做间距，
> 大周期档位的限价会因间距过小而在下跌中位连续成交。新增「纳入当前 K 线波幅」：
> 本档间距取「14 根已收盘 K 线的高低点总跨度」与「当前这根未收盘 K 线已走出的高低差」的较大值，
> 急跌时自动把限价推远。
>
>
> | #   | 决策       | 原方案            | 新方案与理由                                                                                            |
> | --- | -------- | -------------- | ------------------------------------------------------------------------------------------------- |
> | 1   | 取数       | 只取已收盘 K 线      | **一次读取同时取基线与实时**（`pos=0, count=16`）：基线与实时若分两次读，暴跌那根收盘的瞬间基线还是旧缓存，有效波幅会从 200 塌回 50，限价单被猛地拉回直接成交在最低点 |
> | 2   | 跨周期参考    | 需参考所有更小周期的实时波幅 | **不需要**：当前这根大周期 K 线在几何上已包含所有小周期正在发生的事，`max(基线, 实时)` 已支配跨周期方案                                      |
> | 3   | 回落 / 维持期 | 需要回落速度与冻结状态    | **不需要**：bar 收盘时实时值并入已收盘窗口，有效波幅自然交接、连续不塌陷                                                          |
> | 4   | 适用规则     | 逆势与顺势通用        | **仅逆势**：放大间距对逆势是「挂得更远」，对顺势是「进场更晚、错过最快的一段」                                                         |
> | 5   | M1 档     | —              | **默认不开启**：要让第一笔加仓先成交，M1 档必须保持原样                                                                   |
> | 6   | 新增字段     | —              | 档位上新增 `range_include_live` / `range_live_cap` / `range_live_refresh`，默认关闭，老配置行为不变                 |
> | 7   | 上限语义     | 上限裁剪         | **上限改为「数据异常安全网」**：实时波幅超 `基线 × cap` 时判定为读数异常，忽略实时值退回基线；不裁成中间值（既不保守也不激进，是最坏选择）。默认 1000 倍，真实急跌（数十~数百倍）不会被裁 |
>

---



## 附录 A：字段与说明速查


| 字段                   | 位置                  | 说明                          |
| -------------------- | ------------------- | --------------------------- |
| `range_include_live` | `batch_levels[].*`  | 开关（仅逆势）                     |
| `range_live_cap`     | `batch_levels[].*`  | 数据异常判定倍数（超限则忽略实时值、退回基线）        |
| `range_live_refresh` | `batch_levels[].*`  | 重检间隔（秒），同时是改价最小间隔           |
| `ctx.bar_live`       | `PositionCtx`       | 实时波幅表，键 `f"{metric}:{TF}"`  |
| `Threshold.adapt`    | `strategy_rules`    | 自适应明细（基线 / 实时 / 生效 / 是否判定为异常） |
| `AddDecision.adapt`  | `strategy_rules`    | 自适应明细，随上报送回后台               |
| `baseline_range`     | `decision_detail()` | 基线波幅                        |
| `live_range`         | `decision_detail()` | 实时波幅                        |
| `effective_range`    | `decision_detail()` | 最终生效波幅                      |
| `range_live_adapted` | `decision_detail()` | 是否发生放大                      |
| `range_live_clamped` | `decision_detail()` | 实时值是否被判为异常而忽略                  |




## 附录 B：术语对照


| 术语   | 英文 / 字段              | 含义                                           |
| ---- | -------------------- | -------------------------------------------- |
| 基线   | `baseline`           | 本周期 14 根已收盘 K 线的高低点总跨度                       |
| 实时   | `live`               | 当前未收盘 K 线已走出的高低差                             |
| 有效波幅 | `effective`          | `max(基线, 实时)`（实时超 `基线 × cap` 判为异常时退回基线），实际生效的间距来源 |
| 波幅   | `range`              | `bar_metrics` 的 `METRIC_RANGE`，与 ATR 并列的间距口径 |
| 纳入实时 | `range_include_live` | 开关：间距是否纳入当前未收盘 K 线的实时波幅                      |


