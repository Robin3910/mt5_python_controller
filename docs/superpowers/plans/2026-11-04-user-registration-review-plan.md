# 用户注册审核（KYC 实名审核）Implementation Plan

> 日期：2026-11-04
> 配套设计：`docs/superpowers/specs/2026-11-04-user-registration-review-design.md`
> 状态：**已完成**（2026-11-04）
> 原型：`UI/H5.html` →【我的】→【用户注册审核】

---

## 已确认的决策（2026-11-04 用户确认）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 实名材料来源 | **用户自填**（注册第二步收集） |
| 2 | 审核通过时是否同时分配节点 | **是**（通过弹窗里勾选节点） |
| 3 | 驳回后是否允许重新提交 | **允许** |
| 4 | 菜单归属 | **独立菜单** `user_audit`，仅管理员 |
| 5 | 证件号明文是否留档 | ~~不留（只存掩码 + sha256 哈希）~~ → **已被 2026-11-05 简化撤销**（不再收集证件） |
| 6 | 权限粒度 | **用管理员菜单即可**，不新增能力码 |

> **2026-11-05 简化**：实名材料从「姓名 / 证件类型 / 证件号 / 邮箱」四项
> 简化为「姓名 / 邮箱 / 手机号」三项，证件相关全部删除。
> 决策 2（审核通过时同时分配节点）**保持不变**。
> 详见设计文档 §12。

---

## Global Constraints

1. **审核状态独立于 `users.is_active`**：`user_reviews.status` 表达业务状态（pending/approved/rejected），`users.is_active` 退化为**纯登录闸门**。「通过」= `status='approved'` **且** `is_active=True`；「驳回」= `status='rejected'` **且** `is_active` 保持 False。
2. **`users` 表不加任何列**。实名材料只落在 `user_reviews` 表。
3. **实名材料只有三项：姓名 / 邮箱 / 手机号**（2026-11-05 简化）。姓名与邮箱**各自独立可选**；手机号取注册时已验证的号码，存**脱敏快照**（`138****8000`）。不收集证件类型与证件号。
4. **手机号脱敏复用 `sms_service.mask_phone`**（去国家码前缀、前 3 后 4、中间 4 位打码）—— 不另写一份，避免同一号码在审核页与审计里长得不一样。
5. **不引入第三方 KYC**。本期只做「用户自填 + 管理员审核」。
6. **`user_audit` 菜单 `assignable=False`** —— 只有超管能进，普通角色不可分配（该页汇聚全部用户的实名材料与节点明细，属全局数据）。
7. **审核通过时分配节点必须复用 `users_api.assign_nodes` 的既有校验**（活动子任务 409 / 分组成员 409 confirm_required），**不得另写一套**。
8. **驳回后重新提交不削弱防刷**：能走到重提的用户**已通过短信验证码验证**（持有该号码）；重提只能把审核单打回 `pending`，**审核通过仍必须管理员点**。
9. **免审核模式（`require_approval=false`）不建审核单** —— 保持 `user_reviews` 表内数据语义干净（表内只有走过审核流程的用户）。
10. **审计里不写手机号明文**（只写脱敏值 `138****8000`），与既有「手机号在审计里脱敏」口径一致。
11. 前端**不复用 Tailwind 类名**（原型是 Tailwind，本项目是自有 `pf-` CSS 体系）——把原型的色值/尺寸映射到 `pf-` 体系 + 本页 scoped style。
12. 每期结束必须跑全量单测（`backend`）+ 前端 `npm run typecheck`。

---

## File Structure

| 文件 | 说明 |
|---|---|
| `backend/app/orm.py` | 新增 `UserReview` 模型 |
| `backend/app/db.py` | 新增 `_migrate_user_reviews` + 挂进 `init_db()` |
| `backend/app/user_review_service.py` | **新** —— 审核单的持久化 / 状态流转（纯数据层） |
| `backend/app/user_review_api.py` | **新** —— 审核 API（列表聚合 / 通过 / 驳回 / 批量） |
| `backend/app/models.py` | 新增 `UserReviewOut` / `ReviewNodeOut` / `UserReviewStats` / `UserReviewList` / `UserReviewDecision` 等 Pydantic 模型 |
| `backend/app/phone_auth.py` | 注册侧：实名字段入参 + 审核单创建 + 驳回后重提分支 |
| `backend/app/users_api.py` | 抽出 `assign_nodes` 核心逻辑供审核通过复用 |
| `backend/app/permissions.py` | 新增 `MENU_USER_AUDIT` + 注册表条目 |
| `backend/app/main.py` | 注册 `user_review_api.router` |
| `backend/tests/test_user_review.py` | **新** —— 审核流程端到端用例 |
| `frontend/src/api/types.ts` | 新增审核相关类型 |
| `frontend/src/stores/userReview.ts` | **新** —— 审核 store |
| `frontend/src/views/UserAuditView.vue` | **新** —— 审核主页面 |
| `frontend/src/components/UserAuditCard.vue` | **新** —— 审核卡片（待审核/已审核两形态） |
| `frontend/src/components/UserAuditRejectModal.vue` | **新** —— 驳回弹窗（预设理由 chip + 补充说明） |
| `frontend/src/components/UserAuditApproveModal.vue` | **新** —— 通过弹窗（含节点勾选） |
| `frontend/src/views/RegisterView.vue` | 注册第二步新增实名 2 字段（姓名 / 邮箱） |
| `frontend/src/views/MineView.vue` | 「我的模块」加「用户注册审核」入口 |
| `frontend/src/router/index.ts` | 新增 `/user-audit` 路由 |
| `frontend/src/constants/menus.ts` | 新增 `user_audit` 菜单项 |
| `docs/superpowers/specs/2026-11-01-phone-register-aliyun-design.md` | 追加「重复注册」例外说明 |

---

## Task 1: 数据模型 —— `user_reviews` 表

**Files**: `backend/app/orm.py`、`backend/app/db.py`

### Step 1: 加模型

在 `orm.py` 的 `User` 之后加：

