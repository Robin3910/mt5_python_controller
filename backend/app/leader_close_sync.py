"""主节点平仓 -> 跟单节点同步平仓。

## 为什么是「同步平仓」而不是「终止任务」

非网格策略（市价 / 限价开仓、AI 逆势加仓）在节点侧的既有语义是
**空仓即收口**：

- `node_client/market_hub._classify`：`hold_when_empty=False` 的订阅连续
  `empty_confirm` 轮读到空仓就发 `GONE`；
- `node_client/strategy_runner`：收到 `GONE` 先撤掉残留挂单，再
  `_finish("done", positions_cleared)`，任务结束；
- 服务端 `group_persist.reconcile_node_positions` 还有一层账户快照兜底。

所以「把仓位平掉」和「让任务结束」本来就是同一件事，服务器**不需要**
再补一刀 `strategy_stop`。更关键的是，`strategy_stop` 是「按 magic 全平 +
停监控」，做不出「主节点只平一笔、跟单节点也平一笔，任务继续跑」这种场景。

结论：这里只同步**平仓动作**（按 ticket 下发 `close`），任务是否结束交给
节点侧按空仓自然收口。

## 三块拼图

1. **感知**：持仓快照对比。上一轮基线里有、本轮没有了 -> 那一笔被平掉了。
   这是唯一能同时覆盖「手动在 MT5 里平」「券商止盈止损平」「系统平仓 API」
   三种来源的办法 —— 后两者我们没有事件可用。
2. **映射**：主节点与跟单节点在不同账户，ticket 各自独立。同一个 task 下，
   两侧各自的持仓按开仓时间排序，第 i 笔配对第 i 笔。
3. **执行**：`build_close_command(..., "ticket", ..., 跟单那一笔的 ticket)`。

## 方向性

只有**带单方自有节点**（`group.members`）上报的快照才会触发同步。跟单节点
自己的持仓变化不触发任何动作 —— 跟单员平仓不得影响带单员。

## 兜底

跟单节点离线导致 close 没执行成功时：close 立即入 `pending_stop` 队列等重连
补发；若主节点侧该任务已经彻底结束（无持仓、无挂单）而跟单节点仍有持仓，
再强制收口跟单子任务，避免留下僵尸任务占着节点占位。
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from . import group_persist, group_rules, strategy_templates
from .connections import manager
from .models import build_close_command
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

# 下发 close 失败（节点离线）时，入队等重连补发的 TTL
_PENDING_CLOSE_TTL = 24 * 3600

# 同步平仓的 source 标记（审计 / 日志用，不进 stop_source —— 这不是「终止」）
PENDING_REASON = "leader_close_sync"
# 同步撤单写进子任务的 stop_source（前端据此展示「主节点撤单同步」）
PENDING_SOURCE = "leader_cancel"


def _cmd_id() -> str:
    """同步平仓的命令 ID（关联审计与节点回报）。"""
    return "lcs_" + format(int(time.time() * 1000), "x") + secrets.token_hex(2)


def _position_key(row: dict) -> tuple:
    """持仓排序键：按开仓时间，同一时刻再按 ticket 兜底保证顺序稳定。

    配对完全依赖这个顺序 —— 主节点第 i 笔对应跟单节点第 i 笔。时间精度不够时
    ticket 兜底能避免同一拍开出的多笔在两侧排出不同顺序。
    """
    try:
        opened = float(row.get("time") or 0)
    except (TypeError, ValueError):
        opened = 0.0
    try:
        ticket = int(row.get("ticket") or 0)
    except (TypeError, ValueError):
        ticket = 0
    return (opened, ticket)


def _positions_by_magic(positions: list) -> dict[int, list[dict]]:
    """按魔术号归集持仓，每组内部按开仓时间排序（配对用）。"""
    out: dict[int, list[dict]] = {}
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        try:
            magic = int(row.get("magic") or 0)
        except (TypeError, ValueError):
            continue
        if magic <= 0:
            continue
        out.setdefault(magic, []).append(row)
    for rows in out.values():
        rows.sort(key=_position_key)
    return out


KIND_POSITION = "position"
KIND_ORDER = "order"


def _snapshot_rows(positions: list) -> list[dict]:
    """把持仓精简成基线需要的字段（全量持仓太大，且我们只关心这几列）。"""
    out: list[dict] = []
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        magic = row.get("magic")
        if group_rules.subtask_id_from_magic(magic) is None:
            continue  # 不是 strategy 子任务的魔术号，不进基线
        out.append({
            "ticket": row.get("ticket"),
            "magic": magic,
            "symbol": row.get("symbol"),
            "type": row.get("type"),
            "volume": row.get("volume"),
            "time": row.get("time"),
            "kind": KIND_POSITION,
        })
    return out


def _order_rows(orders: list) -> list[dict]:
    """把在途挂单精简成基线需要的字段，`kind="order"`。

    挂单与持仓合并进同一条基线：撤单同步需要「上一轮挂了多少张」，而挂单
    消失既可能是成交（变成持仓）也可能是被撤 —— 需要两类行一起算净变化。
    """
    out: list[dict] = []
    for row in orders or []:
        if not isinstance(row, dict):
            continue
        magic = row.get("magic")
        if group_rules.subtask_id_from_magic(magic) is None:
            continue
        out.append({
            "ticket": row.get("ticket"),
            "magic": magic,
            "symbol": row.get("symbol"),
            "type": row.get("type"),
            "volume": row.get("volume"),
            "time": row.get("time"),
            "kind": KIND_ORDER,
        })
    return out


def _lost_tickets(baseline: list[dict], current: list[dict]) -> set[int]:
    """上一轮有、本轮没有的**持仓** ticket（即被平掉的那些）。

    只认 `kind == "position"`：挂单从账本里消失不代表仓位被平（限价单撤回
    或被成交都不是平仓），把它算成平仓会把撤单误当成平仓下发给跟单节点。
    没有 kind 字段的旧基线行按持仓处理（兼容升级前的存量基线）。
    """
    alive = set()
    for row in current:
        try:
            alive.add(int(row.get("ticket") or 0))
        except (TypeError, ValueError):
            continue
    lost: set[int] = set()
    for row in baseline:
        if str(row.get("kind") or KIND_POSITION) != KIND_POSITION:
            continue
        try:
            ticket = int(row.get("ticket") or 0)
        except (TypeError, ValueError):
            continue
        if ticket and ticket not in alive:
            lost.add(ticket)
    return lost


def _count_by_magic(rows: list[dict], kind: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in rows or []:
        if str(row.get("kind") or KIND_POSITION) != kind:
            continue
        magic = _int(row.get("magic"))
        if magic is None:
            continue
        out[magic] = out.get(magic, 0) + 1
    return out


def _order_book(rows: list[dict]) -> dict[str, dict]:
    """把挂单行压成 `{magic: {"count": n, "tickets": [...]}}` 形式的挂单基线。

    与持仓基线分开存的原因见 `RedisStore.get_leader_order_book`。
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        if str(row.get("kind") or KIND_POSITION) != KIND_ORDER:
            continue
        magic = _int(row.get("magic"))
        if magic is None:
            continue
        key = str(magic)
        item = out.setdefault(key, {"count": 0, "tickets": []})
        item["count"] = int(item["count"]) + 1
        ticket = _int(row.get("ticket"))
        if ticket is not None:
            item["tickets"].append(ticket)
    return out


