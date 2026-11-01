"""MT5 成交类型常量（DEAL_TYPE_*）与「资金操作」的判定。

单独一个模块的原因：`mt5_client` 要 MetaTrader5 包（非 Windows 上没有），
`signal_pl` 是纯函数层、不该被拖上这个依赖。两边都要用同一份常量，
所以放在中间。
"""
from __future__ import annotations

# MT5 成交类型。数值取自官方文档，不依赖 MetaTrader5 包。
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2       # 出入金 / 手工余额调整
DEAL_TYPE_CREDIT = 3        # 授信额度
DEAL_TYPE_CHARGE = 4        # 扣费
DEAL_TYPE_CORRECTION = 5    # 更正
DEAL_TYPE_BONUS = 6         # 赠送 / 促销返佣
DEAL_TYPE_COMMISSION = 7
DEAL_TYPE_COMMISSION_DAILY = 8
DEAL_TYPE_COMMISSION_MONTHLY = 9
DEAL_TYPE_COMMISSION_AGENT_DAILY = 10
DEAL_TYPE_COMMISSION_AGENT_MONTHLY = 11
DEAL_TYPE_INTEREST = 12
DEAL_TYPE_BUY_CANCELED = 13
DEAL_TYPE_SELL_CANCELED = 14

# 计入「资金操作」的成交类型：客户主动改变账户本金的那几种。
#
# 余额变动有两种来源，含义完全不同：
# - 资金操作：客户出入金、券商赠送、扣费、授信 → 本金变了
# - 交易平仓：已实现盈亏结算 → 只是把浮动盈亏转成已实现，本金没变
#
# 止损按「比例 × 余额」反推，只应该跟着**本金**走。若把平仓结算也算进去，
# 一次止损离场后的余额结算会立刻把止损推到一个新位置 —— 那批仓位已经没了，
# 重算出来的价没有意义，还会和后续加仓的止损互相打架。
#
# 佣金类（7..11）是券商侧按日扣的，不算客户主动出入金，排除掉，免得每天
# 结算都触发一次止损重算。更正（5）与利息（12）同理：都不是客户意图。
FUNDING_DEAL_TYPES = frozenset({
    DEAL_TYPE_BALANCE,
    DEAL_TYPE_CREDIT,
    DEAL_TYPE_BONUS,
    DEAL_TYPE_CHARGE,
})
