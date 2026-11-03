"""补列迁移：users 的个人中心列（email / leader_name / leader_intro）。

直接测迁移函数（_migrate_users_profile_columns），用**独立的临时库**：
不走共享的 _test_api.db —— 那个文件被全套用例共用，在别的用例之间做
「删表改结构」会互相干扰。
"""
import pathlib
import sqlite3

from sqlalchemy import create_engine, inspect, text

from app.db import _migrate_users_profile_columns


def _make_legacy_db(path: pathlib.Path) -> None:
    """造一个「老库」：users 表有基础列，但没有那三列。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " username VARCHAR(64) NOT NULL,"
        " display_name VARCHAR(64),"
        " password_hash VARCHAR(64) NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash) "
        "VALUES ('legacy', '老用户', 'deadbeef')"
    )
    conn.commit()
    conn.close()


def _columns(path: pathlib.Path) -> set[str]:
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.connect() as conn:
            return {c["name"] for c in inspect(conn).get_columns("users")}
    finally:
        eng.dispose()


def _row(path: pathlib.Path) -> tuple:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT username, display_name, password_hash, email, leader_name, leader_intro "
            "FROM users WHERE username = 'legacy'"
        ).fetchone()
    finally:
        conn.close()


def test_migration_adds_profile_columns(tmp_path):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)
    assert "email" not in _columns(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_users_profile_columns(conn)
    finally:
        eng.dispose()

    assert {"email", "leader_name", "leader_intro"} <= _columns(path)


def test_migration_preserves_existing_rows_and_leaves_them_null(tmp_path):
    """补列不能动老数据；新列对存量用户一律 NULL（零行为变化）。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_users_profile_columns(conn)
    finally:
        eng.dispose()

    row = _row(path)
    assert row[0] == "legacy"
    assert row[1] == "老用户"
    assert row[2] == "deadbeef"
    assert row[3] is None  # email
    assert row[4] is None  # leader_name
    assert row[5] is None  # leader_intro


def test_migration_is_idempotent(tmp_path):
    """重复跑迁移不应报错 —— 启动时 init_db 每次都会调用它。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        for _ in range(3):
            with eng.begin() as conn:
                _migrate_users_profile_columns(conn)
    finally:
        eng.dispose()

    assert {"email", "leader_name", "leader_intro"} <= _columns(path)
    assert _row(path)[0] == "legacy"


def test_migration_skipped_when_users_table_missing(tmp_path):
    """users 表还没建时（比如首次建库）迁移应直接跳过，不报错。"""
    path = tmp_path / "empty.db"
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_users_profile_columns(conn)
    finally:
        eng.dispose()


def test_migration_keeps_existing_values_on_second_run(tmp_path):
    """已有数据的列再次迁移不被覆盖（不会被重置成 NULL）。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_users_profile_columns(conn)
        with eng.begin() as conn:
            conn.execute(
                text("UPDATE users SET email = :e, leader_name = :n WHERE username = 'legacy'"),
                {"e": "a@b.com", "n": "领航员"},
            )
        with eng.begin() as conn:
            _migrate_users_profile_columns(conn)
    finally:
        eng.dispose()

    row = _row(path)
    assert row[3] == "a@b.com"
    assert row[4] == "领航员"
