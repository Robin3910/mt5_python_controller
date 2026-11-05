# 用户注册审核（KYC 实名审核）实现方案

日期：2026-11-04
状态：已确认并实施（2026-11-04）
原型：`UI/H5.html` →【我的】→【用户注册审核】
范围：管理员侧的注册用户 KYC 审核页面 + 审核数据模型 + 注册侧实名材料提交

> **实施记录**：本文档的 6 个决策点已全部确认并落地。实际实现与本文档的
> 差异见 §11「实施结果与本文档的差异」。

> **修订记录（2026-11-05）**：**实名材料简化为「姓名 / 邮箱 / 手机号」三项**，
> 去掉证件类型（`cert_type`）与证件号（`cert_no`）——
> 连带去掉掩码、哈希、证件格式校验、证件号去重（及其 409）。
> 详见 §12「实名材料简化（2026-11-05）」。审核通过时**同时分配节点**的功能不变。

---

## 1. 原型还原（页面上到底有什么）

原型是「灵光闪应用」生成的 React bundle，逻辑可从 `assets/index-Ci3OJfTd.js` 完整还原。

### 1.1 入口

「我的」页模块卡片（`mine-user-audit-entry`）：

| 字段 | 值 |
|---|---|
| 图标 | `verified_user` |
| 标题 | 用户注册审核 |
| 描述 | 注册用户 · KYC 实名审核 · 节点明细 |
| 配色 | `#38BDF8`（青蓝） |

### 1.2 页面结构

```
┌─ 页头卡（渐变 #101828 → #0E1A2E）
│  图标 verified_user + 标题「用户注册审核」+ how_to_reg
│  频道介绍：所有注册进入平台的用户均需完成 KYC 实名审核，通过后方可使用
│            完整功能；每个用户名下挂载的节点数量与明细在此统一查看。
│  6 个统计块（grid-cols-3）：
│    注册用户 / 待审核 / 已通过 / 已驳回 / 节点总数 / 在线节点
└
┌─ Tab 栏：待审核(N) | 已审核(N)        [全部通过]（仅待审核 tab 且 N>0）
├─ 搜索框：搜索用户名 / 账号 / 手机号 / 节点 ID
└─ 卡片列表
```

### 1.3 卡片（待审核）

```
● 头像(用户名首字, 琥珀渐变)  用户名  [账号 U 7-904101]      [待审核·脉冲点]
  注册 10-28 14:32 · 138****2210
┌ KYC 实名材料 · 提交于 10-29 09:15        (badge 图标, #38BDF8)
│ 实名姓名    徐明远
│ 邮箱        xumingyuan@example.com
│ 手机号      138****2210
└
▼ 名下节点  [2 个]                          (可折叠, account_tree 图标)
   [主节点 A1]  7-904101-01  在线  8,620.50 U
   [备用节点 A2] 7-904101-02  离线  0 U
   （空态：该用户暂无节点，审核通过后可创建）
─────────────────────────────────────────
                              [驳回]   [通过]
```

### 1.4 卡片（已审核）

同上，但：

- 徽章换为 `已通过`(绿 `verified_user`) / `已驳回`(红 `cancel`)
- 第二行变成 `注册 10-25 19:55 · 审核 10-26 10:32`
- 已驳回时在 KYC 块下方追加红框：`驳回原因：{reason}`
- 底部无操作按钮

### 1.5 弹窗

**通过确认弹窗**（`user-audit-approve-modal`）：

```
[verified_user 图标]  通过 KYC 实名审核
                     徐明远 · 徐明远（U 7-904101）
┌ 邮箱          ┐ ┌ 名下节点      ┐
│ xumingyuan@…  │ │ 2 个           │
└ 邮箱          ┘ └ 节点数        ┘
说明：通过后该用户将完成实名认证，可正常登录使用交易、带单与节点管理等完整功能。
              [取消]  [确认通过]
```

**驳回弹窗**（`user-audit-reject-modal`）：

```
[gpp_bad 图标]  驳回 KYC 实名审核
               徐明远 · 徐明远（U 7-904101）
选择驳回原因（5 个预设 chip，单选）:
  · 证件照片模糊，无法清晰辨认，请重新上传后提交
  · 实名信息与上传证件不一致
  · 证件已过期，请更新有效证件
  · 人脸识别与证件照片不匹配
  · 注册信息填写不完整，请补充后重新提交
[补充说明 textarea（可留空，但确认按钮要求非空）]
              [取消]  [确认驳回]
```

> 交互细节：驳回弹窗的「确认驳回」按钮在理由为空时禁用 —— 预设 chip 会自动填入 textarea。

> **修订（2026-11-05）**：不再收集证件，预设文案里**提到「证件」的 4 条全部替换**
> （见 §12.4）。现在的预设围绕「姓名 / 邮箱 / 手机号」三项材料。

### 1.6 原型 mock 数据字段

```ts
{
  id: 'UA-1001',
  userName: '清风徐来',
  account: 'U 7-904101',
  contact: '138****2210',        // 已脱敏
  registerTime: '10-28 14:32',
  kycStatus: 'pending' | 'approved' | 'rejected',
  realName: '徐明远',
  email: 'xumingyuan@example.com',
  submitTime: '10-29 09:15',
  auditTime: '10-26 10:32' | undefined,
  rejectReason: string | undefined,
  nodes: [{ id, name, status: 'online'|'offline', balance }],
}
```

> **修订（2026-11-05）**：原型 mock 里的 `certType` / `certNo` 字段**已去掉**。
> 注意原型卡片的 KYC 块本来就只显示 3 行（姓名 / 邮箱 / 手机号）——
> 去掉证件后，**实现与原型完全一致**，不再有「多显示 2 行」的差异。

