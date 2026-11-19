"""带单员终止任务 -> 跟单节点**连带终止**（终止连带）。

背景：分组策略交易时，一次信号只建一个主任务（task_id），但给每个参与节点
各建一个子任务（GroupTaskDispatch，魔术号各自分配）。带单员在**自己的主节点**
上终止任务时，跟单节点手里的同任务子任务必须一起终止 —— 否则带单员已经走了，
跟单节点还在跑，没人管、没人平仓。

覆盖的红线：
- 方向性：连带**只从带单方流向跟单方**。跟单员自己平仓 / 移出跟单，
  绝不能把带单员的任务停掉。
- 范围：任务级连带只限同一 task_id；频道 / 账户级连带按分组收敛。
- 逃生口：分组级 `stop_cascade_enabled=False` 时不连带。
- 离线：跟单节点离线时强制收口并把平仓入队，等重连补发。
- 存量：没有跟单节点的普通分组行为完全不变。
"""

import pytest
from sqlalchemy import delete, select

from app import follow_service, group_persist, group_service
from app.connections import manager
from app.db import SessionLocal, init_db
from app.group_dispatcher import GroupDispatcher
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
    """节点入库 + 置为在线（manager.nodes 有连接即视为在线）。"""
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


async def offline(store, *nodes):
    """节点已入库但没有连接（离线）。"""
    await online(store, *nodes)
    for n in nodes:
        manager.nodes.pop(n["node_id"], None)


_strategy_seq = iter(range(1, 10_000))


async def mk_strategy(store, *, symbol="XAUUSD", owner_user_id=None):
    """一条可绑定的趋势策略（tpl_2 = 带单市场）。"""
    sid = f"sty_c{next(_strategy_seq)}"
    rule = {
        "type": RULE_TYPE_RISK_SIZED, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0,
    }
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
    """建一个带单员用户（持 follow_leader 能力）。"""
    async with SessionLocal() as s:
        role = Role(code="mk_leader_c", name="带单员", capabilities='["follow_leader"]')
        s.add(role)
        await s.flush()
        role_id = role.id
        user = User(
            username="mkleaderc", display_name="带单员", role="user",
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
    """把某节点登记为该分组的跟单节点（并刷新分组缓存）。"""
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
    """伪造下发：只有 online_nodes 里的节点才算「在线可达」。

    online_nodes 为 None 时全部可达；否则其它节点视为离线（返回 False），
    用来验证离线跟单节点的强制收口 + 补发队列。
    """
    async def fake_send(node_id, msg):
        if online_nodes is not None and node_id not in online_nodes:
            return False
        sent.append((node_id, msg))
        return True

    return fake_send


async def dispatch_one(store, signal_id="sig_cascade"):
    """跑一次信号分发，产出主任务 + 各节点子任务。"""
    await GroupDispatcher(store).dispatch(
        TradingSignal(action="BUY", symbol="XAUUSD", volume=0.1, stop_loss=2397.0),
        signal_id,
    )


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
                "magic": r.magic,
            }
            for r in rows
        ]


async def leader_dispatch_id(group_id, node_id="nd_leader"):
    for row in await subtask_rows(group_id):
        if row["node_id"] == node_id:
            return row["id"]
    raise AssertionError(f"未找到节点 {node_id} 的子任务")


# ---------------------------------------------------------------------------
# ① 主节点终止 -> 跟单节点同任务连带终止
# ---------------------------------------------------------------------------
async def test_leader_close_cascades_to_followers(store, monkeypatch):
    """带单员在主节点终止任务：跟单节点的同 task_id 子任务一起终止。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "连带组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c1")

    rows = await subtask_rows(group["group_id"])
    assert {r["node_id"] for r in rows} == {"nd_leader", "nd_f"}, "前置：两个节点都应有子任务"
    task_ids = {r["task_id"] for r in rows}
    assert len(task_ids) == 1, "前置：两个节点应属于同一主任务"

    leader_did = await leader_dispatch_id(group["group_id"])
    sent.clear()
    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    assert out["status"] == "closing"
    assert out["cascade"]["total"] == 1, "跟单节点应被连带终止"
    assert out["cascade"]["nodes"] == ["nd_f"]

    # 跟单节点确实收到了 strategy_stop（原因标记为 leader_cascade_stop）
    stops = {n: m for n, m in sent if m.get("cmd") == "strategy_stop"}
    assert "nd_f" in stops, "跟单节点必须收到 strategy_stop"
    assert stops["nd_f"]["reason"] == "leader_cascade_stop"
    assert stops["nd_leader"]["reason"] == "manual_close", "带单员自己那条是手动终止"

    rows = await subtask_rows(group["group_id"])
    by_node = {r["node_id"]: r for r in rows}
    assert by_node["nd_f"]["status"] == "closing"
    assert by_node["nd_f"]["stop_source"] == "leader_cascade"
    assert by_node["nd_leader"]["stop_source"] == "self"


async def test_cascade_only_affects_same_task(store, monkeypatch):
    """任务级连带只限同一 task_id：同分组的另一个任务不受影响。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "连带组2", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c2_a")
    await dispatch_one(store, "sig_c2_b")

    rows = await subtask_rows(group["group_id"])
    task_ids = sorted({r["task_id"] for r in rows})
    assert len(task_ids) == 2, "前置：应有两个主任务"

    first_task = task_ids[0]
    did = next(r["id"] for r in rows
               if r["node_id"] == "nd_leader" and r["task_id"] == first_task)
    out = await GroupDispatcher(store).close_subtask(group["group_id"], did)

    assert out["cascade"]["total"] == 1, "只应连带同任务的那一个跟单子任务"
    rows = await subtask_rows(group["group_id"])
    other = [r for r in rows if r["task_id"] != first_task]
    assert all(r["status"] not in ("closing", "failed") for r in other), \
        "另一个主任务的子任务不应被连带"


