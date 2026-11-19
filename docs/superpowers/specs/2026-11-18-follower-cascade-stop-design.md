# 带单任务连带终止设计（Follower Cascade Stop）

> **⚠️ 已废弃（2026-11-19）**：本方案的「服务器下发 `strategy_stop` 强制连带终止」已被
> [`2026-11-19-leader-close-position-sync-design.md`](./2026-11-19-leader-close-position-sync-design.md) 取代。
>
> 废弃原因：非网格策略在节点侧本来就是「空仓即收口」，服务器再补一刀 `strategy_stop`
> 是多余的双重终止；而且 `strategy_stop` 是「按 magic 全平 + 停监控」，做不出
> 「主节点只平一笔、跟单节点也平一笔，任务继续跑」这个核心场景。
>
> 现行做法：**只同步平仓动作**（按 ticket 下发 `close`），任务是否结束交给节点侧按空仓自然收口。
> 本文保留仅作历史参考；文中描述的 `_cascade_stop_followers` 等代码已全部移除。

日期：2026-11-18
范围：strategy 链路（分组 / 带单市场 tpl_2、tpl_3）
状态：**已废弃**（被 2026-11-19-leader-close-position-sync-design 取代）

---

## 1. 背景与问题

带单员（owner）把自己的**主节点**放进分组对外发信号，跟单员把自己的节点加入同一个分组跟单。
一条信号在分组内只建**一个主任务**（`group_signal_task`），owner 成员与跟单成员各自持有一条
**节点子任务**（`group_task_dispatch`），各用各的魔术号。

问题：**带单员在主节点上终止了任务，跟随节点的任务不会跟着终止** —— 跟单节点的策略仍在跑、
仓位仍在。这与「跟单 = 跟随主节点动作」的直觉不符，也让带单员无法通过主节点一个动作收回整组风险。

### 1.1 现状盘点（实施前）

平仓 / 终止在系统里有 5 条入口，终结落点都是 `strategy_stop`（按魔术号平仓 + 结束节点侧监控）：

| 入口 | 代码位置 | 实施前作用域 | 实施前是否连带跟随节点 |
|---|---|---|---|
| Webhook `CLOSE` 信号 | `group_dispatcher._dispatch_close` → `_close_group_subs` | 按 group/symbol 筛全分组活动子任务 | 是（天然覆盖） |
| 分组一键平仓 | `POST /api/groups/{gid}/close` → `close_group` | 组内所有节点的未收口子任务 | 是（天然覆盖） |
| 主节点单任务终止 | `POST /api/groups/{gid}/dispatches/{did}/close` → `close_subtask` | 仅该 dispatch | **否** |
| 节点主页「跟单频道平仓」 | `POST /api/nodes/{nid}/follow-channels/close` → `stop_magics_on_node` | 仅该节点的魔术号 | **否** |
| 节点账户级全平 | `POST /api/nodes/{nid}/close` → `stop_all_on_node` | 仅该节点 | **否** |

口径分裂：分组级 / 信号级已经是「一个逻辑任务大家一起停」，但带单员在自己节点上做的
单任务终止、频道平仓、账户全平，都是「我这个节点为止」。

### 1.2 关键事实

**跟随节点与主节点的子任务本就同属一个 `task_id`**：主任务按分组建一次，
`_send_sync` 把同一条 `strategy_start` 命令并发下发给所有有效节点（owner 成员 + 跟单成员），
每个节点各建一条 dispatch、各拿一个自己的魔术号。

结论：**「该任务」不需要新数据模型就能表达** —— `task_id` 就是主节点与所有跟随节点子任务的
天然公共键。这让本方案可以做得很轻：加一条「同 `task_id` 的跟随侧 dispatch 一起停」的规则即可，
不新建关联表。

---

## 2. 目标与非目标

### 2.1 目标

