# 主节点平仓 → 跟单节点同步平仓（Leader Close Position Sync）

日期：2026-11-19
范围：strategy 链路（带单市场 tpl_2 / tpl_3 + AI 市场 tpl_1）
状态：方案定稿，待实施
取代：`2026-11-18-follower-cascade-stop-design.md`（强制终止连带，已废弃）

---

## 1. 背景：为什么要推翻上一版

上一版（`_cascade_stop_followers`）的做法是：主节点终止任务时，服务器给跟单节点
下发 `strategy_stop`（按 magic 全平 + **强制结束监控**）。

这个做法与实际机制**冲突**，并且做不出核心场景：

### 1.1 非网格策略「空仓 = 任务结束」是既有语义，不需要服务器补刀

节点客户端与服务端本来就是这样工作的：

| 层 | 行为 |
|---|---|
| `node_client/market_hub._classify` | `hold_when_empty=False` 的订阅，连续 `empty_confirm` 轮读到空仓 → 发 `GONE` |
| `node_client/strategy_runner` | 收到 `GONE` → 先撤残留挂单 → `_finish("done", positions_cleared)`，任务结束 |
| `backend/group_persist.reconcile_node_positions` | 服务端兜底：`hold_when_empty=False` 且 `opened_at` 非空的子任务，快照里 magic 消失即收口 |

**所以「平掉仓位」和「终止任务」在非网格策略里本来就是同一件事。**
服务器再补一刀 `strategy_stop` 是双重终止：多余，且有副作用（撤销监控的时机比
节点自然收口更粗暴）。

### 1.2 `strategy_stop` 做不出「平一笔」

用户的核心场景是两条：

- **场景 1**：主节点平掉全部仓位 → 跟单节点也全部平掉 → 两边任务都结束。
- **场景 2**：主节点只平掉其中一笔 → 跟单节点也平掉**对应那一笔** → 任务**继续运行**。

`strategy_stop` 的语义是「按 magic 平**全部** + 停监控」，没有「平一笔」这个概念。
上一版在场景 2 会把跟单节点的整个任务停掉，直接错。

### 1.3 结论

> **服务器只同步「平仓动作」，不下发任何「终止任务」命令。**
> 任务是否结束，由节点侧按「空仓」自然收口 —— 这正是用户的原话。

---

## 2. 目标与非目标

### 2.1 目标

- **G1**：主节点平掉一笔持仓 → 跟单节点平掉**配对的那一笔**；任务继续运行。
- **G2**：主节点平掉全部持仓 → 跟单节点全部平掉 → 两边任务自然收口。
- **G3**：能感知**所有**平仓来源：手动 MT5 平仓、券商止盈止损平仓、系统平仓 API。
- **G4**：网格策略（`hold_when_empty=True`）**只平仓、不停任务** —— 空仓是网格常态。
- **G5**：方向性不变：跟单员自己平仓 / 移出跟单，**不得**影响主节点。
- **G6**：失败可兜底：跟单节点离线导致同步平仓未执行时，不能留下「主节点已结束、
      跟单节点还在跑」的僵尸任务。

### 2.2 非目标

- 不改变节点客户端（`node_client/`）；全部改动在服务端。
- 不改变「暂停接单」语义（暂停只挡新开仓）。
- 不做跨账户的手数换算（跟单节点按自己的仓位平，不按主节点手数等比换算）。

---

## 3. 核心设计

### 3.1 三块拼图

```
① 感知        持仓快照对比：上一轮有、本轮没有 → 判定「被平掉了」
② 映射        同一 task 下，主/从持仓按开仓时间排序，第 i 笔 ↔ 第 i 笔
③ 执行        下发 close(target="ticket")，不是 strategy_stop
```

### 3.2 ① 感知：快照对比

平仓有三个来源，只有第三个我们能从请求里拿到：

1. 用户在 MT5 终端里手动平仓 —— 没有事件
2. 券商触发止盈 / 止损 —— 没有事件
3. 通过系统平仓（`POST /api/nodes/{id}/close` 等）—— 有请求，但**不是唯一来源**

所以只能用**持仓快照对比**来统一感知。服务端已经有这个数据源：
每个节点上报的账户快照落在 `K_ACCOUNT`（`store.get_account`）。

