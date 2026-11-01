# 跟单节点继承实施计划（OrderDialog 自动建组时沿用带单员既有跟单节点）

> 场景：带单员触发**新的分组 + 策略**创建（换品种 / 换策略类型）时，把**他名下其它
> 带单源分组里已有的跟单节点**一并写入新分组，避免跟单员每次都要手动去【带单】页重新跟一次。
>
> 本计划按可独立验证的步骤排列，每步末尾标注「验证方法」和「回滚方式」。

---

## 目标与决策

**核心语义**：在 `OrderDialog` 首次建组那一刻，**快照**该带单员名下其它带单源分组里的全部跟单节点，一并写入新分组。

**三条已确认决策**：

| # | 决策 | 含义 |
|---|---|---|
| 1 | **跨策略类型继承** | 从市价切网格、或换品种，**不做类型过滤**。分组名 `${strategy_type}-${username}-${login}-${symbol}`，任一维度变化即全新分组，跟单节点全部继承 |
| 2 | **只对已在跟单的节点操作** | 绝不能拉「本来就没跟我」的用户进来。只搬运**已存在的跟单关系**，源来自 `group_follower_node` 表 |
| 3 | **失败不阻断信号** | 继承失败时信号照常下发（主节点正常开仓，跟单节点这轮不跟），仅弹提示 |

---

## 关键约束（动手前必读）

### 约束 A：不能复用 `POST /api/follow/join`

```366:377:backend/app/follow_api.py
async def join_follow(
    body: FollowJoinRequest,
    ...
    """把当前用户的若干节点加入带单员的若干分组。
```

该接口语义是「**我**的节点加入**别人**的分组」，`follower_user_id=p.user_id`。带单员调它会把自己当跟单员，方向完全错误。**必须新增一条后端路径。**

### 约束 B：`node_ids` 碰不到跟单表

```255:255:backend/app/models.py
    node_ids: Optional[list[str]] = None  # 传入即整体替换成员列表
```

`node_ids` 只写 `NodeGroupMember`（owner 成员），**碰不到** `group_follower_node`。两表在 ORM 里严格分离：

```296:303:backend/app/orm.py
class GroupFollowerNode(Base):
    """跟单成员：非主节点加入他人的带单源分组。

    与 NodeGroupMember 严格分离，两者职责不可混用：
        - NodeGroupMember    = 分组所有者自己的节点（owner 可编辑 / 排序 / 开限价监听）；
        - GroupFollowerNode  = 跟单员的节点（只读、不可被 owner 编辑、限价监听恒关）。
```

`GroupUpdate` 因此**不新增** `followers` 字段 —— 保持「owner 不能直接编辑跟单成员」这条既有边界。

### 约束 C：`follower_user_id` 必须从源关系带过来

```177:180:backend/app/follow_service.py
        s.add(GroupFollowerNode(
            group_id=group_id,
            node_id=node_id,
            follower_user_id=int(follower_user_id),
            leader_user_id=int(leader_user_id),
```

**这是整件事唯一会写错的地方**。填 leader 自己会让跟单员在 FollowView 里看不到这条关系（`list_followings` 按 `follower_user_id` 过滤），且 `follows_node` 归属判定失效。

### 约束 D：必须在 `strategy_id` 绑定之后

```426:427:backend/app/follow_api.py
        if g.get("strategy_id") and g["strategy_id"] not in strategies:
            continue
```

跟单关系对无策略分组没有意义。`OrderDialog` 建组阶段 `strategy_id` 还是 `null`，所以**只能放在触发流程里**，不能放在 `loadGroupAndStrategy()` 的建组处。

### 约束 E：无条件调用，不看 `carry` 开关

分组成员是**持久状态**，`carry` 只控制单条信号的投递范围（`payload.node_ids`）。这与现有 `limit_watch_enabled` / `signal_concurrent_enabled` 的无条件 PATCH 保持一致。附带收益：幂等，**能顺带修复「功能上线前已存在但无跟单节点」的存量分组**。

---

