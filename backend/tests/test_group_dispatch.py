"""分组分发引擎集成测试（fakeredis + SQLite，无需外部服务）。

覆盖 strategy 链路的核心行为：
- 所有启用分组各自处理同一条信号；禁用分组不参与；
- 有效节点口径 = 已启用 + 在线；
- 每个节点子任务自持魔术号（由子任务号派生），随命令下发；
- 互斥粒度是 (分组, 节点)：同分组内一个节点只跑一个子任务，跨分组各自独立；
- 主任务与各节点子任务落库，节点回报按魔术号回填并收口状态；
- 分组 sync / poll 两种分发模式；
- 与 normal 链路的规则隔离（不受中控台品种配置、区间/持仓过滤、节点按币种配置影响）。
"""
import fakeredis
import pytest
from sqlalchemy import delete, select

from app import group_persist, group_rules, group_service
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.models import GroupCreate, GroupUpdate
from app.orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    NodeGroup,
    NodeGroupMember,
    SignalDispatch,
    SignalHistory,
    TradingStrategy,
)
from app.parser import TradingSignal
from app.redis_store import RedisStore
from app.strategy_templates import (
    RULE_TYPE_GRID,
    RULE_TYPE_RISK_SIZED,
    TEMPLATE_1_ID,
    TEMPLATE_1_NAME,
    TEMPLATE_2_ID,
    TEMPLATE_2_NAME,
    TEMPLATE_3_ID,
    TEMPLATE_3_NAME,
)

# 分组链路必须真实落库（子任务号由数据库自增，是魔术号的来源），
# 所以这里不像 test_dispatch.py 那样 mock 掉持久化，只用 conftest 里的 SQLite 测试库。
_TABLES = (
    GroupTaskEvent,
    GroupTaskDispatch,
    GroupSignalTask,
    NodeGroupMember,
    NodeGroup,
    TradingStrategy,
    SignalDispatch,
    SignalHistory,
)


@pytest.fixture
async def store():
    """建表并清空分组链路相关表，返回挂了 fakeredis 的 store。"""
    await init_db()
    async with SessionLocal() as s:
        for table in _TABLES:
            await s.execute(delete(table))
        await s.commit()
    return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture(autouse=True)
def clean_manager():
    manager.nodes.clear()
    manager.admins.clear()
    yield
    manager.nodes.clear()
    manager.admins.clear()


def mk_node(node_id, *, enabled=True, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": enabled,
        "mt5_login": abs(hash(node_id)) % 100000,
        # 故意不配置任何按币种 filters：分组链路不读节点按币种配置
        "filters": None,
        "created_at": 0,
    }
    d.update(kw)
    return d


async def online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        manager.nodes[n["node_id"]] = object()


async def offline_node(store, *nodes):
    """节点已入库但未建立连接（离线）。"""
    for n in nodes:
        await store.cache_node(n)


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", name=None, enabled=True, rules=None,
                      template_id=TEMPLATE_1_ID, template_name=TEMPLATE_1_NAME):
    """建一条可绑定的策略（分组必须绑定策略才会参与 strategy 信号分发）。"""
    sid = f"sty_t{next(_strategy_seq)}"
    row = {
        "strategy_id": sid,
        "name": name or f"策略{sid}",
        "template_id": template_id,
        "template_name": template_name,
        "symbol": symbol,
        "enabled": enabled,
        "rules": rules if rules is not None else [],
        "remark": None,
        "created_at": 0,
    }
    async with SessionLocal() as s:
        s.add(
            TradingStrategy(
                strategy_id=sid,
                name=row["name"],
                template_id=template_id,
                template_name=template_name,
                symbol=symbol,
                enabled=enabled,
                config_json=row["rules"],
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def mk_group(store, name, node_ids, *, mode="sync", enabled=True,
                   symbol="XAUUSD", strategy=None, bind_strategy=True):
    """建分组；默认自动绑定一条同品种策略，使其能接收 strategy 信号。"""
    strategy_id = None
    if bind_strategy:
        strategy = strategy or await mk_strategy(store, symbol=symbol)
        strategy_id = strategy["strategy_id"]
    return await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=enabled, dispatch_mode=mode,
            strategy_id=strategy_id, node_ids=list(node_ids),
        ),
    )


def capture_sender(sent, *, ok=True):
    async def fake_send(node_id, msg):
        sent.append((node_id, msg))
        return ok

    return fake_send


async def fetch_tasks(signal_id):
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(GroupSignalTask)
                .where(GroupSignalTask.signal_id == signal_id)
                .order_by(GroupSignalTask.task_id.asc())
            )
        ).scalars().all()


async def fetch_dispatches(task_id):
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(GroupTaskDispatch)
                .where(GroupTaskDispatch.task_id == task_id)
                .order_by(GroupTaskDispatch.id.asc())
            )
        ).scalars().all()


async def open_first_orders(task_id, node_ids, *, success=True):
    """模拟各节点回报首单结果（成功后子任务进入 opened，不代表任务完成）。"""
    for i, node_id in enumerate(node_ids):
        await group_persist.update_dispatch_result(
            node_id=node_id,
            result=(
                {"success": True, "order": 9000 + i, "price": 2400.0 + i}
                if success else {"success": False, "error": "x"}
            ),
            task_id=task_id,
        )


async def release_locks(store, result):
    """按持久化层回传的信息释放组内节点占位（ws_gateway 在真实链路里做这件事）。"""
    for group_id, node_id in (result or {}).get("released", []):
        await store.release_group_node_busy(group_id, node_id)


async def finish_task(store, task_id, node_ids, *, status="done"):
    """模拟任务全流程收口：首单成交 -> 持仓平掉 -> 释放节点占位。"""
    await open_first_orders(task_id, node_ids)
    for node_id in node_ids:
        res = await group_persist.finish_subtask(
            node_id=node_id, task_id=task_id,
            data={"status": status, "reason": "positions_cleared"},
        )
        await release_locks(store, res)


async def magics_of(task_id):
    """某主任务下各节点子任务的魔术号。"""
    return {r.node_id: r.magic for r in await fetch_dispatches(task_id)}