def _order_book_counts(rows: list[dict]) -> dict[int, int]:
    """本轮挂单行按 magic 归集（只取 kind=order）。"""
    return _count_by_magic(rows, KIND_ORDER)


async def _leader_groups_of(store: RedisStore, node_id: str) -> list[dict]:
    """该节点作为**带单方自有成员**参与、且开启了同步平仓的分组。

    只返回 `stop_cascade_enabled` 为真、且节点在 `members` 里的分组。
    节点可能是某个分组的跟单成员（在 `followers` 里），那种分组必须排除 ——
    跟单员的持仓变化不触发任何同步动作。
    """
    out: list[dict] = []
    for group in await store.all_groups():
        if not isinstance(group, dict):
            continue
        if not group.get("stop_cascade_enabled", True):
            continue
        members = {
            str(m.get("node_id") or "").strip()
            for m in (group.get("members") or [])
            if isinstance(m, dict)
        }
        if node_id not in members:
            continue
        followers = [
            f for f in (group.get("followers") or [])
            if isinstance(f, dict) and f.get("node_id")
        ]
        if not followers:
            continue
        out.append(group)
    return out


async def _send_close_to_node(store: RedisStore, node_id: str, ticket: int,
                              signal_id: str) -> str:
    """给节点下发一笔按 ticket 的平仓；返回 sent / queued。"""
    cmd = build_close_command(signal_id, "ticket", None, ticket)
    if await manager.send_to_node(node_id, cmd):
        return "sent"
    # 节点离线：入队等重连补发（与手动终止共用同一条队列）
    await store.push_pending_stop(node_id, cmd, _PENDING_CLOSE_TTL)
    return "queued"


