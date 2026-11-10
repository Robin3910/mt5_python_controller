# 修改手机号（自助改绑）设计方案

- 日期：2026-11-08
- 范围：`/profile`（个人中心）里「个人信息 → 电话」字段，从**只读**改为**可自助改绑到其他手机号**
- 状态：设计稿（未实现）

---

## 1. 背景与现状

### 1.1 现状

| 位置 | 现状 |
|---|---|
| `frontend/src/views/ProfileView.vue` | 电话字段是 `readonly` 输入框，提示「自助改绑需短信验证，暂未开放」 |
| `backend/app/auth.py` `PATCH /api/me/profile` | `ProfileUpdate` 里**没有** `phone` 字段；`user_service.update_profile` 显式注释「不碰 phone」 |
| `backend/app/users_api.py` `PATCH /api/users/{id}` | 管理员可以改任意用户的手机号（`phone_verified=False`），已有唯一性冲突检测（409） |
| `backend/app/user_service.py` | `bind_phone()` / `update_user(phone=...)` 已具备改绑的落库能力，`users.phone` 上有 `unique=True` 索引 |
| `backend/app/phone_auth.py` | 短信验证码只有**一个** purpose：`auth`（登录/注册共用）；票据 `phone_pending` JWT 带 `purpose` 做用途隔离 |
| `backend/app/sms_service.py` | 验证码按 `purpose` 存在 Redis（`sms:code:{purpose}:{phone}`），校验通过即焚，错误 5 次作废 |

### 1.2 目标

用户能在个人中心自助把账号绑定的手机号改成**另一个**手机号，改完立刻能用新号验证码登录。

### 1.3 非目标（本期不做）

- 不做自助**解绑**手机号（避免账号失去所有登录凭据）。
- 不做「一个账号绑多个手机号」。
- 不做改绑后的历史号码回收/再绑定规则（换绑次数不设硬上限，靠限频与审计兜底）。
- 不改 `users.username` 的主键语义，不改 JWT 结构，不影响 RBAC / 审计的 `operator` 口径。

---

## 2. 安全模型（最关键的部分）

改绑手机号 = **换掉账号的登录凭据**。所以这不是「改一个个人资料字段」，而是「转移账号控制权」。

### 2.1 威胁模型

| 攻击 | 防御 |
|---|---|
| 攻击者拿到会话 token（XSS / token 泄露），把手机号改成自己的 | **必须验证旧凭据**：旧号验证码 或 登录密码（+2FA）。不能只验新号。 |
| 攻击者用别人的号注册/换绑（抢占他人号码） | 新号验证码 + 服务端验码，**只有号码持有者能过** |
| 枚举「哪些号已被注册」 | 发码接口**不返回**该号是否已绑定；只有最终改绑（且已验新号所有权）才 409 |
| 改绑被用于「撞库接管」 | 新号必须未被任何账号绑定（唯一索引）；旧号在改绑瞬间失效 |
| 刷短信费 | 复用现有限频（号/日、IP/时、IP/日、全局总量）+ 改绑专属冷却与配额 |
| 中途放弃留下脏状态 | 验证码即焚、票据 10 分钟过期、改绑是单事务落库 |

### 2.2 身份确认的两条可选路径（至少满足其一）

改绑必须同时满足：

1. **新号所有权**：`new_phone` 通过短信验证码校验 → 换发一张 `phone_rebind` 票据（绑定 `user_id + new_phone`）。
2. **本人身份**（二选一）：
   - **路径 A：登录密码**（+ 若账号已开 2FA，再加 TOTP 码）；
   - **路径 B：旧手机号验证码**。

> **为什么要路径 B**：手机号注册时允许「不填密码」，服务端会生成随机强密码（`phone_auth.register_by_phone`）。这类用户**不知道自己的密码**，只有旧号能证明身份。若只留路径 A，这批用户永远改不了号。
> 因此 UI 上两个入口都摆出来，由服务端按「密码是否校验通过」决定是否放行。

### 2.3 强制规则

- 账号**没有绑定手机号**时（`users.phone IS NULL`，例如管理员建号的用户）：跳过旧凭据验证，只要新号验证码即可。但仍需密码或 2FA 之一吗？—— **不需要**：此时账号没有「旧号码」可被抢占，验证新号所有权即足够（与「绑定」语义一致）。
- 已绑定手机号时，**旧凭据验证不可跳过**（不可配置开关，避免被误关）。
- 改绑成功后：旧号立即失去「验证码登录」资格，新号获得登录资格，`phone_verified=True`。

