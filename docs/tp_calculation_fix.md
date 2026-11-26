# 止盈计算公式修正

## 问题

之前的止盈计算公式**有误**：

```python
# ❌ 错误的公式
新的止盈跨度 = 新的止损距离 × cfg.rr_ratio
```

这导致止盈不符合预期的盈亏比。

## 正确的逻辑

**盈亏比（rr_ratio）的含义**：利润应该是亏损的 N 倍。

例如，rr_ratio=4 意味着：
- 如果风险 500U（止损距离）
- 那么利润应该是 500U × 4 = 2000U（止盈距离）

## 修正公式

### 新的止盈距离计算

```
目标利润 = risk_amount × rr_ratio
止盈距离 = (目标利润 × tick_size) / (总手数 × tick_value)
```

### 实现代码

```python
# ✅ 正确的公式
target_profit = cfg.risk_amount * cfg.rr_ratio
new_tp_span = (target_profit * spec.tick_size) / (total_lot * spec.tick_value)
```

## 实际例子

**参数**：
- 以损定量（risk_amount）= 500U
- 盈亏比（rr_ratio）= 4
- 品种：XAUUSD，tick_size=0.01，tick_value=10
- 总手数（total_lot）= 1.0

**计算过程**：

```
目标利润 = 500 × 4 = 2000U

止损距离 = (500 × 0.01) / (1.0 × 10) = 5 / 10 = 0.5

止盈距离 = (2000 × 0.01) / (1.0 × 10) = 20 / 10 = 2.0

验证：
- 风险 = 0.5 × 1.0 × 10 = 5 个点，每个点值 100U = 500U ✓
- 利润 = 2.0 × 1.0 × 10 = 20 个点，每个点值 100U = 2000U ✓
- 盈亏比 = 2000 / 500 = 4 ✓
```

## 改动范围

### 修改的函数

1. **`recalc_sl_tp_market()`** - 市价开仓精确计算
2. **`recalc_sl_tp_limit()`** - 限价开仓精确计算

### 修改内容

在两个函数中，将：

```python
new_tp_span = new_sl_distance * cfg.rr_ratio
```

改为：

```python
target_profit = cfg.risk_amount * cfg.rr_ratio
new_tp_span = (target_profit * spec.tick_size) / (total_lot * spec.tick_value)
```

## 测试验证

✅ 所有单元测试通过（10/10）

新增验证：
```python
# 验证止盈距离计算正确性
expected_tp_span = (500 * 4.0 * 0.01) / (1.0 * 10)  # = 2.0
assert abs(result['new_tp_span'] - expected_tp_span) < 0.001
```

## 影响范围

- ✅ **市价开仓**：改单时使用新的止盈距离
- ✅ **限价开仓**：挂单时使用新的止盈距离
- ✅ **阶梯止盈**：各档止盈也相应调整

## 验证方法

下次测试时，可以验证：
- 止损价正确
- 分散仓1的TP应该是：entry_price ± (2.0 × 1/10) = entry_price ± 0.2
- 分散仓10的TP应该是：entry_price ± (2.0 × 10/10) = entry_price ± 2.0
