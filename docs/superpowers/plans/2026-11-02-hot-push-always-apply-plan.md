# AI智能加仓策略 · 配置无条件热推 + 节点自决执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让 AI智能加仓策略（模版1）的任何配置改动都能保存、能热推到节点；节点按自身真实持仓状态决定「加不加 / 怎么加」，并通过逐字段回执和加仓进度上报把结论告诉用户。

**Architecture:** 分六层推进 —— (1) 后端删掉保存路径上的全部拦截与节点预检；(2) 节点 reconcile 改为「字段值一律采纳新值」，`frozen` 状态退役，回执区分「采纳」与「执行结论」；(3) 热推后下一拍立刻做一次加仓对齐检查；(4) 档位未覆盖时节点兜底沿用最后一档、服务端自动补档；(5) 节点上报加仓进度 → 落库 → `runtime_hints` → 前端展示；(6) 前端校验降级为提示。

**Tech Stack:** Python 3.13、pytest、Vue 3 + TypeScript + Element Plus。

**Spec:** `docs/superpowers/specs/2026-11-02-hot-push-always-apply-design.md`

> **状态：全部完成（2026-11-02）**。下方 91 个任务已全部实施并通过回归：
> `node_client` 769 项、`backend` 1032 项全部通过，前端 `npm run build`（含 `vue-tsc --noEmit`）0 错误。

---

## Global Constraints

- **核心不变量**：字段值永远采纳用户填的值；「加不加 / 怎么加」由节点自身状态决定。
- **保存路径零拦截**：PATCH 一律 200；节点预检（`strategy_precheck`）整条链路删除。
- **版本门保留**：`config_version` 倒退 → 丢弃；运行中切网格 / 以损定量 → 拒绝。这两条不属于「配置生效」范畴。
- **价格事件口径不变**：加仓后重算出的止盈落在现价错误侧 → 仍市价平仓。只在「配置改动」时才保留旧值（2b 既有语义）。
- **手动分散仓能力原样保留**：不新增、不删除、不改语义。其 `locked` 是「节点状态锁」，不是保存拦截。
- **已开仓位不由配置变更平仓**：删规则、缩容、改档位都不平仓。
- **档位兜底语义**：宁可加仓，也不要静默卡死。
- Python 代码风格：中文注释，解释「为什么」而非「是什么」；模块顶部 docstring 说明职责与边界。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `node_client/strategy_reconcile.py`（改） | 上限字段一律采纳新值；规则删除采纳；回执加 `exec`；`frozen` 退役 |
| `node_client/strategy_rules.py`（改） | `pick_batch_level` 兜底沿用最后一档；`_prepare_rule` 不再因未命中档位而 `return None` |
| `node_client/strategy_runner.py`（改） | `_add_align_pending` 加仓对齐检查；`_snapshot` 增 `add_progress`；`send_update_ack` 增 `exec`/`progress`；删 `precheck_strategy` |
| `node_client/strategy_precheck.py`（删） | 预检纯逻辑，整模块移除 |
| `node_client/node_client.py`（改） | 删 `_do_strategy_precheck` 与命令分发分支 |
| `node_client/test_strategy_reconcile.py`（改） | 改期望值：`frozen` → `no_effect`；规则删除 → `applied` |
| `node_client/test_strategy_precheck.py`（删） | 随模块一起移除 |
| `node_client/test_strategy_runner.py`（改） | 新增：采纳新值、对齐检查、进度上报、回执 exec |
| `backend/app/strategies.py`（改） | 删 `limit_below_added` 400 分支、删 `_precheck_running_tasks` |
| `backend/app/strategy_precheck.py`（删） | 预检 RPC 协调器，整模块移除 |
| `backend/app/strategy_validate.py`（删） | 仅含 `check_limit_below_added`，随调用方一起移除 |
| `backend/app/ws_gateway.py`（改） | 删 `strategy_precheck_result` 分支与 import |
| `backend/app/group_rules.py`（改） | `strategy_rules_snapshot` 档位自动补齐 |
| `backend/app/group_persist.py`（改） | 落 `add_progress`；`strategy_runtime_hints` 增 `add_progress` |
| `backend/app/orm.py`（改） | `GroupTaskDispatch.add_progress_json` |
| `backend/app/db.py`（改） | 迁移 `_migrate_group_dispatch_add_progress` |
| `backend/tests/test_strategy_tuning.py`（改） | 400 用例改为 200；新增进度落库用例 |
| `frontend/src/components/StrategyFormModal.vue`（改） | 下限校验降级；`redistribute` 放开；档位自动追加；进度横幅 |
| `frontend/src/views/GroupSignalsView.vue`（改） | 回执渲染支持 `exec`；`no_effect` 文案调整 |
| `frontend/src/api/types.ts`（改） | `StrategyRuntimeHints.add_progress`；回执字段 `exec` |