def _is_limit_risk_sized(snapshot: object) -> bool:
    """该子任务的策略快照是否为 tpl_2「以损定量 + 限价开仓」。

    只有这种任务会在信号时把整批单一次性挂成限价等成交；撤单同步只对它有意义。
    """
    if not isinstance(snapshot, dict):
        return False
    rule = strategy_templates.pick_risk_sized_rule(snapshot.get("rules"))
    return bool(rule) and strategy_templates.is_limit_entry(rule)


async def _detect_pending_cancelled(
    store: RedisStore, node_id: str, current: list[dict],
    *, positions: list[dict],
) -> set[int]:
    """基于**挂单基线记忆**检出「主节点的限价挂单被撤光」。

    为什么不用「本轮净减少」：挂单消失的那一轮，持仓基线里的挂单行也一起
    被覆盖成空 —— 下一轮再对比就看不出「曾经挂过单」。用户手动撤单恰好
    撞在这个窗口里，撤单检出就永远不触发（跟单节点的挂单留在盘上）。

    这里用一份**独立的挂单基线**（`leader_order_book`）记住每个魔术号
    曾经挂过多少张：
    - 基线里记着 N 张，本轮一张都没有 -> 挂单被撤光 -> 撤单；
    - 本轮还剩几张 -> 只是部分撤单，继续等（但要记住撤过，等它归零时才算）。

    成交不会被误判：成交会让持仓 +1、挂单等量减少，本轮仍有挂单时
    继续等；只有「挂单归零 **且** 持仓没有相应增加」才判撤单。

    返回已确认撤单的 magic 集合。
    """
    book = await store.get_leader_order_book(node_id)
    cur_counts = _order_book_counts(current)
    cur_pos = _count_by_magic(positions, KIND_POSITION)
    confirmed: set[int] = set()
    next_book: dict[str, dict] = {}
    prev_book = book or {}

    for key, prev in prev_book.items():
        try:
            magic = int(key)
        except (TypeError, ValueError):
            continue
        prev_count = int((prev or {}).get("count") or 0)
        if prev_count <= 0:
            continue
        cur = int(cur_counts.get(magic, 0))
        pos_now = int(cur_pos.get(magic, 0))
        pos_prev = int((prev or {}).get("position_count") or 0)
        if cur >= prev_count:
            # 挂单还在（数量没少）：不是撤单，继续记住基线。
            next_book[key] = {
                "count": cur,
                "tickets": list((prev or {}).get("tickets") or []),
                "position_count": pos_now,
            }
            continue
        # 挂单比基线少了：可能是撤单，也可能是成交（成交 → 持仓 +1）。
        filled = max(pos_now - pos_prev, 0)
        lost = prev_count - cur
        cancelled = lost - filled
        if cancelled <= 0:
            # 减少量能被持仓增长解释 -> 是成交，不是撤单。继续等。
            next_book[key] = {
                "count": cur,
                "tickets": list((prev or {}).get("tickets") or []),
                "position_count": pos_now,
            }
            continue
        if cur > 0:
            # 部分撤单：还剩几张挂单在盘上。等它们也归零再确认 ——
            # 但要把「撤过几张」记下来，避免下一轮基线被覆盖后忘了。
            next_book[key] = {
                "count": cur,
                "tickets": list((prev or {}).get("tickets") or []),
                "position_count": pos_now,
                "cancelled": int((prev or {}).get("cancelled") or 0) + cancelled,
            }
            continue
        # 挂单归零，且减少量无法用成交解释 -> 撤单。
        # 不写进 next_book：这一批挂单已经结束，下一轮不该再检出同一批。
        confirmed.add(magic)

    # 本轮仍挂着的单：记进基线。已经确认撤单的魔术号若又挂上了新的单
    # （策略重新挂单），也要能重新跟踪 —— 否则新挂的单被撤掉时检不出来。
    for magic, cnt in cur_counts.items():
        key = str(magic)
        if key in next_book:
            continue
        next_book[key] = {
            "count": cnt,
            "tickets": [],
            "position_count": int(cur_pos.get(magic, 0)),
        }

    await store.set_leader_order_book(node_id, next_book)
    return confirmed