# ---------------------------------------------------------------------------
# ② 方向性：跟单员自己终止不会反向停掉带单员
# ---------------------------------------------------------------------------
async def test_follower_close_does_not_cascade_back(store, monkeypatch):
    """跟单员在自己节点上终止子任务：带单员的任务**不受影响**。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "方向组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c3")

    follower_did = next(r["id"] for r in await subtask_rows(group["group_id"])
                        if r["node_id"] == "nd_f")
    out = await GroupDispatcher(store).close_subtask(group["group_id"], follower_did)

    assert out["cascade"]["total"] == 0, "跟单节点终止不得反向连带"
    rows = await subtask_rows(group["group_id"])
    leader_row = next(r for r in rows if r["node_id"] == "nd_leader")
    assert leader_row["status"] not in ("closing", "failed"), "带单员任务不应被停掉"
    assert leader_row["stop_source"] is None


async def test_leave_follow_does_not_cascade_back(store, monkeypatch):
    """跟单员移出跟单（close_tasks=True）：带单员的任务**不受影响**。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "移出组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c4")

    follower_did = next(r["id"] for r in await subtask_rows(group["group_id"])
                        if r["node_id"] == "nd_f")
    await GroupDispatcher(store).close_subtask(group["group_id"], follower_did,
                                               reason="leave_follow")
    rows = await subtask_rows(group["group_id"])
    leader_row = next(r for r in rows if r["node_id"] == "nd_leader")
    assert leader_row["status"] not in ("closing", "failed"), "移出跟单不得停掉带单员任务"


# ---------------------------------------------------------------------------
# ③ 逃生口：分组级 stop_cascade_enabled
# ---------------------------------------------------------------------------
async def test_cascade_disabled_suppresses_cascade(store, monkeypatch):
    """分组关闭「终止连带」：跟单节点不被连带（自己跑完）。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "不连带组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id, stop_cascade_enabled=False)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c5")

    leader_did = await leader_dispatch_id(group["group_id"])
    sent.clear()
    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    assert out["cascade"]["total"] == 0, "关闭连带后不得连带终止跟单节点"
    rows = await subtask_rows(group["group_id"])
    follower_row = next(r for r in rows if r["node_id"] == "nd_f")
    assert follower_row["status"] not in ("closing", "failed"), "跟单子任务应继续运行"
    assert follower_row["stop_source"] is None


async def test_cascade_toggle_persists(store):
    """分组级开关能改回来并落库。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "开关组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id, stop_cascade_enabled=False)

    d = await group_service.update_group(
        store, group["group_id"], GroupUpdate(stop_cascade_enabled=True),
    )
    assert d["stop_cascade_enabled"] is True
    cached = await store.get_group(group["group_id"])
    assert cached["stop_cascade_enabled"] is True


# ---------------------------------------------------------------------------
# ④ 离线跟单节点：强制收口 + 补发队列
# ---------------------------------------------------------------------------
async def test_offline_follower_forced_and_queued(store, monkeypatch):
    """跟单节点离线：强制收口子任务，并把平仓指令入队等重连补发。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "离线组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    # 先让两个节点都可达，把子任务建出来（跟单节点有在跑的子任务）
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c6")

    # 此刻跟单节点掉线：只有主节点可达
    monkeypatch.setattr(manager, "send_to_node",
                        capture_sender(sent, online_nodes={"nd_leader"}))

    leader_did = await leader_dispatch_id(group["group_id"])
    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    assert out["cascade"]["forced"] == 1, "离线跟单节点应被强制收口"
    assert out["cascade"]["total"] == 1

    rows = await subtask_rows(group["group_id"])
    follower_row = next(r for r in rows if r["node_id"] == "nd_f")
    assert follower_row["status"] in ("failed", "offline", "done"), \
        f"离线跟单子任务应已强制收口，实际 {follower_row['status']}"
    assert follower_row["stop_source"] == "leader_cascade"

    # 平仓指令已入队，等节点重连后补发
    pending = await store.pop_pending_stops("nd_f")
    assert pending, "离线节点的平仓指令应入队"
    assert pending[0]["cmd"] == "strategy_stop"
    assert pending[0]["reason"] == "leader_cascade_stop"


async def test_offline_leader_still_cascades(store, monkeypatch):
    """带单方自己离线时同样连带：强制收口跟单节点，不因发起方离线而漏掉。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "离线主组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c7")

    leader_did = await leader_dispatch_id(group["group_id"])
    # 主节点此刻也掉线：主节点自己强制收口，跟单节点照常被连带
    monkeypatch.setattr(manager, "send_to_node",
                        capture_sender(sent, online_nodes=set()))
    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    assert out["cascade"]["total"] == 1, "主节点离线也不得漏掉连带"