- **G1（核心）**：带单员对**主节点**终止任务 X → 该分组内所有**跟随节点**的任务 X 一起终止并清仓。
- **G2**：频道级 / 账户级平仓发生在**主节点**上时，同样连带该分组内跟随节点的任务。
- **G3**：反向不成立 —— 跟单员自己「移出跟单」时停自己的任务，**不得**反向停掉带单员的任务。
- **G4**：离线节点不丢单：复用既有 `forced` + `pending_stop` 队列，重连后补发平仓。
- **G5**：可观测：跟随节点侧能看到「被带单员终止」；带单员侧能看到连带了哪些节点。

### 2.2 非目标

- **AI 交易市场（tpl_1）不在本期范围**。AI 跟单有独立的 `ai_follow_runtime`（单次跟单
  `single_dispatch_id`、热推快照等），语义与普通带单不同，另起一期。
- 不改变「暂停接单」的既有语义（暂停只挡新开仓、不动在跑任务）。
- 不做跟随节点的仓位托管、不做带单员对跟单员仓位的直接操作权。

---

## 3. 方案设计

### 3.1 两级连带

| 级别 | 触发 | 连带范围 | 入口 |
|---|---|---|---|
| **L1 任务级** | 带单员终止主节点上的某个任务 | 同 `group_id` + 同 `task_id` 的跟随侧未收口 dispatch | `close_subtask` |
| **L2 频道 / 账户级** | 带单员在主节点上频道平仓 / 账户全平 | 同 `group_id` 的跟随侧**全部**未收口 dispatch | `stop_magics_on_node` / `stop_all_on_node` |

L1 与 L2 的差别只在「范围是同 `task_id` 还是同 `group_id` 的全部任务」，
实现上是同一个函数的两个参数（`task_id=None` 表示不限任务）。

### 3.2 判定规则（关键）

连带只在**触发者是带单方节点**时发生。这条规则保证 G3：

```
触发节点 ∈ group.members（owner 自有成员）  → 触发连带
触发节点 ∈ group.followers（跟单员节点）    → 不触发连带
```

- 跟单员「移出跟单」走 `follow_api._stop_following_subtasks` → `close_subtask`，
  触发节点是跟随节点 → **不连带**，带单员的任务不受影响。
- 跟单员自己在自己节点上点「平仓」→ 触发节点是跟随节点 → **不连带**。
- 带单员在主节点上点任何终止 → 触发节点是 owner 成员 → **连带**。

### 3.3 执行顺序

**先停跟随侧，再停主节点自己。**

理由：跟随侧策略（网格）在你终止期间可能又开新仓。先把它摁住再收自己，
暴露窗口更小。连带目标与触发目标都走同一个 `_stop_subtask`，
不另写一套平仓逻辑 —— 否则会出现「移出跟单平不掉仓」那类口径分裂
（这是 `follow_api._stop_following_subtasks` 既有原则）。

### 3.4 按魔术号停，不按 ticket

跟随节点的持仓魔术号与主节点不同。每个 dispatch 用**自己**的 magic 构造 `strategy_stop`
（`build_strategy_stop_command` 已按 dispatch 传各自的 magic），节点侧 `close_by_magic`
会把该魔术号下的持仓与挂单一次清干净 —— 连带窗口内新开的仓也一并兜住。

### 3.5 幂等与并发

- 连带目标只取**未收口** dispatch（`active_subtasks` 已按终态过滤），重复触发安全。
- `_stop_subtask` 内部对每个 dispatch 独立处理，某个失败不影响其它。
- 跟随节点离线 → `forced` 分支：`force_finish_subtasks` 强制收口 + `push_pending_stop`
  入队等重连补发（与既有路径完全一致）。

---

## 4. 数据模型

### 4.1 `group_task_dispatch.stop_source`（新增列，可空）

记录「这次终止是谁发起的」，让跟随节点侧能显示「被带单员终止」而不是笼统的「已平仓」。

| 值 | 含义 |
|---|---|
| `self` | 终止发起方就是该 dispatch 所在节点（自己停自己） |
| `leader_cascade` | 被带单员在主节点上的终止连带 |
| `account_close` | 账户级全平 |
| `leave_follow` | 跟单员移出跟单 |
| `close_signal` | Webhook CLOSE 信号 |