```python
class UserReview(Base):
    """注册用户的一次 KYC 实名审核单。

    与 `users.is_active` 的分工（两者不可互相替代）：
      - `status`  = 业务审核状态（pending / approved / rejected）
      - `users.is_active` = 登录闸门（审核通过才置 True）

    一人可有多张单：被驳回后重新提交会**新建**一张，旧单保留，审核历史可追溯。

    **实名材料只有三项**：姓名 / 邮箱 / 手机号（手机号为脱敏快照）。
    """
    __tablename__ = "user_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    # —— 实名材料：姓名 / 邮箱 / 手机号 ——
    real_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 脱敏快照
    # 用户是否自填了姓名（False = 管理员审核时手工补录）
    kyc_submitted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # —— 审核流转 ——
    submit_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    audited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

### Step 2: 加迁移

`db.py` 新增 `_migrate_user_reviews(sync_conn)`，写法参照既有 `_migrate_users_follow_paused`（MySQL 用 `TINYINT(1) NOT NULL DEFAULT 0`，SQLite 用 `BOOLEAN NOT NULL DEFAULT 0`）。

```python
def _migrate_user_reviews(sync_conn) -> None:
    """user_reviews 表：注册用户的 KYC 实名审核单。

    表不存在 → 按模型建表（让 SQLAlchemy 生成方言正确的 DDL）。
    表存在   → 删掉 2026-11-05 简化前的证件三列，补上 phone 列。
    """
    inspector = inspect(sync_conn)
    if "user_reviews" not in inspector.get_table_names():
        UserReview.__table__.create(bind=sync_conn, checkfirst=True)
        return

    cols = {c["name"] for c in inspector.get_columns("user_reviews")}
    for legacy in ("cert_type", "cert_no_masked", "cert_no_hash"):
        if legacy in cols:
            sync_conn.execute(text(f"ALTER TABLE user_reviews DROP COLUMN {legacy}"))
    if "phone" not in cols:
        sync_conn.execute(text("ALTER TABLE user_reviews ADD COLUMN phone VARCHAR(20) NULL"))
```

> **实施时的差异（2026-11-04）**：计划原本让手写 MySQL / SQLite 双分支 DDL，
> 实现改为**让 SQLAlchemy 按方言建表**（`UserReview.__table__.create`）——
> `AUTOINCREMENT` 是 SQLite 专有语法、`CREATE INDEX IF NOT EXISTS` MySQL
> 不支持，手写要维护两套分支；新表没有存量数据要兼容，让 ORM 生成更省事。
>
> **2026-11-05 追加**：表已存在时的「删旧列 + 补新列」分支。
> ⚠️ **SQLite 删列不会连带删索引** —— 必须先 `DROP INDEX` 再删列，否则迁移
> 直接报错（`error in index ... after drop column`）。
> ⚠️ **`DROP INDEX` 语法两方言不同**：MySQL `DROP INDEX x ON t`（无 `IF EXISTS`），
> SQLite `DROP INDEX IF EXISTS x` —— 这一处必须按方言分支。
> 回归用例：`backend/tests/test_user_review_migration.py`。

### Step 3: 挂进 init_db

`init_db()` 末尾追加：

```python
await conn.run_sync(_migrate_user_reviews)
```

### Step 4: 验证

- `python -c "import app.orm"` 通过
- 起一次 `init_db()` 不报错
- 后端既有测试通过（`python -m pytest backend/tests/ -q`）

---

## Task 2: 实名材料的校验（姓名 / 邮箱）

**Files**: **新** `backend/app/user_review_service.py`

> **2026-11-05 简化**：原计划的「证件类型白名单」「证件掩码」「证件哈希」
> 三个 Step 全部删除 —— 不再收集证件。手机号的脱敏直接复用
> `sms_service.mask_phone`（既有函数，口径统一）。

### Step 1: 实名材料校验

```python
class KycInvalid(ValueError):
    """实名材料不合法。"""


def validate_kyc(real_name: str | None, email: str | None) -> Optional[dict]:
    """校验并归一化一份实名材料（姓名 / 邮箱）。

    返回 {"real_name", "email"}；未填姓名时返回 None。
    """


def mask_phone_snapshot(phone: str | None) -> str | None:
    """审核单里的手机号快照（脱敏）：13800138000 -> 138****8000。"""
```

**校验规则**：

| 字段 | 规则 |
|---|---|
| 姓名未填 | 合法（返回 `None`，表示用户未提交实名材料） |
| 姓名 | 1~64 字符 |
| 邮箱未填 | 合法 |
| 邮箱已填 | 匹配 `^[^@\s]+@[^@\s]+\.[^@\s]+$`，否则 `KycInvalid("邮箱格式不正确")` |

> 姓名与邮箱**各自独立** —— 不再有「四项要么全填要么全空」的规则。

### Step 2: 验证

`python -c "from app.user_review_service import validate_kyc, mask_phone_snapshot"` 通过。

---

## Task 3: 审核单持久化层

**Files**: `backend/app/user_review_service.py`

### Step 1: 基础读写

```python
async def create_review(user_id: int, *, real_name=None, email=None, phone=None,
                        kyc_submitted=True) -> UserReview
async def get_review(review_id: int) -> Optional[UserReview]
async def latest_review(user_id: int) -> Optional[UserReview]
async def list_reviews(status: str | None = None) -> list[UserReview]
```

`latest_review` 按 `id DESC` 取第一行（不用时间排序 —— 时间戳精度可能相同，id 严格递增更可靠）。

### Step 2: 状态流转

```python
async def approve_review(review_id: int, *, auditor: str) -> Optional[UserReview]:
    """审核通过：status='approved'，记录审核人与时间，清掉驳回原因。"""

async def reject_review(review_id: int, *, auditor: str, reason: str) -> Optional[UserReview]:
    """审核驳回：status='rejected'，记录原因与审核人。"""
```

两者都**不改** `users.is_active` —— 那是 API 层的编排（因为要连带会话吊销 + 审计）。

### Step 3: 验证

`python -c "import app.user_review_service"` 通过。

> **2026-11-05 简化**：原 Step 3「证件号去重」（`cert_no_taken`）已删除。

---

## Task 4: 审核列表聚合（后端）

**Files**: **新** `backend/app/user_review_api.py`、`backend/app/models.py`

### Step 1: Pydantic 模型

`models.py` 新增：

```python
class ReviewNodeOut(BaseModel):
    node_id: str
    name: str
    status: str = "offline"      # online / offline
    balance: float = 0.0

