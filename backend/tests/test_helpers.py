"""Shared helpers for API / webhook integration tests."""

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
