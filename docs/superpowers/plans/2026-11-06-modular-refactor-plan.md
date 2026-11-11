# 长文件模块化拆分 Implementation Plan

> 日期：2026-11-06
> 状态：**待执行**（方案设计阶段，未改任何代码）
> 范围：`node_client` / `backend/app` / `frontend/src` / `nodel_client_dashboard(_web)` 四个子项目
> 目标：把超大单文件按职责域拆成模块，让后续迭代"改一个功能只动一个文件"

---

## 0. 结论摘要（先看这段）

| 结论 | 说明 |
|---|---|
| 拆分原则 | **纯搬移优先**：第一轮不做任何逻辑重写，只把代码搬到正确的模块里，用 re-export 保持外部 import 路径不变 |
| 验收门槛 | 每一轮拆分必须满足：**单测全绿 + 文件行数达标 + import 路径兼容** 三条，缺一不可 |
| 落地形式 | **拆包 + 保留旧模块名**（`strategy_runner.py` → `strategy_runner/` 包，`from strategy_runner import StrategyRunner` 继续可用） |
| 不做的事 | 本轮**不重构业务逻辑**、不改数据流、不改协议字段；逻辑优化另开任务 |
| 预计工作量 | 12 个 Phase，约 30–45 个 PR 级提交；建议 2–3 人并行，按目录分工 |
| 最大风险 | `strategy_runner.py`（5184 行）和 `StrategyFormModal.vue`（4382 行）耦合极深，需要先做"依赖测绘"再动手 |

---

## 1. 现状盘点（按行数排序，真实行数）

### 1.1 Python — 节点端 `node_client/`

| 行数 | 文件 | 问题性质 |
|---|---|---|
| 5184 | `strategy_runner.py` | **上帝对象**：`StrategyRunner` 一个类 180+ 方法，混了 4 条互斥执行路径 + 手动挂单 + 分档改单 + 盈亏保护 |
| 3675→4371 | `test_strategy_runner.py` | 测试同样超长，拆分后应同步按被测模块分文件 |
| 1721 | `mt5_client.py` | 一个类包揽：连接/会话、行情、报单、挂单、历史成交、品种规格 |
| 1246 | `node_client.py` | 主循环 + WS 收发 + 心跳 + 重连，职责混杂 |
| 1353 | `manual_scatter.py` | 手工散单：配置归一化 / 挂单状态机 / 分档计算 / 文案生成 / 运行时快照 五合一 |
| 1211 | `strategy_rules.py` | 规则求值 + SMC + 阈值 + 价格位 + 描述文案 |
| 1096 | `signal_pl.py` | 浮盈比例 + 止损止盈规划 + 触发判定 + 分档 |
| 858 | `risk_sizing.py` | 以损定量：分批 + 保本移动 + 描述 |
| 833 | `grid_trading.py` | 网格：计划 + 追踪 + 尾随 + 描述 |
| 801 | `account_risk.py` | 账户风控（与 `signal_pl` 有**明显重复**：float_pl / lot_pl 逻辑两处各写一遍） |
| 2658 | `test_manual_scatter.py` | 测试超长 |
| 1063 / 1067 | `nodel_client_dashboard/process_manager.py` vs `nodel_client_dashboard_web/process_manager.py` | **相似度 99.2%，纯复制粘贴** |

### 1.2 Python — 服务端 `backend/app/`

| 行数 | 文件 | 问题性质 |
|---|---|---|
| 1855 | `group_persist.py` | 任务/派单/快照/事件 四类持久化混在一个模块 |
| 1800 | `models.py` | 全站 Pydantic 模型（~110 个类）平铺，改任一接口都要翻这个文件 |
| 1321 | `strategy_templates.py` | 模板定义 + 规则归一化 + 校验 三合一 |
| 1247 | `signal_stats.py` | 信号中心统计：总览/流/节点/延迟/任务 五种查询挤一起 |
| 1151 | `group_dispatcher.py` | 派单编排 + 候选筛选 + 发送 + 收口 |
| 872 / 863 | `ai_follow_api.py` / `follow_api.py` | AI 跟单与普通跟单逻辑高度重叠（`follow_channels` / `follow_rules` 已部分共用） |
| 814 | `db.py` | 建表 + 迁移 + 引擎，`_migrate_*` 函数持续增长 |

