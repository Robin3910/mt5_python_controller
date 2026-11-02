# AI智能加仓策略 · 配置无条件热推 + 节点自决执行（方案 v2）

> 日期：2026-11-02
> 适用模版：`tpl_1`「AI智能加仓策略」（规则 type=1 逆势加仓 / type=2 顺势加仓）
> 关联代码：`node_client/strategy_runner.py`、`node_client/strategy_rules.py`、`node_client/strategy_reconcile.py`、`node_client/strategy_precheck.py`、`node_client/node_client.py`、`backend/app/strategies.py`、`backend/app/strategy_precheck.py`、`backend/app/strategy_validate.py`、`backend/app/group_rules.py`、`backend/app/group_persist.py`、`backend/app/ws_gateway.py`、`frontend/src/components/StrategyFormModal.vue`、`frontend/src/views/GroupSignalsView.vue`、`frontend/src/api/types.ts`
> 上游文档：`docs/AI智能加仓策略动态调参方案.md`（本方案推翻其中「保存前拦截」与「缩容冻结」两条核心口径）

---

## 0. 一句话

> **字段值永远采纳用户填的值；「加不加 / 怎么加」由节点自身状态决定，并在回执里说清楚。**

配置能不能保存、能不能下发，**不再有任何拦截**。节点收到后按自己的真实持仓状态决定这批配置还能不能产生动作，并把结论回报给用户。

---

## 1. 目标与非目标

### 1.1 目标

1. **任何配置改动都能保存、都能热推到节点**。保存路径上不再有任何 400 拦截，也不再有节点预检往返。
2. **上限类字段实时生效**：
   - 配置 5 次、节点已加 3 次 → 节点**继续往上加**（第 4、5 批），直到与配置对齐。
   - 配置 2 次、节点已加 3 次 → 节点**不再加仓**，保留现有仓位等止盈止损。
3. **用户能看见节点真实进度**：「现在加到第几批第几仓」「还能再加几笔」「下一批的间距/手数是多少」。
4. **回执说人话**：区分「配置有没有被采纳」与「节点会不会照此执行」。

### 1.2 非目标

| 不做 | 原因 |
|---|---|
| 改造手动分散仓的能力 | 需求方明确「不再触发手动分散仓」，其代码原样保留，本次不新增也不删除 |
| 改变「加仓后重算出的止盈越价 → 市价平仓」这条价格事件口径 | 那是既有交易语义，不是保存期拦截 |
| 让 `lot_pl_tiers.tiers[1..]` 参与共同止盈计算 | 节点只取批次 1，属既有已知限制 |
| 让网格 / 以损定量策略也支持热推 | 那两条路径的任务快照在运行期是冻结的，属另一语义域 |
| 摘除持仓上已有的 SL / TP（关开关即摘单） | 需扩展 `modify_position_sl` 的「显式清零」通道，属另一方案 |

---

## 2. 现状诊断

### 2.1 节点侧「缩容保留旧值」与目标相反

`node_client/strategy_reconcile.py :: reconcile_rules` 的上限类字段分支：

```python
elif new_v and added and new_v < added:
    out.append({... "status": "frozen" ...})
    # 低于已开笔数：保留旧上限，避免把任务推进非法状态
    applied_rule[field] = old_v
```

用户把上限从 5 改成 2、节点已加 3 笔时，这里把**旧值 5 写回生效快照** → 节点下一拍照旧挂下一档逆势限价、照旧加仓。**目标要求的「不再加仓」完全没发生**，而且用户完全不知情。

### 2.2 判定侧其实已经支持目标语义

`node_client/strategy_rules.py :: _prepare_rule`：

```python
limit = _as_int(rule.get("total_lot_limit"), 0)
if limit and rule_adds >= limit:
    return None
```

只要**生效快照里的上限是用户填的新值**，节点天然就是「已加 3 ≥ 上限 2 → 不再加」。

> **因此核心语义只需把 `frozen → 采纳新值` 改掉，不需要新增任何判定机制。**

### 2.3 保存层的两道拦截

`backend/app/strategies.py :: update_strategy`：