---

## 3. 后端设计

### 3.1 短信用途（purpose）扩展

`sms_config.SMS_PURPOSES` 目前是 `("auth",)`。改绑需要两个新用途，且**必须与 `auth` 隔离**：

| purpose | 用途 | 收码号码 |
|---|---|---|
| `auth`（现有） | 登录 / 注册 | 任意 |
| `rebind_new` | 改绑：验证**新**号所有权 | 新号 |
| `rebind_old` | 改绑：验证**旧**号（路径 B） | 旧号 |

Redis key 是 `sms:code:{purpose}:{phone}`，purpose 进 key 天然隔离：登录用的码拿去做改绑无效，反之亦然。

> 阿里云的签名与模板码是写死的常量（`FIXED_TEMPLATE_CODE = "100001"`），所有用途共用同一条短信文案。因此**新号短信与登录短信文案完全相同**（都是「验证码 xxxxxx」）。
> 这是本期已知限制：用户可能以为在登录。若介意，后续可申请第二个模板码并让 `issue_code` 按 purpose 选模板（需改动 provider 链路，留作后续增强）。

### 3.2 新增票据类型

在 `backend/app/security.py` 增加：

```
typ = "phone_rebind"
payload = { sub: new_phone, uid: user_id, typ: "phone_rebind", exp: now + 600 }
```

- 由「校验新号验证码」成功后签发；
- 校验函数 `verify_phone_rebind_jwt(token, user_id, phone)`：类型、uid、sub 三者都要对上；
- 与现有 `phone_pending`（`typ=phone_pending`）类型隔离，不能互相顶替。

### 3.3 新增接口

新建 `backend/app/phone_rebind.py`（独立路由文件，避免把 `phone_auth.py` 继续撑大），前缀 `/api/me/phone`。

#### ① `POST /api/me/phone/send-code`

请求：

```json
{ "target": "new" | "old", "phone": "13800138000", "captcha_verify_param": null }
```

- 鉴权：普通 JWT（`get_principal`）。
- `target=new`：
  - `phone` 归一化（`normalize_phone`），只校验格式，**不查库**（不泄露该号是否已绑定）；
  - **被占用的号照样发短信**：占用与否只在最终提交时以 409 呈现。发码阶段跳过只会让
    用户卡在第一步（「验证码收不到」）却换不来隐私收益 —— 能收到验证码本身就证明
    调用方是这个号的持有者。响应体在「已占用 / 空闲」两种情况下完全一致。
- `target=old`：
  - **旧号由服务端从当前绑定号码取**（前端 `/api/me` 里的号码是脱敏的，让前端回传
    完整旧号等于要求前端持有明文，没必要也不安全）；
  - 账号未绑定手机号时 400「账号未绑定手机号」。
- 限频：
  - 复用 `check_rate_limits()`（号间隔/号日、IP时、IP日、全局总量）；
  - **额外**改绑专属配额：同一账号 24h 内「改绑发码」≤ 5 次（Redis 新 key `sms:rebind:{user_id}:{day}`），避免一个被盗账号把短信额度打满。
- 人机验证：与现有 `/api/auth/sms/send` 一致 —— 命中风控返回 **428**（`detail="需要完成人机验证"`），前端弹阿里云滑块后带 `captcha_verify_param` 重试。
- 响应：`{ ok: true, expire_in, cooldown }`（与现有一致）。

#### ② `POST /api/me/phone/verify-new`

请求：

```json
{ "phone": "13800138000", "code": "123456" }
```

- 校验 `rebind_new` 用途的验证码（`verify_code`）；
- 通过 → 签发 `phone_rebind` 票据，返回 `{ ok: true, ticket, expire_in: 600 }`；
- 校验失败：验证码错误次数累计（沿用 `max_verify_attempts`，超限作废）。

#### ③ `POST /api/me/phone/rebind`（最终提交）

请求：

```json
{
  "new_phone": "13800138000",
  "ticket": "<phone_rebind 票据>",
  "auth_method": "password" | "old_code",
  "password": "******",       // auth_method=password 时
  "totp_code": "123456",      // 账号开了 2FA 时必填
  "old_code": "654321"        // auth_method=old_code 时
}
```

服务端编排：

