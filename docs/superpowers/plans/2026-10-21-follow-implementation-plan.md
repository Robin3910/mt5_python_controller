# 带单功能实施计划

> 本计划按可独立验证的步骤排列，每步末尾标注「验证方法」和「回滚方式」。
> 前端、后端并行度在步骤内注明。

---

## 目标

- **带单 Tab** 对所有登录用户可见（默认菜单，`MENU_FOLLOW` assignable=True）
- **普通用户默认可以跟单**（加入别人的分组）
- **普通用户默认不能带单**（除非管理员授予 `follow_leader` 能力）
- 带单员拥有 `follow_leader` 能力时，其主节点所在分组出现在所有客户的【带单】Tab

---

## 步骤 0：数据库准备（无破坏性，上线即开）

### 0.1 新表 `group_follower_node`

**文件**：`backend/app/orm.py`

**内容**：在 `Role` 类之前添加新模型

```python
class GroupFollowerNode(Base):
    """跟单成员：非主节点加入他人的带单源分组。"""
    __tablename__ = "group_follower_node"
    __table_args__ = (
        UniqueConstraint("group_id", "node_id", name="uq_follow_group_node"),
    )

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

**验证**：`SELECT name FROM sqlite_master WHERE type='table' AND name='group_follower_node'` → 应返回 1 行（MySQL 用 `SHOW TABLES LIKE 'group_follower_node'`）

**回滚**：删除该表即可

---

## 步骤 1：后端能力注册（纯注册表，无副作用）

### 1.1 `permissions.py` —— 加能力码

**文件**：`backend/app/permissions.py`

**改动 1**：第 72 行附近，在 `CAP_AI_STRATEGY` 后加一行：

```python
CAP_AI_STRATEGY = "ai_strategy"
CAP_FOLLOW_LEADER = "follow_leader"
```

**改动 2**：第 78-79 行，`CAPABILITY_REGISTRY` 末尾加一项：

```python
CAPABILITY_REGISTRY: tuple[tuple[str, str], ...] = (
    (CAP_AI_STRATEGY, "AI 智能加仓策略"),
    (CAP_FOLLOW_LEADER, "带单"),
)
```

**改动 3**：第 82 行附近，`ALL_CAPABILITY_CODES` / `CAPABILITY_NAMES` 自动从注册表推导，无需改

**改动 4**：第 87 行附近，新增判定函数：

```python
def can_be_leader(p: Principal) -> bool:
    """能否作为带单员对外接单（仅能力，不看数据归属）。"""
    return can_capability(p, CAP_FOLLOW_LEADER)
```

**验证**：启动后端，`python -c "from backend.app import permissions; print(permissions.CAP_FOLLOW_LEADER, permissions.can_be_leader)"` 应无报错

**回滚**：删除这四处改动即可

---

### 1.2 `stores/rbac.ts` —— 前端能力常量

**文件**：`frontend/src/stores/rbac.ts`

**改动**：第 21-23 行，`CAPABILITIES` 数组追加一项：

```typescript
export const CAPABILITIES = [
  { code: 'ai_strategy', label: 'AI 智能加仓策略', description: '允许新建、编辑、删除 AI 智能加仓策略（模版1）实例' },
  { code: 'follow_leader', label: '带单', description: '授予该能力的用户，其主节点与主节点所在分组将出现在所有客户的带单界面中' },
] as const
```

**验证**：前端 `npm run build` 无类型错误

**回滚**：删除该行即可

---

### 1.3 `stores/auth.ts` —— 前端能力 getter

**文件**：`frontend/src/stores/auth.ts`

**改动**：在 `canUseAiStrategy` getter 后追加：

```typescript
canBeFollowLeader: (s): boolean =>
  !!s.me?.is_admin || (!!s.me && s.me.capabilities?.includes('follow_leader')) || false,