1. `strategy_validate.check_limit_below_added` → 400 `limit_below_added`
2. `_precheck_running_tasks` → 节点侧 `strategy_precheck` 报阻断 → 400（`sltp_crossed` / `manual_locked` / `batch_inconsistent`）

两者都与「热推即生效」直接冲突。

### 2.4 前端还有第三道拦截

`frontend/src/components/StrategyFormModal.vue`：
- `limitFloor` 下限校验 → 标红 + 禁用保存
- `redistribute` 在有在跑任务时禁用（`batchEditMode()` 强制 `append`）

### 2.5 「第几批第几仓」没有对外暴露

节点内部有 `add_counts`（按规则类型分的已加笔数），已落库并进了 `runtime_hints.max_added_by_rule`，但只到「已开几笔」这一层；**当前第几批（档位序号）、下一批的区间/点数/手数**没有上报。

### 2.6 「档位未覆盖下一次加仓」会让节点静默卡死

`pick_batch_level` 按「本规则第几次加仓」命中档位区间：

```python
for idx, level in enumerate(rule.get("batch_levels") or []):
    lo = _as_int(level.get("pos_from"), 0)
    hi = _as_int(level.get("pos_to"), 0)
    if lo <= next_position_no <= hi:
        return level, idx
return None, None
```

命中失败后 `_prepare_rule` 的 `else` 分支：

```python
else:
    # 分批未启用（或已超出所有档位区间）：走基础参数 + 次数上限，基础参数只支持点数
    if rule.get("batch_enabled"):
        return None
```

`batch_enabled=True` 但没命中档位 → **直接 return None → 这条规则永远不再加仓**。

**这直接违反核心要求**：配置 5 次、节点加了 3 次，但档位只写到了第 3 次 → 节点在第 3 次之后静默卡死，既不加仓、也不报错。

发生场景：扩容时忘了加档位 / 用户手工改档位区间留洞 / 历史脏数据 / 重切档位后不匹配。

现有的 `check_batch_anchor` 能抓「中间有洞」和「下一笔没档位」，但抓不到「档位总和 < 上限」；且本方案要去掉 precheck，所以**必须靠节点侧兜底**。

### 2.7 规则删除当前被锁死

`reconcile_rules` 末尾：

```python
for rule_type, old_rule in old_by_type.items():
    if rule_type in new_by_type:
        continue
    out.append({"field": f"rules[type={rule_type}]", "status": "locked", ...})
    effective.append(dict(old_rule))
```

删规则 → 保留旧规则 + 标记 locked。用户填了「删掉这条」，节点照旧按它加仓。

---

## 3. 总体架构

```
用户改配置
  │
  ├─ 前端：校验降级为黄色提示（不阻止保存）；档位自动追加
  │
  ├─ 后端 PATCH：一律落库 + config_version++ + 热推（零 400、零预检）
  │    └─ 快照生成时保证档位连续覆盖 [1, total_lot_limit]
  │
  └─ 节点 apply_strategy_detailed
       ├─ 版本门（config_version 倒退 → 丢弃）
       ├─ 模式门（运行中切网格/以损定量 → 拒绝）
       ├─ reconcile：字段值一律采纳新值
       │    └─ 逐字段回报「采纳结果」+「节点执行结论 exec」
       ├─ 置 _add_align_pending → 下一拍立刻做一次加仓对齐检查
       └─ 回执 ack { result, fields[{status, exec, message}], progress }
```

**不变的部分**：节点判定逻辑（`evaluate` / `plan_counter_limit` / `_prepare_rule` 的上限比较）、止盈止损（`signal_pl`）的运行期语义、手动分散仓的全部能力。

---

## 4. 字段可变性矩阵（改后）