---

## Task 1: 后端删除保存路径上的全部拦截与预检链路

**Files:**
- Modify: `backend/app/strategies.py`
- Delete: `backend/app/strategy_precheck.py`
- Delete: `backend/app/strategy_validate.py`
- Modify: `backend/app/ws_gateway.py`
- Modify: `backend/tests/test_strategy_tuning.py`

**Interfaces:**
- Consumes: 无（起点任务）
- Produces: `PATCH /api/strategies/{id}` 一律 200；无 `strategy_precheck` 命令；无 `limit_below_added` 错误码

### Step 1: 读现有代码，确认要删的块

- [x] Read `backend/app/strategies.py`，定位 `update_strategy` 里的 `limit_below_added` 分支与 `_precheck_running_tasks` 调用
- [x] Read `backend/app/ws_gateway.py`，定位 `strategy_precheck_result` 分支与 `strategy_precheck` import
- [x] Grep `strategy_validate` / `strategy_precheck` 的全部引用点

### Step 2: 删掉 strategies.py 的两处拦截

- [x] 删除 `update_strategy` 里的 `strategy_validate.check_limit_below_added` 整块（连同 `group_persist.strategy_runtime_hints` 调用）
- [x] 删除 `_precheck_running_tasks` 调用
- [x] 删除 `_precheck_running_tasks` 函数定义
- [x] 清理 `strategies.py` 顶部不再使用的 import（`strategy_precheck`、`strategy_validate`）

### Step 3: 删除预检模块

- [x] Delete `backend/app/strategy_precheck.py`
- [x] Delete `backend/app/strategy_validate.py`
- [x] Modify `backend/app/ws_gateway.py`：删 `strategy_precheck_result` 分支 + import

### Step 4: 改测试

- [x] `backend/tests/test_strategy_tuning.py :: test_limit_below_added_is_rejected` → 改名 `test_limit_below_added_is_accepted`，断言 200 且 `config_version` 递增
- [x] `test_limit_check_is_per_rule` → 改为断言两条规则都 200
- [x] 其余上限用例保持（本来就是 200）

### Step 5: 验证

- [x] `cd backend && python -m pytest tests/test_strategy_tuning.py -q`
- [x] Grep 确认 `strategy_precheck` / `strategy_validate` 在 `backend/app` 下无残留引用

---

## Task 2: 节点 reconcile —— 字段值一律采纳新值

**Files:**
- Modify: `node_client/strategy_reconcile.py`
- Modify: `node_client/test_strategy_reconcile.py`

**Interfaces:**
- Consumes: `reconcile_snapshot(old, new, added_by_rule, closed_rules, manual_locked, manual_pending)`
- Produces: 字段结论新增 `exec` 键；`status` 取值集合变为 `{applied, no_effect, locked}`（`frozen` 退役）

### Step 1: 改上限类字段分支

- [x] 在 `reconcile_rules` 的 `LIMIT_FIELDS` 循环里，把「低于已开笔数 → 保留旧值」改成「一律采纳新值」
- [x] 三种结论：`new_v <= 0` → `applied` + `will_add`；`new_v > added` → `applied` + `will_add`；`new_v <= added` → `no_effect` + `no_more_adds`
- [x] `rule_type in closed_rules` → `no_effect` + `no_more_adds`
- [x] 确认 `applied_rule[field] = new_v` 已写入（不再保留 `old_v`）

### Step 2: 改规则删除分支

- [x] 「旧有新无」分支：`status` 由 `locked` 改 `applied`，**不再 `effective.append(dict(old_rule))`**
- [x] 消息：「该规则已移除，不再参与加仓判定；已开仓位继续由止盈止损管理，不平仓」

### Step 3: 改手动仓已成交文案