```

**验证**：同 1.2

**回滚**：删除该行即可

---

## 步骤 2：后端菜单注册（纯注册表，无副作用）

### 2.1 `permissions.py` —— 加菜单码

**文件**：`backend/app/permissions.py`

**改动 1**：第 19 行，在 `MENU_TRADING` 后加一行：

```python
MENU_FOLLOW = "follow"
```

**改动 2**：第 48-53 行，`MENU_REGISTRY` 末尾追加：

```python
    MenuDef(MENU_TRADING, "交易", "/trading", True),
    MenuDef(MENU_FOLLOW, "带单", "/follow", True),
    MenuDef(MENU_DASHBOARD, "总览", "/", True),
```

> 插入位置：紧跟 `MENU_TRADING`，在 `MENU_DASHBOARD` 之前

**验证**：`python -c "from backend.app import permissions; print(permissions.MENU_FOLLOW)"` → 应输出 `follow`

**回滚**：删除 `MENU_FOLLOW` 常量和 MenuDef 条目即可

---

### 2.2 `models.py` —— `MenuOut` 更新（自动推导，无需改）

`MenuOut` 用 `MenuDef` 的 `code/name/path/assignable` 字段，已是动态的，无需改。

---

## 步骤 3：前端路由与导航（无后端依赖，可提前做）

### 3.1 `src/constants/menus.ts` —— 菜单路径常量

**文件**：`frontend/src/constants/menus.ts`

**改动**：追加

```typescript
MENU_PATHS[MENU_PATHS.length - 1] = '/follow'  // 在 dashboard 之前插入
// 或直接: export const MENU_PATHS: Record<string, string> = { ..., follow: '/follow' }
```

**验证**：`grep -n "follow" frontend/src/constants/menus.ts` 能找到

**回滚**：删除该条目

### 3.2 `src/router/index.ts` —— 路由注册

**文件**：`frontend/src/router/index.ts`

**改动**：在路由数组中追加（放在 `trading` 路由后）

```typescript
{
  path: '/follow',
  name: 'Follow',
  component: () => import('@/views/FollowView.vue'),
  meta: { menu: 'follow', title: '带单', requiresAuth: true },
},
```

**验证**：访问 `/follow` 不 404

**回滚**：删除该路由条目

---

### 3.3 `src/App.vue` —— 顶部导航

**文件**：`frontend/src/App.vue`

**改动**：在导航数组中，找到 `path: '/trading'` 的项，在其后追加：

```vue
{ path: '/follow', label: '带单', icon: '📡' },
```

> 图标可先用 emoji 占位，后续替换为 SVG

**验证**：登录后顶部导航出现「带单」链接

**回滚**：删除该导航项

---

## 步骤 4：前端空页面（验证导航链路）

### 4.1 `src/views/FollowView.vue`

**文件**：新建 `frontend/src/views/FollowView.vue`

**最小内容**（先验证链路，后续替换为完整 UI）：

```vue
<template>
  <div class="page">
    <h2>带单</h2>
    <p>功能开发中...</p>
  </div>
</template>

<script setup lang="ts">
// 暂时为空
</script>
```

**验证**：点击顶部「带单」导航 → 显示「功能开发中...」

**回滚**：删除该文件 + 路由条目

---

## 步骤 5：种子逻辑补齐（存量 user 角色幂等补菜单）

### 5.1 `rbac_service.py` —— 补齐内置 user 角色的菜单

**文件**：`backend/app/rbac_service.py`

**改动**：在 `seed_rbac` 函数中，找到 `if code == DEFAULT_ROLE_CODE:` 块（第 52 行附近），在此块**之后**追加一个新的 if 块：

```python
        # 首次升级：为存量内置 user 角色补齐 DEFAULT_USER_MENUS（幂等：已存在则跳过）
        if DEFAULT_ROLE_CODE in existing:
            user_role = existing[DEFAULT_ROLE_CODE]
            existing_menus = {
                row[0] for row in (
                    await s.execute(
                        select(RoleMenu.menu_code).where(RoleMenu.role_id == user_role.id)
                    )
                ).all()
            }
            for menu in permissions.DEFAULT_USER_MENUS:
                if menu not in existing_menus:
                    s.add(RoleMenu(role_id=user_role.id, menu_code=menu))
            await s.flush()