async def _send_cancel_pending_to_node(
    store: RedisStore, sub: dict, reason: str,
) -> str:
    """给跟单子任务下发「撤挂单 + 收口」；返回 sent / queued。"""
    from .group_dispatcher import build_strategy_cancel_pending_command

    cmd = build_strategy_cancel_pending_command(
        signal_id=sub.get("signal_id") or _cmd_id(),
        task_id=int(sub["task_id"]),
        dispatch_id=int(sub["dispatch_id"]),
        magic=_int(sub.get("magic")),
        group_id=str(sub.get("group_id") or ""),
        symbol=sub.get("symbol"),
        reason=reason,
    )
    node_id = str(sub.get("node_id") or "")
    if await manager.send_to_node(node_id, cmd):
        return "sent"
    # 节点离线：入「待补发策略指令」队列，重连后只重发 strategy_* 指令
    await store.push_pending_stop(node_id, cmd, _PENDING_CLOSE_TTL)
    return "queued"


def _is_limit_risk_sized(snapshot: object) -> bool:
    """该子任务的策略快照是否为 tpl_2「以损定量 + 限价开仓」。

    只有这种任务会在信号时把整批单一次性挂成限价等成交；撤单同步只对它有意义。
    """
    if not isinstance(snapshot, dict):
        return False
    rule = strategy_templates.pick_risk_sized_rule(snapshot.get("rules"))
    return bool(rule) and strategy_templates.is_limit_entry(rule)


