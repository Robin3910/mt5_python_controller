# 手机号注册与验证码登录设计（阿里云短信认证）

日期：2026-11-01
状态：待用户确认
范围：手机号注册 + 手机号验证码登录（新注册用户需管理员审核后启用）

---

## 1. 目标与动机

给系统增加「用手机号注册 / 用手机号验证码登录」的能力，降低用户上手门槛（不再要求先想用户名 + 记密码）。

短信通道选用 **阿里云「号码认证服务 PNVS」下的「短信认证」子服务**（API：`dypnsapi.aliyuncs.com`，SDK：`alibabacloud_dypnsapi20170525`）。

**已确认的范围决策：**

| 决策点 | 结论 |
|---|---|
| 功能范围 | 手机号注册 **+** 手机号验证码登录（不含自助找回密码、不含手机号绑定/解绑） |
| 新用户审核 | **需要**管理员审核后启用（`require_approval=true`），且做成运行时可切换 |
| 厂商 | 阿里云短信认证（`SendSmsVerifyCode`） |
| 验证码归属 | **自己生成 + 自己校验**（模式 B），阿里云只负责下发；核验不走阿里云 |

**为什么选阿里云短信认证而不是普通的「阿里云短信服务」：**

| | 阿里云短信服务 SMS | 阿里云**短信认证**（本方案） |
|---|---|---|
| 资质 | 必须企业营业执照 + 实名资质报备 | **免资质**，个人实名认证即可 |
| 签名 | 自己申请，报备后才能用 | **平台赠送**，不可自定义 |
| 模板 | 自己申请（2 小时审核） | **5 个预置模板**，不可自定义 |
| 接入周期 | 10–15 工作日（运营商报备 5–7 天） | **对接 API 后当天可用** |
| 核验 | 全部自研 | 平台托管（`CheckSmsVerifyCode` 免费） |

运营商实名报备是所有「正常短信」厂商的共同瓶颈（阿里云 5–7 天、腾讯云 7–10 天、火山 5–7 天、华为 7–10 天），**换厂商换不掉这个等待**。短信认证走平台已报备的通道，是唯一能绕开的路径。

**非目标（明确不做）：**
- 不改现有 `/api/login`（账号密码 + TOTP 2FA）的任何行为。
- 不改 JWT 结构、不改 `users.username` 的主键语义、不改 RBAC / 审计 / 节点归属链路。
- 不做自助找回密码、不做已登录用户的手机号绑定/解绑（留作二期，见 §13）。
- 不做号码认证的一键登录（`GetMobile`）、不做图形验证码人机校验（留作二期，见 §13）。
- 不做国际/港澳台短信（短信认证仅支持中国大陆号码）。

---

## 2. 现状盘点

### 2.1 现有认证链路

```
POST /api/login  {username, password}
  → verify_user_password()          # hash_token + compare_secret
  → 若 user_requires_2fa()          # totp_enabled 且 totp_secret
      → 返回 {requires_2fa:true, login_token: create_2fa_pending_jwt(username)}
  → 否则 → create_jwt(username, uid, ver)  # HS256，sub=username，ver=token_version
```

### 2.2 关键模块与本次改造的关系

| 关注点 | 现状位置 | 本次改造 |
|---|---|---|
| 登录入口 | `backend/app/auth.py` | 新增独立路由文件，不动原文件 |
| 用户表 | `backend/app/orm.py` `users` | 新增 `phone` / `phone_verified` 两列 |
| 建号 | `backend/app/user_service.py::create_user()` | 直接复用 |
| 角色绑定 | `backend/app/rbac_service.py::set_user_roles()` / `default_role_id()` | 直接复用 |
| 会话令牌 | `backend/app/security.py::create_jwt()` / `create_2fa_pending_jwt()` | 复用；新增 `phone_pending` 票据类型 |
| 审计 | `backend/app/persist.py::audit()` | 复用，`category="auth"` |
| 运行时 KV | `backend/app/redis_store.py` | 新增验证码 / 限频相关方法 |
| 系统配置 | `backend/app/system_settings.py` | 新增 `sms_config` key（DB 落库 + Redis 缓存） |
| 迁移 | `backend/app/db.py` 一系列 `_migrate_xxx()` | 新增 `_migrate_user_phone()` |
| 密钥 | `backend/app/settings.py`（读 `.env`） | 新增阿里云 AccessKey 两项 |
| 前端登录 | `frontend/src/views/LoginView.vue` | 加「验证码登录」Tab |
| 前端路由 | `frontend/src/router/index.ts` | 新增 `/register` 公开路由 |

