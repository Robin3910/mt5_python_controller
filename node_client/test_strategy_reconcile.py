"""热推配置的逐字段 reconcile：字段分类、锁定保留旧值、结论生成。"""
import asyncio

import market_hub as mh
import strategy_reconcile as sr
from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


def _rule(rule_type: int, **over) -> dict:
    rule = {
        "type": rule_type, "status": 1, "action": "all",
        "point": 10.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 5, "total_lot_limit": 5,
        "batch_enabled": False, "batch_levels": [],
        "manual_scatter": {"enabled": True, "entry_price": 4400.0, "take_profit": 4410.0},
    }
    rule.update(over)
    return rule


def _snap(rules: list, **over) -> dict:
    snap = {"strategy_id": "s1", "template_id": "tpl_1", "rules": rules}
    snap.update(over)
    return snap


class FakeHub:
    def __init__(self) -> None:
        self.sub: mh.Subscription | None = None

    def subscribe(self, *, symbol, magic, direction, hold_when_empty=False,
                  track_orders=False) -> mh.Subscription:
        self.sub = mh.Subscription(
            symbol=symbol, magic=magic, direction=direction,
            hold_when_empty=bool(hold_when_empty),
            track_orders=bool(track_orders),
        )
        return self.sub

    def unsubscribe(self, sub) -> None:
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        return 0.0


def _runner(sent: list, *, mt5=None, strategy=None, runtime=None):
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"template_id": "tpl_1", "rules": [_rule(1)]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
        runtime=runtime,
    )
    return runner, hub


# ----------------------------------------------------------------------
# 上限类字段
# ----------------------------------------------------------------------
def test_limit_increase_is_applied():
    old = _snap([_rule(1, total_lot_limit=5)])
    new = _snap([_rule(1, total_lot_limit=10)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={1: 3})
    assert sr.summarize(fields) == "applied"
    assert effective["rules"][0]["total_lot_limit"] == 10
    assert "还能再开 7 笔" in fields[0]["message"]
    assert fields[0]["exec"] == "will_add"


def test_limit_decrease_above_added_is_applied():
    """缩容到 ≥ 已开笔数：合法，能缩减剩余额度。"""
    old = _snap([_rule(1, total_lot_limit=5)])
    new = _snap([_rule(1, total_lot_limit=4)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={1: 3})
    assert sr.summarize(fields) == "applied"
    assert effective["rules"][0]["total_lot_limit"] == 4
    assert "还能再开 1 笔" in fields[0]["message"]
    assert fields[0]["exec"] == "will_add"


def test_limit_below_added_is_accepted_but_no_more_adds():
    """缩容到 < 已开笔数：新值照样采纳，节点不再新增批次。

    配置无条件热推：不保留旧上限（那等于偷偷改了用户填的 2），
    判定侧现成的 `rule_adds >= limit` 会自然停止加仓。
    """
    old = _snap([_rule(1, total_lot_limit=5)])
    new = _snap([_rule(1, total_lot_limit=2)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={1: 3})
    assert fields[0]["status"] == "no_effect"
    assert fields[0]["exec"] == "no_more_adds"
    assert sr.summarize(fields) == "partial"
    assert effective["rules"][0]["total_lot_limit"] == 2, "新值必须被采纳，不能保留旧的 5"
    assert "不再新增批次" in fields[0]["message"]
    assert "止盈止损" in fields[0]["message"]


def test_limit_zero_is_treated_as_unlimited():
    """0 = 不限：不参与下限比较。"""
    old = _snap([_rule(1, total_lot_limit=5)])
    new = _snap([_rule(1, total_lot_limit=0)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={1: 3})
    assert fields[0]["status"] == "applied"
    assert fields[0]["exec"] == "will_add"
    assert effective["rules"][0]["total_lot_limit"] == 0


# ----------------------------------------------------------------------
# 加仓已收口
# ----------------------------------------------------------------------
def test_limit_change_after_manual_closed_is_no_effect():
    """手动仓开过 → 加仓已收口：改上限不产生效果。"""
    old = _snap([_rule(1)])
    new = _snap([_rule(1, total_lot_limit=10)])
    fields, _effective = sr.reconcile_snapshot(
        old, new, added_by_rule={1: 3}, closed_rules={1},
    )
    assert fields[0]["status"] == "no_effect"
    assert fields[0]["exec"] == "no_more_adds"
    assert sr.summarize(fields) == "partial"
    assert "加仓已收口" in fields[0]["message"]


# ----------------------------------------------------------------------
# 手动分散仓锁定：只在「已成交」后锁死
# ----------------------------------------------------------------------
def test_manual_config_locked_after_filled():
    """已成交（FILLED）：整块锁死，保留旧值。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4500.0})])
    fields, effective = sr.reconcile_snapshot(old, new, manual_locked={2})
    assert fields[0]["status"] == "locked"
    assert effective["rules"][0]["manual_scatter"]["entry_price"] == 4400.0, \
        "锁定的字段应保留旧值"
    assert sr.summarize(fields) == "partial"


def test_manual_toggle_locked_after_filled():
    """手动仓的 enabled 在已成交后也锁死：不能取消。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True})])
    new = _snap([_rule(2, manual_scatter={"enabled": False})])
    fields, effective = sr.reconcile_snapshot(old, new, manual_locked={2})
    assert fields[0]["status"] == "locked"
    assert effective["rules"][0]["manual_scatter"]["enabled"] is True


