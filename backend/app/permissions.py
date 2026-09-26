"""用户权限的纯规则：菜单注册表、登录身份（Principal）与数据归属判定。

不做任何 I/O：路由 / 服务层拿到数据后调这里判断，便于单测。

- 菜单是代码注册表：页面与接口写死在代码里，库（sys_role_menu）只存「角色勾了哪些菜单」；
- 数据归属靠 owner_user_id：空 = 管理员名下；普通用户只能看 / 改自己名下的数据；
- 超级管理员（内置角色 admin）拥有全部菜单、看全部数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

ADMIN_ROLE_CODE = "admin"   # 内置超级管理员角色
DEFAULT_ROLE_CODE = "user"  # 内置普通用户角色（新建用户未指定角色时使用）

MENU_DASHBOARD = "dashboard"
MENU_NODES = "nodes"
MENU_TREND = "trend"
MENU_GROUPS = "groups"
MENU_STRATEGIES = "strategies"
MENU_AUDITS = "audits"
MENU_CONSOLE = "console"
MENU_CLIENT_VERSIONS = "client_versions"
MENU_EVENTS = "events"
MENU_PERMISSIONS = "permissions"


@dataclass(frozen=True)
class MenuDef:
    code: str
    name: str
    path: str
    # False = 仅管理员：页面展示全局数据或承载全局写操作，分配给普通角色会越过数据归属
    assignable: bool


MENU_REGISTRY: tuple[MenuDef, ...] = (
    MenuDef(MENU_DASHBOARD, "总览", "/", True),
    MenuDef(MENU_NODES, "节点", "/nodes", True),
    MenuDef(MENU_TREND, "趋势面板", "/trend", True),
    MenuDef(MENU_GROUPS, "分组管理", "/groups", True),
    MenuDef(MENU_STRATEGIES, "策略管理", "/strategies", True),
    MenuDef(MENU_AUDITS, "操作审计", "/audits", True),
    MenuDef(MENU_CONSOLE, "中控台", "/console", False),
    MenuDef(MENU_CLIENT_VERSIONS, "客户端版本", "/client-versions", False),
    MenuDef(MENU_EVENTS, "事件", "/events", False),
    MenuDef(MENU_PERMISSIONS, "用户权限", "/permissions", False),
)

ALL_MENU_CODES: tuple[str, ...] = tuple(m.code for m in MENU_REGISTRY)
ASSIGNABLE_MENU_CODES: tuple[str, ...] = tuple(m.code for m in MENU_REGISTRY if m.assignable)
# 内置普通用户角色的初始菜单
DEFAULT_USER_MENUS: tuple[str, ...] = ASSIGNABLE_MENU_CODES


@dataclass(frozen=True)
class Principal:
    """一次请求 / 一条后台 WS 连接的登录身份。"""
    user_id: int
    username: str
    is_admin: bool
    menus: frozenset[str]
    token_version: int = 0


# ---------------------------------------------------------------------------
# 菜单
# ---------------------------------------------------------------------------
def _clean_codes(codes: Iterable[object] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes or []:
        code = str(raw or "").strip()
        if code and code not in seen:
            out.append(code)
            seen.add(code)
    return out


def normalize_menu_codes(codes: Iterable[object] | None) -> list[str]:
    """去空白、去重，按注册表顺序排列；注册表里没有的 code 丢弃。"""
    wanted = set(_clean_codes(codes))
    return [c for c in ALL_MENU_CODES if c in wanted]


def invalid_menu_codes(codes: Iterable[object] | None) -> list[str]:
    """角色不能勾选的 code：注册表里没有的，以及仅管理员菜单。"""
    return [c for c in _clean_codes(codes) if c not in ASSIGNABLE_MENU_CODES]


def resolve_menus(is_admin: bool, role_menu_codes: Iterable[object] | None) -> frozenset[str]:
    """用户最终可用的菜单：管理员 = 全部；普通用户 = 角色菜单并集 ∩ 可分配菜单。

    库里残留的已下线 / 仅管理员 code 在这里被滤掉，不会因历史数据放大权限。
    """
    if is_admin:
        return frozenset(ALL_MENU_CODES)
    return frozenset(c for c in _clean_codes(role_menu_codes) if c in ASSIGNABLE_MENU_CODES)


def can_menu(p: Principal, *codes: str) -> bool:
    """是否拥有任一菜单（同一操作可能出现在多个页面，例如总览与节点页都能平仓）。"""
    return p.is_admin or any(c in p.menus for c in codes)


# ---------------------------------------------------------------------------
# 数据归属
# ---------------------------------------------------------------------------
def normalize_owner(value: object) -> Optional[int]:
    """owner_user_id 规范化：空 / 非法 / 非正数 → None（管理员名下）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        owner = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return owner if owner > 0 else None


