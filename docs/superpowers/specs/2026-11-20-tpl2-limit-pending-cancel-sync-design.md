# tpl_2 限价开仓：主节点撤单 → 跟单节点同步撤单

日期：2026-11-20（v4：确认 tpl_2 **挂一次即止、绝不补挂**，去掉作废档位机制）
范围：**仅 tpl_2（以损定量）· 限价开仓**
状态：方案待确认，未实施
前置：[`2026-11-19-leader-close-position-sync-design.md`](./2026-11-19-leader-close-position-sync-design.md)（同步平仓，已实施）

---

## 0. 结论先行

**节点侧不需要实现任何「补挂 / 作废档位」逻辑 —— tpl_2 本来就不会补挂。**

tpl_2 限价开仓的挂单生命周期是**一次性的**：

| 时机 | 动作 | 位置 |
|---|---|---|
| 任务启动 | 底仓 + N 张分散仓**一次全部挂出** | `_risk_open_base` → `_risk_open_pending_distribute` |
| 之后 | **不再挂任何新单** | — |

`_risk_advance` 里确实调用 `pending_batches`（`strategy_runner.py:4238`），但 tpl_2 限价模式下
这个列表**只在任务启动那一拍非空**：`_risk_open_pending_distribute` 会一次性把所有档打齐，
每档成功后 `total_orders += 1`（`strategy_runner.py:4279`），所以从第二拍起
`pending_batches(plan, filled=total_orders)` 恒为空。

> 这一条是设计意图而非巧合：限价开仓的挂单价来自信号的入场价（`risk_sizing.plan_entries`
> 里 `pending_price = plan.entry_price if plan.is_limit_entry else 0.0`，所有 `Batch` 同价），
> 是一个**固定的计划**，不需要像 tpl_1 的逆势限价那样随波幅自适应推远、也没法在价格走过去之后
> 改挂到更近的位置。所以「挂一次即止」是唯一自洽的做法。

**唯一残留的可挂空档**：启动那一刻某档挂单失败（券商拒单 / 品种规格缺失）→
`total_orders` 没增加 → 下一拍 `pending_batches` 仍返回它。这是**启动期的重试**，
不是「撤了再补」。它的存在不影响本方案（见 §6）。

---

## 1. 场景

```
主节点：tpl_2 限价开仓 → 底仓 + N 张分散仓限价单挂在盘上
        用户放弃这个机会 → 在 MT5 里把剩余挂单撤了
          ├─ 情况 A：一张都没成交过（无持仓、全撤）
          └─ 情况 B：底仓/部分分散仓已成交（有持仓，撤掉剩余的）
跟单节点：同样的挂单还挂着
        → 必须一起撤掉（已成交的持仓不动），并且任务结束
```

| | 处理 |
|---|---|
| tpl_2 限价，主节点**全撤**（情况 A） | ✅ 跟单节点全撤 + 收口 |
| tpl_2 限价，主节点**撤掉剩余**（情况 B，仍有持仓） | ✅ 跟单节点撤剩余 + 收口，**持仓保留** |
| tpl_1 / tpl_3 | ❌ 不做 |
| 跟单节点自己撤单 | ❌ 不向主节点同步（方向性） |

**收口**：两种情况主节点自己、跟单节点都收口为 `done`，`finish_reason = "leader_cancelled"`。

---

## 2. 感知

### 2.1 基线结构

扩展 `follow:leader_snapshot:{node_id}`，同时存持仓与挂单，每行打 `kind`：

```json
[
  {"kind": "position", "ticket": 1001, "magic": 900000001, "symbol": "EURUSD",
   "type": 0, "volume": 0.1, "time": 100.0},
  {"kind": "order", "ticket": 1004, "magic": 900000001, "comment": "R3B1",
   "price_open": 2390.0, "volume": 0.1, "time": 150.0}
]
```

**必须同步修改**：`leader_close_sync._lost_tickets` 目前把基线里所有行的 ticket 一视同仁，
加了 order 行之后，挂单消失会被同步平仓误判成「持仓被平」→ 对着不存在的仓位发 `close`。
改成只看 `kind == "position"` 的行。这是本次改动最容易踩的坑。

