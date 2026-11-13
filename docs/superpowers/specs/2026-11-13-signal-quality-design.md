# 信号质量分：设计文档

> 状态：已拍板（质量分 = 历史同类信号的成功率，前期从简）
> 关联：`docs/superpowers/specs/2026-11-11-signal-center-perf-routing-design.md`（信号中心绩效 + 路由中枢）

---

## 〇、一句话定义

> **质量分 = 历史同类信号（相同「信号类型 + 周期」）的信号级胜率**（0~100，小样本做收缩）

不掺行情、不掺新闻、不掺趋势 —— 那些是二期的事。

**为什么是这个定义**：质量分是「把已经算出来的历史绩效，前置成一条可排序的参考」。
既然绩效面板已经把信号级胜率算对了，质量分就不该另起一套算法 ——
**同一个口径、同一个数字**，只是换个位置呈现（列表里的一列 vs 面板里的一张表）。

---

## 一、与既有口径的关系（一期最重要的约束）

### 1.1 必须完全复用 `signal_perf` 的口径

| 项 | 口径 | 出处 |
|---|---|---|
| 统计范围 | 只算 **AI 智能加仓（`tpl_1`）** 的任务 | `signal_perf.AI_ADAPTIVE_TEMPLATE_ID` |
| 判定 | 该信号**全部持仓**净盈亏合计 `> 0` → 赢（**信号级**，不是持仓级） | `signal_perf.PerfBucket.signals_win` |
| 分母 | 已收口**且有成交**的信号数（`signals_closed - signals_no_trade`） | `PerfBucket.signal_win_rate` |
| 进行中 | 不进分母（单列「进行中」） | `signal_perf.performance()` |

> **铁律一：质量分与绩效面板必须同源同口径。**
> 同一批数据，两边数字必须**逐位相等**（有测试兜底，见 §七）。
> 否则会出现「绩效页显示 OB 胜率 62%，质量分显示 58%」—— 这种对不上会让管理员直接不信这个数字。

### 1.2 分桶键只用**信号级**维度

| 字段 | 用哪个 | 为什么 |
|---|---|---|
| 周期 | `signal_history.timeframe`（**信号级**） | 入场级周期（`signal_entry_leg.timeframe`）会把「点数间距」混进「ATR 间距」的分桶 |
| 信号类型 | `signal_history.signal_type` | 首仓结构类型，天然是信号级概念 |

两个字段 `signal_history` 里**已经有了**，`signal_meta` 也已归一 → **零新增字段**。

### 1.3 不做档位级

与既定决策一致：档位级胜率**不做**，质量分同理**不做档位级评分**。

---

## 二、分桶键与回退链

### 2.1 分桶键

**主桶**：`(signal_type, timeframe)`
**次桶**：`signal_type`（周期没细分出来时）
**兜底**：全局

### 2.2 回退链（样本不足时逐级放宽）

```
① (type, tf)   n ≥ MIN_SAMPLES(=5)  →  用这一档
② type 单独     n ≥ MIN_SAMPLES(=5)  →  用这一档
③ 全局          n ≥ 1                →  用这一档
④ 都没有                                 →  分数 NULL，前端显示「暂无历史」
```

**第②档是「同类型跨周期」的聚合**，不是「样本最多的那个周期」：
一个类型在 M15 有 2 笔、H1 有 8 笔，类型桶就该是 **10 笔** ——
取单个最大桶会漏掉其它周期的样本，第二档就名存实亡了。
聚合桶的胜率 = 总赢数 / 总样本数（**不能平均各周期的胜率** —— 那是加权错误）。

**必须带回退标记**（`basis` / `level`）—— 前端要能告诉用户
「这个 58 分是 87 笔样本算出来的」还是「只有 3 笔样本，拿全局兜的底」。
不给标记的数字就是噪音。

---

## 三、小样本：贝叶斯收缩（唯一的算法）

### 3.1 公式

```
先验分 = (赢数 × 100 + k × 全局胜率) / (样本数 + k)
```

`k = 10`（先验强度 ≈ 相当于额外观察 10 个样本）。

**这是标准的 Laplace / 贝叶斯收缩**：样本多时退化成原始胜率，样本少时自动向全局回归。

> 注：这里写成 `赢数 × 100` 而不是 `赢数`，是为了让 `全局胜率` 可以直接传**百分数**（0~100），
> 不用在调用点先除以 100 —— 单位混用是这个公式最容易写错的地方。

### 3.2 数值示例（全局胜率 g = 55%）