### 1.3 前端 `frontend/src/`

| 行数 | 文件 | 问题性质 |
|---|---|---|
| 4382 | `components/StrategyFormModal.vue` | **单文件组件**：script 2200 + template 1300 + style 600；4 种规则类型的编辑器全塞一起 |
| 3465 | `styles.css` | 全局样式单文件，无分区 |
| 2625 | `api/types.ts` | 全部 TS 类型集中，被 **52 个文件** 引用 |
| 2283 | `components/OrderDialog.vue` | 下单弹窗：3 种策略形态 + 模板加载 + 触发 + 保存 |
| 1949 | `views/GroupsView.vue` | 分组列表 + 成员编辑 + 限价观察日志抽屉 + 关注者弹窗 |
| 1609 | `views/UserStrategiesView.vue` | 交易页：持仓/资产/历史/网格 四 tab + AI 策略 + 下单弹窗 |
| 1455 | `views/NodeDetailView.vue` | 节点详情：风控表单 + 历史 + 持仓 + 事件流 |
| 1404 | `views/GroupSignalsView.vue` | 信号任务表 + 派单事件展开 + 详情渲染 |
| 1402 | `views/FollowView.vue` | 关注/我的 两大 tab + 移动端 sheet + 离开确认 |
| 1344 | `components/TrendPanel.vue` | 趋势指标面板 |

---

## 2. 拆分目标与统一规约

### 2.1 文件规模规约（拆分后必须达标）

| 类型 | 上限 | 理由 |
|---|---|---|
| Python 模块 | 600 行 | 超过后 diff 难读、review 成本高 |
| Python 单类 | 400 行 / 25 个方法 | `StrategyRunner` 必须打散 |
| Vue 单文件组件 | 500 行 | 超过后 script 难以定位 |
| Vue 单文件 script 块 | 300 行 | 逻辑应下沉到 composable |
| TS 类型文件 | 500 行 | 按域分文件 |
| CSS 文件 | 600 行 | 按区块分文件 |

### 2.2 命名与目录规约

**Python（`node_client`）**：扁平模块 → 按域建包，模块内用相对导入。

```
node_client/
  strategy_runner/            # 从 strategy_runner.py 拆出
    __init__.py               # re-export StrategyRunner（保持旧 import 可用）
    base.py                   # StrategyRunner 骨架：生命周期/事件循环/快照/上报
    add_on.py                 # 加仓路径（原 _on_event 分支）
    risk_sized.py             # 以损定量路径
    grid.py                   # 网格路径
    manual_pending.py         # 手工挂单状态机
    lot_sltp.py               # 分档止损止盈
    price_level.py            # 价格位
    counter_limit.py          # 逆势限价
    pl_guard.py               # 浮盈/分档止盈保护
    common.py                 # 共享小工具（_as_float / _fmt_stop ...）
```

**Python（`backend/app`）**：`app` 下建子包，路由层保持顶层模块名不变（`main.py` 的 import 列表不动）。

```
backend/app/
  models/                     # 从 models.py 拆出
    __init__.py               # re-export 全部模型（保持 `from .models import X` 可用）
    node.py  group.py  strategy.py  follow.py  ai.py
    user.py  auth.py  deals.py  audit.py  signal.py  common.py
  persist/                    # 从 group_persist.py 拆出
    tasks.py  dispatches.py  snapshots.py  events.py  runtime.py
  templates/                  # 从 strategy_templates.py 拆出
    rules.py  normalize.py  validate.py  presets.py
  stats/                      # 从 signal_stats.py 拆出
    filters.py  overview.py  stream.py  nodes.py  latency.py  tasks.py
```

**Vue**：`composables/` 抽逻辑，`components/<Domain>/` 抽子组件，`types/` 按域拆类型。