## 已核查的两个隐患点（均安全，无需额外防御）

### 隐患 1：限价监听不会被跟单节点误触发 —— 安全

```210:220:backend/app/group_rules.py
def limit_watch_report_ids(group: dict) -> list[str]:
    """已打开「限价监听上报」的成员，按组内顺序。缺字段视为关闭。

    只看 owner 成员：跟单员的挂单永不作为触发源，否则跟单者在自己终端随手挂一张
    限价单就会触发带单方的分组开仓。
    """
```

`OrderDialog` 开启监听时传的是 `currentGroup.nodes`（owner 成员）而非 `followers`。**继承动作放在监听 PATCH 前或后都安全。**

### 隐患 2：带单员切主节点不产生脏关系 —— 安全

> ⚠️ **2026-10-24 更新**：切主节点的行为已从「静默清理跟单」改为**硬拦截**。
> 跟单关系是持久状态，静默清掉会让用户的跟单配置无声消失，所以现在命中跟单时
> 直接返回 409（`code=primary_switch_following_blocked`），让用户先自己移出跟单。
> 下面的 `remove_node_followings` 调用保留为**兜底**（正常路径已被阶段 0 拦下）。

```538:544:backend/app/node_service.py
    # 跟单关系清理（升为主节点时）：
    # - 正常路径：阶段 0 的 precheck_following_blocking 已经拦下「仍在跟单」的节点，
    #   不会走到这里，所以正常情况下这里是无操作。
    # - 兜底：阶段 0 与本次清理之间的极小窗口期若新加入了跟单关系，
    #   这里把它清掉，避免该节点同时当带单源和跟单方（语义冲突）。
    # - 取消主节点：重新具备跟单资格，但不会自动重新加入，保持用户显式操作。
    if is_primary:
        await follow_service.remove_node_followings(store, node_id)
```

老分组因「不再含新主节点」自动退出带单市场，运行时分发退回 owner 口径（`group_still_leads` → `owner_effective_node_ids`）。**降级安全。**

---

## 步骤 1：后端 —— 认领接口

### 1.1 `models.py` —— 请求/响应模型

**文件**：`backend/app/models.py`

在 `GroupUpdate` 类之后（约第 259 行）追加：

```python
class AdoptFollowersResponse(BaseModel):
    """分组 owner 认领跟单节点的结果。"""
    adopted: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    scanned_groups: int = 0
```

**验证**：`python -c "from backend.app.models import AdoptFollowersResponse"` 无报错

**回滚**：删除该类

---

### 1.2 `follow_service.py` —— 认领写入函数

**文件**：`backend/app/follow_service.py`

在「写入」区（`add_following` 之后，约第 184 行）追加：

```python
async def adopt_followers_for_group(
    store: RedisStore,
    *,
    target_group: dict,
    source_group_ids: list[str],
) -> tuple[list[dict], list[dict]]:
    """把来源分组里的跟单节点写入目标分组；返回 (adopted, skipped)。

    - 来源分组由调用方筛选（只取 owner 是本人的带单源分组）；
    - 同一跟单节点跟了多个来源分组时按 node_id 去重，follower_user_id 取第一条；
    - 逐条写 add_following（内部已存在即返回 False，天然幂等）。

    leader_user_id / leader_primary_node_id **一律从源关系取**，不作为入参：
    源关系里已存了这两个字段（写入时快照），直接复用可避免管理员名下分组
    （owner_user_id 为 None）触发 int(None) 崩溃，也避免与源关系不一致。
    """
    target_gid = str((target_group or {}).get("group_id") or "").strip()
    src_ids = [
        str(g).strip() for g in (source_group_ids or [])
        if str(g or "").strip() and str(g).strip() != target_gid
    ]
    if not target_gid or not src_ids:
        return [], []

    # 按 node_id 去重，保留首次出现的来源分组，便于回显「继承自哪个分组」
    seen: dict[str, dict] = {}
    for f in await list_for_groups(src_ids):
        nid = str(f.get("node_id") or "").strip()
        if nid and nid not in seen:
            seen[nid] = f

    adopted: list[dict] = []
    skipped: list[dict] = []
    for nid, src in seen.items():
        follower_uid = src.get("follower_user_id")
        leader_uid = src.get("leader_user_id")
        if not follower_uid or not leader_uid:
            skipped.append({"node_id": nid, "reason": "跟单关系缺少归属信息"})
            continue
        ok = await add_following(
            store,
            group_id=target_gid,
            node_id=nid,
            follower_user_id=int(follower_uid),
            leader_user_id=int(leader_uid),
            leader_primary_node_id=src.get("leader_primary_node_id"),
        )
        row = {
            "node_id": nid,
            "follower_user_id": int(follower_uid),
            "from_group_id": src.get("group_id"),
        }
        (adopted if ok else skipped).append(row)
    return adopted, skipped
```