| 桶 | n | 赢 | 原始胜率 | **先验分** | 效果 |
|---|---|---|---|---|---|
| ob·M15 | 87 | 51 | 58.6% | **58.2%** | 样本足，原始值几乎不受影响 ✓ |
| fvg·H1 | 3 | 3 | 100% | **65.4%** | 3 笔 100% 被拉回 65% ✓ |
| fvg·H1 | 3 | 0 | 0% | **42.3%** | 3 笔全亏被拉回 42% ✓ |
| pdz·D1 | 120 | 66 | 55.0% | **55.0%** | 完全不受影响 ✓ |

### 3.3 样本量分档（前端打标签）

| n | `level` | 展示 |
|---|---|---|
| ≥ 20 | `sufficient` | 正常色带 |
| 5 ~ 19 | `thin` | 正常色带 + 灰字「样本少」 |
| < 5 | `insufficient` | 灰色 + 「样本不足」 |
| — | `none` | 「暂无历史」 |

> 注意 `level` 表达的是**样本充分度**，与 §2.2 的**回退层级**是**两个正交维度**：
> 一个桶可以是「主桶命中（level=type_tf）但样本偏少（n=6）」。
> 所以输出里 `basis`（回退到了哪一档）与 `sample_level`（样本够不够）**分开两个字段**。

---

## 四、数据从哪来（唯一的代码改动：抽一个纯函数）

### 4.1 重构点

`signal_perf.performance()` 里的聚合循环现在写死了三个桶（totals / by_timeframe / by_signal_type）。
要加「类型×周期」联合桶与质量分，**不要复制一份**，而是把聚合部分**抽出来**：

```
signal_perf.py
  └─ 抽出 _aggregate(signals, tasks_by_signal, profit_by_task, key_fn) -> {key: PerfBucket}
       ├─ performance()  用它（现有口径，一行不改）
       └─ signal_quality.py 也用它（新增组合键）
```

这是**纯重构** —— 现有绩效面板的数字不会变（`test_signal_perf.py` 兜底）。

### 4.2 新模块 `backend/app/signal_quality.py`

职责只有三件：

1. `compute_prior(window)` → 算出全局先验 + 各桶先验
2. `score_for(signal_type, timeframe, prior)` → 给定信号查分（含回退链）
3. TTL 缓存（60 秒足够 —— 先验只在信号收口时才变）

---

## 五、关键设计决策

### 5.1 先验**不受信号中心筛选条影响**

先验**固定用「近 30 天全量」**计算，与 `SignalFilters` 无关。

**理由**：先验是**背景知识**，不是「当前筛选结果的统计」。
否则管理员切一下筛选条，质量分就变了 —— 同一条信号在不同 tab 显示不同分数，必然被质疑。

（与 `signal_perf` 里「绩效面板、下钻明细都调这里，不允许各页自己算一遍」的既有纪律一致。）

### 5.2 **不落库** → `signal_history` 不新增任何列

这是「最简」最大的收益：**无迁移、无回填、无版本号问题**。

> **代价（必须知道）**：同一条信号，分数会随时间变（今天 58.2，一周后可能 60.1）。
> 一期可接受（先验本来就是活的）。
> 将来若要复盘「当时为什么路由了它」，再补一列**快照分**即可 —— 那是二期的事，现在别做。

### 5.3 分数 ≠ 概率，别包装成「82% 概率赚钱」

质量分**就是**历史胜率，含义清晰、无需解释。
但 UI 文案别写「赚钱概率 82%」—— 那是在给一个**后验统计量**贴因果标签。

### 5.4 分数**只展示，不参与任何交易决策**（一期）

- 不因低分自动丢弃信号；
- 不因低分阻止路由；
- 不作为任何门禁条件。

**理由**：低分桶一旦被系统性过滤 → 样本不再更新 → 先验越来越陈旧 → 越筛越极端。
二期要做的话，做成**分组级软阈值 + 默认关闭 + 覆盖率门槛**。

### 5.5 胜率单独看不够，但一期不进分数

严格说，「胜率」不含盈亏比 —— 55% 胜率配 1:10 盈亏比是**负期望**。

一期**只展示均盈亏作参考，不进分数**（保持「质量分 = 成功率」这个单一清晰语义）。
期望值合成留到二期 —— 那时需要先有足够数据验证均盈亏是否稳定。

### 5.6 缓存键

先验缓存按 `window` 维度（默认 30d）。缓存 TTL 60 秒。