| 类别 | 字段 | 运行中行为 |
|---|---|---|
| **HOT** | `point`、`lot_times`、`extra_lot`、`action`、`batch_action`、`status`、`batch_levels`（间距/倍数/手/calc_type/timeframe/range_*） | 下一拍判定即用新值 |
| **LIMIT** | `total_lot_limit`、`max_allow_num` | **一律采纳新值**。<br>• 新值 > 已加笔数 → 继续加仓<br>• 新值 ≤ 已加笔数 → **不再新增批次**（回执 `no_effect` + `exec=no_more_adds`）<br>• 新值 = 0 → 不限 |
| **REMOVED** | 整条加仓规则被删除 | **采纳删除**：该规则不再参与加仓判定；在途逆势限价单撤掉；**已开仓位照旧由止盈止损管理，不平仓** |
| **LOCKED**（节点状态锁，非保存拦截） | 规则 `type`、模版、`manual_scatter` 全部字段（手动仓已成交时） | 保存成功、配置下发；**节点不执行这部分**；已成交仓位不动 |
| **NODE-STATE** | — | 「加仓已收口」（手动仓已挂出）时改加仓相关字段 → 采纳但 `exec=no_more_adds`，回执说明 |

> **状态词表（改后）**：`applied` / `no_effect` / `locked` / `rejected`。
> **`frozen` 整体退役**（唯一产生者被删）。`partial` 的触发条件保留（`no_effect` / `locked` 仍算部分生效）。

---

## 5. 节点侧设计

### 5.1 `strategy_reconcile.py` —— 字段值一律采纳

#### 5.1.1 上限类字段

```python
# 改后
new_v = _limit_value(new_rule, field)
added = _as_int(added_by_rule.get(rule_type), 0)
# 一律采纳新值（不再保留旧值）
applied_rule[field] = new_v
if rule_type in closed_rules:
    status = "no_effect"; exec_ = "no_more_adds"
    msg = "该规则手动分散仓已开仓，本任务加仓已收口，调整上限不会新增批次"
elif new_v <= 0:
    status = "applied"; exec_ = "will_add"
    msg = "上限已改为不限（0）；本任务继续按规则加仓"
elif new_v > added:
    status = "applied"; exec_ = "will_add"
    msg = f"上限已从 {old_v} 调整为 {new_v}；本任务已加 {added} 笔，还能再开 {new_v - added} 笔"
else:
    status = "no_effect"; exec_ = "no_more_adds"
    msg = (f"上限 {new_v} 笔 ≤ 已开 {added} 笔，本任务不再新增批次；"
           f"现有 {added} 笔继续由止盈止损管理")
```

#### 5.1.2 规则删除

```python
# 改后：不再保留旧规则
out.append({
    "field": f"rules[type={rule_type}]",
    "status": "applied",
    "exec": "will_add" if ... else "no_more_adds",
    "message": "该规则已移除，不再参与加仓判定；已开仓位继续由止盈止损管理，不平仓",
})
# effective 不再 append 旧规则
```

⚠️ 连带影响：`_closed_rule_types` / `_manual_*` 的按规则判定随规则消失而消失，这是预期的（规则都没了，收口标记也无意义）。

#### 5.1.3 手动仓已成交（`manual_locked`）

**保持 `locked`**，但文案改为「配置已保存并热推；因该规则手动分散仓已成交，节点不再执行这部分改动，已开仓位不受影响」。

#### 5.1.4 `summarize`

`frozen` 从触发条件里移除（无产生者）；`no_effect` / `locked` 仍触发 `partial`。

### 5.2 `strategy_rules.py` —— 档位兜底

`pick_batch_level` 命中失败时**不再返回 None**，改为回退到最后一个档位：

```python
def pick_batch_level(rule, next_position_no):
    if not rule.get("batch_enabled"):
        return None, None
    levels = [l for l in (rule.get("batch_levels") or []) if isinstance(l, dict)]
    for idx, level in enumerate(levels):
        lo = _as_int(level.get("pos_from"), 0)
        hi = _as_int(level.get("pos_to"), 0)
        if lo <= next_position_no <= hi:
            return level, idx
    # 兜底：档位没覆盖到下一次加仓 → 沿用最后一个档位的参数，
    # 而不是让整条规则静默卡死
    if levels:
        return levels[-1], len(levels) - 1
    return None, None
```

`_prepare_rule` 的 `else` 分支同步调整：`batch_enabled=True` 且未命中档位时**不再 `return None`**，改为走兜底路径（间距/倍数/手数用最后一档）。

