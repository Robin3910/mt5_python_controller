"""permissions.py 纯函数：菜单注册表、数据归属判定、手动触发收窄、推送事件归属。"""
from app import permissions as perm
from app.permissions import Principal

ADMIN = Principal(user_id=1, username="admin", is_admin=True, menus=frozenset(perm.ALL_MENU_CODES))
ALICE = Principal(user_id=7, username="alice", is_admin=False, menus=frozenset({"groups"}))


def test_registry_codes_unique_and_admin_only_not_assignable():
    codes = [m.code for m in perm.MENU_REGISTRY]
    assert len(codes) == len(set(codes))
    for code in ("console", "client_versions", "events", "permissions"):
        assert code not in perm.ASSIGNABLE_MENU_CODES
    assert set(perm.DEFAULT_USER_MENUS) == set(perm.ASSIGNABLE_MENU_CODES)


def test_normalize_menu_codes_orders_by_registry_and_drops_unknown():
    assert perm.normalize_menu_codes([" groups", "nodes", "groups", "bogus", ""]) == [
        "nodes", "groups",
    ]


def test_invalid_menu_codes_rejects_unknown_and_admin_only():
    assert perm.invalid_menu_codes(["groups", "console", "bogus", " "]) == ["console", "bogus"]
    assert perm.invalid_menu_codes(["dashboard", "audits"]) == []


def test_resolve_menus_admin_gets_all_user_gets_assignable_union():
    assert perm.resolve_menus(True, []) == frozenset(perm.ALL_MENU_CODES)
    # 库里残留的仅管理员菜单 / 已下线菜单不会放大普通用户权限
    assert perm.resolve_menus(False, ["groups", "console", "gone", "nodes"]) == frozenset(
        {"groups", "nodes"}
    )


def test_can_menu_any_of_codes():
    assert perm.can_menu(ADMIN, "console")
    assert perm.can_menu(ALICE, "nodes", "groups")
    assert not perm.can_menu(ALICE, "nodes", "dashboard")


def test_normalize_owner_and_same_owner():
    assert perm.normalize_owner(None) is None
    assert perm.normalize_owner("") is None
    assert perm.normalize_owner("abc") is None
    assert perm.normalize_owner(0) is None
    assert perm.normalize_owner(True) is None
    assert perm.normalize_owner("7") == 7
    assert perm.same_owner(None, None)
    assert perm.same_owner("7", 7)
    assert not perm.same_owner(None, 7)


def test_owns_and_visible():
    rows = [
        {"id": "a", "owner_user_id": None},
        {"id": "b", "owner_user_id": 7},
        {"id": "c", "owner_user_id": 8},
    ]
    assert perm.owns(ADMIN, None) and perm.owns(ADMIN, 8)
    assert perm.owns(ALICE, 7)
    assert not perm.owns(ALICE, None)
    assert not perm.owns(ALICE, 8)
    assert [r["id"] for r in perm.visible(ALICE, rows)] == ["b"]
    assert [r["id"] for r in perm.visible(ADMIN, rows)] == ["a", "b", "c"]


def test_owner_for_new():
    assert perm.owner_for_new(ADMIN) is None
    assert perm.owner_for_new(ALICE) == 7


def test_foreign_node_ids():
    nodes = [
        {"node_id": "n1", "owner_user_id": 7},
        {"node_id": "n2", "owner_user_id": None},
        {"node_id": "n3", "owner_user_id": 8},
    ]
    assert perm.foreign_node_ids(nodes, 7) == ["n2", "n3"]
    assert perm.foreign_node_ids(nodes, None) == ["n1", "n3"]


def test_scope_group_ids_injects_own_when_empty():
    scoped, foreign = perm.scope_group_ids([], ["GRP_A", "grp_b", "grp_a"])
    assert scoped == ["grp_a", "grp_b"]
    assert foreign == []


def test_scope_group_ids_splits_foreign():
    scoped, foreign = perm.scope_group_ids([" GRP_A ", "grp_x", "grp_x"], ["grp_a", "grp_b"])
    assert scoped == ["grp_a"]
    assert foreign == ["grp_x"]


def test_event_resource():
    assert perm.event_resource({"type": "node_registered", "data": {"node_id": "n1"}}) is None
    assert perm.event_resource({"type": "account", "data": {"node_id": "n1"}}) == ("node", "n1")
    assert perm.event_resource(
        {"type": "group_dispatch", "data": {"group_id": "g1", "node_id": "n1"}}
    ) == ("group", "g1")
    assert perm.event_resource({"type": "pong"}) is None
    assert perm.event_resource("junk") is None


def test_event_visible():
    res = ("node", "n1")
    assert perm.event_visible(ADMIN, None, None)
    assert perm.event_visible(ALICE, res, 7)
    assert not perm.event_visible(ALICE, res, 8)
    assert not perm.event_visible(ALICE, res, None)
    assert not perm.event_visible(ALICE, None, 7)