1. 校验票据（`verify_phone_rebind_jwt`）：类型、`uid == 当前用户`、`sub == new_phone`。否则 401。
2. 归一化 `new_phone`，与票据里的号码比对（防改）。
3. **唯一性**：查 `get_user_by_phone(new_phone)`：
   - 已存在且不是本人 → **409**「该手机号已被其他账号绑定」；
   - 是本人（即改回原号）→ 允许（幂等，直接返回成功，`phone_verified=True`）。
4. **身份确认**：
   - 账号当前**没有**绑定手机号 → 跳过本步（见 §2.2）。
   - `auth_method == "password"`：
     - `verify_user_password(username, password)` 不通过 → 401「密码不正确」；
     - 若 `user_requires_2fa(username)` → 还必须校验 TOTP（`verify_user_totp`），失败 → 401「验证码错误或已过期」。
   - `auth_method == "old_code"`：
     - 校验 `rebind_old` 用途的验证码（旧号）；失败 → 400「验证码错误或已过期」。
5. 落库：`user_service.update_user(user_id, phone=new_phone, phone_verified=True)`
   - 内部已有唯一性校验（重复则抛 `ValueError` → 409）；
   - `phone_verified=True`（新号已验证）。
6. 审计 `persist.audit(operator=username, action="phone_rebind", target=username, params={"old": mask(old), "new": mask(new), "method": auth_method}, result="ok", ip, category="auth", before={"phone": mask(old)}, after={"phone": mask(new)})`
   - 审计里**只放脱敏号码**（`mask_phone`），明文号码只留在 `users.phone`。
7. 通知（可选，见 §6.3）：向**旧号**发一条「账号手机号已变更」提醒短信。
8. 响应：`{ ok: true }`（前端随后 `auth.fetchMe()` 刷新展示）。

**失败语义统一**（前端据此提示）：

| 状态码 | detail | 含义 |
|---|---|---|
| 400 | `手机号格式不正确` | 号码非法 |
| 400 | `验证码错误或已过期` | 验证码错 |
| 400 | `账号未绑定手机号` | 走 old_code 但账号没绑号 |
| 401 | `invalid or expired ticket` | 票据失效 → 前端要求重新验证新号 |
| 401 | `密码不正确` | 密码错 |
| 401 | `验证码错误或已过期`（2FA） | TOTP 错 |
| 409 | `该手机号已被其他账号绑定` | 号码被占 |
| 429 | 限频文案 | 冷却 / 超限 |
| 503 | `服务暂不可用` | 短信厂商异常 |

### 3.4 管理员兜底（已存在，保持不变）

`PATCH /api/users/{id}` 管理员改绑仍可用（`phone_verified=False`）。作为「用户确实丢了旧号且不记得密码」的最后手段，由管理员在用户权限页操作。

> 建议给管理员改绑也补一条 `phone_rebind_admin` 审计（当前 `users_api` 的 update 分支是否已写审计需实现时确认；若无则补上）。

### 3.5 数据模型

**不改表结构。** `users.phone`（`String(20)`, `unique`, `index`）已够用。

可选增强（本期不做，但留接口）：新增 `user_phone_history` 表（`user_id, old_phone_mask, new_phone_mask, method, operator, created_at`），用于「换绑历史」长期留痕。审计表已能覆盖基本需求，故本期不做。

### 3.6 数据库迁移

无迁移。`phone_verified` / `phone` 列已存在（见 `backend/app/orm.py` 与 `docs/superpowers/specs/2026-11-01-phone-register-aliyun-design.md`）。

---

## 4. 前端设计

### 4.1 入口

`frontend/src/views/ProfileView.vue` 的「个人信息」卡片：

- 非编辑态：列表项「电话」右侧展示 `138****8000`，末尾加一个「修改」文字按钮；
- 编辑态（`editingProfile`）：电话输入框保持只读展示，但去掉「暂未开放」的禁用文案，改为「点『修改手机号』自助改绑」。

点击「修改手机号」→ 打开**独立弹层**（`AppModal` 或自绘遮罩层），不塞进「修改资料」的表单里 —— 改绑是多步带验证码的流程，混在资料表单里会让「保存资料」按钮语义混乱。

### 4.2 弹层：三步向导

```
第 1 步：验证新手机号
  ├ 输入新手机号（11 位，inputmode=numeric）
  ├ [获取验证码] → 冷却倒计时（同注册页写法）
  ├ 输入验证码 → [下一步]
  └ 成功 → 展示"新号已验证"绿条，进入第 2 步

第 2 步：验证本人身份（二选一）
  ├ Tab A：用登录密码
  │   ├ 当前密码 + (若开 2FA) TOTP 6 位
  │   └ [确认改绑]
  └ Tab B：用原手机号验证码
  ├ 输入旧号验证码（[获取验证码]发到当前绑定号）
  └ [确认改绑]

第 3 步：完成
  └ 成功提示 + 自动关闭 + 刷新展示新号码
```