**验证**：新增单测（见步骤 3）覆盖去重与幂等

**回滚**：删除该函数

---

### 1.3 `groups.py` —— 路由

**文件**：`backend/app/groups.py`

**改动 1**：补 import。现有：

```12:32:backend/app/groups.py
from . import group_persist, group_rules, group_service, permissions, persist, rbac_service
```

改为：

```python
from . import (
    follow_rules,
    follow_service,
    group_persist,
    group_rules,
    group_service,
    permissions,
    persist,
    rbac_service,
)
```

并在 `from .models import (` 块内追加 `AdoptFollowersResponse`。

**改动 2**：在 `update_group`（约第 363 行）之前插入路由：

```python
@router.post("/{group_id}/adopt-followers", response_model=AdoptFollowersResponse)
async def adopt_followers(
    group_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    """分组 owner 认领该分组沿用自己其它分组的跟单节点（跨策略类型 / 品种继承）。

    只搬运**已存在的跟单关系**，绝不引入「本来就没跟我」的用户。
    来源分组 = 本人名下、含本人主节点的分组，**忽略启用状态**（已确认决策），
    因此手工禁用的源分组里的跟单节点同样会继承。

    归属校验用 owned_group_or_404：非本人分组一律 404，不泄露任何存在性。
    """
    target = await owned_group_or_404(store, p, group_id)
    owner = permissions.normalize_owner(target.get("owner_user_id"))

    all_nodes = await store.all_nodes()
    primary = follow_rules.leader_primary_node_id(all_nodes, owner)
    if not primary:
        # 没有主节点就没有带单源，也就没有可继承的跟单关系
        return AdoptFollowersResponse(adopted=[], skipped=[], scanned_groups=0)

    all_groups = await store.all_groups()
    sources = [
        g for g in follow_rules.followable_groups(all_groups, owner, primary)
        if str(g.get("group_id")) != str(group_id)
    ]
    adopted, skipped = await follow_service.adopt_followers_for_group(
        store,
        target_group=target,
        source_group_ids=[g["group_id"] for g in sources],
    )
    await persist.audit(
        p.username, "adopt_followers", group_id, None, "ok", client_ip(request),
        category="follow",
        before=None,
        after={
            "adopted": adopted,
            "skipped_count": len(skipped),
            "source_group_count": len(sources),
        },
    )
    return AdoptFollowersResponse(
        adopted=adopted, skipped=skipped, scanned_groups=len(sources),
    )
```

**关键点**：

- 来源筛选用 `is_leader_source_group`（owner + 含主节点），**去掉 `enabled` 条件**（决策 1）。
- 权限用 `MENU_GROUPS`（与 `POST /api/groups` 同口径），带单员必然有「分组管理」菜单。
- `owned_group_or_404` 放行管理员对任意分组的操作，管理员代普通用户认领时 `owner` 取该分组的真实 owner，行为与本人操作一致。
- **管理员名下分组**（`owner_user_id` 为 `None`）永远没有跟单成员 —— 因为 `is_leader_source_group` 要求分组 owner 与 `leader_user_id` 匹配，管理员分组的 owner 是 `None`，没有任何真实用户 id 能匹配上。该路径自然为空集，无需特判。
- `leader_user_id` 不作为入参传入，而是从源关系取（见步骤 1.2 的说明），避免 `int(None)` 崩溃。