```
frontend/src/
  composables/
    useStrategyForm.ts        # StrategyFormModal 的表单状态与校验
    useBatchLevels.ts         # 分档区间编辑
    useGridSizing.ts          # 网格试算
    useSignalPl.ts            # 浮盈/分档表单
    useOrderForm.ts           # OrderDialog 表单
    useWatchLogs.ts           # 限价观察日志
  components/strategy/
    RuleCounterEditor.vue  RuleTrendEditor.vue
    RuleRiskSizedEditor.vue  RuleGridEditor.vue
    BatchLevelsEditor.vue  ManualScatterEditor.vue
    SignalPlEditor.vue
  api/types/                  # 从 api/types.ts 拆出（re-export 汇总）
    node.ts group.ts strategy.ts follow.ts ai.ts user.ts signal.ts deals.ts
  styles/                     # 从 styles.css 拆出
    base.css layout.css components.css views.css
```

### 2.3 兼容性硬约束

1. **旧 import 路径必须继续可用**：`from strategy_runner import StrategyRunner`、`from .models import GroupOut`、`from '@/api/types'` 全部不报错。
   - 实现：新包 `__init__.py` 里 `from .base import StrategyRunner` + `__all__`。
2. **不改公共行为**：函数签名、返回结构、异常类型、日志文案全部保持；本轮只搬移。
3. **PyInstaller 打包不回归**：`node_client.spec` / `node_client_onedir.spec` 走 `Analysis(["node_client.py"])` 自动跟随 import，无需改；但打包后需实际跑一次验证。
4. **前端类型不破坏**：`api/types.ts` 改为 `export * from './types/xxx'`，52 个引用文件零改动。

---

## 3. 执行 Phase 拆分（12 个）

> 每个 Phase 是一个可独立合并的批次；Phase 内每个文件拆分一个提交。
> 每批次结束必须跑：节点端 `pytest node_client`、服务端 `pytest backend`、前端 `npm run typecheck` + `npm run build`。

### Phase 0 — 地基准备（无行为改动）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 0.1 | 引入规模检查脚本 | `scripts/check_file_size.py`（阈值按 §2.1，可临时豁免白名单） | 脚本能跑并在 CI/本地输出超限清单 |
| 0.2 | 补 `node_client` 与 `backend` 的测试基线快照 | 基线测试通过记录 | 拆分前后测试结果逐条比对，数量一致 |
| 0.3 | 写 `docs/architecture/module-map.md` | 各目录职责与依赖方向图 | 新人 5 分钟能说清"改 X 该动哪个文件" |
| 0.4 | 依赖方向规约落文档 | 规约：domain ← service ← api；禁止反向 | — |

**Phase 0 关键点**：先建"能测量、能对比"的尺子，再动刀。

### Phase 1 — `node_client` 纯工具模块拆分（低风险先行）

| # | 任务 | 拆分目标 | 预估 |
|---|---|---|---|
| 1.1 | `manual_scatter.py` | → `manual_scatter/` 包：`config.py`（归一化/default）/ `pending.py`（挂单解析与状态）/ `tiers.py`（分档计算）/ `describe.py`（文案与详情）/ `snapshot.py`（运行时快照）+ `__init__` re-export | 1353 → 6×~250 |
| 1.2 | `signal_pl.py` | → `signal_pl/` 包：`config.py` / `tiers.py` / `stops.py`（止损止盈规划）/ `trigger.py`（触发判定）/ `describe.py` | 1096 → 5×~220 |
| 1.3 | `risk_sizing.py` | → `risk_sizing/` 包：`config.py` / `plan.py`（分批计划）/ `breakeven.py` / `describe.py` | 858 → 4×~210 |
| 1.4 | `grid_trading.py` | → `grid_trading/` 包：`config.py` / `plan.py` / `trailing.py` / `describe.py` | 833 → 4×~210 |
| 1.5 | `strategy_rules.py` | → `strategy_rules/` 包：`ctx.py`（PositionCtx/AddDecision）/ `evaluate.py` / `price_level.py` / `smc.py` / `threshold.py` / `describe.py` | 1211 → 6×~200 |
| 1.6 | 消除 `signal_pl` 与 `account_risk` 的重复 | 浮盈比例 / 分档金额 / 触发判定**只保留一份实现**（放 `risk_core/`），另一方改为引用 | 逻辑去重，需写等价性测试 |