class UserReviewStats(BaseModel):
    total: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    node_total: int = 0
    node_online: int = 0

class UserReviewOut(BaseModel):
    id: int                       # review id
    user_id: int
    username: str
    display_name: Optional[str] = None
    account: str                  # 展示编号，如 "U-12"
    phone: Optional[str] = None   # 脱敏 138****8000
    email: Optional[str] = None
    real_name: Optional[str] = None
    kyc_submitted: bool = False
    status: str = "pending"       # pending / approved / rejected
    register_time: Optional[float] = None
    submit_time: Optional[float] = None
    audit_time: Optional[float] = None
    audited_by: Optional[str] = None
    reject_reason: Optional[str] = None
    nodes: list[ReviewNodeOut] = Field(default_factory=list)

class UserReviewList(BaseModel):
    stats: UserReviewStats = Field(default_factory=UserReviewStats)
    items: list[UserReviewOut] = Field(default_factory=list)
```

### Step 2: 聚合逻辑

在 `user_review_api.py`：

```python
async def _collect(store: RedisStore) -> list[UserReviewOut]:
    """把「用户 + 审核单 + 名下节点」聚合成一份列表。

    节点归属用 owner_user_id 分组；在线态取内存连接表；
    余额取 Redis 账户快照（无快照 = 0，与原型「离线节点余额 0」一致）。
    """
```

要点：

| 项 | 来源 |
|---|---|
| 用户 | `user_service.list_users()` |
| 审核单 | `user_review_service.list_reviews()` |
| 每个用户取**最新一张**审核单 | `max(reviews, key=lambda r: r.id)` |
| 节点归属 | `store.all_nodes()` → `permissions.normalize_owner(n.get("owner_user_id"))` 分组 |
| 节点在线 | `connections.manager.is_node_online(node_id)` |
| 节点余额 | `store.get_account(node_id)` → `.get("balance", 0.0)` |
| 手机号脱敏 | `sms_service.mask_phone(user.phone)` |
| 账号编号 | `f"U-{user.id}"` |

**没有审核单的用户不出现** —— 本表只列「走过审核流程的用户」（Global Constraint 9）。

### Step 3: 列表接口

```python
@router.get("", response_model=UserReviewList)
async def list_user_reviews(
    status: str | None = None,
    keyword: str | None = None,
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
```

**筛选**：
- `status` ∈ {`pending`, `approved`, `rejected`, `all`/None}；非法值 → `400`。
- `keyword`：对 **4 个维度**做**大小写不敏感**的子串匹配 —— `username` / `account` / `phone`（脱敏串与原串都试）/ 任一 `node_id`。

**统计**：`stats` 反映**全部**审核单（不受 `status` 筛选影响），这样 Tab 上的计数始终是全局的（与原型一致：Tab 徽章显示的是该 Tab 的总数，不是筛选后的数量）。

> ⚠️ 统计口径要写清：`stats.pending` = 全部 pending 数；`items` = 按 `status` + `keyword` 过滤后的列表。原型里 Tab 徽章与统计块都是全局数。

### Step 4: 验证

- `python -c "import app.user_review_api"` 通过
- 手工：`GET /api/user-reviews` 返回 `{stats, items}` 结构

---

## Task 5: 审核动作接口

**Files**: `backend/app/user_review_api.py`、`backend/app/users_api.py`

### Step 1: 抽出 assign_nodes 核心

`users_api.assign_nodes` 现在是一个路由函数，内部逻辑（去重 → 存在性 → 活动子任务检查 → 分组成员检查 → 移出分组 → 改归属 → 刷新会话 → 审计）约 90 行。**审核通过时要复用它**，不能抄一份。

做法：把主体抽成 `users_api.assign_nodes_core(user_id, node_ids, *, confirm, principal, ip, store) -> dict`，路由函数只做「取 body → 调 core → 返回」。

```python
async def assign_nodes_core(
    user_id: int,
    node_ids: list[str],
    *,
    confirm: bool = False,
    p: Principal,
    ip: str | None,
    store: RedisStore,
) -> dict:
    """分配节点的核心逻辑（原 assign_nodes 路由的主体）。

    审核通过时复用此函数，保证「活动子任务 / 分组成员」两条校验口径完全一致。
    """
```

原路由改为薄封装：

```python
@router.put("/{user_id}/nodes")
async def assign_nodes(user_id: int, body: UserNodesUpdate, request: Request,
                       store: RedisStore = Depends(get_store), p: Principal = Depends(require_admin)):
    return await assign_nodes_core(user_id, body.node_ids, confirm=body.confirm,
                                   p=p, ip=client_ip(request), store=store)
```

### Step 2: 通过接口

```
POST /api/user-reviews/{review_id}/approve
body: { "node_ids": ["7-904101-01"] }   # 可选；不传 = 不改节点归属
```

流程：

1. 取审核单，不存在 → `404`。
2. `status != 'pending'` → `409`「该审核单已处理」。
3. `user_review_service.approve_review(review_id, auditor=p.username)`。
4. `user_service.update_user(user_id, is_active=True)` → 置登录闸门开。
5. 若 `node_ids` 非空 → `users_api.assign_nodes_core(...)`；**若返回 409（活动子任务 / 分组成员）** → 整个操作回滚并向前端透传 409 的 `detail`（`reason` / `message` / `nodes` / `memberships`），让前端弹确认。
   - ⚠️ 顺序很关键：**先分配节点，再置 `is_active=True`**？还是反过来？
     - 先分配后置活：分配失败 → 用户仍是待审核态，状态干净（**采纳**）。
     - 先置活后分配：分配失败 → 用户已能登录但没节点，是不一致的中间态。
   - → **采纳：先分配节点，成功后再置 `is_active=True`。** 但审核单状态要在**分配成功后**才改，否则分配失败会留下「已通过但没活」的审核单。
   - 最终顺序：**校验 → 分配节点（可能 409）→ 改审核单为 approved → 置 `is_active=True` → 刷新会话 → 审计**。
6. `rbac_service.refresh_sessions(store, [username])` —— 让该用户的后台连接重新拉权限。
7. 审计 `user_review_approve`（`category="auth"`）。

**返回**：

```json
{ "ok": true, "user_id": 12, "is_active": true, "assigned_node_ids": ["7-904101-01"] }
```

### Step 3: 驳回接口

```
POST /api/user-reviews/{review_id}/reject
body: { "reason": "姓名信息填写不完整，请补充后重新提交" }
```

流程：

1. 审核单不存在 → `404`；`status != 'pending'` → `409`。
2. `reason` 去空白后为空 → `400`「请填写驳回原因」。
3. `user_review_service.reject_review(review_id, auditor=p.username, reason=reason)`。
4. `users.is_active` **保持 False**（不动 —— 驳回不改登录闸门，因为待审核态本来就不能登录）。
5. 审计 `user_review_reject`，`after` 含 `{review_id, status, reason}`。

**返回**：`{ "ok": true, "user_id": 12, "is_active": false }`

### Step 4: 批量通过接口

```
POST /api/user-reviews/bulk-approve
body: { "review_ids": [1,2,3] }    # 空数组 = 全部 pending
```

流程：

- 遍历目标审核单，逐个走「通过」流程（**不带节点分配** —— 批量场景无法逐单选节点）。
- 单个失败**不中断**其余，返回 `{ok, approved: 2, failed: [{review_id, reason}]}`。
- 汇总写一条审计 `user_review_bulk_approve`，`after` 含 `{count, usernames[]}`。

> ⚠️ 路由顺序：`/bulk-approve` 必须定义在 `/{review_id}/approve` **之前**，否则 FastAPI 会把 `bulk-approve` 当成 `review_id` 解析。

### Step 5: 验证

- `python -c "import app.user_review_api"` 通过
- 后端既有测试通过（`assign_nodes` 抽函数后行为不变）

---

## Task 6: 菜单码 + 路由注册

**Files**: `backend/app/permissions.py`、`backend/app/main.py`

### Step 1: 菜单码

`permissions.py`：

```python
MENU_USER_AUDIT = "user_audit"
```

`MENU_REGISTRY` 追加（放在 `MENU_PERMISSIONS` 之后）：

```python
MenuDef(MENU_USER_AUDIT, "用户注册审核", "/user-audit", False),   # 仅管理员
```

> `assignable=False` → 角色编辑页不出现该勾选项；管理员角色固定拥有全部菜单，不受影响。

### Step 2: 注册路由

`main.py` 的 `for r in (...)` 元组里加 `user_review_api.router`（放在 `users_api.router` 后面）。

### Step 3: 验证

- `GET /api/roles/menus` 里能看到 `user_audit` 且 `assignable=false`
- `python -m pytest backend/tests/ -q` 通过

---

## Task 7: 注册侧 —— 实名材料自填

**Files**: `backend/app/phone_auth.py`、`backend/app/models.py`

### Step 1: 请求模型

`models.py` 的 `PhoneRegisterRequest` 加 4 个可选字段：

```python
    # —— 实名材料（可选；姓名与邮箱各自独立）——
    real_name: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=128)
