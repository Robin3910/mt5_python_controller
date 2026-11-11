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
# 注入占位 AccessKey：短信服务默认启用后，「是否可用」的判断会看密钥是否就位。
# 用例真正发码时走 FakeSmsProvider，不会真的调阿里云。
os.environ.setdefault("ALIYUN_ACCESS_KEY_ID", "test-access-key-id")
os.environ.setdefault("ALIYUN_ACCESS_KEY_SECRET", "test-access-key-secret")
# 关掉财经日历定时抓取：抓取走外网（nfs.faireconomy.media），在测试里既拖慢
# 用例又会被对方限流（429）。判定层本身不依赖 worker，照常可测。
os.environ.setdefault("ECONOMIC_CALENDAR_ENABLED", "false")