细节：

- 「获取验证码」按钮在 429（限频）时展示后端给的文案；428 时复用 `frontend/src/utils/aliyunCaptcha.ts` 弹滑块（与 `RegisterView.vue` 完全同一套写法）。
- 短信服务不可用（`GET /api/auth/sms/status` → `enabled=false`）：弹层第一步就禁用并提示「短信服务未配置，请联系管理员在用户权限页修改」，不显示发码按钮。
- 账号**未绑定**手机号时（`auth.me?.phone` 为空）：第 2 步直接跳过（后端也跳过校验），只做第 1 步。
- 账号已开 2FA（`auth.fetch2faStatus().enabled`）：Tab A 额外要求 TOTP 输入框。
- 改绑成功后调用 `auth.fetchMe()`，列表里的号码即时刷新（`/api/me` 返回的是**脱敏**号码，正好符合展示需求）。
- 失败提示统一从 `err.response.data.detail` 取，与 `ProfileView.vue` 现有错误处理风格一致；`429` 直接展示后端中文文案。

### 4.3 类型与 store

`frontend/src/api/types.ts`：

```ts
export type SmsPurpose = 'auth' | 'rebind_new' | 'rebind_old'

export interface PhoneRebindTicket { ok: boolean; ticket: string; expire_in: number }
export type PhoneRebindAuthMethod = 'password' | 'old_code'
export interface PhoneRebindPayload {
  new_phone: string
  ticket: string
  auth_method: PhoneRebindAuthMethod
  password?: string
  totp_code?: string
  old_code?: string
}
```

`frontend/src/stores/auth.ts` 新增 3 个 action：

- `sendRebindCode(target: 'new' | 'old', phone: string, captchaVerifyParam?: string)`
- `verifyRebindNewPhone(phone: string, code: string): Promise<PhoneRebindTicket>`
- `rebindPhone(payload: PhoneRebindPayload): Promise<void>`

（现有 `sendSmsCode(phone, purpose)` 已经按 purpose 参数化，改绑复用同一个 `/api/auth/sms/send`？—— **不行**，`/api/auth/sms/send` 是公开无鉴权接口，无法把「当前用户」带进限频配额。）
→ 所以**改绑发码走新接口** `POST /api/me/phone/send-code`，不复用公开接口。

### 4.4 UI 一致性

- 复用 `pf-card` / `pf-input` / `pf-btn` / `pf-hint` / `pf-error` / `pf-grid-3` 等既有类，不新增视觉语言；
- 弹层复用 `frontend-console/src/components/AppModal.vue` 的思路，但 `frontend` 里目前没有通用 Modal 组件 —— 实现时在 `frontend/src/components/` 新增一个轻量 `PhoneRebindModal.vue`（或直接用 `el-dialog`，项目已引入 element-plus 的 message 样式，见 `MineView.vue` 顶部 import）。

---

## 5. 限频与配额汇总

| 维度 | 值 | 来源 |
|---|---|---|
| 同号发送间隔 | 60s（`send_interval_seconds`） | 现有 `check_rate_limits` |
| 同号每日发送 | 10（`daily_limit_per_phone`） | 现有 |
| 同 IP 每小时 | 20（`hourly_limit_per_ip`） | 现有 |
| 同 IP 每日 | 50（`daily_limit_per_ip`） | 现有 |
| 全局每日总量 | 500（`daily_limit_total`） | 现有 |
| **改绑专属：同账号 24h 发码** | **5 次** | **新增**（Redis `sms:rebind:{uid}:{YYYYMMDD}`） |
| 验证码有效期 | 300s | 现有 |
| 验证码错误次数 | 5 次后作废 | 现有 |
| 票据有效期 | 600s | 新增票据 |

---

## 6. 边界与失败处理

### 6.1 典型失败路径