# =====================================================================
# 1. 所有启用分组都处理同一条信号
# =====================================================================
async def test_every_enabled_group_processes_signal(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "组一", ["nd_a"])
    await mk_group(store, "组二", ["nd_b"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.2), "sig_g1",
    )

    assert res["mode"] == "group"
    assert res["groups"] == 2
    assert res["targets"] == 2
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}
    # 两个分组各生成一条主任务，任务号互不相同
    tasks = await fetch_tasks("sig_g1")
    assert len(tasks) == 2
    assert {t.group_name for t in tasks} == {"组一", "组二"}
    assert len({t.task_id for t in tasks}) == 2
    # 魔术号在子任务上，每个节点各持一个且互不相同
    magics = [m for t in tasks for m in (await magics_of(t.task_id)).values()]
    assert len(set(magics)) == 2


async def test_disabled_group_is_not_processed(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "启用组", ["nd_a"])
    await mk_group(store, "禁用组", ["nd_b"], enabled=False)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g2",
    )

    assert res["groups"] == 1
    assert [s[0] for s in sent] == ["nd_a"]
    tasks = await fetch_tasks("sig_g2")
    assert [t.group_name for t in tasks] == ["启用组"]


async def test_rejected_when_no_enabled_group(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g3",
    )

    assert res["mode"] == "rejected"
    assert res["targets"] == 0
    assert "无匹配分组" in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_g3") == []


async def test_group_without_strategy_is_skipped(store, monkeypatch):
    """未绑定策略的分组不参与 strategy 信号分发。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "裸分组", ["nd_a"], bind_strategy=False)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_nostrategy",
    )

    assert res["mode"] == "rejected"
    assert "未绑定策略" in res["reason"]
    assert sent == []


async def test_group_with_other_symbol_strategy_is_skipped(store, monkeypatch):
    """策略绑定品种与信号品种不符的分组不参与。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "欧美组", ["nd_a"], symbol="EURUSD")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_symdiff",
    )

    assert res["mode"] == "rejected"
    assert "不符" in res["reason"]
    assert sent == []


async def mk_risk_sized_group(store, name, node_ids, *, symbol="XAUUSD", **rule_over):
    """建一个绑定模版2（以损定量趋势单）策略的分组。"""
    rule = {
        "type": RULE_TYPE_RISK_SIZED, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0,
    }
    rule.update(rule_over)
    strategy = await mk_strategy(
        store, symbol=symbol, name=f"{name}策略", rules=[rule],
        template_id=TEMPLATE_2_ID, template_name=TEMPLATE_2_NAME,
    )
    return await mk_group(store, name, node_ids, symbol=symbol, strategy=strategy)


async def test_risk_sized_signal_with_stop_loss_is_dispatched(store, monkeypatch):
    """模版2：信号带止损价时正常下发，规则快照随命令一起送到节点。"""
    await online(store, mk_node("nd_a"))
    await mk_risk_sized_group(store, "以损定量组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0),
        "sig_risk_ok",
    )

    assert res["mode"] == "group"
    assert res["targets"] == 1
    cmd = sent[0][1]
    assert cmd["cmd"] == "strategy_start"
    assert cmd["entry"]["stop_loss"] == 2397.0
    assert cmd["strategy"]["template_id"] == TEMPLATE_2_ID
    rule = cmd["strategy"]["rules"][0]
    assert rule["type"] == RULE_TYPE_RISK_SIZED
    assert rule["risk_amount"] == 300.0
    assert rule["rr_ratio"] == 2.5


async def test_risk_sized_signal_without_stop_loss_is_rejected(store, monkeypatch):
    """模版2 的手数靠止损距离反推，没有止损就在分发前挡住，不让节点白跑一趟。"""
    await online(store, mk_node("nd_a"))
    await mk_risk_sized_group(store, "缺止损组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_risk_nosl",
    )

    assert res["mode"] == "rejected"
    assert "止损价" in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_risk_nosl") == []


async def test_add_on_strategy_still_dispatches_without_stop_loss(store, monkeypatch):
    """模版1 首单手数来自信号，没有止损也照常下发——新准入不能影响它。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "加仓组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_addon_nosl",
    )

    assert res["mode"] == "group"
    assert res["targets"] == 1


async def test_risk_sized_group_skipped_only_for_itself(store, monkeypatch):
    """缺止损时只有模版2 的分组落选，同品种的模版1 分组照常入选。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "混合-加仓组", ["nd_a"])
    await mk_risk_sized_group(store, "混合-以损定量组", ["nd_b"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_mixed_nosl",
    )

    assert res["groups"] == 1
    assert [s[0] for s in sent] == ["nd_a"]


async def mk_grid_group(store, name, node_ids, *, symbol="XAUUSD", **rule_over):
    """建一个绑定模版3（网格交易）策略的分组。"""
    rule = {
        "type": RULE_TYPE_GRID, "status": 1, "action": "all",
        "price_lower": 2300.0, "price_upper": 2400.0,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
        "trailing_up": False, "trailing_max": 0,
    }
    rule.update(rule_over)
    strategy = await mk_strategy(
        store, symbol=symbol, name=f"{name}策略", rules=[rule],
        template_id=TEMPLATE_3_ID, template_name=TEMPLATE_3_NAME,
    )
    return await mk_group(store, name, node_ids, symbol=symbol, strategy=strategy)


async def test_grid_signal_dispatches_with_rule_snapshot(store, monkeypatch):
    """模版3：网格参数随 strategy_start 下发，节点靠它决定走网格执行路径。"""
    await online(store, mk_node("nd_a"))
    await mk_grid_group(store, "网格组", ["nd_a"], trailing_up=True, trailing_max=5)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_grid_ok",
    )

    assert res["mode"] == "group"
    assert res["targets"] == 1
    cmd = sent[0][1]
    assert cmd["cmd"] == "strategy_start"
    assert cmd["strategy"]["template_id"] == TEMPLATE_3_ID
    rule = cmd["strategy"]["rules"][0]
    assert rule["type"] == RULE_TYPE_GRID
    assert rule["price_lower"] == 2300.0
    assert rule["price_upper"] == 2400.0
    assert rule["grid_count"] == 10
    assert rule["trailing_up"] is True
    assert rule["trailing_max"] == 5