- [x] `manual_locked` 分支保持 `status="locked"`，文案改为「配置已保存并热推；因该规则手动分散仓已成交，节点不再执行这部分改动，已开仓位不受影响」
- [x] `exec` 设为 `locked_by_manual`

### Step 4: 退役 `frozen`

- [x] `summarize` 的 `partial` 触发条件去掉 `frozen`
- [x] 模块 docstring 的状态词表更新

### Step 5: 改测试

- [x] `test_strategy_reconcile.py :: test_limit_below_added_is_frozen_and_keeps_old_value` → 改为断言 `status == "no_effect"` 且生效快照里 `total_lot_limit == 2`（新值）
- [x] `test_limit_change_after_manual_closed_is_no_effect` → 断言 `exec == "no_more_adds"`
- [x] 新增：规则删除 → `status == "applied"` 且生效快照里不再有该规则

### Step 6: 验证

- [x] `cd node_client && python -m pytest test_strategy_reconcile.py -q`

---

## Task 3: 节点判定侧 —— 档位兜底

**Files:**
- Modify: `node_client/strategy_rules.py`
- Modify: `node_client/test_strategy_rules.py`

**Interfaces:**
- Consumes: `pick_batch_level(rule, next_position_no)`
- Produces: 未命中档位时回退到最后一个档位（而非 `None`）

### Step 1: 改 `pick_batch_level`

- [x] 收集合法档位列表；逐个匹配区间
- [x] 未命中时若列表非空 → 返回 `(levels[-1], len(levels) - 1)`
- [x] 列表为空才返回 `(None, None)`
- [x] docstring 说明「兜底是为了避免整条规则静默卡死」

### Step 2: 改 `_prepare_rule` 的 `else` 分支

- [x] `batch_enabled=True` 且未命中档位时**不再 `return None`**
- [x] 改为走兜底路径：间距/倍数/手数取最后一档的参数
- [x] 上限判定仍生效（`rule_adds >= limit` → `return None`）

### Step 3: 改测试

- [x] `test_strategy_rules.py` 新增：档位只覆盖 1-3、已加 3、上限 5 → `_prepare_rule` 不返回 None，且 gap/倍数取档位#3 的参数
- [x] 回归：命中区间时行为不变

### Step 4: 验证

- [x] `cd node_client && python -m pytest test_strategy_rules.py -q`

---

## Task 4: 节点热推 —— 加仓对齐检查 + 回执加 exec/progress

**Files:**
- Modify: `node_client/strategy_runner.py`
- Delete: `node_client/strategy_precheck.py`
- Delete: `node_client/test_strategy_precheck.py`
- Modify: `node_client/node_client.py`
- Modify: `node_client/test_strategy_runner.py`

**Interfaces:**
- Produces: `StrategyRunner._add_align_pending: bool`；`_snapshot()["add_progress"]`；回执 `fields[].exec` 与 `data["progress"]`

### Step 1: 删预检

- [x] Delete `node_client/strategy_precheck.py`
- [x] Delete `node_client/test_strategy_precheck.py`
- [x] `node_client.py`：删 `_do_strategy_precheck` 方法、删命令分发里的 `strategy_precheck` 分支
- [x] `strategy_runner.py`：删 `precheck_strategy` 与 `_precheck_sltp_crossed`，清理 `strategy_precheck` import

### Step 2: 加 `_add_align_pending`

- [x] `__init__` 里初始化 `self._add_align_pending = False`
- [x] `apply_strategy_detailed` 成功应用后置 `True`
- [x] `_on_event` 在加仓路径分支里检查该标记：为真时**立刻**执行一次顺势判定 + 逆势限价同步（不等价格触发），完成后置 `False`

### Step 3: 撤单文案

- [x] `_sync_counter_limit` 里「逆势加仓没有下一档」的撤单原因，区分「上限已低于已开笔数」与「档位用尽」两种情形，给出可读文案

### Step 4: `_snapshot` 增 `add_progress`

- [x] 新增私有方法 `_add_progress()`：按规则类型产出 `{added, limit, remaining, current_level_index, current_level_range, next_level_index, next_level_range, next_gap_points, next_volume, pending_limit_ticket}`
- [x] `_snapshot()` 的返回体加 `"add_progress": self._add_progress()`