**Phase 1 验收**：所有 `test_manual_scatter.py` / `test_signal_pl.py` / `test_risk_sizing.py` / `test_grid_trading.py` / `test_strategy_rules.py` **不改动一行**通过（证明纯搬移）。测试文件过长问题留到 Phase 4。

### Phase 2 — `node_client/mt5_client.py` 拆分

| # | 任务 | 拆分目标 |
|---|---|---|
| 2.1 | 会话与连接 | `mt5_client/session.py`（connect/disconnect/ensure/account_info/algo_trading_allowed） |
| 2.2 | 行情与品种 | `mt5_client/market.py`（quotes/prices/resolve_symbol/filling_modes/symbol_point/symbol_spec/closed_bars/recent_bars/current_bar） |
| 2.3 | 报单 | `mt5_client/orders.py`（place_market_order / place_pending_order / modify_pending_order / cancel_order(s) / close_position / close_ticket / modify_position_sl / modify_sl_by_magic） |
| 2.4 | 历史与盈亏 | `mt5_client/history.py`（history_deals_since / realized_profit_by_magic / exit_deals_by_magic / balance_operations_since） |
| 2.5 | 工具函数 | `mt5_client/utils.py`（server_time_offset / history_window / pair_hedge_positions / retry helpers） |
| 2.6 | 门面 | `mt5_client/__init__.py` 暴露 `MT5Client`（组合上述 mixin），保证 `from mt5_client import MT5Client` 不变 |

> 注意：`MT5Client` 拆成 mixin 组合会改变 MRO，需保证实例属性初始化顺序（`__init__` 仍在门面类里）。若嫌风险，改为 **组合而非继承**：`MT5Client` 内部持有 `_Market`、`_Orders` 两个协作者，对外方法保持同名委托。

### Phase 3 — `node_client/strategy_runner.py` 拆分（**核心，需先做测绘**）

**Phase 3.0 依赖测绘（强制前置）**
产出 `docs/architecture/strategy_runner-deps.md`：列出 180+ 方法各自读写的 `self` 属性清单、依赖的外部模块、被哪些方法调用。这一步决定拆分边界，**不测绘不拆**。

**拆分方式：策略（Strategy/Handler）模式，不是 Mixin**
> Mixin 会把上帝对象换个形式继续存在，且属性耦合仍在。推荐把三条执行路径抽成独立协作者对象，`StrategyRunner` 只保留"事件循环 + 生命周期 + 快照上报"。

```python
# 概念结构（非最终代码）
class StrategyRunner:          # 只留 ~600 行
    def __init__(...): ...
    async def _on_event(self, event): 
        await self._handler.on_event(event)   # 按 mode 委派

class AddOnHandler:  ...
class RiskSizedHandler: ...
class GridHandler: ...
class ManualPendingHandler: ...
class LotSltpHandler: ...
class PriceLevelHandler: ...
class CounterLimitHandler: ...
class PlGuardHandler: ...
```

| # | 任务 | 拆分目标 | 预估 |
|---|---|---|---|
| 3.1 | 依赖测绘 | `strategy_runner-deps.md` | — |
| 3.2 | 抽公共工具与生命周期 | `base.py`（`__init__` / start / stop / cancel / status / snapshot / `_finish` / `_emit_progress` / `_resolve_realized_profit`） | ~700 |
| 3.3 | 抽加仓路径 | `add_on.py`（`_on_event` 加仓分支、`_open_first`、`_add_position`、`_counter_*`） | ~700 |
| 3.4 | 抽以损定量路径 | `risk_sized.py`（`_risk_*` 全族） | ~450 |
| 3.5 | 抽网格路径 | `grid.py`（`_grid_*` 全族） | ~450 |
| 3.6 | 抽手工挂单状态机 | `manual_pending.py`（`_manual_*` 全族 + `_place_manual_pending` + `_void_manual_pending`） | ~700 |
| 3.7 | 抽分档止损止盈 | `lot_sltp.py`（`_lot_sltp_*` 全族） | ~450 |
| 3.8 | 抽价格位 | `price_level.py`（`_price_level_*` 全族） | ~400 |
| 3.9 | 抽盈亏保护 | `pl_guard.py`（浮盈/分档止盈触发与减仓） | ~300 |
| 3.10 | 门面与兼容 | `__init__.py` 导出 `StrategyRunner`；旧 `from strategy_runner import StrategyRunner` 保持 | — |