| 场景 | 期望行为 |
|---|---|
| 改到已被别人绑定的号 | 新号验证码**能收到**；最终提交时 409。**发码阶段不提示「该号已注册」**（防枚举） |
| 改回自己原来的号 | 幂等成功，`phone_verified=True` |
| 票据过期（10 分钟后再提交） | 401 → 前端自动回到第 1 步重新验证新号 |
| 旧号已停用/换人（路径 B） | 用户改用路径 A（密码） |
| 手机号注册且未设密码 | 只能用路径 B；若旧号也丢了 → 找管理员（§3.4） |
| 改绑中途关页面 | 验证码/票据自然过期，无副作用 |
| 改绑成功后另一台设备用旧号登录 | 旧号已不绑定本账号 → 走 `/api/auth/sms/login` 会「该手机号尚未注册」（或属于别人），登录失败 ✅ |
| 短信服务停用 | 弹层提示不可用，引导管理员 |

### 6.2 与登录态的关系

- 改绑**不强制下线**（当前会话继续可用），但**建议**对「改绑成功」的用户 `token_version` 不做变更 —— 因为改绑不是密码变更，避免打断正在操作的用户。
- 若安全上更保守，可选：改绑成功后吊销**其它**设备会话（当前实现 `rbac_service.revoke_sessions` 是全量吊销，不区分设备，故本期不做）。

### 6.3 旧号变更提醒（可选增强，本期建议做，成本 1 条短信）

改绑成功后向**旧手机号**发一条提醒短信：「您的账号绑定手机号已变更为 xxx，如非本人操作请联系客服。」

- 需要 provider 支持「自定义内容下发」；若阿里云模板只支持「验证码」文案，则需申请新模板 —— **本期先不做**，改为在后台「操作审计」里可查（管理员能看到 `phone_rebind` 记录）。
- 折中实现：改绑成功后给旧号发一条**验证码用途**的短信不合适（会污染 `auth` 码）。→ 结论：**不做自动通知**，依赖审计 + 管理员巡检。

---

## 7. 实施拆解（待确认后执行）

### 后端

1. `backend/app/sms_config.py`：`SMS_PURPOSES` 增加 `rebind_new` / `rebind_old`。
2. `backend/app/security.py`：新增 `create_phone_rebind_jwt` / `verify_phone_rebind_jwt`。
3. `backend/app/redis_store.py`：新增改绑配额计数方法 + key。
4. `backend/app/phone_rebind.py`（新文件）：三个接口 + 编排 + 审计。
5. `backend/app/models.py`：新增 `PhoneRebindSendRequest` / `PhoneRebindVerifyRequest` / `PhoneRebindRequest`。
6. `backend/app/main.py`：注册新路由。
7. `backend/app/users_api.py`：确认管理员改绑是否已写审计，缺失则补。

### 前端

8. `frontend/src/api/types.ts`：`SmsPurpose` 扩展 + 三个新类型。
9. `frontend/src/stores/auth.ts`：三个新 action。
10. `frontend/src/components/PhoneRebindModal.vue`（新文件）：三步向导弹层。
11. `frontend/src/views/ProfileView.vue`：电话行加「修改」入口 + 接入弹层。
12. `frontend/src/views/MineView.vue`：账号卡片副标题可加一句「个人信息 · 修改密码 · 修改手机号」（可选）。

### 测试

13. `backend/tests/test_phone_rebind.py`：
    - 正常改绑（密码路径）；
    - 正常改绑（旧号验证码路径）；
    - 新号已被占用 → 409；
    - 票据 uid / phone 不匹配 → 401；
    - `rebind_new` 的验证码不能用于登录（purpose 隔离）；
    - 改绑配额超限 → 429；
    - 未绑定手机号的账号跳过身份确认；
    - 改绑后旧号不能再验证码登录。
14. `backend/tests/test_profile_api.py`：确认 `PATCH /api/me/profile` 仍不接受 `phone`（防止绕过改绑流程）。

---

## 8. 待确认的决策点

1. **路径 B（旧号验证码）是否默认开启？** 建议开（否则未设密码的用户无法改绑）。
2. **改绑频率上限**：是否再设「N 天内最多改绑 M 次」的业务级硬上限？建议先只做 24h 发码配额，观察一段时间再定。
3. **管理员改绑是否需要「二次确认 + 审计必填」？** 建议审计必填。
4. **是否需要给旧号发变更提醒短信**？需确认阿里云是否可申请自定义模板；本期默认不做。
5. **改绑是否触发 KYC 复审**？建议不触发（手机号变更不等于实名变更；若业务要求，可在 `user_review` 建一张 `kyc_submitted=False` 的复查单，实施时再加）。

---

## 9. 实施记录（2026-11-08 已完成）

### 已落地