# ---------------------------------------------------------------------------
# ⑤ 频道级 / 账户级连带
# ---------------------------------------------------------------------------
async def test_account_close_cascades(store, monkeypatch):
    """账户级全平发生在带单方主节点上：跟单节点同分组任务一起终止。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "账户组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c8")

    out = await GroupDispatcher(store).stop_all_on_node(
        "nd_leader", signal_id="sig_c8_all",
    )
    assert out["cascade"]["total"] == 1, "账户级全平应连带跟单节点"
    rows = await subtask_rows(group["group_id"])
    follower_row = next(r for r in rows if r["node_id"] == "nd_f")
    assert follower_row["status"] == "closing"
    assert follower_row["stop_source"] == "leader_cascade"


async def test_channel_close_cascades(store, monkeypatch):
    """频道平仓发生在带单方主节点上：跟单节点同频道任务一起终止。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "频道组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c9")

    rows = await subtask_rows(group["group_id"])
    magic = next(r["magic"] for r in rows if r["node_id"] == "nd_leader")
    out = await GroupDispatcher(store).stop_magics_on_node(
        "nd_leader", [magic], signal_id="sig_c9_ch",
    )
    assert out["cascade"]["total"] == 1, "频道平仓应连带跟单节点"
    follower_row = next(r for r in await subtask_rows(group["group_id"])
                        if r["node_id"] == "nd_f")
    assert follower_row["status"] == "closing"


async def test_account_close_on_follower_node_does_not_cascade(store, monkeypatch):
    """跟单节点上做账户级全平：不反向连带带单员。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "反向组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c10")

    out = await GroupDispatcher(store).stop_all_on_node(
        "nd_f", signal_id="sig_c10_all",
    )
    assert out["cascade"]["total"] == 0, "跟单节点全平不得反向连带"
    leader_row = next(r for r in await subtask_rows(group["group_id"])
                       if r["node_id"] == "nd_leader")
    assert leader_row["status"] not in ("closing", "failed")


# ---------------------------------------------------------------------------
# ⑥ 存量回归：没有跟单节点的普通分组行为不变
# ---------------------------------------------------------------------------
async def test_group_without_followers_unchanged(store, monkeypatch):
    """没有跟单节点的分组：终止只影响自己，不报错、cascade 为 0。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_leader2", owner_user_id=leader_id))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "普通组", ["nd_leader", "nd_leader2"],
                           strategy=strategy, owner_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c11")

    leader_did = await leader_dispatch_id(group["group_id"])
    sent.clear()
    out = await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    assert out["status"] == "closing"
    assert out["cascade"]["total"] == 0
    rows = await subtask_rows(group["group_id"])
    other = next(r for r in rows if r["node_id"] == "nd_leader2")
    assert other["status"] not in ("closing", "failed"), "同组其它成员不应被连带"


async def test_leader_stop_event_recorded(store, monkeypatch):
    """连带终止会落一条 leader_stop 事件流记录（跟单员能看出是主人停的）。"""
    leader_id = await mk_leader(store)
    await online(store, mk_node("nd_leader", owner_user_id=leader_id, is_primary=True))
    await online(store, mk_node("nd_f"))
    strategy = await mk_strategy(store, owner_user_id=leader_id)
    group = await mk_group(store, "事件组", ["nd_leader"], strategy=strategy,
                           owner_user_id=leader_id)
    await add_follower(store, group["group_id"], "nd_f", leader_user_id=leader_id)

    sent: list = []
    monkeypatch.setattr(manager, "send_to_node", capture_sender(sent))
    await dispatch_one(store, "sig_c12")

    leader_did = await leader_dispatch_id(group["group_id"])
    await GroupDispatcher(store).close_subtask(group["group_id"], leader_did)

    async with SessionLocal() as s:
        events = (
            await s.execute(
                select(GroupTaskEvent).where(
                    GroupTaskEvent.event_type == "leader_stop",
                )
            )
        ).scalars().all()
    assert len(events) == 1, "应落一条 leader_stop 事件"
    assert events[0].node_id == "nd_f"
    assert events[0].detail_json.get("source_node_id") == "nd_leader"
