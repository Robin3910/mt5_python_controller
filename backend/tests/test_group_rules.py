"""分组纯规则单元测试（无 I/O）——与 test_rules.py（normal 链路）配套。

锁定 strategy 链路的判定口径：model 字段规范化、分组分发模式、有效节点、
组内轮转对齐、主任务号 <-> 魔术号换算、主任务状态汇总。
"""
import pytest

from app import group_rules
from app.config import Config


# =====================================================================
# model 字段（Webhook 新增枚举）
# =====================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "normal"),        # 缺失 -> 默认 normal（当前项目默认方式）
        ("", "normal"),          # 空串 -> 默认 normal
        ("   ", "normal"),       # 纯空白 -> 默认 normal
        ("normal", "normal"),
        ("NORMAL", "normal"),    # 大小写不敏感
        ("strategy", "strategy"),
        (" Strategy ", "strategy"),
    ],
)
def test_normalize_signal_model_accepts_enum_and_blank(raw, expected):
    assert group_rules.normalize_signal_model(raw) == expected


@pytest.mark.parametrize("raw", ["group", "abc", "0", "1", "normal2", "strategyy"])
def test_normalize_signal_model_rejects_unknown(raw):
    assert group_rules.normalize_signal_model(raw) is None


# =====================================================================
# 分组分发模式（不区分币种）
# =====================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sync", "sync"),
        ("poll", "poll"),
        ("POLL", "poll"),
        (None, "sync"),      # 缺省 -> sync
        ("", "sync"),
        ("random", "sync"),  # 非法值回落 sync
    ],
)
def test_normalize_dispatch_mode(raw, expected):
    assert group_rules.normalize_dispatch_mode(raw) == expected


# =====================================================================
# 手数：直接用信号手数，仅做单笔上限保护（不读节点/中控台配置）
# =====================================================================
def test_resolve_volume_uses_signal_volume():
    assert group_rules.resolve_volume(0.37) == 0.37


def test_resolve_volume_capped_by_max_lot():
    assert group_rules.resolve_volume(Config.MAX_LOT_SIZE + 5) == Config.MAX_LOT_SIZE


@pytest.mark.parametrize("raw", [-1, None, "abc"])
def test_resolve_volume_floors_at_zero(raw):
    assert group_rules.resolve_volume(raw) == 0.0


# =====================================================================
# 任务号 <-> 魔术号
# =====================================================================
def test_task_magic_round_trip():
    magic = group_rules.task_magic(42)
    assert magic == Config.GROUP_TASK_MAGIC_BASE + 42
    assert group_rules.task_id_from_magic(magic) == 42


def test_task_magic_never_collides_with_normal_magic():
    assert group_rules.task_magic(1) != Config.DEFAULT_MAGIC_NUMBER


@pytest.mark.parametrize("magic", [None, "", "abc", Config.DEFAULT_MAGIC_NUMBER, 0])
def test_task_id_from_magic_rejects_out_of_range(magic):
    assert group_rules.task_id_from_magic(magic) is None


# =====================================================================
# 成员顺序与有效节点
# =====================================================================
def _group(*members, enabled=True, mode="sync"):
    return {
        "group_id": "grp_1",
        "name": "g1",
        "enabled": enabled,
        "dispatch_mode": mode,
        "members": list(members),
    }


def test_member_ids_sorted_by_sort_order():
    g = _group(
        {"node_id": "nd_c", "sort_order": 2},
        {"node_id": "nd_a", "sort_order": 0},
        {"node_id": "nd_b", "sort_order": 1},
    )
    assert group_rules.member_ids(g) == ["nd_a", "nd_b", "nd_c"]


def test_member_ids_ignores_malformed_entries():
    g = _group({"node_id": "nd_a", "sort_order": 0}, {"sort_order": 1}, "not-a-dict")
    assert group_rules.member_ids(g) == ["nd_a"]


def test_effective_node_ids_requires_enabled_and_online():
    g = _group(
        {"node_id": "nd_on", "sort_order": 0},
        {"node_id": "nd_off", "sort_order": 1},       # 在线但被禁用
        {"node_id": "nd_absent", "sort_order": 2},    # 已启用但不在线
        {"node_id": "nd_gone", "sort_order": 3},      # 节点已被删除
    )
    nodemap = {
        "nd_on": {"node_id": "nd_on", "enabled": True},
        "nd_off": {"node_id": "nd_off", "enabled": False},
        "nd_absent": {"node_id": "nd_absent", "enabled": True},
    }
    online = {"nd_on", "nd_off"}
    assert group_rules.effective_node_ids(g, nodemap, online) == ["nd_on"]


def test_effective_node_ids_preserves_group_order():
    g = _group(
        {"node_id": "nd_b", "sort_order": 0},
        {"node_id": "nd_a", "sort_order": 1},
    )
    nodemap = {"nd_a": {"enabled": True}, "nd_b": {"enabled": True}}
    assert group_rules.effective_node_ids(g, nodemap, {"nd_a", "nd_b"}) == ["nd_b", "nd_a"]


# =====================================================================
# 分组跳过原因
# =====================================================================
def test_group_skip_reason_disabled():
    g = _group({"node_id": "nd_a", "sort_order": 0}, enabled=False)
    assert "已禁用" in group_rules.group_skip_reason(g, ["nd_a"])


def test_group_skip_reason_no_member():
    assert "无成员节点" in group_rules.group_skip_reason(_group(), [])


def test_group_skip_reason_no_effective_node():
    g = _group({"node_id": "nd_a", "sort_order": 0})
    reason = group_rules.group_skip_reason(g, [])
    assert "无有效节点" in reason
    assert "未启用或不在线" in reason


def test_group_skip_reason_none_when_dispatchable():
    g = _group({"node_id": "nd_a", "sort_order": 0})
    assert group_rules.group_skip_reason(g, ["nd_a"]) is None


# =====================================================================
# 组内轮转顺序对齐
# =====================================================================
def test_reconcile_rotation_keeps_existing_order():
    assert group_rules.reconcile_rotation(
        ["nd_b", "nd_c", "nd_a"], ["nd_a", "nd_b", "nd_c"],
    ) == ["nd_b", "nd_c", "nd_a"]


def test_reconcile_rotation_drops_removed_and_appends_new():
    assert group_rules.reconcile_rotation(
        ["nd_b", "nd_gone", "nd_a"], ["nd_a", "nd_b", "nd_new"],
    ) == ["nd_b", "nd_a", "nd_new"]


def test_reconcile_rotation_from_empty_uses_member_order():
    assert group_rules.reconcile_rotation([], ["nd_a", "nd_b"]) == ["nd_a", "nd_b"]


# =====================================================================
# 主任务状态汇总
# =====================================================================
@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "skipped"),                          # 未产生任何下发
        (["sent"], "dispatching"),
        (["pending", "done"], "dispatching"),
        (["done"], "done"),
        (["done", "done"], "done"),
        (["done", "failed"], "partial"),
        (["done", "offline"], "partial"),
        (["failed"], "failed"),
        (["failed", "offline"], "failed"),
        (["skipped"], "skipped"),
        (["skipped", "skipped"], "skipped"),
    ],
)
def test_aggregate_task_status(statuses, expected):
    assert group_rules.aggregate_task_status(statuses) == expected
