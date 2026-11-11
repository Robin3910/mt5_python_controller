# 财经日历黑名单 + 停止做单开关（设计方案）

> 本文档只设计、不写代码。
>
> **一句话定位**：在**个人中心**加一个「财经日历」板块，提供**高影响经济数据公布前后可配置时间窗内自动停止新开仓**的能力，外加一个随时可拉的**手动停止做单急停开关**。

参考实现：[Robin3910/xauusd_ai_trader_v2](https://github.com/Robin3910/xauusd_ai_trader_v2) 的 `economic_calendar.py`（ForexFactory 日历 → 交易黑名单时间窗）。

---

## 0. 已拍板的决策

| # | 决策 | 说明 |
|---|---|---|
| 1 | **只拦截高影响事件** | `impact == High` 的事件才产生黑名单；`Medium` / `Low` / `Holiday` 不拦 |
| 2 | **默认只关注 USD** | 用户可改，默认币种集合 = `["USD"]`；黄金（XAUUSD）归 USD |
| 3 | **只停「新开仓」** | 已在跑的任务不终止；已有持仓的止损 / 止盈 / 移动止损照常运行 |
| 4 | **窗口可配置，默认 30/30** | 事件前 N 分钟 + 事件后 N 分钟，默认各 30 分钟 |
| 5 | 判定放在**服务端分发层** | 复用既有 `dispatcher` / `group_dispatcher` 的拦截骨架，落 `skip_code` |
| 6 | 日历数据**全局共享** | 事件表全站一份；每个用户只存自己的开关与参数 |
| 7 | **财经日历独立成页** | 不挤在个人中心里；独立路由 `/news-calendar`，入口放在【我的】模块网格（不限菜单权限） |
| 8 | **顶部滚动提示条，可关闭** | 黑名单当天在页面最上方滚动警示；点 X 后**当天**不再显示，次日自动重新提示 |

---

## 1. 背景与现状

### 1.1 现状：系统里没有「新闻」这个概念

全仓库检索 `news` / `calendar` / `新闻` / `财经` —— **零匹配**。当前没有任何机制能在重大经济数据公布前后避开下单。

### 1.2 现状：已有的拦截范式（要复用，不要另造）

项目里已经有一套成熟的「拦截 → 记 `skip_code` → 不下发」骨架，散落在两处分发引擎：

| 位置 | 已有拦截 | 写入方式 |
|---|---|---|
| `backend/app/dispatcher.py` `_try_open_active` | 登录号不符 / 链路断连 / 区间过滤 / 持仓过滤 / 并发占用 | `self._skip(signal_id, node_id, reason, code)` |
| `backend/app/group_dispatcher.py` `_dispatch_node` | 并发占用 / 链路断连 / 趋势风控 | `group_persist.record_skipped_dispatch(..., skip_code=...)` |

`backend/app/signal_codes.py` 集中管理结构化编码，并明确约定：**「编码是数据，文字是展示」**，统计侧只按 `code` 聚合，永不解析中文。

> **本需求最正确、最省事的落点就是这里** —— 复用既有骨架，零新概念，统计天然可用。

### 1.3 现状：个人中心页的结构

`frontend/src/views/ProfileView.vue`（路由 `/profile`，**登录即可进，不挂 `meta.menu`**），当前区块顺序：

1. 账号头部（`pf-card profile-head`）
2. 个人信息（`pf-card`）
3. 带单员（`pf-card`，仅带单相关人可见）
4. 修改密码（`pf-card`，折叠）
5. 双因素认证 2FA（`pf-card`）

样式体系：全局 `styles.css` 里的 `.pf-card` / `.pf-section-head` / `.pf-section-title` / `.pf-list` / `.pf-input` / `.pf-btn` / `.pf-hint` / `.pf-error` / `.pf-count`，页面内 `<style scoped>` 只放本页布局。

> **本需求落点（已调整）**：最初设计是插一个 `pf-card` 到本页；
> 后按需求改为**独立页面** `/news-calendar`（见 §8.1），本页不再承载该板块。

---

## 2. 参考仓库评估：能抄什么、必须改什么

参考实现 `economic_calendar.py` 的核心逻辑：

```python
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_HIGH = "high"; IMPACT_MEDIUM = "medium"; IMPACT_LOW = "low"
# 事件 → BlackoutWindow(event_time - before, event_time + after)
# is_high_impact(): impact == high，或 medium 且标题命中 HIGH_IMPACT_KEYWORDS
# 拉取失败 → 回落到本地缓存 → 都没有则「trade blackouts disabled」（fail-open）
```

### 2.1 可直接借鉴

1. **「事件 → 时间窗」建模**：`BlackoutWindow(start, end, reason)` 是正确的抽象；
2. **本地缓存 + 拉取失败回落缓存**；
3. **fail-open**：数据源挂了就不阻断交易（详见 §11 降级策略）；
4. **`HIGH_IMPACT_KEYWORDS` 关键词表**（CPI / FOMC / NFP / Nonfarm / Interest Rate / PCE / PPI / GDP / PMI / ISM / Retail Sales / Fed Chair / Powell / ECB / BOE / BOJ / RBA / RBNZ / BOC / SNB）。

### 2.2 必须改的（照抄会出错）

| # | 参考仓库的做法 | 本项目的正确做法 | 原因 |
|---|---|---|---|
| 1 | `lookahead_days=14`（两周前瞻） | **改为 7 天** | 该数据源**只有 `ff_calendar_thisweek.json` 一个可用地址**，`ff_calendar_nextweek.json` 返回 **404**（已实测）。声称支持 14 天前瞻会给出「未来一周没有事件」的假安全感 |
| 2 | 一切转成北京时间裸字符串再比较 | **统一存 UTC epoch 整数**，判定时直接比 epoch，展示层才转 UTC+8 | 本项目是多用户 / 多节点 / 多时区；裸时间字符串一改服务器时区就整体错位 |
| 3 | 每个 bot 进程各自拉取、各自缓存 | **后端拉一次，全站共享**（事件表） | 本项目是「服务端集中判定 → 下发多节点」，节点侧不应各自去拉外网 |
| 4 | 拦截判定在 bot 本地 | 拦截判定在**服务端分发层** | 服务端有统一时钟与结构化 `skip_code`；节点侧闸门作为二期兜底 |
| 5 | `impact` 用 `high/medium/low` 小写 | 用**大写原值** `High/Medium/Low/Holiday`，归一化为小写 | 已实测该 JSON 的 `impact` 取值就是 `High`/`Medium`/`Low`/`Holiday`，参考仓库的小写常量经 `.lower()` 后能对上，但 `Holiday` 是第四种取值，必须显式处理（不拦） |
| 6 | 只判 `High` 或「Medium + 关键词」 | **只判 `High`**（决策 1） | 用户已拍板；关键词提升留作二期开关 |
| 7 | 黑名单期间的**已持仓**如何处理未定义 | 明确：**只拦开仓，持仓照常管理**（决策 3） | 与现有「链路断连闸门只拦开仓不拦平仓」的既有口径一致 |

### 2.3 实测的数据源事实（2026-11-11 验证）

```
GET https://nfs.faireconomy.media/ff_calendar_thisweek.json   → 200, 106 条
GET https://nfs.faireconomy.media/ff_calendar_nextweek.json   → 404（不存在）

impact 取值分布：Low=92, Medium=6, High=5, Holiday=3
country 取值：USD=38, EUR=20, GBP=17, JPY=10, CNY=6, AUD=5, CAD=5, All=2, NZD=2, CHF=1
date 格式："2026-11-08T17:30:00-05:00"（带美东偏移，非 UTC）
```

关键推论：

- **`date` 带 `-05:00` 偏移**（美东，且随夏令时变化）→ 必须用 `fromisoformat` 解析后转 UTC，**不能当裸本地时间**；
- **`country` 就是币种码**（`USD`/`EUR`/…），可以直接作为「事件币种」使用；
- **`country == "All"`** 表示全球性事件（2 条）→ 按「所有币种都命中」处理；
- **`Holiday` 必须显式排除**（参考仓库的 `is_high_impact` 会把它当非高影响天然排除，本项目照做即可，但要写进注释说明这是刻意的）。

---

## 3. 总体架构

```
        ForexFactory 公开 JSON（thisweek）
                 │  后端定时 worker（30 分钟一次）
                 ▼
        economic_event 表（DB 权威，全站共享）
                 │
                 ▼
        news_guard 模块（判定「此刻是否禁止新开仓」）
                 │
    ┌────────────┴────────────┐
    ▼                         ▼
dispatcher._try_open_active   group_dispatcher._dispatch_node
（normal 链路：按币种分发）     （strategy 链路：分组 / 跟单）
    │ 命中 → skip_code = "news_blackout"，不下发
    ▼
个人中心「财经日历」板块（事件列表 + 状态 + 开关 + 倒计时）
```

### 3.1 为什么判定放服务端而不是节点

1. **复用既有骨架**：`dispatcher.py` / `group_dispatcher.py` 的拦截 → `skip_code` → 不下发，是项目已验证的范式；
2. **结构化编码**：后台「拦截原因 Top N」统计直接能聚合出「因新闻少开了多少单」；
3. **统一时钟**：服务端 `time.time()` 不受节点本地时钟漂移影响；
4. **节点侧看不见「这是哪个用户的哪条信号」**：节点只收到命令，判定用户归属必须在服务端做。

### 3.2 节点侧闸门（二期，本次不做）

服务端只拦得住「新信号触发的首单」。以下开仓发生在**节点进程内部**，服务端拦不到：

- 网格策略的加仓（`strategy_runner._grid_open_first` 等）
- AI 智能加仓（tpl_1 的持续加仓）

由于决策 3 明确「**已经在跑的任务不用管**」，节点侧加仓属于「已在跑的任务」，**本次不改**。二期若要做，协议扩展方式见 §14。

---

## 4. 数据模型

### 4.1 表 `economic_event`（全局共享）

| 列 | 类型 | 说明 |
|---|---|---|
| `event_id` | `String(64)` PK | `sha1(title\|event_time\|country)` 截断，保证重复拉取幂等 |
| `event_time` | `BigInteger` | **UTC epoch 秒** —— 唯一权威时间 |
| `title` | `String(160)` | 事件标题 |
| `country` | `String(8)` | 币种码（`USD` / `EUR` / …），`All` 表示全球性 |
| `impact` | `String(8)` | 归一化后的小写：`high` / `medium` / `low` / `holiday` |
| `forecast` | `String(32)` | 预期值（可空） |
| `previous` | `String(32)` | 前值（可空） |
| `source` | `String(24)` | 数据源标识，默认 `forexfactory` |
| `fetched_at` | `DateTime` | 最近一次抓取到该条的时间 |
| `created_at` / `updated_at` | `DateTime` | |

索引：`ix_economic_event_time (event_time)`（按时间窗查询）；`event_id` 主键天然幂等。

> **不存时区字符串**，只存 epoch —— 消灭整类时区 bug。

### 4.2 表 `user_news_settings`（每人一行）

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `user_id` | `Integer` PK | — | 归属用户 |
| `enabled` | `Boolean` | `True` | 新闻黑名单总开关（默认开） |
| `before_min` | `Integer` | `30` | 事件前多少分钟开始停（0–120） |
| `after_min` | `Integer` | `30` | 事件后多少分钟恢复（0–120） |
| `min_impact` | `String(8)` | `high` | 最低影响等级（决策 1：默认只 `high`） |
| `currencies_json` | `JSON` | `["USD"]` | 关注币种（决策 2） |
| `symbols_json` | `JSON` | `[]` | 品种级覆盖（可选，二期；空 = 按币种推导） |
| `manual_paused` | `Boolean` | `False` | **手动停止做单**开关（急停） |
| `manual_paused_at` | `DateTime` | `None` | 暂停时刻 |
| `manual_pause_note` | `String(255)` | `None` | 暂停原因（可选） |
| `banner_dismissed_date` | `String(10)` | `None` | 顶部提示条被关闭的**当天**（北京时间 `YYYY-MM-DD`）；`NULL` = 当天未关闭 |
| `created_at` / `updated_at` | `DateTime` | — | |

> **为什么不挂 `users` 表的 JSON 列**：`users` 表已有 `follow_paused` / `ai_follow_paused` 这类**纯布尔**开关的先例；本需求是一组**带默认值、需要校验、需要审计变更**的参数，独立表更好扩展。若要省一张表，退路是 `users.news_settings_json`（与 `follow_settings_json` 的既有范式一致）。

---

## 5. 判定模块 `news_guard`

对外只暴露一个核心判定：

```
check_open_allowed(user_id, symbol, now) -> (allowed: bool, reason: str|None, code: str|None)
```

### 5.1 判定顺序（短路）

1. 取该用户设置；**没有记录 = 用默认值**（默认开启黑名单、30/30、只 high、只 USD）→ 存量用户零配置即生效；
2. 用户关了 `enabled` → **放行**；
3. 命中 `manual_paused` → **拦截**，reason = 「已手动停止做单」；
4. 由 `symbol` 推导币种（见 §5.3）；`symbols_json` 非空时以品种级覆盖优先；
5. 查 `now` 落在哪些事件的黑名单窗口内（`event_time - before_min*60 ≤ now ≤ event_time + after_min*60`）；
6. 事件须满足：`impact ≥ min_impact`（按决策 1，默认只 `high`）**且** 币种匹配（`country == 该币种` 或 `country == "All"`）；
7. 命中 → **拦截**，reason 形如 `CPI y/y (United States, high) 公布时段 ±30 分钟`；
8. 否则 → **放行**。

### 5.2 影响等级序

```
high > medium > low
holiday 不参与（永远不拦）
```

比较用「等级序号 ≥ 阈值序号」，这样二期把 `min_impact` 调成 `medium` 就能自动生效，无需改判定逻辑。

### 5.3 币种推导

```
XAUUSD / XAUUSDm / XAUUSD.pro  → USD
EURUSD                          → EUR
GBPUSD                          → GBP
USDJPY                          → JPY
```

规则：**品种名里出现的任一「已关注币种」，即认为该品种受影响**。

> 为什么不是「只取品种后 3 位」：`XAUUSD` 后 3 位是 `USD` 恰好对，但 `EURUSD` 后 3 位也是 `USD`（错，应是 EUR）。正确做法是**在品种名里查找已关注币种码**：`XAUUSD` 含 `USD` ✓；`EURUSD` 含 `EUR` 也含 `USD` —— 两者都在默认关注集合里就都命中，这正是我们想要的行为（欧元事件同样会影响 EURUSD）。

对 `XAUUSD`：币种集合默认只有 `USD`，`XAUUSD` 含 `USD` → 命中 ✓。

**`symbols_json` 品种级覆盖**（二期）：显式列出「这个品种归某个币种 / 这个品种永不拦」。

### 5.4 事件查询

```sql
SELECT * FROM economic_event
WHERE event_time BETWEEN :now - :before AND :now + :after
  AND impact IN (...)          -- 按 min_impact 过滤
```
命中后再按币种在内存里过滤（候选窗口内事件极少，通常 0–5 条）。

---

## 6. 拦截接入点（关键：只拦开仓）

### 6.1 A. normal 链路 —— `dispatcher.py` `_try_open_active`

插入位置：**在现有的「登录号不符 → 链路断连 → 区间过滤 → 持仓过滤」之后、抢占并发锁与发包之前**。

```python
# 新闻黑名单：高影响数据公布前后停止新开仓
blocked = await news_guard.block_open_reason(
    user_id=node_owner_id, symbol=signal.symbol,
)
if blocked:
    await self._skip(signal_id, node_id, blocked,
                     signal_codes.SKIP_NEWS_BLACKOUT)
    return {"status": "skipped", "reason": blocked}
```

用户身份 = 该节点的 `owner_user_id`（`nodes.owner_user_id`，Redis 缓存里有）。

> **放在抢占并发锁之前**：被新闻拦截的信号不应占用节点的执行锁，否则黑名单期间会把节点的并发槽白占住。

### 6.2 B. strategy 链路 —— `group_dispatcher.py` `_dispatch_node`

分组链路有**两种身份**，必须分别取：

| 节点角色 | 用户身份 | 取法 |
|---|---|---|
| 分组 **owner** 自己的成员节点 | 分组所有者 | `group.owner_user_id` |
| **跟单员**节点 | 跟单员本人 | `GroupFollowerNode.follower_user_id`（分组缓存 `group["followers"][i]["follower_user_id"]`） |

> **跟单员用自己的开关**：跟单员暂停不应该影响带单员本人。这与既有 `follow_state` / `leader_paused` 的分层语义一致 —— 黑名单是**再叠加的一层**，不要混进 `leader_paused`。

### 6.3 明确不拦的地方（写进注释，防止后人误加）

| 位置 | 理由 |
|---|---|
| `webhook.py` 的**信号受理阶段** | 同一路里 CLOSE 与 OPEN 走同一条路，在这里整体拒收会**误伤平仓** → 拦在逐目标下发层 |
| `_dispatch_close` / 所有 CLOSE 路径 | 决策 3：平仓永远放行 |
| `strategy_stop` / `strategy_update` / 改单 | 持仓管理不受黑名单影响 |
| 节点侧 `place_market_order` 之外的路径 | 决策 3：已在跑的任务不管 |

### 6.4 `signal_codes.py` 扩展（遵循既有「只增不改」规矩）

```python
SKIP_NEWS_BLACKOUT = "news_blackout"   # 财经日历黑名单
```
- 加入 `SKIP_CODES` 元组；
- 补 `SKIP_CODE_LABELS`：`"新闻黑名单"`；
- `_BACKFILL_RULES` 可加一条 `("新闻黑名单", ("黑名单", "财经", "新闻"))`（兼容旧文案，可选）。

---

## 7. 接口设计

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| `GET` | `/api/me/news` | **核心接口**：我的设置 + 当前黑名单状态 + 顶部提示条状态 + 未来 N 天事件列表（每条标注是否落入我的黑名单） | 登录即可 |
| `PUT` | `/api/me/news/settings` | 保存设置（开关 / 30-30 / 币种 / 手动急停） | 登录即可 |
| `POST` | `/api/me/news/banner/dismiss` | **关闭顶部提示条**（当天不再显示，次日重新提示） | 登录即可 |
| `POST` | `/api/me/news/refresh` | 手动强制刷新日历（排障用，限频） | 登录即可 |
| `GET` | `/api/admin/news/events` | 全局事件表 + 最近抓取状态（排查「为什么没拦住」） | 管理员 |

### 7.1 `GET /api/me/news` 响应结构

```jsonc
{
  "settings": {
    "enabled": true, "before_min": 30, "after_min": 30,
    "min_impact": "high", "currencies": ["USD"], "symbols": [],
    "manual_paused": false, "manual_paused_at": null,
    "banner_dismissed_date": null   // 顶部提示条被关闭的当天（北京时间 YYYY-MM-DD）
  },
  "status": {
    "blocked": true,              // 当前是否禁止新开仓
    "reason": "CPI y/y (United States, high) 公布时段 ±30 分钟",
    "code": "news_blackout",
    "until": 1762968000,          // 黑名单结束时刻（epoch），未阻塞为 null
    "remaining_sec": 812,         // 剩余秒数（前端倒计时）
    "server_time": 1762967188
  },
  "banner": {
    "show": true,                 // 今天有黑名单时段且未被关闭 → 显示顶部提示条
    "dismissed": false,           // 今天是否已被用户关闭
    "today": "2026-11-11",        // 服务器当前的北京日期
    "reason": "CPI y/y 等 2 项高影响数据（11-13 21:30 北京时间）公布期间已停止新开仓",
    "events": [ { "event_id": "…", "event_time": 1762964400, "title": "CPI y/y",
                  "country": "USD", "impact": "high" } ],
    "server_time": 1762967188
  },
  "events": [
    {
      "event_id": "…", "event_time": 1762964400, "title": "CPI y/y",
      "country": "USD", "impact": "high",
      "forecast": "0.8%", "previous": "0.9%",
      "in_blackout": true,         // 是否落入我的黑名单
      "block_reason": null         // 不在黑名单时说明为什么（币种不匹配 / 等级不足）
    }
  ],
  "calendar": {
    "fetched_at": 1762960000, "stale": false, "event_count": 5
  }
}
```

### 7.2 权限与越权防护

- **`user_id` 一律取自 `Principal`**，**绝不接受请求体里的 user_id**；
- 普通用户只能读写自己那一行；
- 管理员**不能替普通用户改开关**（全局强制黑名单是另一个需求，不在本次范围）；
- 事件表是公共财经数据，无敏感性。

---

## 8. 前端设计

### 8.1 位置：独立页面 + 【我的】入口

财经日历是一个**独立的功能模块**（有设置、有事件列表、有全局提示条），
不再挤在个人中心的第 3 个卡片里。

| 部件 | 文件 | 位置 |
|---|---|---|
| 独立页面 | `frontend/src/views/NewsCalendarView.vue` | 路由 `/news-calendar`，**登录即可进，不挂 `meta.menu`** |
| 入口卡片 | `frontend/src/views/MineView.vue` | 「我的模块」网格第一项，**不限菜单权限**（所有登录用户可见） |
| 顶部提示条 | `frontend/src/components/NewsTopBanner.vue` | 挂在 `App.vue` 的 `header` 之后、`main` 之前 —— **全站可见**，不属于任何一个页面 |
| 共享状态 | `frontend/src/stores/news.ts` | Pinia store：页面与提示条读同一份数据 |

> **为什么要有 store**：提示条挂在 `App.vue` 上（每个页面都要显示），
> 页面自己也要显示同一份状态。两处各自维护副本必然出现
> 「顶栏说有黑名单、页面说没有」的自相矛盾。统一从 store 读，
> 任何一处改了设置都调 `save()` 回写并刷新。

### 8.2 独立页面结构（自上而下两块）

**① 状态卡（`pf-card`）**

| 状态 | 展示 |
|---|---|
| 正常 | 绿点 + 「开单正常」 |
| 手动暂停 | 橙底 + 「已手动停止做单」 |
| 新闻黑名单中 | 红底横幅 + `CPI y/y (USD, high) 公布时段黑名单（±30 分钟）` + 自动倒计时 |

**② 事件卡（`pf-card`）**

- 按日期分组；每条：北京时间 `11-12 20:30` + 标题 + 国家 + 影响标签（高=红）+ 前值/预期 + 「我的黑名单」状态徽标；
- 支持「只看我的黑名单内事件」筛选；
- 展示一律标注「北京时间」；
- 数据新鲜度提示（抓取失败时显式提示，不静默）。

### 8.3 顶部滚动提示条

**触发条件**（后端 `news_guard.banner_state` 判定，三者同时满足）：

1. 黑名单总开关是开的；
2. **今天**（北京时间自然日）有事件落在黑名单窗口内 —— 注意是「今天有」，
   不是「此刻正在」，这样用户在事件开始前就能看到预警；
3. 用户**今天还没关闭过**提示条。

**交互**：

| 元素 | 行为 |
|---|---|
| 滚动文案 | `CPI y/y 等 2 项高影响数据（11-13 21:30 北京时间）公布期间已停止新开仓 · 点击查看财经日历` |
| 「查看」按钮 | 跳到 `/news-calendar` |
| 「X」按钮 | **关闭提示条**：记「当天已关闭」，当天不再显示，次日自动重新提示 |

**「当天关闭」的语义**：后端存 `banner_dismissed_date`（北京日期 `YYYY-MM-DD`），
**不存布尔值** —— 布尔值做不到「每天一次」的语义。次日自动重新提示。

**两条不能破的约束**：

- 关提示条**只关提示条**：不能动 `manual_paused`（停止做单）与任何开仓参数；
- 改设置**不能清掉**「当天已关闭」标记 —— 否则用户关掉提示条、又改个币种，
  提示条就又冒出来了。所以 `save_settings()` 明确不碰 `banner_dismissed_date`。

### 8.4 「今天」的判定口径

提示条的「今天」按**北京时间自然日**算，与前端展示口径一致。
服务端 `beijing_today()` / `beijing_day_bounds()` 提供权威口径。

> 为什么不能按服务器 UTC：服务器跑 UTC 时，UTC 的 8 号深夜对应北京 9 号凌晨。
> 用户在凌晨 1 点关掉提示条、早上 8 点再看 —— 若前后端口径不一致，
> 会一边算「今天」一边算「昨天」，提示条莫名复活。

### 8.5 倒计时

前端本地倒计时：服务端给 `event_time` 与 `server_time`，前端 `setInterval` 每秒自减。
跨端进入/退出黑名单**不需要轮询**（服务端仍是权威判定）。

### 8.6 样式

复用全局 `styles.css` 的 `.pf-card` / `.pf-section-head` / `.pf-section-title` / `.pf-list` / `.pf-input` / `.pf-btn` / `.pf-hint` / `.pf-error` / `.pf-count`，页面内 `<style scoped>` 只放本页布局。**不引入新 CSS 变量**。

提示条用独立配色（暗红底 + 脉冲红点），与页面内的状态条区分开 ——
它在页面**之外**，需要一眼扫到。

### 8.2 板块结构（自上而下四块）

**① 状态条（最显眼）**

| 状态 | 展示 |
|---|---|
| 正常 | 绿点 + 「开单正常」 |
| 手动暂停 | 橙底 + 「已手动停止做单」+ 恢复按钮 |
| 新闻黑名单中 | 红底横幅 + `CPI y/y (美国) 公布时段 · 剩余 18 分钟` + 自动倒计时 |

**② 开关与参数**

- 「新闻黑名单」总开关（默认开）
- 「停止做单」手动开关（急停，独立于新闻）
- 事件前 / 后分钟数（默认 30 / 30，各 0–120）
- 最低影响等级（本期固定「高影响」，或下拉可选）
- 关注币种（多选，默认只勾 USD）
- **必须显眼的说明文案**：
  > 「只影响**新开仓**。已在运行的任务与已有持仓的止损止盈**照常执行**，不会被中断。」

**③ 事件列表**

- 按日期分组；每条：北京时间 `11-12 20:30` + 标题 + 国家 + 影响标签（高=红）+ 前值/预期 + 「我的黑名单」状态徽标；
- 支持「只看我的黑名单内事件」筛选；
- 展示一律标注「北京时间」。

**④ 数据新鲜度**

- 「日历数据更新于 X 分钟前」；
- 抓取失败时**显式提示**「更新失败，当前展示上次数据（黑名单可能不完整）」—— **不静默**。

### 8.3 倒计时

前端本地倒计时：服务端给 `event_time` 与 `server_time`，前端 `setInterval` 每秒自减。跨端进入/退出黑名单**不需要轮询**（服务端仍是权威判定）。

### 8.4 样式

复用全局 `styles.css` 的 `.pf-card` / `.pf-section-head` / `.pf-section-title` / `.pf-list` / `.pf-input` / `.pf-btn` / `.pf-hint` / `.pf-error` / `.pf-count`，页面内 `<style scoped>` 只放本页布局。**不引入新 CSS 变量**。

---

## 9. 时间基准（必须钉死，否则一定出 bug）

| 环节 | 口径 |
|---|---|
| **存储** | DB 一律存 **UTC epoch 整数**（`event_time`），彻底消灭时区字符串 |
| **事件时间解析** | FF 的 `date` 带美东偏移（`2026-11-12T08:30:00-04:00`）→ `datetime.fromisoformat` 解析 → `.timestamp()` 转 epoch。**不带偏移的裸串按美东处理** |
| **判定** | 服务端 `time.time()` 与 epoch 直接比，不涉及时区 |
| **展示** | 统一转 **UTC+8（北京时间）**，界面标注「北京时间」 |
| **节点时钟** | 服务端判定不依赖节点时钟（决策 3 不需要节点侧闸门） |
| **服务器 NTP** | 服务端判定依赖本机时钟准确，请确保 NTP 同步 |

---

## 10. 降级与容错策略（明确取舍）

| 场景 | 策略 | 理由 |
|---|---|---|
| 日历抓取失败 | **保留上次数据 + 显式提示「数据可能过期」**，不阻断交易 | 数据源短暂不可用就把全站交易停了，比不停更糟；参考仓库同口径 |
| 事件表为空（首次启动就失败） | 同上，不阻断 | 同上 |
| 判定模块抛异常 | **放行**并打日志告警 | 判定层故障不能静默变成「永不开单」 |
| Redis 不可用 | 判定降级读 DB（DB 是权威） | 与本项目其它模块口径一致 |
| `before_min` / `after_min` 设为 0 | 允许（等于只拦精确时刻），UI 提示风险 | 用户自主权 |
| `manual_paused` 手动急停 | 任何情况下立即生效，**不受上述任何降级影响** | 急停必须绝对可靠 |
| 用户没有任何设置记录 | 用默认值（开 / 30-30 / high / USD） | 存量用户零配置即生效 |

---

## 11. 审计与权限

- 开关与参数变更走 `persist.audit(category="system")`，进「我的最近操作」，与现有审计体系一致；
- 普通用户只能读写自己那一行（`user_id` 取自 `Principal`）；
- 管理员可通过 `/api/admin/news/events` 看全局事件表与抓取状态，**但不能替普通用户改开关**。

---

## 12. 测试要点

### 12.1 纯函数级（`news_guard`，最容易测、价值最高）

- 窗口边界：事件前 30 分整 / 后 30 分整（`≤` 含端点）；
- 影响等级映射与阈值比较；
- 币种推导：`XAUUSD`→USD、`EURUSD`→EUR、币种不匹配不拦；
- 多事件窗口重叠；
- **手动暂停优先级高于新闻黑名单**；
- 用户关闭 `enabled` → 全部放行；
- `country == "All"` → 所有币种命中。

### 12.2 拦截级（集成）

- 构造一条信号，命中黑名单 → 断言 `skip_code == "news_blackout"`、**没有下发到节点**；
- **断言 CLOSE 信号在黑名单期间仍能下发**（最关键的一条回归）；
- 跟单员节点按**跟单员自己的**设置判定，与带单员无关；
- 币种不匹配 / 等级不足 / 用户关开关 → 正常下发。

### 12.3 抓取器级

- 解析：`High` / `Medium` / `Low` / `Holiday` / 非法值；
- 时间解析：带偏移 / 不带偏移 / 非法格式；
- 拉取失败 → 保留旧数据 + `stale=True`。

---

## 13. 实施顺序

| # | 步骤 | 产出 |
|---|---|---|
| 1 | ORM 两张表 + 迁移 | `orm.py` / `db.py` |
| 2 | 抓取器 + 定时 worker | `economic_calendar.py` |
| 3 | 判定模块 | `news_guard.py` |
| 4 | `skip_code` 扩展 | `signal_codes.py` |
| 5 | 两处分发引擎接入拦截 | `dispatcher.py` / `group_dispatcher.py` |
| 6 | `/api/me/news` 三接口 | `news_api.py` |
| 7 | 前端板块 | `ProfileView.vue` + `types.ts` + `api/news.ts` |
| 8 | 测试 | `backend/tests/test_news_*.py` |

---

## 14. 后续演进（本期不做）

| 想法 | 价值 | 前置条件 |
|---|---|---|
| **节点侧闸门**（覆盖网格加仓 / AI 加仓） | 黑名单期间连加仓也停 | 决策 3 明确「已在跑的任务不管」，本期排除；协议扩展方式：`auth_ok` 带 `news` 段 + 新增 `news_config` 推送消息，节点在 `place_market_order` 前判定 |
| 关键词提升为高影响（`Medium` + 关键词 → 拦） | 覆盖更多重要数据 | `min_impact` 参数已预留，改判定逻辑即可 |
| 更多币种 | 支持 EUR / GBP / JPY 交易 | 前端多选已支持 |
| 手动加事件 | 用户自补临时会议 | 事件表加 `source="manual"` + `user_id` |
| 「因新闻少开单」收益对比报表 | 量化黑名单价值 | `skip_code` 已可聚合 |
| 全局强制黑名单（管理员对全站） | 风控兜底 | 另一套权限模型，不在个人中心范围 |

---

## 15. 验收标准

- [ ] 个人中心出现「财经日历」板块，未配置过任何设置的用户**默认生效**（开 / 30-30 / 只 high / 只 USD）
- [ ] 高影响事件公布前后各 30 分钟内，**新开仓信号被拦截**，`skip_code == "news_blackout"`，节点**未收到**开仓指令
- [ ] **黑名单期间 CLOSE 信号仍能正常下发**（平仓不受影响）
- [ ] 黑名单期间**已在跑的任务继续运行**（不终止、不强制平仓）
- [ ] 手动「停止做单」开关打开后，新开仓立即被拦；关闭后恢复
- [ ] 关注币种改为不含 USD 后，USD 事件不再拦截本用户
- [ ] 事件列表显示北京时间，并标注「北京时间」
- [ ] 日历抓取失败时页面**显式提示数据可能过期**，且**不阻断交易**
- [ ] 普通用户只能看到 / 修改自己的设置（越权请求 404/403）
- [ ] 移动端（≤768px）无横向滚动，板块可读可用

---

## 16. 决策记录（ADR 摘要）

| # | 决策 | 备选 | 选择理由 |
|---|---|---|---|
| 1 | 只拦高影响 | 高+中 | 用户拍板；中影响事件过密会让黑名单时段过多 |
| 2 | 默认只关注 USD | 默认全币种 | 用户拍板；本系统主力是黄金 / 美元对 |
| 3 | 只停新开仓 | 连加仓一起停 | 用户拍板；已在跑的任务不该被中途打断 |
| 4 | 窗口可配，默认 30/30 | 写死 30/30 | 用户拍板；不同数据波动幅度不同，需可调 |
| 5 | 判定在服务端分发层 | 节点侧判定 | 复用既有拦截骨架 + 结构化 `skip_code` + 统一时钟 |
| 6 | 事件表全局共享 | 每用户一份 | 公共财经数据，共享成本低、一致性好 |
| 7 | 存储用 epoch 整数 | 存时间字符串 | 彻底消灭时区 bug |
| 8 | 只做 7 天前瞻 | 声称 14 天 | 数据源**只有 thisweek 一个地址**，`nextweek` 实测 404 |
| 9 | 抓取失败 fail-open | 拉不到就全站停单 | 数据源短暂故障不该停掉全站交易 |
| 10 | 手动急停独立于新闻开关 | 合并成一个 | 语义不同：一个是「暂停所有新开单」，一个是「避开高影响数据」 |
