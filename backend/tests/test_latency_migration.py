"""验证时延埋点列的迁移：老库加列、新库直接建表。

迁移是 M8 落地最关键的一步——`create_all` 不会改已存在的表，
列加不上就意味着后台看不到任何时延数据（且不报错，只是静默为空）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import _LATENCY_COLUMNS, _migrate_latency_marks


@pytest.fixture
def legacy_db(tmp_path):
    """造一个「迁移前」的老库：两张表都只有基础列，没有埋点列。"""
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE signal_dispatch ("
            "id INTEGER PRIMARY KEY, signal_id VARCHAR(32), node_id VARCHAR(32))"
        ))
        conn.execute(text(
            "CREATE TABLE group_task_dispatch ("
            "id INTEGER PRIMARY KEY, task_id BIGINT, signal_id VARCHAR(32))"
        ))
    return engine


def test_migration_adds_latency_columns(legacy_db):
    with legacy_db.begin() as conn:
        _migrate_latency_marks(conn)

    inspector = inspect(legacy_db)
    for table in ("signal_dispatch", "group_task_dispatch"):
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name, _ddl in _LATENCY_COLUMNS:
            assert name in cols, f"{table} 缺少埋点列 {name}"


def test_migration_is_idempotent(legacy_db):
    """迁移必须能重复跑——服务重启会再跑一次，不能因为列已存在就报错。"""
    with legacy_db.begin() as conn:
        _migrate_latency_marks(conn)
        _migrate_latency_marks(conn)  # 第二次：列已存在，应静默跳过


def test_migration_skips_missing_table(tmp_path):
    """表不存在时直接返回，不报错。"""
    db_path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _migrate_latency_marks(conn)