async def _sync_cancel_pending(
    store: RedisStore, group: dict, leader_node_id: str,
    leader_magic: int,
) -> dict:
    """主节点撤掉了 tpl_2 限价挂单 -> 跟单节点撤掉自己名下同任务的全部挂单并收口。

    与同步平仓不同，这里**同时**要「撤单」和「收口」：限价单在成交前只有挂单
    没有持仓，节点侧「空仓即收口」的语义走不到（`_opened` 从未置位）。
    所以每个跟单子任务都下发一条 `strategy_cancel_pending`，由节点侧撤单
    干净后回报 `strategy_finished(done)`；节点离线时入队，并直接按 done 收口
    子任务（避免僵尸任务占住节点占位，重连后补发的指令只负责撤孤儿单）。

    主节点自己的子任务同样要收口（它也不会被空仓收口兜住）。

    返回 {sent, queued, finished, reason}。
    """
    out = {"sent": 0, "queued": 0, "finished": 0, "released": [], "reason": None}
    # 主节点子任务**不能**只查非终态：主节点撤掉挂单后，它自己的子任务通常
    # 已经被账户对账收口成 done（挂单消失 → 该魔术号没痕迹 → 自动收口）。
    # 只查活动子任务会查不到 → 撤单指令永远发不出去 → 跟单节点的挂单成孤儿。
    leader_subs = await group_persist.subtasks_for_magic(
        group["group_id"], leader_node_id, leader_magic,
    )
    if not leader_subs:
        return out
    leader_sub = leader_subs[0]
    if not _is_limit_risk_sized(leader_sub.get("strategy_snapshot")):
        out["reason"] = "not_limit_entry"
        return out
    task_id = int(leader_sub["task_id"])

    follower_nodes = {
        str(f.get("node_id") or "").strip()
        for f in (group.get("followers") or [])
        if isinstance(f, dict) and f.get("node_id")
    }
    subs = await group_persist.active_follower_subtasks_for_task(
        task_id, follower_nodes,
    )

    for sub in subs:
        # 跟单子任务如果已经吃进过部分成交（有持仓），撤单只撤剩余挂单，
        # 持仓保留、由用户自己管理 —— 状态依然是 done（不是失败）。
        state = await _send_cancel_pending_to_node(store, sub, PENDING_REASON)
        if state == "sent":
            out["sent"] += 1
        else:
            out["queued"] += 1
        # 无论在线与否都按 done 收口：在线节点的撤单已下达（节点侧撤完会回报
        # strategy_finished，命中终态即忽略），离线时的指令已入队。
        # 不等节点回报，避免节点侧撤单卡住时子任务一直挂着。
        f_magic = _int(sub.get("magic"))
        f_acct = await store.get_account(str(sub.get("node_id") or "")) or {}
        f_held = (
            len(_positions_by_magic(f_acct.get("positions") or []).get(f_magic, []))
            if f_magic is not None else 0
        )
        res = await group_persist.force_finish_subtasks(
            [int(sub["dispatch_id"])],
            reason=PENDING_REASON,
            status="done",
            residual_positions=f_held,
        )
        if res:
            out["finished"] += 1
            out["released"].extend(res.get("released") or [])
        await group_persist.mark_dispatches_cancel_source(
            [int(sub["dispatch_id"])],
        )
        await group_persist.record_leader_stop_event(
            task_id=task_id, node_id=str(sub.get("node_id") or ""),
            magic=_int(sub.get("magic")),
            message="带单员撤消了限价挂单，已同步撤单并结束任务"
            if state == "sent" else
            "带单员撤消了限价挂单，节点离线：已结束任务，待重连后补发撤单",
            event_type="leader_cancel",
        )

    # 主节点自己：挂单已撤，不会再成交，子任务按 done 收口
    res = await group_persist.force_finish_subtasks(
        [int(leader_sub["dispatch_id"])],
        reason=PENDING_REASON,
        status="done",
        residual_positions=0,
    )
    if res:
        out["finished"] += 1
        out["released"].extend(res.get("released") or [])
    await group_persist.mark_dispatches_cancel_source(
        [int(leader_sub["dispatch_id"])],
    )
    return out


