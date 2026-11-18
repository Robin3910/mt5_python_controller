"""补列迁移：user_order_presets（开仓参数预设表）。

直接测迁移函数（_migrate_user_order_presets），用**独立的临时库**：
不走共享的 _test_api.db —— 那个文件被全套用例共用，在别的用例之间做
「删表改结构」会互相干扰。

覆盖三件事：
1. 残缺表（只有主键 + user_id）能被补齐成完整列集；
2. 唯一索引 (user_id, symbol, strategy_type) 建得上（SQLite 不支持
   ALTER TABLE ADD CONSTRAINT，只能走 CREATE UNIQUE INDEX）；
3. 重复跑迁移不报错（init_db 每次启动都会调它）。
"""
import pathlib
import sqlite3

from sqlalchemy import create_engine, inspect

from app.db import _migrate_user_order_presets


def _make_legacy_db(path: pathlib.Path) -> None:
    """造一个「老库」：预设表只有主键和 user_id，缺其余所有列。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE user_order_presets ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()


def _columns(path: pathlib.Path) -> set[str]:
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.connect() as conn:
            return {c["name"] for c in inspect(conn).get_columns("user_order_presets")}
    finally:
        eng.dispose()


def _unique_indexes(path: pathlib.Path) -> list[tuple[str, ...]]:
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.connect() as conn:
            insp = inspect(conn)
            out: list[tuple[str, ...]] = []
            for uc in insp.get_unique_constraints("user_order_presets"):
                names = tuple(uc.get("column_names") or ())
                if names:
                    out.append(names)
            for idx in insp.get_indexes("user_order_presets"):
                if idx.get("unique"):
                    names = tuple(idx.get("column_names") or ())
                    if names:
                        out.append(names)
            return out
    finally:
        eng.dispose()


EXPECTED_COLUMNS = {
    "symbol",
    "strategy_type",
    "risk_amount",
    "rr_ratio",
    "base_ratio",
    "breakeven_times",
    "grid_mode",
    "max_allow_loss",
    "trailing_up",
}


def test_migration_adds_missing_columns(tmp_path):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)
    assert not (EXPECTED_COLUMNS & _columns(path))

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_order_presets(conn)
    finally:
        eng.dispose()

    assert EXPECTED_COLUMNS <= _columns(path)


def test_migration_creates_unique_index(tmp_path):
    """唯一索引 (user_id, symbol, strategy_type) 必须真的建得上。

    这是「一人一份通用模板」约束生效的前提 —— 没有它，同一用户可以
    存出多条 symbol='*' 的预设，解析时哪条生效就成了随机的。
    """
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_order_presets(conn)
    finally:
        eng.dispose()

    assert ("user_id", "symbol", "strategy_type") in _unique_indexes(path)


def test_migration_is_idempotent(tmp_path):
    """重复跑迁移不应报错 —— 启动时 init_db 每次都会调用它。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        for _ in range(3):
            with eng.begin() as conn:
                _migrate_user_order_presets(conn)
    finally:
        eng.dispose()

    assert EXPECTED_COLUMNS <= _columns(path)
    assert ("user_id", "symbol", "strategy_type") in _unique_indexes(path)


def test_migration_skipped_when_table_missing(tmp_path):
    """预设表还没建时（比如首次建库）迁移应直接跳过，不报错。"""
    path = tmp_path / "empty.db"
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_order_presets(conn)
    finally:
        eng.dispose()