> 兜底的语义选择：**宁可加仓，也不要静默卡死**。卡死 = 用户以为还有额度、实际永远不加；沿用最后一档 = 加出来的仓参数可能不是最想要的，但行为可预期，且事件里有说明。

`plan_counter_limit`（逆势挂价）同样依赖 `pick_batch_level`，兜底后能继续算出挂单价 → 上限未达时逆势能正常续挂。

### 5.3 `strategy_runner.py` —— 加仓对齐检查

热推后配置可能从「不够加」变成「还能加」（扩容），或从「还能加」变成「不能加」（缩容）。原来的行为是「等下一次价格触发才体现」。

新增运行态 `_add_align_pending: bool`：

- `apply_strategy_detailed` 成功后置 `True`；
- `_on_event` 每拍开始时检查：为真则**立刻做一次加仓对齐**，不等价格触发：
  - 顺势：按新配置走一次市价判定（偏离够就立刻加）；
  - 逆势：`_sync_counter_limit` 立刻重算挂单价 → 撤旧单 + 挂新单（已有路径）；
- 对齐完成后置 `False`。

理由：扩容后如果不等价格触发，用户会以为「改了没生效」；而实际上逆势那档限价单是**已经挂在盘上的**，不重算就会按旧参数成交。

### 5.4 `strategy_runner.py` —— 在途逆势限价单的撤销/重挂

缩容到 ≤ 已开笔数时，`plan_counter_limit` 返回 None → 现有路径 `_sync_counter_limit` 已会撤单，撤单原因是「逆势加仓没有下一档」。**改后文案**要写清是上限导致：

> 「上限 2 笔 ≤ 已开 3 笔，本任务不再新增批次」

扩容时：`_align_counter_order` 已按新档位重算挂单价并改单/撤挂。

### 5.5 回执结构

```jsonc
{
  "type": "strategy_update_ack",
  "data": {
    "task_id": 1234, "magic": 8888, "config_version": 42,
    "result": "partial",
    "fields": [
      {
        "field": "rules[type=1].total_lot_limit",
        "status": "no_effect",
        "old": 5, "new": 2,
        "exec": "no_more_adds",
        "message": "上限 2 笔 ≤ 已开 3 笔，本任务不再新增批次；现有 3 笔继续由止盈止损管理"
      },
      {
        "field": "rules[type=1].batch_levels",
        "status": "applied",
        "exec": "will_add",
        "message": "档位已更新；下次加仓（第 4 笔）将按档位#2 的新参数计算"
      }
    ],
    "progress": { "1": {...}, "2": {...} }
  }
}
```

`exec` 取值：

| 值 | 含义 |
|---|---|
| `will_add` | 配置已采纳，节点会按此继续加仓 |
| `no_more_adds` | 配置已采纳，但已加笔数 ≥ 上限（或该规则已收口），节点不再新增批次 |
| `waiting_tp_sl` | 配置已采纳，本规则不再加仓，现有仓位等待止盈止损 |
| `locked_by_manual` | 该规则手动分散仓已成交，节点不执行这部分 |

`status` 语义（**采纳与否**）与 `exec` 语义（**节点会不会执行**）严格分离。

### 5.6 「第几批第几仓」运行态上报

节点每个规则输出一段：

```jsonc
"add_progress": {
  "1": {
    "added": 3,
    "limit": 5,
    "remaining": 2,
    "current_level_index": 1,
    "current_level_range": [3, 4],
    "next_level_index": 2,
    "next_level_range": [5, 5],
    "next_gap_points": 200,
    "next_volume": 0.15,
    "pending_limit_ticket": 0
  }
}
```

- `added`：已加仓笔数（不含开仓、不含手动仓）
- `limit`：当前生效的上限（0 = 不限）
- `remaining`：还能再加几笔（`limit == 0` 时为 `-1`，表示不限）
- `current_level_index`：当前处在第几批（档位序号，从 0 起）；`added == 0` 时为 `-1`
- `next_*`：下一批的信息
- `pending_limit_ticket`：逆势在途限价单票号（0 = 未挂）

