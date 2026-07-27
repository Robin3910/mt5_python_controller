"""分组分发引擎集成测试（fakeredis + SQLite，无需外部服务）。

覆盖 strategy 链路的核心行为：
- 所有启用分组各自处理同一条信号；禁用分组不参与；
- 有效节点口径 = 已启用 + 在线；
- 下发前生成主任务、任务号换算出的魔术号随命令下发；
- 主任务与各节点明细落库，节点回报按魔术号回填并收口状态；
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
    NodeGroup,
    NodeGroupMember,
    SignalDispatch,
    SignalHistory,
)
from app.parser import TradingSignal
from app.redis_store import RedisStore

# 分组链路必须真实落库（主任务号由数据库自增，是魔术号的来源），
# 所以这里不像 test_dispatch.py 那样 mock 掉持久化，只用 conftest 里的 SQLite 测试库。
_TABLES = (
    GroupTaskDispatch,
    GroupSignalTask,
    NodeGroupMember,
    NodeGroup,
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


async def mk_group(store, name, node_ids, *, mode="sync", enabled=True):
    return await group_service.create_group(
        store,
        GroupCreate(name=name, enabled=enabled, dispatch_mode=mode, node_ids=list(node_ids)),
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
    # 两个分组各生成一条主任务，且任务号/魔术号互不相同
    tasks = await fetch_tasks("sig_g1")
    assert len(tasks) == 2
    assert {t.group_name for t in tasks} == {"组一", "组二"}
    assert len({t.task_id for t in tasks}) == 2
    assert len({t.magic for t in tasks}) == 2


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
    assert "没有已启用的分组" in res["reason"]
    assert sent == []
    assert await fetch_tasks("sig_g3") == []


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
# 3. 主任务号即魔术号，随命令下发
# =====================================================================
async def test_task_id_is_used_as_magic_number(store, monkeypatch):
    await online(store, mk_node("nd_a"))
    await mk_group(store, "魔术号组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.3), "sig_g6",
    )

    task = (await fetch_tasks("sig_g6"))[0]
    cmd = sent[0][1]
    assert cmd["cmd"] == "open"
    assert cmd["magic"] == task.magic == group_rules.task_magic(task.task_id)
    assert cmd["task_id"] == task.task_id
    assert cmd["group_id"] == task.group_id
    assert cmd["model"] == "strategy"
    assert cmd["volume"] == 0.3
    assert res["tasks"][0]["magic"] == task.magic
    # 下发数据完整落库，便于事后追溯
    assert task.payload_json["magic"] == task.magic


async def test_task_created_before_dispatch(store, monkeypatch):
    """下发到节点时主任务必须已经存在（任务号是魔术号的来源）。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "顺序组", ["nd_a"])
    seen_at_send = {}

    async def fake_send(node_id, msg):
        seen_at_send["tasks"] = await fetch_tasks("sig_g7")
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_g7",
    )
    assert len(seen_at_send["tasks"]) == 1


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
    assert all(r.magic == task.magic for r in rows)
    assert all(r.decided_vol == 0.15 for r in rows)
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
        task_id=group_rules.task_id_from_magic(magic),
        signal_id="sig_g10",
    )

    assert handled is True
    task = (await fetch_tasks("sig_g10"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].status == "done"
    assert rows[0].order_ticket == 8801
    assert rows[0].price == 2401.5
    assert task.status == "done"
    assert task.finished_at is not None
    async with SessionLocal() as s:
        sig = await s.get(SignalHistory, "sig_g10")
    assert sig.status == "done"
    assert sig.model == "strategy"
    assert sig.dispatch_mode == "group"


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

    assert handled is True
    task = (await fetch_tasks("sig_g11"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert rows[0].status == "failed"
    assert rows[0].error == "no money"
    assert task.status == "failed"


async def test_trade_result_of_normal_signal_not_handled_by_group(store):
    """normal 链路的回报不应命中分组明细（两张表互不写入）。"""
    handled = await group_persist.update_dispatch_result(
        node_id="nd_a", result={"success": True}, task_id=None, signal_id="sig_normal",
    )
    assert handled is False


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

    res = await d.dispatch(signal, "sig_p1")
    assert res["targets"] == 1
    assert [s[0] for s in sent] == ["nd_a"]
    assert await store.get_group_rotation(gid) == ["nd_b", "nd_c", "nd_a"]

    sent.clear()
    await d.dispatch(signal, "sig_p2")
    assert [s[0] for s in sent] == ["nd_b"]
    assert await store.get_group_rotation(gid) == ["nd_c", "nd_a", "nd_b"]

    sent.clear()
    await d.dispatch(signal, "sig_p3")
    assert [s[0] for s in sent] == ["nd_c"]
    assert await store.get_group_rotation(gid) == ["nd_a", "nd_b", "nd_c"]

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

    # nd_a 移出、nd_c 加入
    await group_service.update_group(store, gid, GroupUpdate(node_ids=["nd_b", "nd_c"]))
    await d.dispatch(signal, "sig_p8")
    assert await store.get_group_rotation(gid) == ["nd_c", "nd_b"]


# =====================================================================
# 6. CLOSE 与规则隔离
# =====================================================================
async def test_close_signal_broadcasts_within_group(store, monkeypatch):
    """CLOSE 不受分发模式限制，通知分组内所有有效节点平掉该品种。"""
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    await mk_group(store, "平仓组", ["nd_a", "nd_b"], mode="poll")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="CLOSE", symbol="XAUUSD", volume=0.1), "sig_c1",
    )

    assert res["targets"] == 2
    assert {s[0] for s in sent} == {"nd_a", "nd_b"}
    assert all(s[1]["cmd"] == "close" for s in sent)
    assert all(s[1]["close_symbol"] == "XAUUSD" for s in sent)
    task = (await fetch_tasks("sig_c1"))[0]
    assert all(s[1]["magic"] == task.magic for s in sent)


async def test_group_dispatch_ignores_console_symbol_config(store, monkeypatch):
    """规则隔离：品种未在中控台配置、且节点无按币种配置，分组信号仍照常下发。"""
    await store.set_filters({})  # 中控台完全没有品种配置
    await online(store, mk_node("nd_a", filters=None))
    await mk_group(store, "隔离组", ["nd_a"])
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

    page1 = await group_persist.recent_group_signals(gid, 1, 2, {"nd_a": "节点A"})
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    # 按任务号倒序：最新的在最前
    assert page1["items"][0]["signal_id"] == "sig_q2"
    item = page1["items"][0]
    assert item["group_id"] == gid
    assert item["symbol"] == "XAUUSD"
    assert item["magic"] == group_rules.task_magic(item["task_id"])
    assert item["node_ids"] == ["nd_a"]
    assert item["payload"]["cmd"] == "open"
    assert item["dispatches"][0]["node_name"] == "节点A"
    assert item["dispatches"][0]["status"] == "sent"

    page2 = await group_persist.recent_group_signals(gid, 2, 2)
    assert [i["signal_id"] for i in page2["items"]] == ["sig_q0"]


async def test_count_by_group(store, monkeypatch):
    await online(store, mk_node("nd_a"), mk_node("nd_b"))
    g1 = await mk_group(store, "计数组一", ["nd_a"])
    g2 = await mk_group(store, "计数组二", ["nd_b"])
    monkeypatch.setattr(manager, "send_to_node", capture_sender([]))
    d = GroupDispatcher(store)
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_n1")
    await d.dispatch(TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_n2")

    counts = await group_persist.count_by_group()
    assert counts[g1["group_id"]] == 2
    assert counts[g2["group_id"]] == 2