---

## 2. 现状盘点

### 2.1 已有的（可直接复用）

| 能力 | 位置 | 说明 |
|---|---|---|
| 手机号注册 + 待审核态 | `backend/app/phone_auth.py` | `require_approval=true` → `is_active=False`、不签 token |
| `next_action=pending_approval` | `phone_auth.py` | 登录页已能识别「待审核」并给出提示 |
| 用户启用/禁用 | `backend/app/users_api.py` `PATCH /api/users/{id}` | `is_active` 切换 + 会话吊销 + 审计 |
| 手机号存储与脱敏 | `orm.User.phone` / `sms_service.mask_phone` | 唯一索引，E.164 |
| 邮箱字段 | `orm.User.email` | 已存在，个人中心可自助维护，**不参与登录** |
| 节点归属 | `Node.owner_user_id` + `users_api.assign_nodes` | 分配/回收、冲突校验齐全 |
| 节点在线态 | `connections.manager.is_node_online` | 内存态 |
| 节点账户快照 | `redis_store.get_account(node_id)` | 含 `balance` |
| 审计 | `persist.audit(category="auth")` | before/after 快照 |
| 管理员会话失效 | `rbac_service.revoke_sessions` / `invalidate_principals` | 改权限后即时踢下线 |
| 前端用户管理页 | `frontend/src/views/PermissionView.vue` | 已有列表/搜索/启用禁用/角色/节点分配 |

### 2.2 缺的（必须新增）

| 缺口 | 影响 |
|---|---|
| **审核状态独立字段** | `is_active` 只是登录闸门，无法表达「已驳回」；也无法记录审核时间、审核人、驳回原因 |
| **实名材料字段** | `users` 表没有 `real_name` / `cert_type` / `cert_no`；原型 KYC 块无数据可显示 |
| **注册时不收集实名信息** | `RegisterView.vue` 第二步只有昵称/用户名/密码 |
| **审核流转接口** | 现在只能在「用户权限」页逐个启用/禁用，无批量通过、无驳回 |
| **节点明细聚合接口** | `UserOut.node_ids` 只有 ID 列表，无名称/在线态/余额 |
| **页面与路由** | 无 `/user-audit` 路由、无 `user_audit` 菜单码 |

### 2.3 关键差异（原型 vs 现实）

> ⚠️ **原型里「待审核」用户名下已经有节点**（主节点 A1、网格节点 C2…）。
> 现实中做不到：待审核用户 `is_active=False` → **无法登录** → 无法自建节点；节点必须由管理员在「用户权限」页分配。

这个差异决定了「名下节点」区块的语义，见 §5.4。

---

## 3. 设计决策

### 3.1 决策一：审核状态独立于 `is_active`（**核心**）

**方案**：新增审核单表 `user_reviews`，用它的 `status` 表达业务状态；`users.is_active` 退化为**纯登录闸门**。

| | 只用 `is_active`（否决） | 独立审核单表（采纳） |
|---|---|---|
| 表达「已驳回」 | ❌ 与「被禁用」不可区分 | ✅ `status='rejected'` + `reject_reason` |
| 记录审核时间/人 | ❌ 无处存 | ✅ `audited_at` / `audited_by` |
| 审核历史轮次 | ❌ 覆盖式 | ✅ 一用户多单，保留被驳回后的重新提交记录 |
| 与登录解耦 | ❌ 耦合 | ✅ 审核通过 = `status='approved'` **且** `is_active=True`；两个开关可独立（如「审核通过但业务上停用」） |
| 改动量 | 小 | 多一张表 + 迁移 |

**通过** = `reviews.status='approved'` + `users.is_active=True`（一步完成，写审计）。
**驳回** = `reviews.status='rejected'` + `users.is_active=False`（保持不可登录）+ `reject_reason`。

### 3.2 决策二：实名材料存在审核单上，不塞进 `users`

`users` 表是身份表（登录凭据 + 自助维护字段），实名材料是**一次性的审核输入**，会变（补交、改名），且与登录无关。

- `users` 表**不加** `real_name` 列 → 避免把敏感个人信息散布到身份表、避免存量用户默认值歧义。
- 实名材料只在 `user_reviews` 表：`real_name` / `email` / `phone`（手机号存**脱敏快照**）。

> **修订（2026-11-05）**：原文还有 `cert_no_masked` + `cert_no_hash` 两列，
> 现已去掉（不再收集证件）。详见 §12。

### 3.3 决策三：实名材料从哪来 —— 三选一（**需你拍板**）

| 方案 | 用户负担 | 真实度 | 改动 |
|---|---|---|---|
| **A. 注册时用户自填**（推荐） | 注册第二步多 2 个字段（姓名 / 邮箱） | 低（用户自己编） | 注册接口 + 注册页 |
| **B. 管理员审核时手工录入** | 注册无负担 | 中（管理员照证件抄） | 只做审核侧 |
| **C. 第三方 KYC API**（阿里云实人认证 / 腾讯云身份证 OCR） | 用户需授权 | 高 | 需选厂商 + 资质，另开一条线 |

**建议路线**：本期做 **A + B 兼容** —— 注册时实名材料为**可选**（不填也能提交注册，审核单 `status='pending'` 且 `kyc_submitted=false`，管理员在审核弹窗里手工补录后通过）；后续评估 C 替换 A。

这样**不阻塞**「审核页」本身的开发，也给运营留了「先审后补」的现实路径。