---

## 六、输出形态

### 6.1 单条信号挂 `quality`

```json
{
  "quality": {
    "score": 58.2,
    "win_rate": 58.6,
    "n": 87,
    "wins": 51,
    "avg_profit": 12.4,
    "basis": "ob|M15",
    "basis_label": "OB · M15",
    "level": "type_tf",
    "sample_level": "sufficient"
  }
}
```

- `basis` / `basis_label`：分数**从哪个桶来**（回退链的落点）
- `level`：`type_tf` / `type` / `global` / `none`
- `sample_level`：`sufficient` / `thin` / `insufficient` / `none`

### 6.2 先验表接口

```
GET /api/signal-center/quality?window=30d
→ {
    "window": "30d",
    "global": { "win_rate": 55.0, "n": 412, "wins": 227, "score": 55.0 },
    "buckets": [
      { "key": "ob|M15", "signal_type": "ob", "timeframe": "M15",
        "label": "OB · M15",
        "n": 87, "wins": 51, "win_rate": 58.6, "score": 58.2,
        "avg_profit": 12.4 }
    ],
    "updated_at": 1731...
  }
```

---

## 七、前端呈现

| 位置 | 表现 |
|---|---|
| **信号流表格** | 新增一列「质量」，徽章显示 `58.2`，色带：≥60 绿 / 50~60 蓝灰 / <50 橙红 |
| **悬浮提示** | 「OB · M15 历史 87 笔 · 胜率 58.6% · 均盈亏 +12.4」 |
| **样本不足时** | 徽章置灰 + 「样本少」角标 —— **不让它看起来和数据充足时一样** |
| **路由面板** | 信号卡片右上角显示质量分（管理员路由时的主要参考） |
| **绩效面板** | 新增「先验表」区块：按类型×周期列出 n / 胜率 / 质量分 |

---

## 八、实施清单

### 后端

| 文件 | 改动 |
|---|---|
| `backend/app/signal_perf.py` | 抽出 `_aggregate(...)` 纯函数；**现有口径一行不改** |
| `backend/app/signal_quality.py` | **新增**：先验计算 + 回退链 + 收缩 + TTL 缓存 |
| `backend/app/signal_stats.py` | `stream()` 每条信号挂 `quality` 字段 |
| `backend/app/signal_center_api.py` | 新增 `GET /quality` |
| `backend/app/orm.py` / `db.py` | **不改**（不落库，无迁移） |

### 前端

| 文件 | 改动 |
|---|---|
| `frontend/src/api/types.ts` | `SignalQuality` / `SignalQualityPrior` 类型 |
| `frontend/src/stores/hub.ts` | `fetchSignalCenterQuality()` |
| `frontend/src/components/signal-center/SignalStreamTable.vue` | 质量分列 + 悬浮说明 |
| `frontend/src/components/signal-center/RoutingPanel.vue` | 信号卡片显示质量分 |
| `frontend/src/components/signal-center/PerformancePanel.vue` | 先验表区块 |

### 测试

| 文件 | 覆盖 |
|---|---|
| `backend/tests/test_signal_quality.py` | 收缩公式数值、回退链、样本分档、`n=0` 兜底、**与 `signal_perf` 口径一致性**（同一批数据两边数字必须相等） |

---

## 九、明确不做（二期再说）

| 项 | 为什么不做 |
|---|---|
| 行情 / 趋势 / 新闻 / 波动率因子 | 需异步探针，复杂度翻几倍，且**要等真实数据验证有没有区分度** |
| 分组维度（「这个信号对这个分组好不好」） | 需按分组重算先验，样本会被切碎 |
| 档位级评分 | 与既定决策一致 —— 不做档位级 |
| 快照分落库 | 一期不需要复盘 |
| 分数参与交易决策 | 见 §5.4 |
| 期望值（胜率 × 均盈亏）进分数 | 见 §5.5 |

---

## 十、待拍板（一期默认值）

| # | 决策点 | 默认值 |
|---|---|---|
| 1 | 分桶键 | `(signal_type, timeframe)` |
| 2 | 先验窗口 | 近 30 天 |
| 3 | 收缩强度 `k` | 10 |
| 4 | 最小样本数 `MIN_SAMPLES` | 5 |

---

## 十一、实施落地情况（已实现）