**Phase 3 风险控制**：
- 每个 handler 抽出后，先单独跑 `test_strategy_runner.py` 全量；再跑 mock 端到端（`mock_mt5.py`）。
- `self` 属性跨 handler 共享的部分，集中到 `RuntimeState`（dataclass），避免"抽出去发现要访问一堆 self"。

### Phase 4 — `node_client` 剩余 + 测试文件拆分

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | `node_client.py` 拆分 | → `node_client_app/` 包：`ws.py`（连接/收发/重连）/ `heartbeat.py` / `main_loop.py`；入口 `node_client.py` 保留为薄壳（PyInstaller 入口不变） |
| 4.2 | `test_strategy_runner.py` 按被测模块拆分 | → `tests/test_runner_lifecycle.py` / `test_runner_add_on.py` / `test_runner_risk.py` / `test_runner_grid.py` / `test_runner_manual.py` / `test_runner_sltp.py` |
| 4.3 | `test_manual_scatter.py` 拆分 | 按 `manual_scatter` 子模块对应拆 |
| 4.4 | 引入 `node_client/tests/` 目录 | 测试与被测代码同包，pytest.ini 的 `pythonpath` 调整 |

### Phase 5 — `backend/app/models.py` 拆分

| # | 任务 | 拆分目标 |
|---|---|---|
| 5.1 | 按域抽取 | `models/node.py`（Node*/ClientVersion*）/ `group.py`（Group*/LimitWatch*）/ `strategy.py`（Strategy*/BatchLevel*/Signal*Pl*）/ `follow.py` / `ai.py` / `user.py`（User*/Role*/Profile*/Me*）/ `auth.py`（Login*/TwoFA*/Phone*/Sms*）/ `deals.py` / `audit.py` / `signal.py` / `common.py`（NodeDispatch*/Paginated*） |
| 5.2 | 汇总导出 | `models/__init__.py` `from .node import *` 等，`__all__` 完整列出 |
| 5.3 | 兼容验证 | `grep -rn "from .models import\|from app.models import" backend` 全部仍可用 |

### Phase 6 — `backend/app` 持久化层拆分

| # | 任务 | 拆分目标 |
|---|---|---|
| 6.1 | `group_persist.py` → `persist/` 包 | `tasks.py`（task 状态流转）/ `dispatches.py`（派单增删改查）/ `snapshots.py`（快照读写 + 重写）/ `events.py`（事件列表）/ `runtime.py`（manual runtime 合并 / consume_*）/ `totals.py`（_refresh_* 汇总刷新） |
| 6.2 | `strategy_templates.py` → `templates/` 包 | `presets.py`（default_* 模板）/ `rules.py`（规则 dataclass + to_rules）/ `normalize.py`（normalize_*）/ `validate.py`（校验规则）/ `__init__` re-export |
| 6.3 | `signal_stats.py` → `stats/` 包 | `filters.py`（SignalFilters/时间窗口）/ `overview.py` / `stream.py`（游标分页）/ `nodes.py` / `latency.py` / `tasks.py` |
| 6.4 | `group_dispatcher.py` 拆分 | `dispatcher.py`（主编排）/ `candidates.py`（候选筛选）/ `sender.py`（sync/poll 发送）/ `commands.py`（build_*_command） |
| 6.5 | `db.py` 迁移函数分离 | `db.py` 保留引擎/建表；`_migrate_*` 全部移到 `migrations.py`（按表分函数），`init_db()` 只做编排 |

### Phase 7 — `backend` 服务层与 AI/跟单去重