### Step 5: 回执加 `exec` / `progress`

- [x] `send_update_ack` 的 payload 加 `"progress": self._add_progress()`
- [x] `fields` 里的 `exec` 字段由 reconcile 产出，直接透传（确认 `strategy_reconcile` 已产出）

### Step 6: 改测试

- [x] `test_strategy_runner.py` 新增：上限 5→2、已加 3 → 生效快照上限 == 2（不是 5）
- [x] 新增：热推后下一拍立刻对齐（不等价格触发）
- [x] 新增：`_snapshot()` 含 `add_progress`，`added`/`limit`/`remaining` 正确

### Step 7: 验证

- [x] `cd node_client && python -m pytest -q`

---

## Task 5: 服务端 —— 档位自动补齐

**Files:**
- Modify: `backend/app/group_rules.py`
- Modify: `backend/tests/test_group_rules.py`（或新建）

**Interfaces:**
- Produces: `strategy_rules_snapshot` 返回的 `rules[].batch_levels` 连续覆盖 `[1, total_lot_limit]`

### Step 1: 写补齐函数

- [x] 新增私有函数 `_ensure_batch_coverage(rule)`：
  - `batch_enabled` 为假 → 原样返回
  - 收集档位，按 `pos_from` 排序，检查是否连续覆盖 `[1, total_lot_limit]`
  - 有洞或末尾不足 → 用**最后一个档位**的参数补齐（`pos_from`/`pos_to` 接续）
  - 上限为 0（不限）→ 只保证现有档位连续，不追加

### Step 2: 在快照生成时应用

- [x] `strategy_rules_snapshot` 里对每条规则调用 `_ensure_batch_coverage`

### Step 3: 测试

- [x] 新增：档位只覆盖 1-3、上限 5 → 快照里档位覆盖到 5，末档参数沿用档位#3

### Step 4: 验证

- [x] `cd backend && python -m pytest tests/test_group_rules.py -q`

---

## Task 6: 加仓进度落库与对外暴露

**Files:**
- Modify: `backend/app/orm.py`
- Modify: `backend/app/db.py`
- Modify: `backend/app/group_persist.py`
- Modify: `backend/tests/test_strategy_tuning.py`

**Interfaces:**
- Produces: `group_task_dispatch.add_progress_json` 列；`strategy_runtime_hints()["add_progress"]`

### Step 1: 加列与迁移

- [x] `orm.py :: GroupTaskDispatch` 加 `add_progress_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)`
- [x] `db.py` 加 `_migrate_group_dispatch_add_progress(sync_conn)`，仿照 `_migrate_group_dispatch_add_counts`
- [x] `init_db()` 里注册调用

### Step 2: 落库

- [x] `group_persist.py :: record_strategy_progress` 里落 `add_progress`（与 `add_counts` 并列，心跳也刷）

### Step 3: 对外暴露

- [x] `strategy_runtime_hints` 增 `add_progress`：多节点合并 —— `added` 取最大、`limit` 取最新配置、`current_level` 取 `added` 最大的那个节点
- [x] 空态也要返回 `"add_progress": {}`

### Step 4: 测试

- [x] `test_strategy_tuning.py` 新增：`runtime_hints` 含 `add_progress` 键

### Step 5: 验证

- [x] `cd backend && python -m pytest tests/test_strategy_tuning.py -q`

---

## Task 7: 前端 —— 校验降级、档位自动追加、进度展示、回执渲染

**Files:**
- Modify: `frontend/src/components/StrategyFormModal.vue`
- Modify: `frontend/src/views/GroupSignalsView.vue`
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: `runtime_hints.add_progress`
- Produces: 保存按钮不再被上限拦截；进度横幅；回执 `exec` 渲染

### Step 1: `api/types.ts`

- [x] `StrategyRuntimeHints` 增 `add_progress?: Record<string, StrategyAddProgress>`
- [x] 新增 `StrategyAddProgress` 接口：`added / limit / remaining / current_level_index / current_level_range / next_level_index / next_level_range / next_gap_points / next_volume`
- [x] 回执字段类型增 `exec?: string`

### Step 2: `StrategyFormModal.vue` —— 下限校验降级