async def _sync_task_group(
    store: RedisStore, group: dict, leader_node_id: str,
    leader_magic: int, lost_in_magic: list[dict], signal_id: str,
) -> dict:
    """处理主节点某个 magic 下被平掉的若干笔。

    返回 {sent, queued, skipped, reason}。
    """
    out = {"sent": 0, "queued": 0, "skipped": 0, "reason": None, "mismatch": False}
    leader_subs = await group_persist.active_subtasks_for_magic(
        group["group_id"], leader_node_id, leader_magic,
    )
    if not leader_subs:
        return out
    task_id = int(leader_subs[0]["task_id"])

    follower_nodes = {
        str(f.get("node_id") or "").strip()
        for f in (group.get("followers") or [])
        if isinstance(f, dict) and f.get("node_id")
    }
    subs = await group_persist.active_follower_subtasks_for_task(
        task_id, follower_nodes,
    )
    if not subs:
        return out

    # 主节点该 magic 下的**当前**持仓不参与配对索引：索引取自基线（按开仓时间排好），
    # 而被平掉的那几笔正是基线里有、当前没有的。归集 f_positions 时再核对笔数。
    baseline = await store.get_leader_snapshot(leader_node_id) or []
    baseline_sorted = sorted(
        [r for r in baseline if _int(r.get("magic")) == leader_magic],
        key=_position_key,
    )
    index_of: dict[int, int] = {}
    for idx, row in enumerate(baseline_sorted):
        try:
            index_of[int(row.get("ticket") or 0)] = idx
        except (TypeError, ValueError):
            continue
    if not index_of:
        return out

    for sub in subs:
        f_node = str(sub.get("node_id") or "")
        f_magic = _int(sub.get("magic"))
        if f_magic is None:
            continue
        f_acct = await store.get_account(f_node) or {}
        f_positions = _positions_by_magic(f_acct.get("positions") or []).get(f_magic, [])
        # 配对前提：两侧笔数一致（同一 task 下应开同样多笔）。
        # 不一致说明有笔开仓失败 / 被单独平掉，此时索引不可信 —— 宁可不平，也不错平。
        if len(f_positions) != len(baseline_sorted):
            logger.warning(
                "leader-close-sync skip task %s group %s follower %s: "
                "position count mismatch leader=%d follower=%d",
                task_id, group["group_id"], f_node,
                len(baseline_sorted), len(f_positions),
            )
            out["skipped"] += 1
            out["reason"] = "position_count_mismatch"
            out["mismatch"] = True
            continue
        for lost in lost_in_magic:
            ticket = _int(lost.get("ticket"))
            if ticket is None:
                continue
            idx = index_of.get(ticket)
            if idx is None or idx >= len(f_positions):
                out["skipped"] += 1
                out["reason"] = "no_pair"
                continue
            target = f_positions[idx]
            target_ticket = _int(target.get("ticket"))
            if target_ticket is None:
                out["skipped"] += 1
                out["reason"] = "no_pair"
                continue
            # 每笔都在下发前核对一次「跟单节点当前确实还有这张单」：即便上游配对
            # 出现偏差，也不会向一个已不存在的 ticket 发 close。
            if not await _follower_has_ticket(
                store, f_node, f_magic, target_ticket,
            ):
                out["skipped"] += 1
                out["reason"] = "already_closed"
                continue
            state = await _send_close_to_node(store, f_node, target_ticket, signal_id)
            if state == "sent":
                out["sent"] += 1
            else:
                out["queued"] += 1
    return out


async def _follower_has_ticket(
    store: RedisStore, node_id: str, magic: int, ticket: int,
) -> bool:
    """跟单节点当前账本里是否仍有这张票（防止向已平掉的仓位重复下发 close）。"""
    acct = await store.get_account(node_id) or {}
    for row in _positions_by_magic(acct.get("positions") or []).get(magic, []):
        if _int(row.get("ticket")) == ticket:
            return True
    return False