def test_manual_config_free_while_pending():
    """挂单在盘上（PENDING）：配置可以热推，客户改参数后节点撤单重挂。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4500.0})])
    fields, effective = sr.reconcile_snapshot(old, new, manual_locked=set())
    assert not fields
    assert effective["rules"][0]["manual_scatter"]["entry_price"] == 4500.0


def test_manual_config_free_before_fired():
    """手动仓还没挂过时，配置可以正常热推。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4500.0})])
    fields, effective = sr.reconcile_snapshot(old, new, manual_locked=set())
    assert not fields
    assert effective["rules"][0]["manual_scatter"]["entry_price"] == 4500.0


def test_manual_removed_while_pending_reports_cancel():
    """挂单在盘上时客户把手动单删了：回执要说明挂单会被撤掉。

    动作不可逆（撤单后那张单就没了），必须让客户在回执里看见。
    """
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": False})])
    fields, _effective = sr.reconcile_snapshot(old, new, manual_pending={2})
    assert len(fields) == 1
    assert fields[0]["status"] == "applied"
    assert "撤掉" in fields[0]["message"]
    assert "manual_scatter" in fields[0]["field"]


def test_manual_removed_without_pending_has_no_extra_field():
    """挂单不在盘上时删手动单：不用额外报一条（没有单可撤）。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": False})])
    fields, _effective = sr.reconcile_snapshot(old, new, manual_pending=set())
    assert not fields


def test_manual_block_removed_while_pending_reports_cancel():
    """手动单整块被删（不是 enabled=false）：同样要报「挂单会被撤掉」。"""
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2)])
    new["rules"][0].pop("manual_scatter")
    fields, _effective = sr.reconcile_snapshot(old, new, manual_pending={2})
    assert len(fields) == 1
    assert "撤掉" in fields[0]["message"]


def test_manual_removed_after_filled_stays_locked():
    """已成交（FILLED）后删手动单：仍然锁死，不报「会被撤掉」。

    已成交的手动仓是持仓，不是挂单——撤单对它没有意义。
    """
    old = _snap([_rule(2, manual_scatter={"enabled": True, "entry_price": 4400.0})])
    new = _snap([_rule(2, manual_scatter={"enabled": False})])
    fields, effective = sr.reconcile_snapshot(
        old, new, manual_locked={2}, manual_pending={2},
    )
    assert fields[0]["status"] == "locked"
    assert effective["rules"][0]["manual_scatter"]["enabled"] is True


# ----------------------------------------------------------------------
# 规则增删
# ----------------------------------------------------------------------
def test_rule_deletion_is_applied_and_rule_disappears():
    """删规则 = 采纳删除：该规则不再参与加仓判定，已开仓位不平仓。"""
    old = _snap([_rule(1), _rule(2)])
    new = _snap([_rule(1)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={2: 3})
    deleted = [f for f in fields if f["field"] == "rules[type=2]"]
    assert len(deleted) == 1
    assert deleted[0]["status"] == "applied"
    assert "不再参与加仓判定" in deleted[0]["message"]
    assert "不平仓" in deleted[0]["message"]
    assert len(effective["rules"]) == 1, "被删的规则应从生效快照里消失"


def test_new_rule_is_applied():
    old = _snap([_rule(1)])
    new = _snap([_rule(1), _rule(2)])
    fields, effective = sr.reconcile_snapshot(old, new)
    assert fields[0]["status"] == "applied"
    assert "新增顺势加仓规则" in fields[0]["message"]
    assert len(effective["rules"]) == 2


def test_template_change_is_locked():
    old = _snap([_rule(1)], template_id="tpl_1")
    new = _snap([_rule(1)], template_id="tpl_2")
    fields, effective = sr.reconcile_snapshot(old, new)
    assert fields[0]["field"] == "template_id"
    assert fields[0]["status"] == "locked"
    assert effective["template_id"] == "tpl_1"


# ----------------------------------------------------------------------
# 跨规则隔离
# ----------------------------------------------------------------------
def test_counter_closed_does_not_affect_trend():
    old = _snap([_rule(1), _rule(2)])
    new = _snap([_rule(1, total_lot_limit=10), _rule(2, total_lot_limit=10)])
    fields, _effective = sr.reconcile_snapshot(
        old, new, added_by_rule={1: 3, 2: 0}, closed_rules={1},
    )
    by_field = {f["field"]: f for f in fields}
    assert by_field["rules[type=1].total_lot_limit"]["status"] == "no_effect"
    assert by_field["rules[type=2].total_lot_limit"]["status"] == "applied"


def test_rules_are_paired_by_type_not_index():
    """规则顺序变了也不串位：按类型配对。"""
    old = _snap([_rule(1, point=10.0), _rule(2, point=20.0)])
    new = _snap([_rule(2, point=20.0), _rule(1, total_lot_limit=9)])
    fields, effective = sr.reconcile_snapshot(old, new, added_by_rule={1: 0, 2: 0})
    limit_field = next(f for f in fields if f["field"] == "rules[type=1].total_lot_limit")
    assert limit_field["status"] == "applied"
    by_type = {r["type"]: r for r in effective["rules"]}
    assert by_type[1]["total_lot_limit"] == 9
    assert by_type[2]["point"] == 20.0


# ----------------------------------------------------------------------
# 结论生成
# ----------------------------------------------------------------------
def test_summarize_empty_is_applied():
    assert sr.summarize([]) == "applied"


# ----------------------------------------------------------------------
# 次数型盈亏比：热推不复活已消耗的次数
# ----------------------------------------------------------------------
def test_float_pl_max_times_increase_reports_no_resurrect():
    old = _snap([_rule(1, float_pl_ratio={
        "enabled": True, "ratio": -20.0, "monitor_mode": "times", "max_times": 2,
    })])
    new = _snap([_rule(1, float_pl_ratio={
        "enabled": True, "ratio": -20.0, "monitor_mode": "times", "max_times": 3,
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    pl = next(f for f in fields if f["field"] == "rules[float_pl_ratio].max_times")
    assert pl["status"] == "applied"
    assert "不复活" in pl["message"]


def test_float_pl_ratio_change_reports_field():
    old = _snap([_rule(1, float_pl_ratio={
        "enabled": True, "ratio": -20.0, "monitor_mode": "times", "max_times": 2,
    })])
    new = _snap([_rule(1, float_pl_ratio={
        "enabled": True, "ratio": -30.0, "monitor_mode": "times", "max_times": 2,
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    pl = next(f for f in fields if f["field"] == "rules[float_pl_ratio].ratio")
    assert pl["status"] == "applied"
    assert pl["old"] == -20.0
    assert pl["new"] == -30.0


def test_hot_push_keeps_consumed_times():
    """热推次数型盈亏比：已消耗的次数不复活。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner._signal_pl = {
        "float_pl_ratio": {
            "enabled": True, "ratio": -20.0, "action": "close_all",
            "monitor_mode": "times", "max_times": 3, "remaining_times": 1,
        },
        "lot_pl_tiers": {"enabled": False, "batch_count": 2, "close_action": "all",
                         "tiers": []},
    }
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_rule(1, float_pl_ratio={
            "enabled": True, "ratio": -20.0, "monitor_mode": "times", "max_times": 3,
        })],
    })
    pl = runner._signal_pl["float_pl_ratio"]
    assert pl["remaining_times"] == 1, "热推不应把已消耗的次数复活"


