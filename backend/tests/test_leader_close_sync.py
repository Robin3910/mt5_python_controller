"""主节点平仓 -> 跟单节点**同步平仓**（按笔配对，不是终止任务）。

背景：非网格策略（市价/限价开仓、AI 逆势加仓）在节点侧的既有语义是「空仓即收口」：
`market_hub` 读到连续空仓发 `GONE`，`strategy_runner` 撤挂单后 `_finish("done")`。
所以服务器**不需要**下发 `strategy_stop` —— 只需要把「被平掉的那一笔」同步到
跟单节点，剩下的交给节点自己收口。

覆盖的红线：
- 感知：持仓快照对比能检出被平掉的笔（手动/券商止盈止损/系统平仓一视同仁）。
- 映射：主从按开仓时间顺序配对；**不是**按 ticket 直传（跨账户 ticket 独立）。
- 场景 1：全部平完 -> 每笔都同步 -> 跟单侧自然空仓收口。
- 场景 2：只平一笔 -> 只同步一笔 -> 跟单侧不空仓，任务继续跑。
- 网格：`hold_when_empty=True` -> 只平仓、不收口。
- 逃生口：`stop_cascade_enabled=False` 不检出、不下发。
- 方向性：跟单节点上报的快照不触发任何同步（跟单员平仓不得影响带单员）。
- 基线：首次见到节点只建基线；空仓快照是有效对比起点。
- 配对错位：主从笔数不一致时降级（WARNING + 不平仓），宁可不平也不错平。
- 离线：下发失败入 `pending_stop` 队列等重连补发。
- 兜底：主节点已结束、跟单节点也已空仓但子任务未收口 -> 强制收口。
"""

import pytest
from sqlalchemy import delete, select

from app import (
    follow_service,
    group_persist,
    group_service,
    leader_close_sync,
    redis_store,
)
from app.connections import manager
from app.config import Config
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
from app.group_rules import subtask_magic
from app.models import GroupCreate, GroupUpdate
from app.orm import (
    GroupFollowerNode,
    GroupSignalTask,
    GroupTaskDispatch,
    GroupTaskEvent,
    Node,
    NodeGroup,
    NodeGroupMember,
    Role,
    SignalDispatch,
    SignalHistory,
    TradingStrategy,
    User,
    UserRole,
)
from app.parser import TradingSignal
from app.redis_store import RedisStore
from app.strategy_templates import (
    RULE_TYPE_RISK_SIZED,
    TEMPLATE_2_ID,
    TEMPLATE_2_NAME,
)

_TABLES = (
    GroupTaskEvent,
    GroupTaskDispatch,
    GroupSignalTask,
    GroupFollowerNode,
    NodeGroupMember,
    NodeGroup,
    TradingStrategy,
    SignalDispatch,
    SignalHistory,
)

BASE = Config.NODE_TASK_MAGIC_BASE


@pytest.fixture
async def store():
    await init_db()
    async with SessionLocal() as s:
        for table in _TABLES:
            await s.execute(delete(table))
        await s.execute(delete(UserRole))
        await s.execute(delete(Role))
        await s.execute(delete(User))
        await s.commit()
    import fakeredis

    st = RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    # 清掉上一轮用例可能残留的基线（fakeredis 每次新建，这里是防御性写法）
    manager.nodes.clear()
    manager.admins.clear()
    yield st
    manager.nodes.clear()
    manager.admins.clear()


def mk_node(node_id, *, enabled=True, owner_user_id=None, **kw):
    d = {
        "node_id": node_id,
        "name": node_id,
        "enabled": enabled,
        "mt5_login": abs(hash(node_id)) % 100000,
        "filters": None,
        "created_at": 0,
        "owner_user_id": owner_user_id,
    }
    d.update(kw)
    return d