链路：节点 `_snapshot()` → WS `strategy_progress` → `group_persist` 落 `add_progress_json` → `strategy_runtime_hints` 增 `add_progress` → 前端展示。

多节点合并口径：`added` 取最大；`limit` 取最新配置；`current_level` 取 `added` 最大的那个节点。

---

## 6. 后端设计

| 位置 | 改动 |
|---|---|
| `strategies.py :: update_strategy` | 删除 `limit_below_added` 的 400 分支；删除 `_precheck_running_tasks` 调用。**PATCH 一律 200** |
| `strategies.py :: _precheck_running_tasks` | **整个函数删除** |
| `strategy_precheck.py` | **整个模块删除** |
| `strategy_validate.py` | `check_limit_below_added` **删除**（该模块仅剩这一个函数 → 整个模块删除） |
| `ws_gateway.py` | 删除 `strategy_precheck_result` 分支与 `strategy_precheck` import |
| `group_rules.py :: strategy_rules_snapshot` | **档位自动补齐**：保证 `batch_levels` 连续覆盖 `[1, total_lot_limit]`，不足则在尾部补档（参数沿用最后一档） |
| `group_persist.py` | 新增列 `group_task_dispatch.add_progress_json`；`record_strategy_progress` 落 `add_progress`；`strategy_runtime_hints` 增 `add_progress` |
| `orm.py` | `GroupTaskDispatch.add_progress_json`（JSON，nullable） |
| `db.py` | 新增迁移 `_migrate_group_dispatch_add_progress` |
| `group_dispatcher.py` / `strategy_service.py` / `group_rules.py` | 不变（`config_version` 版本门保留） |

> `config_version` 版本门保留（防乱序到达的迟到包）；`strategy_update` 下发链路不变。

---

## 7. 前端设计

| 位置 | 现状 | 改为 |
|---|---|---|
| `StrategyFormModal.vue :: limitFloor` 校验 | 标红 + 禁用保存 | **删除拦截**，改黄色提示：「已开 3 笔，新上限 2 笔低于已开笔数；保存后本任务不再新增批次，现有仓位继续由止盈止损管理」 |
| `StrategyFormModal.vue :: batchEditMode` | 有在跑任务时强制 `append` | **放开**：允许 `redistribute`，提示「重切档位会改变后续加仓命中的参数；已开仓位不受影响」 |
| `StrategyFormModal.vue :: onDisplayedTotalLotChange` 等 | 改上限后档位可能不覆盖 | **自动追加档位**（沿用最后一档参数）+ 黄色提示，不阻止保存 |
| `StrategyFormModal.vue :: runtimeBanner` | 只显示在跑任务数 | 增「当前进度」段：「逆势加仓：已加 **3 / 5** 笔（当前第 2 批），下一批 200 点 × 1.5 手，还能再加 2 笔」 |
| `StrategyFormModal.vue :: manualScatterLocked` | 只读遮罩 | **保持不变**（手动单能力不动） |
| `GroupSignalsView.vue` | 回执渲染只认 `status` | 支持 `exec` 字段；`no_effect` 文案从「无效果」改为「不再新增批次」（避免误导成「没保存」） |
| `api/types.ts` | `StrategyRuntimeHints` | 增 `add_progress`；`StrategyUpdateField` 增 `exec` |

---

## 8. 边界情况最终口径