```

**完整 `seed_rbac` 改造后应如下**（灰色部分为原有代码，仅在灰色后追加灰色部分）：

```python
async def seed_rbac() -> None:
    async with SessionLocal() as s:
        existing = {r.code: r for r in (await s.execute(select(Role))).scalars().all()}
        for code, name, remark in _BUILTIN_ROLES:
            if code in existing:
                continue
            role = Role(code=code, name=name, remark=remark, is_builtin=True, enabled=True)
            s.add(role)
            await s.flush()
            existing[code] = role
            if code == DEFAULT_ROLE_CODE:
                for menu in permissions.DEFAULT_USER_MENUS:
                    s.add(RoleMenu(role_id=role.id, menu_code=menu))
        # --- 以下为新增：给存量 user 角色补齐菜单（幂等）---
        if DEFAULT_ROLE_CODE in existing:
            user_role = existing[DEFAULT_ROLE_CODE]
            existing_menus = {
                row[0] for row in (
                    await s.execute(
                        select(RoleMenu.menu_code).where(RoleMenu.role_id == user_role.id)
                    )
                ).all()
            }
            for menu in permissions.DEFAULT_USER_MENUS:
                if menu not in existing_menus:
                    s.add(RoleMenu(role_id=user_role.id, menu_code=menu))
            await s.flush()
        # -------------------------------------------------------
        bound = await s.scalar(select(func.count()).select_from(UserRole))
        if not bound:
            admin_role_id = existing[ADMIN_ROLE_CODE].id
            legacy = (
                await s.execute(select(User.id).where(User.role == "admin"))
            ).scalars().all()
            for uid in legacy:
                s.add(UserRole(user_id=uid, role_id=admin_role_id))
            if legacy:
                logger.info("rbac: bound %d legacy admin account(s) to role admin", len(legacy))
        await s.commit()