存量数据为 NULL，按「未知来源」展示，零行为变化。

### 4.2 `node_group.stop_cascade_enabled`（新增列，默认开）

分组级开关：带单员终止任务时是否连带终止跟随节点。

- 默认 **True**（开）：符合「跟单 = 跟随」的直觉，也是本次诉求的默认值。
- 关闭后带单员可以「只停我自己」，作为逃生口。

存量分组建表后即为此值，零行为变化（本来就是想要的默认行为）。

### 4.3 迁移

`backend/app/db.py` 的 `init_db` 用 `create_all` + 一串 `_migrate_*` 补丁函数。
新增两列按同样模式加两个 `_migrate_*` 函数并挂到 `init_db` 列表里。

---

## 5. 后端改动清单

### 5.1 `backend/app/orm.py`

- `GroupTaskDispatch` 增加 `stop_source: Mapped[str | None] = mapped_column(String(24), nullable=True)`。
- `NodeGroup` 增加 `stop_cascade_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")`。

### 5.2 `backend/app/db.py`

新增 `_migrate_group_task_stop_source(sync_conn)` 与 `_migrate_node_group_stop_cascade(sync_conn)`，
并挂到 `init_db()`。

### 5.3 `backend/app/group_persist.py`

- `active_subtasks_for_task(group_id, task_id)`：取同分组同任务的未收口 dispatch（连带目标）。
- `mark_dispatches_stop_source(dispatch_ids, source)`：落 `stop_source`。
- `_subtask_dict` 增加 `stop_source` 字段。
- `_dispatch_row` 透出 `stop_source`（前端展示用）。

### 5.4 `backend/app/group_dispatcher.py`（核心）

新增两个方法：

```python
async def _cascade_stop_followers(
    self, group: dict | None, *, trigger_node_id: str, task_id: Optional[int],
    signal_id: str,
) -> dict
```

判定与执行：

1. `group` 为空 / `group.stop_cascade_enabled` 为 False → 直接返回空结果。
2. 触发节点必须在 `group.members`（owner 成员）里 → 否则不连带（G3）。
3. 取连带目标：同 `group_id`、同 `task_id`（若给了）、且节点在 `group.followers` 里的未收口 dispatch。
4. 对每个目标调 `_stop_subtask(reason="leader_cascade_stop")`，统计 `closing` / `forced`。
5. 落 `stop_source="leader_cascade"`，写 `group_task_event`（`event_type="leader_stop"`）留痕。
6. 返回 `{stopped, forced, total, nodes}`。

另有 `_cascade_stop_groups(group_ids, *, trigger_node_id, signal_id)`：对多个分组分别执行
上面的连带（`task_id=None`，即分组内全部任务）并汇总 —— 供频道级 / 账户级使用。

接入点（**只在触发者是带单方时**）：

| 方法 | 连带范围 | 说明 |
|---|---|---|
| `close_subtask` | 同 `task_id` | L1；跟单员移出跟单走同一方法，但触发节点是跟随节点 → 自动不连带 |
| `stop_magics_on_node` | 同分组全部任务 | L2 频道级 |
| `stop_all_on_node` | 同分组全部任务 | L2 账户级，**按分组收敛** |

`stop_all_on_node` 的分组收敛说明：该节点可能同时跑多个分组的任务。账户级全平对**本节点**是
账户级的，但对**其它节点**必须按分组收敛 —— 只连带该节点所属带单分组里的跟随 dispatch，
跟单员在其它分组的仓位不动（避免越权清掉跟单员的无关持仓）。

### 5.5 `backend/app/models.py`

- `GroupOut` 增加 `stop_cascade_enabled: bool = True`。
- `GroupCreate` / `GroupUpdate` 增加 `stop_cascade_enabled: Optional[bool] = None`。
- `GroupTaskDispatchRecord` 增加 `stop_source: Optional[str] = None`。
- `FollowChannelCloseResult` 增加 `cascade: dict = Field(default_factory=dict)`。