| # | 任务 | 说明 |
|---|---|---|
| 7.1 | 抽取公共跟单基座 | `follow_channels.py` / `follow_rules.py` 已存在；把 `ai_follow_api.py` 与 `follow_api.py` 重复的"加入/离开/暂停/明细聚合"抽到 `follow_base.py`，两套 API 只保留参数差异 |
| 7.2 | `follow_api.py` 瘦身 | 目标 ≤ 500 行；`ai_follow_api.py` ≤ 400 行 |
| 7.3 | 后端大测试文件拆分 | `test_group_dispatch.py`(2291) / `test_group_api.py`(1875) / `test_follow_api.py`(1317) 按被测函数域拆分 |

### Phase 8 — 前端类型与样式拆分

| # | 任务 | 拆分目标 |
|---|---|---|
| 8.1 | `api/types.ts` → `api/types/` 包 | `node.ts` / `group.ts` / `strategy.ts` / `follow.ts` / `ai.ts` / `user.ts` / `signal.ts` / `deals.ts` / `common.ts`；`api/types.ts` 保留 `export * from './types/xxx'`（52 个引用文件零改动） |
| 8.2 | `styles.css` → `styles/` | `base.css`（变量/reset/字体）/ `layout.css` / `components.css` / `views.css`；`styles.css` 改为 `@import` 入口（注意 `@import` 顺序影响层叠） |
| 8.3 | 类型拆分验证 | `npm run typecheck` + `npm run build` 通过 |

### Phase 9 — `StrategyFormModal.vue` 拆分（**前端核心**）

**9.0 结构测绘**：标注 template 中 4 类规则各自的字段区块边界、哪些 state 被哪些区块使用。

| # | 任务 | 拆分目标 |
|---|---|---|
| 9.1 | 抽 composable | `useStrategyForm.ts`（表单状态/初始化/保存/校验编排）/ `useBatchLevels.ts`（分档区间增删改、redistribute/append）/ `useSignalPl.ts`（浮盈与分档表单）/ `useGridSizing.ts`（网格试算）/ `useRuleCollapse.ts` |
| 9.2 | 抽子组件 | `strategy/RuleCounterEditor.vue` / `RuleTrendEditor.vue` / `RuleRiskSizedEditor.vue` / `RuleGridEditor.vue` / `BatchLevelsEditor.vue` / `ManualScatterEditor.vue` / `SignalPlEditor.vue` |
| 9.3 | 抽常量 | `RULE_TYPE_LABEL` / `FIELD_HELP` / `*_OPTIONS` 等移到 `constants/strategyForm.ts` |
| 9.4 | 收敛 template | 主组件 template 只剩"规则容器 + 弹窗骨架"，各规则区块交给子组件 |
| 9.5 | scoped style 拆分 | 每子组件自带 style；公共表单样式提到 `styles/forms.css` |

> 验收：`StrategyFormModal.vue` ≤ 500 行；子组件均 ≤ 400 行；**表单行为零变化**（用现有保存流程做端到端校验）。

### Phase 10 — 其余前端大视图拆分

| # | 任务 | 拆分目标 |
|---|---|---|
| 10.1 | `OrderDialog.vue`(2283) | 抽 `useOrderForm.ts` + `components/order/GridParamsForm.vue` / `RiskParamsForm.vue` / `TemplatePicker.vue`；3 种策略各自成子组件 |
| 10.2 | `GroupsView.vue`(1949) | 抽 `useWatchLogs.ts` + `components/group/MemberEditor.vue` / `WatchLogDrawer.vue` / `FollowerDialog.vue` / `GroupFormDialog.vue` |
| 10.3 | `UserStrategiesView.vue`(1609) | 四 tab 抽成 `components/trading/PositionsTab.vue` / `AssetsTab.vue` / `HistoryTab.vue` / `GridTasksTab.vue`；AI 策略区独立组件 |
| 10.4 | `NodeDetailView.vue`(1455) | 抽 `useRiskForm.ts` + `components/node/RiskForm.vue` / `DispatchHistory.vue` / `PositionTable.vue` / `EventFeed.vue` |
| 10.5 | `GroupSignalsView.vue`(1404) | 抽 `useSignalTasks.ts` + `components/signals/TaskTable.vue` / `DispatchDetail.vue` / `EventTimeline.vue` / `EventDetailRows.vue` |
| 10.6 | `FollowView.vue`(1402) | 抽 `components/follow/LeaderList.vue` / `MyFollowing.vue` / `NodePicker.vue` / `LeaveDialog.vue`；移动端 sheet 独立组件 |
| 10.7 | `TrendPanel.vue`(1344) | 抽 `components/trend/TrendChart.vue` / `IndicatorCards.vue` / `VerdictBadge.vue` |