def test_hot_push_switching_to_times_restarts_count():
    """从循环模式切成次数型：监控方式变了，从头起算。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner._signal_pl = {
        "float_pl_ratio": {
            "enabled": True, "ratio": -20.0, "action": "close_all",
            "monitor_mode": "loop", "max_times": 3, "remaining_times": 0,
        },
        "lot_pl_tiers": {"enabled": False, "batch_count": 2, "close_action": "all",
                         "tiers": []},
    }
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_rule(1, float_pl_ratio={
            "enabled": True, "ratio": -20.0, "monitor_mode": "times", "max_times": 3,
        })],
    })
    pl = runner._signal_pl["float_pl_ratio"]
    assert pl["monitor_mode"] == "times"
    assert pl["remaining_times"] == 3


def test_float_pl_remaining_helper():
    import signal_pl as sp
    assert sp.float_pl_remaining({
        "float_pl_ratio": {"monitor_mode": "times", "remaining_times": 2},
    }) == 2
    assert sp.float_pl_remaining({
        "float_pl_ratio": {"monitor_mode": "loop", "remaining_times": 2},
    }) == 0
    assert sp.float_pl_remaining({}) == 0


def test_summarize_mixed_is_partial():
    fields = [
        {"status": "applied"},
        {"status": "locked"},
    ]
    assert sr.summarize(fields) == "partial"


def test_rule_type_label():
    assert sr.rule_type_label(1) == "逆势加仓"
    assert sr.rule_type_label(2) == "顺势加仓"


# ----------------------------------------------------------------------
# 执行器：版本门与逐字段结论
# ----------------------------------------------------------------------
def test_apply_strategy_detailed_reports_applied():
    sent: list = []
    runner, _hub = _runner(sent)
    outcome = runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1, total_lot_limit=8)]},
    )
    assert outcome["result"] == "applied"
    assert runner._applied_version is None


def test_apply_strategy_detailed_records_version():
    sent: list = []
    runner, _hub = _runner(sent)
    runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1)]}, config_version=7,
    )
    assert runner._applied_version == 7


def test_apply_strategy_detailed_rejects_stale_version():
    """迟到的旧版本包直接丢弃，运行态不变。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1, total_lot_limit=8)]},
        config_version=10,
    )
    before = runner.strategy.get("rules")
    outcome = runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1, total_lot_limit=3)]},
        config_version=5,
    )
    assert outcome["result"] == "rejected"
    assert outcome["fields"][0]["field"] == "config_version"
    assert runner.strategy.get("rules") == before, "旧版本不应改变运行态"
    assert runner._applied_version == 10