### 2.3 核心设计取向

**不改 `users.username` 的主键语义。** 手机号是新增的**唯一别名 + 登录凭据**，所有 JWT 的 `sub`、RBAC 的 `user_id`、审计的 `operator`、节点的 `owner_user_id` 仍以 `username` 为主。

- 好处：下游零改动、零回归风险。
- 代价：`users` 需同时维护 `username` 与 `phone` 两个唯一索引；`username` 仍必须存在且唯一（验证码注册时自动生成，如 `m13800138000`）。

---

## 3. 阿里云侧前置准备

> **这是全流程中唯一需要人工的部分，且当天可完成。**

### 3.1 开通路径

1. 阿里云账号完成**实名认证**（个人或企业均可，**个人认证即可开通短信认证**）。
2. 进入 **号码认证服务控制台**（`https://dypns.console.aliyun.com`）→ 开通「短信认证」功能。
3. 创建 **RAM 用户**并授予最小权限（见 §3.5），生成 AccessKey ID / Secret。
4. 在「短信认证参数配置」页面查 **赠送签名** 与 **赠送模板** 的实际值（控制台只在这里显示，代码里不能硬编码猜测）。

### 3.2 赠送签名

- 短信认证**只提供平台赠送的签名，不可自定义、不可修改**。
- 企业认证账号的赠送签名通常就是你的企业名或商标名；**个人认证账号的赠送签名是平台统一的名字**（品牌感弱，需业务方确认是否接受）。
- 赠送签名与赠送模板**必须配套使用，不可交叉**（自定义签名 + 赠送模板、或赠送签名 + 自定义模板都用不了）。

### 3.3 赠送模板（5 个，固定 Code）

| TemplateCode | 用途 | 本方案是否使用 |
|---|---|---|
| `100001` | 登录/注册 | ✅ **注册与验证码登录都用这个** |
| `100002` | 修改绑定手机号 | 二期（绑定/解绑） |
| `100003` | 重置密码 | 二期（自助找回密码） |
| `100004` | 绑定新手机号 | 二期 |
| `100005` | 验证绑定手机号 | 二期 |

> `register` 与 `login` 共用 `100001` 不影响业务：两种用途的**验证码本身是不同的**（Redis key 里带 `purpose`），模板文案相同无妨。

**模板变量**：赠送的登录/注册模板形如
```
您的验证码是${code}，有效期${min}分钟，请勿告诉他人。
```
因此 `TemplateParam` 需传 `{"code": "<验证码>", "min": "<分钟数>"}` —— **两个变量，key 固定为 `code` 和 `min`**。`min` 只填数字（`"5"`，不能是 `"5分钟"`）。

### 3.4 验证码生成策略：**模式 B（自己生成 + 自己校验）**

阿里云 `SendSmsVerifyCode` 支持两种验证码来源：

| 模式 | TemplateParam | 核验方式 | 评价 |
|---|---|---|---|
| A. 阿里云生成 | `{"code":"##code##","min":"5"}` | `CheckSmsVerifyCode`（免费） | 拿不到明文；无法区分 purpose；无法自定义失败次数上限 |
| **B. 自己生成** ✅ | `{"code":"123456","min":"5"}` | **自己比对**（本方案） | 保住 purpose 隔离、attempts 限制、自定义有效期 |

**本方案选 B。** 理由：
1. `register` / `login` 需要验证码**用途隔离**（防止用登录验证码去注册）。
2. 需要「5 次错误作废」和自定义有效期的能力。
3. 核验逻辑本来就要写（Redis + 哈希 + TTL），模式 A 只是省事但牺牲业务语义。
4. 将来若切回普通短信服务，逻辑完全一致。

> **模式 B 的必然代价**：传自定义 `code` 时，`CheckSmsVerifyCode` **不支持**核验，必须自己比对。这正是模式 B 的设计前提。
> 另外 `CodeType` / `CodeLength` 在模式下 B 无意义（码是我们自己生成的），不传即可。

### 3.5 RAM 权限与密钥

- 建 **RAM 用户**，只授予 `dypnsapi:SendSmsVerifyCode`（不含模板管理、费用管理）。
- `AccessKey ID` / `AccessKey Secret` 只从环境变量注入，**不入库、不进仓库**。
- 生产环境用 K8s Secret / 密钥管理服务注入；更安全的做法是临时 STS 凭证。
- SDK client **单例复用**（`lru_cache`），避免每次请求重新握手。