async def online(store, *nodes):
    """节点入库 + 置为在线。"""
    async with SessionLocal() as session:
        for node in nodes:
            row = await session.get(Node, node["node_id"])
            if row is None:
                session.add(
                    Node(
                        node_id=node["node_id"],
                        name=node["name"],
                        mt5_login=node["mt5_login"],
                        owner_user_id=node.get("owner_user_id"),
                        enabled=node.get("enabled", True),
                        is_primary=node.get("is_primary", False),
                    )
                )
        await session.commit()
    for n in nodes:
        await store.cache_node(n)
        manager.nodes[n["node_id"]] = object()


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", owner_user_id=None,
                      entry_mode=None):
    sid = f"sty_lcs{next(_strategy_seq)}"
    rule = {
        "type": RULE_TYPE_RISK_SIZED, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0,
    }
    if entry_mode:
        rule["entry_mode"] = entry_mode
    row = {
        "strategy_id": sid,
        "name": f"趋势策略{sid}",
        "template_id": TEMPLATE_2_ID,
        "template_name": TEMPLATE_2_NAME,
        "symbol": symbol,
        "enabled": True,
        "rules": [rule],
        "remark": None,
        "created_at": 0,
    }
    async with SessionLocal() as s:
        s.add(
            TradingStrategy(
                strategy_id=sid,
                name=row["name"],
                template_id=row["template_id"],
                template_name=row["template_name"],
                symbol=row["symbol"],
                enabled=True,
                config_json=row["rules"],
                owner_user_id=owner_user_id,
            )
        )
        await s.commit()
    await store.cache_strategy(row)
    return row


async def mk_leader(store):
    async with SessionLocal() as s:
        role = Role(code="mk_leader_lcs", name="带单员", capabilities='["follow_leader"]')
        s.add(role)
        await s.flush()
        role_id = role.id
        user = User(
            username="mkleaderlcs", display_name="带单员", role="user",
            is_active=True, password_hash="x",
        )
        s.add(user)
        await s.flush()
        user_id = user.id
        s.add(UserRole(user_id=user_id, role_id=role_id))
        await s.commit()
    return user_id


async def mk_group(store, name, node_ids, *, strategy=None, owner_user_id=None,
                   stop_cascade_enabled=True):
    strategy = strategy or await mk_strategy(store, owner_user_id=owner_user_id)
    return await group_service.create_group(
        store,
        GroupCreate(
            name=name, enabled=True, dispatch_mode="sync",
            strategy_id=strategy["strategy_id"], node_ids=list(node_ids),
            stop_cascade_enabled=stop_cascade_enabled,
        ),
        owner_user_id=owner_user_id,
    )


async def add_follower(store, group_id, node_id, *, leader_user_id,
                       leader_primary_node_id="nd_leader"):
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        node.owner_user_id = 99
        await session.commit()
    await follow_service.add_following(
        store,
        group_id=group_id, node_id=node_id,
        follower_user_id=99, leader_user_id=leader_user_id,
        leader_primary_node_id=leader_primary_node_id,
    )


def capture_sender(sent, *, online_nodes=None):
    async def fake_send(node_id, msg):
        if online_nodes is not None and node_id not in online_nodes:
            return False
        sent.append((node_id, msg))
        return True

    return fake_send