```

> **2026-11-05 简化**：`cert_type` / `cert_no` 两个字段已删除。

`PhoneRegisterResult` 加：

```python
    review_id: Optional[int] = None     # 新建的审核单 id（require_approval 时有值）
```

### Step 2: 注册流程改造

`phone_auth.register_by_phone` 里，把现在的「手机号已占用 → 409」改成三态：

```
1. 验票据（不变）
2. 查 user by phone
   ├─ 不存在 → 走「新建用户」路径
   ├─ 存在 且 最新审核单 status == 'rejected' → 走「重新提交」路径
   └─ 存在 且 其他情况 → 409「该手机号已注册，请直接登录」（不变）
```

**新建用户路径**：

1. `validate_kyc(real_name, email)` → 不合法 → `400`。
2. `user_service.create_user(...)`（不变）。
3. 绑默认角色（不变）。
4. **新建审核单**：
   - `require_approval=true` → `create_review(user_id, ..., phone=mask_phone_snapshot(phone), kyc_submitted=True, status='pending')`，返回 `review_id`。
   - `require_approval=false` → **不建审核单**（Global Constraint 9），`review_id=None`。
5. 邮箱：`body.email` 有值 → 同时写 `users.email`（`create_user` 不支持 email → 注册完调 `user_service.update_profile(user_id, email=...)`）。

**重新提交路径**（被驳回后重提）：

1. `validate_kyc(...)` 同上。
2. 更新 profile：`display_name` / `email`（**只更新用户自己可维护的字段**，不动用户名、不动密码、不动角色）。
3. **新建一张审核单**（`status='pending'`，`kyc_submitted=True`）—— 旧单保留，形成历史。
4. `users.is_active` 保持 False（重提不等于通过）。
5. 审计 `phone_review_resubmit`。
6. 返回 `{ok: true, pending_approval: true, user_id, username, review_id}`。

> ⚠️ **免审核模式下的重提**：`require_approval=false` 时用户注册即 `is_active=True`，根本不会被驳回 → 重提路径不存在。

> **2026-11-05 简化**：原第 2 步的证件号去重（`409`「该证件已被注册」）在
> 两条路径上都已删除 —— 证件号不再收集，也就没有去重。

### Step 3: 驳回原因展示

`phone_auth` 的 `/sms/verify` 里，`next_action` 现在只区分 `login` / `register` / `pending_approval`。

**新增 `next_action = 'rejected'`**：当用户存在、`is_active=False`、且最新审核单 `status='rejected'` 时返回：

```json
{ "ticket": "...", "next_action": "rejected", "username": "...",
  "reject_reason": "姓名信息填写不完整，请补充后重新提交" }