def test_apply_strategy_detailed_allows_newer_version():
    sent: list = []
    runner, _hub = _runner(sent)
    runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1)]}, config_version=10,
    )
    outcome = runner.apply_strategy_detailed(
        {"template_id": "tpl_1", "rules": [_rule(1, total_lot_limit=8)]},
        config_version=11,
    )
    assert outcome["result"] == "applied"
    assert runner._applied_version == 11


def test_apply_strategy_detailed_rejects_grid_switch():
    sent: list = []
    runner, _hub = _runner(sent)
    outcome = runner.apply_strategy_detailed({
        "rules": [{
            "type": 4, "status": 1, "action": "all",
            "price_lower": 2300, "price_upper": 2400, "grid_count": 10,
            "grid_mode": "arithmetic", "grid_side": "long", "lot_per_grid": 0.01,
        }],
    })
    assert outcome["result"] == "rejected"


async def test_send_update_ack_emits_payload():
    sent: list = []
    runner, _hub = _runner(sent)
    await runner.send_update_ack(
        {"result": "partial", "fields": [{"field": "f", "status": "locked",
                                         "message": "锁定"}]},
        config_version=3,
    )
    acks = [m for m in sent if m.get("type") == "strategy_update_ack"]
    assert len(acks) == 1
    data = acks[0]["data"]
    assert data["task_id"] == 1
    assert data["config_version"] == 3
    assert data["result"] == "partial"
    assert data["fields"][0]["status"] == "locked"
    # 回执顺带带上加仓进度
    assert "progress" in data