```

**验证**：
1. 查数据库：`SELECT COUNT(*) FROM sys_role_menu WHERE role_id = (SELECT id FROM sys_role WHERE code = 'user')`
2. 重启后端 → 值应等于 `len(ASSIGNABLE_MENU_CODES)`（当前约 7 个，加上 `MENU_FOLLOW` 后应为 8）
3. 新用户登录 → 能看到「带单」菜单

**回滚**：删除新增的 if 块（保留原有 `if code == DEFAULT_ROLE_CODE:` 块）即可，存量菜单数据不受影响

---

## 步骤 6：核心后端实现

### 6.1 `backend/app/follow_rules.py`（新建）

**文件**：新建 `backend/app/follow_rules.py`

```python
"""带单纯规则：可跟单分组判定、可展示带单员判定（无 I/O，便于单测）。"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .permissions import Principal

CAP_FOLLOW_LEADER = "follow_leader"


def can_be_leader(p: "Principal") -> bool:
    """能否作为带单员对外接单（仅能力，不看数据归属）。"""
    return p.is_admin or CAP_FOLLOW_LEADER in p.capabilities


def group_has_leader_primary(group: dict, leader_primary_node_id: str | None) -> bool:
    """该分组是否包含带单员的主节点（即是否可跟单）。"""
    if not leader_primary_node_id:
        return False
    member_ids = group.get("members", [])
    return any(m.get("node_id") == leader_primary_node_id for m in member_ids)


def is_followable_group(
    group: dict,
    leader_user_id: int,
    leader_primary_node_id: str | None,
    require_enabled: bool = True,
) -> bool:
    """一个分组是否可作为带单源分组。"""
    if group.get("owner_user_id") != leader_user_id:
        return False
    if require_enabled and not group.get("enabled", False):
        return False
    if not group_has_leader_primary(group, leader_primary_node_id):
        return False
    if group.get("strategy_id") and not group.get("strategy_enabled", True):
        return False
    return True


def is_displayable_leader(
    leader: dict,
    groups: list[dict],
    current_user_id: int,
) -> bool:
    """一个用户是否应出现在带单员列表中。"""
    if leader["user_id"] == current_user_id:
        return False
    if not leader.get("is_active", True):
        return False
    if not can_be_leader_str(leader.get("capabilities", [])):
        return False
    if not leader.get("primary_node_id"):
        return False
    # 至少有一个可跟单分组
    return any(
        is_followable_group(g, leader["user_id"], leader["primary_node_id"])
        for g in groups
    )


def can_be_leader_str(capabilities: list) -> bool:
    """按 capabilities list 字符串判断（用于不持有 Principal 的场景，如列表投影）。"""
    return CAP_FOLLOW_LEADER in capabilities


def participant_ids(group: dict) -> list[str]:
    """owner 成员 + 跟单成员（去重保序）。"""
    owner_ids = [m["node_id"] for m in group.get("members", []) if m.get("node_id")]
    follower_ids = [f["node_id"] for f in group.get("followers", []) if f.get("node_id")]
    seen = set()
    out = []
    for nid in owner_ids:
        if nid not in seen:
            out.append(nid)
            seen.add(nid)
    for nid in follower_ids:
        if nid not in seen:
            out.append(nid)
            seen.add(nid)
    return out


def effective_node_ids(
    group: dict,
    nodemap: dict,
    online_ids: set[str],
) -> list[str]:
    """有效节点 = participant_ids ∩ (存在 ∧ enabled ∧ 在线 ∧ 非主节点)。"""
    all_ids = participant_ids(group)
    primary_id = group.get("primary_node_id")
    out = []
    for nid in all_ids:
        if nid == primary_id:
            continue  # 主节点不参与分发
        node = nodemap.get(nid)
        if not node:
            continue
        if not node.get("enabled", False):
            continue
        if nid not in online_ids:
            continue
        out.append(nid)
    return out


def follower_member_ids(group: dict) -> list[str]:
    """仅跟单成员（不含 owner 成员）。"""
    return [f["node_id"] for f in group.get("followers", []) if f.get("node_id")]
```

**验证**：`python -c "from backend.app import follow_rules; print('ok')"` → 无报错

**回滚**：删除该文件

---

### 6.2 `backend/app/follow_service.py`（新建）

**文件**：新建 `backend/app/follow_service.py`

```python
"""带单持久化 + 缓存层。"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select

from backend.app.db import SessionLocal
from backend.app.orm import GroupFollowerNode, Node, User
from backend.app.redis_store import RedisStore

logger = logging.getLogger(__name__)


