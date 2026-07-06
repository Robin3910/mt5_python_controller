# Webhook 入参文档

TradingView → MT5 跟单系统的信号接收接口说明。本文档描述的是**代码的真实行为**（以 `backend/app/webhook.py` + `backend/app/parser.py` + `backend/app/config.py` 为准），而非 README 的宣传性描述。

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
| 手数 `volume` | `volume` / `lotsize` / `lot` / `v` / `q` | 缺省或 ≤0 或非法 → `DEFAULT_LOT`（默认 `0.1`）；超过 `MAX_LOT_SIZE`（默认 `1.0`）则封顶 |
| 止损 `stop_loss` | `sl` / `stoploss` / `stop_loss` / `stop` | 解析为正浮点；`0`/空/非法 → 不设（`null`） |
| 止盈 `take_profit` | `tp` / `takeprofit` / `take_profit` / `target` | 解析为正浮点；`0`/空/非法 → 不设（`null`） |
| 备注 `comment` | `comment` | 字符串，默认 `""` |
| 订单类型 `order_type` | `type` / `ordertype` | 转小写，默认 `"market"` |
| 允许加仓 `allow_position` | `allow_position` / `allowposition` / `position_allowed` | 取值能被 `int()` 解析为**非零整数**时为 `true`，否则 `false`（精确规则见下方） |

> 注：这里的 `volume` 仅是「信号携带手数」。最终每个节点实际下单手数还取决于该节点的手数策略（跟随全局 / 固定 / 跟随信号），见节点配置。

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

---

## 5. 格式二：纯文本告警

当请求体不是合法 JSON 时按文本解析。**只提取 action / symbol / volume / sl / tp**，不支持 comment、order_type、allow_position。

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

### 9.1 接收成功 `200`

```json
{
  "status": "accepted",
  "signal_id": "sig_18f...",
  "action": "BUY",
  "symbol": "EURUSD",
  "volume": 0.1,
  "mode": "sync",
  "targets": 3
}
```

- `mode`：`sync`（全员同步）/ `poll`（轮询领取）/ `close`（平仓广播）。
- `targets`：本次分发命中的在线节点数。

### 9.2 重复信号 `200`（被去重抑制）

```json
{"status": "duplicate", "action": "BUY", "symbol": "EURUSD"}
```

### 9.3 品种未登记 `200`（拒收，不分发）

解析成功，但 `symbol` **未在中控台**（`GET/PUT /api/config/filters`）登记时返回：

```json
{
  "status": "rejected",
  "signal_id": "sig_18f...",
  "action": "BUY",
  "symbol": "EURUSD",
  "volume": 0.1,
  "mode": "rejected",
  "targets": 0,
  "reason": "品种未配置：EURUSD未在中控台配置，信号拒收"
}
```

- HTTP 仍为 `200`（与 `duplicate` 相同）；TradingView 若只校验 2xx 会显示投递成功，需在管理端核对。
- 处理：Web「中控台」→ 添加该品种 → 保存过滤规则。

### 9.4 错误响应

| 状态码 | body | 触发条件 |
| --- | --- | --- |
| `400` | `{"detail":"cannot parse signal"}` | 无法解析出有效信号（解析器返回 `None`） |
| `400` | `{"detail":"invalid signal: ..."}` | 解析成功但校验不通过（见 §11，**默认配置下基本不会触发**） |
| `401` | `{"detail":"invalid token"}` | 开启鉴权且 token 不匹配 |
| `403` | `{"detail":"ip not allowed"}` | 开启白名单且来源 IP 不在名单 |

> 关于 `invalid signal`：解析器自身已经保证 `action`/`symbol` 非空、`volume` 回退到 `DEFAULT_LOT`（>0）、`sl`/`tp` 只会是 `None` 或正数。因此**只要 `DEFAULT_LOT>0`（默认 0.1），解析器产出的信号必然通过校验**，这条 400 实际是一道安全网（仅当把 `DEFAULT_LOT` 配成 0 之类的极端情况才可能命中）。

---

## 10. 去重机制

在 `DEDUP_WINDOW` 秒（默认 `5`）内，指纹完全相同的信号视为重复，直接返回 `duplicate` 不再分发。

指纹 = `action : symbol : volume : stop_loss : take_profit`。

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
| `{"action":"buy","symbol":"NZDUSD"}`（中控台未登记 NZDUSD） | ⚠️ `status: rejected` | 解析成功但品种未在中控台登记，不分发（见 §9.3） |

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

> 行为基准：`backend/app/parser.py`（解析）、`backend/app/webhook.py`（鉴权/去重/响应）、`backend/app/config.py`（品种与关键字）、`backend/app/settings.py`（环境变量）。
>
> 全场景回归测试：
> - `backend/tests/test_parser.py` —— 纯解析层（动作/品种/手数/止盈止损/`allow_position` 精确规则/文本模式坑位/格式三回退/校验规则）。
> - `backend/tests/test_webhook.py` —— HTTP 端到端（token 4 种传入方式、IP 白名单与 `X-Forwarded-For`、白名单→鉴权→解析的顺序、三种请求体形态、去重、**未登记品种 rejected**、各类 400/401/403、响应字段）。
> - `backend/tests/test_api.py` —— 含 webhook→分发→节点回报的全链路冒烟。
>
> 运行：`cd backend && python -m pytest tests/test_parser.py tests/test_webhook.py -q`