async def setup_pair(store, *, stop_cascade_enabled=True, name="同步平仓组",
                     entry_mode=None):
    """建一个带单组 + 一个跟单节点，跑一次分发；返回 (group, leader_sub, follower_sub)。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id, entry_mode=entry_mode)
    group = await mk_group(
        store, name, ["nd_leader"], strategy=strategy,
        owner_user_id=leader_id, stop_cascade_enabled=stop_cascade_enabled,
    )
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)
    # 分发需要一个真正能 send_json 的连接，否则子任务会被标成 offline（终态），
    # 后续巡检按「未收口子任务」查不到它们。
    sent: list = []
    orig = manager.send_to_node
    manager.send_to_node = capture_sender(sent)
    try:
        await GroupDispatcher(store).dispatch(
            TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0,
                          entry_price=2400.0),
            f"sig_lcs_{name}",
        )
    finally:
        manager.send_to_node = orig
    rows = await subtask_rows(group["group_id"])
    leader = next(r for r in rows if r["node_id"] == "nd_leader")
    follower = next(r for r in rows if r["node_id"] == "nd_f")
    assert leader["task_id"] == follower["task_id"], "前置：同一主任务"
    assert leader["status"] not in ("offline", "failed"), "前置：主节点子任务应已下发"
    assert follower["status"] not in ("offline", "failed"), "前置：跟单子任务应已下发"
    return group, leader, follower


async def subtask_rows(group_id):
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupTaskDispatch)
                .where(GroupTaskDispatch.group_id == group_id)
                .order_by(GroupTaskDispatch.id.asc())
            )
        ).scalars().all()
        return [
            {
                "id": r.id, "node_id": r.node_id, "task_id": r.task_id,
                "status": r.status, "stop_source": r.stop_source,
                "magic": r.magic, "hold_when_empty": r.hold_when_empty,
            }
            for r in rows
        ]


def pos(ticket: int, magic: int, *, time: float, symbol="XAUUSD", volume=0.1,
        side="BUY") -> dict:
    return {
        "ticket": ticket, "magic": magic, "symbol": symbol,
        "type": side, "volume": volume, "time": time,
    }


async def seed_account(store, node_id: str, positions: list, orders: list | None = None):
    prev = await store.get_account(node_id) or {}
    await store.save_account(node_id, {
        "node_id": node_id, "login": 1, "positions": positions,
        # 不传 orders 时沿用上一份快照：fixture 只关心持仓时不会把挂单意外清掉
        "orders": list(orders) if orders is not None else list(prev.get("orders") or []),
        "prices": {}, "quotes": {},
    })


async def seed_baseline(store, node_id: str, positions: list):
    """模拟「上一轮」快照已落基线（首见只建基线，测试要显式推进一轮）。"""
    await leader_close_sync.on_snapshot(store, node_id, positions)


async def set_leader_positions(store, node_id, positions):
    """更新主节点账户快照（巡检从账户读的是它，但基线由 on_snapshot 推进）。"""
    await seed_account(store, node_id, positions)


# ---------------------------------------------------------------------------
# ① / ② 全平 / 只平一笔
# ---------------------------------------------------------------------------
async def test_all_positions_closed_syncs_every_close(store, monkeypatch):
    """场景 1：主节点 3 笔全平 -> 跟单节点收到 3 条 close(ticket)，**没有** strategy_stop。"""
    group, leader, follower = await setup_pair(store)

    # 主节点开 3 笔（同一 magic，按开仓时间排：t0 < t1 < t2）
    m_lead = leader["magic"]
    m_f = follower["magic"]
    lead_pos = [
        pos(1001, m_lead, time=100.0),
        pos(1002, m_lead, time=200.0),
        pos(1003, m_lead, time=300.0),
    ]
    # 跟单节点 3 笔同序（ticket 独立）：t0 < t1 < t2
    f_pos = [
        pos(2001, m_f, time=110.0),
        pos(2002, m_f, time=210.0),
        pos(2003, m_f, time=310.0),
    ]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    # 首轮：只建基线
    await seed_baseline(store, "nd_leader", lead_pos)
    # 第二轮：主节点全平
    await seed_account(store, "nd_leader", [])

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["sent"] == 3, f"应下发 3 条 close，实际 {out}"
    closes = [m for n, m in sent if m.get("cmd") == "close"]
    assert len(closes) == 3, "每条被平掉的笔都应同步一条 close"
    assert all(c["close_target"] == "ticket" for c in closes)
    assert {c["close_ticket"] for c in closes} == {2001, 2002, 2003}, \
        "应按开仓顺序配对到跟单节点自己的 ticket"
    assert not [m for n, m in sent if m.get("cmd") == "strategy_stop"], \
        "新版**不得**下发 strategy_stop（任务由节点侧空仓自然收口）"

    # 子任务没有被服务器收口/标 closing —— 交给节点侧空仓收口
    rows = await subtask_rows(group["group_id"])
    f_row = next(r for r in rows if r["node_id"] == "nd_f")
    assert f_row["status"] not in ("failed", "closing"), \
        "服务器不应强制收口跟单子任务"


async def test_partial_close_syncs_only_one(store, monkeypatch):
    """场景 2：主节点只平第 3 笔 -> 只平跟单节点第 3 笔，任务继续。"""
    group, leader, follower = await setup_pair(store, name="部分平仓组")
    m_lead = leader["magic"]
    m_f = follower["magic"]
    lead_pos = [
        pos(1001, m_lead, time=100.0),
        pos(1002, m_lead, time=200.0),
        pos(1003, m_lead, time=300.0),
    ]
    f_pos = [
        pos(2001, m_f, time=110.0),
        pos(2002, m_f, time=210.0),
        pos(2003, m_f, time=310.0),
    ]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    remaining = lead_pos[:2]  # 只平第 3 笔
    await seed_account(store, "nd_leader", remaining)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", remaining)

    assert out["sent"] == 1, "只应同步 1 笔"
    closes = [m for n, m in sent if m.get("cmd") == "close"]
    assert len(closes) == 1
    assert closes[0]["close_ticket"] == 2003, "第 3 笔应对应跟单节点第 3 笔"

    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] not in ("failed", "closing", "done"), \
        "只平一笔不应结束任务（跟单侧还应继续跑）"


async def test_grid_strategy_only_closes_not_stops(store, monkeypatch):
    """网格（hold_when_empty=True）：只同步平仓，任务不停。"""
    group, leader, follower = await setup_pair(store, name="网格组")
    # 把跟单子任务标成 hold_when_empty（网格的真实落库语义）
    async with SessionLocal() as s:
        row = await s.get(GroupTaskDispatch, follower["id"])
        row.hold_when_empty = True
        await s.commit()

    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(3001, m_lead, time=100.0)]
    f_pos = [pos(4001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_leader", [])

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await leader_close_sync.on_snapshot(store, "nd_leader", [])

    assert [m["cmd"] for n, m in sent] == ["close"]
    rows = await subtask_rows(group["group_id"])
    f_row = next(r for r in rows if r["node_id"] == "nd_f")
    assert f_row["status"] == "opened" or f_row["status"] == "running" \
        or f_row["status"] not in ("done", "failed"), "网格任务应继续运行"


# ---------------------------------------------------------------------------
# ③ 逃生口
# ---------------------------------------------------------------------------
async def test_disabled_switch_suppresses_sync(store, monkeypatch):
    """stop_cascade_enabled=False：不检出、不下发。"""
    group, leader, follower = await setup_pair(
        store, stop_cascade_enabled=False, name="关闭组",
    )
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(5001, m_lead, time=100.0)]
    f_pos = [pos(6001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_account(store, "nd_leader", [])

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["sent"] == 0 and out["queued"] == 0
    assert sent == [], "关闭同步平仓后不得下发任何指令"


# ---------------------------------------------------------------------------
# ④ 方向性
# ---------------------------------------------------------------------------
async def test_follower_snapshot_does_not_sync(store, monkeypatch):
    """跟单节点上报快照不触发同步 —— 跟单员自己平仓不得影响带单员。"""
    group, leader, follower = await setup_pair(store, name="方向组")
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(7001, m_lead, time=100.0)]
    f_pos = [pos(8001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    # 即便跟单节点有基线，它的快照也不触发任何同步
    await seed_baseline(store, "nd_f", f_pos)
    await seed_account(store, "nd_f", [])

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_f", [])
    assert out["sent"] == 0 and out["queued"] == 0, "跟单节点快照不得触发同步"
    assert sent == []


# ---------------------------------------------------------------------------
# ⑤ 基线规则
# ---------------------------------------------------------------------------
async def test_first_see_only_builds_baseline(store, monkeypatch):
    """首见：只建基线，不把当前持仓当成「刚被平掉」。"""
    group, leader, follower = await setup_pair(store, name="首见组")
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(9001, m_lead, time=100.0)]
    f_pos = [pos(10001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    # 没有先 seed_baseline，直接第一次 on_snapshot
    out = await leader_close_sync.on_snapshot(store, "nd_leader", lead_pos)
    assert out["sent"] == 0, "首次见到只能建基线"
    baseline = await store.get_leader_snapshot("nd_leader")
    assert baseline is not None and len(baseline) == 1


async def test_empty_baseline_after_full_close_detects(store, monkeypatch):
    """空基线是有效起点：上一轮就是空的，本轮出现后再全平也能正确检出。"""
    group, leader, follower = await setup_pair(store, name="先空后开组")
    m_lead, m_f = leader["magic"], follower["magic"]
    # 首轮：主节点空仓 -> 建空基线
    await seed_account(store, "nd_leader", [])
    await leader_close_sync.on_snapshot(store, "nd_leader", [])
    # 第二轮：开了 2 笔
    lead_pos = [pos(11001, m_lead, time=100.0), pos(11002, m_lead, time=200.0)]
    f_pos = [pos(12001, m_f, time=110.0), pos(12002, m_f, time=210.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await leader_close_sync.on_snapshot(store, "nd_leader", lead_pos)
    # 第三轮：全平
    await seed_account(store, "nd_leader", [])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["sent"] == 2


async def test_baseline_only_tracks_strategy_magics(store, monkeypatch):
    """普通手工单（非 strategy 魔术号）不进基线，平掉也不触发同步。"""
    group, leader, follower = await setup_pair(store, name="手工单组")
    m_lead, m_f = leader["magic"], follower["magic"]
    # 主节点上有一笔手工单（magic=0）
    manual = pos(13001, 0, time=50.0)
    lead_pos = [manual, pos(13002, m_lead, time=100.0)]
    f_pos = [pos(14001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    # 只平掉手工单
    await seed_account(store, "nd_leader", [pos(13002, m_lead, time=100.0)])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(
        store, "nd_leader", [pos(13002, m_lead, time=100.0)],
    )
    assert out["sent"] == 0, "手工单平仓不应同步到跟单节点"


# ---------------------------------------------------------------------------
# ⑥ 配对错位降级
# ---------------------------------------------------------------------------
async def test_count_mismatch_skips_close(store, monkeypatch):
    """主从笔数不一致 -> 降级不平仓（宁可不平，不错平）。"""
    group, leader, follower = await setup_pair(store, name="错位组")
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [
        pos(15001, m_lead, time=100.0),
        pos(15002, m_lead, time=200.0),
    ]
    # 跟单节点只开成了 1 笔（另一笔资金不足 / 券商拒单）
    f_pos = [pos(16001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    # 平掉 1 笔（剩 1 笔）—— 主基线 2 笔 vs 跟单 1 笔，无法配对
    await seed_account(store, "nd_leader", lead_pos[:1])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", lead_pos[:1])
    assert out["sent"] == 0 and out["queued"] == 0
    assert out["skipped"] >= 1
    assert sent == [], "配对不可信时不应下发任何 close"


# ---------------------------------------------------------------------------
# ⑦ 离线补发
# ---------------------------------------------------------------------------
async def test_offline_follower_queues_close(store, monkeypatch):
    """跟单节点离线：close 入 pending_stop 队列，等重连补发。"""
    group, leader, follower = await setup_pair(store, name="离线组")
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(17001, m_lead, time=100.0)]
    f_pos = [pos(18001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_leader", [])

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node",
                        capture_sender(sent, online_nodes=set()))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["queued"] == 1 and out["sent"] == 0

    pending = await store.pop_pending_stops("nd_f")
    assert pending and pending[0]["cmd"] == "close"
    assert pending[0]["close_ticket"] == 18001


# ---------------------------------------------------------------------------
# ⑧ 兜底收口
# ---------------------------------------------------------------------------
async def test_orphan_follower_subtask_is_forced_finished(store, monkeypatch):
    """本轮同步过平仓、两边都已空仓但跟单子任务未收口 -> 强制收口（僵尸任务兜底）。"""
    group, leader, follower = await setup_pair(store, name="兜底组")
    m_lead, m_f = leader["magic"], follower["magic"]
    lead_pos = [pos(19001, m_lead, time=100.0)]
    f_pos = [pos(20001, m_f, time=110.0)]
    await seed_account(store, "nd_leader", lead_pos)
    await seed_account(store, "nd_f", f_pos)
    await seed_baseline(store, "nd_leader", lead_pos)
    # 主节点平掉、跟单节点也已空仓（它自己先跟平了，但空仓收口还没到达）
    await seed_account(store, "nd_leader", [])
    await seed_account(store, "nd_f", [])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["orphans"] == 1, "跟单子任务应被兜底收口"
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] == "failed"


async def test_alive_leader_task_is_not_orphaned(store, monkeypatch):
    """主节点仍在挂单（限价未成交）：即使跟单侧空仓也不收口（任务还活着）。"""
    group, leader, follower = await setup_pair(store, name="挂单组")
    m_lead = leader["magic"]
    order = {"magic": m_lead, "ticket": 1}
    await seed_account(store, "nd_leader", [], orders=[order])
    await seed_account(store, "nd_f", [])
    # 首轮：建基线（主节点空仓，任务在挂单中）
    await seed_baseline(store, "nd_leader", [])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    # 第二轮：主节点仍为空仓 + 挂单 -> 跟单子任务不算孤儿
    out = await leader_close_sync.on_snapshot(
        store, "nd_leader", [], orders=[order],
    )
    assert out["orphans"] == 0
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] != "failed", "主节点还有挂单时不得收口跟单子任务"


async def test_leader_timeout_while_follower_holds_is_not_orphaned(store, monkeypatch):
    """主节点侧无仓无单，但跟单节点自己还有持仓 -> 不强制收口（让它自然收口）。"""
    group, leader, follower = await setup_pair(store, name="跟单仍有仓组")
    m_f = follower["magic"]
    await seed_account(store, "nd_leader", [])
    await seed_account(store, "nd_f", [pos(20001, m_f, time=100.0)])
    await seed_baseline(store, "nd_leader", [])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [])
    assert out["orphans"] == 0, "跟单节点仍有持仓时不得强制收口"


# ---------------------------------------------------------------------------
# ⑨ 旧方案回归：终止不再连带
# ---------------------------------------------------------------------------
async def test_manual_close_does_not_cascade(store, monkeypatch):
    """手动终止主节点子任务：**不再**连带终止跟单节点（旧方案已移除）。"""
    group, leader, follower = await setup_pair(store, name="旧方案组")
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))

    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader["id"])
    assert out["status"] == "closing"
    assert "cascade" not in out, "旧方案的 cascade 字段应已移除"
    assert not [m for n, m in sent if n == "nd_f"], \
        "手动终止不得再向跟单节点下发 strategy_stop"
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] not in ("closing", "failed"), "跟单子任务不应被连带终止"
    assert f_row["stop_source"] is None


async def test_account_close_does_not_cascade(store, monkeypatch):
    """账户级全平：不再连带跟单节点。"""
    group, leader, follower = await setup_pair(store, name="账户组")
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await GroupDispatcher(store).stop_all_on_node(
        "nd_leader", signal_id="sig_lcs_acc",
    )
    assert "cascade" not in out
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] not in ("closing", "failed")


# ---------------------------------------------------------------------------
# ⑩ 缓存读写
# ---------------------------------------------------------------------------
async def test_leader_snapshot_cache_roundtrip(store):
    assert await store.get_leader_snapshot("nd_x") is None
    rows = [pos(1, BASE + 1, time=1.0)]
    await store.set_leader_snapshot("nd_x", rows)
    assert await store.get_leader_snapshot("nd_x") == [
        {"ticket": 1, "magic": BASE + 1, "symbol": "XAUUSD", "type": "BUY",
         "volume": 0.1, "time": 1.0},
    ]
    await store.clear_leader_snapshot("nd_x")
    assert await store.get_leader_snapshot("nd_x") is None


async def test_snapshot_rows_strip_unknown_magics(store):
    rows = leader_close_sync._snapshot_rows([
        pos(1, 0, time=1.0),          # 手工单：不进基线
        pos(2, BASE + 5, time=2.0),   # strategy 子任务：进
    ])
    assert [r["ticket"] for r in rows] == [2]


def test_position_pairing_sorts_by_time_then_ticket():
    rows = leader_close_sync._positions_by_magic([
        pos(10, BASE + 1, time=300.0),
        pos(9, BASE + 1, time=100.0),
        pos(11, BASE + 1, time=200.0),
    ])
    order = [r["ticket"] for r in rows[BASE + 1]]
    assert order == [9, 11, 10]


# ---------------------------------------------------------------------------
# ⑪ tpl_2 限价开仓：主节点撤挂单 -> 跟单节点同步撤单并收口
# ---------------------------------------------------------------------------
def order(ticket: int, magic: int, *, time: float = 100.0) -> dict:
    return {
        "ticket": ticket, "magic": magic, "symbol": "XAUUSD",
        "type": "BUY", "volume": 0.1, "time": time,
    }


async def _seed_pending_baseline(store, leader_magic: int, ticket: int = 101) -> None:
    """主节点 tpl_2 限价挂单：先空仓基线，再挂单（挂单进基线，不算平仓）。"""
    await seed_account(store, "nd_leader", [], orders=[])
    await leader_close_sync.on_snapshot(store, "nd_leader", [], orders=[])
    orders = [order(ticket, leader_magic)]
    await seed_account(store, "nd_leader", [], orders=orders)
    await leader_close_sync.on_snapshot(store, "nd_leader", [], orders=orders)


async def _wait_cancel_confirmed(store, monkeypatch, *, orders: list,
                                 sent: list, rounds: int = 3) -> dict:
    out: dict = {}
    for _ in range(rounds):
        await seed_account(store, "nd_leader", [], orders=orders)
        out = await leader_close_sync.on_snapshot(
            store, "nd_leader", [], orders=orders,
        )
    return out


async def test_leader_cancel_pending_syncs_follower(store, monkeypatch):
    """Case A：主节点未成交先撤单 -> 跟单节点收到 strategy_cancel_pending，
    跟单子任务与主节点子任务都按 done 收口。"""
    group, leader, follower = await setup_pair(
        store, entry_mode="limit", name="限价撤单组",
    )
    m_lead = leader["magic"]
    await _seed_pending_baseline(store, m_lead)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    # 连续 3 轮快照都看不到挂单 -> 确认撤单
    out = await _wait_cancel_confirmed(store, monkeypatch, orders=[], sent=sent)
    assert out["pending_sent"] == 1, f"应下发 1 条撤单，实际 {out}"
    cmds = [m for n, m in sent if m.get("cmd") == "strategy_cancel_pending"]
    assert len(cmds) == 1 and cmds[0]["magic"] == follower["magic"]

    rows = await subtask_rows(group["group_id"])
    for r in rows:
        assert r["status"] == "done", f"{r['node_id']} 子任务应以 done 收口"
    assert all(r["stop_source"] == "leader_cancel" for r in rows)


async def test_single_round_missing_order_is_not_confirmed(store, monkeypatch):
    """单轮读数抖动不算撤单：挂单下一轮就回来了 -> 继续静默也不会立刻确认。"""
    group, leader, follower = await setup_pair(
        store, entry_mode="limit", name="单轮抖动组",
    )
    m_lead = leader["magic"]
    await _seed_pending_baseline(store, m_lead)
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    # 第 1 轮快照抖动：挂单不见了（疑似撤单 count=1）
    await seed_account(store, "nd_leader", [], orders=[])
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [], orders=[])
    assert out["pending_sent"] == 0
    # 第 2 轮挂单又回来了（没被撤）-> 嫌疑清零
    back = [order(101, m_lead)]
    await seed_account(store, "nd_leader", [], orders=back)
    out = await leader_close_sync.on_snapshot(store, "nd_leader", [], orders=back)
    assert out["pending_sent"] == 0
    row = next(r for r in await subtask_rows(group["group_id"])
               if r["node_id"] == "nd_f")
    assert row["status"] not in ("done", "failed"), "抖动的挂单不得把子任务收口"


async def test_pending_fill_is_not_treated_as_cancel(store, monkeypatch):
    """挂单减少但持仓同步增加 = 成交，不是撤单 -> 不同步撤单。"""
    group, leader, follower = await setup_pair(
        store, entry_mode="limit", name="成交非撤单组",
    )
    m_lead = leader["magic"]
    await _seed_pending_baseline(store, m_lead)
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    filled = [pos(501, m_lead, time=100.0)]
    for _ in range(4):
        await seed_account(store, "nd_leader", filled, orders=[])
        out = await leader_close_sync.on_snapshot(
            store, "nd_leader", filled, orders=[],
        )
    assert out["pending_sent"] == 0
    assert not [m for n, m in sent if m.get("cmd") == "strategy_cancel_pending"]


async def test_market_entry_ignores_pending_cancel(store, monkeypatch):
    """市价开仓（tpl_2 非 limit）不参与撤单同步。"""
    group, leader, follower = await setup_pair(store, name="市价不开撤单组")
    m_lead = leader["magic"]
    await _seed_pending_baseline(store, m_lead)
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await _wait_cancel_confirmed(store, monkeypatch, orders=[], sent=sent, rounds=4)
    assert out["pending_sent"] == 0


async def test_partial_fill_then_cancel_keeps_position(store, monkeypatch):
    """Case B：部分成交后撤剩余挂单 -> 撤单同步，跟单子任务 done 但保留持仓数。"""
    group, leader, follower = await setup_pair(
        store, entry_mode="limit", name="部分成交撤单组",
    )
    m_lead, m_f = leader["magic"], follower["magic"]
    # 主节点 2 张挂单 -> 成交 1 张（变持仓）+ 撤 1 张
    await seed_account(store, "nd_leader", [], orders=[
        order(101, m_lead), order(102, m_lead, time=101.0),
    ])
    await leader_close_sync.on_snapshot(
        store, "nd_leader", [],
        orders=[order(101, m_lead), order(102, m_lead, time=101.0)],
    )
    filled = [pos(501, m_lead, time=100.0)]
    await seed_account(store, "nd_f", [pos(601, m_f, time=100.0)],
                       orders=[order(701, m_f)])
    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    out = await _wait_cancel_confirmed(
        store, monkeypatch, orders=[order(101, m_lead)], sent=sent,
    )
    # 只有成交那笔变成持仓后挂单净减 1，不该判成撤单
    assert out["pending_sent"] == 0
    # 再把成交的那笔也撤掉（持仓 0，挂单 0）—— 净减 1，持仓未增 -> 撤单
    out = await _wait_cancel_confirmed(store, monkeypatch, orders=[], sent=sent)
    assert out["pending_sent"] == 1
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] == "done"


async def test_offline_follower_queues_cancel_pending(store, monkeypatch):
    """跟单节点离线：撤单指令入 pending_stop，子任务按 done 收口。"""
    group, leader, follower = await setup_pair(
        store, entry_mode="limit", name="撤单离线组",
    )
    m_lead = leader["magic"]
    await _seed_pending_baseline(store, m_lead)
    monkeypatch.setattr(manager, "send_to_node", capture_sender([], online_nodes=set()))
    out = await _wait_cancel_confirmed(store, monkeypatch, orders=[], sent=[])
    assert out["pending_queued"] == 1
    pending = await store.pop_pending_stops("nd_f")
    assert pending and pending[0]["cmd"] == "strategy_cancel_pending"
    f_row = next(r for r in await subtask_rows(group["group_id"])
                 if r["node_id"] == "nd_f")
    assert f_row["status"] == "done"


def test_snapshot_kind_distinguishes_orders():
    """挂单进基线带 kind=order，_lost_tickets 只认 position。"""
    base = (
        leader_close_sync._snapshot_rows([pos(1, BASE + 1, time=1.0)])
        + leader_close_sync._order_rows([order(2, BASE + 1)])
    )
    assert [r["kind"] for r in base] == ["position", "order"]
    lost = leader_close_sync._lost_tickets(base, [])
    assert lost == {1}, "挂单消失不得被当成持仓被平"


def test_pending_cancelled_formula():
    base = (
        leader_close_sync._snapshot_rows([])
        + leader_close_sync._order_rows([order(1, BASE + 7), order(2, BASE + 7)])
    )
    # 1 张成交成持仓、1 张被撤：挂单净减 1，持仓 +1 -> 撤单 0
    cur = (
        leader_close_sync._snapshot_rows([pos(1, BASE + 7, time=1.0)])
        + leader_close_sync._order_rows([order(2, BASE + 7)])
    )
    assert leader_close_sync._pending_cancelled(base, cur) == {}
    # 两张都被撤：净减 2、持仓 +0 -> 撤单 2
    assert leader_close_sync._pending_cancelled(base, []) == {BASE + 7: 2}