### 3.6 计费

**按量付费**（号码认证服务）：

| 使用量阶梯（次/月） | 短信认证单价（元/次） |
|---|---|
| ≤ 10 万 | 0.042 |
| 10 万 < 量 ≤ 500 万 | 0.040 |
| 500 万 < 量 ≤ 1000 万 | 0.038 |
| > 1000 万 | 0.036 |

> 官方选型指南另标注「≤1000 次档位 0.06 元/次起」。**具体以控制台实时价格为准。**

**套餐包**（更划算，推荐）：

| 套餐 | 短信认证套餐价 | 折合单价 |
|---|---|---|
| 100 次 | 5 元 | 0.05 元/次 |
| 1 千次 | 50 元 | 0.05 元/次 |
| 5 万次 | 2000 元 | 0.04 元/次 |
| 10 万次 | 3900 元 | 0.039 元/次 |

套餐包有效期 **12 个月**（不可延期），购买后**不支持自助退费**（需提工单）。

**计费规则**：
- **按运营商回执状态计费** —— 短信提交成功但运营商回执失败**不计费**；核验服务免费。
- **号码认证服务与短信服务的套餐包不互通、不支持转换**。别买错产品的套餐包。

---

## 4. 总体架构

```
前端 (Vue3)
  RegisterView.vue  /  LoginView.vue（双 Tab）
        │  POST /api/auth/sms/send  {phone, purpose}
        ▼
┌───────────────────────────────────────────────────┐
│ backend/app/sms_provider.py    ← 厂商抽象层        │
│   class SmsProvider (Protocol)                    │
│     send_code(phone, code, ttl_minutes) -> SmsResult│
│   ├─ AliyunSmsProvider  (SendSmsVerifyCode)       │──► 阿里云 dypnsapi
│   ├─ TencentCloudSmsProvider (SendSms，留作切换)    │──► 腾讯云 SMS
│   └─ FakeSmsProvider（测试用，落内存/日志）         │
├───────────────────────────────────────────────────┤
│ backend/app/sms_service.py     ← 业务编排          │
│   · 号码归一化 / 格式校验                          │
│   · 多维限频（Redis 计数）                         │
│   · 验证码生成（secrets）+ 加盐哈希 + 存 Redis      │
│   · 调用 provider + 落审计                        │
├───────────────────────────────────────────────────┤
│ backend/app/phone_auth.py      ← 注册 / 登录编排     │
│   · /api/auth/sms/send                          │
│   · /api/auth/sms/verify                         │
│   · /api/auth/register                           │
│   · /api/auth/sms/login                          │
├───────────────────────────────────────────────────┤
│ backend/app/auth.py / users_api.py / user_service.py │
│   复用 create_user / rbac / audit / create_jwt     │
└───────────────────────────────────────────────────┘
        │  Redis: sms:code:* / sms:send:* / sms:sendip:* / sms:lock:*
        │  MySQL: users.phone / users.phone_verified
```

**分层理由：**
- `sms_provider` 是**唯一的厂商耦合点**。选阿里云是因为它能当天上线，不是因为阿里云更好 —— 抽象层的价值正在于：将来若因业务需要（要发通知短信、要自定义品牌签名、要合并结算）切到普通短信服务或腾讯云，**只改这一个文件**。
- `sms_service` 承载限频与验证码生命周期，与「注册还是登录」的业务语义解耦。
- `phone_auth` 只做编排，不碰具体存储细节。
- 单元测试注入 `FakeSmsProvider`，**不真发短信**（省钱、也不依赖阿里云账号配置）。

---

## 5. 数据模型

### 5.1 `users` 表新增两列

```python
# backend/app/orm.py — User
phone: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True, index=True)
phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
```

- `phone` 存**归一化后的 E.164**：`+8613800138000`（带 `+86`，避免号码歧义）。
- 存量用户 `phone = NULL`；MySQL / SQLite 的 unique 索引都允许多个 NULL → **存量零影响**。
- `phone_verified`：管理员手工绑定时置 `False`（未走短信验证）；注册路径置 `True`。

### 5.2 迁移函数

在 `backend/app/db.py` 新增 `_migrate_user_phone(sync_conn)`，并追加到 `init_db()` 的调用链。写法参照既有的 `_migrate_users_follow_paused`（MySQL 用 `TINYINT(1) NOT NULL DEFAULT 0`，SQLite 用 `BOOLEAN NOT NULL DEFAULT 0`）。