> **修订（2026-11-05）**：用户负担从「4 个字段」降到「2 个字段」——
> 姓名与邮箱都可独立留空，手机号是注册时已有的（认证过的号码），不额外收集。

### 3.4 决策四：菜单归属 —— 独立菜单，仅管理员

- 新增菜单码 `user_audit`，`assignable=False`（与 `MENU_PERMISSIONS` 同级）→ 只有超管能进。
- 理由：该页汇聚**全部用户**的实名材料与节点明细，属于全局数据，分配给普通角色会越过数据归属原则（`permissions.py` 的既有约定）。
- 入口双挂：
  1. 「我的」页模块卡片（原型位置）；
  2. 顶部导航不挂（原型也没有）—— 保持与 `permissions` 一致，只在「我的」聚合。

### 3.5 决策五：驳回后允许重新提交

现实里「驳回」必须给出补救路径，否则用户永久卡死。

- **现状**：`phone_auth.register` 对已注册手机号一律 `409`（§A.3 防刷设计）。
- **改动**：`409` 增加例外分支 —— 若该手机号对应的审核单 `status='rejected'`，允许**重新提交**：更新昵称/邮箱/实名材料，审核单 `status` 回到 `pending`（新建一条新的 review 单，保留旧的被驳回记录），`is_active` 仍为 `False`。
- **为什么安全**：能走到这一步的用户**已通过短信验证码验证**（持有该号码），且重复提交不能把「未审核」刷成「已审核」——审核仍然必须由管理员点「通过」。防刷目标不受影响。

---

## 4. 数据模型

### 4.1 新表 `user_reviews`

```python
# backend/app/orm.py
class UserReview(Base):
    """注册用户的一次 KYC 实名审核单。

    与 users.is_active 分工：
      - reviews.status  = 业务审核状态（pending / approved / rejected）
      - users.is_active = 登录闸门（审核通过才置 True）
    一人可有多张单（被驳回后重新提交会新建一张），历史可追溯。
    """
    __tablename__ = "user_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 审核状态
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    # —— KYC 实名材料 ——
    real_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)   # 脱敏快照 138****8000
    # 是否已提交姓名（False = 管理员审核时手工补录）
    kyc_submitted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # —— 审核流转 ——
    submit_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    audited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)   # 审核管理员 username
    reject_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

**关于三个实名材料字段**：
- `real_name` —— 用户自填的姓名，明文存（姓名不是证件级敏感信息）。
- `email` —— 用户自填的邮箱，明文存（联系渠道，与 `users.email` 同口径）。
- `phone` —— **脱敏快照**（`138****8000`），记录「提交审核那一刻的号码」。
  明文号码在 `users.phone`（登录凭据，有唯一索引）；审核单里只留展示用的脱敏值，
  这样「用户后来改绑了手机号」不会篡改历史单据展示的当时信息。

> **修订（2026-11-05）**：原设计还有 `cert_type` / `cert_no_masked` / `cert_no_hash`
> 三列与「掩码 + sha256 哈希」的取舍说明，现已全部删除 —— 不再收集证件，
> 也就没有明文 / 掩码 / 哈希之分了。

### 4.2 迁移

在 `backend/app/db.py` 新增 `_migrate_user_reviews(sync_conn)`，追加到 `init_db()` 调用链，写法参照既有 `_migrate_users_follow_paused`（MySQL 用 `TINYINT(1) NOT NULL DEFAULT 0`，SQLite 用 `BOOLEAN NOT NULL DEFAULT 0`）：

- `users` 表**不动**（零迁移风险）。
- 新表用 `Base.metadata.create_all` 也能建，但本项目已有显式迁移约定 → 走显式迁移函数，MySQL / SQLite 双分支。

**2026-11-05 追加的迁移分支**（表已存在的情况）：

```python
# 表不存在 → 按模型建表
# 表存在 → 删掉 cert_type / cert_no_masked / cert_no_hash 三列，补上 phone 列
```

- DROP COLUMN：MySQL 8.0 ✅ / SQLite 3.35+ ✅（本项目实测 SQLite 3.50）。
- 这三列上没有外键约束，删列不牵连别的表；`cert_no_hash` 上的索引随列一起消失。
- MySQL 与 SQLite 的 DDL 语法在这两件事上一致（`DROP COLUMN` / `ADD COLUMN`），
  所以**不需要方言分支**（与本节其余部分不同）。

### 4.3 `users` 表唯一改动

**不加列**。仅在注册路径上额外写一张 `user_reviews` 单。

> 例外（可选）：若需要「注册时间」精确到秒，用 `users.created_at`（已有）即可，不加列。

---

## 5. 后端设计

### 5.1 新文件 `backend/app/user_review_api.py`

`router = APIRouter(prefix="/api/user-reviews", tags=["user-review"])`，全部接口 `Depends(require_admin)`。

### 5.2 列表接口（聚合，替代前端拼装）

```
GET /api/user-reviews?status=pending|approved|rejected|all&keyword=<str>
→ 200 {
    "stats": { "total": 13, "pending": 6, "approved": 5, "rejected": 2,
               "node_total": 21, "node_online": 14 },
    "items": [ UserReviewOut, ... ]
  }
```

`UserReviewOut`：

```python
class ReviewNodeOut(BaseModel):
    node_id: str
    name: str
    status: str          # online / offline
    balance: float       # 来自 Redis 账户快照；无快照为 0

