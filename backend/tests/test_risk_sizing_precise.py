"""测试精确止损止盈调整的核心计算函数。

口径（限价 / 市价一致）：
- 风险侧：risk_amount 由整个头寸（底仓 + 分散仓）共同承担
  止损距离 = risk_amount × tick_size / (总手数 × tick_value)
- 利润侧：满档时分散仓已全平、只剩底仓在手，要求「已平 + 浮盈」= risk_amount × rr_ratio
  止盈跨度 = risk_amount × rr_ratio × tick_size / (tick_value × 满档加权系数)
"""
import pytest
from node_client import risk_sizing


class FakeSpec:
    """模拟品种规格。"""
    def __init__(self, digits=2, volume_digits=2, tick_size=0.01, tick_value=1.0, volume_min=0.01):
        self.digits = digits
        self.volume_digits = volume_digits
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.volume_min = volume_min
        self.volume_step = 10 ** (-volume_digits)

    def floor_lot(self, lot: float) -> float:
        """向下取整到最小手数精度。"""
        if lot <= 0:
            return 0
        step = self.volume_step
        return round(round(lot / step) * step, self.volume_digits)


class FakeCfg:
    """模拟配置。"""
    def __init__(self, risk_amount=500, rr_ratio=2.0, add_batches=10, base_ratio=100.0):
        self.risk_amount = risk_amount
        self.rr_ratio = rr_ratio
        self.add_batches = add_batches
        self.base_ratio = base_ratio


def _ladder_profit(base_lot, per_lot, count, span, tick_value, tick_size=0.01):
    """满档时的累计利润：底仓吃满档 + 分散仓分档兑现。

    每手利润 = 距离 / tick_size × tick_value（价格距离折算成金额）
    """
    base = base_lot * (span / tick_size) * tick_value
    dist = sum(
        per_lot * (span / tick_size) * (i / count) * tick_value
        for i in range(1, count + 1)
    )
    return base + dist


# ---------------------------------------------------------------------------
# 限价开仓
# ---------------------------------------------------------------------------

def test_recalc_sl_tp_limit_basic():
    """限价：止损距离按总手数算，带 tick_size 换算。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0,
        total_lot=0.45,
        cfg=cfg,
        spec=spec,
        direction='BUY',
        base_lot=0.15,
        per_lot=0.03,
        count=10,
    )

    assert result['error'] is None
    assert len(result['ladder']) == 11  # 底仓 + 10 档分散仓

    # 止损距离 = 500 × 0.01 / (0.45 × 1.0) = 11.111 点
    expected_sl_distance = 500 * 0.01 / (0.45 * 1.0)
    assert abs(result['new_sl_distance'] - expected_sl_distance) < 1e-6

    # 止损价 = 3560 − 11.111 = 3548.89（按品种小数位取整）
    assert abs(result['new_sl_price'] - (3560.0 - expected_sl_distance)) < 0.01


def test_recalc_sl_tp_limit_risk_is_exact():
    """限价：整个头寸打到止损，亏损恰好等于 risk_amount。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)
    total_lot = 0.45

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0,
        total_lot=total_lot,
        cfg=cfg,
        spec=spec,
        direction='BUY',
        base_lot=0.15,
        per_lot=0.03,
        count=10,
    )

    loss = total_lot * (result['new_sl_distance'] / spec.tick_size * spec.tick_value)
    assert abs(loss - 500) < 1e-6


def test_recalc_sl_tp_limit_full_ladder_profit_is_exact():
    """限价：满档时「分散仓已平 + 底仓浮盈」恰好 = risk_amount × rr_ratio。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)
    base_lot, per_lot, count = 0.15, 0.03, 10

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0,
        total_lot=base_lot + per_lot * count,
        cfg=cfg,
        spec=spec,
        direction='BUY',
        base_lot=base_lot,
        per_lot=per_lot,
        count=count,
    )

    profit = _ladder_profit(
        base_lot, per_lot, count, result['new_tp_span'], spec.tick_value,
    )
    assert abs(profit - 2000) < 1e-6


def test_recalc_sl_tp_limit_ladder():
    """限价：阶梯止盈逐档递增，最后一档吃满盈亏比。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0,
        total_lot=0.45,
        cfg=cfg,
        spec=spec,
        direction='BUY',
        base_lot=0.15,
        per_lot=0.03,
        count=10,
    )

    ladder = result['ladder']
    assert ladder[0] == 0.0  # 底仓不设止盈

    for i in range(2, len(ladder)):
        assert ladder[i] > ladder[i - 1], f"TP{i} 应大于 TP{i-1}"

    # 最后一档 = 入场价 + 止盈跨度（止盈价按品种小数位取整，允许末位误差）
    assert abs(ladder[-1] - (3560.0 + result['new_tp_span'])) < 0.01