**挂在哪个钩子上**：`ws_gateway._save_account_active` 在快照入库后调用
`_reconcile_strategy_tasks`（子任务收口）。新巡检挂在**同一个位置**，并且
**在收口之前执行** —— 保证同步平仓先于「跟单节点自己空仓收口」发生。

```
_save_account_active(node_id, snapshot)
  ├─ store.save_account(...)
  └─ if books_ok:
       ├─ leader_close_sync.on_snapshot(node_id, positions)   ← 新增，先执行
       └─ _reconcile_strategy_tasks(node_id, positions, orders) ← 既有
```

**为什么要先执行**：若跟单节点先自己收口了，再发 close 就没人认领了。
先同步平仓，跟单节点在随后几拍里读到空仓 → 走它自己的 `GONE` 路径 → 收口。

### 3.3 基线缓存

```
key:   follow:leader_snapshot:{node_id}
value: [{ticket, magic, symbol, type, volume, time}, ...]
ttl:   LEADER_SNAPSHOT_TTL（默认 6 小时）
```

规则：

- **首次见到该节点**（缓存没有）：只写基线，**不对比**。避免服务重启后基线为空，
  把「当前所有持仓」误判成「新开的一批」反向出错。
- **基线过期**（节点离线超 TTL）：同上，重建基线不对比。
- **`books_ok=False`**：快照不可信，**跳过**（不更新基线、不对比）。与
  `_reconcile_strategy_tasks` 同一防线。
- 只记录**属于 strategy 子任务魔术号**的持仓：`group_rules.subtask_id_from_magic(magic)`
  反查得到 dispatch_id 才算。普通手工单不进入基线，避免噪音。

### 3.4 ② 映射：按开仓时间顺序配对

**为什么不能按 ticket 直传**：主节点与跟单节点在不同账户、不同券商，ticket 完全独立。

**共用键**：`task_id` + 各自的 `magic`。

```
主节点侧：dispatch(node=主, task_id=T) → magic M
跟单侧  ：dispatch(node=从, task_id=T) → magic M'

主节点 M 下持仓按 (time, ticket) 排序： [p0, p1, p2]
跟单节点 M' 下持仓按 (time, ticket) 排序：[q0, q1, q2]
第 i 笔的映射：p_i ↔ q_i
```

主节点 `p2` 消失 → 平跟单节点 `q2`。

**已知前提与风险**：配对假设「两个节点在同一个 task 下开出的仓位数量一致且顺序一致」。
若跟单节点某笔开仓失败（资金不足 / 券商拒单），后续索引会错位。因此：

- 巡检时对比主从笔数，不一致时**打 WARNING 日志**（含 task_id / 两侧笔数），
  并在该 task 上**降级为「不平仓」**（宁可不平，也不错平）。
- 若线上长期出现不一致，再升级为「配对表」（开仓回报时记录主↔从 ticket 映射）。
  这是本方案预留的升级路径，不在本期实施。

**平仓顺序的边界**：主节点 `p2` 被平掉，但跟单节点 `q2` 已经不存在（例如它自己
那笔先被券商止损掉了）→ 该笔映射失效，跳过。若主从笔数因此不一致，走上面的
WARNING + 降级路径。

### 3.5 ③ 执行：下发 close

```python
{"cmd": "close", "signal_id": ..., "close_target": "ticket",
 "close_symbol": None, "close_ticket": 跟单那一笔的 ticket}
```

即 `build_close_command(signal_id, "ticket", None, ticket)`。

**不是 `strategy_stop`。** 节点侧收到 `close` 只平这一笔，不碰监控状态。

---

## 4. 场景验证

### 场景 1：全部平掉

```
主节点 M 下 [p0, p1, p2]，用户平掉全部
  → 快照对比检出 p0/p1/p2 全部消失
  → 对每笔发 close 到跟单节点（q0, q1, q2）
  → 跟单节点 magic M' 下无持仓、无挂单
  → market_hub 连续 empty_confirm 轮读到空仓 → GONE
  → strategy_runner._finish("done", positions_cleared)
  → 任务结束（非网格）/ 网格继续等待（hold_when_empty）
```

### 场景 2：只平一笔

```
主节点 M 下 [p0, p1, p2]，用户平掉 p2
  → 快照对比检出 p2 消失
  → 配对：p2 是第 3 笔 → 平跟单节点 q2
  → 跟单节点还剩 q0、q1 → 不空仓 → 任务继续运行 ✅
```