```python
def _migrate_user_phone(sync_conn) -> None:
    """users.phone / users.phone_verified：手机号登录凭据。

    phone 存 E.164（+86xxxxxxxxxxx），唯一；存量用户为 NULL 不受影响。
    phone_verified：是否已通过短信验证（管理员手工绑定时为 False）。
    """
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    dialect = sync_conn.engine.dialect.name
    if "phone" not in cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
        # 唯一索引：先扫重（存量全 NULL 不会冲突），再建
        sync_conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_phone ON users (phone)"))
    if "phone_verified" not in cols:
        col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
        sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN phone_verified {col_type}"))
```

> MySQL 不支持 `CREATE UNIQUE INDEX IF NOT EXISTS`，需按方言分支（项目内 `_migrate_node_group_strategy_id` 已有此模式）。

### 5.3 Redis 键设计

| Key | 类型 | TTL | 用途 |
|---|---|---|---|
| `sms:code:{purpose}:{phone}` | JSON `{code_hash, attempts, issued_at}` | 300s | 验证码（**只存哈希，不存明文**） |
| `sms:send:{phone}:{YYYYMMDD}` | int 计数 | 当日剩余 | 每手机号每日发送上限 |
| `sms:sendip:{ip}:{hour}` | int 计数 | 3600s | 每 IP 每小时上限 |
| `sms:lock:{phone}` | `"1"` | 60s | 发送冷却，防连点 |

**`purpose` 进 key 是必须的** —— 这正是选择模式 B 而非模式 A 的核心原因（§3.4）。

**票据用 JWT 而非 Redis**：照抄 `create_2fa_pending_jwt()` 模式，`typ="phone_pending"`、`sub=phone`、`purpose`、`exp=600`，无状态、少一次 IO。Redis 只留验证码本体。

### 5.4 配置

**环境变量**（`backend/app/settings.py`，只放密钥）：

```python
aliyun_access_key_id: str = ""
aliyun_access_key_secret: str = ""
```

**数据库配置**（`system_settings.py`，新增 `KEY_SMS_CONFIG = "sms_config"`，值为 JSON）：

```json
{
  "enabled": true,
  "provider": "aliyun_sms_auth",
  "endpoint": "dypnsapi.aliyuncs.com",
  "sign_name": "<控制台的赠送签名内容>",
  "template_code": "100001",
  "require_approval": true,
  "code_ttl_seconds": 300,
  "send_interval_seconds": 60,
  "daily_limit_per_phone": 10,
  "hourly_limit_per_ip": 20,
  "max_verify_attempts": 5
}
```

- **密钥永远不进 DB**，只从 env 读；配置页只回显 `sign_name` / `template_code`。
- 开关与模板 Code 放 DB，是为了让运维能**临时关停注册**（出故障立刻止血）、以及不重启就能换模板。
- `provider` 字段是为将来切换厂商预留的（`aliyun_sms_auth` / `tencentcloud_sms`），切换只需改这一个配置项。
- `require_approval` 默认 `true`（已确认的需求），运行时可切换为 `false` 实现免审核注册。
- 需提供「配置缺失时的默认值」：全部字段有默认值；`sign_name` 或 `template_code` 为空时视为**未配置** → 所有短信接口返回 404（前端自动隐藏验证码入口）。

---

## 6. 接口设计

新文件 `backend/app/phone_auth.py`，`router = APIRouter(prefix="/api/auth", tags=["auth"])`，公开无鉴权。

### 6.1 发送验证码

```
POST /api/auth/sms/send
{ "phone": "13800138000", "purpose": "register" | "login" }
→ 200 { "ok": true, "expire_in": 300, "cooldown": 60 }
```

- `purpose` 枚举校验：`register` / `login`。
- **无论手机号是否已注册，响应体与状态码完全一致**（见 §8.3 号码枚举防护）。
- 限频命中 → `429` + 文案「操作过于频繁，请 N 秒后再试」。
- 未配置 / 已关闭 → `404`（不暴露原因）。

### 6.2 校验验证码

```
POST /api/auth/sms/verify
{ "phone": "13800138000", "code": "123456", "purpose": "register" }
→ 200 { "ticket": "<JWT typ=phone_pending exp=600>" }
```

- 校验失败 → `400`「验证码错误或已过期」；`attempts += 1`；达 5 次立即删除该验证码。
- 校验通过 → **立即删除验证码**（一次性）+ 签发票据。
- 票据含 `purpose`，用 register 票据调 login 用途会被拒（§12.1 用例）。

### 6.3 注册