### 5.6 `backend/app/group_service.py`

- `create_group` / `_update_group` 写入 `stop_cascade_enabled`。
- `group_row_to_dict` 缓存带上该字段（Redis 缓存是分发层的判据来源）。

### 5.7 `backend/app/groups.py`

- `_to_group_out` 透出 `stop_cascade_enabled`。

### 5.8 `backend/app/follow_channel_api.py`

频道平仓（`mode=all`）在 `stop_magics_on_node` 之后，把返回的 `strategies.cascade`
提升到响应体的顶层 `cascade` 字段，并写进审计的 `after`。

> 说明：`stop_magics_on_node` / `stop_all_on_node` 的返回体里 `cascade` 是**嵌套**在
> `strategies` 下的（`stop_magics_on_node` 直接返回 `{stopped, forced, total, cascade}`，
> `follow_channel_api` 把它整体放进 `strategies`）。跟随侧接口额外在顶层再放一份，
> 前端取顶层 `cascade` 即可，不必知道 `strategies` 的内部形状。

---

## 6. 前端改动清单

### 6.1 `frontend/src/api/types.ts`

- `GroupOut` / `GroupCreatePayload` / `GroupUpdatePayload` 增加 `stop_cascade_enabled`。
- `GroupTaskDispatchRecord` 增加 `stop_source`。
- `GroupCloseResult` 增加 `cascade?: { stopped: number; forced: number; total: number }`。
- `FollowChannelCloseResult` 增加 `cascade?`。

### 6.2 `frontend/src/views/GroupsView.vue`

- 分组表单增加「终止连带」开关（默认开），并写进创建 / 更新请求。
- 分组卡片增加一个 `连带开/关` mini-toggle 快速开关（带确认弹窗，说明影响面）。
- 创建 / 更新前的确认摘要里加一行「终止连带：开启 / 关闭」。

### 6.3 `frontend/src/views/GroupSignalsView.vue`

- 子任务的「结束原因」旁边展示 `stop_source` 的人话标签
  （`leader_cascade` → 「被带单员终止」、`leave_follow` → 「移出跟单」、
  `account_close` → 「账户全平」、`close_signal` → 「信号平仓」、`self` → 「自行终止」）。
- 子任务平仓的确认弹窗提示「若该节点是带单方自有节点，跟单节点的同一任务会一起终止」，
  并按后端返回的 `cascade.total` 在结果提示里说明连带终止了几个跟单节点子任务。

### 6.4 `frontend/src/views/TradingView.vue` / `HomeNodeView.vue`

- 网格任务停止 / 频道平仓的确认弹窗同样提示连带影响。
- 结果提示带上连带终止的跟单节点子任务数（`已下发停止 X · 成功 N · 连带终止跟单节点 M`）。

---

## 7. 可观测性与审计

### 7.1 事件流

`group_task_event` 新增事件类型 `leader_stop`，`detail_json` 记录：
源 dispatch_id、源节点、连带节点清单。

### 7.2 审计

`persist.audit(action="leader_cascade_stop", category="follow")`：
target 为分组 ID，after 记录被连带停止的节点清单与结果计数。

**这是跨用户动作**（带单员处置跟单员的仓位），必须留痕。

### 7.3 返回体

所有受影响的接口返回体都要能表达**部分失败**：

```json
{
  "cascade": {
    "stopped": 3,      // 已下发 strategy_stop
    "forced": 1,       // 离线强制收口，待重连补发
    "total": 4,
    "nodes": ["nd_a", "nd_b", "nd_c", "nd_d"]
  }
}
```

前端据此提示「已终止 3 个跟随节点任务，1 个节点离线待重连补发」，
不让带单员误以为全部清干净了。

---

## 8. 边界与风险