```

> 注意：审核单状态变更**不影响** `/sms/verify` 的安全性 —— 该接口只在验证码校验通过后才透露这些信息，调用方已证明持有该号码。

### Step 4: 审计

| action | target | 说明 |
|---|---|---|
| `phone_review_resubmit` | username | `{review_id, kyc_submitted}` |

### Step 5: 验证

- `python -c "import app.phone_auth"` 通过
- 既有 `test_phone_auth.py` 全通过（**重点**：重复注册 409 的既有用例必须仍然通过 —— 只有「最新审核单是 rejected」才走重提）

---

## Task 8: 前端类型 + store

**Files**: `frontend/src/api/types.ts`、`frontend/src/stores/userReview.ts`

### Step 1: 类型

`types.ts` 追加：

```ts
export interface ReviewNodeOut {
  node_id: string
  name: string
  status: 'online' | 'offline'
  balance: number
}

export interface UserReviewStats {
  total: number
  pending: number
  approved: number
  rejected: number
  node_total: number
  node_online: number
}

export interface UserReviewOut {
  id: number
  user_id: number
  username: string
  display_name: string | null
  account: string
  phone: string | null
  email: string | null
  real_name: string | null
  kyc_submitted: boolean
  status: 'pending' | 'approved' | 'rejected'
  register_time: number | null
  submit_time: number | null
  audit_time: number | null
  audited_by: string | null
  reject_reason: string | null
  nodes: ReviewNodeOut[]
}

