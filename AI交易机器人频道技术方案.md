# AI 交易机器人频道 — 技术实现方案

| 项目       | 说明                                                                 |
| ---------- | -------------------------------------------------------------------- |
| 文档版本   | v1.1（2026-11-09 实施补充）                                             |
| 编写日期   | 2026-11-04                                                           |
| 适用范围   | 后台 FastAPI（`backend/app`）+ Web 前端（`frontend/src`）             |
| 关联原型   | `UI/H5.html` →【我的】→【AI交易机器人频道】                           |
| 关联文档   | `我的带单频道技术方案.md`（**本文档是其姊妹篇，共用同一套底层机制**）  |
| 状态       | 已实施，当前行为与升级步骤见 §17                                           |

---

> §1–16 保留初版评审设计；当前实现与已确认产品规则见 §17，冲突处以 §17 为准。§6.5 等级阈值沿用。

## 目录

1. [背景与目标](#1-背景与目标)
2. [原型解读](#2-原型解读)
3. [与带单频道的差异分析（关键）](#3-与带单频道的差异分析关键)
4. [核心概念对齐：「机器人」到底是什么](#4-核心概念对齐机器人到底是什么)
5. [总体架构与设计取舍](#5-总体架构与设计取舍)
6. [数据模型设计](#6-数据模型设计)
7. [审核生命周期状态机](#7-审核生命周期状态机)
8. [后端 API 设计](#8-后端-api-设计)
9. [审批写穿与 AI 分发安全](#9-审批写穿与-ai-分发安全)
10. [级联清理与生命周期联动](#10-级联清理与生命周期联动)
11. [前端设计与实现](#11-前端设计与实现)
12. [数据库迁移与审计日志](#12-数据库迁移与审计日志)
13. [分阶段实施计划（PR 拆分）](#13-分阶段实施计划pr-拆分)
14. [测试方案](#14-测试方案)
15. [风险与遗留问题](#15-风险与遗留问题)
16. [附录 A：原型字段 → 后端字段对照表](#16-附录-a原型字段--后端字段对照表)
17. [实施契约与升级验收](#17-实施契约与升级验收)

---

## 1. 背景与目标

### 1.1 现状

`backend/app/ai_follow_api.py` 实现了【AI 交易】Tab 的**镜像于带单市场**的一套 API：

| 维度     | 【带单】Tab                  | 【AI 交易】Tab                     |
| -------- | ---------------------------- | ---------------------------------- |
| 路由     | `/api/follow/*`              | `/api/ai-follow/*`                 |
| 审计     | category=`follow`            | category=`ai_follow`               |
| 市场     | `tpl_2` + `tpl_3`            | `tpl_1`（AI 智能加仓）             |
| 准入     | `follow_leader`              | `ai_strategy` **且** `follow_leader` |
| 分组开关 | 无                          | `accept_ai_follow`（默认关）       |

两个市场**共用同一张 `group_follower_node` 表**、**同一套分发链路**，靠「分组绑定策略的模版」天然互斥。

当前跟单是**即时的**：`POST /api/ai-follow/join` 直接写 `group_follower_node`，节点立刻进入信号分发。

### 1.2 问题

AI 跟单比普通带单**更需要审核**，原因有三条，且都不是普通带单那种程度：

1. **AI 跟单是持续加仓，不是单次交易**。普通带单一单一结，错了赔一次；AI 智能加仓是**连续加仓直到策略条件满足**，跟错一次可能连续错很多次。跟单员在跟单期间**不控手数**（`AiTradeView.vue` 的既定决策：「跟随 AI 加仓机器人的加仓节奏，手数不缩放」），意味着**风险量级完全由机器人一侧决定**，跟单员没有任何止损旋钮。
2. **风险参数是对称的但责任不对称的**。跟单员看不到、也改不了 `max_allow_num` / `total_lot_limit` / `batch_levels`，但这些参数决定了单轮加仓能加多少手、总共能亏多少。盲跟 = 把资金交给一个自己不承担后果的策略。
3. **机器人主人的策略参数可热推**（M2 机制：主人改参数实时作用到跟单员在跑任务）。这意味着跟单员一旦接入，其在跑任务的行为会被**远程改变**。没有审核闸门时，任何人都能把自己节点挂在别人的机器人上被远程改参数。

### 1.3 目标

新增【我的】→【AI交易机器人频道】页面，把「加入 AI 跟单」改造为**机器人频道审核制**，并给出频道的门面（等级、认证、运行状态、介绍、统计指标）。

### 1.4 非目标

- 不改 AI 信号分发链路的任何逻辑（`group_dispatcher` / `group_rules` / AI 热推 M2 全部零改动）。
- 不改「AI 跟单员不控手数」这一既定产品决策。
- 不做机器人之间的竞争排名 / 收益分成。
- 不做普通带单市场的审核（见姊妹篇《我的带单频道技术方案.md》）。

---

## 2. 原型解读

原型页 `AI交易机器人频道` 的完整结构（已通过浏览器实测 DOM 快照确认）：

### 2.1 频道资料卡（Hero）

```
smart_toy  AI交易机器人频道  auto_awesome
机器人专属 · 按机器人分组管理申请审核
─────────────────────────────────────────────
psychology  AI 策略引擎  Lv.6 智能交易频道  [策略在线]
ETH · BTC · 网格策略 · AI 智能 · 已运行 320 天 · 累计收益 +268,420.50 U
─────────────────────────────────────────────
频道介绍
聚合 AI 智能交易机器人，支持现货网格、趋势追击与一单一结等策略。
用户申请接入后，由频道审核并同步策略信号，按机器人分组集中处理。
─────────────────────────────────────────────
task_alt 已审核申请 30 │ hourglass_top 待审核申请 10 │ smart_toy 运行中机器人 2
```

> 注：Hero 卡片标题是「AI 策略引擎」（频道主人身份），副标题是频道名「Lv.6 智能交易频道」。
> 这与带单频道页的「黄金猎人 Lv.5 金牌带单员」是**同一套版式**（主标题 + 等级 + 状态胶囊）。

### 2.2 申请列表区

- 分段切换：`待审核 10` / `已审核 30` / `全部通过`
- 搜索框：`搜索机器人名称 / 申请人 / ID / 品种`
- **一级分组 = 机器人**（不是一个申请人！）：

```
猎金豹                       成员 20 人   5 个待审
ID：RB 8000125                          [done_all 本组通过]  [⌄ 折叠]
  ├ 李投资   猎金豹   ID: RB 8000125   [待审核]  [✕ 拒绝该申请]  [✓ 通过]
  ├ 王趋势   猎金豹   ID: RB 8000125   [待审核]  [✕ 拒绝该申请]  [✓ 通过]
  ├ 陈稳健   猎金豹   ID: RB 8000125   [待审核]  [✕ 拒绝该申请]  [✓ 通过]
  ├ 赵波段   猎金豹   ID: RB 8000125   [待审核]  [✕ 拒绝该申请]  [✓ 通过]
  └ 钱复利   猎金豹   ID: RB 8000125   [待审核]  [✕ 拒绝该申请]  [✓ 通过]

一单一结                     成员 20 人   5 个待审
ID：RB 8000113                          [done_all 本组通过]  [⌄ 折叠]
  └ 李投资 / 王趋势 / 陈稳健 / 赵波段 / 钱复利 … 同上
```

### 2.3 页面入口

【我的】枢纽页中的卡片：**AI交易机器人频道 · 机器人申请审核 · 频道管理**（与「我的带单频道」并列）。

---

## 3. 与带单频道的差异分析（关键）

这是本方案最重要的一节。两个页面看起来是同一个模子，但**审批的层级维度完全相反**。

| 维度         | 【我的带单频道】                              | 【AI交易机器人频道】                              | 影响                                       |
| ------------ | --------------------------------------------- | ------------------------------------------------ | ------------------------------------------ |
| **一级分组** | **跟单员（申请人）**                          | **机器人（策略）**                               | 聚合维度、API 的 group_by 键都不同          |
| **二级条目** | 节点（一个申请人有多个节点，可展开）          | 申请人（一个申请人一条接入申请）                 | 审批粒度不同                               |
| **「本组通过」作用域** | 该**申请人**在本分组下的全部节点              | 该**机器人**下的全部申请人                      | 批量接口语义完全不同，不能共用参数           |
| **申请对象** | 用户 → 节点（挂到某个带单分组）               | 用户 → 机器人（接入某个机器人）                 | 带单是"挂到分组"，AI 是"接入机器人"          |
| **审批主体** | 分组 owner                                    | 频道主人（= 机器人主人）                        | AI 侧所有机器人的审批人都可能是同一人        |
| **拒绝文案** | `删除该节点`                                  | `拒绝该申请`                                    | 语义不同：AI 拒绝的是"接入申请"而非"某个节点" |
| **统计维度** | 已审核**节点** 42 / 待审核**节点** 26 / 跟单中**节点** 38 | 已审核**申请** 30 / 待审核**申请** 10 / 运行中**机器人** 2 | AI 侧一个申请 = 一个节点 + 一个机器人的组合 |
| **数据源**   | `group_follower_node` + 节点                  | `group_follower_node` + tpl_1 分组 + 策略        | AI 侧多一层「机器人」聚合                    |
| **风险参数** | 无（普通带单一单一结）                        | 必须展示 `max_allow_num` / `total_lot_limit`    | 审批人必须看到风险量级才能判断               |

### 3.1 直接结论

**两个页面不能共用同一个列表接口，也不能共用同一个「本组通过」实现**。但它们**可以共用**：

- 底层申请表（加 `market` 维度列），
- 审批写穿的核心逻辑（写 `group_follower_node` + 刷新缓存 + 审计），
- 级联清理函数族，
- 频道资料表（加 `market` 维度列），
- 前端页面的视觉骨架与交互模式。

因此本方案采用 **「一套底层机制 + 两个市场维度」** 的架构，而不是复制粘贴两套代码。

---

## 4. 核心概念对齐：「机器人」到底是什么

这是落地前必须钉死的定义，否则后面全是歧义。

### 4.1 现有系统里「机器人」是什么

`backend/app/models.py` 的 `AiRobotSummary` 注释写得很明确：

> 展示单位 = 主节点：一位带单员的主节点即一个机器人，其名下绑定 `tpl_1` 且已开放 AI 跟单的分组挂在它下面。

也就是说，**现有系统的「机器人」= 一个主人（`leader_user_id`）**，其名下有 N 个 tpl_1 分组（= N 个策略）。

### 4.2 原型里的「机器人」是什么

原型中一个频道下有多个机器人条目（`猎金豹` RB 8000125、`一单一结` RB 8000113），每个有独立的「成员 20 人 / 5 个待审」。这些名称（猎金豹、一单一结）明显是**策略名**而不是用户名 —— 与 `AiRobotSummary` 里「策略名称才是机器人的展示标题」的既有约定一致。

**结论：原型里的「机器人」= 一个 tpl_1 分组（及其绑定的策略）**，而不是一个主人。

### 4.3 两种口径的对齐

| 层级     | 原型口径                     | 现有系统口径                    | 本方案采用                                              |
| -------- | ---------------------------- | ------------------------------- | ------------------------------------------------------- |
| 频道     | 频道（主人）                 | —                               | 新增实体 `leader_channel`（market=ai_follow）           |
| 机器人   | 一个 tpl_1 分组 / 策略       | 主人的全部 tpl_1 分组（合在一起） | **一个 tpl_1 分组**（= 一个机器人）                      |
| 成员     | 该机器人的跟单节点（20 人）  | 跟单节点总数                    | 该分组下 `group_follower_node` 的去重节点数             |
| 申请人   | 申请接入该机器人的用户       | —                               | `follow_request` 行（market=ai_follow）                 |

### 4.4 这个改动带来的影响（必须正视）

**现有【AI 交易】广场页**（`/ai-trade` 左侧列表）的「机器人」卡片是**按主人聚合**的：一个主人一张卡片，卡片上 `ai_group_count = 3` 表示他有 3 个策略分组。

改成「机器人 = 分组」后有两种选择：

| 选项 | 做法                                                        | 优点                                   | 缺点                                                     |
| ---- | ----------------------------------------------------------- | -------------------------------------- | -------------------------------------------------------- |
| **A（推荐）** | **广场页保持按主人聚合不变**；仅【我的 AI 机器人频道】审批页按分组展开 | 零改动现有广场页与详情页；改动面小；用户认知不打架 | 同一频道页内「机器人」与广场页「机器人」粒度不同，需文案区分 |
| B              | 广场页也改成按分组拆分卡片                                    | 概念统一                              | 改动 `AiRobotSummary` / 广场页 / 详情页 / 热推 M2 关联，回归面大 |

**本方案采用 A**。审批页中一级分组显示的「机器人」卡片，文案明确为「机器人（策略）」，并同时展示所属主人，避免歧义。

> 若业务方希望全局统一为「机器人 = 策略」，则需追加一个独立 PR 处理广场页改造，已列入 §15 遗留问题。

---

## 5. 总体架构与设计取舍

### 5.1 取舍一：与带单频道共用底层表 vs 独立表

| 方案                                     | 做法                                                             | 评价                                                                 |
| ---------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------- |
| A. 独立两张表 `ai_follow_request` 等     | AI 侧独立建表、独立 CRUD                                          | 隔离彻底，但审批写穿 / 级联清理 / 频道资料要写两遍，两边逻辑漂移是必然 |
| **B. 同一张表加 `market` 维度列**（采纳） | `follow_request.market ∈ {follow, ai_follow}`                     | 审批写穿、级联清理、频道资料全部复用；隔离靠**严格的市场过滤 + 唯一约束含 market** |

**采纳 B**。理由：审批写穿的 90% 逻辑（事务、幂等、复核、刷新缓存、审计）完全同构；拆两套必然漂移，而漂移在「未审核就收到信号」这个风险上代价极高。

**隔离强度**：每个读写接口的 WHERE 条件**必须**同时带 `market` 与 `leader_user_id`，且这两个条件由统一的 `follow_request_repo` 层拼装，不允许业务代码自己拼 SQL。

### 5.2 取舍二：待审核与生效关系物理分离（同带单频道，**不做二次论证**）

- `follow_request` 存申请单（带 `status` 生命周期）。
- `group_follower_node` **只**存生效关系。
- 审批通过时「写穿」写入 `group_follower_node`。

好处：`group_dispatcher` → `group_rules.participant_ids` → `effective_node_ids` 链路**零改动**，未审核节点在物理上不存在于跟单关系表，从根本上杜绝「未审核就收到 AI 加仓信号」。

### 5.3 取舍三：审批粒度 = 单申请单（申请人 × 机器人）

原型的「通过 / 拒绝该申请」是**申请单级**的（不是节点级）。本方案尊重原型：

- **申请单** = (机器人, 申请人, 该申请人提交的节点集合)
- 「通过」= 该申请单内**全部节点**一次性通过（AI 侧节点数通常少，因为一个机器人对应一个策略）
- 「拒绝该申请」= 整个申请单被拒，**该申请人在该机器人上被拒**
- 「本组通过」（机器人级）= 该机器人下全部 pending 申请单一次性通过

> 与带单频道的差异：带单频道的「通过」是**节点级**（一个申请人多节点，逐个通过），AI 频道的「通过」是**申请单级**。这是原型决定的，已在 §3 记录。

### 5.4 模块划分

```
后端
├── orm.py                       ~ FollowRequest 加 market 列；LeaderChannel 加 market 列
│                                 + 新增 AiRobotProfile?（否，见下）
├── db.py                        ~ _migrate_follow_request_table 补 market 列与索引
├── follow_service.py            ~ 申请单 CRUD（带 market 参数）+ 审批写穿 + 级联清理
├── follow_channel_rules.py      (新) 等级/统计口径/文案（带单与 AI 共用，market 参数化）
├── ai_channel_api.py            (新) /api/ai-channel/** 带单员侧接口
├── ai_follow_api.py             ~ /join 改为建申请；/me 增补「审核中」
├── models.py                    + AI 侧请求/响应模型
└── permissions.py               ~ can_host_ai_robot 已存在，直接复用

前端
├── views/MyAiChannelView.vue    (新) AI 机器人频道页
├── views/ProfileView.vue        ~ 增「AI交易机器人频道」卡片
├── views/AiTradeView.vue        ~ 跟单员侧展示「审核中」/「重新申请」
├── api/aiChannel.ts             (新) 接口封装
├── api/types.ts                 + 类型定义
└── router/index.ts              + /my-ai-channel 路由
```

---

## 6. 数据模型设计

### 6.1 `follow_request` — 跟单申请表（两市场共用，加 market 维度）

> **对姊妹篇《我的带单频道技术方案.md》§5.1 的修订**：新增 `market` 列。

```python
class FollowRequest(Base):
    """跟单申请：用户向带单员 / 机器人频道提交的、等待审核的关系申请。

    两市场（带单 / AI 交易）共用本表，靠 `market` 维度隔离。
    与 GroupFollowerNode 严格分离：
    - FollowRequest     = 待审核 / 已审批的**申请单**（带 status 生命周期）
    - GroupFollowerNode = 已生效的跟单关系（信号分发的唯一数据源）

    分离的意义：未审核的节点在物理上不存在于跟单关系表中，
    分发链路（group_dispatcher -> group_rules.participant_ids）
    不需要任何过滤条件，从根本上杜绝「未审核就收到信号」。
    """
    __tablename__ = "follow_request"
    __table_args__ = (
        # 同一 (市场, 分组, 节点) 至多一条 pending 申请
        Index("ix_follow_request_pending_unique",
              "market", "group_id", "node_id", unique=True,
              sqlite_where=text("status = 'pending'"),
              mysql_where=text("status = 'pending'")),
        Index("ix_follow_request_leader_status", "market", "leader_user_id", "status"),
        Index("ix_follow_request_group_status", "market", "group_id", "status"),
    )

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    # follow = 普通带单市场；ai_follow = AI 交易市场
    market: Mapped[str] = mapped_column(String(16), default="follow", index=True)
    # 目标分组 / 机器人（AI 侧一个 tpl_1 分组即一个机器人）
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    # 申请人（跟单员）用户 ID
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)
    # 频道主人（带单员 / 机器人主人）用户 ID
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # pending / approved / rejected / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 申请人自述
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 申请时的节点标签快照（模式 / 品种），供列表直接展示
    node_tags_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # AI 侧特有：申请时机器人的风险参数快照（审批人看风险量级用）
    risk_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # AI 侧特有：申请时策略配置版本（M2 热推的版本门参照）
    strategy_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    reviewer_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

### 6.2 申请单的聚合键（AI 侧）

一个「申请单」在 AI 侧是 **(group_id, follower_user_id)** 的聚合，物理上仍是每节点一行。

- 申请时：为该申请人在该机器人下提交的每个节点各插一行，`status='pending'`，共享 `follower_user_id` + `group_id`。
- 审批时：**按 `(group_id, follower_user_id)` 整组操作**（`approve` = 该组全部 pending 行一起过）。
- 列表聚合：SQL 层 `GROUP BY group_id, follower_user_id`，取 `MIN(id)` 作申请单 ID。

> 这样既尊重原型的「申请单级」交互，又保持与带单频道**节点级**审批共表、共写穿路径。

### 6.3 `leader_channel` — 频道资料（两市场共用，加 market 维度）

> **对姊妹篇 §5.2 的修订**：新增 `market` 列，主键改为 `(leader_user_id, market)` 复合。

```python
class LeaderChannel(Base):
    """带单 / AI 频道资料：频道主人对外的门面（简介 / 标签 / 认证）。

    统计类字段（等级、运行天数、累计收益、成员数）**不入库**，一律实时计算，
    避免统计口径与实际数据漂移。
    """
    __tablename__ = "leader_channel"
    __table_args__ = (UniqueConstraint("leader_user_id", "market", name="uq_leader_channel"),)

    leader_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # follow = 带单频道；ai_follow = AI 机器人频道
    market: Mapped[str] = mapped_column(String(16), default="follow", primary_key=True)
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intro: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    certified: Mapped[bool] = mapped_column(Boolean, default=False)
    certified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    certified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accent: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # AI 侧特有：频道运行状态（策略在线 / 维护中 / 暂停）
    channel_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
```

### 6.4 派生字段计算口径（一律后端算）

| 派生字段       | 口径                                                                                                  | 带单频道对照              |
| -------------- | ----------------------------------------------------------------------------------------------------- | ------------------------- |
| 运行天数       | `now - min(node_group.created_at where owner_user_id = leader)`（频道起点取最早分组）                    | 同                        |
| 累计收益       | `deal_stats.leader_batch_multi([primary_node_id], from_ts=None).total_profit`（**主节点口径**）          | 同                        |
| **运行中机器人** | 该主人名下 `tpl_1` 且 `accept_ai_follow=true` 且 `enabled=true` 的**分组数**                            | 带单侧为「跟单中节点数」  |
| **已审核申请**  | `count(distinct (group_id, follower_user_id)) where status='approved'`                                   | 带单侧为「已审核节点数」  |
| **待审核申请**  | `count(distinct (group_id, follower_user_id)) where status='pending'`                                    | 带单侧为「待审核节点数」  |
| **成员数**      | 该分组下 `group_follower_node` 去重节点数（每个机器人分组单独统计）                                       | 无                        |
| 聚焦品种       | 该主人名下启用分组所绑策略的 `symbol` 去重集合                                                          | 同                        |
| 状态胶囊       | `策略在线` / `维护中` / `已暂停`：由是否所有 tpl_1 分组启用且开放 AI 跟单决定                            | 带单侧为「已认证」        |

> **重要口径差异**：带单频道统计的是**节点数**，AI 频道统计的是**申请单数**（= 申请人与机器人的组合数）。
> 这不是笔误，源于原型两个页面的统计项语义不同（§3 表格最后一行）。

### 6.5 AI 频道等级规则（v1）

原型显示 `Lv.6 智能交易频道`，称谓与带单频道不同：

| 等级 | 条件（满足其一）                  | 称谓         |
| ---- | --------------------------------- | ------------ |
| Lv.1 | 默认                              | 智能交易频道 |
| Lv.2 | 运行中机器人 ≥ 1                  | 智能交易频道 |
| Lv.3 | 成员节点总数 ≥ 20                 | 优选频道     |
| Lv.4 | 成员节点总数 ≥ 50 且 运行天数 ≥ 60  | 认证频道     |
| Lv.5 | 成员节点总数 ≥ 100 且 运行天数 ≥ 180 | 专业频道     |
| Lv.6 | 成员节点总数 ≥ 200 且 运行天数 ≥ 365 | 旗舰频道     |

规则集中在 `follow_channel_rules.py`，**带单与 AI 两套阈值分开定义但同一份实现**（market 参数化）。

---

## 7. 审核生命周期状态机

### 7.1 状态转移（与带单频道同构）

```
                  跟单员提交申请
                        │
                        ▼
                  ┌───────────┐
    重复提交 ────▶ │  pending  │ ◀──── 重新提交（rejected → pending，新建行）
    (拒绝)         └───────────┘
                        │
     ┌──────────────────┼──────────────────┐
     │ 频道主人通过      │ 频道主人拒绝       │ 级联清理
     │ (申请单级/机器人级)│ (拒绝该申请)      │ (节点/分组/主人消失)
     ▼                  ▼                  ▼
  ┌──────────┐     ┌──────────┐      ┌───────────┐
  │ approved │     │ rejected │      │ cancelled │ ← 终态，界面不可见
  └──────────┘     └──────────┘      └───────────┘
     │
     │ 写穿：add_following → group_follower_node
     │ 复核：accept_ai_follow / tpl_1 / 主人双能力 / 节点可跟
     ▼
  跟单关系生效，开始接收 AI 加仓信号
```

### 7.2 AI 侧「重新提交」的规则

| 情形                                            | 结果                                                       |
| ----------------------------------------------- | ---------------------------------------------------------- |
| 该申请人曾被该机器人拒绝过，现在再申请          | **允许**（新建 pending 行，旧行保留 `reject_reason`）      |
| 该申请人已有该机器人的 pending 申请              | 409「该申请审核中」                                        |
| 该申请人已有该机器人的**生效**跟单关系            | 409「已接入该机器人」                                      |
| 该申请人在**其他**机器人被拒过                    | 不影响本机器人（拒绝按 `(group_id, follower_user_id)` 维度） |

> 与带单频道一致：拒绝是**软性的**，不做永久黑名单。带单员在列表上能看到该申请人的累计申请/拒绝次数作为参考。

### 7.3 幂等与并发

| 场景                       | 处理                                                                    |
| -------------------------- | ----------------------------------------------------------------------- |
| 重复点「通过」              | 已是 `approved` → 幂等 200，不重复写关系                               |
| 「本组通过」与单条「通过」重叠 | 事务内逐条检查 `status`，已 `approved` 的计入 `skipped`                 |
| 两个主人页面同时审批         | `SELECT ... FOR UPDATE` 锁申请行；SQLite 走写锁                        |
| 申请已被级联取消             | 404「申请不存在或已失效」                                                |

---

## 8. 后端 API 设计

新文件：`backend/app/ai_channel_api.py`，前缀 `/api/ai-channel`。
权限：接口需 `MENU_AI_TRADE` 菜单；写操作额外需 `can_host_ai_robot`（`ai_strategy` + `follow_leader`），管理员放行。

### 8.1 跟单员侧：提交申请

```
POST /api/ai-follow/join      （改造既有接口，语义变为「提交 AI 接入申请」）
```

请求体沿用 `AiJoinRequest`（`leader_user_id` / `group_ids` / `node_ids`），新增可选 `note`。

响应新增 `pending` 段，`joined` 保留但 v1 起恒为空：

```jsonc
{
  "pending": [
    { "group_id": "g_xxx", "group_name": "一单一结", "robot_name": "一单一结",
      "node_ids": ["n_1"], "follower_count": 5 }
  ],
  "joined": [],
  "skipped": [
    { "node_id": "n_2", "group_id": "g_xxx", "reason": "该申请审核中" },
    { "node_id": "n_3", "group_id": "g_xxx", "reason": "已接入该机器人" }
  ],
  "leader_primary_node_id": "n_leader"
}
```

**校验顺序**（沿用现有 `join_ai_follow` 顺序，新增审核态判断）：

1. 机器人主人存在且启用 → 否则 404。
2. 主人具备 `ai_strategy` + `follow_leader` → 否则 404。
3. 主人未暂停 AI 跟单（`ai_follow_paused`）→ 否则 400 `PAUSED_AI_REASON`。
4. 节点属于当前用户、启用、非主节点 → 否则进 `skipped`。
5. 分组：`tpl_1` 模版 + `accept_ai_follow=true` + 含主人主节点 + 启用 + 策略启用 → 否则静默跳过。
6. **(新)** 该 `(group, node)` 已有生效关系 → skip「已接入该机器人」。
7. **(新)** 该 `(group, follower)` 已有 pending → skip「该申请审核中」。
8. 通过则插入 `follow_request` 行（`market='ai_follow'`, `status='pending'`），并快照风险参数与策略配置版本。

### 8.2 频道主人侧：申请列表（**按机器人分组**）

```
GET /api/ai-channel/requests?group_id=&status=&q=&page=&page_size=
```

`status`：`pending`（默认）/ `approved` / `rejected` / `all`。

响应：

```jsonc
{
  "items": [
    {
      "robot": {
        "group_id": "g_xxx",
        "robot_name": "猎金豹",
        "strategy_id": "s_xxx",
        "strategy_name": "猎金豹",
        "symbol": "BTCUSDT",
        "leader_user_id": 7,
        "leader_name": "AI 策略引擎",
        "member_count": 20,
        "enabled": true,
        "accept_ai_follow": true,
        "pending_count": 5
      },
      "requests": [
        {
          "request_id": 1024,             // = MIN(id) of 该 (group, follower) 组
          "status": "pending",
          "follower": {
            "user_id": 42, "username": "alice", "display_name": "李投资",
            "applied_count": 2, "rejected_count": 1,
            "node_count": 2,
            "nodes": [
              { "node_id": "n_1", "node_name": "主账户", "node_login": "7000125",
                "tags": [ {"label": "BTCUSDT", "kind": "symbol"} ],
                "status": "pending", "created_at": 1762000000.0 }
            ],
            "created_at": 1762000000.0
          },
          # AI 特有：申请时该机器人的风险量级（审批人据此判断）
          "risk_snapshot": {
            "max_allow_num": 5, "total_lot_limit": 0.5, "lot_times": 3
          }
        }
      ]
    }
  ],
  "total": 10,
  "summary": {
    "pending_requests": 10,
    "approved_requests": 30,
    "running_robots": 2
  }
}
```

- 聚合层级：**一级 = 机器人（分组）**，二级 = 申请人申请单。
- `q` 模糊匹配：机器人名称 / 策略名 / 申请人 `username` / `display_name` / 节点名 / MT5 号 / 品种。
- 分页：`page=1, page_size=20`，上限 100。

### 8.3 频道主人侧：频道资料

```
GET  /api/ai-channel/profile
PUT  /api/ai-channel/profile
```

`GET` 响应：

```jsonc
{
  "profile": {
    "leader_user_id": 7,
    "username": "engine",
    "display_name": "AI 策略引擎",
    "title": "智能交易频道",
    "intro": "聚合 AI 智能交易机器人，支持现货网格、趋势追击与一单一结等策略。",
    "tags": ["ETH", "BTC", "网格策略", "AI 智能"],
    "accent": "#a855f7",
    "certified": true,
    "level": 6,
    "level_name": "旗舰频道",
    "channel_status": "online",          // online / maintenance / paused
    "running_days": 320,
    "focus_symbols": ["ETH", "BTC"],
    "total_profit": 268420.50,
    "stats": { "approved_requests": 30, "pending_requests": 10, "running_robots": 2 },
    "member_node_count": 42
  }
}
```

`PUT` 请求：`{ "title", "intro", "tags", "accent" }`（`certified` 只能由管理员接口授予）。

### 8.4 审批接口

```
POST /api/ai-channel/requests/{request_id}/approve    # 申请单级（原型语义）
POST /api/ai-channel/requests/{request_id}/reject
POST /api/ai-channel/requests/approve-batch            # 机器人级「本组通过」
```

`approve` 请求体（可选）：`{ "note": "欢迎接入" }`

```jsonc
{
  "request_id": 1024,
  "status": "approved",
  "approved": [ { "node_id": "n_1", "created": true } ],
  "skipped":  [ { "node_id": "n_2", "reason": "已接入该机器人" } ]
}
```

`reject` 请求体：`{ "reason": "节点未通过风控检查" }` → 该 `(group, follower)` 下全部 pending 行置 `rejected`。

**批量审批**（机器人级，对应原型「本组通过」）：

```jsonc
// POST /api/ai-channel/requests/approve-batch
{
  "group_id": "g_xxx",              // 必填：机器人
  "follower_user_id": 42,           // 可选：只批该申请人；缺省 = 该机器人下全部 pending
  "request_ids": [1024, 1025]       // 可选：精确指定；缺省 = 按上面两条推导
}
```

```jsonc
{
  "approved": [ { "request_id": 1024, "follower_user_id": 42, "node_ids": ["n_1"], "created": true } ],
  "skipped":  [ { "request_id": 1025, "reason": "已接入该机器人" } ],
  "summary":  { "approved_count": 5, "skipped_count": 1 }
}
```

> **与带单频道的差异**：带单频道的「本组通过」作用域是**申请人**（该用户在本分组下的全部节点）；AI 频道的「本组通过」作用域是**机器人**（该机器人下全部申请人）。因此本接口以 `group_id` 为必填主参数，而带单频道版以 `follower_user_id` 为主参数。**两者的参数含义不同，不可互相调用。**

### 8.5 频道主人侧：移除已生效的接入

沿用既有 `POST /api/ai-follow/leave`。本轮增强：

- 删除生效关系的同时，把该 `(group, follower)` 最近一批 `approved` 申请置 `rejected`，`reject_reason='频道已移除该接入'`。
- 允许对方**重新申请**（与 §7.2 一致）。

### 8.6 跟单员侧：我的申请状态

既有 `GET /api/ai-follow/me` 增补 `pending` 段：

```jsonc
{
  "following": [ /* 生效关系，按机器人主人聚合，不变 */ ],
  "pending": [
    {
      "leader_user_id": 7,
      "leader_name": "AI 策略引擎",
      "group_id": "g_xxx",
      "robot_name": "一单一结",
      "node_ids": ["n_1"],
      "status": "pending",
      "created_at": 1762000000.0,
      "last_reject_reason": "节点未通过风控检查"
    }
  ],
  "summary": { "following_count": 2, "follow_node_count": 5, "pending_count": 1 }
}
```

### 8.7 错误码

| HTTP | 场景                                       | detail 文案                       |
| ---- | ------------------------------------------ | --------------------------------- |
| 400  | 主人已暂停 AI 跟单                         | 该机器人主人已暂停 AI 跟单，不再接收新的跟单 |
| 400  | 没有可申请的节点                           | 没有可用于跟单的节点（主节点不可跟单） |
| 403  | 非本人且非管理员                           | 只能审批自己的频道申请            |
| 404  | 申请不存在 / 已失效 / 不属于该频道          | 申请不存在或已失效                |
| 404  | 主人不存在 / 未开放 AI 跟单 / 无主节点       | 该用户暂未开放 AI 跟单            |
| 409  | 重复提交 pending 申请                       | 该申请审核中                      |
| 409  | 已存在生效接入关系                          | 已接入该机器人                    |

---

## 9. 审批写穿与 AI 分发安全

### 9.1 审批写穿顺序（**唯一写入口**，`follow_service.approve_requests`）

```
1. 事务开启
2. 锁定并复核申请行状态（FOR UPDATE），非 pending → 幂等返回
3. 复核市场与分组（AI 侧特有）：
     - request.market == 'ai_follow'
     - 分组存在、enabled
     - 分组 owner_user_id == leader_user_id
     - 分组绑定的策略存在且启用
     - 策略 template_id == 'tpl_1'（AI 市场）
     - 分组 accept_ai_follow == true          ← AI 侧独有的硬门槛
     - 主人仍持有 ai_strategy 且 follow_leader
     - 主人未暂停 AI 跟单
4. 复核节点仍然可跟：
     - 节点存在、enabled
     - 节点 owner_user_id == follower_user_id
     - 节点不是该用户的主节点
     - 节点未在其它分组重复占用
5. 写 group_follower_node（唯一约束 uq_follow_group_node 防重）
6. 更新 follow_request.status='approved' + reviewer + reviewed_at
7. 提交事务
8. 事务外刷新分组缓存（_refresh_groups）
9. 写审计日志（category='ai_follow'）
```

> **第 3 步是 AI 侧最关键的一步**：`accept_ai_follow` 可能在申请提交之后被主人关闭。若不复核，会把用户接入到一个已经关闭对外接单的机器人上。

### 9.2 AI 分发安全的三重保证

| 保证       | 说明                                                                                                          |
| ---------- | ------------------------------------------------------------------------------------------------------------- |
| ① 数据隔离 | 待审核节点不在 `group_follower_node` 中，`group_rules.participant_ids` 取不到它们                                |
| ② 缓存一致 | 写穿成功后立即 `_refresh_groups`；写前不刷新、未生效不进缓存                                                    |
| ③ 二次校验 | `group_rules.group_still_leads` 在分发时复核分组仍可带单                                                        |

**AI 特有的一条**：分组 `accept_ai_follow` 关闭后，**存量**跟单关系的处理沿用既有语义（`ai_follow_api` 里已有 `reason = "该机器人已关闭 AI 跟单，不再接收新信号"` 的置灰逻辑），本方案不改动。

### 9.3 M2 热推与审批的关系（重要澄清）

`AiTradeView.vue` 记载：AI 智能加仓策略支持**热推（M2）** —— 机器人主人改参数会**实时**作用到跟单员在跑任务。

这与审核制的关系：

- **热推是策略参数的下发，不是跟单关系的建立**。热推走 `strategy_config_version` 版本门（迟到的旧版本包直接丢弃），作用对象是**已生效**的跟单节点。
- 审核制**不改变**热推行为：一旦审核通过，主人改参数仍会热推给对方 —— 这是「AI 跟单员不控手数」这一产品决策的必然结果，已在 §1.2 说明。
- 因此 `follow_request.risk_snapshot_json` 与 `strategy_config_version` **只用于审批人查看与留痕**，**不用于运行时的风险判定**（运行时一律现算，避免快照过期导致风控失效）。

> 这一点必须在产品层面明确告知跟单员：接入 AI 机器人 = 授权对方远程改变你的加仓参数。UI 上建议在接入确认弹窗中显式提示。

---

## 10. 级联清理与生命周期联动

| 触发事件                                     | 对 `follow_request`（market=ai_follow）的处理                     | 对 `group_follower_node` 的处理              |
| -------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------- |
| 节点被删除 / 归属转移 / 升为主节点             | 该节点 pending → `cancelled`                                      | `remove_node_followings`                    |
| **机器人（tpl_1 分组）被删除**                 | 该分组 pending → `cancelled`                                       | 级联删除跟单关系                             |
| 机器人分组被停用 / 策略被停用                  | pending **保留**，审批时第 3 步复核拒绝并置 rejected                 | 不动                                        |
| **`accept_ai_follow` 关闭**                   | pending **保留**，审批时第 3 步复核拒绝并置 rejected（reason=已关闭 AI 跟单） | 不动（存量关系按既有语义置灰） |
| 跟单员（申请人）被删除 / 停用                   | 其 pending → `cancelled`                                           | `remove_user_followings`                    |
| 主人撤销 `ai_strategy` / `follow_leader`      | 其名下 pending → `cancelled`（reason=机器人资格已取消）             | **不删除已有生效关系**（沿用现有语义）        |
| 主人删除已生效的接入                           | 对应 approved → `rejected`（reason=频道已移除该接入）                | `remove_following`                          |
| 主人拒绝申请                                  | `(group, follower)` 下 pending → `rejected`                          | 不产生关系                                   |

**实现**：`follow_service` 新增（均带 `market` 参数）：

```
cancel_requests_for_node(node_id, market)
cancel_requests_for_group(group_id, market)
cancel_requests_for_follower(follower_user_id, market)
cancel_requests_for_leader(leader_user_id, market)
```

接入点：`node_service`（删节点 / 改归属 / 设主节点）、`group_service`（删分组）、`user_service`（删用户 / 停用）、`rbac_service`（撤销能力）。

> 项目**无外键**（且需要「关系删除但申请保留历史」的语义），级联清理必须**显式调用**，不能靠 DB 级联。

---

## 11. 前端设计与实现

### 11.1 新页面 `MyAiChannelView.vue`

路径：`frontend/src/views/MyAiChannelView.vue`
路由：`/my-ai-channel`，`meta: { menu: 'ai_trade' }`（无 `ai_strategy` + `follow_leader` 双能力时展示空态「你还不是机器人频道主人」）。

**布局（PC）**

```
┌────────────────────────────────────────────────────────────┐
│ ← 返回        AI交易机器人频道                                │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ (图标) AI 策略引擎   Lv.6 旗舰频道  [策略在线]           │ │  ← Hero 卡
│ │        ETH · BTC · 网格策略 · AI 智能 · 已运行 320 天      │ │
│ │        累计收益 +268,420.50 U                            │ │
│ │        频道介绍：聚合 AI 智能交易机器人……                 │ │
│ │  ┌────────────┬────────────┬────────────┐              │ │
│ │  │已审核申请 30│待审核申请10│运行中机器人2│              │ │
│ │  └────────────┴────────────┴────────────┘              │ │
│ └────────────────────────────────────────────────────────┘ │
│ [待审核 10] [已审核 30] [全部通过]      [🔍 搜索机器人/申请人/ID/品种] │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ ▼ 猎金豹   成员 20 人 · 5 个待审   ID: RB 8000125  [本组通过]│ │
│ │    ● 李投资  猎金豹  RB 8000125  [待审核] [拒绝该申请][通过]  │ │
│ │    ● 王趋势  猎金豹  RB 8000125  [待审核] [拒绝该申请][通过]  │ │
│ └────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

**与带单频道页的关键 UI 差异**：

1. **一级折叠单元是机器人**，卡片头显示「机器人名 + 成员数 + 待审数 + 机器人 ID（RB 前缀）」。
2. **「本组通过」在机器人行**，作用域 = 该机器人全部待审申请（带单频道在申请人行）。
3. **拒绝按钮文案为「拒绝该申请」**（带单为「删除该节点」）。
4. **每个申请行右侧展示风险量级**（`max_allow_num` / `total_lot_limit`），AI 侧必须可见。
5. 申请人行下可展开该申请的**节点明细**（`nodes`），供审批人逐节点判断（比带单频道多一层折叠）。

**H5（≤ 768px）**：沿用项目 `.m-only` / `.m-*` 分层约定：

- Hero 卡纵向堆叠，统计指标横向 3 格滚动。
- 机器人卡片折叠为手风琴；「本组通过」在卡片头右侧紧凑按钮。
- 申请人行改为两行卡片（上行：姓名 + 机器人名；下行：待审标签 + 风险量级 + 操作按钮）。
- 风险量级用小字标签，避免挤占操作区。

### 11.2 `ProfileView.vue` 接入

```ts
{
  code: 'my_ai_channel',
  label: 'AI交易机器人频道',
  description: '机器人申请审核 · 频道管理',
  path: '/my-ai-channel',
  icon: '<path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3v1a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3v1a3 3 0 0 0 6 0v-1a3 3 0 0 0 3-3v-1a3 3 0 0 0 0-6V9a3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/>',
  accent: '#a855f7',
}
```

`visibleModules` 过滤：`auth.isAdmin || (auth.hasCapability('ai_strategy') && auth.hasCapability('follow_leader'))`。

### 11.3 `AiTradeView.vue` 跟单员侧改造

- 「确认接入」成功后文案改为 **「申请已提交，等待频道审核」**，按钮置灰并显示「审核中」标签。
- 接入确认弹窗**必须显式提示授权条款**（§9.3）：

  > 接入后，机器人主人调整加仓参数会**实时同步**到你的节点；跟单期间你无法调整手数与加仓批次。

- 「我的跟单」新增分段：**「接入中」**（pending）/ **「已接入」**（生效）。
- 被拒时展示驳回原因 + 「重新申请」按钮。

### 11.4 新增 API 封装 `frontend/src/api/aiChannel.ts`

```ts
export async function fetchAiChannelProfile(): Promise<AiChannelProfile>
export async function updateAiChannelProfile(payload: Partial<AiChannelProfile>): Promise<AiChannelProfile>
export async function fetchAiChannelRequests(params: AiChannelRequestQuery): Promise<AiChannelRequestPage>
export async function approveAiRequest(requestId: number, note?: string): Promise<AiApproveResult>
export async function rejectAiRequest(requestId: number, reason: string): Promise<AiApproveResult>
export async function approveAiRequestBatch(payload: AiBatchApprovePayload): Promise<AiBatchApproveResult>
```

---

## 12. 数据库迁移与审计日志

### 12.1 迁移（沿用 `db.py` 的手工迁移模式）

项目**无 Alembic 版本文件**，统一在 `backend/app/db.py` 用 `_migrate_xxx(sync_conn)` + `inspect(...)` 判存在再执行 SQL。

```python
def _migrate_follow_request_table(sync_conn) -> None:
    """新建跟单申请表 + 补 market 维度列与索引。"""

def _migrate_leader_channel_table(sync_conn) -> None:
    """新建频道资料表 + 补 market 维度列。"""
```

要点：

- `follow_request` / `leader_channel` 都是**新表**，`Base.metadata.create_all` 已能创建；迁移脚本主要用于**补索引**与 **MySQL 5.7 兼容退化**（partial index）。
- 若姊妹篇已先上线，则此处为**增量 ALTER**：`ALTER TABLE follow_request ADD COLUMN market ...` 并重建索引。
- MySQL 5.7 不支持 partial index → 退化为应用层唯一性保证 + `unique(market, group_id, node_id, status)`，提交申请时在事务内 `SELECT ... WHERE market=? AND status='pending'` 后再插入。

### 12.2 审计日志

沿用 `persist.audit(username, action, target, payload, result, client_ip, category, before, after)`，`category='ai_follow'`：

| action                          | 关键 payload                                                        |
| ------------------------------- | ------------------------------------------------------------------- |
| `ai_follow_request_create`      | `{leader_user_id, group_ids, node_ids, request_ids}`                 |
| `ai_follow_request_approve`     | `{request_id, group_id, follower_user_id, node_ids, created}`        |
| `ai_follow_request_batch_approve` | `{group_id, follower_user_id?, approved[], skipped[]}`             |
| `ai_follow_request_reject`      | `{request_id, group_id, follower_user_id, reason}`                   |
| `ai_channel_update`             | `before/after` = 频道资料快照                                        |
| `ai_channel_certify`            | `{leader_user_id, certified}`（管理员授予认证）                       |

---

## 13. 分阶段实施计划（PR 拆分）

> 依赖关系：PR-A1 是姊妹篇《我的带单频道技术方案.md》的 PR-1。若姊妹篇已先行落地，本方案在此基础上做增量。

| 阶段 | PR    | 内容                                                                 | 依赖 |
| ---- | ----- | -------------------------------------------------------------------- | ---- |
| P1   | PR-A1 | **数据层**：`follow_request` / `leader_channel` 加 `market` 维度 + `db.py` 迁移 + `follow_service` 申请单 CRUD（market 参数化）+ 级联清理 + 单元测试 | 姊妹篇 PR-1（或独立先行） |
| P2   | PR-A2 | **频道主人侧 API**：`ai_channel_api.py`（profile / requests / approve / reject / approve-batch）+ 权限 + 审计 | PR-A1 |
| P3   | PR-A3 | **跟单员侧改造**：`POST /api/ai-follow/join` 改为建申请 + `GET /api/ai-follow/me` 增补 pending + 接入确认弹窗授权提示 | PR-A1、PR-A2 |
| P4   | PR-A4 | **前端**：`MyAiChannelView.vue` + `api/aiChannel.ts` + 路由 + `ProfileView` 卡片 | PR-A2 |
| P5   | PR-A5 | **跟单员侧前端**：`AiTradeView.vue` 展示「审核中」/「重新申请」/ 授权提示 + 单元测试 | PR-A3 |
| P6   | PR-A6 | **收尾**：级联清理接入既有生命周期调用点（node / group / user / 能力撤销）+ 全量回归 | PR-A1..A5 |

**上线顺序**：PR-A1 → PR-A2（主人侧先上，此时 join 仍是即时加入）→ PR-A3（切换为审核制）→ PR-A4 → PR-A5 → PR-A6。

> **灰度开关**：`system_setting` 增加 `ai_follow_require_approval`（默认 `1`）。关闭时 `join` 走旧的即时加入路径，便于紧急回滚。姊妹篇的 `follow_require_approval` 与之独立，互不影响。

---

## 14. 测试方案

### 14.1 单元测试（pytest）

- 申请单状态机：合法/非法转移；重复提交 409；驳回后可重新提交；已生效关系不可重复申请。
- **市场隔离**：带单频道的申请绝不出现在 AI 频道列表，反之亦然（`market` 过滤必须全覆盖）。
- 审批写穿：成功写关系 + 刷新缓存 + 审计。
- **AI 侧复核**：`accept_ai_follow=false` / 策略停用 / 主人撤销能力 / 主人暂停 / 分组停用 时**不写关系**并置 rejected。
- 批量审批：机器人级批量与申请单级单批的重叠不产生重复关系。
- 级联清理：节点 / 分组 / 用户 / 能力撤销后 pending 全部 cancelled。
- 统计口径：已审核**申请数**（去重 `(group_id, follower_user_id)`）≠ 已审核**节点数**；两者都要有断言。

### 14.2 集成测试（**最关键的一条**）

> **用例：未审核节点零分发（AI 市场）**
>
> 1. 跟单员对 tpl_1 分组 G 的节点 N 提交申请 → 断言 `group_follower_node` 中**无** N；
> 2. 断言 `store.get_group(G)` 的 `followers` 中**无** N；
> 3. 向 `/webhook` 注入 G 的 AI 加仓信号 → 断言 `group_task_dispatch` 中**无** N 的子任务；
> 4. 主人关闭 `accept_ai_follow` → 审批 → 断言**不写关系**、申请置 rejected；
> 5. 主人重开 `accept_ai_follow` → 审批 → 断言关系已写入、缓存已刷新；
> 6. 再次注入信号 → 断言 N **有**子任务。

### 14.3 端到端

- 主人审批 → 跟单员 `GET /api/ai-follow/me` 出现生效关系 → 跟单员收到 AI 加仓信号。
- 机器人级「本组通过」→ 该机器人下全部申请生效。
- 拒绝 → 跟单员看到原因 → 重新申请 → 主人列表再次出现。

### 14.4 前端

- `vue-tsc --noEmit`；`vitest` 覆盖机器人分组渲染、申请单折叠、风险量级展示。
- 手工验收：PC 1920 / 1366 / H5 375 三档断点。

### 14.5 质量门禁

后端 `pytest` + coverage（`ai_channel_*` / `follow_service` 申请单部分 ≥ 80%）；CI 全量通过方可合并。

---

## 15. 风险与遗留问题

| # | 风险 / 待定                                                       | 处置                                                                    |
| - | ----------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 1 | **「机器人」的口径**：原型按策略分组，广场页按主人聚合              | 本方案采用「广场页不变、审批页按分组」（选项 A）；若要全局统一需追加独立 PR |
| 2 | 「本组通过」作用域：机器人级 vs 申请人级                          | 按原型取**机器人级**；如需同时支持申请人级，加 `follower_user_id` 参数（本接口已预留） |
| 3 | 存量 AI 跟单关系切换为审核制后是否需要复审                         | v1 **不复审**（存量直接视为已通过）；业务方后续拍板                        |
| 4 | MySQL 5.7 无 partial index                                        | 落地前确认 MySQL 版本；5.7 走应用层唯一性 + 唯一索引退化方案            |
| 5 | **M2 热推的授权告知**                                              | 接入确认弹窗必须显式提示「主人改参数会实时作用到你」，见 §9.3             |
| 6 | 风险参数快照过期                                                    | `risk_snapshot` **仅供审批人查看**，运行时一律现算，不作风控依据        |
| 7 | 申请量爆炸时的列表性能                                            | 索引 `(market, leader_user_id, status)` + `(market, group_id, status)`；分页 |
| 8 | 与普通带单频道的代码共享导致串台                                    | 所有读写走统一 repo 层拼 `market` 条件，业务代码不得自拼 SQL；单测覆盖   |

---

## 16. 附录 A：原型字段 → 后端字段对照表

### 16.1 Hero 卡

| 原型字段                 | 后端来源                                                     |
| ------------------------ | ------------------------------------------------------------ |
| `AI 策略引擎`             | `users.display_name`（频道主人）                              |
| `Lv.6 智能交易频道`      | `leader_channel.title` + `follow_channel_rules.level_name`    |
| `策略在线`                | `leader_channel.channel_status='online'`（派生）               |
| `ETH · BTC`              | `focus_symbols`（派生自分组策略 symbol 去重）                   |
| `网格策略 · AI 智能`      | `leader_channel.tags_json`                                     |
| `已运行 320 天`          | `running_days`（派生）                                         |
| `累计收益 +268,420.50 U` | `total_profit`（`deal_stats` 主节点口径，派生）                  |
| `频道介绍`                | `leader_channel.intro`                                        |

### 16.2 统计项

| 原型字段      | 后端派生口径                                                        |
| ------------- | ------------------------------------------------------------------- |
| `已审核申请 30` | `count(distinct (group_id, follower_user_id)) where status='approved'` |
| `待审核申请 10` | `count(distinct (group_id, follower_user_id)) where status='pending'`  |
| `运行中机器人 2` | 该主人名下 `tpl_1` + `accept_ai_follow` + `enabled` 的分组数          |

### 16.3 申请列表

| 原型字段             | 后端来源                                                        |
| -------------------- | --------------------------------------------------------------- |
| `猎金豹`（机器人名）  | 分组绑定的策略名 `trading_strategy.name`                         |
| `ID：RB 8000125`     | `group_id`（展示时加 `RB ` 前缀并分组段）                        |
| `成员 20 人`         | 该分组 `group_follower_node` 去重节点数                            |
| `5 个待审`           | 该分组下 pending 申请单数（去重 follower）                        |
| `李投资`             | 申请人 `users.display_name`                                       |
| `待审核` 标签        | 申请单 `status`                                                   |
| `通过` / `拒绝该申请` | `POST /api/ai-channel/requests/{id}/approve` / `/reject`          |
| `本组通过`           | `POST /api/ai-channel/requests/approve-batch`（`group_id` 必填）   |
| `搜索 机器人名称/申请人/ID/品种` | `q` 参数，匹配机器人名 / 申请人 / group_id / node 名 / symbol   |

---

*（完）本方案在 AI 交易市场引入「申请—审核」流程，与《我的带单频道技术方案.md》共用
底层申请表与写穿机制，通过 `market` 维度严格隔离；AI 侧特有三处差异已显式标注：
「机器人 = tpl_1 分组」的层级口径、「本组通过 = 机器人级」的作用域、
以及 `accept_ai_follow` 审批时必须复核的硬门槛。*

## 17. 实施契约与升级验收

2026-11-10 实施版。以下规则及《技术实现方案》§18.64、§18.65 为当前行为依据。

### 17.1 业务流程

跟单人选择自有已开通节点（含主节点），提交申请；发单人按机器人、申请人整笔审核。审核通过后才创建生效跟单关系。新机器人继承成员同样产生待审申请。

存量 AI 跟单首次升级后转待复审，新信号停止下发，已有策略任务、持仓、账本、魔术号和 busy 保留。原暂停状态审核后仍保留。普通带单继续沿用原准入和即时跟单语义。

### 17.2 页面

- `/ai-trade`：按策略分组展示机器人；跟单弹窗含机器人说明、节点名称/账号搜索、在线状态、余额、主节点标识、多选、已跟随/审核中标记及真实删除入口。
- `/ai-trade/follow/:leaderUserId/:groupId`：选择节点后进入“参数风控 → 确认提交”，更换节点和上一步保留当前机器人本次配置。最后确认才调用申请接口，成功后进入待审核页签；申请人刷新页面需重新选择节点。
- “我的机器人”增加待审核页签。存量待复审及被拒绝的存量任务仍能查看持仓并使用既有平仓入口。
- `/my-ai-channel`：从“我的模块”进入，展示资料卡、等级、真实收益、待审/已审统计；按机器人折叠、按申请人合并展示节点明细。
- 本组通过处理该机器人全部待审申请；全部通过处理搜索命中的全部待审申请，跨分页获取固定申请 ID 集合，确认后逐笔审批。
- 已审核包括通过和拒绝。成员人数按申请人去重，审核计数按申请批次；同一申请人的多次获批合并展示，历史仍保存在申请记录和审计中。

### 17.3 权限

跟单仅需 `ai_trade` 菜单。频道需 `ai_trade` 与 `ai_strategy + follow_leader` 双能力；频道仅展示本人机器人的申请。后端按机器人当前归属、申请归属、节点当前归属重新校验。

主节点可参与 AI 跟单。普通带单仍禁止主节点；升主时合法 AI 关系保留，普通带单关系仍阻止升主，原主节点在跑任务仍阻止切换。

### 17.4 审核与暂停

- 申请主表保存稳定 ID、批次、主人、申请人、机器人/策略/主节点快照、审核信息；节点明细保存归属快照及原运行状态。
- 可空唯一 `pending_key=(group_id,follower_user_id)` 保证同一申请人、同一机器人最多一笔待审。历史申请的该键清空，允许拒绝后重新申请。
- 同一申请全部节点复核通过，审核状态、生效关系与成功审计同事务提交。任一节点失效则整笔不通过；重复通过不重复创建关系。
- 待审节点不进入 `group_follower_node`。审批后刷新分组缓存，重启按 MySQL 恢复。
- `follow_state` 是跟单人状态，`leader_paused` 是频道主人状态。两者分别持久化；任一方暂停即不接收 AI 新开仓，恢复不覆盖对方暂停。
- 节点转移/删除、机器人分组或策略失效、主人失去能力时取消失效申请。保留运行任务及历史数据，CLOSE/strategy_stop 沿用原语义。

### 17.5 接口

| 接口 | 契约 |
| --- | --- |
| `POST /api/ai-follow/join` | 保留 `leader_user_id/group_ids/node_ids`，可选 `settings={risk_mode,loss_ratio,run_mode}`；返回 `pending/skipped`，`joined=[]` |
| `GET /api/ai-follow/me` | `following` 保留生效、已停止及存量任务视图，节点和 `applications` 返回获批/待审设置 |
| `GET /api/ai-channel/profile` | 本人频道的派生资料和全局统计 |
| `GET /api/ai-channel/requests` | `status=pending/reviewed`、`q`、`page/page_size`；返回机器人分组及跨页待审 ID |
| `POST /api/ai-channel/requests/{id}/approve` | 整笔通过、幂等 |
| `POST /api/ai-channel/requests/{id}/reject` | 整笔拒绝，支持原因 |
| `POST /api/ai-channel/requests/approve-batch` | 固定 `request_ids` 集合；逐申请原子处理并返回失败明细 |
| `POST /api/ai-channel/members/state` | `group_id/follower_user_id?/paused`；仅改变频道一方暂停 |
| `POST /api/ai-channel/members/deletion-preview` | `group_id/follower_user_id?`；真实系统节点影响与阻塞明细 |
| `POST /api/ai-channel/members/delete` | 预览作用域、`node_ids/version`；复核后整组原子删除 |
| `GET /api/nodes/{id}/deletion-preview` | 管理员或当前所有者预览 |
| `DELETE /api/nodes/{id}` | 请求体必须包含预览 `version`，管理员或当前所有者可执行 |
| `POST /api/ai-follow/ui-events` | 白名单人工页面行为，操作者从 JWT 获取 |

### 17.6 数据与派生统计

#### 17.6.1 申请表

`ai_follow_application` + `ai_follow_application_node`，不改变普通带单申请语义。申请状态为 pending/approved/rejected/cancelled；节点失效会取消对应明细，待审申请任一节点失效时整笔取消。

#### 17.6.2 生效关系

继续使用 `group_follower_node`，新增 `leader_paused`（默认 false）。AI 纯规则叠加个人状态和频道状态过滤新开仓，普通带单不读取该 AI 暂停状态。

#### 17.6.3 删除记录

`deleted_node` 持久化旧节点 ID、MT5 登录号和删除操作者。旧全局令牌不能重新自动注册被删节点；管理员显式重新创建同一账号时必须走新的专属凭证。

#### 17.6.4 统计口径

运行天数取最早自有 AI 分组创建时间。累计收益限定自有 AI 分组在本人主节点上的魔术号。无成交数据返回 null。运行中机器人为在线且启用主节点所在、已启用且开放跟单的 AI 分组，账号级暂停时为零。

#### 17.6.5 等级阈值

成员节点数仅计当前已通过且未停止的去重节点；满足最高等级条件：

| 等级 | 条件 | 称谓 |
| --- | --- | --- |
| Lv.1 | 默认 | 智能交易频道 |
| Lv.2 | 运行中机器人至少 1 个 | 智能交易频道 |
| Lv.3 | 成员节点至少 20 个 | 优选频道 |
| Lv.4 | 成员节点至少 50 个且运行至少 60 天 | 认证频道 |
| Lv.5 | 成员节点至少 100 个且运行至少 180 天 | 专业频道 |
| Lv.6 | 成员节点至少 200 个且运行至少 365 天 | 旗舰频道 |

等级仅为规模派生称谓，不新增平台认证授权。

### 17.7 不可逆删除

频道单条按该申请人在该机器人下的节点集合删除；整组覆盖待审、已批准、已暂停及已拒绝申请所关联的系统节点。普通用户可删除自有节点；发单人额外获得其机器人申请关联节点的有限删除权限，节点归属已变更则拒绝。

删除会清理节点全部分组成员、两个市场的跟单关系、申请关联、专属凭证和 Redis 实时态，断开节点连接；机器人策略、交易任务历史、成交账本及操作审计保留。绝不自动平仓。

删除要求节点在线、账户账本可读、60 秒内更新且晚于最近交易回报，无任何持仓、挂单、在途 normal 指令、非终态策略任务或执行占位。任一目标不满足时整次拒绝，无部分删除。预览返回固定目标集合及版本，确认时重新计算；目标或绑定变化返回 409。

单实例内节点生命周期锁序列化新开仓准入、归属/主节点变更和删除。等待交易结果不持有该锁，CLOSE/平仓不被锁住。MySQL 事务保证批量节点及审计原子性，启动时按权威账本清理失效缓存。

### 17.8 上线与验证

应用启动、接受请求前执行 `ai_follow_review_v1` 一次性迁移；转换存量有效关系和完成标记同事务提交。新表及 `leader_paused` 列沿用现有建表/增量迁移方式。

上线前备份 MySQL，由用户停止旧单实例后端并启动新版本，避免新旧逻辑同时写关系表。确认存量待复审数量、在跑任务和审计记录，再由发单人审核。不得用旧版本回滚绕过审核；恢复采用备份或包含本版审核语义的前向修复。

验证覆盖申请/并发审核、暂停叠加、迁移重启、跨市场隔离、主节点、真实节点删除及凭证重连，并执行前端类型检查、构建和浏览器桌面/手机验收。

### 17.9 个人风控与一轮策略

“一轮策略”指一个节点的一笔策略子任务从启动到终态的完整周期，包含其首单（含成功挂单）、加仓及收尾。“单次”表示该节点成功启动的一轮收口后停止接收后续信号；下发或首单失败不消耗这一次机会。“循环”表示收口后继续参与后续信号。该选择与旧 `float_pl_ratio.monitor_mode` 不是同一配置。

`settings.risk_mode` 为 `leader/custom`，自定义 `loss_ratio` 必须大于 0 且不超过 100，按账户余额比例计算本信号共同止损；`run_mode` 为 `loop/single`。缺省为跟随带单、循环。手数完整跟随，不按风控百分比缩放。确认页告知策略实时同步，自定义比例独立保留。

申请节点的 `follow_settings_json` 在通过时写入生效关系，并带内部申请 ID；创建子任务时复制配置，后续热推和恢复使用子任务配置。`single_dispatch_id` 与子任务在同一事务内领取，禁止并发领取两轮；终态且未成功启动则释放领取，已成功启动则将关系置为 `stopped` 并同事务审计。重新申请获批会切换批次并重置领取，旧任务不能停止新批次。节点与频道暂停分别保留，恢复不能重启已完成的单次关系。

新增列通过启动增量迁移补齐，兼容旧表及重复执行；旧配置按缺省处理，不再次触发已完成的存量复审迁移。验收覆盖自定义风控下发、热推与重连保持、单次并发领取、下发及首单失败重试、单次完成进入历史及继承到新机器人仍待审。