**验证**：

```bash
curl -X POST http://<host>/api/groups/<gid>/adopt-followers -H "Authorization: Bearer <token>"
```

→ 200，返回 `{"adopted":[...],"skipped":[...],"scanned_groups":N}`

**回滚**：删除该路由

---

### ⚠️ 决策 1 的落地点：忽略源分组启用状态

`follow_rules.followable_groups` 内含 `enabled` 过滤：

```76:85:backend/app/follow_rules.py
def followable_groups(
    groups: list[dict], leader_user_id: int, primary_node_id: Optional[str],
) -> list[dict]:
    """带单员名下所有可被跟单的分组（启用 + 含主节点）。"""
    if not primary_node_id:
        return []
    return [
        g for g in (groups or [])
        if isinstance(g, dict)
        and g.get("enabled", True)
        and is_leader_source_group(g, leader_user_id, primary_node_id)
    ]
```

**已确认要忽略 `enabled`**（继承被禁用分组里的跟单节点）。因此**不用 `followable_groups`**，改用底层判据：

```python
sources = [
    g for g in (all_groups or [])
    if isinstance(g, dict)
    and str(g.get("group_id") or "") != str(group_id)
    and follow_rules.is_leader_source_group(g, owner, primary)
]
```

即：**保留 `is_leader_source_group`（owner + 含主节点），去掉 `enabled` 条件**。

> 副作用（符合预期）：带单员手工禁用某个源分组，只是不想让该分组对外接单；
> 继承走的是「我自己的跟单关系」，与该分组是否对外接单无关，所以仍会继承。

---

## 步骤 2：前端 —— OrderDialog 接入

**文件**：`frontend/src/components/OrderDialog.vue`

### 2.1 新增 store action

**文件**：`frontend/src/stores/hub.ts`

在 `updateGroup`（约第 229 行）之后追加：

```typescript
/** 分组 owner 认领自己其它分组的跟单节点（跨策略类型/品种继承） */
async adoptFollowers(id: string): Promise<{ adopted: unknown[]; skipped: unknown[] }> {
  const { data } = await api.post(`/api/groups/${id}/adopt-followers`)
  await this.fetchGroups()
  return data
},
```

**验证**：`npm run build` 无类型错误

**回滚**：删除该方法

---

### 2.2 在触发流程里调用

**关键位置**：`triggerMarketStrategy()` 与 `triggerGridStrategy()` 中，紧跟 `signal_concurrent_enabled` PATCH 之后、`emit('trigger-strategy', payload)` 之前。

现有代码（`triggerMarketStrategy`）：

```964:978:frontend/src/components/OrderDialog.vue
    // 信号并发：策略建/绑成功后自动开启（市价/限价用模版2，网格用模版3，均支持并发）。
    // 必须在 strategy_id 绑上之后才能开（update_group:346 会校验策略类型）。
    // 已存在的分组若已开启，此 PATCH 是幂等的；用户手动关过则会被重新打开。
    if (currentGroup.value) {
      const groupId = currentGroup.value.group_id
      try {
        await hub.updateGroup(groupId, { signal_concurrent_enabled: true })
        const refreshed = (await hub.listGroups()).find((g) => g.group_id === groupId)
        if (refreshed) currentGroup.value = refreshed
      } catch (e) {
        // 信号并发失败不影响信号下发：仅给用户提示，不抛出
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '打开信号并发失败'
      }
    }
```

在**两个**函数的该 `if` 块之后插入：

