"""`user_reviews` 表的结构迁移：证件三列 → 姓名 / 邮箱 / 手机号。

2026-11-05 实名材料简化，去掉了 `cert_type` / `cert_no_masked` /
`cert_no_hash` 三列，改为 `phone`（脱敏快照）。这张表已经在生产库里存在
（初版带证件三列），所以迁移必须能处理「表已存在」的情况。

直接测迁移函数（`_migrate_user_reviews`），用**独立的临时库**：
不走共享的 `_test_api.db` —— 那个文件被全套用例共用，在别的用例之间做
「删表改结构」会互相干扰。
"""
import sqlite3

from sqlalchemy import create_engine, inspect, text

from app.db import _migrate_user_reviews


def _make_legacy_db(path) -> None:
    """造一个「老库」：user_reviews 表带证件三列，且 cert_no_hash 上有索引。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE user_reviews ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " status VARCHAR(16) NOT NULL DEFAULT 'pending',"
        " real_name VARCHAR(64),"
        " cert_type VARCHAR(32),"
        " cert_no_masked VARCHAR(32),"
        " cert_no_hash VARCHAR(64),"
        " email VARCHAR(128),"
        " kyc_submitted BOOLEAN NOT NULL DEFAULT 0,"
        " submit_time TIMESTAMP,"
        " audited_at TIMESTAMP,"
        " audited_by VARCHAR(64),"
        " reject_reason VARCHAR(256),"
        " created_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE INDEX ix_user_reviews_cert_no_hash ON user_reviews (cert_no_hash)"
    )
    conn.execute(
        "CREATE INDEX ix_user_reviews_user_id ON user_reviews (user_id)"
    )
    conn.execute(
        "INSERT INTO user_reviews"
        " (user_id, real_name, cert_type, cert_no_masked, cert_no_hash, email)"
        " VALUES (1, '徐明远', '身份证', '4403**********1234', 'abc123', 'a@b.com')"
    )
    conn.commit()
    conn.close()


def _columns(path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(user_reviews)")}
    finally:
        conn.close()


def _indexes(path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA index_list(user_reviews)")}
    finally:
        conn.close()


def test_migration_drops_legacy_cert_columns_and_adds_phone(tmp_path):
    """证件三列删掉，phone 列补上，其余列原样保留。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_reviews(conn)
    finally:
        eng.dispose()

    cols = _columns(path)
    assert "cert_type" not in cols
    assert "cert_no_masked" not in cols
    assert "cert_no_hash" not in cols
    assert "phone" in cols
    # 无关列不能被动到
    assert {"user_id", "status", "real_name", "email", "kyc_submitted"} <= cols


def test_migration_drops_index_before_dropping_its_column(tmp_path):
    """SQLite 删列**不会**连带删索引 —— 必须先删索引，否则整条迁移报错。

    回归点：曾经直接 `DROP COLUMN cert_no_hash` 而没先 `DROP INDEX`，
    SQLite 抛 `error in index ix_user_reviews_cert_no_hash after drop
    column: no such column: cert_no_hash`。
    """
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_reviews(conn)
    finally:
        eng.dispose()

    idxs = _indexes(path)
    assert "ix_user_reviews_cert_no_hash" not in idxs
    # 无关的索引必须留着
    assert "ix_user_reviews_user_id" in idxs


def test_migration_preserves_existing_rows(tmp_path):
    """老数据不能丢：姓名与邮箱仍在，手机号快照对老行为 NULL。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_reviews(conn)
    finally:
        eng.dispose()

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT user_id, real_name, email, phone FROM user_reviews"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 1
    assert row[1] == "徐明远"
    assert row[2] == "a@b.com"
    assert row[3] is None  # 老数据没有手机号快照


def test_migration_is_idempotent(tmp_path):
    """迁移要能重复跑 —— 服务重启会再跑一次，不能因为列已不存在就报错。"""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        for _ in range(3):
            with eng.begin() as conn:
                _migrate_user_reviews(conn)
    finally:
        eng.dispose()

    cols = _columns(path)
    assert "phone" in cols
    assert not any(c.startswith("cert") for c in cols)


def test_migration_creates_table_when_missing(tmp_path):
    """表不存在时按模型建表（首次安装路径）。"""
    path = tmp_path / "empty.db"
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            _migrate_user_reviews(conn)
    finally:
        eng.dispose()

    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("user_reviews")}
    assert {"real_name", "email", "phone", "kyc_submitted"} <= cols
    assert not any(c.startswith("cert") for c in cols)