### 2.2 检出：撤单 vs 成交

判定按 magic，用三个计数的差值：

```
lost_orders = 基线 order 数 − 本轮 order 数
gained_pos  = 本轮 position 数 − 基线 position 数
cancelled   = lost_orders − max(gained_pos, 0)
```

| 主节点行为 | lost | gained | cancelled | 判定 |
|---|---|---|---|---|
| 撤 2 张，无成交 | 2 | 0 | **2** | 撤单 ✅ |
| 成交 2 张 | 2 | 2 | 0 | 成交，不动作 |
| 撤 2 张 + 成交 1 张 | 3 | 1 | **2** | 部分撤单 ✅ |
| 有持仓、撤空剩余 | k | 0 | **k** | 撤单 ✅ |
| 正常挂着等成交 | 0 | 0 | 0 | 不动作 |

`cancelled > 0` 且该任务属于 **tpl_2 限价** 时才推进。

### 2.3 必须堵死的误判

**① 首单还没挂出 / 采样间隙**
改用**独立的挂单基线记忆**（`leader_order_book`）判定：只有「基线里记着 N 张，
本轮一张都没有，且持仓没有相应增加」才判撤单。

挂单基线与持仓基线**分开存**：持仓基线每轮整体覆写，挂单消失后就没法判断
「是被撤了」还是「本来就没有」。有了独立基线，撤单检出**单轮即可**（不再需要
跨轮确认），延迟从 15 秒降到 1 个快照周期。

**② 主节点任务本来就已经结束**
查 `group_persist.subtasks_for_magic(group_id, leader_node_id, magic)` —— **不能**
用 `active_subtasks_for_magic`：主节点撤掉挂单后，它自己的子任务通常已被
账户对账收口成 `status="done"`（挂单消失 → 该魔术号没痕迹 → 自动收口）。
只查活动子任务会查不到 → 撤单指令永远发不出去 → 跟单节点的挂单成孤儿单。

**③ 账本读不到（断联）**
`books_ok=False` 时 `on_snapshot` 不会被调用（`ws_gateway.py:379`），已有保障。
节点侧另有 `_books_watch`：读挂单失败时**不补报**快照（读不到 ≠ 变了）。

**④ 不是 tpl_2 限价任务**
判定前查主节点子任务的策略快照：`template_id == "tpl_2"` 且规则 `entry_mode` 是限价。

---

## 3. 配对

只需找到「同一个 task 下的跟单子任务」，不用逐 ticket 配对：

```python
leader_subs = await group_persist.subtasks_for_magic(
    group_id, leader_node_id, leader_magic,
)
task_id = leader_subs[0]["task_id"]
subs = await group_persist.active_follower_subtasks_for_task(task_id, follower_nodes)
```

指令里带 `leader_positions`（主节点撤单后剩余持仓数），供跟单节点判断「要不要保留持仓」。
保留的配对语义沿用同步平仓：同 task 下两侧持仓按 `(time, ticket)` 排序，第 i 笔对第 i 笔。

---

## 4. 节点侧：一次撤单 + 收口即可

因为 tpl_2 不会补挂（§0），节点侧不存在「补挂要拦」的问题，
只需要：

1. **撤掉本任务的挂单**
2. **收口，且不平仓**（情况 B 有持仓）

### 4.1 复用现成的撤单流程

`_cancel_remaining_pending_orders`（`strategy_runner.py:5401`）已经是

```
读 magic 下全部挂单 → 逐张 cancel_order → 复查 → 返回是否干净
```

`tpl_2` 场景下内部的分支（手动分散仓 / 逆势限价 / 指定价）都是空集，直接走兜底那一步。
不需要新写撤单逻辑。

### 4.2 不能复用 `_close_all`（request_stop）

`request_stop` → `_close_all` → `close_by_magic`（`strategy_runner.py:5494`），
**会把持仓一起平掉**。情况 B 必须保留已成交的持仓，所以另开一条路径。

### 4.3 新增字段与方法

```python
# 撤单并收口模式：进入后不再挂新单（见 §6 的重试场景），撤干净即 done
self._cancel_only: bool = False
```