async def test_grid_signal_needs_no_stop_loss(store, monkeypatch):
    """网格不靠止损距离算手数，缺 sl 也照常下发（与模版2 相反）。"""
    await online(store, mk_node("nd_a"))
    await mk_grid_group(store, "无止损网格组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_grid_nosl",
    )

    assert res["mode"] == "group"
    assert res["targets"] == 1


async def test_grid_long_rejects_sell_signal(store, monkeypatch):
    """只做多的网格收到 SELL 信号应在分发前挡下，落选原因写进信号记录。"""
    await online(store, mk_node("nd_a"))
    await mk_grid_group(store, "只做多网格组", ["nd_a"], grid_side="long")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "sig_grid_sell",
    )

    assert res["mode"] == "rejected"
    assert "只做多" in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_grid_sell") == []


async def test_grid_bad_range_is_rejected(store, monkeypatch):
    """区间非法的网格跑不起来，不必让节点收到命令后再失败一次。"""
    await online(store, mk_node("nd_a"))
    await mk_grid_group(store, "坏区间网格组", ["nd_a"], price_lower=0.0, price_upper=0.0)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_grid_badrange",
    )

    assert res["mode"] == "rejected"
    assert "价格区间" in res["reason"]
    assert sent == []


async def test_symbol_match_tolerates_broker_suffix(store, monkeypatch):
    """券商后缀差异（XAUUSDm）仍应视为同一标的。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "后缀组", ["nd_a"], symbol="XAUUSDm")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_suffix",
    )

    assert res["mode"] == "group"
    assert [s[0] for s in sent] == ["nd_a"]


async def test_busy_node_is_skipped_on_second_signal(store, monkeypatch):
    """同一分组内该节点已有进行中的子任务时，新信号在该节点上被跳过。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "互斥组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)

    first = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_busy1",
    )
    assert first["tasks"][0]["status"] == "dispatching"

    second = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_busy2",
    )
    assert second["targets"] == 0
    assert second["tasks"][0]["status"] == "skipped"
    assert "均有进行中的任务" in second["tasks"][0]["reason"]
    assert len(sent) == 1  # 第二条没有下发
    # 主任务仍留痕，子任务记为 skipped 且不分配魔术号
    task2 = (await fetch_tasks("sig_busy2"))[0]
    rows = await fetch_dispatches(task2.task_id)
    assert [r.status for r in rows] == ["skipped"]
    assert rows[0].magic is None
    assert "进行中的子任务" in rows[0].skip_reason


async def test_node_accepts_new_signal_after_subtask_finished(store, monkeypatch):
    """子任务收口并释放占位后，该节点在本分组内能接收下一条信号。"""
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "释放组", ["nd_a"])
    gid = group["group_id"]
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)

    await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_free1",
    )
    task = (await fetch_tasks("sig_free1"))[0]
    await finish_task(store, task.task_id, ["nd_a"])
    assert await store.get_group_node_busy(gid, "nd_a") is None

    res = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_free2",
    )
    assert res["tasks"][0]["status"] == "dispatching"
    assert len(await fetch_tasks("sig_free2")) == 1
    assert await store.get_group_node_busy(gid, "nd_a") is not None


async def test_same_node_serves_multiple_groups_in_parallel(store, monkeypatch):
    """互斥按 (分组, 节点)：同一节点可以同时承接多个分组的任务。"""
    await online(store, mk_node("nd_a"))
    gold = await mk_group(store, "黄金组", ["nd_a"], symbol="XAUUSD")
    euro = await mk_group(store, "欧美组", ["nd_a"], symbol="EURUSD")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    dispatcher = GroupDispatcher(store)

    first = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_par1",
    )
    second = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0.1), "sig_par2",
    )

    assert first["targets"] == 1
    assert second["targets"] == 1
    assert len(sent) == 2
    assert await store.get_group_node_busy(gold["group_id"], "nd_a") is not None
    assert await store.get_group_node_busy(euro["group_id"], "nd_a") is not None


async def test_second_group_same_symbol_also_dispatches(store, monkeypatch):
    """两个分组绑同品种策略、成员相同：各自独立下发。

    这是 (分组, 节点) 粒度的直接后果——同一账户同品种会持有两笔独立持仓
    （魔术号不同、各按自己那份策略参数加仓）。
    """
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "模板一组", ["nd_a", "nd_b"], symbol="XAUUSD")
    await mk_group(store, "模板二组", ["nd_a", "nd_b"], symbol="XAUUSD")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_two_groups",
    )

    assert res["groups"] == 2
    assert res["targets"] == 4
    assert [t["status"] for t in res["tasks"]] == ["dispatching", "dispatching"]
    # 每个节点收到两条命令，魔术号互不相同
    assert len(sent) == 4
    assert len({m["magic"] for _n, m in sent}) == 4
    per_node: dict[str, int] = {}
    for node_id, _msg in sent:
        per_node[node_id] = per_node.get(node_id, 0) + 1
    assert per_node == {"nd_a": 2, "nd_b": 2}