async def _fallback_finish_orphans(
    store: RedisStore, group: dict, leader_node_id: str,
    leader_acct: dict, touched_magics: set[int],
) -> dict:
    """兜底：本轮**同步过平仓**的主节点子任务，跟单侧空仓了但子任务仍没收口 -> 强制收口。

    这是「主节点和跟单节点都已经空仓，但跟单子任务还挂着」的僵尸任务：
    正常路径下跟单节点会在若干拍后自己读到空仓收口，这里只处理收口迟迟不来的极端情况。

    触发条件（三条都必须满足，刻意收紧 —— 宁可留着让节点自己收口，也不要越权停任务）：
    1. 主节点那个 magic 本轮确实检出了平仓（`touched_magics`）—— 限价开仓在挂单成交前
       主节点侧既无持仓也无本轮平仓，绝不能被误判成「已结束」；
    2. 主节点侧该 magic 已无持仓、无挂单（任务确实结束）；
    3. 跟单节点侧**它自己的** magic 也已无持仓、无挂单（确实空仓）。

    注意主从两侧 magic 不同（同一 task 下各自分配），判定要按各自的 magic，
    不能拿跟单子任务的 magic 去和主节点的 magic 集合比。

    返回 {finished, released}；released 是需要释放的 (group_id, node_id) 占位。
    """
    out = {"finished": 0, "released": []}
    if not touched_magics:
        return out
    # 从缓存读最新分组：调用方手里的 dict 可能是建组那一刻的快照（跟单成员是后面加的）
    group = await store.get_group(str(group.get("group_id") or "")) or group
    leader_alive = set(_positions_by_magic(leader_acct.get("positions") or []))
    for order in leader_acct.get("orders") or []:
        if isinstance(order, dict):
            magic = _int(order.get("magic"))
            if magic is not None:
                leader_alive.add(magic)

    follower_nodes = {
        str(f.get("node_id") or "").strip()
        for f in (group.get("followers") or [])
        if isinstance(f, dict) and f.get("node_id")
    }
    for f_node in follower_nodes:
        subs = await group_persist.active_subtasks_for_node(f_node)
        f_acct = await store.get_account(f_node) or {}
        f_alive = set(_positions_by_magic(f_acct.get("positions") or []))
        for order in f_acct.get("orders") or []:
            if isinstance(order, dict):
                magic = _int(order.get("magic"))
                if magic is not None:
                    f_alive.add(magic)
        for sub in subs:
            if str(sub.get("group_id") or "") != str(group["group_id"]):
                continue
            task_id = _int(sub.get("task_id"))
            f_magic = _int(sub.get("magic"))
            if task_id is None or f_magic is None:
                continue
            # ① 主节点在该 task 下是否「本轮平过仓」+「确实已结束」
            leader_subs = await group_persist.active_subtasks_for_task(
                str(group["group_id"]), task_id,
            )
            leader_subs = [s for s in leader_subs if str(s.get("node_id")) == leader_node_id]
            if not leader_subs:
                continue
            l_magic = _int(leader_subs[0].get("magic"))
            if l_magic is None or l_magic not in touched_magics:
                continue  # 主节点该任务本轮没平过仓，不归这条兜底管
            if l_magic in leader_alive:
                continue  # 主节点侧任务还活着（有仓或挂单）
            # ③ 跟单节点自己还有仓位/挂单 -> 交给它自己的空仓收口
            if f_magic in f_alive:
                continue
            # 主节点已结束、跟单节点也已空仓，但子任务还没收口 -> 强制收口
            res = await group_persist.force_finish_subtasks(
                [int(sub["dispatch_id"])], reason="leader_close_orphan",
            )
            if res:
                out["finished"] += 1
                out["released"].extend(res.get("released") or [])
    return out