# ----------------------------------------------------------------------
# 执行器：配置无条件热推
# ----------------------------------------------------------------------
def test_apply_strategy_takes_new_limit_below_added():
    """上限缩到已开笔数以下：新值必须被采纳（不保留旧值）。

    保留旧值等于偷偷改了用户填的数字，而判定侧现成的
    `rule_adds >= limit` 会自然停止加仓。
    """
    sent: list = []
    runner, _hub = _runner(sent)
    runner.add_counts[1] = 3
    outcome = runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_rule(1, total_lot_limit=2)],
    })
    assert outcome["result"] == "partial"
    assert outcome["fields"][0]["exec"] == "no_more_adds"
    effective = runner.strategy["rules"][0]
    assert effective["total_lot_limit"] == 2, "新值必须被采纳"


def test_apply_strategy_marks_align_pending():
    """热推后要立刻做一次加仓对齐（扩容不等价格触发）。"""
    sent: list = []
    runner, _hub = _runner(sent)
    assert runner._add_align_pending is False
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_rule(1, total_lot_limit=8)],
    })
    assert runner._add_align_pending is True


def test_add_progress_reports_level_and_remaining():
    """进度上报：当前第几批、还能再加几笔、下一批参数。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner.strategy = {
        "template_id": "tpl_1",
        "rules": [_rule(1, batch_enabled=True, total_lot_limit=5, batch_levels=[
            {"pos_from": 1, "pos_to": 3, "point": 100, "lot_times": 1.1, "extra_lot": 0},
            {"pos_from": 4, "pos_to": 5, "point": 200, "lot_times": 1.2, "extra_lot": 0},
        ])],
    }
    runner.add_counts[1] = 3
    prog = runner._add_progress()["1"]
    assert prog["added"] == 3
    assert prog["limit"] == 5
    assert prog["remaining"] == 2
    assert prog["current_level_index"] == 0
    assert prog["next_level_index"] == 1
    assert prog["next_gap_points"] == 200
    # 手数 = 倍数 × 基础手数（0.1）+ 额外手数
    assert abs(prog["next_volume"] - 0.12) < 1e-6


def test_add_progress_remaining_is_minus_one_when_unlimited():
    """上限 0（不限）时 remaining 必须是 -1，不能写 0（会误导成不能再加）。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner.strategy = {
        "template_id": "tpl_1",
        "rules": [_rule(1, batch_enabled=True, total_lot_limit=0, batch_levels=[
            {"pos_from": 1, "pos_to": 3, "point": 100, "lot_times": 1.1, "extra_lot": 0},
        ])],
    }
    runner.add_counts[1] = 1
    prog = runner._add_progress()["1"]
    assert prog["limit"] == 0
    assert prog["remaining"] == -1


def test_snapshot_includes_add_progress():
    """_snapshot 里带 add_progress（心跳上报要用）。"""
    sent: list = []
    runner, _hub = _runner(sent)
    snap = runner._snapshot([])
    assert "add_progress" in snap


def test_counter_no_next_reason_distinguishes_limit():
    """撤单原因要区分「上限已低于已开笔数」与「档位用尽」。"""
    sent: list = []
    runner, _hub = _runner(sent)
    runner.strategy = {
        "template_id": "tpl_1",
        "rules": [_rule(1, total_lot_limit=2)],
    }
    runner.add_counts[1] = 3

    class _Ctx:
        def rule_add_count(self, rt):
            return 3

    reason = runner._counter_no_next_reason(_Ctx())
    assert "不再新增批次" in reason
    assert "上限" in reason