async def test_different_nodes_across_groups_both_run(store, monkeypatch):
    """成员不重叠时，两个分组的同品种任务互不阻塞。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "甲组", ["nd_a"], symbol="XAUUSD")
    await mk_group(store, "乙组", ["nd_b"], symbol="XAUUSD")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_disjoint",
    )

    assert res["targets"] == 2
    assert [t["status"] for t in res["tasks"]] == ["dispatching", "dispatching"]
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}


# ---------------------- 策略模版定向（template_ids）----------------------
async def prepare_template_groups(store, monkeypatch):
    """同品种下各建一个模版1 / 模版2 / 模版3 的分组，返回下发记录列表。"""
    await online(store, mk_node("nd_1"), mk_node("nd_2"), mk_node("nd_3"))
    await mk_group(store, "模版一组", ["nd_1"])                       # tpl_1
    await mk_risk_sized_group(store, "模版二组", ["nd_2"])            # tpl_2
    await mk_grid_group(store, "模版三组", ["nd_3"])                  # tpl_3
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    return sent


async def test_template_ids_only_dispatches_to_bound_templates(store, monkeypatch):
    """信号带 template_ids 时，只有绑定了这些模版的分组接收。"""
    sent = await prepare_template_groups(store, monkeypatch)

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0,
            template_ids=[TEMPLATE_2_ID, TEMPLATE_3_ID],
        ),
        "sig_tpl_pick",
    )

    assert res["mode"] == "group"
    assert res["groups"] == 2
    assert {s[0] for s in sent} == {"nd_2", "nd_3"}
    assert {t.group_name for t in await fetch_tasks("sig_tpl_pick")} == {"模版二组", "模版三组"}


async def test_empty_template_ids_keeps_all_groups(store, monkeypatch):
    """不带 template_ids（或空数组）时行为不变：所有匹配分组照常接收。"""
    sent = await prepare_template_groups(store, monkeypatch)

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0, template_ids=[],
        ),
        "sig_tpl_all",
    )

    assert res["groups"] == 3
    assert {s[0] for s in sent} == {"nd_1", "nd_2", "nd_3"}


async def test_template_ids_matching_nothing_is_rejected(store, monkeypatch):
    """指定的模版在本环境没有对应分组：整条信号拒收，落选原因写明模版不符。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "模版一组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, template_ids=[TEMPLATE_3_ID],
        ),
        "sig_tpl_miss",
    )

    assert res["mode"] == "rejected"
    assert TEMPLATE_1_NAME in res["reason"] and TEMPLATE_3_ID in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_tpl_miss") == []


async def test_close_signal_only_stops_targeted_templates(store, monkeypatch):
    """CLOSE 同样受模版定向约束：只终止指定模版分组里的任务。"""
    await online(store, mk_node("nd_1"), mk_node("nd_3"))
    await mk_group(store, "模版一组", ["nd_1"])
    await mk_grid_group(store, "模版三组", ["nd_3"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_tpl_open")
    for task in await fetch_tasks("sig_tpl_open"):
        await open_first_orders(task.task_id, [r.node_id for r in await fetch_dispatches(task.task_id)])
    sent.clear()

    res = await d.dispatch(
        TradingSignal(
            action="CLOSE", symbol="XAUUSD", volume=0.1, template_ids=[TEMPLATE_3_ID],
        ),
        "sig_tpl_close",
    )

    assert res["mode"] == "group_close"
    assert [s[0] for s in sent] == ["nd_3"]
    assert sent[0][1]["cmd"] == "strategy_stop"


# ------------------------ 分组定向（group_ids）------------------------
async def test_group_ids_only_dispatches_to_named_groups(store, monkeypatch):
    """信号带 group_ids 时，只有被点名的分组接收。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"), mk_node("nd_c"))
    await mk_group(store, "甲组", ["nd_a"])
    lucky = await mk_group(store, "乙组", ["nd_b"])
    await mk_group(store, "丙组", ["nd_c"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, group_ids=[lucky["group_id"]],
        ),
        "sig_grp_pick",
    )

    assert res["mode"] == "group"
    assert res["groups"] == 1
    assert [s[0] for s in sent] == ["nd_b"]
    assert [t.group_name for t in await fetch_tasks("sig_grp_pick")] == ["乙组"]


async def test_empty_group_ids_keeps_all_groups(store, monkeypatch):
    """不带 group_ids（或空数组）时行为不变：所有匹配分组照常接收。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "甲组", ["nd_a"])
    await mk_group(store, "乙组", ["nd_b"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, group_ids=[]),
        "sig_grp_all",
    )

    assert res["groups"] == 2
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}


async def test_group_ids_and_template_ids_are_intersected(store, monkeypatch):
    """两个定向字段是「与」：点名的分组还必须命中模版定向。"""
    await online(store, mk_node("nd_1"), mk_node("nd_3"))
    plain = await mk_group(store, "模版一组", ["nd_1"])
    grid = await mk_grid_group(store, "模版三组", ["nd_3"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1,
            group_ids=[plain["group_id"], grid["group_id"]],
            template_ids=[TEMPLATE_3_ID],
        ),
        "sig_grp_tpl",
    )

    assert res["groups"] == 1
    assert [s[0] for s in sent] == ["nd_3"]


async def test_unknown_group_id_is_named_in_reason(store, monkeypatch):
    """点名了不存在的分组：整条信号拒收，原因里点出是哪个 ID。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, group_ids=["grp_none"]),
        "sig_grp_miss",
    )

    assert res["mode"] == "rejected"
    assert "指定的分组不存在：grp_none" in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_grp_miss") == []


async def test_targeted_disabled_group_is_explained(store, monkeypatch):
    """点名了已禁用的分组：不能只回一句「无匹配分组」，要说清是被禁用了。"""
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "停用组", ["nd_a"], enabled=False)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, group_ids=[group["group_id"]],
        ),
        "sig_grp_disabled",
    )

    assert res["mode"] == "rejected"
    assert "停用组：分组已禁用" in res["reason"]
    assert sent == []


async def test_untargeted_groups_stay_out_of_reason(store, monkeypatch):
    """落选说明只讲被点名的分组，否则一条定向信号会带回一堆无关原因。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "黄金组", ["nd_a"], symbol="XAUUSD")
    euro = await mk_group(store, "欧美组", ["nd_b"], symbol="EURUSD")
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(
            action="BUY", symbol="XAUUSD", volume=0.1, group_ids=[euro["group_id"]],
        ),
        "sig_grp_quiet",
    )

    assert res["mode"] == "rejected"
    assert "欧美组" in res["reason"] and "不符" in res["reason"]
    assert "黄金组" not in res["reason"]