def _int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def on_snapshot(store: RedisStore, node_id: str, positions: list,
                      orders: list | None = None) -> dict:
    """账户快照到达时调用：对比主节点持仓/挂单基线，同步平仓与撤单到跟单节点。

    **必须在子任务收口（`_reconcile_strategy_tasks`）之前调用** —— 否则跟单节点
    可能先自己读到空仓收口，之后再发 close 就没人认领了。

    `orders` 用于两件事：撤单检出（tpl_2 限价开仓主节点撤掉挂单）与兜底判断
    「主节点侧任务是否还活着」。挂单消失**不会**被当成持仓被平（`kind` 区分）。

    返回统计 dict（供日志 / 测试断言）。
    """
    result = {
        "triggered": False, "sent": 0, "queued": 0, "skipped": 0, "orphans": 0,
        "released": [], "pending_sent": 0, "pending_queued": 0, "pending_finished": 0,
    }
    groups = await _leader_groups_of(store, node_id)
    if not groups:
        # 不是任何分组的带单方节点：只需维护基线（万一以后成为带单方），
        # 但为避免空跑，这里直接返回并不写基线 —— 首次成为带单方时再建。
        return result

    order_rows = _order_rows(orders or [])
    current = _snapshot_rows(positions) + order_rows
    baseline = await store.get_leader_snapshot(node_id)
    if baseline is None:
        # 首见 / 基线过期：只建基线，不对比。
        # 服务重启后基线为空，若直接对比会把「当前所有持仓」判成「新开的」，
        # 反过来还会把「空基线」当成「上一轮空仓」而漏掉所有平仓。
        await store.set_leader_snapshot(node_id, current)
        # 挂单基线也要建：撤单检出靠它记住「曾经挂过多少张」。
        await store.set_leader_order_book(
            node_id, _order_book(current),
        )
        return result

    lost = _lost_tickets(baseline, current)
    touched_magics: set[int] = set()
    # 检出平仓、但**没能配对下发**的 magic：交给兜底，避免子任务因「配不上」而永久挂着
    unpaired_magics: set[int] = set()
    if lost:
        signal_id = _cmd_id()
        # 被平掉的笔按 magic 归集，逐个 task 处理（一个 magic 对应一个子任务）
        lost_by_magic: dict[int, list[dict]] = {}
        for row in baseline:
            if str(row.get("kind") or KIND_POSITION) != KIND_POSITION:
                continue
            ticket = _int(row.get("ticket"))
            if ticket is None or ticket not in lost:
                continue
            magic = _int(row.get("magic"))
            if magic is None:
                continue
            lost_by_magic.setdefault(magic, []).append(row)
        touched_magics = set(lost_by_magic)
        for group in groups:
            for magic, rows in lost_by_magic.items():
                out = await _sync_task_group(
                    store, group, node_id, magic, rows, signal_id,
                )
                result["sent"] += out["sent"]
                result["queued"] += out["queued"]
                result["skipped"] += out["skipped"]
                # 配对被跳过（笔数不一致 / 找不到对位）的 magic 也交兜底：
                # 若两边其实都已空仓，那条子任务就是僵尸任务，应当收口，
                # 而不是因为「配不上」就一直挂着。
                if out["skipped"] and not (out["sent"] or out["queued"]):
                    unpaired_magics.add(magic)
        if result["sent"] or result["queued"]:
            result["triggered"] = True
            logger.info(
                "leader-close-sync node %s -> sent %d, queued %d, skipped %d",
                node_id, result["sent"], result["queued"], result["skipped"],
            )

    # 撤单检出：tpl_2 限价开仓挂单被撤光 -> 同步撤单到跟单节点。
    # 用独立的「挂单基线记忆」判定，不依赖本轮净减少（净减少会被基线覆写抹平）。
    cancelled = await _detect_pending_cancelled(
        store, node_id, current, positions=_snapshot_rows(positions),
    )
    if cancelled:
        for group in groups:
            for magic in cancelled:
                out = await _sync_cancel_pending(store, group, node_id, magic)
                result["pending_sent"] += out["sent"]
                result["pending_queued"] += out["queued"]
                result["pending_finished"] += out["finished"]
        if result["pending_sent"] or result["pending_queued"]:
            result["triggered"] = True
            logger.info(
                "leader-cancel-pending node %s magics %s -> sent %d, queued %d, finished %d",
                node_id, sorted(cancelled), result["pending_sent"],
                result["pending_queued"], result["pending_finished"],
            )

    # 基线更新放在同步之后：即使上面某笔失败，下一轮基线已是「当前持仓」，
    # 不会重复检出同一笔（避免死循环补发）。失败补偿交给 pending_stop 队列。
    await store.set_leader_snapshot(node_id, current)

    # 兜底：只是给「本轮同步过平仓、两边都已空仓、但跟单子任务迟迟没收口」的僵尸
    # 任务兜底；没有本轮平仓的 magic 一概不碰（限价开仓在成交前不能被误判成结束）。
    acct = {"positions": positions, "orders": orders or []}
    for group in groups:
        orphans = await _fallback_finish_orphans(
            store, group, node_id, acct, touched_magics | unpaired_magics,
        )
        result["orphans"] += orphans["finished"]
        result["released"].extend(orphans["released"])
    return result