| 项 | 状态 | 位置 |
|---|---|---|
| 聚合抽成纯函数 | ✅ | `backend/app/signal_perf.py::_aggregate` |
| 质量分模块 | ✅ | `backend/app/signal_quality.py`（新增） |
| 信号流挂 quality | ✅ | `backend/app/signal_stats.py::_stream_items` |
| `/quality` 接口 | ✅ | `backend/app/signal_center_api.py` |
| 前端类型 | ✅ | `frontend/src/api/types.ts` |
| store 取数 | ✅ | `frontend/src/stores/hub.ts::fetchSignalCenterQuality` |
| 质量分徽章组件 | ✅ | `frontend/src/components/signal-center/QualityBadge.vue`（新增） |
| 信号流质量列 | ✅ | `SignalStreamTable.vue` |
| 路由面板质量分 | ✅ | `RoutingPanel.vue` |
| 绩效面板先验表 | ✅ | `QualityPriorTable.vue`（新增）+ `PerformancePanel.vue` |
| 单测 | ✅ | `backend/tests/test_signal_quality.py`（19 条）、`test_signal_center_api.py`（4 条） |

### 验证结果

- 后端全量：**1573 passed**（含新增 23 条）
- 前端：`vue-tsc --noEmit` 通过、`vite build` 通过、`verify-build` 61/61 chunk 校验通过
- **口径一致性**：有一条专门用例钉死「质量分的胜率 = 绩效面板同一桶的胜率」（逐位相等）

### 实施中发现并修正的两个坑

1. **收缩公式的单位混用**：`(赢数 + k × 百分数) / (样本数 + k)` 会算出 42.5% 这种
   明显不对的数（赢数是「笔」，全局胜率是「百分数」）。修正为
   `(赢数 × 100 + k × 百分数) / (样本数 + k)`。
2. **类型级回退取单个最大桶**：会让回退链第二档漏掉其它周期的样本。
   改成跨周期**聚合**（总赢数 / 总样本数）。

---

## 十二、演示数据（无真实数据时看效果）

没有真实数据时，界面的每个面板都是空的 —— 数字长什么样、颜色怎么分、
回退链怎么降级，全都看不出来。所以提供了三个脚本：

| 脚本 | 作用 |
|---|---|
| `backend/scripts/seed_signal_center_demo.py` | 灌一批「像真实数据」的历史 |
| `backend/scripts/verify_seed_data.py` | 自检：口径一致 / 类型有差异 / 三档样本都触发 |
| `backend/scripts/peek_signal_stream.py` | 打印信号流里每条信号挂的 quality |

```bash
cd backend
python -m scripts.seed_signal_center_demo --reset          # 灌 320 条 / 30 天
python -m scripts.verify_seed_data                          # 自检
python -m scripts.peek_signal_stream                        # 看 quality 长什么样
```

### 造数刻意做出的「形态」

造数据的目的不是让界面有东西，而是**让界面的每个分支都被走到**：

1. **不同类型胜率明显差异** —— OB 64% / FVG 54% / PDZ 44% / Fibo 51%
   → 信号流的「质量」列才会出现绿 / 蓝 / 橙红三种色带；
2. **样本量刻意不均衡** —— OB·M15 有 60 笔（充分）、Fibo·H1 有 19 笔（偏少）、
   若干桶只有 1~4 笔（不足）
   → 「样本充分 / 样本少 / 样本不足」三档标签都能被触发；
3. **部分信号进行中**（28 条）→ 进分母的与不进分母的能对照出差别；
4. **少量零成交 / 拒收 / 跳过** → 「节点命中」「拦截原因」不是空的；
5. **少量未标注类型 / 周期** → 检验「未标注」的处理；
6. **一赢一亏但合计为正** → 信号级胜率（合计 > 0）与持仓级胜率（逐笔判定）
   在界面上能看出**口径差异** —— 这正是 `signal_perf` 要展示的重点。

### 造数的口径纪律

**造数用的判定必须与 `signal_perf` 一致**，否则造出来的数据在界面上看着漂亮，
一打开绩效面板就自相矛盾。

所以脚本**不重写胜率判定**，而是**直接往 `signal_perf` 认得的那几张表里写**
（`signal_history` / `group_signal_task` / `mt5_deal`），
复用同一套字段与聚合口径。宁可 import，也不要复制一份 SQL —— 复制就会漂移。

### 清空演示数据

```bash
python -m scripts.seed_signal_center_demo --reset
```

`--reset` 会先清空 `signal_history` / `group_signal_task` / `mt5_deal` /
`signal_dispatch` / `signal_entry_leg` 五张表再灌。
**注意：它会删掉真实数据**，只在开发库上用。