### 网格策略

```
主节点平掉网格仓位 → 同步 close 到跟单节点
  → 跟单节点网格 hold_when_empty=True，空仓不收口
  → 网格继续按价格波动挂格、重新开仓 ✅（符合网格长期运行的语义）
```

---

## 5. 方向性（G5）

`on_snapshot` 处理的节点**必须是带单方的自有成员**（`group.members`）：

```
node_id（上报快照的那个节点）
  ∈ group.members（owner 自有成员）→ 才做同步
  ∈ group.followers（跟单员节点）  → 跳过（跟单员平仓不影响主节点）
```

这是「连带是带单方向跟单方的单向动作」的延续，与上一版完全一致。

**注意**：主节点也可能同时在别的分组里扮演跟单节点。此时它在**那个分组**里的持仓变化
同样会被检出，但方向性检查会发现它不是那组的 owner 成员 → 跳过。不会串台。

---

## 6. 兜底（G6）

同步平仓失败（跟单节点离线 / 券商拒绝）会留下「主节点已结束、跟单节点还挂着」的僵尸任务。

**逐笔失败的补偿由 close 重试覆盖，不需要新代码**：下发 close 失败（节点离线）
→ 立即入 `pending_stop` 队列（复用既有机制），节点重连时补发。

**任务级兜底**在同一个钩子上做一次收紧的判定：

```
本轮检出「主节点某 task 的 magic 平过仓」      ← 必要条件（限价未成交不算）
且 主节点侧该 magic 已无持仓、无挂单
且 跟单节点侧「它自己的」magic 也已无持仓、无挂单
→ 强制收口跟单子任务（force_finish_subtasks，reason=leader_close_orphan）
```

三点注意：

1. **主从两侧 magic 不同**（同一 task 下各自分配），判定必须按各自的 magic，
   不能拿跟单子任务的 magic 去和主节点的 magic 集合比。
2. **必须带挂单**：限价开仓在成交前主节点侧只有挂单没有持仓，只看持仓会把
   「还在等成交」误判成「已结束」而错误收口。
3. **必须绑定「本轮平过仓」**：否则一个从未开过仓的任务也会被判成孤儿。
   这条是「宁可留着让节点自己收口，也不要越权停任务」的取舍 ——
   正常路径下跟单节点几拍内就会自己读到空仓收口，兜底只处理收口迟迟不来的极端情况。


---

## 7. 数据与配置

### 7.1 Redis 新增

| Key | 内容 | TTL |
|---|---|---|
| `follow:leader_snapshot:{node_id}` | 主节点上一轮 strategy 持仓基线（JSON） | 6h |

### 7.2 沿用既有

- `node_group.stop_cascade_enabled`（默认 True）—— 语义改为「**主节点平仓时是否同步
  跟单节点平仓**」。字段名不变（避免迁移），只改语义与文案。
- `group_task_dispatch.stop_source` —— 同步平仓**不写** `stop_source`
  （它不是「终止」，只是平仓）。`leader_cascade` 值保留在枚举里兼容存量数据。
- `K_NODE_PENDING_STOP` —— close 失败时复用。

### 7.3 待移除（上一版残留）

| 位置 | 处理 |
|---|---|
| `group_dispatcher._cascade_stop_followers` | 删除 |
| `group_dispatcher._cascade_stop_groups` | 删除 |
| `close_subtask` 里的 cascade 调用 | 删除（保留 `stop_source` 标记） |
| `stop_all_on_node` / `stop_magics_on_node` 里的 cascade 调用 | 删除 |
| `group_persist.record_leader_stop_event` | 保留（新方案也用它，消息改为「主节点平仓已同步」） |

---

## 8. 改动清单

### 8.1 后端

| 文件 | 改动 |
|---|---|
| `backend/app/leader_close_sync.py`（新建） | 巡检主体：`on_snapshot(node_id, positions)`、基线读写、配对、下发 close、兜底 |
| `backend/app/redis_store.py` | `K_LEADER_SNAPSHOT` + `get/set_leader_snapshot` |
| `backend/app/ws_gateway.py` | `_save_account_active` 里在 `_reconcile_strategy_tasks` **之前**调用 `leader_close_sync.on_snapshot` |
| `backend/app/group_dispatcher.py` | 删除 `_cascade_stop_followers` / `_cascade_stop_groups` 与三处调用 |
| `backend/app/group_persist.py` | 新增 `active_follower_dispatches_for_task(task_id, follower_nodes)`（如需要）；`record_leader_stop_event` 的消息改为「主节点平仓已同步」 |
| `backend/app/config.py` | `LEADER_SNAPSHOT_TTL`（默认 6h） |