def test_recalc_sl_tp_limit_sell():
    """限价：空单止损在上方，止盈逐档下降。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0,
        total_lot=0.45,
        cfg=cfg,
        spec=spec,
        direction='SELL',
        base_lot=0.15,
        per_lot=0.03,
        count=10,
    )

    assert result['error'] is None
    assert result['new_sl_price'] > 3560.0  # 空单止损在上方

    ladder = result['ladder']
    assert ladder[0] == 0.0
    for i in range(2, len(ladder)):
        assert ladder[i] < ladder[i - 1], f"TP{i} 应小于 TP{i-1}"


def test_recalc_sl_tp_limit_error_zero_price():
    """限价：挂单价无效。"""
    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=0, total_lot=1.0, cfg=FakeCfg(), spec=FakeSpec(),
        direction='BUY',
    )
    assert result['error'] is not None


def test_recalc_sl_tp_limit_error_zero_lot():
    """限价：总手数为零。"""
    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=2000.0, total_lot=0, cfg=FakeCfg(), spec=FakeSpec(),
        direction='BUY',
    )
    assert result['error'] is not None


def test_recalc_sl_tp_limit_error_bad_spec():
    """限价：品种规格不完整。"""
    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=2000.0, total_lot=1.0, cfg=FakeCfg(),
        spec=FakeSpec(tick_value=0), direction='BUY',
    )
    assert result['error'] is not None


def test_recalc_sl_tp_limit_infers_lots_when_missing():
    """限价：没传手数时按 _split_lots 自行推算，且满档利润仍精确。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0, total_lot=0.45, cfg=cfg, spec=spec, direction='BUY',
    )

    assert result['error'] is None
    assert len(result['ladder']) == 11

    # _split_lots(0.45) → 底仓 floor_lot(0.135)=0.13，剩余 0.32 → 每档 floor_lot(0.032)=0.03，10 档
    lots = risk_sizing._split_lots(0.45, cfg, spec)
    base_lot = lots[0]
    per_lot = lots[1] if len(lots) > 1 else 0.0
    count = max(0, len(lots) - 1)

    profit = _ladder_profit(
        base_lot, per_lot, count, result['new_tp_span'], spec.tick_value,
    )
    assert abs(profit - 2000) < 1e-6


# ---------------------------------------------------------------------------
# 市价开仓
# ---------------------------------------------------------------------------

def _positions(base_lot=0.15, per_lot=0.03, count=10, price=3560.0):
    ps = [{
        'ticket': 1, 'volume': base_lot, 'price_open': price,
        'time': 100, 'comment': 'base',
    }]
    ps += [{
        'ticket': i + 1, 'volume': per_lot, 'price_open': price,
        'time': 100 + i, 'comment': f'R3B{i}',
    } for i in range(1, count + 1)]
    return ps


def test_recalc_sl_tp_market_basic():
    """市价：止损距离按总手数算。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_market(
        positions=_positions(), cfg=cfg, spec=spec, direction='BUY',
    )

    assert result['error'] is None
    assert len(result['ladder']) == 11

    expected_sl_distance = 500 * 0.01 / (0.45 * 1.0)
    assert abs(result['new_sl_distance'] - expected_sl_distance) < 1e-6
    assert abs(result['new_sl_price'] - (3560.0 - expected_sl_distance)) < 0.01


def test_recalc_sl_tp_market_risk_is_exact():
    """市价：整个头寸打到止损，亏损恰好等于 risk_amount。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)
    positions = _positions()

    result = risk_sizing.recalc_sl_tp_market(
        positions=positions, cfg=cfg, spec=spec, direction='BUY',
    )

    total_lot = sum(p['volume'] for p in positions)
    loss = total_lot * (result['new_sl_distance'] / spec.tick_size * spec.tick_value)
    assert abs(loss - 500) < 1e-6


def test_recalc_sl_tp_market_full_ladder_profit_is_exact():
    """市价：满档时「分散仓已平 + 底仓浮盈」恰好 = risk_amount × rr_ratio。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_market(
        positions=_positions(), cfg=cfg, spec=spec, direction='BUY',
    )

    profit = _ladder_profit(0.15, 0.03, 10, result['new_tp_span'], spec.tick_value)
    assert abs(profit - 2000) < 1e-6


def test_recalc_sl_tp_market_ladder():
    """市价：阶梯止盈以底仓成交价为锚，逐档递增。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    result = risk_sizing.recalc_sl_tp_market(
        positions=_positions(), cfg=cfg, spec=spec, direction='BUY',
    )

    ladder = result['ladder']
    assert ladder[0] == 0.0
    for i in range(2, len(ladder)):
        assert ladder[i] > ladder[i - 1]
    assert abs(ladder[-1] - (3560.0 + result['new_tp_span'])) < 0.01


def test_recalc_sl_tp_market_error_no_positions():
    """市价：没有持仓。"""
    result = risk_sizing.recalc_sl_tp_market(
        positions=[], cfg=FakeCfg(), spec=FakeSpec(), direction='BUY',
    )
    assert result['error'] is not None


def test_recalc_sl_tp_market_error_zero_total_lot():
    """市价：总手数为零。"""
    result = risk_sizing.recalc_sl_tp_market(
        positions=[{
            'ticket': 1, 'volume': 0, 'price_open': 3560.0,
            'time': 100, 'comment': 'base',
        }],
        cfg=FakeCfg(), spec=FakeSpec(), direction='BUY',
    )
    assert result['error'] is not None


# ---------------------------------------------------------------------------
# 一致性
# ---------------------------------------------------------------------------

def test_recalc_sl_tp_limit_vs_market_consistency():
    """限价与市价在相同参数下结果一致。"""
    cfg = FakeCfg(risk_amount=500, rr_ratio=4.0, add_batches=10, base_ratio=30.0)
    spec = FakeSpec(digits=2, tick_size=0.01, tick_value=1.0)

    limit_result = risk_sizing.recalc_sl_tp_limit(
        entry_price=3560.0, total_lot=0.45, cfg=cfg, spec=spec, direction='BUY',
        base_lot=0.15, per_lot=0.03, count=10,
    )

    market_result = risk_sizing.recalc_sl_tp_market(
        positions=_positions(), cfg=cfg, spec=spec, direction='BUY',
    )

    assert abs(
        limit_result['new_sl_distance'] - market_result['new_sl_distance']
    ) < 1e-9
    assert abs(
        limit_result['new_sl_price'] - market_result['new_sl_price']
    ) < 1e-9
    assert limit_result['ladder'] == market_result['ladder']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