| # | 场景 | 保存 | 节点行为 | 回执 |
|---|---|---|---|---|
| 1 | 上限 5→2，已加 3 | ✅ 200 | 不再加仓；逆势在途单撤掉；保留仓位等止盈止损 | `no_effect` + `no_more_adds` |
| 2 | 上限 5→8，已加 3 | ✅ 200 | 下一拍立刻按新配置挂第 4 档 / 顺势判定 | `applied` + `will_add` |
| 3 | 上限 5→8，档位只覆盖 1-3 | ✅ 200 | 服务端补档；未补上则节点沿用档位#3 参数加第 4 笔 + 告警 | `applied` + `will_add` |
| 4 | 档位参数被改（已用过的档） | ✅ 200 | 后续加仓按新参数；已开仓位不动 | `applied` |
| 5 | 删除一条加仓规则 | ✅ 200 | 该规则不再参与判定；在途限价单撤掉；**持仓数量不变** | `applied` |
| 6 | 手动分散仓已成交，改其配置 | ✅ 200 | 节点不执行这部分；已成交仓位不动 | `locked` + `locked_by_manual` |
| 7 | 改 `lot_pl_tiers` / `float_pl_ratio`，新价越过现价 | ✅ 200 | 该侧**保留旧值** + 发 `signal_pl` 告警（`status="kept"`）；不强平 | `applied`（回执注明越价保留旧值） |
| 8 | 加仓后重算出的止盈落在现价错误侧 | — | **市价平仓**（价格事件，既有语义不变） | — |
| 9 | 节点离线时改配置 | ✅ 200 | 重连 resume 用新快照；若已加 ≥ 新上限，同样不再加 | 无回执（离线） |
| 10 | 版本倒退 / 运行中切网格·以损定量 | — | 拒绝（`rejected`）—— 这两条不属于「配置生效」范畴 | `rejected` |

---

## 9. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 取消全部拦截后，**配置错误直接变成交易行为** | 上限填错、档位写错会立刻影响加仓 | 回执逐字段说明「采纳了 + 会不会执行」；事件流里每次加仓都带档位与参数明细；前端展示实时进度 |
| 逆势在途限价单与新上限不一致 | 挂着的那张单要撤 | `_sync_counter_limit` 现有路径已撤；补「撤单 + 重挂」两个方向的测试 |
| 档位兜底沿用最后一档 | 加出来的仓参数可能不是用户最想要的 | 事件里明说「沿用档位#K 参数」；服务端补档优先，让兜底很少发生 |
| SL/TP 越价不再在保存时拦 | 用户可能以为新止盈立刻生效 | 节点侧 `kept` 告警事件 + 回执注明 |
| 多节点进度分叉 | 各节点自己加自己 | 前端展示各节点进度；上限按配置对齐，各自收敛 |
| `no_effect` 增多 | 上限缩到已开笔数以下就会报 | 这是**如实反映**；文案必须说清「配置已保存，本任务不再新增批次」 |

---

## 10. 关键代码位置索引

| 主题 | 位置 |
|---|---|
| 上限字段采纳新值 | `node_client/strategy_reconcile.py :: reconcile_rules`（`LIMIT_FIELDS` 分支） |
| 规则删除采纳 | `node_client/strategy_reconcile.py :: reconcile_rules`（末尾「旧有新无」分支） |
| 状态词表 | `node_client/strategy_reconcile.py :: summarize` |
| 档位兜底 | `node_client/strategy_rules.py :: pick_batch_level` / `_prepare_rule` |
| 加仓对齐检查 | `node_client/strategy_runner.py :: apply_strategy_detailed` / `_on_event`（`_add_align_pending`） |
| 在途限价单撤销 | `node_client/strategy_runner.py :: _sync_counter_limit` / `_cancel_counter_orders` |
| 加仓进度上报 | `node_client/strategy_runner.py :: _snapshot` |
| 回执结构 | `node_client/strategy_runner.py :: send_update_ack` |
| 预检移除 | `backend/app/strategy_precheck.py`（删）、`backend/app/strategies.py :: _precheck_running_tasks`（删）、`node_client/strategy_precheck.py`（删）、`node_client/node_client.py :: _do_strategy_precheck`（删）、`backend/app/ws_gateway.py`（删分支） |
| 档位自动补齐 | `backend/app/group_rules.py :: strategy_rules_snapshot` |
| 加仓进度落库 | `backend/app/group_persist.py :: record_strategy_progress` / `strategy_runtime_hints`；`backend/app/orm.py :: GroupTaskDispatch.add_progress_json`；`backend/app/db.py :: _migrate_group_dispatch_add_progress` |
| 前端校验降级 | `frontend/src/components/StrategyFormModal.vue :: limitFloor` / `batchEditMode` / `runtimeBanner` |
