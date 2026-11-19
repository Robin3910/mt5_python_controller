"""Test environment setup — must run before the app package is imported."""
import os
import pathlib

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


def _acquire_test_db_lock(db_path: pathlib.Path):
    """给共享测试库加一把进程锁，挡住并发跑的 pytest。

    全部 API 用例共用这一个 sqlite 文件，所以**并发跑两批 pytest 会互相清库**：
    两个进程各自 reset/drop 同一个文件，被清空的那批用例会读到别人的数据，
    表现为「随机一批用例莫名其妙失败」，很容易被误判成代码回归。
    拿不到锁就明确报错，而不是让人去猜。
    """
    lock_path = db_path.with_suffix(".lock")
    handle = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        handle.close()
        # 英文报错：Windows 控制台默认 codepage 装不下中文，CI 日志里会是乱码，
        # 反而看不出在说什么。中文解释留在 docstring 里。
        raise RuntimeError(
            f"Another pytest process is already using test DB {db_path.name}. "
            "Wait for it to finish before starting a new run "
            "(all API tests share one sqlite file; concurrent runs wipe each other's data)."
        ) from e
    return handle


_lock_fd = _acquire_test_db_lock(_TEST_DB)

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
