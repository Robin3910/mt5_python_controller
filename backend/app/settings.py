"""运行期 / 基础设施配置，从环境变量(.env)加载。

与 `config.py` 分离：让纯逻辑（解析器、分发规则）能在不加载配置/Web 栈的情况下导入。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # —— 基础设施 ——
    redis_url: str = "redis://localhost:6379/0"
    # 本地开发默认用 SQLite，免装 MySQL 即可运行；
    # 生产通过环境变量设置 MYSQL_DSN=mysql+asyncmy://user:pwd@host:3306/db（见 docker .env）。
    mysql_dsn: str = "sqlite+aiosqlite:///./mt5hub.db"

    # —— Webhook 鉴权（与参考仓库一致）——
    enable_auth: bool = False           # 是否校验 Webhook token
    auth_token: str = ""                # Webhook 共享 token
    enable_ip_whitelist: bool = False   # 是否启用 IP 白名单
    whitelisted_ips: str = ""           # 逗号分隔的 IP 列表

    # —— 后台管理登录 ——
    jwt_secret: str = "change_me"
    jwt_expire_minutes: int = 720
    admin_user: str = "admin"
    admin_password: str = "admin123"

    # —— WebSocket / 各类时间间隔（秒）——
    heartbeat_interval: int = 30        # 下发给节点的心跳间隔
    online_ttl: int = 90                # 在线标记的过期时间（超过即判离线）
    account_report_interval: int = 5    # 期望的账户上报间隔
    auth_first_packet_timeout: int = 5  # 节点首包鉴权超时

    # —— 分发 ——
    position_scope: str = "symbol"      # 持仓判定范围：symbol（按品种）/ account（按账户）
    dispatch_mode: str = "sync"         # 分发模式：sync（全员同步）/ poll（轮询领取）
    poll_ack_timeout: int = 15          # 轮询模式单节点回报超时
    poll_max_retry: int = 3             # 轮询模式单节点最大重试次数
    dedup_window: int = 5               # 信号去重窗口（秒）

    log_level: str = "INFO"

    @property
    def whitelist(self) -> list[str]:
        """把逗号分隔的白名单字符串切成列表。"""
        return [ip.strip() for ip in self.whitelisted_ips.split(",") if ip.strip()]


@lru_cache
def get_settings() -> Settings:
    """单例：进程内只解析一次环境变量。"""
    return Settings()


settings = get_settings()