class UserReviewOut(BaseModel):
    id: int              # review id
    user_id: int
    username: str
    display_name: str | None
    account: str         # 展示用编号，如 "U 7-904101" → 实际用 "U-{user_id}"
    phone: str | None    # 脱敏
    email: str | None
    real_name: str | None
    cert_type: str | None
    cert_no: str | None  # 脱敏
    kyc_submitted: bool
    status: str          # pending / approved / rejected
    register_time: float | None   # users.created_at
    submit_time: float | None     # review.submit_time
    audit_time: float | None
    audited_by: str | None
    reject_reason: str | None
    nodes: list[ReviewNodeOut]
```

**聚合实现要点**：
- 用户列表：`user_service.list_users()`（已有）。
- 节点归属：复用 `users_api._ownership(store)` 的思路，但需要**节点详情**（名称/在线/余额），因此这里直接 `store.all_nodes()` + `permissions.normalize_owner` 分组。
- 在线态：`connections.manager.is_node_online(node_id)`（内存，O(1)）。
- 余额：`store.get_account(node_id)` → `snapshot.get("balance", 0)`；无快照为 `0`（原型里离线节点余额就是 0）。
- 搜索（原型支持 4 个维度）：`username` / `account` / `phone` / `node_id` 任一命中即匹配，**大小写不敏感**。原型是前端过滤；这里放后端是为了支持分页与大数据量。
- 统计：一次遍历列表算出 `stats`，避免前端二次汇总口径不一致。

### 5.3 审核动作接口

```
POST /api/user-reviews/{review_id}/approve
  → 200 { "ok": true, "user_id": 3, "is_active": true }
  副作用：reviews.status='approved'、audited_at/audited_by、users.is_active=True、
          rbac_service.refresh_sessions(受影响 username)、审计

POST /api/user-reviews/{review_id}/reject
  body: { "reason": "证件照片模糊，无法清晰辨认，请重新上传后提交" }
  → 200 { "ok": true, "user_id": 3, "is_active": false }
  副作用：reviews.status='rejected'、reject_reason、audited_at/audited_by、
          users.is_active 保持 False、审计

POST /api/user-reviews/bulk-approve        # 「全部通过」
  body: { "review_ids": [1,2,3] }          # 空数组 = 全部 pending
  → 200 { "ok": true, "approved": 3, "failed": [] }