async def test_close_signal_only_stops_named_groups(store, monkeypatch):
    """CLOSE 同样受分组定向约束：只终止被点名分组里的任务。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "甲组", ["nd_a"])
    target = await mk_group(store, "乙组", ["nd_b"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_grp_open")
    for task in await fetch_tasks("sig_grp_open"):
        await open_first_orders(task.task_id, [r.node_id for r in await fetch_dispatches(task.task_id)])
    sent.clear()

    res = await d.dispatch(
        TradingSignal(
            action="CLOSE", symbol="XAUUSD", volume=0.1, group_ids=[target["group_id"]],
        ),
        "sig_grp_close",
    )

    assert res["mode"] == "group_close"
    assert [s[0] for s in sent] == ["nd_b"]
    assert sent[0][1]["cmd"] == "strategy_stop"


# =====================================================================
# 2. 有效节点 = 已启用 + 在线
# =====================================================================
async def test_group_without_effective_node_records_skipped_task(store, monkeypatch):
    await offline_node(store, mk_node("nd_off"))          # 已启用但离线
    await online(store, mk_node("nd_dis", enabled=False))  # 在线但被禁用
    await mk_group(store, "无效组", ["nd_off", "nd_dis"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g4",
    )

    assert res["targets"] == 0
    assert sent == []
    tasks = await fetch_tasks("sig_g4")
    assert len(tasks) == 1
    assert tasks[0].status == "skipped"
    assert "无有效节点" in tasks[0].skip_reason
    assert tasks[0].node_count == 0
    assert await fetch_dispatches(tasks[0].task_id) == []


async def test_only_effective_members_receive_command(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_dis", enabled=False))
    await offline_node(store, mk_node("nd_off"))
    await mk_group(store, "混合组", ["nd_a", "nd_dis", "nd_off"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g5",
    )

    assert [s[0] for s in sent] == ["nd_a"]
    tasks = await fetch_tasks("sig_g5")
    assert tasks[0].node_ids_json == ["nd_a"]
    assert tasks[0].node_count == 1


# =====================================================================
# 3. 子任务号即魔术号，随命令下发
# =====================================================================
async def test_subtask_id_is_used_as_magic_number(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "魔术号组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.3), "sig_g6",
    )

    task = (await fetch_tasks("sig_g6"))[0]
    row = (await fetch_dispatches(task.task_id))[0]
    cmd = sent[0][1]
    assert cmd["cmd"] == "strategy_start"
    assert cmd["magic"] == row.magic == group_rules.subtask_magic(row.id)
    assert cmd["dispatch_id"] == row.id
    assert cmd["task_id"] == task.task_id
    assert cmd["group_id"] == task.group_id
    assert cmd["model"] == "strategy"
    assert cmd["entry"]["volume"] == 0.3
    assert cmd["entry"]["action"] == "BUY"
    # 策略规则随命令快照下发，节点据此常驻加仓
    assert cmd["strategy"]["strategy_id"] == task.strategy_id
    # 主任务落的是公共下发数据，魔术号按节点各自不同，故不在其中
    assert task.payload_json["cmd"] == "strategy_start"
    assert "magic" not in task.payload_json


async def test_each_node_gets_its_own_magic(store, monkeypatch):
    """sync 模式下每个节点持有独立魔术号，互不共用。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "独立魔术号组", ["nd_a", "nd_b"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_magic_each",
    )

    task = (await fetch_tasks("sig_magic_each"))[0]
    by_node = await magics_of(task.task_id)
    assert len(set(by_node.values())) == 2
    # 下发的命令里带的就是各自那一个
    for node_id, msg in sent:
        assert msg["magic"] == by_node[node_id]


async def test_subtask_created_before_dispatch(store, monkeypatch):
    """下发到节点时子任务必须已经落库（子任务号是魔术号的来源）。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "顺序组", ["nd_a"])
    seen_at_send = {}

    async def fake_send(node_id, msg):
        task = (await fetch_tasks("sig_g7"))[0]
        seen_at_send["rows"] = await fetch_dispatches(task.task_id)
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g7",
    )
    assert len(seen_at_send["rows"]) == 1
    assert seen_at_send["rows"][0].magic is not None


# =====================================================================
# 4. 明细落库 + 回报回填
# =====================================================================
async def test_dispatch_rows_recorded_per_node(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "明细组", ["nd_a", "nd_b"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.15), "sig_g8",
    )

    task = (await fetch_tasks("sig_g8"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert {r.node_id for r in rows} == {"nd_a", "nd_b"}
    assert all(r.status == "sent" for r in rows)
    assert all(r.decided_vol == 0.15 for r in rows)
    assert all(r.symbol == "XAUUSD" for r in rows)
    assert task.status == "dispatching"


async def test_offline_send_failure_marked_offline(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "断线组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([], ok=False))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g9",
    )

    assert res["targets"] == 0
    task = (await fetch_tasks("sig_g9"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert [r.status for r in rows] == ["offline"]
    assert "连接已断开" in rows[0].skip_reason


async def test_trade_result_by_magic_updates_task_and_signal(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "回报组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g10",
    )
    magic = sent[0][1]["magic"]

    handled = await group_persist.update_dispatch_result(
        node_id="nd_a",
        result={"success": True, "order": 8801, "price": 2401.5, "retcode": 10009},
        dispatch_id=group_rules.subtask_id_from_magic(magic),
        signal_id="sig_g10",
    )

    assert handled is not None
    task = (await fetch_tasks("sig_g10"))[0]
    rows = await fetch_dispatches(task.task_id)
    # 首单成交只是进入 opened：要等魔术号持仓全平才算完成
    assert rows[0].status == "opened"
    assert rows[0].order_ticket == 8801
    assert rows[0].price == 2401.5
    assert rows[0].opened_at is not None
    assert task.status == "running"
    assert task.finished_at is None

    finished = await group_persist.finish_subtask(
        node_id="nd_a", task_id=task.task_id,
        data={"status": "done", "reason": "positions_cleared",
              "total_orders": 3, "total_volume": 0.36, "realized_profit": 12.5},
    )
    assert finished["task_status"] == "done"
    assert finished["released"] == [(task.group_id, "nd_a")]
    task = (await fetch_tasks("sig_g10"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].status == "done"
    assert rows[0].position_count == 0
    assert rows[0].finish_reason == "positions_cleared"
    assert task.status == "done"
    assert task.finished_at is not None
    assert task.total_orders == 3
    assert task.realized_profit == 12.5
    async with SessionLocal() as s:
        sig = await s.get(SignalHistory, "sig_g10")
    assert sig.status == "done"
    assert sig.model == "strategy"
    assert sig.dispatch_mode == "group"


async def test_finish_subtask_keeps_account_risk_reason_and_detail(store, monkeypatch):
    """账户风控收口：结束原因与触发参数要落到 close_all 事件，供开单原因展示。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "风控原因组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_risk_reason",
    )
    task = (await fetch_tasks("sig_risk_reason"))[0]
    await open_first_orders(task.task_id, ["nd_a"])
    reason = (
        "账户风控·手数分档盈亏：分档#2 总手数 0.6>=0.5 且盈亏 26.40 达 20；动作 全部平仓"
    )
    detail = {
        "kind": "account_risk",
        "rule": "lot_pl_tiers",
        "rule_label": "手数分档盈亏",
        "min_lot": 0.5,
        "pl_amount": 20,
        "current_lot": 0.6,
        "current_pl": 26.4,
    }
    finished = await group_persist.finish_subtask(
        node_id="nd_a", task_id=task.task_id,
        data={
            "status": "done",
            "reason": reason,
            "detail": detail,
            "total_orders": 1,
            "total_volume": 0.6,
            "realized_profit": 26.4,
        },
    )
    assert finished["task_status"] == "done"
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].finish_reason == reason
    async with SessionLocal() as s:
        ev = (
            await s.execute(
                select(GroupTaskEvent).where(
                    GroupTaskEvent.task_id == task.task_id,
                    GroupTaskEvent.event_type == "close_all",
                )
            )
        ).scalar_one()
    assert ev.message == reason
    assert ev.detail_json == detail