```python
def request_cancel_pending(self, *, reason: str = "leader_cancelled") -> None:
    """撤掉本任务挂单并收口（不平仓）。

    与 request_stop 的区别：request_stop 走 _close_all → close_by_magic，会把
    持仓一起平掉。本场景要的是「只撤挂单、保留已成交的持仓」。
    """
    self._cancel_only = True
    self._stop_reason = reason
    if self._sub is not None:
        self._sub.wake()
```

### 4.4 收口分支

`_run` 循环开头（`strategy_runner.py:823`）：

```python
while True:
    if self._cancel_only and await self._cancel_pending_and_finish():
        return
    if self._stopping and await self._close_all():
        return
    ...
```

```python
async def _cancel_pending_and_finish(self) -> bool:
    """撤掉本魔术号全部挂单并收口（不平仓）。返回是否已确认干净并收口。"""
    if not await self._cancel_remaining_pending_orders():
        return False          # 读不到挂单簿 / 撤不干净：下一拍重试
    await self._finish(
        "done", self._stop_reason,
        residual=0,           # 持仓保留是有意的，不能当残留上报
    )
    return True
```

**`residual=0` 是关键**：`_finish` 里 `residual > 0` 会被改成 `stop_failed`
（`group_persist.finish_subtask` 还有一层 `if status in _TERMINAL and residual > 0` 的兜底）。
情况 B 有持仓是**有意的**，必须传 0。

---

## 5. 新指令 `strategy_cancel_pending`

现有 `cancel_pending` 是裸撤单，不知道任务上下文：撤完之后任务不知道要收口，
会一直心跳等成交（`_opened=False` 且无持仓 → `reconcile_node_positions` 也收不了口，
因为要求 `opened_at is_not None`，见 `group_persist.py:1734`）。

所以新增一个指令，形状对齐 `strategy_stop`：

```
{"cmd": "strategy_cancel_pending",
 "task_id": 123,
 "dispatch_id": 456,
 "magic": 900000456,
 "group_id": "...",
 "symbol": "EURUSD",
 "reason": "leader_cancelled"}
```

节点侧（`node_client._do_strategy_cancel_pending`）：

```python
runner = self.runners.get(task_id)
if runner and not runner.done:
    runner.request_cancel_pending(reason=msg.get("reason") or "leader_cancelled")
    return
await self._cancel_orphan_pending(ws, task_id, msg)   # 节点重启过、无本地监控
```

`_cancel_orphan_pending`（仿 `_close_orphan_strategy`，`node_client.py:1254`）：

```python
res = await self._exec(self.mt5.cancel_pending_all, "strategy", magics={magic})
# 回报 strategy_finished，status=done，不带 residual_positions
```

`scope="strategy"` + `magics=` 是 `mt5_client.cancel_pending_all` 的签名，
**不能用 "all"**（会撤掉账户里所有挂单）。

---

## 6. 唯一需要留意的边界：启动期挂单失败的重试

`_risk_open_pending_distribute`（`strategy_runner.py:4167`）在**启动那一拍**会循环
`pending_batches`，若某档 `_risk_add_batch` 失败就 `break`，剩下的留到下一拍。

理论上"下一拍"不会再发生（只有启动调用一次），但如果 `_risk_advance` 在
`positions` 非空时被调用（情况 B：底仓已成交），它会再调一次
`pending_batches(plan, filled=total_orders)` —— 而 `total_orders` 里 **缺了失败的那一档**，
所以它会把那一档再挂一次。

这在本方案里是**无害的**：

- 服务端下达 `strategy_cancel_pending` 时，节点已经进入 `_cancel_only`，
  `_run` 循环开头就先走 `_cancel_pending_and_finish`，**不会**再执行到 `_risk_advance`
- 即便同一拍里 `_risk_advance` 刚挂出一张（竞态），`_cancel_pending_and_finish`
  的复查会把它撤掉（`_cancel_remaining_pending_orders` 撤的是 magic 下**全部**挂单）

所以不需要额外的作废档位机制。

---

## 7. 服务端流程