```

**审计动作**（`category="auth"`）：

| action | target | after |
|---|---|---|
| `user_review_approve` | username | `{review_id, status, is_active}` |
| `user_review_reject` | username | `{review_id, status, reason}` |
| `user_review_bulk_approve` | `bulk` | `{count, usernames[]}` |

> 审计里**不写**证件号（只写 `cert_no_hash` 前 8 位做关联），与现有「手机号在审计里脱敏」的既有做法一致。

### 5.4 「名下节点」的语义（对应 §2.3 的差异）

原型里待审核用户名下已有节点。现实中：

- 待审核用户**无法登录** → 名下节点为空（除非管理员已提前分配）。
- 节点归属由管理员在「用户权限」页 `PUT /api/users/{id}/nodes` 分配。

**处理方案**（二选一，实现时按 §7 的确认结论）：

- **方案 A（保真度优先）**：审核页**只读**展示名下节点；节点分配仍在「用户权限」页做。原型里「审核通过后可创建」的空态文案照搬。
- **方案 B（闭环更好）**：审核「通过」弹窗里加**节点勾选**（复用 `assign_nodes` 的校验与事务），一次审核 + 分配完成。原型通过弹窗已经展示「名下节点 N 个」，天然是勾选的落点。

> 推荐 **方案 B**，因为它把原型的「节点明细统一查看」意图真正闭环了；且 `assign_nodes` 的 409 校验（活动子任务 / 分组成员）已有现成实现，复用即可。

### 5.5 注册侧改动（方案 A 实名自填时）

`backend/app/phone_auth.py` 的 `register_by_phone`：

1. 请求体新增可选字段：`real_name` / `email`。
2. 校验：`real_name` 长度 1~64（填了就必须有名字）；`email` 走正则粗校验
   （`^[^@\s]+@[^@\s]+\.[^@\s]+$`，不填不报错）。**姓名与邮箱各自独立**，
   不再是「四项要么全填要么全空」。
3. 写入 `user_reviews` 一行（`status='pending'`，`kyc_submitted=True`），
   `phone` 存**脱敏快照**（`mask_phone_snapshot`）。
4. `require_approval=false`（免审核模式）时：跳过审核单，直接 `status='approved'` + `is_active=True`，或干脆不建单 —— **建议免审核模式下不建审核单**，保持表内数据语义干净（表内只有「走过审核流程的用户」）。

**被驳回后重新提交**：`register_by_phone` 中，若手机号已存在 **且** 其最新审核单 `status='rejected'` → 允许继续，改为「新建审核单 + 更新 profile」，而不是 `409`。
> ⚠️ 这条改动会让「重复注册防刷」的原文（§A.3）出现例外，需在设计文档里同步标注（本文档 §3.5 已标注）。

> **修订（2026-11-05）**：原第 2、3 步（证件白名单 / 格式校验 / 证件号去重 409）
> 已删除；请求体少两个字段；`kyc_submitted` 的含义从「四项齐全」变成「填了姓名」。

### 5.6 实时性

管理员页面需要能较快看到新注册用户。三选一：

| 方案 | 实现 | 成本 |
|---|---|---|
| **WS 推送**（推荐） | `manager.broadcast_admin({"type":"user_review_pending", ...})` | 后端注册成功时多一次广播；前端 `services/ws.ts` 加一个分支 |
| 轮询 | 前端 `setInterval(30s)` 拉列表 | 零后端改动，但有延迟与无谓流量 |
| 手动刷新 | 按钮 | 最省事，体验最差 |

原型无实时要求，**建议先做轮询（30s）**，把 WS 推送留作增量。

---

## 6. 前端设计

### 6.1 新页面 `frontend/src/views/UserAuditView.vue`

**视觉映射**：原型用 Tailwind 任意值（`bg-[#101828]`、`border-[#1C2740]`…），本项目前端是**自有 CSS 类**（`styles.css` 的 `pf-` 前缀 + 各 View 的 scoped style）。因此**不复用 Tailwind 类名**，而是把原型的**色值 / 尺寸 / 圆角**映射到 `pf-` 体系与本页 scoped style。

原型主色板（直接抄进 scoped style 的 CSS 变量）：

| 用途 | 色值 |
|---|---|
| 页面底 | `#0A0E16` |
| 卡片底 | `#101828` / `#0F1726` |
| 卡片 2 底（输入块） | `#0C1220` / `#0A101E` |
| 边框 | `#1C2740` / `#22304A` / `#161F30` |
| 正文 | `#E6EBF4` |
| 次要文字 | `#8B96A8` |
| 弱文字 | `#5B6B85` |
| 更弱 | `#4A5568` |
| 主蓝 | `#2563EB` / `#3B82F6` |
| 青（KYC 强调） | `#38BDF8` |
| 绿（通过） | `#22C55E` / `#4ADE80` |
| 琥珀（待审核） | `#F59E0B` / `#FBBF24` |
| 红（驳回） | `#F6465D` / `#F87171` |

**页面骨架**：

```
UserAuditView.vue
├─ 页头卡（pf-card + 渐变）
│   ├─ 图标块（38BDF8→2563EB 渐变，19px verified_user）
│   ├─ 标题「用户注册审核」+ how_to_reg 小图标
│   ├─ 频道介绍条（#0A101E 底、#38BDF8 加粗前缀）
│   └─ 统计块 grid-cols-3（6 项，数字用等宽字体）
├─ Tab 栏（待审核 N | 已审核 N）+ [全部通过]（条件显示）
├─ 搜索框（左侧 search 图标 + 清空按钮）
└─ 卡片列表（v-for，空态用虚线边框居中块）
     ├─ UserAuditCard.vue（待审核 / 已审核两种形态）
     │    ├─ 头像（首字；待审核琥珀渐变 / 通过绿 / 驳回红）
     │    ├─ 用户名 + 账号徽章 + 注册时间 · 手机号（已审核换审核时间）
     │    ├─ 状态徽章（待审核带脉冲点）
     │    ├─ KYC 实名材料块（badge 图标 + 3 行字段）
     │    ├─ 驳回原因红条（仅 rejected）
     │    └─ 名下节点折叠块（展开 → 节点行：图标/名称/ID/在线态/余额）
     ├─ 审核操作条（仅 pending：驳回 / 通过）
     └─ 折叠状态按 review_id 记在组件内 ref
```

### 6.2 弹窗

沿用项目已有的 `confirmAction` 工具（`frontend/src/utils/confirm.ts`）做**通过**确认（简单文本确认即可）；**驳回**弹窗需要自定义（5 个预设 chip + textarea），参考 `ProfileView.vue` 里「带单员编辑」的展开式编辑区风格，或参考 `PermissionView.vue` 的弹窗表单。

驳回预设文案（与原型一致，存常量）：

```
证件照片模糊，无法清晰辨认，请重新上传后提交
实名信息与上传证件不一致
证件已过期，请更新有效证件
人脸识别与证件照片不匹配
注册信息填写不完整，请补充后重新提交
```

### 6.3 路由与菜单

```ts
// frontend/src/router/index.ts
{ path: '/user-audit', name: 'user-audit',
  component: () => import('@/views/UserAuditView.vue'),
  meta: { menu: 'user_audit' } },
```

```ts
// frontend/src/constants/menus.ts
{ code: 'user_audit', path: '/user-audit' },
```

```python
# backend/app/permissions.py
MENU_USER_AUDIT = "user_audit"
...
MenuDef(MENU_USER_AUDIT, "用户注册审核", "/user-audit", False),   # 仅管理员
```

### 6.4 「我的」页入口

`frontend/src/views/MineView.vue` 的 `ALL_MODULES` 增加一项（原型配色 `#38BDF8`）：

```ts
{
  code: 'user_audit',
  label: '用户注册审核',
  description: '注册用户 · KYC 实名审核 · 节点明细',
  path: '/user-audit',
  icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
  accent: '#38BDF8',
}
```

由于 `visibleModules` 已按 `auth.hasMenu(m.code)` 过滤，而 `user_audit` 仅管理员有 → **普通用户不会看到该入口**，无需额外判断。

> 注意：`ALL_MODULES` 目前的过滤逻辑是 `auth.isAdmin ? ALL_MODULES : filter(hasMenu)`，管理员直接全放行 → 管理员自动可见。

### 6.5 API 与类型

```ts
// frontend/src/api/types.ts
export interface ReviewNodeOut { node_id: string; name: string; status: 'online'|'offline'; balance: number }
export interface UserReviewOut { /* 对齐 §5.2 */ }
export interface UserReviewStats { total: number; pending: number; approved: number; rejected: number; node_total: number; node_online: number }
export interface UserReviewList { stats: UserReviewStats; items: UserReviewOut[] }
```

```ts
// frontend/src/stores/userReview.ts（新建 store）
fetchReviews(params) / approveReview(id) / rejectReview(id, reason) / bulkApprove(ids)
```

### 6.6 注册页（方案 A 才有）

`frontend/src/views/RegisterView.vue` 第二步新增 **2 个字段**：姓名 / 邮箱。
- 邮箱与个人中心的 `users.email` 同步写入（注册时一并填，避免用户还要去个人中心补）。
- 两项**各自独立、可留空**：留空姓名 = 不提交实名材料（审核单 `kyc_submitted=false`），
  管理员在审核时手工补录。
- 姓名 `maxlength="64"`；邮箱 `type="email"` + 前端正则粗校验（后端为准）。

> **修订（2026-11-05）**：原 4 个字段（姓名 / 证件类型下拉 / 证件号 / 邮箱）→ 2 个字段。
> 证件类型下拉与证件号输入框整体删除，`CERT_TYPES` 常量也一并删除。

---

## 7. 待确认（阻塞项）

> ✅ **已全部确认**（2026-11-04）：1=用户自填、2=是（弹窗内分配节点）、3=允许、
> 4=独立菜单、5=不留档、6=用管理员菜单即可。下表保留原决策点作为对照。

| # | 决策点 | 建议 | 影响 |
|---|---|---|---|
| 1 | **实名材料来源** | 本期做 A+B 兼容：注册时可选自填，审核时可手工补录 | 决定 §5.5 是否要做、注册页改动量 |
| 2 | **审核通过是否同时分配节点** | 建议方案 B（通过弹窗里勾选节点） | 决定是否复用 `assign_nodes`、是否改审核弹窗 |
| 3 | **驳回后是否允许重新提交** | 建议允许（改 `phone_auth` 的 409 分支） | 改 `phone_auth.py` 的重复注册分支 |
| 4 | **「用户注册审核」是否独立菜单** | 建议独立菜单 `user_audit`，仅管理员 | 决定 `permissions.py` / `menus.ts` 改动 |
| 5 | **证件号明文是否留档** | 本期**不留**（只存掩码 + 哈希） | 若合规要求留档，需引入加密存储或第三方 KYC |
| 6 | **实名材料是否算「敏感个人信息」需要额外权限/日志** | 本期按管理员可见 + 审计留痕处理 | 决定是否要独立于 `permissions` 菜单的细粒度能力码 |

> **决策 5 已被 2026-11-05 的简化撤销**：不再收集证件号，「明文是否留档」这个问题
> 不复存在。决策 1/2/3/4/6 保持不变。

---

## 8. 实施拆解（预估）

| 阶段 | 内容 | 文件 | 人日 |
|---|---|---|---|
| **P0** | 数据模型 + 迁移 | `backend/app/orm.py`、`backend/app/db.py` | 0.5 |
| **P0** | 审核 API（列表聚合 / 通过 / 驳回 / 批量）+ 审计 | `backend/app/user_review_api.py`（新）、`models.py`、`main.py` 注册路由 | 1.5 |
| **P0** | 菜单码 + 「我的」入口 + 路由 | `backend/app/permissions.py`、`frontend/src/constants/menus.ts`、`frontend/src/router/index.ts`、`frontend/src/views/MineView.vue` | 0.5 |
| **P1** | 前端页面（页头/统计/Tab/搜索/卡片/折叠/弹窗） | `frontend/src/views/UserAuditView.vue`（新）、`components/UserAuditCard.vue`（新）、`api/types.ts`、`stores/userReview.ts`（新） | 2 |
| **P1** | 注册侧实名自填（若选 A） | `backend/app/phone_auth.py`、`frontend/src/views/RegisterView.vue` | 1 |
| **P2** | 通过时分配节点（若选方案 B） | `backend/app/user_review_api.py`、`UserAuditView.vue` | 1 |
| **P2** | WS 推送新待审核（可选，替代轮询） | `backend/app/connections.py`、`frontend/src/services/ws.ts` | 0.5 |
| **P1** | 测试（后端单测 + 集成 + 前端类型检查 + 构建） | `backend/tests/test_user_review.py`（新） | 1.5 |
| — | **合计** | | **约 8 人日** |

---

## 9. 测试要点

### 9.1 后端单测

- 审核单 CRUD：建单 → 通过 → 状态流转；重复审核同一单 → `409`。
- 通过后 `users.is_active=True`，`/api/auth/sms/verify` 返回 `next_action='login'`（端到端闭环验证）。
- 驳回后 `is_active` 仍为 `False`，`next_action='pending_approval'`，`reject_reason` 正确落库。
- 列表聚合：统计数与列表一致；搜索命中 4 个维度（用户名/账号/手机号/节点 ID）各一例；节点明细的在线态与余额正确。
- 权限：非管理员访问 → `403`；未登录 → `401`。
- 批量通过：部分失败（已被他人处理）时返回 `failed` 明细，不中断其余。
- 实名材料：邮箱格式非法 → `400`；只填邮箱不填姓名 → 允许（`kyc_submitted=false`）；
  姓名超长 → `400`；表里**没有** `cert_*` 列；`phone` 列存的是脱敏快照（`138****8000`）。
- 审计：每次审核写一条 `category='auth'` 的记录，`after` 含状态变化；`params` 里不出现手机号明文。

### 9.2 回归

- 原 `/api/login` + 2FA 不变。
- 手机号注册 → 待审核 → 审核通过 → 验证码登录全链路打通。
- 「用户权限」页的用户列表、启用/禁用、角色、节点分配不受影响。
- 「我的」页模块入口数量与权限过滤正确（普通用户看不到「用户注册审核」）。

### 9.3 手工

- 管理员登录 → 我的 → 用户注册审核 → 卡片、Tab 计数、搜索、折叠、两个弹窗、全部通过。
- 被驳回用户重新注册能走通（若 §7-3 选「允许」）。
- 窄屏（375px）下卡片不溢出。

---

## 10. 与既有设计文档的关系

- `docs/superpowers/specs/2026-11-01-phone-register-aliyun-design.md` §A.3「重复注册直接拒绝」需**追加例外**（§3.5 / §5.5）。
- 该文档 §6.5「管理员审核」原写的是「复用 `users_api` 能力，不新增接口」—— 本方案**改为新增独立审核接口**，理由见 §3.1（`is_active` 表达不了「已驳回」）。该文档的 §16-2「是否需要管理员批量审核入口」的开放问题，本方案给出答案：**需要**，即「全部通过」。

---

## 11. 实施结果与本文档的差异（2026-11-04）

### 11.1 落地清单

| 层 | 文件 | 状态 |
|---|---|---|
| 数据模型 | `backend/app/orm.py`（`UserReview`） | ✅ |
| 迁移 | `backend/app/db.py`（`_migrate_user_reviews`） | ✅ |
| 服务层 | `backend/app/user_review_service.py`（校验 / 脱敏 / 持久化） | ✅ |
| 审核 API | `backend/app/user_review_api.py` | ✅ |
| 节点分配复用 | `backend/app/users_api.py`（抽出 `assign_nodes_core`） | ✅ |
| 菜单码 | `backend/app/permissions.py`（`MENU_USER_AUDIT`，`assignable=False`） | ✅ |
| 路由注册 | `backend/app/main.py` | ✅ |
| 注册侧 | `backend/app/phone_auth.py`（实名自填 + 驳回后重提） | ✅ |
| 数据模型（Pydantic） | `backend/app/models.py` | ✅ |
| 测试 | `backend/tests/test_user_review.py`（38 例） | ✅ |
| 迁移测试 | `backend/tests/test_user_review_migration.py`（5 例） | ✅ |
| 前端类型 | `frontend/src/api/types.ts` | ✅ |
| 前端 store | `frontend/src/stores/userReview.ts` | ✅ |
| 审核页 | `frontend/src/views/UserAuditView.vue` | ✅ |
| 卡片 | `frontend/src/components/UserAuditCard.vue` | ✅ |
| 通过弹窗 | `frontend/src/components/UserAuditApproveModal.vue` | ✅ |
| 驳回弹窗 | `frontend/src/components/UserAuditRejectModal.vue` | ✅ |
| 路由 / 菜单 | `frontend/src/router/index.ts`、`frontend/src/constants/menus.ts` | ✅ |
| 「我的」入口 | `frontend/src/views/MineView.vue` | ✅ |
| 注册页 | `frontend/src/views/RegisterView.vue` | ✅ |
| 登录页 | `frontend/src/views/LoginView.vue`（`rejected` 分支） | ✅ |

### 11.2 与本文档不同的几处

1. **掩码位数**：本文档示例写 `4401**********2216`（前 4 + 10 星 + 后 4），
   但示例原号 `440301199001011234` 的前 4 位是 `4403`。实现统一取
   「前 4 + 10 星 + 后 4」，示例值相应为 `4403**********1234`。

2. **迁移写法**：本文档 §4.2 建议手写 MySQL / SQLite 双分支 DDL。实现改为
   **让 SQLAlchemy 按方言建表**（`UserReview.__table__.create`）——
   `AUTOINCREMENT` 是 SQLite 专有语法、`CREATE INDEX IF NOT EXISTS` MySQL
   不支持，手写要维护两套分支；新表没有存量数据要兼容，让 ORM 生成更省事。

3. **`cert_no` 掩码位数与校验**：`_CERT_PATTERNS` 按证件类型分别校验
   （身份证 18 位含 X、护照 `[A-Za-z0-9]{5,17}`、港澳通行证 `[A-Za-z0-9]{8,12}`），
   与 §5.5 一致。

4. **审计不含证件号**：§5.3 写「只写 `cert_no_hash` 前 8 位做关联」。实现
   **完全不写**证件号相关字段（连哈希前 8 位也不写）—— 审计里出现任何证件
   派生物件都不必要，`review_id` 已足够关联。

5. **「已审核」Tab 的实现**：原型只有「待审核 / 已审核」两个 Tab，而状态有
   三种。后端 `status` 参数支持三种，前端「已审核」Tab 取 `all` 后在前端分成
   「已通过 / 已驳回」两档展示。统计块始终是全局数（不随筛选变化）。

6. **实时性**：按 §5.6 建议实现了 **30s 轮询**，未做 WS 推送。

7. **新增的 `next_action='rejected'`**：§3.5 只说了允许重提，实现时补上了
   这个分流值 —— 否则前端无法区分「等审核」与「被驳回可重提」，
   用户会被永远挡在注册页外。

8. **证件号去重在首次注册也查**：§5.5 第 3 点只提了去重，实现时在
   **首次注册**路径也加了同一证件号检查（原先只在重提路径查，是个漏洞）。
   （此项已被 2026-11-05 的简化撤销 —— 证件号去重整体不存在了。）

### 11.3 顺带的重构

`users_api.assign_nodes` 拆成「路由壳 + `assign_nodes_core`」：
`assign_nodes` 只做参数转调，核心逻辑（活动子任务 409 / 分组成员 409 /
写库 / 刷会话 / 审计）整体搬进 `assign_nodes_core`。审核通过时复用它，
保证「用户权限页分配节点」与「审核通过时分配节点」两条路径的校验口径
**完全一致** —— 不复制粘贴一份，自然不会漂移。

---

## 12. 实名材料简化（2026-11-05）

### 12.1 变更点

**实名材料从「姓名 / 证件类型 / 证件号 / 邮箱」四项简化为「姓名 / 邮箱 / 手机号」三项。**

| 项 | 之前 | 之后 |
|---|---|---|
| 姓名 | 必填（四项齐全才成立） | 填了就有；**不填 = 不提交实名材料**，可单独填邮箱 |
| 邮箱 | 必填 | **可独立选填**，只做格式校验 |
| 证件类型 | 必填（白名单 3 种） | **删除** |
| 证件号 | 必填（按类型正则 + sha256 去重） | **删除** |
| 手机号 | （隐含在 `users.phone`） | 存**脱敏快照**到审核单 |

**连带删除的东西**（全部）：

- 表列：`user_reviews.cert_type` / `cert_no_masked` / `cert_no_hash`；
- 服务函数：`CERT_TYPES` / `_CERT_PATTERNS` / `normalize_cert_no` /
  `mask_cert_no` / `cert_no_hash` / `cert_no_taken`；
- 校验规则：证件类型白名单、按类型的正则格式校验、「四项要么全填要么全空」；
- 错误分支：`409`「该证件已被注册」（首次注册与重提两处）；
- 前端：`CERT_TYPES` 常量、证件类型下拉、证件号输入框与前端正则、
  `cert_type` / `cert_no` 两个响应字段、4 条提到「证件」的驳回预设文案。

**保留不变**：审核通过时**同时分配节点**（`assign_nodes_core`），
以及 §3.1~§3.5 的全部其他决策。

### 12.2 为什么这样改

1. **风险大幅下降**：证件号是证件级敏感个人信息，收集它意味着
   存储、脱敏、哈希、去重、审计留痕一整套合规负担。去掉之后这套全部消失。
2. **与原型一致**：原型卡片的 KYC 块本来就只显示 3 行
   （实名姓名 / 邮箱 / 手机号）。原实现「多显示 2 行」是对原型的偏差 ——
   改回去反而是**还原**原型，不是简化。
3. **手机号已经验过了**：注册走的是短信验证码，手机号在 `users.phone` 里
   有唯一索引、且是已验证状态。审核单存一份脱敏快照只是为了「展示当时信息」，
   不承担唯一性职责。

### 12.3 手机号快照的口径

- 存的是**脱敏**值（`138****8000`），与 `sms_service.mask_phone` 同口径
  （去国家码前缀、前 3 后 4、中间 4 位打码）。
- 复用既有函数而不是另写一份 —— 两处脱敏规则若不一致，同一个号码在
  审核页与审计里会长得不一样。
- 列表接口的 `phone` 优先取审核单快照，没有快照时回退到用户当前号码。

### 12.4 驳回预设文案（替换后）

```
· 姓名信息填写不完整，请补充后重新提交
· 姓名与手机号实名认证信息不一致
· 邮箱无效或无法接收通知，请更换
· 注册信息存在明显异常，请补充说明
```

### 12.5 迁移

`backend/app/db.py` 的 `_migrate_user_reviews` 增加分支：

```
表不存在 → 按模型建表
表存在   → 先 DROP 挂在 cert_no_hash 上的索引
           → DROP COLUMN cert_type / cert_no_masked / cert_no_hash
           → ADD COLUMN phone VARCHAR(20) NULL
```

⚠️ **两个方言差异（都踩过）**：

1. **SQLite 删列不会连带删索引** —— 留着 `ix_user_reviews_cert_no_hash`
   再删 `cert_no_hash` 列，SQLite 报
   `error in index ix_user_reviews_cert_no_hash after drop column:
   no such column: cert_no_hash`，整条迁移失败。**必须先 `DROP INDEX`。**
2. **`DROP INDEX` 的语法两方言不同** ——
   MySQL 是 `DROP INDEX <name> ON <table>`（**没有** `IF EXISTS`），
   SQLite 是 `DROP INDEX IF EXISTS <name>`。所以这一处**必须按方言分支**。

DROP COLUMN 支持度：MySQL 8.0 ✅ / SQLite 3.35+ ✅（本项目实测 SQLite 3.50）。

回归用例见 `backend/tests/test_user_review_migration.py`（5 例），
其中 `test_migration_drops_index_before_dropping_its_column` 专门盯第 1 点。

### 12.6 涉及文件清单

| 层 | 文件 | 改动 |
|---|---|---|
| 数据模型 | `backend/app/orm.py` | 删 3 列，加 `phone` |
| 迁移 | `backend/app/db.py` | 加「删旧列 + 补新列」分支 |
| 服务层 | `backend/app/user_review_service.py` | 删证件全套；`validate_kyc(real_name, email)`；加 `mask_phone_snapshot` |
| 审核 API | `backend/app/user_review_api.py` | 响应去掉 `cert_type` / `cert_no`；`phone` 优先取快照 |
| 注册侧 | `backend/app/phone_auth.py` | 删证件校验与去重；建单时写手机号快照 |
| 数据模型（Pydantic） | `backend/app/models.py` | `UserReviewOut` / `PhoneRegisterRequest` 去 cert 字段 |
| 测试 | `backend/tests/test_user_review.py` | 删证件用例；加「表里无 cert 列」「手机号存脱敏值」用例 |
| 前端类型 | `frontend/src/api/types.ts` | 删 `cert_type` / `cert_no` / `CERT_TYPES`；换驳回预设 |
| 卡片 | `frontend/src/components/UserAuditCard.vue` | KYC 块从 5 行变 3 行 |
| 注册页 | `frontend/src/views/RegisterView.vue` | 删证件类型下拉 + 证件号输入；删前端正则校验 |