### Phase 11 — 桌面控制台（`nodel_client_dashboard*`）拆分与去重

| # | 任务 | 说明 |
|---|---|---|
| 11.1 | 消除 `process_manager.py` 双份 | 两份相似度 99.2% → 抽出 `dashboard_core/` 共享包，两端各留薄壳 |
| 11.2 | 其余共享模块去重 | `dashboard_session.py` / `client_deploy.py` / `env_file.py` / `build_package.py` / `version_service.py` / `recovery.py` / `store.py` / `node_log.py` / `single_instance.py` / `admin_elevate.py` / `tray_icon.py` / `terminal_account.py` / `auth_service.py` / `theme.py` 同上 |
| 11.3 | `app_ui.py`(1665) 拆分 | → `dashboard_ui/`：`layout.py`（窗口与栅格）/ `cards.py`（节点卡片）/ `tray.py`（托盘与恢复）/ `version.py`（版本检查与更新）/ `batch.py`（批量操作）/ `dialogs.py`（各弹窗） |
| 11.4 | `dashboard_bridge.py`(1165) 拆分 | → `dashboard_bridge/`：`routes.py`（HTTP 路由）/ `session.py` / `instances.py` / `version.py` / `env_io.py` / `logs.py` |
| 11.5 | `frontend-console` 组件拆分 | `VersionUpdateModal.vue`(614) / `ConsoleShell.vue`(564) / `ImportModal.vue`(387) / `SettingsModal.vue`(381) 各自抽 composable + 子组件 |

### Phase 12 — 收口

| # | 任务 |
|---|---|
| 12.1 | `scripts/check_file_size.py` 收紧到无豁免（或仅剩白名单） |
| 12.2 | 更新 `docs/architecture/module-map.md`，补充"新功能该放哪"的对照表 |
| 12.3 | CI（若有）接入规模检查与测试 |
| 12.4 | 拆分前后性能回归：节点端事件处理延迟、API P95 对比 |

---

## 4. 优先级与并行建议

| 优先级 | Phase | 理由 |
|---|---|---|
| P0 | Phase 0（地基） | 没有尺子就没法验收 |
| P1 | Phase 1、2、5 | 纯搬移、风险最低、立刻见效；backend/models 是最独立的 |
| P2 | Phase 6、7 | 服务端按域拆，依赖 `group_persist` 的调用点较多，需连带改 API 层 |
| P3 | Phase 3、9 | 两块最硬的骨头（上帝对象 + 上帝组件），**依赖测绘必须先做** |
| P4 | Phase 4、8、10、11、12 | 测试拆分与控制台去重；控制台与主业务耦合低，可最后做 |

**并行分工建议（3 人）**：
- A：Phase 1 + 2 + 3（节点端）
- B：Phase 5 + 6 + 7（服务端）
- C：Phase 8 + 9 + 10（前端）+ Phase 11（控制台）

**冲突规避**：`strategy_runner` 拆分与 `manual_scatter/signal_pl` 拆分必须串行（A 单人串行处理），否则 import 路径与 `__init__` 会反复冲突。

---

