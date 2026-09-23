"""Test environment setup — must run before the app package is imported."""
import os
import pathlib

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"

os.environ.setdefault("MYSQL_DSN", f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}")
os.environ.setdefault("JWT_SECRET", "test_secret")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
# 产品默认开启 Webhook token 校验；用例仍按「未开启」编写，由 _enable_auth 显式打开。
os.environ.setdefault("ENABLE_AUTH", "false")