# ----------------------------------------------------------------------
# 信号盈亏控制：逐字段结论 + 去重
# ----------------------------------------------------------------------
def _pl_rule(**over) -> dict:
    rule = _rule(1, float_pl_ratio={
        "enabled": True, "ratio": -20.0, "monitor_mode": "loop", "max_times": 2,
    }, lot_pl_tiers={
        "enabled": True, "batch_count": 2, "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 50.0},
            {"min_lot": 0.5, "pl_amount": 100.0},
        ],
    })
    rule.update(over)
    return rule


def _pl_fields(fields: list, field: str) -> list[dict]:
    return [f for f in fields if f["field"] == field]


def test_pl_tiers_batch1_amount_reports_field():
    """批次1的盈亏金额是实际生效的那个，必须出现在回执里。"""
    old = _snap([_pl_rule()])
    new = _snap([_pl_rule(lot_pl_tiers={
        "enabled": True, "batch_count": 2, "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 80.0},
            {"min_lot": 0.5, "pl_amount": 100.0},
        ],
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    got = _pl_fields(fields, "rules[lot_pl_tiers].tiers[0].pl_amount")
    assert len(got) == 1
    assert got[0]["status"] == "applied"
    assert got[0]["old"] == 50.0
    assert got[0]["new"] == 80.0
    assert "实际生效" in got[0]["message"]


def test_pl_tiers_higher_batch_marks_not_effective():
    """批次2+ 当前不参与共同止盈计算，要如实标注。"""
    old = _snap([_pl_rule()])
    new = _snap([_pl_rule(lot_pl_tiers={
        "enabled": True, "batch_count": 2, "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 50.0},
            {"min_lot": 0.5, "pl_amount": 300.0},
        ],
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    got = _pl_fields(fields, "rules[lot_pl_tiers].tiers[1].pl_amount")
    assert len(got) == 1
    assert "暂不生效" in got[0]["message"]


def test_pl_ratio_enabled_off_says_stop_kept():
    """关开关要说清「不会自动摘除已有止损」。"""
    old = _snap([_pl_rule()])
    new = _snap([_pl_rule(float_pl_ratio={
        "enabled": False, "ratio": -20.0, "monitor_mode": "loop", "max_times": 2,
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    got = _pl_fields(fields, "rules[float_pl_ratio].enabled")
    assert len(got) == 1
    assert "不会自动摘除" in got[0]["message"]


def test_pl_multiple_fields_all_reported():
    """同时改开关 + 比例 + 次数：三条都要出现（早期版本只报一条）。"""
    old = _snap([_pl_rule(float_pl_ratio={
        "enabled": False, "ratio": -20.0, "monitor_mode": "loop", "max_times": 2,
    })])
    new = _snap([_pl_rule(float_pl_ratio={
        "enabled": True, "ratio": -30.0, "monitor_mode": "times", "max_times": 5,
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    assert len(_pl_fields(fields, "rules[float_pl_ratio].enabled")) == 1
    assert len(_pl_fields(fields, "rules[float_pl_ratio].ratio")) == 1
    assert len(_pl_fields(fields, "rules[float_pl_ratio].max_times")) == 1


def test_pl_tiers_close_action_and_min_lot_reported():
    old = _snap([_pl_rule()])
    new = _snap([_pl_rule(lot_pl_tiers={
        "enabled": True, "batch_count": 2, "close_action": "buy",
        "tiers": [
            {"min_lot": 0.2, "pl_amount": 50.0},
            {"min_lot": 0.5, "pl_amount": 100.0},
        ],
    })])
    fields, _effective = sr.reconcile_snapshot(old, new)
    assert len(_pl_fields(fields, "rules[lot_pl_tiers].close_action")) == 1
    assert len(_pl_fields(fields, "rules[lot_pl_tiers].tiers[0].min_lot")) == 1


def test_pl_config_deduped_across_rules():
    """逆势 / 顺势两条规则带同一份盈亏配置：回执只报一次。"""
    old = _snap([_pl_rule(), _pl_rule(type=2)])
    new = _snap([
        _pl_rule(float_pl_ratio={
            "enabled": True, "ratio": -40.0, "monitor_mode": "loop", "max_times": 2,
        }),
        _pl_rule(type=2, float_pl_ratio={
            "enabled": True, "ratio": -40.0, "monitor_mode": "loop", "max_times": 2,
        }),
    ])
    fields, _effective = sr.reconcile_snapshot(old, new)
    assert len(_pl_fields(fields, "rules[float_pl_ratio].ratio")) == 1


# --------------------------- 指定价档位的锁定 ---------------------------
def _price_level_rule(rule_type: int = 1, **over) -> dict:
    """带一个指定价档位的规则。"""
    rule = _rule(rule_type, batch_enabled=True, batch_count=1, batch_levels=[{
        "pos_from": 1, "pos_to": 5, "calc_type": "price",
        "price": 4390.0, "price_tp": 4410.0, "price_sl": 4380.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }])
    rule.update(over)
    return rule


def test_price_level_change_is_applied_when_still_pending():
    """在途未成交 → 改动采纳（节点会撤单重挂）。"""
    old = _snap([_price_level_rule()])
    new = _snap([_price_level_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 5, "calc_type": "price",
        "price": 4380.0, "price_tp": 4410.0, "price_sl": 4380.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }])])
    fields, effective = sr.reconcile_snapshot(old, new)
    hits = [f for f in fields if f.get("field", "").endswith("batch_levels[0].price")]
    assert len(hits) == 1
    assert hits[0]["status"] == "applied"
    assert "撤掉盘上的挂单" in hits[0]["message"]
    # 生效配置用新值
    assert effective["rules"][0]["batch_levels"][0]["price"] == 4380.0


def test_price_level_change_is_locked_after_fill():
    """已成交 → 改动锁定，不影响已开仓位。"""
    old = _snap([_price_level_rule()])
    new = _snap([_price_level_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 5, "calc_type": "price",
        "price": 4380.0, "price_tp": 4410.0, "price_sl": 4380.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }])])
    fields, effective = sr.reconcile_snapshot(
        old, new, price_locked={(1, 0)},
    )
    hits = [f for f in fields if f.get("field", "").endswith("batch_levels[0].price")]
    assert len(hits) == 1
    assert hits[0]["status"] == "locked"
    assert hits[0]["exec"] == sr.EXEC_LOCKED_BY_PRICE_LEVEL
    # 生效配置保留旧值（已开仓位不受影响）
    assert effective["rules"][0]["batch_levels"][0]["price"] == 4390.0


def test_price_level_locked_only_applies_to_locked_levels():
    """锁定的只有那一档；没成交的档位照样能改。"""
    old = _snap([_price_level_rule()])
    new = _snap([_price_level_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 5, "calc_type": "price",
        "price": 4380.0, "price_tp": 4410.0, "price_sl": 4380.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }])])
    # 锁的是档位1（不存在），档位0 没锁
    fields, effective = sr.reconcile_snapshot(
        old, new, price_locked={(1, 1)},
    )
    hits = [f for f in fields if f.get("field", "").endswith("batch_levels[0].price")]
    assert len(hits) == 1
    assert hits[0]["status"] == "applied"
    assert effective["rules"][0]["batch_levels"][0]["price"] == 4380.0


def test_price_level_stop_loss_change_is_locked_after_fill():
    """已成交档位的自有止损也锁定（它是那笔仓位上的防线）。"""
    old = _snap([_price_level_rule()])
    new = _snap([_price_level_rule(batch_levels=[{
        "pos_from": 1, "pos_to": 5, "calc_type": "price",
        "price": 4390.0, "price_tp": 4410.0, "price_sl": 4370.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }])])
    fields, effective = sr.reconcile_snapshot(
        old, new, price_locked={(1, 0)},
    )
    hits = [f for f in fields if f.get("field", "").endswith("batch_levels[0].price_sl")]
    assert len(hits) == 1
    assert hits[0]["status"] == "locked"
    assert effective["rules"][0]["batch_levels"][0]["price_sl"] == 4380.0