```
主节点快照到达（books_ok）
  ↓
leader_close_sync.on_snapshot
  ├─ （新增）按 magic 检出 cancelled > 0（§2.2）
  ├─ （新增）连续确认 3 轮
  ├─ （新增）主节点子任务在跑，且是 tpl_2 限价
  ├─ （新增）对每个跟单子任务：
  │    ├─ 下发 strategy_cancel_pending
  │    ├─ 在线 → 节点回报 strategy_finished 收口
  │    └─ 离线 → force_finish_subtasks(status="done") + 入 pending_stop
  ├─ （新增）主节点自己的子任务 force_finish_subtasks(status="done")
  └─ （原样）持仓消失 → build_close_command（同步平仓，不受影响）
```

**主节点自己也要收口**（你确认过）：全撤场景下主节点 `_opened=False`、`opened_at=None`，
`reconcile_node_positions` 要求 `opened_at.is_not(None)`（`group_persist.py:1734`），
节点自己也不会收口 → 服务端必须补这一刀。

---

## 8. 收口：`done` + `leader_cancelled`

`force_finish_subtasks`（`group_persist.py:1797`）当前把状态写死 `failed`。
直接改会动到「目标节点离线、无人平仓」那条路径 → 加可选参数：

```python
async def force_finish_subtasks(
    dispatch_ids: list[int], *, reason: str, status: str = "failed",
) -> Optional[dict]:
    ...
    row.status = status if status in ("done", "failed") else "failed"
```

- 同步平仓兜底 `leader_close_orphan`：不动，仍 `failed`
- 本方案主节点 / 离线跟单节点：传 `status="done"`

`done` 与 `failed` 在 `_refresh_task_status` / `_refresh_signal_status` 都是终态，
都走 `_released(row)`。区别只在展示与统计。

---

## 9. 持久化与重连

- 节点侧：`_cancel_only` 是内存态。节点重启后服务端会通过 `strategy_resume` 重建 runner，
  但**不会**重建 `_cancel_only`。所以：
  - 服务端下发时若子任务还没收口 → 走在线路径 → 有 runner → 正常撤单收口
  - 若服务端已 `force_finish` 后节点才重启 → 子任务已是终态 → 不会再 resume → 无影响
  - 若节点在收到指令后立刻断线、重连时子任务仍是非终态 → `_resume_strategy_tasks`
    会把 runner 拉起来，但 `_cancel_only` 丢失 → 挂单可能残留

  **兜底**：服务端在 `on_snapshot` 的连续确认里，若检出后 N 轮内跟单节点的挂单仍在，
  重复下发一次 `strategy_cancel_pending`（幂等：已撤完则 `_cancel_remaining_pending_orders`
  返回 True 直接收口）。

- 离线补发走 `redis_store.push_pending_stop`（TTL 24h），
  `ws_gateway._flush_pending_stops` 能处理任意命令形状，不用改。

**顺序要求**：先 `force_finish(status="done")`、再入队。
否则 `_resume_strategy_tasks`（`ws_gateway.py:576`）会先把监控拉起来
（`active_subtasks_for_node` 只过滤终态）。

---

## 10. 竞态与边界

| # | 时刻 | 主节点 | 跟单节点 | 处理 |
|---|---|---|---|---|
| 1 | 挂单在盘 | 有 | 有 | — |
| 2 | 用户全撤 | 无 | 有 | 检出 cancelled>0，确认 1/3 |
| 3 | 确认满 | 无 | 有 | 下发指令；主节点子任务 done |
| 4 | 节点执行 | — | 撤单 + done | 子任务 done |
| 5 | 部分成交后撤剩余 | 有持仓 | 有持仓+挂单 | 同上，持仓保留 |
| 6 | 撤单过程中挂单成交 | — | 已成交 | 撤不掉的跳过；`_finish(residual=0)`，持仓保留 |
| 7 | 跟单节点离线 | 撤单 | 离线 | force_finish(done) + 入队；重连补发，不 resume |
| 8 | 跟单节点已自行结束 | 撤单 | 已 done | 查不到活跃子任务，跳过 |
| 9 | 跟单节点启动时有档挂单失败、指令到达时正待重试 | 撤单 | 还没挂出 | 收口前先撤，重试不会再发生（`_cancel_only` 优先） |
| 10 | tpl_1 / tpl_3 | 撤单 | — | 不触发（模版不符） |
| 11 | 服务重启 | 基线在 Redis（TTL 6h） | — | 过期则首轮只建基线不对比（已有分支） |

