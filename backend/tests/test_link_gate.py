"""链路闸门（服务端侧）测试：终端与券商断联的节点不下发开仓指令。

服务端这层只是「不浪费一次下发 + 提前可见」，真正把关的是节点侧闸门。
因此判据是**上报的滞后状态**：状态过期或缺失一律放行，绝不误伤节点。
"""
import time

import pytest
from sqlalchemy import delete, select

from app import group_persist, group_service, mt5_link
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.mt5_link import LINK_OFFLINE_TTL
from app.models import GroupCreate
from app.orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    NodeGroup,
    NodeGroupMember,
    TradingStrategy,
)
from app.parser import TradingSignal
from app.redis_store import RedisStore

import fakeredis

_strategy_seq = iter(range(1, 10_000))


@pytest.fixture
async def store():
    await init_db()
    async with SessionLocal() as s:
        for table in (
            GroupTaskEvent, GroupTaskDispatch, GroupSignalTask,
            NodeGroupMember, NodeGroup, TradingStrategy,
        ):
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
        "filters": None,
        "created_at": 0,
    }
    d.update(kw)
    return d


async def online(store, *nodes):
    for n in nodes:
        await store.cache_node(n)
        manager.nodes[n["node_id"]] = object()


async def mk_group(store, name, node_ids, *, mode="sync"):
    strategy_id = f"sty_t{next(_strategy_seq)}"
    row = {
        "strategy_id": strategy_id,
        "name": f"策略{strategy_id}",
        "template_id": "tpl_1",
        "template_name": "AI智能加仓策略",
        "symbol": "XAUUSD",
        "enabled": True,
        "rules": [],
        "remark": None,
        "created_at": 0,
    }
    async with SessionLocal() as s:
        s.add(
            TradingStrategy(
                strategy_id=strategy_id,
                name=row["name"],
                template_id=row["template_id"],
                template_name=row["template_name"],
                symbol=row["symbol"],
                enabled=True,
                config_json=[],
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=True, dispatch_mode=mode,
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


async def _snapshot(store, node_id, link, *, age=0.0):
    """写入一份带链路状态的账户快照。"""
    await store.save_account(node_id, {
        "node_id": node_id,
        "positions": [],
        "orders": [],
        "mt5_link": link,
        "updated_at": time.time() - age,
    })


async def test_offline_node_is_not_dispatched(store, monkeypatch):
    """断联节点不下发开仓指令。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    await _snapshot(store, "nd_a", "offline")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_off",
    )

    assert sent == []
    task = (await fetch_tasks("sig_link_off"))[0]
    rows = await fetch_dispatches(task.task_id)
    assert len(rows) == 1
    assert rows[0].status == "skipped"
    assert "断连" in (rows[0].skip_reason or "")
    # 断联节点不该占用组内节点槽位
    assert task.skip_reason


async def test_online_node_is_dispatched(store, monkeypatch):
    """链路正常的节点照常下发。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    await _snapshot(store, "nd_a", "online")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_on",
    )

    assert [s[0] for s in sent] == ["nd_a"]


async def test_unknown_link_is_dispatched(store, monkeypatch):
    """unknown（读不到）照常下发：不能因为状态不明就误伤节点。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    await _snapshot(store, "nd_a", "unknown")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_unknown",
    )

    assert [s[0] for s in sent] == ["nd_a"]


async def test_stale_offline_snapshot_is_ignored(store, monkeypatch):
    """断联状态过期就不作数：节点可能早就恢复了，不能拿旧状态拦它。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    await _snapshot(store, "nd_a", "offline", age=LINK_OFFLINE_TTL + 10)
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_stale",
    )

    assert [s[0] for s in sent] == ["nd_a"]


async def test_missing_snapshot_is_dispatched(store, monkeypatch):
    """没有快照（旧节点）照常下发：闸门只拦明确断联的节点。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_none",
    )

    assert [s[0] for s in sent] == ["nd_a"]


async def test_offline_node_releases_busy_lock(store, monkeypatch):
    """断联拦截必须放开组内节点占位，否则节点会一直被占住。"""
    await online(store, mk_node("nd_a"))
    await mk_group(store, "甲组", ["nd_a"])
    await _snapshot(store, "nd_a", "offline")
    sent = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1), "sig_link_busy",
    )
    task = (await fetch_tasks("sig_link_busy"))[0]

    # 占位已放开：这里能重新抢到（抢到 = True）
    assert await store.acquire_group_node_busy(task.group_id, "nd_a", "pending", 60)


# ------------------------ 共享判定模块 ------------------------
def test_normalize_link_state():
    assert mt5_link.normalize_link_state("online") == "online"
    assert mt5_link.normalize_link_state("OFFLINE") == "offline"
    assert mt5_link.normalize_link_state("unknown") == "unknown"
    # 非法值一律按 unknown：不能因为读不懂就当成断联
    assert mt5_link.normalize_link_state("garbage") == "unknown"
    assert mt5_link.normalize_link_state(None) == "unknown"
    assert mt5_link.normalize_link_state("") == "unknown"


def test_link_offline_reason_blocks_fresh_offline():
    snap = {"mt5_link": "offline", "updated_at": time.time()}
    assert mt5_link.link_offline_reason(snap) == mt5_link.LINK_OFFLINE_REASON


@pytest.mark.parametrize("snapshot", [
    None,
    {},
    {"mt5_link": "online", "updated_at": time.time()},
    {"mt5_link": "unknown", "updated_at": time.time()},
    {"mt5_link": "garbage", "updated_at": time.time()},
    {"mt5_link": "offline"},               # 没有时刻：不作数
    {"mt5_link": "offline", "updated_at": 0},
    {"mt5_link": "offline", "updated_at": "bad"},
])
def test_link_offline_reason_passes_through(snapshot):
    """状态缺失 / 非法 / 过期一律放行：服务端看到的是滞后信息，不能当权威。"""
    assert mt5_link.link_offline_reason(snapshot) is None


def test_link_offline_reason_expires():
    snap = {"mt5_link": "offline", "updated_at": time.time() - LINK_OFFLINE_TTL - 1}
    assert mt5_link.link_offline_reason(snap) is None