**后端**
- `sms_config.py`：新增 `PURPOSE_REBIND_NEW` / `PURPOSE_REBIND_OLD`，并入 `SMS_PURPOSES`。
- `security.py`：新增 `create_phone_rebind_jwt` / `verify_phone_rebind_jwt`（票据钉死 `uid` + 新号 + 类型 `phone_rebind`，10 分钟有效）。
- `redis_store.py`：新增 `K_SMS_REBIND_QUOTA` 与 `incr_rebind_quota` / `get_rebind_quota`（按账号计）。
- `models.py`：新增 `PhoneRebindSendRequest` / `PhoneRebindVerifyRequest` / `PhoneRebindRequest`。
- `phone_rebind.py`（新文件）：三个接口 + 编排 + 审计。
- `main.py`：注册 `phone_rebind.router`。
- `users_api.py`：管理员改绑的审计参数原先写入**明文手机号**，已改为脱敏（`mask_phone`）。

**前端**
- `api/types.ts`：`PhoneRebindTarget` / `PhoneRebindAuthMethod` / `PhoneRebindTicketResult` / `PhoneRebindPayload` / `PhoneRebindResult`。
- `stores/auth.ts`：`sendRebindCode` / `verifyRebindNewPhone` / `rebindPhone`。
- `components/PhoneRebindModal.vue`（新文件）：三步向导弹层。
- `views/ProfileView.vue`：电话行加「修改手机号」按钮；编辑态提示文案更新；接入弹层。

**测试**
- `backend/tests/test_phone_rebind.py`：27 个用例，覆盖两条身份路径、2FA 叠加、票据三类越权、
  验证码用途隔离（三个方向）、发码配额（按账号）、人机验证 428、枚举防护、改绑后旧号失效、
  以及「`PATCH /api/me/profile` 仍不能改手机号」的回归。

### 实施过程中调整的两处设计

1. **被占用的新号照样发短信**（原设计是「静默不发」）。
   实现时发现：静默不发会让用户卡在第一步（收不到验证码）却拿不到任何提示，
   而占用状态本来就只在最终提交时以 409 呈现。已改为「照常发码，提交时 409」，
   与本文档 §6.1 的原始描述一致。
2. **`target=old` 的旧号由服务端取**，不要求前端回传。
   `/api/me` 返回的是**脱敏**号码（`138****8000`），前端根本拿不到明文旧号，
   让它回传等于要求前端持有明文。改为服务端按当前绑定号码取，更安全也更简单。

### 验证结果

- 后端全量：`1300 passed`（含新增 27 个）。
- 前端：`vue-tsc --noEmit` 通过；`npm run build` 通过（52/52 chunk 语法校验 OK）。

### 遗留（未做）

- 改绑成功后**不给旧号发变更提醒短信**（阿里云模板码写死为验证码模板，
  需申请新模板才能发自定义内容）。目前只能靠后台「操作审计」查看 `phone_rebind` 记录。
- 未做「N 天内最多改绑 M 次」的业务级硬上限（当前只有 24h 发码配额）。
- 未做改绑历史表（审计已覆盖基本追溯需求）。


---

## 10. 附：涉及的关键文件

| 文件 | 作用 |
|---|---|
| `backend/app/phone_auth.py` | 现有短信注册/登录链路（参照实现） |
| `backend/app/sms_config.py` | purpose 定义、限频阈值 |
| `backend/app/sms_service.py` | 归一化、限频、发码/验码 |
| `backend/app/security.py` | 票据 JWT 工具 |
| `backend/app/user_service.py` | `update_user(phone=...)` / `bind_phone` |
| `backend/app/auth.py` | `PATCH /api/me/profile`（当前不含 phone） |
| `backend/app/users_api.py` | 管理员改绑（兜底路径） |
| `backend/app/persist.py` | 审计写入 |
| `frontend/src/views/ProfileView.vue` | 个人中心（改绑 UI 落点） |
| `frontend/src/components/PhoneRebindModal.vue` | 改绑三步向导弹层（新增） |
| `backend/app/phone_rebind.py` | 改绑三个接口（新增） |
| `backend/tests/test_phone_rebind.py` | 改绑端到端用例（新增） |
| `frontend/src/views/RegisterView.vue` | 验证码 + 滑块交互的参照实现 |
| `frontend/src/stores/auth.ts` | 登录态与短信相关 action |
| `frontend/src/utils/aliyunCaptcha.ts` | 阿里云滑块封装 |