---

## 11. 改动清单

### 11.1 服务端

| 文件 | 改动 |
|---|---|
| `leader_close_sync.py` | `_snapshot_rows` 加 `kind`；`_lost_tickets` 只看 `kind=="position"`；新增 `_pending_cancelled()` 检出 + 连续确认；新增 `_sync_cancel_pending()`；主节点子任务收口 |
| `group_dispatcher.py` | `build_strategy_cancel_pending_command(...)` |
| `group_persist.py` | `force_finish_subtasks(..., status="failed")` 加可选 `status` |
| `redis_store.py` | 连续确认计数 key（或复用 `note_absent_magics`） |
| `config.py` | `LEADER_PENDING_SYNC_CONFIRM = 3` |

### 11.2 节点（约 25 行 + 测试）

| 文件 | 改动 |
|---|---|
| `strategy_runner.py` | `_cancel_only` 字段；`request_cancel_pending()`；`_cancel_pending_and_finish()`；`_run` 循环加分支 |
| `node_client.py` | 指令分发加 `strategy_cancel_pending`；`_do_strategy_cancel_pending()`；`_cancel_orphan_pending()` |
| `test_node_client.py` | 有 runner / 无 runner 两条路径 |
| `test_strategy_runner.py` | 撤单收口 done、持仓保留、撤不干净时重试 |

**不涉及**：`risk_sizing.pending_batches` / `_risk_advance` / `_risk_open_pending_distribute`
—— 挂单一次性的语义不变，无需过滤「作废档位」。

### 11.3 前端

| 文件 | 改动 |
|---|---|
| `GroupsView.vue` | 「同步平仓」开关说明补「含限价挂单同步撤销」（`GroupsView.vue:913-917`） |

不新增开关：开同步平仓 = 主节点放弃这笔交易，跟单节点也放弃。

---

## 12. 测试清单

### 服务端

| # | 场景 | 期望 |
|---|---|---|
| 1 | 主节点全撤（无持仓） | 跟单收到指令；主节点子任务 done |
| 2 | 主节点撤剩余（有 1 笔持仓） | 同上，`leader_positions=1` |
| 3 | 纯成交 | 不下发（cancelled=0） |
| 4 | 首单未挂出 | 连续 3 轮才考虑 |
| 5 | 主节点任务已 done | 不下发 |
| 6 | 主节点 tpl_1 / tpl_3 | 不下发 |
| 7 | 跟单节点离线 | force_finish(done) + 入队；重连补发；不 resume |
| 8 | 多跟单节点 | 各收到指令 |
| 9 | 同步平仓原有测试 | 全通过（基线加 `kind` 后） |
| 10 | `books_ok=False` | 不调用 |

### 节点

| # | 场景 | 期望 |
|---|---|---|
| 1 | 收到指令，有持仓 | 挂单撤净、持仓不动、上报 done、`residual_positions` 不带正数 |
| 2 | 收到指令，无持仓 | 同上 |
| 3 | 撤单过程中一张成交 | 该张跳过，其余撤净，持仓保留 |
| 4 | 撤单失败（券商拒绝） | 不收口，`_cancel_only` 保持，下一拍重试 |
| 5 | 无本地 runner | 按 magic 撤单，回报 done |
| 6 | 指令重复到达（幂等） | 已撤完 → 直接收口，无副作用 |

---

## 13. 上线顺序

1. **节点发版**（新增指令）
2. **服务端**（老节点不认识新指令 → 不发回包 → 超时 → 降级为 `cancel_pending` + `force_finish(done)`；
   老节点上的挂单撤掉后不会补挂，效果与新版一致，只是收口由服务端强制）
3. **前端文案**

---

## 14. 需要你确认

1. **§11.3 前端只改文案、不加开关** —— 可以吗？
