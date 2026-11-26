# 限价/市价开仓止损止盈精确计算

## 语义定义

### 风险侧

**risk_amount（以损定量）由整个头寸（底仓 + 分散仓）共同承担。**

即：全部仓位成交后打到止损，账户总亏损 = risk_amount。

### 利润侧

**价格走到最后一档止盈价时，分散仓已全部止盈，底仓仍持有。此时「已平 + 浮盈」= risk_amount × rr_ratio。**

即：若底仓也在该价位平掉，总盈利恰好是 risk_amount × rr_ratio。

> 风险侧用「总手数」（满损时所有仓都在），利润侧用「满档加权系数」（满盈时只剩底仓）。
> 两者口径不同，混用会让满档总盈利达不到目标值。

---

## 计算公式

### 止损距离（风险侧）

```
每手止损亏损 = 止损距离 / tick_size × tick_value
总亏损       = 总手数 × 每手止损亏损 = risk_amount

⇒ 止损距离 = risk_amount × tick_size / (总手数 × tick_value)
```

### 止损价

```
sign = +1 (BUY) / -1 (SELL)
止损价 = 开仓价 − sign × 止损距离
```

限价模式开仓价 = 挂单价（固定）；市价模式开仓价 = 底仓实际成交价。

### 止盈跨度（利润侧）

满档时的累计利润：

```
底仓利润   = base_lot × span
分散仓合计 = Σ per_lot × span × i/count  (i=1..count)
           = per_lot × span × (count+1)/2

总利润 = span × [base_lot + per_lot × (count+1)/2] = risk_amount × rr_ratio

⇒ 止盈跨度 = risk_amount × rr_ratio × tick_size / (tick_value × 满档加权系数)

其中 满档加权系数 = base_lot + per_lot × (count+1)/2
```

### 阶梯止盈

```
底仓 TP  = 0
第 i 档 TP = 开仓价 + sign × 止盈跨度 × i / count
```

最后一档吃满配置的盈亏比。

---

## 实际算例（XAUUSDm）

| 参数 | 值 |
|------|-----|
| risk_amount | 500 USD |
| rr_ratio | 4 |
| base_ratio | 30% |
| 挂单价 | 3560.00 |
| tick_size | 0.01 |
| tick_value | 1.0 |
| 底仓 | 0.15 手 |
| 分散仓 | 10 × 0.03 手 |
| 总手数 | 0.45 手 |

```
止损距离 = 500 × 0.01 / (0.45 × 1.0) = 11.111 点
止损价   = 3560 − 11.11 = 3548.89

满档加权系数 = 0.15 + 0.03 × 11/2 = 0.15 + 0.165 = 0.315
止盈跨度 = 500 × 4 × 0.01 / (1.0 × 0.315) = 63.492 点

分散仓 TP：3563.49 / 3566.98 / ... / 3623.49
```

校验：
```
风险 = 0.45 × (11.111 / 0.01 × 1.0) = 0.45 × 1111.1 = 500.00 USD ✅
利润 = 0.15 × (63.492/0.01 × 1.0) + 0.03 × (63.492/0.01 × 1.0) × 5.5
     = 952.38 + 1047.62 = 2000.00 USD ✅
```

---

## 两种开仓方式的差异

| | 限价（limit） | 市价（market） |
|---|---|---|
| 计算时机 | 挂单前 | 全部成交后改单一次 |
| 开仓价锚点 | 挂单价（固定） | 底仓实际成交价 |
| 总手数来源 | `plan.total_lot`（计划值） | 持仓实际手数之和 |
| 手数来源 | `plan.batches` 的真实手数 | 持仓按备注区分底仓/分散仓 |
| 改单次数 | 0 次 | 1 次 |

---

## 踩过的坑

### 1. 漏了 `tick_size` 换算（导致 Invalid stops）

原公式：
```python
new_sl_distance = risk_amount / (base_lot * tick_value)   # ❌ 差 100 倍
```