export interface UserReviewList {
  stats: UserReviewStats
  items: UserReviewOut[]
}
```

同时给 `PhoneRegisterPayload` 加 `real_name` / `email`，给 `PhoneRegisterResult` 加 `review_id`。

### Step 2: store

**新** `frontend/src/stores/userReview.ts`：

```ts
export const useUserReviewStore = defineStore('userReview', {
  state: (): UserReviewState => ({
    list: { stats: {...}, items: [] },
    loading: false,
    keyword: '',
    status: 'pending' as ReviewStatus,
  }),
  actions: {
    async fetchReviews(): Promise<void> { ... }
    async approve(id: number, nodeIds?: string[]): Promise<...> { ... }
    async reject(id: number, reason: string): Promise<void> { ... }
    async bulkApprove(ids: number[]): Promise<...> { ... }
  },
})
```

**approve 需要处理 409 confirm_required**：后端分配节点时可能返回 `409 {reason:'confirm_required', message, memberships, followings}` → store 把 detail 原样抛给调用方，页面弹确认框后带 `confirm=true` 重试。

> 后端 `approve` 接口的 body 需要支持 `confirm` 字段（透传给 `assign_nodes_core`）。

### Step 3: 验证

`npm run typecheck` 通过。

---

## Task 9: 前端 —— 审核页面

**Files**: **新** `frontend/src/views/UserAuditView.vue`、`frontend/src/components/UserAuditCard.vue`、`frontend/src/components/UserAuditApproveModal.vue`、`frontend/src/components/UserAuditRejectModal.vue`

### Step 1: 色板映射

原型是 Tailwind 任意值，本项目是自有 CSS。**在 `UserAuditView.vue` 的 scoped style 里定义局部 CSS 变量**，逐条对应原型色值：

```css
.audit-page {
  --ua-bg: #0a0e16;
  --ua-card: #101828;
  --ua-card-2: #0f1726;
  --ua-soft: #0c1220;
  --ua-deep: #0a101e;
  --ua-border: #1c2740;
  --ua-border-2: #22304a;
  --ua-text: #e6ebf4;
  --ua-muted: #8b96a8;
  --ua-dim: #5b6b85;
  --ua-faint: #4a5568;
  --ua-blue: #2563eb;
  --ua-cyan: #38bdf8;
  --ua-green: #22c55e;
  --ua-green-l: #4ade80;
  --ua-amber: #f59e0b;
  --ua-amber-l: #fbbf24;
  --ua-red: #f6465d;
  --ua-red-l: #f87171;
}
```

### Step 2: 页面骨架

```
UserAuditView.vue
├─ 页头卡（渐变 linear-gradient(135deg,#101828,#0e1a2e)）
│   ├─ 图标块：36×36 圆角 12，渐变 #38BDF8→#2563EB，verified_user 图标 19px 白色
│   ├─ 标题「用户注册审核」14px 700 + how_to_reg 小图标（#38BDF8）
│   ├─ 副标题「注册用户 KYC 实名审核 · 节点分布一目了然」10px #8B96A8
│   ├─ 频道介绍条：#0A101E 底、边框 #22304A；「频道介绍」四字加粗 #38BDF8
│   └─ 统计块 grid-cols-3 gap-8（6 项）
│         每项：#0C1220 底 + #22304A 边框 + 圆角 8
│         标签行：12px 图标 + 9px 文字 #5B6B85
│         数值行：14px 700 + 等宽字体
├─ Tab 栏（待审核 N | 已审核 N）+ [全部通过]
├─ 搜索框（search 图标 + 输入框 + 清空按钮）
└─ 卡片列表 / 空态
```

**统计块 6 项与配色**（与原型一致）：

| 标签 | 图标 | 值色 |
|---|---|---|
| 注册用户 | group | `#38BDF8` |
| 待审核 | hourglass_top | `#F59E0B` |
| 已通过 | verified_user | `#22C55E` |
| 已驳回 | cancel | `#F6465D` |
| 节点总数 | account_tree | `#06B6D4` |
| 在线节点 | dns | `#4ADE80` |

**Tab 配色**：

- 待审核选中：`bg-[#F59E0B]/15 text-[#FBBF24]`，计数徽章 `bg-[#F59E0B] text-[#0A101E]`；未选中 `text-[#8B96A8]`。
- 已审核选中：`bg-[#22C55E]/15 text-[#4ADE80]`，计数徽章 `bg-[#22C55E] text-[#0A101E]`。

**「全部通过」按钮**：`bg-[#22C55E]` 绿底白字 10px 700，`done_all` 图标。**仅在「待审核」tab 且有 pending 时显示**。

### Step 3: 卡片组件 `UserAuditCard.vue`

Props：`review: UserReviewOut`、`expanded: boolean`，Emits：`toggle`、`approve`，`reject`。

**头部**：

- 头像：36×36 圆角 8，首字 14px 700。
  - pending：渐变 `from-[#F59E0B]/30 to-[#F59E0B]/5`，字色 `#FBBF24`
  - approved：`bg-[#22C55E]/15`，字色 `#4ADE80`
  - rejected：`bg-[#F6465D]/15`，字色 `#F6465D`
- 用户名 12px 700 `#E6EBF4`（`display_name || username`）+ 账号徽章（`bg-[#22304A]` 圆角 999，9px `#8B96A8`，显示 `account`）
- 副行 9px `#5B6B85`：
  - pending/未审：`注册 {registerTime} · {phone}`
  - 已审：`注册 {registerTime} · 审核 {auditTime}`
- 状态徽章（右侧）：
  - pending：`border-[#F59E0B]/30 bg-[#F59E0B]/10 text-[#FBBF24]` + 脉冲圆点
  - approved：`border-[#22C55E]/30 bg-[#22C55E]/10 text-[#4ADE80]` + verified_user 图标
  - rejected：`border-[#F6465D]/40 bg-[#F6465D]/10 text-[#F6465D]` + cancel 图标

**KYC 实名材料块**（`#0C1220` 底、`#22304A` 边框、圆角 8、内距 10）：

- 标题行 9px 700 `#38BDF8` + badge 图标 12px + `KYC 实名材料 · 提交于 {submitTime}`
- 字段行（10px，label `#5B6B85` / 值 `#E6EBF4` 700）：实名姓名 / 邮箱 / 手机号
  - **只有这 3 行**，与原型完全一致（2026-11-05 去掉证件类型 / 证件号两行）。
- `kyc_submitted=false`（用户未填、由管理员补录）→ 额外显示一条提示：`用户未提交实名材料（由管理员补录）`

**驳回原因条**（仅 rejected）：`border-[#F6465D]/25 bg-[#F6465D]/5` 圆角 6，9px `#F87171`，文案 `驳回原因：{reason}`。

**名下节点折叠块**：

- 折叠头：`#0C1220` 底、`#22304A` 边框、圆角 8、account_tree 图标 14px `#06B6D4` + 「名下节点」10px 700 `#8B96A8` + 计数徽章（`border-[#06B6D4]/30 bg-[#06B6D4]/10`，10px 700 `#22D3EE`，`{n} 个`）+ 右侧 `keyboard_arrow_down` 图标（展开时 `rotate(180deg)`）
- 展开内容：
  - 空态：虚线边框 `border-dashed border-[#22304A]`，文案「该用户暂无节点，审核通过后可创建」
  - 节点行：`#0A101E` 底、`#1C2740` 边框、圆角 8：
    - 在线态图标（20×20，`bg-[#22C55E]/15` / `bg-[#3D4C66]/20` + dns 图标）
    - 名称 10px 700 `#E6EBF4` + ID 9px 等宽 `#5B6B85`
    - 状态徽章（在线 `bg-[#22C55E]/10 text-[#4ADE80]` / 离线 `bg-[#22304A] text-[#5B6B85]`）
    - 余额 10px 700 `#E6EBF4` 等宽（`{balance} U`，`toLocaleString()`）

**底部操作条**（仅 pending）：`border-t border-[#1C2740] bg-[#131C30]`

- 驳回按钮：`border-[#F6465D]/40 bg-[#F6465D]/10 text-[#F6465D]` 10px 700 + close 图标
- 通过按钮：`bg-[#22C55E] text-white` 10px 700 + check 图标 + 阴影

### Step 4: 驳回弹窗 `UserAuditRejectModal.vue`

结构（对齐原型）：

```
fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-[2px]
  └─ max-w-[340px] rounded-2xl border-[#2A3B5C] bg-[#0F172A] p-4
       ├─ 图标块 36×36 bg-[#F6465D]/15 + gpp_bad 图标 18px #F6465D
       ├─ 标题「驳回 KYC 实名审核」14px 700
       ├─ 副标题「{display_name} · {real_name}（{account}）」10px #8B96A8
       ├─ 「选择驳回原因」11px 500
       ├─ 5 个预设 chip（可点选，选中态 border-[#F6465D] bg-[#F6465D]/15 text-[#F87171]）
       ├─ textarea（2 行，可补充说明）
       └─ [取消] [确认驳回]
```

**5 个预设文案**（常量，逐字与原型一致）：

```
姓名信息填写不完整，请补充后重新提交
姓名与手机号实名认证信息不一致
邮箱无效或无法接收通知，请更换
注册信息存在明显异常，请补充说明
```

**交互**：点 chip → 把文案填进 textarea；「确认驳回」按钮在 textarea 为空时 **disabled**（`opacity-50 cursor-not-allowed`）。

### Step 5: 通过弹窗 `UserAuditApproveModal.vue`

结构（对齐原型 + 扩充节点勾选）：

```
fixed inset-0 z-50 ...
  └─ max-w-[380px] rounded-2xl border-[#2A3B5C] bg-[#0F172A] p-4
       ├─ 图标块 bg-[#22C55E]/15 + verified_user 18px #22C55E
       ├─ 标题「通过 KYC 实名审核」
       ├─ 副标题「{display_name} · {real_name}（{account}）」
       ├─ 两栏信息格：邮箱 / 名下节点数
       ├─ 说明条「通过后该用户将完成实名认证，可正常登录使用交易、带单与节点管理等完整功能。」
       ├─ 【新增】节点勾选区（可折叠）
       │    - 列出该用户**当前名下**的节点 + **管理员名下可分配**的节点
       │    - 勾选后提交 node_ids
       │    - 不勾选 = 不改节点归属（沿用现状）
       └─ [取消] [确认通过]
```

**节点勾选区的数据来源**：`GET /api/nodes`（管理员可见全部节点）。列表项：`{node_id, name, owner_username, enabled}`，用复选框多选。

**分配失败的处理**：后端返回 `409 {reason:'confirm_required', message, memberships}` → 弹一个二次确认（用 `utils/confirm.ts` 的 `confirmAction`，展示 `message`），用户确认后带 `confirm=true` 重试。

### Step 6: 页面逻辑

- `onMounted` → `store.fetchReviews()`。
- Tab 切换 → 改 `store.status` → 重新拉。
- 搜索输入 → **防抖 300ms** → 重新拉（后端筛选，不是前端过滤）。
- 折叠状态：`Map<review_id, boolean>` 存在组件内 `ref`。
- 审核动作成功后 → `ElMessage.success` + 重新拉列表。
- 轮询：每 **30s** 重新拉一次列表（替代 WS 推送，见设计文档 §5.6）。`onUnmounted` 清除定时器。

### Step 7: 验证

- `npm run typecheck` 通过
- 375px 窄屏下卡片不溢出（`flex-wrap` + `truncate`）

---

## Task 10: 前端 —— 路由 / 菜单 / 「我的」入口

**Files**: `frontend/src/router/index.ts`、`frontend/src/constants/menus.ts`、`frontend/src/views/MineView.vue`

### Step 1: 路由

```ts
{
  path: '/user-audit',
  name: 'user-audit',
  component: () => import('@/views/UserAuditView.vue'),
  meta: { menu: 'user_audit' },
},
```

### Step 2: 菜单

`menus.ts` 的 `MENU_PATHS` 追加 `{ code: 'user_audit', path: '/user-audit' }`。

### Step 3: 「我的」入口

`MineView.vue` 的 `ALL_MODULES` 追加：

```ts
{
  code: 'user_audit',
  label: '用户注册审核',
  description: '注册用户 · KYC 实名审核 · 节点明细',
  path: '/user-audit',
  icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
  accent: '#38BDF8',
},
```

`visibleModules` 已按 `hasMenu` 过滤 + 管理员全放行 → **普通用户看不到该入口，管理员可见**，无需额外判断。

### Step 4: 验证

- 管理员登录 → 我的 → 看到「用户注册审核」→ 点击进页面。
- 普通用户登录 → 我的 → 看不到该入口；直接访问 `/user-audit` → 路由守卫弹回第一个有权限的菜单。

---

## Task 11: 前端 —— 注册页实名 2 字段

**Files**: `frontend/src/views/RegisterView.vue`

> **2026-11-05 简化**：原 4 字段 → 2 字段（姓名 / 邮箱）。
> 证件类型下拉与证件号输入框整体删除，`CERT_TYPES` 常量也一并删除。

### Step 1: 表单状态

```ts
const realName = ref('')
const email = ref('')
```

### Step 2: 第二步 UI

在现有「昵称 / 用户名 / 密码」之后加一组：

```
<div class="input-group">
  <label>姓名（选填，用于实名审核）</label>
  <input v-model="realName" maxlength="64" placeholder="真实姓名" />
</div>
<div class="input-group">
  <label>邮箱（选填）</label>
  <input v-model="email" type="email" placeholder="用于接收通知" />
</div>
<p class="muted register-hint">
  填写姓名后需经管理员实名审核通过才能登录。
</p>
```

**校验**（提交时，前端只做粗校验，后端为准）：

- 姓名与邮箱**各自独立可选** —— 没有「要么全填要么全空」的规则。
- 邮箱格式：`/^[^@\s]+@[^@\s]+\.[^@\s]+$/`。

### Step 3: 提交

`submitRegister` 的 payload 加 2 个字段（`real_name` / `email`）。

### Step 4: 登录页的 rejected 分支

`LoginView.vue` 的 `next_action` 分流加一条：

```ts
if (verify.next_action === 'rejected') {
  error.value = `实名审核未通过：${verify.rejectReason || '请重新提交实名材料'}。可在注册页重新提交。`
  return
}
```

**「在注册页重新提交」的路径**：`RegisterView.vue` 挂载时先调 `auth.fetchSmsStatus()`，再调一个**只读的**「查我的审核状态」接口？—— 不需要额外接口：

- 用户在登录页收到 rejected 提示后，**手动点「重新提交」链接** → 跳 `/register`。
- `/register` 第一步发验证码 → `/sms/verify` 返回 `next_action='rejected'` → 前端**不拦截**，而是允许继续到第二步（带票据）。
- 第二步提交 → 后端走「重新提交路径」（Task 7）。

**因此 `RegisterView` 的 `verifyCode` 需要处理 `next_action='rejected'`**：不报错，允许进入第二步（但把 `rejectReason` 显示为提示）。

> ⚠️ 这与 `next_action='login'` / `'pending_approval'` 的处理不同：那两个要拦住（已注册，别重复注册），`'rejected'` 要放行（就是来重提的）。

### Step 5: 验证

- `npm run typecheck` 通过
- 手工：注册时填实名 → 提交 → 管理员审核页能看到 KYC 材料。

---

## Task 12: 后端测试

**Files**: **新** `backend/tests/test_user_review.py`

### Step 1: 测试脚手架

参照 `test_phone_auth.py`：

```python
_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_review.db"
_FAKE_SERVER = fakeredis.FakeServer()

@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(server=_FAKE_SERVER, decode_responses=True))
    reset_test_db(_TEST_DB)
    fakeredis.FakeStrictRedis(server=_FAKE_SERVER).flushall()
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app
    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)
```

### Step 2: 用例清单

**列表与聚合**：

| # | 用例 | 断言 |
|---|---|---|
| 1 | 空库时列表 | `items == []`，`stats.total == 0` |
| 2 | 注册 3 个用户（免审核 / 待审核各建单） | `stats` 计数正确 |
| 3 | 名下节点聚合 | 节点名称/在线态/余额正确；离线节点余额 0 |
| 4 | 搜索命中 4 个维度 | 用户名 / 账号 / 手机号 / 节点 ID 各一例 |
| 5 | 搜索大小写不敏感 | 大写关键词能命中 |
| 6 | `status` 非法值 | `400` |
| 7 | `stats` 不受 `status` 筛选影响 | 筛选后 stats 仍为全局数 |

**审核动作**：

| # | 用例 | 断言 |
|---|---|---|
| 8 | 通过审核 | `status='approved'`、`is_active=True` |
| 9 | 通过后 `/sms/verify` | `next_action='login'`（**端到端闭环**） |
| 10 | 驳回审核 | `status='rejected'`、`reject_reason` 落库、`is_active` 仍 False |
| 11 | 驳回后 `/sms/verify` | `next_action='rejected'` + 返回原因 |
| 12 | 重复审核同一单 | `409` |
| 13 | 驳回原因为空 | `400` |
| 14 | 审核不存在的单 | `404` |
| 15 | 批量通过 | `approved == N`，`failed == []` |
| 16 | 批量通过含已处理单 | 该单进 `failed`，其余成功 |
| 17 | 通过时分配节点 | 节点 `owner_user_id` 变成该用户 |
| 18 | 分配节点遇活动子任务 | `409 reason='active_tasks'`，且**审核单仍是 pending、`is_active` 仍 False**（回滚正确） |
| 19 | 分配节点遇分组成员 | `409 reason='confirm_required'`；带 `confirm=true` 重试成功 |

**权限**：

| # | 用例 | 断言 |
|---|---|---|
| 20 | 普通用户访问列表 | `403` |
| 21 | 未登录访问 | `401` |

**实名材料**：

| # | 用例 | 断言 |
|---|---|---|
| 22 | 手机号脱敏快照 | `mask_phone_snapshot("13800138000") == "138****8000"` |
| 23 | 只填邮箱不填姓名 | 允许（`kyc_submitted=false`） |
| 24 | 邮箱格式非法 | `400` |
| 25 | 姓名超长（>64） | `400` |
| 26 | 表里没有证件列 | 查库确认无 `cert_type` / `cert_no_masked` / `cert_no_hash` |
| 27 | 手机号存的是脱敏快照 | 查库确认 `user_reviews.phone == "138****8000"` |

> **2026-11-05 简化**：原用例 23~28（部分填写 400 / 非法证件类型 400 /
> 证件号格式不符 400 / 同证件号换用户 409 / 同一用户重提同证件号允许 /
> 证件号明文不入库）全部删除或改写。

**重新提交**：

| # | 用例 | 断言 |
|---|---|---|
| 29 | 被驳回后重新注册 | `409` 不再发生；返回 `pending_approval=true` + 新 `review_id` |
| 30 | 重提后审核单数 | 该用户有 2 张单：旧 rejected + 新 pending |
| 31 | 重提不自动通过 | `is_active` 仍 False |
| 32 | 未被驳回的用户重复注册 | `409`（**原行为不变**） |

**审计**：

| # | 用例 | 断言 |
|---|---|---|
| 33 | 通过写审计 | `action='user_review_approve'`、`category='auth'`、`after` 含状态 |
| 34 | 审计不含手机号明文 | `params` 里搜不到 `13800138000`，只有 `138****8000` |

### Step 3: 验证

```
python -m pytest backend/tests/test_user_review.py -q
python -m pytest backend/tests/ -q
```

---

## Task 13: 文档同步

**Files**: `docs/superpowers/specs/2026-11-01-phone-register-aliyun-design.md`

### Step 1: 追加「重复注册」例外

在 §A.3「重复注册直接拒绝」之后追加一段：

> **2026-11-04 修订**：本节所述「直接拒绝」新增一个例外 —— 若该手机号对应的**最新 KYC 审核单状态为 `rejected`**，允许用户**重新提交实名材料**（新建一张审核单，旧的被驳回单保留为历史）。
>
> 理由：驳回必须给出补救路径，否则用户永久卡死无法登录。
>
> 为什么安全：能走到这一步的用户**已通过短信验证码验证**（持有该号码）；重提只能把审核单打回 `pending`，**审核通过仍必须由管理员点**，「未审核」状态无法被反复提交刷成「已审核」。防刷目标不受影响。

### Step 2: §6.5「管理员审核」标注

原文写「复用 `users_api` 能力，**不新增接口**」→ 追加：

> **2026-11-04 修订**：改为**新增独立审核接口**（`/api/user-reviews/*`）。理由：`users.is_active` 只能表达「启用/禁用」，**无法表达「已驳回」**，也不记录审核时间、审核人、驳回原因。详见 `docs/superpowers/specs/2026-11-04-user-registration-review-design.md` §3.1。

### Step 3: §16-2 开放问题给答案

「是否需要一个『管理员可批量审核』的操作入口」→ **需要**，已实现为「全部通过」按钮（`POST /api/user-reviews/bulk-approve`）。

### Step 4: 验证

- 文档无矛盾表述。

---

## Review Focus

评审时重点看这几处：

1. **`approve` 的操作顺序** —— 必须「先分配节点 → 成功后再改审核单 → 再置 `is_active=True`」。顺序反了会留下「审核通过但用户不能登录」或「用户能登录但节点没分配」的中间态（Task 5 Step 2）。
2. **分配失败必须回滚** —— 分配节点返回 409 时，审核单状态**不能**已经变成 approved、`is_active` **不能**已经是 True（Task 12 用例 18 专门盯这个）。
3. **`next_action='rejected'` 不构成号码枚举** —— 该信息只在**验证码校验通过后**才返回，调用方已证明持有该号码（Task 7 Step 3）。
4. **重提路径不能削弱防刷** —— 重提只能打回 `pending`，通过仍需管理员点；且只在「最新审核单是 rejected」时才放行（Task 7 Step 2 + Task 12 用例 32）。
5. **`is_active` 与审核状态是两个正交维度** —— 审核通过后管理员仍可在用户权限页禁用该用户；这在数据上表达为 `status='approved'` + `is_active=False`，列表页要能正确显示（不要用 `is_active` 反推审核状态）。
6. **审核单里不存敏感明文** —— 姓名与邮箱明文存（业务需要），手机号只存脱敏快照；审计的 `params` 里也不能出现手机号明文（Task 12 用例 34 盯这个）。
7. **`stats` 是全局的、`items` 是筛选后的** —— Tab 徽章数与统计块数不受搜索/筛选影响，否则用户会以为「还有 N 个待审但列表里没有」。
8. **MySQL 方言分支** —— `_migrate_user_reviews` 建表用 SQLAlchemy 按方言生成（`AUTOINCREMENT` 与 `CREATE INDEX IF NOT EXISTS` 在 MySQL 上不合法）；「删旧列 + 补新列」的两条 DDL 两方言一致，不需要分支（Task 1 Step 2）。
9. **路由顺序** —— `/bulk-approve` 必须定义在 `/{review_id}/approve` 之前（Task 5 Step 4）。
10. **审核列表的节点在线态来自内存连接表** —— 进程重启后所有节点瞬时判离线，这是既有 `manager.is_node_online` 的语义，页面不额外处理。