```
POST /api/auth/register
{
  "phone": "13800138000",
  "ticket": "<jwt>",
  "nickname": "张三",            // 可选，display_name
  "username": "m13800138000",    // 可选，不填自动生成
  "password": "..."              // 可选，不填则生成随机强密码、只能验证码登录
}
→ 201 { "ok": true, "pending_approval": true, "user_id": 123 }
```

处理流程：
1. 验票据：`typ=phone_pending`、`sub == phone`、`purpose == "register"`、未过期 → 否则 `401`。
2. `users.phone` 未被占用 → 否则 `409`「该手机号已注册，请直接登录」。
3. 生成 `username`：用户未填时用 `m{手机号}`；被占用则回退 `m{手机号}_{uid短码}`；仍冲突 → `409`「该用户名已被占用，请自定义」。
4. `user_service.create_user(username, password or 随机强密码, display_name=nickname)`。
5. `rbac_service.set_user_roles(user.id, [default_role_id()])`——复用 `users_api.create_user` 的默认角色逻辑（内置「普通用户」）。
6. 回填 `users.phone = phone`、`phone_verified = True`。
7. **`require_approval=true` 时**：`is_active=False`，**不签发 token**，返回 `{"ok":true,"pending_approval":true}`。前端提示「注册成功，请等待管理员审核后登录」。
8. `require_approval=false` 时：签发 JWT（复用 `create_jwt(username, uid, ver)`），返回 `{"token":..., "token_type":"bearer", "requires_2fa":false}`。
9. `persist.audit(operator=phone, action="phone_register", category="auth")`。

**密码缺省策略**：未填密码时生成随机强密码（`secrets.token_urlsafe(24)`），用户只能走验证码登录。`/api/change-password` 需要当前密码，因此该用户在管理员重置密码前无法设置新密码——这是可接受的（见 §12 用例）。

### 6.4 验证码登录

```
POST /api/auth/sms/login
{ "phone": "13800138000", "code": "123456" }
→ 200 { "token": "...", "token_type": "bearer", "requires_2fa": false }
```

流程：校验验证码 → 删码 → 查 `users by phone` → 查 `is_active` → 签 JWT。

- 手机号未注册 → `404`「该手机号尚未注册」。
- 用户被禁用（`is_active=false`）→ `403`「账号已被禁用，请联系管理员」。
- `totp_enabled=true` 的用户 → **仍走现有 2FA 第二步**：返回 `{"requires_2fa": true, "login_token": "<2fa_pending jwt>"}`，前端复用 `LoginView` 现有的 2FA 分支。不降低安全等级。
- `require_approval=true` 时新用户 `is_active=false` → 走上面的 403 分支。

### 6.5 管理员审核

复用现有 `users_api` 能力，**不新增接口**：
- `GET /api/users` → 列表里能看到新用户（`is_active=false`）。
- `PATCH /api/users/{id}` `{"is_active": true}` → 启用，用户即可验证码登录。
- `DELETE /api/users/{id}` → 拒绝（名下无资源时可以删）。

> **需在 `UserOut` 上补充 `phone`（脱敏）与 `phone_verified` 两个字段**，前端用户管理页才能展示「待审核用户」并显示绑定手机号。

### 6.6 接口汇总

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/auth/sms/send` | 公开 | 发验证码 |
| POST | `/api/auth/sms/verify` | 公开 | 校验验证码 → 票据 |
| POST | `/api/auth/register` | 公开 | 注册（需票据） |
| POST | `/api/auth/sms/login` | 公开 | 验证码登录 |

---

## 7. 核心流程时序

**注册（含审核）：**
```
用户填手机号
  → POST /api/auth/sms/send {purpose: register}
      → 号码校验 → 限频检查 → 生成 6 位码 → Redis 存哈希(TTL 300)
      → AliyunSmsProvider.send_code()   # SendSmsVerifyCode
      → audit(sms_send, ok/fail, request_id)
  → 阿里云回执 Code=OK（仅表示受理成功，不代表送达）
  → 用户填码 → POST /api/auth/sms/verify → ticket（验证码即焚）
  → 用户填昵称/密码 → POST /api/auth/register
      → 建号 + 绑默认角色 + 绑手机号 + is_active=False
      → audit(phone_register)
  → 返回 pending_approval=true → 前端提示等待审核
  → 管理员在用户管理页启用 → 用户用验证码登录
```

**验证码登录：**
```
POST /api/auth/sms/send {purpose: login}
  → POST /api/auth/sms/login {phone, code}
      → 校验并焚毁验证码 → 查 user by phone → 查 is_active
      → 签 JWT（2FA 用户走两步）