def same_owner(a: object, b: object) -> bool:
    """两条数据是否同一所有者（都为空也算同一所有者：都在管理员名下）。"""
    return normalize_owner(a) == normalize_owner(b)


def owns(p: Principal, owner_user_id: object) -> bool:
    """能否看 / 改这条数据：管理员全部可以；普通用户只能是自己名下的。"""
    if p.is_admin:
        return True
    owner = normalize_owner(owner_user_id)
    return owner is not None and owner == p.user_id


def visible(p: Principal, items: Iterable[dict]) -> list[dict]:
    """按归属过滤列表（节点 / 分组 / 策略的缓存 dict 都带 owner_user_id）。"""
    return [d for d in items if owns(p, (d or {}).get("owner_user_id"))]


def owner_for_new(p: Principal) -> Optional[int]:
    """新建分组 / 策略的所有者：管理员建的归管理员名下（空），普通用户归自己。"""
    return None if p.is_admin else p.user_id


def foreign_node_ids(nodes: Iterable[dict], owner_user_id: object) -> list[str]:
    """不在 owner_user_id 名下的节点 ID（分组成员必须与分组同一所有者）。"""
    return [
        str(n.get("node_id"))
        for n in nodes
        if n and not same_owner(n.get("owner_user_id"), owner_user_id)
    ]


def _norm_id(value: object) -> str:
    return str(value or "").strip().lower()


def scope_group_ids(
    requested: Iterable[object] | None, own_group_ids: Iterable[object],
) -> tuple[list[str], list[str]]:
    """把普通用户手动触发的分组定向收窄到本人分组。

    没点名 → 本人全部分组；点名了 → 只保留本人的，其余作为越权返回。
    返回 (收窄后的分组 ID, 越权的分组 ID)，都已 strip + lower，去重保序。
    """
    own: list[str] = []
    for gid in own_group_ids:
        key = _norm_id(gid)
        if key and key not in own:
            own.append(key)
    wanted: list[str] = []
    for gid in requested or []:
        key = _norm_id(gid)
        if key and key not in wanted:
            wanted.append(key)
    if not wanted:
        return own, []
    own_set = set(own)
    return [g for g in wanted if g in own_set], [g for g in wanted if g not in own_set]


# ---------------------------------------------------------------------------
# 后台实时推送（/ws/admin）
# ---------------------------------------------------------------------------
# 只推给管理员的事件：新节点自动注册时还没分配给任何人
ADMIN_ONLY_EVENTS = frozenset({"node_registered"})


def event_resource(message: object) -> Optional[tuple[str, str]]:
    """推送事件归属的资源：("group", id) 或 ("node", id)；None = 只推给管理员。

    同时带 group_id 与 node_id 时按分组算：分组成员与分组同一所有者，二者一致。
    """
    if not isinstance(message, dict) or message.get("type") in ADMIN_ONLY_EVENTS:
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    gid = str(data.get("group_id") or "").strip()
    if gid:
        return ("group", gid)
    nid = str(data.get("node_id") or "").strip()
    if nid:
        return ("node", nid)
    return None


def event_visible(
    p: Principal, resource: Optional[tuple[str, str]], owner_user_id: object,
) -> bool:
    """该连接能否收到这条事件：管理员全收；普通用户只收自己名下资源的事件。"""
    if p.is_admin:
        return True
    if resource is None:
        return False
    return owns(p, owner_user_id)