| 风险 | 处理 |
|---|---|
| 跟随节点在连带窗口内又开仓 | 按魔术号 `close_by_magic` 全清，天然兜住 |
| 跟随节点离线 | 复用 `forced` + `pending_stop` 队列，重连补发（G4） |
| 幂等 / 并发 | 连带目标只取未收口 dispatch；`_stop_subtask` 逐个独立处理 |
| 跟单员自己移出跟单 | 触发节点是跟随节点 → 不触发连带（G3） |
| 跟单员在自己节点点平仓 | 同上，不连带 |
| 带单员换主节点 | 旧主节点上的在途任务仍属同一 `task_id`，连带照常；新主节点不追溯 |
| 账户全平越权清掉跟单员其它分组的仓 | `stop_all_on_node` 的连带**按分组收敛** |
| 带单员被撤销带单能力 | 跟单关系保留；在跑任务的连带仍生效（能力撤销只影响新开仓） |
| AI 市场（tpl_1） | 不在本期范围 |
| 「暂停接单」与连带混淆 | 暂停只挡新开仓、不动在跑任务；连带是终止在跑任务，两者语义独立 |
| 老版本节点客户端 | `strategy_stop` 命令格式未变，`stop_source` 是服务端字段，节点不感知 |

---

## 9. 测试用例

新增 `backend/tests/test_follower_cascade_stop.py`（13 条用例，全部通过）：

| # | 用例 | 断言 |
|---|---|---|
| 1 | `test_leader_close_cascades_to_followers` | 主节点终止 → 跟单节点同 `task_id` 的 dispatch `closing`，`stop_source=leader_cascade`，收到 `strategy_stop(reason=leader_cascade_stop)` |
| 2 | `test_cascade_only_affects_same_task` | 同分组的另一个主任务不受连带影响 |
| 3 | `test_follower_close_does_not_cascade_back` | 跟单节点终止 → 带单员 dispatch 不变 `closing`（G3） |
| 4 | `test_leave_follow_does_not_cascade_back` | 移出跟单 → 带单员 dispatch 不受影响（G3） |
| 5 | `test_cascade_disabled_suppresses_cascade` | `stop_cascade_enabled=False` → 跟单节点不被连带 |
| 6 | `test_cascade_toggle_persists` | 开关可改回并落库 + 进 Redis 缓存 |
| 7 | `test_offline_follower_forced_and_queued` | 离线跟单节点 → `forced` 强制收口 + 平仓指令入 `pending_stop` 队列 |
| 8 | `test_offline_leader_still_cascades` | 带单方自己离线也不漏掉连带 |
| 9 | `test_account_close_cascades` | 账户级全平（主节点）→ 连带跟单节点 |
| 10 | `test_channel_close_cascades` | 频道平仓（主节点）→ 连带跟单节点 |
| 11 | `test_account_close_on_follower_node_does_not_cascade` | 跟单节点账户全平 → 不反向连带 |
| 12 | `test_group_without_followers_unchanged` | 无跟单节点的普通分组行为不变（回归） |
| 13 | `test_leader_stop_event_recorded` | 连带终止落一条 `leader_stop` 事件流记录 |

全量回归：`1658 passed`（后端），前端 `vue-tsc --noEmit` 与 `npm run build` 均通过。

---

## 10. 落地顺序

1. `orm.py` + `db.py`：新增两列与迁移。
2. `group_persist.py`：新增查询与标记函数。
3. `group_dispatcher.py`：实现 `_cascade_stop_followers` 并接入三条路径。
4. `models.py` + `group_service.py` + `groups.py`：开关与返回体。
5. `follow_channel_api.py`：频道平仓带上 `cascade`。
6. 前端：types + GroupsView 开关 + 提示文案。
7. 测试：新增 `backend/tests/test_follower_cascade_stop.py`，跑全量回归。

---

## 11. 关联文件

- `backend/app/group_dispatcher.py` —— 连带停止的核心实现
- `backend/app/group_persist.py` —— 连带目标查询 / `stop_source` 落库
- `backend/app/follow_api.py` —— 反向不连带的既有依据（`_stop_following_subtasks`）
- `backend/app/follow_rules.py` —— 分组 / 跟单关系的纯规则判定
- `docs/superpowers/plans/2026-10-20-follow-tab-design.md` —— 带单关系的历史设计
