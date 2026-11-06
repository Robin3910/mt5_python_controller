"""策略运行期调参：config_version、上限下限校验、runtime_hints。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.strategy_templates import TEMPLATE_1_ID

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    from tests.test_helpers import drop_test_db, reset_test_db

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


from tests.test_helpers import auth_headers  # noqa: E402


def _rule(rule_type: int = 1, **over) -> dict:
    rule = {
        "type": rule_type, "status": 1, "action": "all",
        "point": 100, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 5, "batch_enabled": False, "batch_count": 0,
        "total_lot_limit": 5,
    }
    rule.update(over)
    return rule


# ----------------------------------------------------------------------
# config_version
# ----------------------------------------------------------------------
def test_config_version_increments_on_save(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "版本号策略", "symbol": "XAUUSD",
            "rules": [_rule()],
        },
        headers=h,
    ).json()
    assert created["config_version"] == 1

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=6)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["config_version"] == 2

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"name": "改个名"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["config_version"] == 3


def test_snapshot_carries_config_version(client):
    """快照里带 config_version，节点侧用它做版本门。"""
    from app import group_rules

    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "config_version": 7,
        "rules": [_rule()],
    })
    assert snap["config_version"] == 7


def test_snapshot_config_version_defaults_to_zero():
    from app import group_rules

    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_rule()],
    })
    assert snap["config_version"] == 0


# ----------------------------------------------------------------------
# 档位自动补齐
# ----------------------------------------------------------------------
def _batched_rule(levels: list[dict], limit: int = 5) -> dict:
    return _rule(batch_enabled=True, batch_count=len(levels),
                 total_lot_limit=limit, batch_levels=levels)


def test_snapshot_pads_levels_to_cover_limit():
    """档位只覆盖 1~3、上限 5 → 快照里补到 5，参数沿用最后一档。"""
    from app import group_rules

    levels = [
        {"pos_from": 1, "pos_to": 3, "calc_type": "point", "point": 100,
         "lot_times": 1.1, "extra_lot": 0},
        {"pos_from": 4, "pos_to": 4, "calc_type": "point", "point": 200,
         "lot_times": 1.2, "extra_lot": 0},
    ]
    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_batched_rule(levels, limit=6)],
    })
    out = snap["rules"][0]["batch_levels"]
    covered = max(int(lv["pos_to"]) for lv in out)
    assert covered >= 6, "档位必须覆盖到上限"
    # 补出来的档沿用最后一档的参数
    last = out[-1]
    assert last["point"] == 200
    assert last["lot_times"] == 1.2


def test_snapshot_pads_gap_between_levels():
    """档位中间有洞 → 补齐后连续无洞。"""
    from app import group_rules

    levels = [
        {"pos_from": 1, "pos_to": 2, "calc_type": "point", "point": 100,
         "lot_times": 1.1, "extra_lot": 0},
        {"pos_from": 5, "pos_to": 6, "calc_type": "point", "point": 300,
         "lot_times": 1.3, "extra_lot": 0},
    ]
    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_batched_rule(levels, limit=6)],
    })
    out = snap["rules"][0]["batch_levels"]
    # 从 1 开始连续覆盖到 6
    cursor = 1
    for lv in sorted(out, key=lambda x: int(x["pos_from"])):
        assert int(lv["pos_from"]) <= cursor, f"档位 {lv} 之前有洞"
        cursor = max(cursor, int(lv["pos_to"]) + 1)
    assert cursor >= 7


def test_snapshot_does_not_pad_price_level():
    """末档是指定价时不补档：复制它会凭空多挂几张同价位的指定价单。

    补档是把最后一档的参数复制到后面的空位；指定价档位挂的是一张
    **指定价的单**，复制它等于替用户多挂了他没配的单。
    """
    from app import group_rules

    levels = [
        {"pos_from": 1, "pos_to": 1, "calc_type": "point", "point": 100,
         "lot_times": 1.1, "extra_lot": 0},
        {"pos_from": 2, "pos_to": 2, "calc_type": "price", "point": 100,
         "price": 4067.0, "price_tp": 4072.0, "lot_times": 1.2, "extra_lot": 0},
    ]
    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_batched_rule(levels, limit=5)],
    })
    out = snap["rules"][0]["batch_levels"]
    price_levels = [lv for lv in out if lv.get("calc_type") == "price"]
    assert len(price_levels) == 1, "指定价档位只能有一档，不能被补档复制"
    assert (price_levels[0]["pos_from"], price_levels[0]["pos_to"]) == (2, 2)


def test_snapshot_does_not_pad_when_limit_unlimited():
    """上限 0（不限）→ 不往上补档。"""
    from app import group_rules

    levels = [
        {"pos_from": 1, "pos_to": 3, "calc_type": "point", "point": 100,
         "lot_times": 1.1, "extra_lot": 0},
    ]
    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_batched_rule(levels, limit=0)],
    })
    assert len(snap["rules"][0]["batch_levels"]) == 1


def test_snapshot_does_not_mutate_source_rule():
    """补档不能污染原始规则对象（快照是另一次下发用的）。"""
    from app import group_rules

    levels = [
        {"pos_from": 1, "pos_to": 3, "calc_type": "point", "point": 100,
         "lot_times": 1.1, "extra_lot": 0},
    ]
    rule = _batched_rule(levels, limit=6)
    group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [rule],
    })
    assert len(rule["batch_levels"]) == 1, "原始规则不该被补档改写"


# ----------------------------------------------------------------------
# 指定价加仓：一次生效，摘除
# ----------------------------------------------------------------------
def _price_level(pos_from, pos_to, *, price=4350.0, tp=4400.0, sl=4300.0) -> dict:
    return {
        "pos_from": pos_from, "pos_to": pos_to, "calc_type": "price",
        "point": 100, "price": price, "price_tp": tp, "price_sl": sl,
        "lot_times": 1.0, "extra_lot": 0.0,
    }


def _point_level(pos_from, pos_to, *, point=100) -> dict:
    return {
        "pos_from": pos_from, "pos_to": pos_to, "calc_type": "point",
        "point": point, "price": 0.0, "price_tp": 0.0, "price_sl": 0.0,
        "lot_times": 1.0, "extra_lot": 0.0,
    }


def test_strip_price_levels_removes_level_and_shifts_up():
    """删整档 + 后续档位上移：1~1 / 2~2(指定价) / 3~3 → 1~1 / 2~2，上限 3→2。"""
    from app.strategy_templates import strip_price_levels

    rules = [_rule(
        batch_enabled=True, batch_count=3, total_lot_limit=3,
        batch_levels=[
            _point_level(1, 1),
            _price_level(2, 2),
            _point_level(3, 3),
        ],
    )]
    out, changed = strip_price_levels(rules)
    assert changed is True
    levels = out[0]["batch_levels"]
    assert len(levels) == 2
    assert all(lv["calc_type"] != "price" for lv in levels)
    # 上移后首尾衔接、不留空洞，区间长度保持不变
    assert (levels[0]["pos_from"], levels[0]["pos_to"]) == (1, 1)
    assert (levels[1]["pos_from"], levels[1]["pos_to"]) == (2, 2)
    assert out[0]["batch_count"] == 2
    assert out[0]["total_lot_limit"] == 2


def test_strip_price_levels_keeps_level_lengths():
    """其余档位保持各自长度：删中间档后 3~4 变成 2~3。"""
    from app.strategy_templates import strip_price_levels

    rules = [_rule(
        batch_enabled=True, batch_count=3, total_lot_limit=4,
        batch_levels=[
            _point_level(1, 1),
            _price_level(2, 2),
            _point_level(3, 4),
        ],
    )]
    out, changed = strip_price_levels(rules)
    assert changed is True
    levels = out[0]["batch_levels"]
    assert (levels[0]["pos_from"], levels[0]["pos_to"]) == (1, 1)
    assert (levels[1]["pos_from"], levels[1]["pos_to"]) == (2, 3)
    assert out[0]["total_lot_limit"] == 3


def test_strip_price_levels_also_strips_legacy_price_level():
    """老口径指定价档位（没填止盈价）也一并摘掉。"""
    from app.strategy_templates import strip_price_levels

    rules = [_rule(
        batch_enabled=True, batch_count=2, total_lot_limit=2,
        batch_levels=[
            _point_level(1, 1),
            {"pos_from": 2, "pos_to": 2, "calc_type": "price", "point": 100,
             "price": 4350.0, "price_tp": 0.0, "price_sl": 0.0,
             "lot_times": 1.0, "extra_lot": 0.0},
        ],
    )]
    out, changed = strip_price_levels(rules)
    assert changed is True
    assert len(out[0]["batch_levels"]) == 1
    assert out[0]["batch_levels"][0]["calc_type"] == "point"


def test_strip_price_levels_disables_batch_when_all_removed():
    """档位被摘光 → 关掉分批，否则节点侧整条规则会静默哑掉。"""
    from app.strategy_templates import strip_price_levels

    rules = [_rule(
        batch_enabled=True, batch_count=1, total_lot_limit=1,
        batch_levels=[_price_level(1, 1)],
    )]
    out, changed = strip_price_levels(rules)
    assert changed is True
    assert out[0]["batch_levels"] == []
    assert out[0]["batch_enabled"] is False
    assert out[0]["batch_count"] == 0
    assert out[0]["total_lot_limit"] == 0


def test_strip_price_levels_no_change_without_price_level():
    """没有指定价档位 → 不算改动（幂等的前提）。"""
    from app.strategy_templates import strip_price_levels

    rules = [_rule(
        batch_enabled=True, batch_count=2, total_lot_limit=2,
        batch_levels=[_point_level(1, 1), _point_level(2, 2)],
    )]
    out, changed = strip_price_levels(rules)
    assert changed is False
    assert out == rules


def test_strip_price_levels_does_not_mutate_input():
    """入参不被修改（配置快照与已下发快照共用同一份字典时要安全）。"""
    from app.strategy_templates import strip_price_levels

    levels = [_point_level(1, 1), _price_level(2, 2), _point_level(3, 3)]
    rules = [_rule(batch_enabled=True, batch_count=3, total_lot_limit=3,
                   batch_levels=levels)]
    strip_price_levels(rules)
    assert len(rules[0]["batch_levels"]) == 3
    assert rules[0]["batch_levels"][1]["calc_type"] == "price"
    assert rules[0]["total_lot_limit"] == 3


def test_strip_price_levels_ignores_non_batch_rules():
    """网格 / 趋势规则没有分批档位 → 不受影响。"""
    from app.strategy_templates import strip_price_levels

    rules = [{
        "type": 4, "status": 1, "action": "all", "price_lower": 2300,
        "price_upper": 2400, "grid_count": 10,
    }]
    out, changed = strip_price_levels(rules)
    assert changed is False
    assert out == rules


# ----------------------------------------------------------------------
# runtime_hints
# ----------------------------------------------------------------------
def test_get_strategy_returns_runtime_hints(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "提示策略", "symbol": "XAUUSD",
            "rules": [_rule()],
        },
        headers=h,
    ).json()
    r = client.get(f"/api/strategies/{created['strategy_id']}", headers=h)
    assert r.status_code == 200, r.text
    hints = r.json()["runtime_hints"]
    assert hints is not None
    assert hints["running_task_count"] == 0
    assert hints["max_added_by_rule"] == {}
    assert hints["manual_fired"] == []
    assert hints["add_progress"] == {}, "没有在跑任务时进度为空"


# ----------------------------------------------------------------------
# 上限字段：配置无条件热推，保存路径上不再拦截
# ----------------------------------------------------------------------
def test_limit_below_added_is_accepted(client, monkeypatch):
    """已开 3 笔时把上限改成 2 → 仍然 200 保存。

    上限低于已开笔数不再阻止保存：字段值一律采纳并热推到节点，
    「还加不加」由节点自己按已开笔数决定。
    """
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "缩容策略", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=2)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    # 新值真的落库了（不是保留旧值）
    assert r.json()["rules"][0]["total_lot_limit"] == 2
    assert r.json()["config_version"] == 2


def test_limit_equal_to_added_is_allowed(client, monkeypatch):
    """缩容到恰好等于已开笔数 → 允许。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "刚好缩容", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=3)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_expansion_is_allowed(client, monkeypatch):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "扩容", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=3)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=10)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_check_is_per_rule(client, monkeypatch):
    """按规则独立生效：逆势超限照常保存（节点自己决定不再加），顺势也不受影响。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "分规则", "symbol": "XAUUSD",
            "rules": [_rule(1, total_lot_limit=5), _rule(2, total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3, "2": 0},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    # 逆势改成 2（低于已开 3）→ 照常保存
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(1, total_lot_limit=2), _rule(2, total_lot_limit=5)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["rules"][0]["total_lot_limit"] == 2

    # 只改顺势 → 放行
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(1, total_lot_limit=5), _rule(2, total_lot_limit=1)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_zero_is_unlimited_not_rejected(client, monkeypatch):
    """0 = 不限制，不参与下限比较。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "不限", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=0)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_no_running_task_means_no_limit_check(client):
    """没有在跑任务时，上限随便改。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "空闲", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=1)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