async def test_trade_result_falls_back_to_signal_id(store, monkeypatch):
    """节点未上报魔术号时，按 signal_id 匹配该节点在途的明细。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "兼容组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g11",
    )

    handled = await group_persist.update_dispatch_result(
        node_id="nd_a", result={"success": False, "error": "no money"},
        task_id=None, signal_id="sig_g11",
    )

    task = (await fetch_tasks("sig_g11"))[0]
    # 首单失败直接收口，并回传要释放的组内节点占位
    assert handled["released"] == [(task.group_id, "nd_a")]
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].status == "failed"
    assert rows[0].error == "no money"
    assert task.status == "failed"


async def test_trade_result_of_normal_signal_not_handled_by_group(store):
    """normal 链路的回报不应命中分组明细（两张表互不写入）。"""
    handled = await group_persist.update_dispatch_result(
        node_id="nd_a", result={"success": True}, task_id=None, signal_id="sig_normal",
    )
    assert handled is None


async def test_partial_status_when_mixed_results(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "部分成功组", ["nd_a", "nd_b"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g12",
    )
    task = (await fetch_tasks("sig_g12"))[0]

    await group_persist.update_dispatch_result(
        node_id="nd_a", result={"success": True, "order": 1}, task_id=task.task_id,
    )
    await group_persist.update_dispatch_result(
        node_id="nd_b", result={"success": False, "error": "x"}, task_id=task.task_id,
    )
    # nd_a 首单成交后还在跑，主任务此时是 running
    assert (await fetch_tasks("sig_g12"))[0].status == "running"

    await group_persist.finish_subtask(
        node_id="nd_a", task_id=task.task_id, data={"status": "done"},
    )

    task = (await fetch_tasks("sig_g12"))[0]
    assert task.status == "partial"
    async with SessionLocal() as s:
        sig = await s.get(SignalHistory, "sig_g12")
    assert sig.status == "partial"


async def test_signal_status_aggregates_across_groups(store, monkeypatch):
    """一条信号进多个分组：一组成功、一组失败 -> 信号整体 partial。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "成功组", ["nd_a"])
    await mk_group(store, "失败组", ["nd_b"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g13a",
    )
    by_group = {t.group_name: t.task_id for t in await fetch_tasks("sig_g13a")}

    await group_persist.update_dispatch_result(
        node_id="nd_a", result={"success": True, "order": 1}, task_id=by_group["成功组"],
    )
    await group_persist.finish_subtask(
        node_id="nd_a", task_id=by_group["成功组"], data={"status": "done"},
    )
    async with SessionLocal() as s:  # 只收口了一个分组，信号仍在途
        assert (await s.get(SignalHistory, "sig_g13a")).status == "dispatching"

    await group_persist.update_dispatch_result(
        node_id="nd_b", result={"success": False, "error": "x"}, task_id=by_group["失败组"],
    )
    statuses = {t.group_name: t.status for t in await fetch_tasks("sig_g13a")}
    assert statuses == {"成功组": "done", "失败组": "failed"}
    async with SessionLocal() as s:
        assert (await s.get(SignalHistory, "sig_g13a")).status == "partial"


async def test_signal_status_done_when_all_groups_skipped(store, monkeypatch):
    """分组都无有效节点：处理已结束，信号收口为 done（只是无人成交）。"""
    await offline_node(store, mk_node("nd_a"))
    await mk_group(store, "全跳过组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g13b",
    )

    assert (await fetch_tasks("sig_g13b"))[0].status == "skipped"
    # mark_task_skipped 不写明细，故信号状态由后续回报驱动；此处校验主任务已终态
    async with SessionLocal() as s:
        assert (await s.get(SignalHistory, "sig_g13b")).model == "strategy"


# =====================================================================
# 5. 分组分发模式：sync / poll
# =====================================================================
async def test_sync_mode_sends_to_all_effective_nodes(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"), mk_node("nd_c"))
    await mk_group(store, "同步组", ["nd_a", "nd_b", "nd_c"], mode="sync")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="SELL", symbol="XAUUSD", volume=0.1), "sig_g13",
    )

    assert res["targets"] == 3
    assert {s[0] for s in sent} == {"nd_a", "nd_b", "nd_c"}