async def get_follower_groups(
    store: RedisStore,
    follower_user_id: int,
    leader_user_id: int,
) -> list[dict]:
    """获取指定用户对指定带单员的跟单关系。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupFollowerNode)
                .where(
                    GroupFollowerNode.leader_user_id == int(leader_user_id),
                    GroupFollowerNode.follower_user_id == int(follower_user_id),
                )
            )
        ).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def add_follower_nodes(
    store: RedisStore,
    group_id: str,
    node_ids: list[str],
    leader_user_id: int,
    leader_primary_node_id: str,
    follower_user_id: int,
) -> tuple[list[str], list[str]]:
    """加入跟单：返回 (joined_node_ids, skipped_node_ids)。"""
    joined, skipped = [], []
    async with SessionLocal() as s:
        for nid in node_ids:
            existing = (
                await s.execute(
                    select(GroupFollowerNode).where(
                        GroupFollowerNode.group_id == group_id,
                        GroupFollowerNode.node_id == nid,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                skipped.append(nid)
                continue
            row = GroupFollowerNode(
                group_id=group_id,
                node_id=nid,
                leader_user_id=int(leader_user_id),
                leader_primary_node_id=leader_primary_node_id,
                follower_user_id=int(follower_user_id),
                sort_order=0,
                joined_at=datetime.utcnow(),
            )
            s.add(row)
            joined.append(nid)
        await s.commit()
    # 刷新分组缓存
    from backend.app import group_service
    try:
        await group_service.refresh_group_cache(store, group_id)
    except Exception:  # noqa: BLE001
        logger.warning("failed to refresh group cache after add_follower: %s", group_id)
    return joined, skipped


async def remove_follower_nodes(
    store: RedisStore,
    group_id: str,
    node_ids: list[str],
    follower_user_id: int,
) -> list[str]:
    """移出跟单：返回已移除的 node_ids。"""
    removed = []
    async with SessionLocal() as s:
        for nid in node_ids:
            result = await s.execute(
                delete(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == group_id,
                    GroupFollowerNode.node_id == nid,
                    GroupFollowerNode.follower_user_id == int(follower_user_id),
                )
            )
            if result.rowcount:
                removed.append(nid)
        await s.commit()
    # 刷新分组缓存
    from backend.app import group_service
    try:
        await group_service.refresh_group_cache(store, group_id)
    except Exception:  # noqa: BLE001
        logger.warning("failed to refresh group cache after remove_follower: %s", group_id)
    return removed


async def remove_node_from_all_followings(store: RedisStore, node_id: str) -> int:
    """节点删除 / 归属转移时清理全部跟单关系，返回清理的行数。"""
    async with SessionLocal() as s:
        result = await s.execute(
            delete(GroupFollowerNode).where(GroupFollowerNode.node_id == node_id)
        )
        await s.commit()
        return result.rowcount


def _row_to_dict(row: GroupFollowerNode) -> dict:
    return {
        "id": row.id,
        "group_id": row.group_id,
        "node_id": row.node_id,
        "follower_user_id": row.follower_user_id,
        "leader_user_id": row.leader_user_id,
        "leader_primary_node_id": row.leader_primary_node_id,
        "sort_order": row.sort_order,
        "joined_at": row.joined_at.timestamp() if row.joined_at else None,
    }
```

**验证**：`python -c "from backend.app import follow_service; print('ok')"` → 无报错

**回滚**：删除该文件

---

### 6.3 `backend/app/follow_api.py`（新建）

**文件**：新建 `backend/app/handlers/follow_api.py`

> 注意：先检查 `backend/app/` 下是 `handlers/` 子目录还是直接放 `app/`。若直接放 `app/`，路径改为 `backend/app/follow_api.py`。

```python
"""带单 API 路由（跟单方向：展示、加入、移出）。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.app import follow_rules, follow_service, permissions, rbac_service
from backend.app.deps import get_store, get_principal, require_menu
from backend.app.models import NodeOut
from backend.app.permissions import Principal, MENU_FOLLOW
from backend.app.redis_store import RedisStore

router = APIRouter(prefix="/api/follow", tags=["follow"])


# ---- 请求/响应模型 ----

class JoinRequest(BaseModel):
    leader_user_id: int
    group_ids: list[str]
    node_ids: list[str]


class LeaveRequest(BaseModel):
    leader_user_id: int
    group_ids: list[str]
    node_ids: list[str]
    close_tasks: bool = False


class JoinResponse(BaseModel):
    joined: list[dict]
    skipped: list[dict]
    leader_primary_node_id: str | None


class LeaveResponse(BaseModel):
    removed: list[dict]
    closing: list[dict]
    forced: list[dict]
    warning: str | None


# ---- 辅助 ----

async def _get_current_user_id(p: Principal) -> int:
    return p.user_id


async def _leader_or_404(user_id: int) -> dict:
    from backend.app.orm import User
    async with rbac_service.SessionLocal() as s:
        from sqlalchemy import select
        row = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="leader not found")
    return {
        "user_id": row.id,
        "username": row.username,
        "is_active": bool(row.is_active),
        "capabilities": [],  # TODO: 从 Principal 投影填充
    }


