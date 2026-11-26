# 以损定量精确止损止盈调整方案

## 目标

解决以损定量开仓中因最小步长和分散仓手数取整导致的止损/止盈偏差问题，确保最终风险金额精确等于预设的 `risk_amount`。

## 问题描述

- 开仓规格：XAUUSD，最小步长 0.01
- 配置：损定量 500，底仓 1 笔 + 分散仓 10 笔
- 现象：因为各档手数取整（向下取整），最终止损打掉的亏损会小于 500
- 需求：在真实开仓后重新精确计算止损和止盈，使风险 = 500

## 整体思路

### 市价开仓流程

```
下底仓 (市价，1笔)
    ↓ 立刻成交
获得: base_fill_price, base_volume

下所有分散仓 (市价，10笔一次打齐)
    ↓ 全部立刻成交
现有: 底仓 1笔 + 分散仓 10笔 = 11笔持仓

【改单点 - 仅一次】所有11笔都成交后
    ├─ 计算新的止损距离（基于实际总手数和risk_amount）
    ├─ 计算新的阶梯止盈参数（基于底仓成交价）
    ├─ 修改所有持仓的止损价 (modify_sl_by_magic 一次搞定)
    └─ 修改分散仓的止盈价 (逐个modify ticket的TP)
        ├─ 底仓: TP = 0 (保留)
        └─ 分散仓1~10: 各自的新TP
```

### 限价开仓流程

```
【计算阶段 - 一次性】挂单前精确计算参数
    ├─ 挂单价 = entry_price (固定)
    ├─ 新的止损距离 = risk_amount × tick_size / (总手数 × tick_value)
    ├─ 新的止损价 = entry_price - sign × 新的止损距离
    └─ 新的阶梯止盈 = entry_price + sign × 止盈跨度 × i / count

下底仓 + 分散仓 (限价，全部带精确的SL和TP)
    ├─ 底仓: SL = 新的止损价, TP = 0
    └─ 分散仓1~10: SL = 新的止损价, TP = 阶梯止盈[i]

挂单等待成交（无需后续改单）
```

## 核心公式

### 止损距离计算

```
新的止损距离 = risk_amount × tick_size / (总手数 × tick_value)
```

其中：
- `risk_amount` = 预设风险金额（如 500）
- `tick_size` = 品种的最小变动单位（如 XAUUSD 的 0.01）
- `总手数` = 底仓手数 + 所有分散仓手数之和
- `tick_value` = 一手波动一个 tick 的价值

### 止损价计算

```
sign = 1 if direction == 'BUY' else -1
新的止损价 = base_fill_price - sign × 新的止损距离
```

### 阶梯止盈计算

```
止盈跨度 = 新的止损距离 × rr_ratio

底仓的TP = 0

分散仓i的TP = base_fill_price + sign × 止盈跨度 × i / count
```

其中：
- `base_fill_price` = 底仓的实际成交价
- `rr_ratio` = 配置的盈亏比
- `i` = 分散仓的序号（1~10）
- `count` = 分散仓总数（10）

## 实现步骤

### 1. 在 `risk_sizing.py` 中新增计算函数

#### 市价模式：底仓成交后重新计算

```python
def recalc_sl_tp_market(
    positions: list[dict],
    cfg: RiskSizedConfig,
    spec: SymbolSpec,
    direction: str,
) -> dict:
    """
    市价开仓，所有持仓成交后重新计算精确的止损和止盈。
    
    参数：
        positions: 所有持仓列表
        cfg: 以损定量配置
        spec: 品种规格
        direction: 交易方向 (BUY/SELL)
    
    返回：{
        'new_sl_distance': float,     # 新的止损距离
        'new_sl_price': float,        # 新的止损价
        'new_tp_span': float,         # 新的止盈跨度
        'ladder': [tp_0, tp_1, ...],  # 各档止盈价 (tp_0=0 for base)
    }
    """
```

#### 限价模式：挂单前计算

```python
def recalc_sl_tp_limit(
    entry_price: float,
    total_lot: float,
    cfg: RiskSizedConfig,
    spec: SymbolSpec,
    direction: str,
) -> dict:
    """
    限价开仓，挂单前精确计算止损和止盈。
    
    参数：
        entry_price: 挂单价（固定）
        total_lot: 总手数
        cfg: 以损定量配置
        spec: 品种规格
        direction: 交易方向 (BUY/SELL)
    
    返回：{
        'new_sl_distance': float,
        'new_sl_price': float,
        'new_tp_span': float,
        'ladder': [tp_0, tp_1, ...],
    }
    """
```

### 2. 在 `strategy_runner.py` 中改造市价逻辑

#### 新增状态标记

```python
self._risk_sl_tp_modified = False    # 是否已改单（市价专用）
self._risk_new_params = {}           # 新计算的参数
```

#### 改造 `_risk_open_pending_distribute()`

在分散仓全部下单成交后，检查是否需要改单

#### 新增 `_risk_modify_sl_tp_market()` 方法

执行改单逻辑：
1. 计算新参数
2. 改所有单的止损
3. 改分散仓的止盈

### 3. 在 `strategy_runner.py` 中改造限价逻辑

#### 改造 `_risk_open_base()`

在挂单前调用 `recalc_sl_tp_limit()` 获得精确参数，然后下单时使用这些参数

#### 改造 `_risk_send_order()`

对限价模式，使用精确参数代替计划中的参数

### 4. 持仓识别

从备注中识别持仓身份：

```python
def get_batch_index_from_position(position: dict) -> int:
    """从持仓备注中提取分散仓序号。底仓返回 None，分散仓返回 0~9"""
    comment = position.get('comment', '')
    match = re.search(r'R3B(\d+)', comment)
    if match:
        return int(match.group(1))
    return None
```

### 5. 容错处理

- **改单失败**：记录日志但不中断任务
- **参数计算异常**：降级到原计划值
- **断线恢复**：重建计划时保持逻辑一致

## 改单方法说明

### 市价改单

1. **改所有单的止损**：
   ```
   modify_sl_by_magic(magic, new_sl_price)
   ```
   一次搞定所有11笔单的止损

2. **改分散仓的止盈**：
   ```
   modify_position(ticket, sl=new_sl_price, tp=new_tp)
   ```
   对10笔分散仓逐个改TP（底仓无需改）

### 限价改单

无需改单，参数在挂单时已精确

## 实现检查清单

- [ ] 在 `risk_sizing.py` 中实现 `recalc_sl_tp_market()`
- [ ] 在 `risk_sizing.py` 中实现 `recalc_sl_tp_limit()`
- [ ] 在 `strategy_runner.py` 中新增 `_risk_modify_sl_tp_market()` 方法
- [ ] 在 `_risk_open_base()` 中集成限价的精确参数计算
- [ ] 在 `_risk_open_pending_distribute()` 后追加改单检查
- [ ] 添加持仓识别函数
- [ ] 完整的单元测试
- [ ] 集成测试（市价 + 限价场景）

## 预期效果

✅ **市价**：所有单成交后改一次，止损 = 预设值 ± 1个最小变动单位

✅ **限价**：挂单时就精确，无需后续改单

✅ **容错**：改单失败不中断，降级运行

✅ **精确度**：风险金额偏差 < 1 个点差