`tick_value` 是「一手波动一个 tick 的价值」，不是「每点每手」。价格距离要折算成
金额必须经过 `距离 / tick_size × tick_value`，反过来就得乘 `tick_size`。

漏掉 `tick_size` 会让止损距离放大 100 倍，止损价变成负数 → MT5 报 `Invalid stops`。

### 2. 用底仓手数算风险

原公式用 `base_lot`（底仓手数）作分母，导致止损距离随 `base_ratio` 放大。
正确做法是用**总手数**，因为底仓 + 分散仓共用同一个止损价，全部打到止损。

### 3. 底仓止盈传 0

底仓不设止盈。`ladder[0] = 0.0` 若被当成止盈价传给 MT5 会报 `Invalid stops`，
必须跳过 index 0（`batch.index > 0` 才赋值）。

### 4. 分散仓止盈没更新到 plan

精确参数算出来后要回写 `batch.take_profit`，否则分散仓下单时仍用旧的计划值。

### 5. 止盈跨度用总手数当分母（满档盈利不足）

原公式 `tp_span = 止损距离 × rr_ratio` 隐含假设「所有仓都吃到满档」。
实际满档时分散仓已全平、只剩底仓在手，累计盈利会少一个 `per_lot × (count+1)/2` 的系数。
必须用「满档加权系数」当分母。

### 6. 手数推算要与实际下单一致

缺省推算手数时必须复用 `_split_lots()`，否则托底/缩单逻辑不一致，
加权系数算出来和实际持仓对不上。

### 7. 市价改单调用了不存在的 `modify_position`（止盈根本没改）

```python
await self._exec(self._mt5.modify_position, ticket, sl=..., tp=...)   # ❌
```

MT5 客户端只有 `modify_position_sl(ticket, sl, tp=None)`。属性查找直接抛
`AttributeError`，**分散仓止盈一条都没改成**，盘上留的还是 `_apply_ladder`
的旧阶梯值（`止损距 × 盈亏比`）。

表现：止损是新的（走 `modify_sl_by_magic`，那个方法名对），止盈是旧的。
满档总盈利因此远达不到 `risk_amount × rr_ratio` —— 实测 105U vs 目标 2000U。

而且异常一路抛到 `_run` 的 `except Exception`，整批持仓被判成 `faulted`。

修法：
- 方法名改成 `modify_position_sl(ticket, new_sl_price, new_tp)`（位置参数）
- 止损改单与逐笔 TP 改单都套 `try/except`，单笔失败只少改这一档

### 8. 底仓要靠备注识别，不能靠「最早成交」

市价连发时 11 笔可能在同一秒成交，票号顺序不保证。用备注判定更稳：
有 `R3B{n}` 的是分散仓，其余是底仓。备注全被券商改写时退回「最早成交」。

### 9. 权重要用实际手数，别假设等手数

部分成交或券商改过手数时，按「各档实际手数 × 档位/层数」累加才准。
层数 = `max(配置档数, 最大批次号)`（批次号 1..N 对应 `ladder[1..N]`，不是 N+1）。

### 10. 阶梯止盈取整带来的尾差

阶梯止盈价按品种最小价位取整，满档实际利润与理论值有分级舍入误差：
XAUUSDm 上约 0.02U / 2000U，可接受。断言测试时容差要放到 0.1U 量级。

---

## 相关文件

- `node_client/risk_sizing.py` — `_ladder_profit_weight()` / `recalc_sl_tp_market()` / `recalc_sl_tp_limit()`
- `node_client/strategy_runner.py` — `_risk_open_base()` / `_risk_send_order()` / `_risk_modify_sl_tp_market()`
- `backend/tests/test_risk_sizing_precise.py` — 16 个单元测试
- `node_client/test_strategy_runner.py` — `test_risk_sized_market_rewrites_sl_tp_after_all_fills` /
  `test_risk_sized_market_modify_survives_single_ticket_failure`