### 8.2 前端

| 文件 | 改动 |
|---|---|
| `frontend/src/views/GroupsView.vue` | 开关文案：「主节点平仓同步」；说明改为「主节点平仓后，跟单节点的对应仓位会一起平掉」 |
| `frontend/src/views/GroupSignalsView.vue` | `stop_source` 的 `leader_cascade` 标签改为兼容存量（新数据不再写入）；连带提示改为「同步平仓」 |
| `frontend/src/views/HomeNodeView.vue` | 提示文案：「跟单节点的对应仓位会跟着平掉」 |
| `frontend/src/api/types.ts` | 注释更新；`cascade` 字段保留但标注为「上版遗留」 |

### 8.3 测试

| # | 用例 | 断言 |
|---|---|---|
| 1 | 主节点全平（3 笔） | 跟单节点收到 3 条 `close(target=ticket)`，且**没有** `strategy_stop` |
| 2 | 主节点只平第 3 笔 | 跟单节点只收到 1 条 close，命中配对的那一笔；任务不收口 |
| 3 | 网格策略 | 同步平仓后，任务**不**收口（`hold_when_empty`） |
| 4 | `stop_cascade_enabled=False` | 不检出、不下发 |
| 5 | 跟单员自己平仓 | 主节点不受影响（方向性） |
| 6 | 首次见到节点（无基线） | 不误判为大批平仓 |
| 7 | `books_ok=False` 快照 | 不对比、不更新基线 |
| 8 | 主从持仓笔数不一致 | 打 WARNING 日志，该 task 不平仓（降级） |
| 9 | 主节点已结束、跟单节点残留 | 兜底强制收口 + pending_stop 入队 |
| 10 | 跟单节点离线时同步 close | 入 `pending_stop`，重连补发 |

---

## 9. 风险与边界

| 风险 | 处理 |
|---|---|
| 配对错位（跟单某笔开仓失败） | 笔数不一致 → WARNING + 该 task 降级不平仓 |
| 服务重启后基线为空 | 首见只写基线，不对比 |
| 节点断联时快照失真 | `books_ok=False` → 跳过（既有防线） |
| 主节点在多个分组 | 按 group 逐一处理，方向性检查天然隔离 |
| 同一笔被重复检出 | 同步成功后立即更新基线，下一轮不再检出 |
| 手动平仓与系统平仓同时发生 | 幂等：同一 ticket 的 close 重复下发无副作用（节点侧按 ticket 平，已不存在即忽略） |
| 跟单节点手动平了一笔 | 只影响它自己，主节点不受影响；但主从笔数会不一致 → 下轮降级告警。可接受（宁可不平） |
| 手数与主节点不同（风控缩放） | 按跟单节点自己那一笔的手数平，不做换算 |

---

## 10. 落地顺序

1. `redis_store`：基线缓存读写。
2. `leader_close_sync`：巡检主体（感知 → 映射 → 执行 → 兜底）。
3. `ws_gateway`：挂钩，**放在收口之前**。
4. `group_dispatcher`：删除旧连带实现。
5. `group_persist`：配套查询与消息文案。
6. 前端：开关与提示文案。
7. 测试：新增用例 + 全量回归。

---

## 11. 与上一版的关键差异对照

| 维度 | 上一版（强停） | 本版（同步平仓） |
|---|---|---|
| 下发命令 | `strategy_stop` | `close(target=ticket)` |
| 任务终止 | 服务器强制 | 节点侧空仓自然收口 |
| 场景 2（平一笔） | 做不到 | 原生支持 |
| 网格 | 被强停 | 只平仓，继续运行 |
| 触发时机 | 用户在系统里点「终止」 | 任何平仓来源（快照对比） |
| 服务端职责 | 平仓 + 终止 | 只同步平仓 |
| 新增数据 | 2 列 + 事件流 | 1 个 Redis 缓存键 |