## 5. 风险登记表

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 拆包后 `from strategy_runner import X` 失效 | 节点端全线 ImportError | 每个包 `__init__.py` 做 `re-export` + `__all__`；Phase 1 起就加一条冒烟测试断言旧路径可用 |
| R2 | `StrategyRunner` 抽取后 `self` 属性跨模块访问混乱 | 隐性行为变更 | 引入 `RuntimeState` 显式传参；每抽一个 handler 跑全量测试 |
| R3 | Mixin 拆分导致 MRO/初始化顺序变化 | 难查 bug | 采用**组合**而非继承；`__init__` 只留门面类 |
| R4 | Vue 拆分后 `scoped` 样式失效或层叠错乱 | UI 视觉回归 | 公共样式提到全局 `styles/`，保留顺序；改完必须人工比对截图 |
| R5 | `api/types.ts` 拆包后 `export *` 名字冲突 | TS 编译报错 | 按域划分时保证类型名全局唯一；`typecheck` 作为门禁 |
| R6 | `styles.css` 用 `@import` 引入导致首屏渲染变慢/顺序错乱 | 视觉回归 | 拆出后改为在 `main.ts` 按序 import；或保留单文件仅做区块注释分组分批迁移 |
| R7 | PyInstaller 打包后隐藏 import 缺失 | 客户端无法启动 | 每 Phase 3/4 后跑一次 `build_exe.bat` 实机验证 |
| R8 | 控制台双目录去重影响 Tk/Web 两端行为 | 桌面端回归 | 先加特征测试锁行为，再合并；`test_dashboard_*.py` 全绿再动 |
| R9 | 拆分期间多人并行产生大面积冲突 | 合并痛苦 | 每个 Phase 独立分支；同一目录不并行；合入主干前 rebase |
| R10 | 拆分与业务需求同时进行 | 需求被拖 | 拆分期间冻结该目录的新功能，或先合需求再拆 |

---

## 6. 每批次统一验收清单（Definition of Done）

- [ ] 目标文件全部 ≤ §2.1 上限（无豁免）
- [ ] 旧 import 路径全部可用（有冒烟断言）
- [ ] `pytest node_client` 全绿（用例数与拆分前一致，无 skip 化）
- [ ] `pytest backend` 全绿（用例数一致）
- [ ] `npm run typecheck` + `npm run build` 通过
- [ ] 无新增 `# noqa`、无新增"临时兼容 shim"（超过 1 个 shim 说明拆得不彻底）
- [ ] `git diff --stat` 中 `*.py` 无逻辑行改动（纯搬移时 diff 应表现为删除+新增）
- [ ] `scripts/check_file_size.py` 通过
- [ ] 拆分涉及的模块在 `docs/architecture/module-map.md` 有对应条目

---

## 7. 首周建议排期（可落地）

| 日 | 任务 | 产出 |
|---|---|---|
| D1 | Phase 0.1 / 0.2 | `check_file_size.py`；基线测试记录 |
| D2 | Phase 0.3 / 0.4 | `module-map.md`；依赖规约 |
| D3 | Phase 1.1（`manual_scatter`） | 第一个包拆分，作为样板 |
| D4 | Phase 1.2 + 1.3（`signal_pl` / `risk_sizing`） | — |
| D5 | Phase 1.4 + 1.5（`grid_trading` / `strategy_rules`） | Phase 1 收口 + 复盘样板 |
| D6 | Phase 3.0（`strategy_runner` 依赖测绘） | 测绘文档 |
| D7 | Phase 5.1（`backend/models` 拆包） | 后端第一块 |

---

## 附：为什么是这个方案（取舍说明）

1. **为什么"拆包 + re-export"而不是直接改 import？**
   52 个前端文件 + 30+ 测试文件依赖旧路径。一次性改 import 是"为了整洁而制造风险"。保留 re-export 让拆分与功能改动解耦，后续有稳定后再逐步收敛 import。

2. **为什么 `strategy_runner` 用组合而不是 Mixin？**
   Mixin 拆分只是把 5184 行分摊到 8 个文件里，`self` 上的 60+ 个可变属性仍然共享，"改一个功能可能炸另一个功能"的本质没变。组合 + `RuntimeState` 显式状态对象，才能真正做到职责隔离。

3. **为什么不一次性大重构？**
   本项目是实盘交易系统，`strategy_runner` / `signal_pl` / `mt5_client` 直接决定下单行为。纯搬移 + 全量测试是唯一能证明"没改行为"的方式。任何与重构混在一起的逻辑改动都会让回归无法归因。

4. **为什么先拆测试文件（Phase 4）？**
   超长测试文件是拆分的**阻力**而非负担：拆完源码后，测试文件会变成唯一"说清楚行为"的文档。所以源码拆完立刻拆测试，而不是留到最后。