- [x] 找到 `limitFloor` 用于**拦截**的那处（`prepareStrategy` 里的校验），改为**不返回错误**，只产出黄色提示
- [x] 提示文案：「已开 3 笔，新上限 2 笔低于已开笔数；保存后本任务不再新增批次，现有仓位继续由止盈止损管理」
- [x] 保留 `limitFloor` 作为**展示**用（输入框旁的「还能再加 N 笔」提示）

### Step 3: `StrategyFormModal.vue` —— 放开 redistribute

- [x] `batchEditMode()` 不再因 `hasRunningTask` 强制 `append`
- [x] 选 `redistribute` 时给提示：「重切档位会改变后续加仓命中的参数；已开仓位不受影响」

### Step 4: `StrategyFormModal.vue` —— 档位自动追加

- [x] 改上限后，若档位覆盖不到新的上限 → 自动追加档位（沿用最后一档参数）
- [x] 追加后给黄色提示，不阻止保存

### Step 5: `StrategyFormModal.vue` —— 进度横幅

- [x] `runtimeBanner` 增「当前进度」段，从 `runtimeHints.add_progress` 取数
- [x] 文案示例：「逆势加仓：已加 3 / 5 笔（当前第 2 批），下一批 200 点 × 1.5 手，还能再加 2 笔」

### Step 6: `GroupSignalsView.vue` —— 回执渲染

- [x] `strategyUpdateFieldLabel`：`no_effect` 文案从「无效果」改为「不再新增批次」
- [x] `detail.kind === 'strategy_update'` 分支：字段行追加 `exec` 对应的说明标签

### Step 7: 验证

- [x] `cd frontend && npm run typecheck`

---

## Task 8: 文档同步与全量回归

**Files:**
- Modify: `docs/AI智能加仓策略动态调参方案.md`（§8 案例表重写 + §0 决策表修订）
- Modify: `使用与部署文档.md` / `技术实现方案.md`（删除「保存前拦截」的描述）

**Interfaces:**
- Consumes: 无
- Produces: 文档与实现一致

### Step 1: 修订上游文档

- [x] §0 决策表第 1 条改为「上限类字段是普通可改字段，**任何值都允许保存并热推**；节点按自身已开笔数决定是否继续加仓」
- [x] §4「保存前预校验」整章标注为**已废弃**（保留历史，说明何时移除）
- [x] §8 案例表：场景 4 / 5 / 10 / 11 的「保存」列改为 ✅，并补 `no_effect` 说明

### Step 2: 同步部署文档

- [x] 搜索 `precheck` / `拦截` / `冻结` 关键字，更新过时描述

### Step 3: 全量回归

- [x] `cd node_client && python -m pytest -q`
- [x] `cd backend && python -m pytest -q`
- [x] `cd frontend && npm run typecheck`
- [x] Grep 确认无残留：`strategy_precheck` / `limit_below_added` / `frozen`（reconcile 语境）

---

## Review Focus

以下几处最容易咬人，实施时重点核对：

1. **`applied_rule[field] = new_v` 是否真的写进去了** —— `reconcile_rules` 里 `applied_rule = dict(new_rule)` 已经带了新值，但旧代码在 `frozen` 分支显式覆盖成 `old_v`；删掉覆盖后要确认没有别的地方再写回旧值。
2. **规则删除后 `_closed_rule_types` / `_manual_*` 的行为** —— 规则从生效快照里消失后，按规则类型索引的收口标记会跟着失效，这是预期；但不能因此让**另一条规则**（如顺势）也失去加仓资格。
3. **档位兜底对 `plan_counter_limit` 的影响** —— 逆势挂价依赖 `pick_batch_level`；兜底后它能继续算出挂单价，但**上限判定在 `_prepare_rule` 里**，兜底不能绕过上限（缩容后必须仍然不挂单）。
4. **`_add_align_pending` 的清理时机** —— 对齐成功后必须置 `False`，否则每拍都会重复对齐（顺势会重复加仓）。对齐失败时保持 `True` 会导致每拍重试，需确认这是期望行为。
5. **`no_effect` 的文案不能误导** —— 前端原本把 `no_effect` 渲染成「➖ 无效果」，用户会以为「没保存」。必须改成「不再新增批次」之类的说法。
6. **`add_progress` 的 `remaining` 语义** —— `limit == 0`（不限）时不能写 `0`，会误导成「不能再加」；用 `-1` 表示不限。