async def test_poll_mode_rotates_across_members(store, monkeypatch):
    """轮询轮转：一条信号只交给队首一个节点，领取者移到队尾，循环轮转。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"), mk_node("nd_c"))
    group = await mk_group(store, "轮询组", ["nd_a", "nd_b", "nd_c"], mode="poll")
    gid = group["group_id"]
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)
    signal = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)

    # 分组互斥：每条信号处理完要先收口，才能接收下一条
    res = await d.dispatch(signal, "sig_p1")
    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]
    assert await store.get_group_rotation(gid) == ["nd_b", "nd_c", "nd_a"]
    await finish_task(store, (await fetch_tasks("sig_p1"))[0].task_id, ["nd_a"])

    sent.clear()
    await d.dispatch(signal, "sig_p2")
    assert [s[0] for s in sent] == ["nd_b"]
    assert await store.get_group_rotation(gid) == ["nd_c", "nd_a", "nd_b"]
    await finish_task(store, (await fetch_tasks("sig_p2"))[0].task_id, ["nd_b"])

    sent.clear()
    await d.dispatch(signal, "sig_p3")
    assert [s[0] for s in sent] == ["nd_c"]
    assert await store.get_group_rotation(gid) == ["nd_a", "nd_b", "nd_c"]
    await finish_task(store, (await fetch_tasks("sig_p3"))[0].task_id, ["nd_c"])

    sent.clear()
    await d.dispatch(signal, "sig_p4")
    assert [s[0] for s in sent] == ["nd_a"]  # 回到队首，循环轮转


async def test_poll_mode_skips_ineffective_head(store, monkeypatch):
    """队首节点离线时顺延到下一个有效节点，离线节点保留其轮转位置。"""
    await offline_node(store, mk_node("nd_a"))
    await online(store, mk_node("nd_b"))
    group = await mk_group(store, "顺延组", ["nd_a", "nd_b"], mode="poll")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_p5",
    )

    assert [s[0] for s in sent] == ["nd_b"]
    assert await store.get_group_rotation(group["group_id"]) == ["nd_a", "nd_b"]


async def test_poll_mode_falls_through_unreachable_node(store, monkeypatch):
    """队首在线但发送失败（连接刚断）时顺延下一个，并把失败节点记为 offline。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "发送失败组", ["nd_a", "nd_b"], mode="poll")
    sent = []

    async def flaky_send(node_id, msg):
        sent.append(node_id)
        return node_id != "nd_a"

    monkeypatch.setattr(manager, "send_to_node", flaky_send)

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_p6",
    )

    assert sent == ["nd_a", "nd_b"]
    assert res["targets"] == 1
    task = (await fetch_tasks("sig_p6"))[0]
    rows = {r.node_id: r.status for r in await fetch_dispatches(task.task_id)}
    assert rows == {"nd_a": "offline", "nd_b": "sent"}
    assert task.node_ids_json == ["nd_b"]


async def test_poll_rotation_survives_member_change(store, monkeypatch):
    """成员变动后轮转顺序自动对齐：移出的丢弃，新加入的排队尾。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"), mk_node("nd_c"))
    group = await mk_group(store, "变动组", ["nd_a", "nd_b"], mode="poll")
    gid = group["group_id"]
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)
    signal = TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1)

    await d.dispatch(signal, "sig_p7")
    assert await store.get_group_rotation(gid) == ["nd_b", "nd_a"]
    await finish_task(store, (await fetch_tasks("sig_p7"))[0].task_id, ["nd_a"])

    # nd_a 移出、nd_c 加入
    await group_service.update_group(store, gid, GroupUpdate(node_ids=["nd_b", "nd_c"]))
    await d.dispatch(signal, "sig_p8")
    assert await store.get_group_rotation(gid) == ["nd_c", "nd_b"]


# =====================================================================
# 6. CLOSE 与规则隔离
# =====================================================================
async def test_close_signal_terminates_running_subtasks(store, monkeypatch):
    """CLOSE 是终止指令：绕过互斥，让每个在跑节点平掉自己魔术号的持仓。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "平仓组", ["nd_a", "nd_b"], mode="sync")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_c0")
    task = (await fetch_tasks("sig_c0"))[0]
    await open_first_orders(task.task_id, ["nd_a", "nd_b"])
    by_node = await magics_of(task.task_id)
    sent.clear()

    res = await d.dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.1), "sig_c1",
    )

    assert res["mode"] == "group_close"
    assert res["targets"] == 2
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}
    assert all(s[1]["cmd"] == "strategy_stop" for s in sent)
    # 每个节点收到的是自己那个魔术号
    for node_id, msg in sent:
        assert msg["magic"] == by_node[node_id]
    # 子任务转入 closing，等节点回报平仓完成
    rows = await fetch_dispatches(task.task_id)
    assert {r.status for r in rows} == {"closing"}


async def test_manual_close_subtask_only_stops_one_node(store, monkeypatch):
    """手动平仓只终止指定子任务，不影响同任务其它节点。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    group = await mk_group(store, "单节点平仓组", ["nd_a", "nd_b"], mode="sync")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_mc0")
    task = (await fetch_tasks("sig_mc0"))[0]
    await open_first_orders(task.task_id, ["nd_a", "nd_b"])
    rows = await fetch_dispatches(task.task_id)
    target = next(r for r in rows if r.node_id == "nd_a")
    peer = next(r for r in rows if r.node_id == "nd_b")
    sent.clear()

    outcome = await d.close_subtask(group["group_id"], target.id)

    assert outcome["status"] == "closing"
    assert outcome["node_id"] == "nd_a"
    assert len(sent) == 1
    assert sent[0][0] == "nd_a"
    assert sent[0][1]["cmd"] == "strategy_stop"
    assert sent[0][1]["dispatch_id"] == target.id
    assert sent[0][1]["reason"] == "manual_close"

    refreshed = await fetch_dispatches(task.task_id)
    by_id = {r.id: r for r in refreshed}
    assert by_id[target.id].status == "closing"
    assert by_id[peer.id].status in ("opened", "running")
    assert by_id[peer.id].status != "closing"


async def test_manual_close_subtask_rejects_terminal(store, monkeypatch):
    """已结束的子任务不可再平仓。"""
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "终态平仓组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_mc1")
    task = (await fetch_tasks("sig_mc1"))[0]
    await finish_task(store, task.task_id, ["nd_a"])
    row = (await fetch_dispatches(task.task_id))[0]

    with pytest.raises(ValueError, match="已结束"):
        await d.close_subtask(group["group_id"], row.id)


async def test_close_signal_force_finishes_offline_node(store, monkeypatch):
    """目标节点离线时无人平仓：强制收口子任务并放开占位，避免被永久锁死。"""
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "离线平仓组", ["nd_a"])
    gid = group["group_id"]
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_c3")
    task = (await fetch_tasks("sig_c3"))[0]
    await open_first_orders(task.task_id, ["nd_a"])
    assert await store.get_group_node_busy(gid, "nd_a") is not None

    monkeypatch.setattr(manager, "send_to_node", capture_sender([], ok=False))
    res = await d.dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.1), "sig_c4",
    )

    assert res["tasks"][0]["status"] == "failed"
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].status == "failed"
    assert rows[0].finish_reason == "close_signal_node_offline"
    assert await store.get_group_node_busy(gid, "nd_a") is None


async def test_close_signal_queues_pending_stop_for_offline_node(store, monkeypatch):
    """节点离线时终止指令入队，等其重连后补发，避免 MT5 持仓无人收口。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "补发平仓组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_c5")
    task = (await fetch_tasks("sig_c5"))[0]
    await open_first_orders(task.task_id, ["nd_a"])
    magic = (await magics_of(task.task_id))["nd_a"]

    monkeypatch.setattr(manager, "send_to_node", capture_sender([], ok=False))
    await d.dispatch(TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.1), "sig_c6")

    pending = await store.pop_pending_stops("nd_a")
    assert len(pending) == 1
    assert pending[0]["cmd"] == "strategy_stop"
    assert pending[0]["magic"] == magic
    # 取过一次即清空，节点重连只会收到一次
    assert await store.pop_pending_stops("nd_a") == []


