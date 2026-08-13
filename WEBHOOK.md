# Webhook 入参文档

> **最后更新：2026-08-10**（对齐 v0.6.7：策略响应形态、分组趋势风控、品种拒收范围）

TradingView → MT5 跟单系统的信号接收接口说明。本文档描述的是**代码的真实行为**（以 `backend/app/webhook.py` + `backend/app/parser.py` + `backend/app/config.py` + `backend/app/group_dispatcher.py` 为准），而非 README 的宣传性描述。

---

## 1. 接口概览

| 项 | 值 |
| --- | --- |
| 方法 | `POST` |
| 路径 | `/webhook`（无前缀） |
| 经 nginx | `^/(api\|webhook\|health\|docs\|redoc\|openapi.json)` 反代到后端 |
| Content-Type | `application/json` 或 `text/plain` 均可（按请求体内容自动判断，不依赖该头） |
| 编码 | UTF-8（非法字节会被忽略） |

请求体读取逻辑：先按 UTF-8 解码并 `strip()`，再尝试 `json.loads()`；**成功即按 JSON 处理，失败即按纯文本处理**，空体按空对象 `{}` 处理。

---

## 2. 鉴权与访问控制

解析信号**之前**有两道可选门槛，按顺序执行。两者默认均关闭（见 `.env`）。

### 2.1 IP 白名单（先执行）

- 开关：`ENABLE_IP_WHITELIST=true`
- 名单：`WHITELISTED_IPS=ip1,ip2,...`（逗号分隔）
- 取源 IP 时兼容反向代理（优先 `X-Forwarded-For`）。
- 不在名单内 → `403 {"detail":"ip not allowed"}`。
- 默认名单为 TradingView 官方出口 IP：`52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7`。

### 2.2 Token 鉴权（后执行）

- 开关：`ENABLE_AUTH=true`
- 共享密钥：`AUTH_TOKEN=xxx`
- token 可通过以下 **4 种方式**传入（按此优先级查找，命中即止）：
  1. 请求头 `X-Auth-Token: <token>`
  2. 查询参数 `?token=<token>`
  3. JSON 体字段 `token` 或 `auth_token`（仅当请求体是 JSON 对象时）
  4. 请求头 `Authorization: Bearer <token>`
- 不匹配 → `401 {"detail":"invalid token"}`。

---

## 3. 请求体形态与解析流程

只有以下 **3 种形态**能被解析，其余（JSON 数组、纯数字、布尔、null）一律失败返回 `400`：

```
请求体
 ├─ 纯文本字符串 ──────────────► 文本解析（格式二）
 ├─ JSON 对象 {…}
 │     ├─ 能取到 action+symbol ─► 结构化解析（格式一）
 │     └─ 否则若含 text/message/body 字段 ─► 取该字段文本 → 文本解析（格式三）
 └─ 其它 JSON（数组/数字/布尔/null）─► 失败（400）
```

---

## 4. 格式一：结构化 JSON（推荐）

**硬性要求：必须能同时识别出 `action` 和 `symbol`，缺任意一个即返回 `400`。** 字段名**大小写不敏感**。

### 4.1 action（动作，必填）