```

---

## 8. 安全设计

### 8.1 验证码本身

- 6 位纯数字，用 `secrets.randbelow(10**6)` 而非 `random`。
- 存 `sha256(code + server_salt)`，**不存明文**（与项目现有 `hash_token` 风格一致），校验用 `compare_secret` 常量时间比较。
- TTL 5 分钟，**一次性使用**（校验通过即删）。
- 最多 5 次错误尝试，超限立即作废，需重新获取。

### 8.2 限频（多维）

| 维度 | 默认阈值 | 作用 |
|---|---|---|
| 同手机号发送间隔 | 60s | 防连点 |
| 同手机号每日发送 | 10 条 | 防刷费 |
| 同 IP 每小时发送 | 20 条 | 防批量轰炸 / 脚本 |
| 同手机号每日校验失败 | 5 次 | 防爆破 |
| 阿里云侧配额告警 | 日/月阈值 | 控成本兜底 |

超限返回 `429` + 明确文案（"操作过于频繁，请 60 秒后再试"）。

> 阿里云 `SendSmsVerifyCode` 本身也支持 `Interval`（发送间隔，默认 60 秒）与 `DuplicatePolicy`（默认 1 = 覆盖旧码）——本方案**在自己这层也做限频**，不依赖厂商的默认行为（厂商行为可配置且不保证）。

### 8.3 号码枚举防护

- 发送接口对「已注册 / 未注册」返回**完全相同**的响应体与状态码。
- 差异只在**验证码校验通过之后**才体现——此时请求方已证明持有该号码，泄露无害。
- `register` 阶段若号码已占用 → `409`「该手机号已注册，请直接登录」；这是可接受的，因为此时已持有有效验证码。

### 8.4 密钥与权限

- RAM 子账号，仅授予 `dypnsapi:SendSmsVerifyCode`。
- `AccessKey ID` / `AccessKey Secret` 只从 env 读；生产环境由 K8s Secret / 密钥管理服务注入，**不入 `.env` 文件、不进仓库**。
- SDK client **单例复用**（`lru_cache`），避免每次请求重新握手。

### 8.5 审计

所有动作 `category="auth"`：

| action | target | 说明 |
|---|---|---|
| `sms_send` | 手机号（脱敏 `138****8000`） | ok / fail，fail 带阿里云错误码 |
| `sms_send_blocked` | 手机号 | 被限频拦截 |
| `sms_verify` | 手机号 | 成功 / 失败、失败次数 |
| `phone_register` | username | 附带脱敏 phone |
| `phone_login` | username | 附带是否 2FA |
| `user_approve` | username | 管理员启用待审核用户（复用现有 `update_user` 审计） |

手机号在审计里**脱敏**；完整号码只存在于 `users.phone` 与 Redis 验证码键中。

### 8.6 成本护栏

- 阿里云号码认证控制台配置**用量告警**（日/月阈值）→ 通知管理员。
- 后台「配置页」展示「今日已发送条数」（Redis 计数累加，只读）。
- 余额不足 / 套餐包耗尽 → 后端转 `503` + 告警，**不把原始错误透给前端**（会泄露账号状态）。
- 注意套餐包**不支持自助退费**，避免误购大额包；号码认证与短信服务的套餐包**不互通**，别买错。

---

## 9. 前端改造

| 文件 | 改动 |
|---|---|
| `frontend/src/views/RegisterView.vue` | **新增**。手机号 → 验证码 → 昵称/密码，两屏式。60s 倒计时重发按钮。 |
| `frontend/src/views/LoginView.vue` | 加 Tab：「账号密码登录」/「验证码登录」。复用现有 2FA 分支。 |
| `frontend/src/stores/auth.ts` | 新增 `sendSmsCode(phone, purpose)`、`verifySmsCode(phone, code, purpose)`、`registerByPhone(payload)`、`loginBySms(phone, code)`。 |
| `frontend/src/router/index.ts` | 新增 `{ path: '/register', name: 'register' }`，**不加 `meta.menu`**（公开路由）。守卫里 `login` / `register` 互斥（已登录访问 register → 跳 home）。 |
| `frontend/src/views/ProfileView.vue` | 暂不改（绑定/解绑不在本期范围）。 |

### 9.1 移动端适配

本项目主体是 PC 控制台，但注册入口常被手机访问。注册页单独做窄屏布局（`max-width: 420px` 居中卡片），手机号输入框用 `inputmode="numeric"`、验证码框用 `autocomplete="one-time-code"`（调起系统键盘的短信自动填充）。

### 9.2 审核态的前端表现

`require_approval=true` 时，注册成功后的文案：
> 注册成功！请等待管理员审核，审核通过后即可用手机号登录。

若 `require_approval=false`（运行时切过来），注册成功直接拿到 token，前端直接进首页。

### 9.3 未配置时的降级

`/api/auth/sms/send` 返回 404 → 前端捕获后**隐藏**「验证码登录」Tab 与「注册」入口，并提示「短信服务未配置」。这比展示一个必然失败的按钮体验更好。

---

## 10. 兼容、灰度与迁移

1. **纯增量**：不改 `/api/login` 行为，不改 JWT 结构，不改 `users.username` 语义。存量用户零影响。
2. **开关兜底**：`sms_config.enabled=false` → 所有短信接口返回 404，前端自动隐藏入口。出故障一键关停。
3. **灰度顺序**：
   - 阶段一：`enabled=true`，前端仅对管理员显示验证码登录入口。
   - 阶段二：开放注册入口，`require_approval=true`（新用户待管理员审核）。
   - 阶段三：视运营需要把 `require_approval` 切到 `false`（免审核）。
4. **用户名冲突**：自动生成 `m{手机号}` 被占用时回退 `m{手机号}_{短码}`，仍冲突则要求用户自定义并返回 409。
5. **存量用户手机号**：本期不做自助绑定；管理员可在用户管理页手工维护（`phone_verified=false`）。**手工绑定的用户可用验证码登录**（登录只校验 `users.phone` 存在且 `is_active`，不要求 `phone_verified`），这样管理员可以逐步把存量用户迁到手机号登录。
6. **登录双路径并存**：同一用户既能用密码也能用验证码登录；审计 `action` 区分 `login` / `phone_login`。

---

## 11. 失败处理与可观测性

| 故障 | 阿里云表现 | 后端处理 |
|---|---|---|
| 余额不足 / 套餐包耗尽 | 接口报错 | `503` + 告警 + 审计 fail；前端「服务暂不可用」 |
| 赠送签名/模板未选或填错 | `Code != "OK"` / 参数校验失败 | `503` + 告警；查控制台配置 |
| 号码不在赠送通道覆盖范围 | 运营商回执失败 | 审计 fail + 告警；注意**只支持中国大陆号码** |
| Redis 不可用 | 验证码发不出 | `503`「服务暂不可用」；**绝不降级为「跳过验证码」** |
| 手机号格式错误 | — | `400`；前端正则 `^1[3-9]\d{9}$`，后端同样校验（不信任前端） |
| 登录时用户不存在 | — | `404`「该手机号尚未注册」 |

**日志**：阿里云 `RequestId` 必须落日志（运营商侧排障要用），审计表 `params_json` 带上 `request_id`。

---

## 12. 测试计划

### 12.1 单元测试（`backend/tests/`）

- 号码归一化：`+86` / `86` / 裸 11 位 / 带空格 / 非法号 / 非大陆号段。
- 验证码哈希存取、过期判定、5 次错误作废。
- **purpose 隔离**：register 的验证码不能通过 login 的校验；register 票据不能用于 login 用途。
- 限频计数：同号间隔 / 同号日限 / 同 IP 时限。
- 票据签发与过期；purpose 越权被拒。
- **用 `FakeSmsProvider`，不真发短信。**

### 12.2 集成测试

- 注册全流程 → `pending_approval=true` → 管理员启用 → 验证码登录成功。
- 重复注册 → 409。
- 已注册手机号发短信，响应体与未注册**逐字节一致**。
- 验证码 5 次错误后失效；验证码一次性（二次使用失败）。
- 2FA 用户走验证码登录后**仍需** 2FA。
- `require_approval=false` 时注册直接拿到 token。
- 未配置短信（`sign_name` 为空）→ 接口返回 404。

### 12.3 回归测试

- 原 `/api/login` + 2FA 流程不变。
- `/api/me` 返回结构不变（`MeOut` 不新增字段，避免影响前端）。
- `GET /api/users` 正常显示新用户；`UserOut` 新增字段不影响既有断言。
- RBAC 菜单、节点归属、审计列表正常。

### 12.4 手工测试

- 真机收短信（**移动 / 联通 / 电信号码各测一次**）；60s 倒计时；同号多端登录；管理员禁用用户后验证码登录被拒。
- 阿里云控制台的「发送量 / 回填量 / 回填率」与后端日志的 `RequestId` 对得上。

---

## 13. 二期候选（本期不做）

1. **自助找回密码**：用赠送模板 `100003`（重置密码），复用现有 `update_user_password()`（它已自动 `token_version+1` 吊销旧会话）。
2. **手机号绑定 / 解绑**：用赠送模板 `100004` / `100005`，需短信验证（解绑也要，防误解）。
3. **号码认证一键登录（`GetMobile`）**：运营商网络内本机号直连，体验优于短信。⚠️ 阿里云此产品面向 App 端，**H5 / 纯 Web 场景受运营商限制**（双网络环境下有取号串号风险），本项目是 PC Web 控制台，优先级低。
4. **阿里云图形验证码（人机校验）**：在「发送验证码」前置，大幅降低被刷概率与无效短信量。公网开放注册时建议加。
5. **埋点与转化漏斗**：发送成功率 / 校验成功率 / 注册完成率。
6. **换用普通短信服务（自有签名）**：当需要发通知短信、自定义品牌签名、或需要合并到现有短信服务结算时再评估。届时需走资质报备（5–7 工作日），且**赠送签名与自有签名不可混用**，属切换而非叠加。

---

## 14. 排期

| 阶段 | 内容 | 人日 | 关键依赖 |
|---|---|---|---|
| P0 | 阿里云开通号码认证-短信认证 + 取赠送签名/模板 + RAM 权限 | 0.5（人工） | **当天可完成，无外部等待** |
| P0 | 后端：`sms_provider` + `sms_service` + 限频 + 验证码 | 2 | — |
| P0 | 后端：`phone_auth` 注册/登录 + 用户表迁移 + 审计 | 2 | — |
| P1 | 前端：注册页 + 登录双 Tab + store + 路由 | 1.5 | — |
| P1 | 管理员页：短信配置项 + 开关 + 今日发送量 + 手机号展示 | 0.5 | — |
| P1 | 测试（单元 + 集成 + 手工） | 1.5 | — |
| — | **合计** | **约 8 人日** | — |

> **相比原腾讯云方案，最大的变化是去掉了「等 7–10 天运营商报备」这条关键路径。** 阿里云短信认证开通当天即可联调，后端开发与账号准备完全并行，无阻塞。

---

## 15. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| **赠送签名品牌感不足**（个人认证账号尤其明显） | **高** | 开通前先在控制台确认赠送签名内容；若不可接受，只能换普通短信服务（需等报备）或升级为企业认证 |
| 赠送签名/模板不可自定义，文案无法体现业务 | 中 | 模板文案是标准验证码文案，天然规避行业关键词（见下条） |
| **行业关键词风险**：本项目属外汇/黄金/期货交易，各厂商模板规范均禁止「金融/股票/贵金属/原油期货」类文案 | 中 | 赠送模板为平台预置标准文案，**不含任何业务词，天然规避**；这也是选短信认证而非自建模板的额外收益 |
| 短信认证与短信服务套餐包不互通，误购无法自助退费 | 中 | 采购前确认产品；先买 100 次 / 1 千次小包验证，别买大额 |
| 被刷短信 → 费用失控 + 账号风控 | 中 | 多维限频 + 用量告警 + 必要时加图形验证码 |
| 手机号枚举泄露用户规模 | 中 | 发送接口响应体完全一致（§8.3） |
| 赠送通道仅支持中国大陆号码 | 低 | 本期只做国内，无需处理 |
| `users.phone` 唯一约束与存量数据冲突 | 低 | 新列默认 NULL；迁移前扫重 |
| 密钥泄露 | 中 | RAM 子账号最小权限 + env 注入 + 定期轮换 + 不入仓 |
| 自动生成的 `username` 撞名 | 低 | 回退 `m{手机号}_{短码}`；仍冲突返回 409 让用户自定义 |

---

## 16. 待确认

1. **赠送签名的实际内容**：需在阿里云控制台查看「赠送签名配置」页面确认。个人认证账号的赠送签名是平台统一名称，品牌感弱；企业认证账号通常是企业名/商标名。**这一项决定方案是否可接受**，请优先确认。
2. `require_approval` 的**默认值**：已确认为 `true`（需审核）。是否需要一个「管理员可批量审核」的操作入口，还是逐个在用户管理页启用即可？
3. 审核期间用户**是否允许重复提交注册**：建议允许（覆盖更新昵称），但需防止绕过审核。
4. 套餐包采购量：建议先买 **100 次 / 5 元**验证链路，跑通后再按量补。