async def test_dispatch_rechecks_db_when_busy_lock_expired(store, monkeypatch):
    """占位 TTL 到期但子任务仍在跑：以库为准跳过，并把占位按真实子任务号续上。"""
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "占位过期组", ["nd_a"])
    gid = group["group_id"]
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    d = GroupDispatcher(store)

    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_x1")
    task = (await fetch_tasks("sig_x1"))[0]
    await open_first_orders(task.task_id, ["nd_a"])
    dispatch_id = (await fetch_dispatches(task.task_id))[0].id

    await store.release_group_node_busy(gid, "nd_a")  # 模拟占位过期
    sent.clear()

    res = await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_x2")

    assert res["tasks"][0]["status"] == "skipped"
    assert sent == []
    assert await store.get_group_node_busy(gid, "nd_a") == str(dispatch_id)


async def test_close_signal_without_active_task_is_skipped(store, monkeypatch):
    """分组没有进行中的任务时，CLOSE 无事可做。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "空闲组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.1), "sig_c2",
    )

    assert res["mode"] == "group_close"
    assert res["targets"] == 0
    assert res["tasks"][0]["status"] == "skipped"
    assert sent == []


async def test_group_dispatch_ignores_console_symbol_config(store, monkeypatch):
    """规则隔离：品种未在中控台配置、且节点无按币种配置，分组信号仍照常下发。"""
    await store.set_filters({})  # 中控台完全没有品种配置
    await online(store, mk_node("nd_a", filters=None))
    await mk_group(store, "隔离组", ["nd_a"], symbol="GBPUSD")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="GBPUSD", volume=0.1), "sig_i1",
    )

    assert res["mode"] == "group"
    assert [s[0] for s in sent] == ["nd_a"]


async def test_group_dispatch_ignores_position_and_interval_gates(store, monkeypatch):
    """规则隔离：节点已有同品种持仓、且无可用报价，分组信号也不会被拦截。"""
    await online(store, mk_node("nd_a"))
    await store.save_account("nd_a", {"positions": [{"symbol": "XAUUSD"}], "prices": {}})
    await store.set_filters(
        {
            "XAUUSD": {
                "enabled": False,          # normal 链路会因此拒收
                "allow_buy": False,
                "dispatch_mode": "sync",
                "position_scope": "symbol",
                "default_action": "block",
                "intervals": [],
            }
        }
    )
    await mk_group(store, "无闸门组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_i2",
    )

    assert res["mode"] == "group"
    assert [s[0] for s in sent] == ["nd_a"]


async def test_group_signal_writes_no_normal_dispatch_rows(store, monkeypatch):
    """strategy 信号不写 normal 链路的 signal_dispatch 表。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "不串表组", ["nd_a"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_i3",
    )

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(SignalDispatch).where(SignalDispatch.signal_id == "sig_i3")
            )
        ).scalars().all()
    assert rows == []


# =====================================================================
# 7. 分组信号明细查询（前端弹窗数据源）
# =====================================================================
async def test_recent_group_signals_pagination_and_shape(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    group = await mk_group(store, "查询组", ["nd_a"])
    gid = group["group_id"]
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)
    for i in range(3):
        await d.dispatch(
            TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), f"sig_q{i}",
        )
        # 分组互斥：收口后才能接收下一条
        await finish_task(store, (await fetch_tasks(f"sig_q{i}"))[0].task_id, ["nd_a"])

    page1 = await group_persist.recent_group_signals(gid, 1, 2, {"nd_a": "节点A"})
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    # 按任务号倒序：最新的在最前
    assert page1["items"][0]["signal_id"] == "sig_q2"
    item = page1["items"][0]
    assert item["group_id"] == gid
    assert item["symbol"] == "XAUUSD"
    assert "magic" not in item  # 魔术号在子任务上
    assert item["node_ids"] == ["nd_a"]
    assert item["payload"]["cmd"] == "strategy_start"
    assert item["strategy_id"]
    detail = item["dispatches"][0]
    assert detail["node_name"] == "节点A"
    assert detail["status"] == "done"
    assert detail["symbol"] == "XAUUSD"
    assert detail["magic"] == group_rules.subtask_magic(detail["id"])

    page2 = await group_persist.recent_group_signals(gid, 2, 2)
    assert [i["signal_id"] for i in page2["items"]] == ["sig_q0"]


async def test_count_by_group(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    g1 = await mk_group(store, "计数组一", ["nd_a"])
    g2 = await mk_group(store, "计数组二", ["nd_b"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_n1")
    for task in await fetch_tasks("sig_n1"):
        await finish_task(store, task.task_id, [task.node_ids_json[0]])
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_n2")

    counts = await group_persist.count_by_group()
    assert counts[g1["group_id"]] == 2
    assert counts[g2["group_id"]] == 2