- 接受的字段名（任取其一，按顺序取第一个非空值）：`action` / `signal` / `direction` / `cmd`
- 取值转小写后做**子串匹配**，优先级 **CLOSE > BUY > SELL**（所以 `"close buy"` 判为 `CLOSE`）。
- 关键字见 [§8](#8-动作关键字清单)。值中不含任何关键字 → 视为取不到 action。

### 4.2 symbol（品种，必填）

- 接受的字段名（任取其一）：`symbol` / `ticker` / `s` / `sym`
- 取值转大写去空格后：
  - 命中品种映射表 → 返回映射后的 MT5 品种；
  - 未命中但非空（且不等于 `"NONE"`）→ **原样透传**（交由节点端再解析后缀）。
- 品种映射表见 [§7](#7-支持的品种清单)。

### 4.3 可选字段

| 含义 | 接受的字段名 | 默认值 / 规则 |
| --- | --- | --- |
| 处理模型 `model` | `model` | `normal`（默认，按币种分发）/ `strategy`（按分组分发，见 [§4.5](#45-策略信号-modelstrategy)）；缺省或空 → `normal`；其它取值 → `400` |
| 策略模版定向 `template_ids` | `template_ids` / `templateids` / `template_id` / `templateid` | 策略模版 ID 数组，只有绑定了其中某个模版的分组才接收本信号（见 [§4.5](#45-策略信号-modelstrategy)）；缺省或空数组 → 不限制；**仅 `model=strategy` 可用**，其它情况 → `400` |
| 分组定向 `group_ids` | `group_ids` / `groupids` / `group_id` / `groupid` | 分组 ID 数组，只有 ID 在其中的分组才接收本信号（见 [§4.5](#45-策略信号-modelstrategy)）；缺省或空数组 → 不限制；**仅 `model=strategy` 可用**，其它情况 → `400` |
| 手数 `volume` | `volume` / `lotsize` / `lot` / `v` / `q` | 缺省或 ≤0 或非法 → `DEFAULT_LOT`（默认 `0.1`）；超过 `MAX_LOT_SIZE`（默认 `1.0`）则封顶 |
| 止损 `stop_loss` | `sl` / `stoploss` / `stop_loss` / `stop` | 解析为正浮点；`0`/空/非法 → 不设（`null`） |
| 止盈 `take_profit` | `tp` / `takeprofit` / `take_profit` / `target` | 解析为正浮点；`0`/空/非法 → 不设（`null`） |
| 入场价 `entry_price` | `limit_price` / `limitprice` / `entry_price` / `entryprice` / `limit` / `entry` / `price` | 解析为正浮点；`0`/空/非法 → 不设（`null`）。**只有配成限价开仓的策略模版2 会用它**，其余链路忽略，见 [§4.6](#46-限价开仓策略模版2) |
| 备注 `comment` | `comment` | 字符串，默认 `""` |
| 订单类型 `order_type` | `type` / `ordertype` | 转小写，默认 `"market"`。目前仅作留痕，开仓方式由策略配置的**开仓方式**开关决定，不看这个字段 |
| 允许加仓 `allow_position` | `allow_position` / `allowposition` / `position_allowed` | 取值能被 `int()` 解析为**非零整数**时为 `true`，否则 `false`（精确规则见下方） |

> 注：这里的 `volume` 仅是「信号携带手数」。最终每个节点实际下单手数还取决于该节点的手数策略（跟随全局 / 固定 / 跟随信号），见节点配置。**策略模版2 与模版3 是例外**：前者按风险金额反推手数、后者按每格手数下单，都不使用信号 `volume`。

> `model` 与两个定向字段只能通过结构化 JSON 传入。纯文本告警（格式二）没有携带它们的位置，一律按 `normal`、不做定向处理。

> ⚠️ `allow_position` 的真实判定是 `bool(int(value)) == 1`（取值非空时），即「`int(value)` 能成功且结果非 0」才为 `true`。这与直觉上的「等于 1」并不完全一致：

| 取值 | 结果 | 说明 |
| --- | --- | --- |
| `1` / `"1"` | ✅ `true` | 标准用法 |
| `2` / `"2"` / `-1` / `"-1"` | ✅ `true` | 任意非零整数都成立 |
| `1.5` | ✅ `true` | `int(1.5)==1` |
| `true`（JSON 布尔） | ✅ `true` | `int(True)==1` |
| `0` / `"0"` / `false` | ❌ `false` | 零值 / `int(False)==0` |
| `"true"` / `"yes"` | ❌ `false` | 非数字字符串，`int()` 抛错 |
| `"1.5"` | ❌ `false` | 带小数点的字符串，`int("1.5")` 抛错 |
| `0.4` | ❌ `false` | `int(0.4)==0` |
| 缺省 / `null` | ❌ `false` | 默认值 |

> 实践建议：**只传整数 `1` 开启、`0`（或不传）关闭**，不要依赖上面的边界写法。

### 4.4 示例

```json
{"action": "buy", "symbol": "EURUSD", "volume": 0.1}
```

```json
{"action": "sell", "ticker": "XAUUSD", "volume": 0.2, "sl": 2300.0, "tp": 2400.0, "comment": "pine"}
```

```json
{"direction": "做多", "s": "GBPUSD", "lot": 0.2, "allow_position": 1}
```

```json
{"cmd": "close", "sym": "US30"}
```

### 4.5 策略信号（`model=strategy`）

带 `"model": "strategy"` 的信号走**分组分发**链路（`group_dispatcher.py`），与默认的按币种分发完全隔离：不读中控台品种配置（**不走 §9.3 的「品种未登记 / 已禁用」拒收**），不做区间方向 / 持仓过滤，也不读节点的按币种手数策略。

入选条件是「分组已启用 + 绑定了启用中的策略 + 策略绑定品种与信号品种一致」，信号带 `template_ids` / `group_ids` 时再加一层定向（见下）。命中的每个分组各自下发一条 `strategy_start` 给组内有效节点（已启用 + 在线），由节点按策略规则托管到持仓全平；`CLOSE` 信号则是终止指令，平掉对应魔术号的全部持仓。

可选的**分组趋势风控**（管理端「分组」页按组开启，默认关）：开启后，开仓信号在该节点抢到占位、建子任务前，会按「趋势面板」**全局参数**读取该节点终端行情并算趋势——`BUY` 仅多头放行、`SELL` 仅空头放行；中性、数据不足或行情读取失败一律拦截，子任务记为 `skipped` 并写中文 `skip_reason`。`CLOSE` / 停策略**绕过**该门禁。组内 `poll` 模式下被拦节点会顺延下一节点。Webhook 本身无额外字段；是否拦截取决于分组配置，不写进信号体。

策略实例基于**策略模版**创建，不同模版对信号的要求不同：

| 模版 | 首单手数来源 | 对信号的额外要求 |
| --- | --- | --- |
| 策略模版1（逆势 / 顺势加仓） | 信号 `volume` | 无 |
| 策略模版2（以损定量趋势单） | 按风险金额反推，**忽略信号 `volume`** | **必须携带止损价 `sl`** |
| 策略模版3（网格交易） | 规则里的每格手数，**忽略信号 `volume`** | 方向须与网格方向相容（见下） |

无论哪个模版，信号本身只提供「品种 + 方向」，策略参数一律来自后台配置的规则快照——**不能通过 Webhook 传网格区间、风险金额之类的参数**。分组与策略为一对一绑定；管理端也可通过「中控台 → 手动发信号」触发同一条链路。

#### 定向下发：`template_ids` / `group_ids`

同一个品种下往往同时挂着多个分组（一个趋势单分组、一个网格分组……）。默认情况下它们会**同时**收到该品种的每条策略信号；如果某条信号只想让其中一部分接收，就带上定向字段——按玩法收窄用 `template_ids`，按具体分组点名用 `group_ids`：

```json
{"model": "strategy", "action": "buy", "symbol": "XAUUSD", "template_ids": ["tpl_3"]}
```

```json
{"model": "strategy", "action": "buy", "symbol": "XAUUSD", "group_ids": ["grp_3f2a9c1b8d7e6054"]}
```

第一条只发给「绑定了模版3（网格交易）策略」的分组；第二条只发给该 ID 的那个分组。落选的分组会把原因写进信号记录。

| 模版 ID | 对应模版 |
| --- | --- |
| `tpl_1` | 策略模版1：顺势 / 逆势加仓 |
| `tpl_2` | 策略模版2：以损定量趋势单 |
| `tpl_3` | 策略模版3：网格交易 |

分组 ID 形如 `grp_` + 16 位十六进制，可在管理端「分组」页或 `GET /api/groups` 取到；用的是 ID 而不是分组名称，改名不会让线上信号失效。

规则要点：

- **数组内是「或」**：命中任意一项即可接收；**两个字段之间是「与」**：同时给出时要两边都命中。
- **缺省或空数组 = 不限制**，行为与本功能上线前完全一致。
- 写法上支持数组 `["tpl_1","tpl_2"]` 与逗号分隔字符串 `"tpl_1,tpl_2"`；取值会去空白并转小写。
- **定向只是叠加的一层筛选**，品种匹配、分组与策略的启用状态、模版自身的开仓准入（模版2 要 `sl`、模版3 要方向相容）都照旧生效。
- `CLOSE` 信号同样受约束：只终止被点中的分组里进行中的任务。
- 没写 `model=strategy` 却带了定向字段 → `400`（避免误按币种链路广播下单）。
- 不存在的模版 ID → `400 unknown template_ids: xxx`（模版是固定的三个，写错就是配置错误）；不存在的分组 ID **不报 400**，而是 `200` + `status=rejected`，原因里写明 `指定的分组不存在：grp_xxx`——分组随时可增删，删掉一个就让告警开始返回 400 反而更难排查。
- 被点名的分组若处于禁用状态，落选原因会明确写成 `分组已禁用`；没被点名的分组则不会出现在落选说明里，避免一条定向信号带回几十条无关原因。

#### 模版2：以损定量趋势单

手数由「风险金额 ÷ 止损距离」算出，没有止损价就算不出手数，因此缺 `sl` 的信号会在分发前被挡下，落选原因写进信号记录（响应 `mode=rejected`）。同品种下若还有模版1 的分组，它不受影响、照常入选。

```json
{"model": "strategy", "action": "buy", "symbol": "XAUUSD", "sl": 2397.0}
```

上例中节点会：按风险金额与 `2397.0` 反推总手数 → 底仓市价成交（止损 `2397.0`，**不挂止盈**）→ 立即把剩余仓位等分成多笔分散仓一并市价打出，共用同一止损，止盈按等分阶梯逐档挂出（第 i 笔 = 开仓价 + 止损距离 × 盈亏比 × i ÷ 分散仓单数，只有最远一档吃满盈亏比）→ 若开了保本触发，浮盈达标后把止损移到持仓加权均价。

> 模版2 的信号里 `volume`、`tp` 都会被忽略：手数由风险金额决定，止盈由盈亏比与阶梯档位决定。

#### 4.6 限价开仓（策略模版2）

模版2 的规则里有一个**开仓方式**开关，默认 `市价`（行为与上一节完全一致）。改成 `限价` 后，信号除了 `sl` 还必须携带**入场价**：

```json
{"model": "strategy", "action": "buy", "symbol": "XAUUSD", "sl": 2380.0, "limit_price": 2390.0}
```

节点收到后不再按现价成交，而是一次挂出一整组限价单：底仓挂在 `limit_price`，分散仓在「入场价 → 止损价」之间等分挂阶梯限价（分母取分散仓单数 + 1，所以末档不会正好落在止损价上）。挂单一直等到成交（GTC），期间该节点该品种不接新信号。

两处与市价模式不同，配置前需要知道：

- **手数会更大**。各档开仓价不同、止损价共用，止损距离因此是一组递减值，总手数改按仓位比例加权的平均止损距离反推。挂单价通常比现价有利，加权距离更小，同样的风险金额会开出更大的仓。
- **风险金额变成上限而不是定值**。它对应「全部档位都成交后打到止损」的最坏情况；只成交了前几档就打到止损的话，实际亏损小于该值。

准入校验也随之收紧，以下情况在分发前就会被挡下并写进落选原因：

| 情况 | 落选原因 |
| --- | --- |
| 缺入场价 | `限价开仓需要信号携带入场价（limit_price / price），本信号未提供` |
| `BUY` 的入场价 ≤ 止损价 | `限价开仓的入场价 x 需高于止损价 y` |
| `SELL` 的入场价 ≥ 止损价 | `限价开仓的入场价 x 需低于止损价 y` |

> 挂在止损之外的单一旦成交就已越过止损，等于开仓即止损，所以这类信号一律拒收而不是照单挂出。
>
> 挂单价距现价太近（低于券商的 `trade_stops_level`）或挂错方向时，节点侧会下单失败并把券商原因回报到子任务，不会静默丢单。

#### 模版3：网格交易

手动创建网格。信号只负责「启动这组网格」，区间 / 格数 / 每格手数 / 止损止盈都取自策略规则，因此**不要求携带 `sl`**：

```json
{"model": "strategy", "action": "buy", "symbol": "XAUUSD"}
```

节点收到后会：按区间与格数切出网格线 → 若配了触发价则等现价**触及**（穿越或落到）该价、否则立即启动 → 可选按「仍有盈利空间的格位」市价建底仓（多头：卖出价仍高于现价；空头对称）→ 之后价格穿越网格线买卖对应格。**空仓是正常运行态**，所以网格任务不会因持仓归零而收口，只由止损价 / 止盈价 / `CLOSE` 信号结束。规则字段 `stop_lower` / `stop_upper` 按价格上下沿存储：多头时下沿为止损、上沿为止盈；**空头相反**（上沿为止损、下沿为止盈）。`close_on_stop=false` 时终止只停交易、保留持仓（子任务 `detached`），直到仓位被外部平光。

信号方向受规则里的**网格方向**约束，不相容的信号会在分发前挡下：

| 网格方向 | 接受的 `action` |
| --- | --- |
| `long` 只做多 | `BUY`（收到 `SELL` 拒收） |
| `short` 只做空 | `SELL`（收到 `BUY` 拒收） |

开了**向上追踪**的网格，价格越过区间外沿时不会停机，而是把整个区间连同止损止盈平移一格继续跑（多头追涨、空头追跌），直到触发止损或达到配置的最大平移格数。同一根突破 tick 会先兑现穿越卖出，再平移。

> 模版3 的信号里 `volume`、`sl`、`tp` 都会被忽略：手数按每格手数下单，止损止盈来自网格规则。无启用规则、区间非法、或总手数上限小于每格手数时，创建策略与分发都会拒收。
>
> `CLOSE` 按**进行中的子任务 + 开仓时策略快照**匹配（含 `group_ids` / `template_ids` / 品种），不要求分组或策略当前仍启用——禁用后仍可终止在跑网格。
---

## 5. 格式二：纯文本告警

当请求体不是合法 JSON 时按文本解析。**只提取 action / symbol / volume / sl / tp**，不支持 comment、order_type、entry_price、allow_position。因此配成限价开仓的模版2 只能用结构化 JSON 触发。

| 项 | 规则 |
| --- | --- |
| action | 整段文本子串匹配关键字（优先级 CLOSE > BUY > SELL） |
| symbol | 依次尝试 4 种正则：①`SYMBOL=xxx` / `SYMBOL: xxx` 标签 ②`TICKER=xxx` 标签 ③裸 **6 个字母**（如 `EURUSD`） ④`EUR/USD` 斜杠形式。命中后：在映射表内则映射，否则需 ≥6 字符才原样透传（坑位见下方 ⚠️） |
| volume | 必须带关键字：`VOLUME=0.1` / `LOT=0.1` / `0.1 LOT`；否则用默认手数 |
| sl | `SL=1.23` 或 `STOP LOSS=1.23` |
| tp | `TP=1.23` / `TAKE PROFIT=1.23` / `TARGET=1.23` |

> 标签与数值之间的分隔符可为 `:`、`=` 或空格。匹配大小写不敏感。

### 5.1 示例（均可解析）

```text
buy EURUSD
```

```text
EURUSD sell
```

```text
close XAUUSD VOLUME=0.2 SL=2300 TP=2400
```

```text
SYMBOL=GBPUSD long LOT=0.1
```

> ⚠️ 纯文本模式的品种识别坑位（与 JSON 模式差异很大，建议指数/商品类一律走格式一 JSON）：
>
> - **裸品种必须恰好 6 个字母**：`EURUSD` / `XAUUSD` / `XAGUSD` 等可裸写；而 5 字母的 `USOIL` / `UKOIL`、含数字的 `US30` / `US100` / `GER40` **裸写都识别不了**。
> - **标签写法 `SYMBOL=` / `TICKER=`** 要求标签后以 3-6 个字母开头（其后可再跟数字）：
>   - ✅ `SYMBOL=GER40` / `TICKER=GER40`、`SYMBOL=USOIL`、`SYMBOL=UKOIL` 能识别（`GER`/`USOIL`/`UKOIL` 满足「≥3 字母开头」）。
>   - ❌ `SYMBOL=US30` / `SYMBOL=US100` 不行（`US` 只有 2 个字母）；**更坑的是** 此时「SYMBOL」这个词本身因为恰好是 6 个字母，会被「裸 6 字母」规则误命中，解析出 `symbol="SYMBOL"` 的垃圾值（仍会当成一次有效信号下发）。
> - 结论：指数 `US30` / `US100` 在纯文本下**永远拿不到正确品种**，必须用 JSON `symbol` 字段；`GER40` / `USOIL` / `UKOIL` 只能用 `SYMBOL=` / `TICKER=` 标签、不能裸写。

---

## 6. 格式三：JSON 内嵌文本字段（回退）

当 JSON 对象**结构化解析失败**，但包含 `text` / `message` / `body` 任一字段时，取该字段的字符串值，按[格式二](#5-格式二纯文本告警)再解析一次。

```json
{"text": "buy EURUSD"}
```

```json
{"message": "close XAUUSD"}
```

```json
{"body": "SYMBOL=GBPUSD sell LOT=0.1"}
```

> 回退触发条件是「结构化解析返回 None」。因此只要 JSON 取不全 `action`+`symbol`，**哪怕你写了 `action`/`symbol` 字段**，只要同时带了 `text`/`message`/`body`，就会改用文本字段的内容。例如 `{"action":"buy","text":"sell GBPUSD"}` 最终按文本得到 **SELL GBPUSD**（JSON 里的 `action:buy` 被忽略）。优先级：`text` > `message` > `body`。

---

## 7. 支持的品种清单

映射表为恒等映射（TradingView 名 = MT5 名）。未命中的品种在 JSON 模式下会**原样透传**。

| 类别 | 品种 |
| --- | --- |
| 外汇 | `EURUSD` `GBPUSD` `USDJPY` `USDCHF` `AUDUSD` `USDCAD` `NZDUSD` `EURGBP` `EURJPY` `GBPJPY` |
| 商品 | `XAUUSD` `XAGUSD` `USOIL` `UKOIL` |
| 指数 | `US100` `US30` `GER40` |

---

## 8. 动作关键字清单

取值做**子串**匹配（大小写不敏感），优先级 **CLOSE > BUY > SELL**。

| 动作 | 关键字 |
| --- | --- |
| `BUY` | `buy` `long` `做多` `买入` `多` |
| `SELL` | `sell` `short` `做空` `卖出` `空` |
| `CLOSE` | `close` `exit` `平仓` `平` `close_all` |

> 因为是子串匹配，短词（如「多」「空」「平」）可能被更长的词命中（如「做多」含「多」）；这是与参考仓库对齐的预期行为。

---

## 9. 响应格式

成功 / 拒收 / 去重响应都会带 `model`（`normal` 或 `strategy`），表示本条信号走的是哪条分发链路。`mode` 的取值因链路而异，不要用 normal 的 `sync`/`poll`/`close` 去解读 strategy 响应。

### 9.1 接收成功 `200`（`model=normal`）

```json
{
  "status": "accepted",
  "signal_id": "sig_18f...",
  "model": "normal",
  "action": "BUY",
  "symbol": "EURUSD",
  "volume": 0.1,
  "mode": "sync",
  "targets": 3
}
```

- `mode`：`sync`（全员同步）/ `poll`（轮询轮转）/ `close`（平仓广播）。
- `targets`：`sync`/`close` 为本次命中的在线节点数；`poll` 为参与该品种轮转的候选节点数（实际只会由其中 1 个节点领取消费）。

### 9.1.1 接收成功 `200`（`model=strategy`）

策略开仓分发成功时顶层 `mode` 固定为 `group`（不是组内的 `sync`/`poll`）；组内分发模式写在 `tasks[].dispatch_mode`：

```json
{
  "status": "accepted",
  "signal_id": "sig_18f...",
  "model": "strategy",
  "action": "BUY",
  "symbol": "XAUUSD",
  "volume": 0.1,
  "mode": "group",
  "groups": 1,
  "targets": 2,
  "tasks": [
    {
      "group_id": "grp_3f2a9c1b8d7e6054",
      "group_name": "金网格组",
      "dispatch_mode": "sync",
      "task_id": 42,
      "targets": 2,
      "status": "dispatching",
      "reason": null
    }
  ]
}
```

- `groups`：本次实际处理的分组数（每组一条主任务摘要）。
- `targets`：各分组 `tasks[].targets` 之和（成功下发到节点的次数合计）。
- `tasks[].status` 常见值：`dispatching`（已下发）/ `skipped`（组内无有效节点、节点均忙等）/ `failed`（主任务创建失败或连接不可用等）。
- 开启了分组趋势风控时，被拦节点记为子任务 `skipped`（中文 `skip_reason`）；Webhook 顶层仍可能是 `accepted`（其它节点或其它分组已发出），需在分组信号页核对。

策略 `CLOSE` 终止成功时顶层 `mode` 为 `group_close`，结构同样含 `groups` / `targets` / `tasks`（任务侧多为 `closing`）。

无匹配分组（开仓）或无匹配活动子任务（CLOSE）时返回 §9.3.1 的 `rejected`，而不是本节的 `accepted`。

### 9.2 重复信号 `200`（被去重抑制）

```json
{"status": "duplicate", "model": "normal", "action": "BUY", "symbol": "EURUSD"}
```

### 9.3 品种未登记 / 已禁用 `200`（拒收，**仅 `model=normal`**）

**仅按币种链路**会做此校验。解析成功，但 `symbol` **未在中控台**（`GET/PUT /api/config/filters`）登记，或已登记但 **`enabled=false`**（取消「启用」）时返回；开仓与平仓（CLOSE）均拒收（后台手动平仓不受影响）。

`model=strategy` **不读**中控台品种清单：未登记的品种只要有匹配分组仍可分发；策略侧的拒收见 §9.3.1。

```json
{
  "status": "rejected",
  "signal_id": "sig_18f...",
  "model": "normal",
  "action": "BUY",
  "symbol": "EURUSD",
  "volume": 0.1,
  "mode": "rejected",
  "targets": 0,
  "reason": "品种未配置：EURUSD未在中控台配置，信号拒收"
}
```

禁用时 `reason` 形如：`品种已禁用：EURUSD在中控台未启用，信号拒收`。

- HTTP 仍为 `200`（与 `duplicate` 相同）；TradingView 若只校验 2xx 会显示投递成功，需在管理端核对。
- 处理：Web「中控台」→ 添加该品种并勾选「启用」→ 保存过滤规则。

### 9.3.1 策略无匹配 `200`（拒收，`model=strategy`）

开仓找不到入选分组，或 CLOSE 找不到匹配的活动子任务时：

```json
{
  "status": "rejected",
  "signal_id": "sig_18f...",
  "model": "strategy",
  "action": "BUY",
  "symbol": "XAUUSD",
  "volume": 0.1,
  "mode": "rejected",
  "targets": 0,
  "groups": 0,
  "reason": "无匹配分组：…",
  "tasks": []
}
```

`reason` 会串联落选说明（如指定分组不存在、模版定向未命中、模版2 缺 `sl`、模版3 方向不相容等）。HTTP 仍为 `200`。

### 9.4 持久化与查询（v0.4）

每次 Webhook 请求（含解析失败）都会尽力写入 `signal_history.raw_payload`（原始 JSON 或纯文本），供管理端 **「事件」页**（`GET /api/events/signals`）与节点详情 Tab 追溯。解析失败时 `parsed_ok=false`、`status=rejected`，仍保留原始体。

### 9.5 错误响应

| 状态码 | body | 触发条件 |
| --- | --- | --- |
| `400` | `{"detail":"cannot parse signal"}` | 无法解析出有效信号（解析器返回 `None`） |
| `400` | `{"detail":"invalid signal: ..."}` | 解析成功但校验不通过（见 §11，**默认配置下基本不会触发**） |
| `400` | `{"detail":"invalid model: ..."}` | `model` 不是 `normal` / `strategy` |
| `400` | `{"detail":"template_ids 仅适用于 model=strategy 的信号"}` | 带了 `template_ids` 却不是策略信号（见 §4.5） |
| `400` | `{"detail":"group_ids 仅适用于 model=strategy 的信号"}` | 带了 `group_ids` 却不是策略信号（见 §4.5） |
| `400` | `{"detail":"unknown template_ids: ..."}` | `template_ids` 里有未登记的模版 ID（见 §4.5；`group_ids` 不做此校验） |
| `401` | `{"detail":"invalid token"}` | 开启鉴权且 token 不匹配 |
| `403` | `{"detail":"ip not allowed"}` | 开启白名单且来源 IP 不在名单 |
| `503` | `{"detail":"service not ready"}` | `model=strategy` 但分组分发引擎尚未就绪（进程启动中等短暂状态） |

> 关于 `invalid signal`：解析器自身已经保证 `action`/`symbol` 非空、`volume` 回退到 `DEFAULT_LOT`（>0）、`sl`/`tp` 只会是 `None` 或正数。因此**只要 `DEFAULT_LOT>0`（默认 0.1），解析器产出的信号必然通过校验**，这条 400 实际是一道安全网（仅当把 `DEFAULT_LOT` 配成 0 之类的极端情况才可能命中）。

---

## 10. 去重机制

在 `DEDUP_WINDOW` 秒（默认 `5`）内，指纹完全相同的信号视为重复，直接返回 `duplicate` 不再分发。

指纹 = `model : action : symbol : volume : stop_loss : take_profit : template_ids : group_ids`。

> 指纹带上 `model` 与两个定向字段：同一笔行情面向不同链路、不同分组的信号是各自独立的指令，不能互相当成重复抑制掉。

---

## 11. 校验规则

解析成功后还会做基本校验，不通过返回 `400 invalid signal`：

- `action` 非空
- `symbol` 非空
- `volume > 0`
- 若设置了 `stop_loss`，必须 `> 0`
- 若设置了 `take_profit`，必须 `> 0`

---

## 12. 不被支持 / 常见坑

| 输入 | 结果 | 原因 |
| --- | --- | --- |
| `{"signal": "buy EURUSD 0.1"}` | ❌ `400` | `signal` 仅用于取动作；无 `symbol` 字段、也无 `text/message/body`，取不到品种 |
| `{"action": "buy"}` | ❌ `400` | 缺 `symbol` |
| `{"symbol": "EURUSD"}` | ❌ `400` | 缺 `action` |
| `"hello world"` | ❌ `400` | 文本中无动作关键字 |
| `["buy","EURUSD"]` / `123` / `true` / `null` | ❌ `400` | 非字符串、非对象，不支持 |
| 空请求体 | ❌ `400` | 按空对象 `{}` 处理，取不到 action/symbol |
| 纯文本里裸写 `US30` / `US100` / `GER40` / `USOIL` / `UKOIL` | ❌ | 裸品种必须恰好 6 字母，请改用 JSON `symbol` 字段（或对 `GER40`/`USOIL`/`UKOIL` 用 `SYMBOL=` 标签） |
| 纯文本 `SYMBOL=US30 buy` | ⚠️ 解析出 `symbol="SYMBOL"` | `US` 仅 2 字母不满足标签正则，「SYMBOL」一词被裸 6 字母规则误命中（见 §5 坑位） |
| `{"action":"buy","symbol":"EURUSD","allow_position":"true"}` | ⚠️ `allow_position=false` | `"true"` 是字符串，`int("true")` 抛错；只有能转非零整数的值才生效（见 §4.3） |
| `{"action":"buy","symbol":"EURUSD","allow_position":2}` | ⚠️ `allow_position=true` | 任意非零整数都为 `true`，不止 `1`（见 §4.3） |
| `{"action":"buy","symbol":"XAUUSD","template_ids":["tpl_3"]}`（漏写 `model`） | ❌ `400` | 定向字段只对策略信号有意义；漏写 `model=strategy` 直接拒收，避免误按币种链路广播下单（见 §4.5） |
| `{"action":"buy","symbol":"XAUUSD","group_ids":["grp_..."]}`（漏写 `model`） | ❌ `400` | 同上 |
| `{"model":"strategy","action":"buy","symbol":"XAUUSD","template_ids":["tpl_3"]}`（无 tpl_3 分组） | ⚠️ `status: rejected` | 模版定向没命中任何分组，落选原因写进信号记录（见 §4.5） |
| `{"model":"strategy","action":"buy","symbol":"XAUUSD","group_ids":["grp_已删除"]}` | ⚠️ `status: rejected` | 分组不存在不算 400，原因写成「指定的分组不存在」（见 §4.5） |
| `{"action":"buy","symbol":"NZDUSD"}`（中控台未登记 NZDUSD） | ⚠️ `status: rejected` | **仅 `model=normal`**：解析成功但品种未在中控台登记，不分发（见 §9.3） |
| `{"action":"close","symbol":"EURUSD"}`（中控台已取消启用 EURUSD） | ⚠️ `status: rejected` | **仅 `model=normal`**：品种已禁用，开仓/平仓均不分发（见 §9.3；手动平仓除外） |

---

## 13. TradingView 配置示例

### 13.1 告警消息（Alert message，推荐 JSON）

```json
{"action": "{{strategy.order.action}}", "symbol": "{{ticker}}", "volume": 0.1}
```

若开启了 token 鉴权，可把 token 放进消息体：

```json
{"action": "{{strategy.order.action}}", "symbol": "{{ticker}}", "volume": 0.1, "token": "你的AUTH_TOKEN"}
```

> Webhook URL 填 `http(s)://你的域名/webhook`。`{{strategy.order.action}}` 会输出 `buy`/`sell`，命中关键字表。

### 13.2 纯文本告警

```text
buy EURUSD VOLUME=0.1
```

---

## 14. curl 自测示例

JSON（无鉴权）：

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"action":"buy","symbol":"EURUSD","volume":0.1}'
```

纯文本：

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: text/plain" \
  --data-raw 'close XAUUSD'
```

带 token（Header 方式）：

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: 你的AUTH_TOKEN" \
  -d '{"action":"sell","symbol":"XAUUSD","volume":0.2,"sl":2300,"tp":2400}'
```

---

## 15. 相关环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENABLE_AUTH` | `false` | 是否校验 Webhook token |
| `AUTH_TOKEN` | `""` | Webhook 共享 token |
| `ENABLE_IP_WHITELIST` | `false` | 是否启用 IP 白名单 |
| `WHITELISTED_IPS` | TradingView 出口 IP | 逗号分隔的 IP 名单 |
| `DEDUP_WINDOW` | `5` | 信号去重窗口（秒） |
| `DEFAULT_LOT` | `0.1` | 默认手数 |
| `MAX_LOT_SIZE` | `1.0` | 单笔最大手数（封顶） |

---

> 行为基准：`backend/app/webhook.py`（鉴权/去重/响应/**raw_payload 持久化**）、`backend/app/parser.py`（解析）、`backend/app/config.py`（品种与关键字）、`backend/app/settings.py`（环境变量）、`backend/app/persist.py`（`recent_webhook_events`）、`backend/app/group_dispatcher.py`（`model=strategy` 分发与趋势风控挂点）。
>
> 全场景回归测试：
> - `backend/tests/test_parser.py` —— 纯解析层（动作/品种/手数/止盈止损/`allow_position` 精确规则/`template_ids` 与 `group_ids` 归一化/文本模式坑位/格式三回退/校验规则）。
> - `backend/tests/test_webhook.py` —— HTTP 端到端（token 4 种传入方式、IP 白名单与 `X-Forwarded-For`、白名单→鉴权→解析的顺序、三种请求体形态、去重、**未登记品种 rejected**、定向字段校验、各类 400/401/403、响应字段）。
> - `backend/tests/test_group_dispatch.py` —— 策略链路分发、模版准入、定向落选、趋势风控跳过等。
> - `backend/tests/test_api.py` —— 含 webhook→分发→节点回报的全链路冒烟，以及 **`GET /api/events/signals`** 分页与 `raw_payload` 断言。
>
> 运行：`cd backend && python -m pytest tests/test_parser.py tests/test_webhook.py tests/test_group_dispatch.py -q`