# ---- 路由 ----

@router.get("/leaders")
async def list_leaders(
    q: str = "",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """带单员列表（展示给跟单者）。"""
    # TODO: 完整实现见设计方案 §5.1
    # 1. 查询全部有 follow_leader 能力且有主节点的用户
    # 2. 对每个用户计算可跟单分组
    # 3. 过滤 q 模糊匹配
    # 4. 补充当前用户的跟单状态
    return {"leaders": []}


@router.get("/leaders/{leader_user_id}")
async def get_leader_detail(
    leader_user_id: int,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """带单员详情（可跟单分组 + 我的可跟单节点）。"""
    # TODO: 见设计方案 §5.2
    return {"leader": {}, "groups": [], "my_nodes": []}


@router.get("/me")
async def get_my_following(
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """我的跟单列表。"""
    # TODO: 见设计方案 §5.3
    return {"following": [], "summary": {"following_count": 0, "follow_node_count": 0}}


@router.post("/join", response_model=JoinResponse)
async def join_follow(
    body: JoinRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """加入跟单。"""
    # TODO: 见设计方案 §5.4
    # 校验 leader 存在 + is_active + 有 follow_leader 能力
    # 校验 group_ids 属于该 leader 且含其主节点
    # 校验 node_ids 归属当前用户 + 非主节点 + enabled
    # 写入 group_follower_node
    # 刷新缓存
    return JoinResponse(joined=[], skipped=[], leader_primary_node_id=None)


@router.post("/leave", response_model=LeaveResponse)
async def leave_follow(
    body: LeaveRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
):
    """移出跟单。"""
    # TODO: 见设计方案 §5.5
    return LeaveResponse(removed=[], closing=[], forced=[], warning=None)
```

> **重要**：步骤 6.3 的文件内容是**骨架**，所有路由返回空数据。目的是让前后端联调链路能跑通，后续步骤逐步填充逻辑。每填充一个路由前，保持该文件可 import 不报错。

**验证**：`python -c "from backend.app.handlers import follow_api; print('ok')"` → 无报错

**回滚**：删除该文件

---

### 6.4 `backend/app/main.py` —— 注册路由

**文件**：`backend/app/main.py`

**查找方式**：`grep -n "router.*import\|include_router" backend/app/main.py | head -20`

**改动**：在现有 `include_router` 附近添加

```python
from .handlers import follow_api
# 或 from . import follow_api（如果 follow_api 直接放在 app/ 下）
app.include_router(follow_api.router)
```

**验证**：启动后端，`curl http://localhost:PORT/api/follow/leaders` 不 404

**回滚**：删除该 include_router 行

---

## 步骤 7：分发链路改造（核心，谨慎）

### 7.1 `backend/app/group_rules.py` —— `participant_ids`

**文件**：`backend/app/group_rules.py`

**查找**：`grep -n "def member_ids\|def effective_node_ids" backend/app/group_rules.py`

**改动**：在 `member_ids` 函数**后**追加

```python
def participant_ids(group: dict) -> list[str]:
    """owner 成员（按 sort_order）+ 跟单成员（按 sort_order），去重保序。"""
    owner_ids = [m["node_id"] for m in group.get("members", []) if m.get("node_id")]
    follower_ids = [f["node_id"] for f in group.get("followers", []) if f.get("node_id")]
    seen = set()
    out = []
    for nid in owner_ids:
        if nid not in seen:
            out.append(nid)
            seen.add(nid)
    for nid in follower_ids:
        if nid not in seen:
            out.append(nid)
            seen.add(nid)
    return out


def effective_node_ids_with_followers(
    group: dict,
    nodemap: dict,
    online_ids: set[str],
) -> list[str]:
    """有效节点（含跟单成员，排除主节点）。"""
    primary_id = group.get("primary_node_id")
    all_ids = participant_ids(group)
    out = []
    for nid in all_ids:
        if nid == primary_id:
            continue  # 主节点不参与分发
        node = nodemap.get(nid)
        if not node:
            continue
        if not node.get("enabled", False):
            continue
        if nid not in online_ids:
            continue
        out.append(nid)
    return out
```

**改动 `effective_node_ids` 函数**：将函数内的 `member_ids(group)` 调用替换为 `participant_ids(group)`，并在前端加 `if nid == group.get("primary_node_id"): continue` 过滤主节点。

**验证**：`python -c "from backend.app import group_rules; print(group_rules.participant_ids({'members':[],'followers':[]}))"` → 应返回 `[]`

**回滚**：删除新增函数，撤销 `effective_node_ids` 的改动

---

### 7.2 `backend/app/group_service.py` —— 缓存加 `followers`

**文件**：`backend/app/group_service.py`

**查找**：`grep -n "group_row_to_dict\|cache_group\|members" backend/app/group_service.py`

**改动 1**：`group_row_to_dict` 函数中，返回 dict 里追加 `"followers": []`

**改动 2**：确保 `warm_cache` 加载了 `followers`（在填充 `members` 之后追加填充 `followers` 的逻辑）

**改动 3**：`delete_group` 函数中，在删除分组成员后追加

```python
await s.execute(
    delete(GroupFollowerNode).where(GroupFollowerNode.group_id == group_id)
)
```

**验证**：查询 Redis 中任意分组的缓存，`JSON.parse` 后应有 `followers` 字段（目前为空数组）

**回滚**：撤销三处改动

---

### 7.3 `backend/app/group_dispatcher.py` —— 替换 `member_ids` → `participant_ids`

**文件**：`backend/app/group_dispatcher.py`

**查找**：`grep -n "member_ids\|effective_node_ids" backend/app/group_dispatcher.py`

**逐个检查**：将所有 `group_rules.member_ids(group)` 调用改为 `group_rules.participant_ids(group)`；将 `group_rules.effective_node_ids(...)` 改为 `group_rules.effective_node_ids_with_followers(...)`（新增函数）。

**特别注意**：以下两处**不要**改：
- `limit_watch_report_ids` 相关逻辑（跟单成员不进限价监听）
- CLOSE 逻辑（按 group_ids 全量终止，不走 participant_ids）

**验证**：`python -c "from backend.app import group_dispatcher; print('ok')"` → 无报错；信号分发逻辑行为不变（因为 `followers` 目前为空数组）

**回滚**：撤销所有改动

---

## 步骤 8：交易页带单开关接线

### 8.1 `frontend/src/views/TradingView.vue`

**文件**：`frontend/src/views/TradingView.vue`

**查找**：`grep -n "carry\|v-model" frontend/src/views/TradingView.vue | grep -i carry`

**改动 1**：找到 `OrderDialog` 的使用处（`defineModel` 或 `ref`），确保传了 `v-model:carry="carry"`

```vue
<OrderDialog
  v-model:carry="carry"
  ...
/>
```

**改动 2**：在 `TradingView` 的 `script setup` 中添加

```typescript
const carry = ref(true)
```

**改动 3**：在 `quickTrigger` 或 `triggerManualSignal` 的 payload 中，根据 `carry` 决定是否加 `node_ids` 字段

**验证**：
1. 打开【交易】Tab，选中任意分组，`carry` 开关可见（目前是死开关，但 UI 链路通了）
2. 开关状态在重新打开弹窗后重置为 true

---

### 8.2 `frontend/src/components/OrderDialog.vue`

**文件**：`frontend/src/components/OrderDialog.vue`

**查找**：`grep -n "carry\|od-carry" frontend/src/components/OrderDialog.vue | head -20`

**改动**：确保 `defineModel('carry')` 存在，并将按钮文案改为动态

```typescript
const carryLabel = computed(() =>
  carry.value
    ? `触发 · 本次将下发给 ${totalNodes} 个节点`
    : '触发 · 仅下发到主节点'
)
```

**验证**：切换 `carry` 开关，按钮文案随之变化

---

## 步骤 9：填充 `follow_api.py` 逻辑（按设计方案 §5）

按设计方案 §5 逐个填充路由：

1. `GET /api/follow/leaders` → §5.1
2. `GET /api/follow/leaders/{id}` → §5.2
3. `GET /api/follow/me` → §5.3
4. `POST /api/follow/join` → §5.4
5. `POST /api/follow/leave` → §5.5

每填充完一个接口，立即用 `curl` 验证。

---

## 步骤 10：填充 `FollowView.vue` 完整 UI

按设计方案 §7 实现完整 UI：
1. 带单员列表（卡片 + 搜索）
2. 跟单弹窗（分组 × 节点双区选择）
3. 我的跟单抽屉
4. 移出确认弹窗

---

## 步骤 11：用户权限页「能力」列

**文件**：`frontend/src/views/PermissionView.vue`

**改动**：在用户表格中新增一列「能力」，展示该用户所有角色的能力并集

**验证**：编辑一个角色，勾选「带单」→ 保存 → 该角色下的用户列表「能力」列应显示「带单」

---

## 步骤 12：全局功能开关（可选，二期）

在 `system_settings.py` 中添加 `follow_feature` 配置，默认 `"0"`，上线后改为 `"1"`。

---

## 执行顺序总结

```
阶段 A（无破坏性，可随时上线）
├── 步骤 0  数据库准备（建空表）
├── 步骤 1  能力注册（后端 + 前端）
├── 步骤 2  菜单注册（后端 + 前端）
├── 步骤 3  前端路由 + 导航
└── 步骤 4  空页面（验证链路）

阶段 B（种子逻辑，上线即生效）
└── 步骤 5  seed_rbac 幂等补菜单
    → 验证：存量普通用户能看到「带单」Tab

阶段 C（核心后端，谨慎）
├── 步骤 6  follow_rules / follow_service / follow_api（骨架）
├── 步骤 7  分发链路改造
│   ├── 7.1  group_rules.participant_ids
│   ├── 7.2  group_service 缓存加 followers
│   └── 7.3  group_dispatcher 替换 member_ids
└── 步骤 6.4  main.py 注册路由
    → 验证：信号分发行为不变（followers 为空时无副作用）

阶段 D（交易页接线）
└── 步骤 8  TradingView + OrderDialog 带单开关

阶段 E（业务逻辑）
├── 步骤 9  follow_api 逻辑填充
└── 步骤 10 FollowView 完整 UI

阶段 F（用户权限）
└── 步骤 11 用户权限页「能力」列

阶段 G（上线）
└── 步骤 12 全局功能开关
```

---

## 关键风险点

| 步骤 | 风险 | 缓解 |
|---|---|---|
| 7.3 group_dispatcher | 改错 `member_ids` 调用导致分发逻辑出错 | 改前备份，验证时用两条 curl 确认分发行为不变 |
| 7.1 effective_node_ids | 主节点被意外过滤 | 加单元测覆盖 |
| 5 seed_rbac | 存量 user 角色已有菜单导致冲突 | 幂等逻辑用 `if not in existing_menus`，不会重复插入 |

---

## 回滚预案

| 步骤 | 回滚方式 |
|---|---|
| 0 | `DROP TABLE group_follower_node` |
| 1 | 撤销 permissions.py 三处改动 + rbac.ts 两处改动 |
| 2 | 撤销 permissions.py MENU_FOLLOW 常量 + MenuDef 条目 |
| 3 | 删除 FollowView.vue + 撤销 router/index.ts + App.vue |
| 5 | 删除新增的 if 块（保留原有 if 块） |
| 7 | 逐个撤销三处改动（备份文件对比） |
| 任意 | 重启后端即可清除内存状态（无持久化状态污染） |