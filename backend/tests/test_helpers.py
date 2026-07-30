"""Shared helpers for API / webhook integration tests."""
import sqlite3
import time

DEFAULT_TEST_FILTERS = {
    "EURUSD": {
        "enabled": True,
        "allow_buy": True,
        "allow_sell": True,
        "dispatch_mode": "sync",
        "position_scope": "symbol",
        "default_action": "pass",
        "intervals": [],
    },
    "XAUUSD": {
        "enabled": True,
        "allow_buy": True,
        "allow_sell": True,
        "dispatch_mode": "sync",
        "position_scope": "symbol",
        "default_action": "pass",
        "intervals": [],
    },
    "GBPUSD": {
        "enabled": True,
        "allow_buy": True,
        "allow_sell": True,
        "dispatch_mode": "sync",
        "position_scope": "symbol",
        "default_action": "pass",
        "intervals": [],
    },
}


def _unlink_db(db_path, *, attempts: int) -> bool:
    """尽力删除库文件与 WAL 边文件；全部删净返回 True。"""
    targets = [db_path, *(db_path.with_name(db_path.name + s) for s in ("-wal", "-shm"))]
    for _ in range(attempts):
        blocked = False
        for target in targets:
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except PermissionError:
                blocked = True
        if not blocked:
            return True
        time.sleep(0.05)
    return False


def drop_test_db(db_path) -> None:
    """用例收尾：尽力删除测试库，删不掉也不算失败。

    下一个用例开始前会用 reset_test_db 兜底，所以这里不必较真。
    """
    _unlink_db(db_path, attempts=4)


def reset_test_db(db_path) -> None:
    """用例开始前：确保测试库是干净的。

    Windows 上上一个用例的文件句柄释放会滞后，删不掉时退一步用 SQL 清空所有表——
    只要逻辑上是空的，用例就不会读到残留数据。这一步不能省：残留数据会以
    「database is locked」「数量不对」之类的面目连锁失败，极难定位。
    """
    if _unlink_db(db_path, attempts=20):
        return
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                conn.execute(f'DELETE FROM "{table}"')
            conn.commit()
    except sqlite3.Error as e:
        raise AssertionError(f"测试库既删不掉也清不空：{db_path}") from e


def auth_headers(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def seed_default_filters(client) -> dict:
    """写入测试用全局品种配置，使 webhook 信号可被接受。"""
    h = auth_headers(client)
    r = client.put("/api/config/filters", json=DEFAULT_TEST_FILTERS, headers=h)
    assert r.status_code == 200, r.text
    return h