```typescript
    // 跟单节点继承：把本带单员其它带单源分组里已存在的跟单节点一并写入本分组。
    // 跨策略类型 / 品种生效（分组名按 策略类型-用户名-MT5号-品种 生成，任一维度变化即新分组）。
    // 无条件调用（不看 carry 开关）：分组成员是持久状态，carry 只管单条信号的投递范围。
    // 幂等，顺带修复「功能上线前已存在但无跟单节点」的存量分组。
    // 必须在 strategy_id 绑上之后 —— 跟单关系对无策略分组没有意义。
    if (currentGroup.value) {
      try {
        const r = await hub.adoptFollowers(currentGroup.value.group_id)
        const n = r.adopted?.length || 0
        if (n > 0) {
          const refreshed = (await hub.listGroups()).find(
            (g) => g.group_id === currentGroup.value!.group_id,
          )
          if (refreshed) currentGroup.value = refreshed
        }
      } catch (e) {
        // 继承失败不影响信号下发：仅给用户提示，不抛出
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '跟单节点同步失败，信号已照常下发'
      }
    }
```

**验证**：

1. 带单员 A 已建 `trend_market-A-1234-EURUSD` 分组，跟单员 B 的 `b_follow` 已加入。
2. A 打开交易页 → 切到 XAUUSD → 开市价单。
3. 查 `GET /api/groups/{new_gid}` → `followers` 含 `b_follow`，`follower_count >= 1`。
4. B 打开【带单】→【我的跟单】→ 能看到 A 的新分组，可自行移出。

**回滚**：删除这两处插入块 + 步骤 2.1 的 store action

---

## 步骤 3：测试

### 3.1 后端单测

**文件**：`backend/tests/test_follow_api.py`（追加到文件末尾）

复用现有 fixture 与辅助函数：`_mk_user(client, h, username)`、`_mk_node(client, h, name, mt5_login, primary=...)`、`alice_h` / `bob_h`。

```python
def test_adopt_followers_inherits_across_groups(client):
    """新建分组认领继承：跨品种 / 跨策略类型，且 follower_user_id 取自源关系。"""
    ...


def test_adopt_followers_is_idempotent(client):
    """重复调用不产生重复关系（uq_follow_group_node + add_following 返回 False）。"""
    ...


def test_adopt_followers_ignores_disabled_source_group(client):
    """已确认决策：源分组被禁用时，跟单节点仍应继承。"""
    ...


def test_adopt_followers_rejects_foreign_group(client):
    """非本人分组一律 404，不泄露存在性。"""
    ...


def test_adopt_followers_only_uses_existing_followings(client):
    """决策 2：不会凭空引入没跟我的人；只搬运已存在的跟单关系。"""
    ...


def test_adopt_followers_dedupes_multi_group_followers(client):
    """同一节点跟了 A 的多个分组时只继承一次。"""
    ...
```

**验证**：`pytest backend/tests/test_follow_api.py -k adopt -v`

**回滚**：删除这些测试

---

### 3.2 回归

**必须跑**：

```bash
pytest backend/tests/test_follow_api.py -v
pytest backend/tests/test_group_api.py -v
pytest backend/tests/test_limit_watch.py -v
```

`test_owner_edit_members_keeps_followers` 尤其重要 —— 它守着「owner 编辑 `node_ids` 不会误删跟单成员」这条边界，本次新增接口不能破坏它。

**验证**：全绿

**回滚**：不适用

---

## 上线顺序

1. 先上后端（新增接口，纯增量，无破坏）
2. 再上前端（`hub.adoptFollowers` + 两处调用）
3. 后端先上不会有人调用；前端先上会 404 —— **必须后端先行**

---

## 风险与回滚总表

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 跟单员被意外拉进新分组 | 只搬运 `group_follower_node` 已存在关系（决策 2） | 跟单员自行【我的跟单】移出，或直接删分组 |
| `follower_user_id` 填错成 leader | 步骤 1.2 显式从源关系取；测试断言该字段 | 删掉已写入的行 |
| 重复继承产生重复关系 | `uq_follow_group_node` + `add_following` 返回 False | 唯一约束兜底，不会脏 |
| 跟单节点被限价监听误触发 | `limit_watch_report_ids` 只读 owner 成员（已核查） | 无需处理 |
| 前端先于后端上线 | 上线顺序强制后端先行 | — |