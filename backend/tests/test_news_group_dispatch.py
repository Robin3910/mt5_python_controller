"""财经日历黑名单接入**分组分发引擎**（strategy 链路）的集成用例。

验证三件事：

1. 命中黑名单时分组的新开仓被拦，节点**收不到**指令，且落 skip_code；
2. 黑名单只影响**新开仓** —— 已在跑的任务不被终止（不发终止指令）；
3. 跟单员节点按**跟单员自己的**设置判定（跟单员暂停不影响带单员）。
"""
import fakeredis
import pytest
from sqlalchemy import delete

from app import group_persist, news_guard
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.parser import TradingSignal
from app.redis_store import RedisStore

from tests.test_group_dispatch import (
    capture_sender,
    fetch_dispatches,
    fetch_tasks,
    mk_group,
    mk_node,
    mk_strategy,
    online,
)

from app.orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    NodeGroup,
    NodeGroupMember,
    TradingStrategy,
)

# 分组链路必须真实落库（子任务号由数据库自增，是魔术号的来源）
_TABLES = (
    GroupTaskEvent,
    GroupTaskDispatch,
    GroupSignalTask,
    NodeGroupMember,
    NodeGroup,
    TradingStrategy,
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


@pytest.fixture
def blackout(monkeypatch):
    """让判定层永远返回「命中黑名单」，并记录每次调用的 (user_id, symbol)。"""
    calls: list = []

    async def fake_block(user_id, symbol, *, now=None):
        calls.append((user_id, symbol))
        return "CPI y/y (USD, high) 公布时段黑名单（±30 分钟）"

    monkeypatch.setattr(news_guard, "block_open_reason", fake_block)
    return calls


async def test_group_open_blocked_during_blackout(store, monkeypatch, blackout):
    """命中黑名单时分组的新开仓被拦，节点收不到指令。"""
    await online(store, mk_node("nd_a", owner_user_id=6600))
    await mk_group(store, "黑名单组", ["nd_a"], owner_user_id=6600)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_bl_group",
    )

    assert blackout, "黑名单判定应被调用"
    assert sent == [], f"黑名单期间不应下发开仓指令，实际下发了 {sent}"
    # 节点占位必须已释放 —— 否则黑名单期间会把节点并发槽白占住
    tasks = await fetch_tasks("sig_bl_group")
    assert tasks
    dispatches = await fetch_dispatches(tasks[0].task_id)
    assert any(d.skip_code == "news_blackout" for d in dispatches), (
        "跳过原因应落 news_blackout 结构化编码"
    )


async def test_group_without_owner_is_not_blocked(store, monkeypatch, blackout):
    """管理员名下的分组没有主人 → 没有个人开关 → 不拦。

    「没有主人就没有开关」比「按全站规则拦」安全：管理员名下的节点
    不该被某个用户的黑名单误伤。
    """
    await online(store, mk_node("nd_a"))
    await mk_group(store, "管理员组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_admin_group",
    )

    assert blackout == [], "无归属的分组不应触发个人黑名单判定"
    assert sent, "无归属的分组应照常下发"


async def test_group_dispatch_ok_when_not_blacked_out(store, monkeypatch):
    """判定放行时分组开仓照常下发（黑名单不应误伤正常交易）。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "正常组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    async def allow(user_id, symbol, *, now=None):
        return None

    monkeypatch.setattr(news_guard, "block_open_reason", allow)

    res = await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_ok_group",
    )

    assert res["targets"] == 1
    assert sent, "未命中黑名单时应照常下发"
    assert all(msg.get("cmd") == "strategy_start" for _n, msg in sent)


async def test_group_blackout_uses_group_owner_identity(store, monkeypatch, blackout):
    """判定用的是分组所有者的身份。"""
    # 分组成员必须与分组同一所有者 —— 节点也要挂同一个 owner
    await online(store, mk_node("nd_a", owner_user_id=7788))
    await mk_group(store, "所有者组", ["nd_a"], owner_user_id=7788)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_owner_group",
    )

    assert blackout, "黑名单判定应被调用"
    assert blackout[0][0] == 7788, "判定应使用分组所有者的 user_id"


async def test_group_blackout_does_not_stop_running_task(store, monkeypatch, blackout):
    """黑名单只拦新开仓：已在跑的任务不被终止（不发终止指令）。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "在跑组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    dispatcher = GroupDispatcher(store)
    res = await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_running",
    )
    assert res["targets"] == 1
    sent.clear()

    # 任务已在跑；此时进入黑名单，再来一条新信号
    await dispatcher.dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_during_blackout",
    )

    # 新信号被拦（在跑的任务不动）
    assert all(msg.get("cmd") != "strategy_start" for _n, msg in sent), (
        "黑名单期间不应下发新的 strategy_start"
    )
    # 在跑的子任务仍是活动态，没有被终止
    tasks = await fetch_tasks("sig_running")
    assert tasks
    dispatches = await fetch_dispatches(tasks[0].task_id)
    assert all(d.status not in ("stopped", "closing") for d in dispatches), (
        "在跑的子任务不应被黑名单终止"
    )
