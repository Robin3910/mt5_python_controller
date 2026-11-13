"""SQLAlchemy ORM 模型（生产用 MySQL，本地开发用 SQLite）。

承担“持久化”职责：节点账本、后台用户与角色权限（sys_*）、操作审计、信号历史、
分发明细，以及 strategy 信号的分组账本与主任务链路（node_group* / group_signal_task*）。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# 自增主键类型：MySQL 用 BIGINT AUTO_INCREMENT；SQLite 只有声明为 INTEGER 的主键
# 才是 rowid 别名（BIGINT 主键不会自增，插入会撞 NOT NULL），故按方言取变体。
AutoPK = BigInteger().with_variant(Integer, "sqlite")

# 毫秒精度时间戳：MySQL 的 DATETIME 默认不带小数秒（DATETIME(0)），
# 时延埋点会被整秒截断（发包与回报同秒就记成 0ms），跨机往返时延算不准。
# MySQL 用 DATETIME(3) 存毫秒；SQLite 的 DateTime 本身就存微秒，不用变体。
MsDateTime = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")


class Base(DeclarativeBase):
    pass


class Node(Base):
    """节点账本（权威数据源；Redis 仅作缓存）。

    自 v0.2 起鉴权令牌改为全局共享（见 SystemSetting），节点身份用 mt5_login 唯一标识。
    """
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lot_mode: Mapped[str] = mapped_column(String(16), default="global")
    lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    follow_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    follow_poll: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_order: Mapped[int] = mapped_column(Integer, default=0)
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 节点级过滤
    risk_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 账户级风控
    # 旧版按节点存的趋势参数；权威已迁到 system_setting.trend_config，此列仅作迁移种子
    trend_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # mt5_login 自 v0.2 起作为节点的业务唯一键（不可为空、全局唯一）
    mt5_login: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    mt5_server: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 节点鉴权首包上报的客户端版本；旧版本节点不带该字段，留空表示未知
    client_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_version_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 数据归属：管理员分配给哪个用户；空 = 管理员名下（含自动注册的新节点）
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    approval_status: Mapped[str] = mapped_column(String(16), default="approved", server_default="approved")
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    admin_enable_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 主节点：按 owner_user_id 维度每用户唯一（NULL 视为管理员名下用户的「无主节点」）。
    # 同一所有者下只能有一个主节点；所有权变更时自动清掉，避免跨用户遗留。
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NodeCredential(Base):
    """专属节点凭证；独立于旧版 nodes.token_hash，防止触发旧结构清理。"""
    __tablename__ = "node_credentials"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    legacy_allowed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SystemSetting(Base):
    """系统级 key/value 配置（持久化）。

    当前用途：
    - `node_token` — 所有节点共享的接入令牌（明文，便于管理员复制到各节点 .env）
    - `webhook_auth_enabled` — Webhook 是否校验 AUTH_TOKEN（"1" / "0"；未写入时看 ENABLE_AUTH）
    - `trend_config` — 趋势面板全局参数（JSON 文本）
    - `client_release` — 当前发布的客户端版本指针（JSON 文本，含回滚用的 previous）
    """
    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ClientVersion(Base):
    """客户端安装包版本清单。

    包体本身落磁盘（settings.client_package_dir），此表只存元数据与校验和；
    「当前发布哪个版本」不在这里，而是 system_setting.client_release 指针。
    """
    __tablename__ = "client_version"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """后台用户：超级管理员与普通用户（权威数据源）。

    权限以 sys_user_role 为准；`role` 是 RBAC 之前的旧字段，只在首次升级时
    用来识别谁是管理员，之后不再读取。
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(64))  # SHA256 十六进制
    role: Mapped[str] = mapped_column(String(16), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(32), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手机号：手机号注册 / 验证码登录的唯一凭据。存归一化后的 E.164（+86xxxxxxxxxxx）。
    # 存量用户为 NULL（MySQL / SQLite 的唯一索引都允许多个 NULL），因此加列零影响。
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True, index=True)
    # 是否已通过短信验证：注册路径为 True；管理员手工绑定手机号时为 False
    # （手工绑定的用户同样可用验证码登录，登录不要求 phone_verified）。
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # —— 个人中心自助维护字段（后加列，存量用户一律 NULL）——
    # email：联系邮箱，**不参与任何登录流程**（登录凭据是用户名+密码 / 手机号验证码）。
    #   不设唯一约束 —— 邮箱不作为身份标识，允许重复。
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # leader_name：带单员对外展示名。为 NULL 时前端回退显示 display_name，
    #   所以不强制填 —— 不填也不会让带单广场显示不出名字。
    leader_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # leader_intro：带单员介绍（自由文本）。目前只在个人中心自己可见，
    #   带单广场暂不公开展示（公开页展示用户自由文本需要内容审核）。
    leader_intro: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 会话版本：禁用 / 改密 / 重置密码时 +1，签发时写进 JWT 的 ver，旧 token 随即失效
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # —— 暂停接单（两个市场各自独立，互不影响）——
    # follow_paused：暂停【带单】市场（tpl_2 趋势 / tpl_3 网格）对外接单。
    #   语义 = 新客户无法加入跟单分组 + 之后的新信号只发主节点；
    #   已有跟单关系与在跑任务**不动**，等跟单员自行移出。
    # ai_follow_paused：暂停【AI 交易】市场对外接单及 tpl_1 分组所有节点的新开仓。
    #   已有关系与在跑任务保留，CLOSE / 平仓仍可执行。
    # 两列分开而不是合成一个：一个带单员可能同时做普通带单与 AI 机器人，
    # 合并后无法表达「只停一边」。
    follow_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    follow_paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_follow_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_follow_paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class EconomicEvent(Base):
    """财经日历事件（**全局共享**，全站一份，所有用户看到同一批事件）。

    数据源：ForexFactory 公开 JSON（`ff_calendar_thisweek.json`），无需 API key。

    与 `user_news_settings` 的分工：
      - 本表 = **公共财经数据**（谁都能看，管理员也只能看不能改）；
      - 用户表 = **各自的开关与参数**（拦截窗口、关注币种、手动急停）。

    **时间一律存 UTC epoch 整数**（`event_time`），不存时区字符串 ——
    多用户 / 多节点 / 多时区下，裸时间字符串一改服务器时区就整体错位。
    展示层再转 UTC+8（北京时间）。
    """
    __tablename__ = "economic_event"

    # sha1(title|event_time|country) 截断：重复抓取天然幂等（同一事件覆盖写）
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 事件发生时刻（UTC epoch 秒）—— 唯一权威时间
    event_time: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(160))
    # 币种码（USD / EUR / ...）；"All" 表示全球性事件（对所有币种都生效）
    country: Mapped[str] = mapped_column(String(8))
    # 归一化后的小写：high / medium / low / holiday
    impact: Mapped[str] = mapped_column(String(8), index=True)
    forecast: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(24), default="forexfactory")
    # 最近一次抓取到该条的时间（用于「数据新鲜度」展示与陈旧提示）
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class UserNewsSettings(Base):
    """用户的财经日历黑名单设置（每人一行）。

    语义：
      - `enabled`：新闻黑名单总开关（默认开）。关掉后完全不影响开单。
      - `before_min` / `after_min`：事件前后多少分钟禁止**新开仓**（默认 30/30）。
      - `min_impact`：最低影响等级（默认 "high"，即只拦高影响）。
      - `currencies_json`：关注币种（默认 ["USD"]）。黄金 XAUUSD 归 USD。
      - `symbols_json`：品种级覆盖（默认 []，空 = 按币种推导）。
      - `manual_paused`：**手动停止做单**急停开关。与新闻黑名单是两层 ——
        手动急停随时可拉，不受数据源故障、参数配置的任何影响。
      - `banner_dismissed_date`：顶部滚动提示条被用户关闭的**当天**
        （北京时间 YYYY-MM-DD）。记日期而不是布尔值 —— 提示条要求
        「关闭后当天不再显示」，次日仍要重新提示，布尔值做不到这个语义。

    **没有设置行 = 用默认值**：存量用户零配置即生效（开 / 30-30 / high / USD），
    不需要给每个存量用户回填一行。
    """
    __tablename__ = "user_news_settings"

    # 归属用户（users.id）。不 autoincrement：这是外键语义，由 users 分配。
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    before_min: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    after_min: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    min_impact: Mapped[str] = mapped_column(String(8), default="high", server_default="high")
    currencies_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    symbols_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    manual_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    manual_paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manual_pause_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 顶部滚动提示条被用户关闭的**当天**（北京时间 YYYY-MM-DD）。
    # 记日期而不是布尔值：提示条要求「关闭后当天不再显示」，次日仍要重新提示，
    # 布尔值做不到「每天一次」的语义。NULL = 当天还没关闭过。
    banner_dismissed_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class UserReview(Base):
    """注册用户的一次 KYC 实名审核单。

    与 `users.is_active` 的分工（两者不可互相替代）：
      - `status`         = 业务审核状态（pending / approved / rejected）
      - `users.is_active` = 登录闸门（审核通过才置 True）

    `is_active` 表达不了「已驳回」—— 它只有启用 / 禁用两态，也没有地方存
    审核时间、审核人、驳回原因。所以审核状态独立成表，`is_active` 退化为
    纯登录闸门。两个维度正交：审核通过后管理员仍可在用户权限页禁用该用户
    （表现为 status='approved' + is_active=False）。

    一人可有多张单：被驳回后重新提交会**新建**一张，旧单保留，审核历史可追溯。

    **实名材料只有三项**：姓名 / 邮箱 / 手机号。
    手机号在这里是**快照**（提交审核那一刻的号码）—— 用户之后改绑手机号
    不影响历史单据展示的当时信息。
    """
    __tablename__ = "user_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    # —— 实名材料：姓名 / 邮箱 / 手机号 ——
    real_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 提交时的手机号快照（脱敏展示用：138****8000）
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 用户是否自填了姓名（False = 由管理员在审核时手工补录）
    kyc_submitted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # —— 审核流转 ——
    submit_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    audited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Role(Base):
    """角色：一组菜单 + 一组能力。内置角色（admin / user）由启动种子创建，不可删除。

    菜单走 sys_role_menu 关联表；capabilities 是本角色的能力码列表（JSON 数组字符串）。
    管理员角色固定拥有全部能力，无需在这里维护 admin 的全量值。
    """
    __tablename__ = "sys_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 角色勾选的能力码，JSON 数组字符串（如 '["ai_strategy"]'）
    capabilities: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class RoleMenu(Base):
    """角色 × 菜单。菜单本身是代码注册表（permissions.MENU_REGISTRY），库里只存勾选关系。"""
    __tablename__ = "sys_role_menu"
    __table_args__ = (UniqueConstraint("role_id", "menu_code", name="uq_role_menu"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(Integer, index=True)
    menu_code: Mapped[str] = mapped_column(String(32))


class UserRole(Base):
    """用户 × 角色（多对多）；用户的菜单为其全部启用角色的菜单并集。"""
    __tablename__ = "sys_user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    role_id: Mapped[int] = mapped_column(Integer, index=True)


class AuditLog(Base):
    """操作审计：谁、在何时、对什么、做了什么、结果如何；可附带操作前后数据。"""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    operator: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # console（中控台）/ node（节点）/ system（其它）
    category: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 操作前数据
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 操作后数据
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SignalHistory(Base):
    """信号历史：每条 Webhook 信号的解析与分发概况。"""
    __tablename__ = "signal_history"

    signal_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # 信号来源：tradingview（外部 Webhook）/ manual（中控台手动触发）；历史为空按 tradingview 展示
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 信号处理模型：normal（按币种分发，默认）/ strategy（按分组分发）；历史为空按 normal 展示
    model: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # 整体拒收原因的结构化编码（见 signal_codes.REJECT_CODES）；NULL = 未被拒收
    reject_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    # —— 信号元信息（统计维度，不参与任何交易判断）——
    # 周期：TV 告警的 {{interval}} 归一后的值（M15 / H1 / …）。缺就 NULL，**绝不拒收信号**
    timeframe: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    # 首仓信号类型（ob / fvg / bos / …）。未知类型保留原值小写，不丢
    signal_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # —— 路由状态（信号中心的 inbox 层）——
    # pending（待路由）/ auto（直连已消费）/ routed（被中心下发过）/ dropped（已丢弃）
    route_state: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # direct（分组直连）/ center（信号中心下发）
    route_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SignalDispatch(Base):
    """分发明细：单条信号 × 单个节点的一次执行结果。"""
    __tablename__ = "signal_dispatch"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    decided_vol: Mapped[float | None] = mapped_column(Float, nullable=True)  # 实际决策手数
    gate_result: Mapped[str] = mapped_column(String(16), default="passed")   # passed / skipped
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 跳过原因的结构化编码（见 signal_codes.SKIP_CODES）；统计一律按它聚合，不解析中文
    skip_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)      # MT5 返回码
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)         # 成交价位
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 毫秒精度：sent_at → finished_at 是「命令往返时延」的两端，秒级精度算不出真实时延
    finished_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # —— 时延埋点（观测用，不参与任何交易判断）——
    # sent_at：服务端把命令交给节点的时刻（UTC）。与 dispatched_at 不同——
    # 后者是落库时刻，落在 send_to_node 之后，语义不够准。
    sent_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # recv_at：节点从 WS 读到命令的时刻（节点本地时钟，与 sent_at 跨机不可比）
    recv_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # order_at：节点真正调 order_send 之前的时刻
    order_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # filled_at：order_send 返回（拿到成交价）的时刻
    filled_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # queued_ms：order_at - recv_at，节点内部排队耗时（单线程 MT5 队列抢占）
    queued_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # order_to_fill_ms：filled_at - order_at，券商往返耗时
    order_to_fill_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


# ===================== MT5 成交明细（deal）=====================
# 节点上报的历史成交，是「已实现盈亏 / 胜率」的唯一权威数据源。
# 与 group_task_dispatch.realized_profit（策略任务收口时上报的累计值）不同：
# deal 是**逐笔**真实成交，覆盖策略任务、普通信号分发、终端手工单全部来源。


class Deal(Base):
    """MT5 成交明细（deal）：节点上报的历史成交，节点概览盈亏统计的数据源。

    幂等键 = (node_id, deal_id)：同一个节点上同一笔成交只入库一次。节点采集是
    增量的（带重叠窗口），重传是常态，因此**必须**靠这个唯一键去重，否则统计
    会因重复上报而翻倍。

    deal_id = MT5 的成交票号（deal.ticket），在单个账户内单调递增。
    """

    __tablename__ = "mt5_deal"
    __table_args__ = (
        UniqueConstraint("node_id", "deal_id", name="uq_mt5_deal_node_deal"),
    )

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    # MT5 成交票号（deal.ticket）
    deal_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # BUY / SELL：成交方
    direction: Mapped[str] = mapped_column(String(8), default="BUY")
    # IN / OUT / INOUT / OUT_BY：开仓 / 平仓 / 反手 / 对敲
    entry_type: Mapped[str] = mapped_column(String(8), default="IN", index=True)
    # MT5 DEAL_REASON：0=客户端 1=移动 2=网页 3=专家 4=止损 5=止盈 6=对手方
    reason: Mapped[int] = mapped_column(Integer, default=0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    swap: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    # net_profit = profit + swap + commission：统计口径统一用这个
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    # 魔术号：可反查是哪个策略任务下的成交
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 成交时间（MT5 服务器时间，Unix 秒）
    deal_time: Mapped[int] = mapped_column(BigInteger, index=True)
    # —— 来源与关联（回填）——
    # strategy（策略任务）/ signal（信号派发）/ manual（手工平仓）/ terminal（终端手工单）
    source: Mapped[str] = mapped_column(String(16), default="terminal", index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 上报时的账户权益：收益率分母（区间期初权益）依赖它
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=0.0)
    # 上报时该节点的券商服务器时钟相对真实 UTC 的偏移（秒）。
    # deal_time 是「服务器钟面按 UTC 编码的 Unix」，比真实 UTC 快 offset 秒；
    # 按自然日分桶时必须减掉它，否则经纪商不在 UTC+0 时会整体错位一天。
    server_time_offset: Mapped[int] = mapped_column(Integer, default=0)
    # 是否为补采的历史成交（不是实时增量）
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


# 以下四张表只服务于 model=strategy 的分组分发流程，与上面的 signal_dispatch
# （model=normal 的按币种分发）互不读写，保证两条链路的规则与数据完全隔离。


class NodeGroup(Base):
    """节点分组（strategy 信号的分发单元；权威数据源，Redis 仅作缓存）。

    分组自带分发模式（sync / poll），不按币种区分——这是与中控台按币种配置的
    根本差别，也是 strategy 与 normal 两条链路隔离的关键。
    每个分组最多绑定一个交易策略（一对一）；strategy_id 为空表示未绑定。
    """
    __tablename__ = "node_group"

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # sync（全员同步）/ poll（组内轮转，一个信号只由一个节点领取）
    dispatch_mode: Mapped[str] = mapped_column(String(8), default="sync")
    # 信号并发：仅全员同步生效；开启后同组同节点可同时承接多条开仓信号（默认关）
    signal_concurrent_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 趋势风控：开仓前按全局趋势参数对各节点算信号品种趋势，仅顺势放行（默认关）
    trend_risk_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 限价挂单监听：把节点 MT5 上手动挂的限价单转成 strategy 信号（默认关；关键字默认为 limit，允许空）
    limit_watch_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_watch_keyword: Mapped[str] = mapped_column(String(32), default="limit")
    # AI 跟单开关：是否对外开放【AI 交易】广场的跟单（默认关）。
    # 只对绑定 tpl_1（AI 智能加仓策略）的分组有意义；AI 加仓是持续加仓，
    # 暴露一个参数激进的老分组代价远高于普通带单，所以默认关闭、需显式开启。
    accept_ai_follow: Mapped[bool] = mapped_column(Boolean, default=False)
    # 信号来源模式：direct（TV / 手动信号直连触发，默认）/ center（只接受信号中心下发）。
    # 路由策略挂在分组上而不是全局开关——这样存量分组零行为变化，
    # 且可以在不同分组间混用两种模式。
    signal_source: Mapped[str] = mapped_column(
        String(16), default="direct", server_default="direct",
    )
    # 一对一绑定 TradingStrategy；unique 保证同一策略不能挂到多个分组
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 数据归属：创建者；空 = 管理员名下。成员节点与绑定策略必须同一所有者
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NodeGroupMember(Base):
    """分组成员：节点加入分组的关联关系（一个节点可同时属于多个分组）。"""
    __tablename__ = "node_group_member"
    __table_args__ = (UniqueConstraint("group_id", "node_id", name="uq_group_node"),)

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 组内轮询顺序（越小越先）
    # 该成员的挂单是否作为限价监听触发来源；默认关，与分组级开关独立
    limit_watch_report: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupFollowerNode(Base):
    """生效跟单成员：带单与 AI 跟单均允许本人的主节点。

    与 NodeGroupMember 严格分离，两者职责不可混用：
    - NodeGroupMember    = 分组所有者自己的节点（owner 可编辑 / 排序 / 开限价监听）；
    - GroupFollowerNode  = 跟单员的节点（只读、不可被 owner 编辑、限价监听恒关）。

    同一个节点不能既是 owner 成员又是跟单成员：加入跟单时校验节点归属，
    而 owner 成员要求 owner_user_id 与分组一致，天然互斥。
    """
    __tablename__ = "group_follower_node"
    __table_args__ = (UniqueConstraint("group_id", "node_id", name="uq_follow_group_node"),)

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    # 跟单员用户 ID（冗余自 nodes.owner_user_id，便于按人查询 / 审计）
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)
    follow_state: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    leader_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    follow_settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    single_dispatch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 带单源冗余快照：写入时取「分组 owner 的主节点」。
    # 只用于展示 / 审计 / 主节点切换后的存量排查，不参与运行时判定
    # （运行时一律按 group.owner_user_id 现算主节点，避免主节点切换后快照过期）。
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 组内轮询顺序
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FollowRequest(Base):
    """带单申请与生效关系分离；每个分组、节点独立审核。"""
    __tablename__ = "follow_request"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pending_key: Mapped[str | None] = mapped_column(String(96), nullable=True, unique=True)
    source: Mapped[str] = mapped_column(String(16), default="join")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiFollowApplication(Base):
    __tablename__ = "ai_follow_application"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    leader_user_id: Mapped[int] = mapped_column(Integer, index=True)
    follower_user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pending_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    source: Mapped[str] = mapped_column(String(16), default="join")
    robot_name: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    leader_primary_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiFollowApplicationNode(Base):
    __tablename__ = "ai_follow_application_node"
    __table_args__ = (UniqueConstraint("application_id", "node_id", name="uq_ai_application_node"),)

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(AutoPK, index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    follow_settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    original_follow_state: Mapped[str] = mapped_column(String(16), default="running")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DeletedNode(Base):
    __tablename__ = "deleted_node"

    mt5_login: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    deleted_by_user_id: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupSignalTask(Base):
    """分组信号主任务：一条 strategy 信号在一个分组内的**分发记录**。

    主任务本身不是执行单元、不持有魔术号、也不参与并发互斥——真正执行策略的是
    下面的 GroupTaskDispatch（节点子任务），每个节点独立持有自己的魔术号。
    这里只记录「这条信号在这个分组命中了哪些节点、整体进展如何」，status 由各
    子任务状态汇总而来（见 group_rules.aggregate_task_status）。
    """
    __tablename__ = "group_signal_task"

    task_id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # —— 信号信息 ——
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # —— 绑定策略（触发时快照；保存策略时可热推给进行中子任务）——
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # —— 分组信息与下发数据 ——
    dispatch_mode: Mapped[str] = mapped_column(String(8), default="sync")
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 实际下发给节点的命令
    node_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 下发节点 ID 列表
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    # pending / dispatching / running / done / partial / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 主任务级跳过原因的结构化编码（分组禁用 / 无成员 / 无有效节点等）
    skip_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    # —— 策略运行期汇总（各子任务累加）——
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    realized_profit: Mapped[float] = mapped_column(Float, default=0.0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # —— 路由（信号中心）——
    # 触发来源：tv_webhook / manual / limit_watch / center（从信号中心下发）
    trigger_source: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # 路由方式：direct（分组直连自动触发）/ center（信号中心下发）
    route_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SignalEntryLeg(Base):
    """信号入场明细：一条信号的一次入场（首仓 / 某一档加仓）。

    把「每一次入场都有他的原因」落成**可聚合的窄行**，而不是每次统计都去解析
    `group_signal_task.strategy_snapshot_json`（JSON 无法 group by，慢且脆）。

    两级周期语义（务必分清）：
    - `signal_history.timeframe` = **信号级**周期，来自 TV 告警，与加仓方式无关；
    - 本表 `timeframe` = **入场级**周期，是这次入场实际生效的周期。
      **点数（point）与指定价（price）加仓不依赖周期**（配了也不读），
      所以它们的 `timeframe` 恒为 NULL —— 硬凑一个周期会把「点数间距」
      混进「ATR 间距」的分桶，统计出来的数字没有意义。

    `realized_profit` 是该入场对应持仓的已实现净盈亏（profit + swap + commission）。
    """
    __tablename__ = "signal_entry_leg"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 入场序号：0 = 首仓；1+ = 第 N 档加仓
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # 入场原因：first / point / price / atr / range / fvg / ob / pdz / fibo / manual
    reason: Mapped[str] = mapped_column(String(16), index=True)
    # 规则类型：1 = 逆势加仓，2 = 顺势加仓（首仓为 NULL）
    rule_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 档位号（首仓为 NULL）
    level_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 实际生效的周期；point / price 恒为 NULL
    timeframe: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    # 该入场的手数与成交价（成交后回填）
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 该入场对应的 MT5 订单票号（成交后回填）：用来反查 position_id，
    # 再把该持仓的净盈亏归集回这一行
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # 已实现净盈亏（成交入库后按持仓归集回填）
    realized_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupTaskDispatch(Base):
    """节点信号任务：主任务 × 节点，一对多，是策略执行的实际单元。

    `magic = NODE_TASK_MAGIC_BASE + id`，即每个节点持有独立魔术号，因此一个魔术号
    全局唯一地对应一次节点执行，MT5 订单可直接反查到本行（历史数据里存在多节点
    共用魔术号的旧记录，所以 magic 上不加唯一索引）。

    生命周期：pending -> sent -> opened -> running -> closing -> done。
    完成判定默认是「该节点上 magic 关联的持仓全部平掉」，由节点主动上报，
    服务端再用账户快照对账兜底（见 group_persist.reconcile_*）。
    网格等策略空仓是常态时 hold_when_empty=True，对账不会因无持仓强制收口。

    节点停止交易但持仓没平干净时会落 stop_failed / detached / faulted，
    这些都**不是终态**：持仓仍在，占位继续持有，直到确认清空才收口。

    并发互斥也落在这一层：子任务处于非终态时，(node_id, symbol) 会持有一个 Redis
    占位，同一节点同品种不会被重复下发（不同品种可并行）。
    """
    __tablename__ = "group_task_dispatch"

    follow_settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    # 冗余存一份品种：释放节点互斥占位、按品种对账时不必回查主任务
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 冗余存一份动作（BUY / SELL / CLOSE）：信号中心的统一统计要把两张分发表
    # UNION 成一张视图，带上它就不必每次统计都 join 主任务表
    action: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    decided_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    # pending / sent / opened / running / closing / done / failed / skipped / offline
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 跳过原因的结构化编码（见 signal_codes.SKIP_CODES）；统计一律按它聚合，不解析中文
    skip_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # —— 策略运行期实时快照 ——
    position_count: Mapped[int] = mapped_column(Integer, default=0)   # 当前 magic 持仓笔数
    # 当前 magic 未成交挂单笔数；限价开仓在成交前只有它，是「在途但未持仓」的唯一凭据
    pending_orders: Mapped[int] = mapped_column(Integer, default=0)
    add_count: Mapped[int] = mapped_column(Integer, default=0)        # 已加仓次数
    # 各规则已开笔数（按规则类型分开，如 {"1": 3, "2": 0}）：前端校验
    # 「上限不能低于已开笔数」的依据，规则类型是动态键所以用 JSON 存
    add_counts_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 各规则的加仓进度（当前第几批 / 还能再加几笔 / 下一批的间距与手数）：
    # 前端展示「加到第几批第几仓」的唯一来源。规则类型是动态键，用 JSON 存
    add_progress_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)     # 累计下单笔数
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)   # 累计手数
    realized_profit: Mapped[float] = mapped_column(Float, default=0.0)
    finish_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 网格等策略空仓是常态：为 True 时账户快照对账不会因无持仓强制收口
    hold_when_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    # 节点已停止交易但魔术号下仍有持仓的笔数（stop_failed / detached / faulted）
    residual_positions: Mapped[int] = mapped_column(Integer, default=0)
    # 策略运行态（网格的触发状态、平移量、当前网格线与止损止盈等），
    # 供节点重连后原样恢复；结构由节点定义，服务端只负责透传
    runtime_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 终止意图落库时刻：跨服务重启也不会把 closing 的子任务又恢复成继续跑
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 毫秒精度：sent_at → opened_at 是「策略命令往返时延」的两端（节点首次回报开仓）
    opened_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # —— 时延埋点（观测用，不参与任何交易判断）——
    # sent_at：服务端把 strategy_start 交给节点的时刻（UTC）
    sent_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # recv_at：节点从 WS 读到命令的时刻（节点本地时钟，与 sent_at 跨机不可比）
    recv_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # order_at：节点真正调 order_send 之前的时刻
    order_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # filled_at：order_send 返回（拿到成交价）的时刻
    filled_at: Mapped[datetime | None] = mapped_column(MsDateTime, nullable=True)
    # queued_ms：order_at - recv_at，节点内部排队耗时（单线程 MT5 队列抢占）
    queued_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # order_to_fill_ms：filled_at - order_at，券商往返耗时
    order_to_fill_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class GroupTaskEvent(Base):
    """子任务的策略执行事件流：开仓 / 加仓 / 平仓 / 异常。

    只记录状态变更事件；纯行情心跳不落库（只更新子任务的实时快照字段），
    否则长周期任务会把表写爆。
    """
    __tablename__ = "group_task_event"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # open / add_counter / add_trend / add_manual / manual_pending / manual_filled /
    # manual_tp_align / manual_tp_close / grid_add / grid_shift /
    # cnt_limit / cnt_limit_mod / cnt_limit_off / close_partial / close_all / error / resume
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LimitWatchLog(Base):
    """分组限价挂单监听日志（strategy 链路附属，不进 normal 分发）。

    一张触发单可能命中多个分组，按 group_id 各写一行。权威历史在 MySQL；
    后台列表靠 WS `limit_watch_log` 实时追加。
    """
    __tablename__ = "limit_watch_log"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    ticket: Mapped[int] = mapped_column(BigInteger, default=0)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # rejected / cancel_failed / ok / dispatch_rejected / duplicate
    event: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(String(512), default="")
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ===================== 策略管理（加仓规则实例） =====================


class TradingStrategy(Base):
    """交易策略实例：基于策略模版创建，绑定品种，保存一份可独立修改的规则配置。"""
    __tablename__ = "trading_strategy"

    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    template_id: Mapped[str] = mapped_column(String(32), index=True)
    template_name: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 规则列表：逆势/顺势各一条，含可选分批档位 batch_levels
    # [{type, status, action, point, lot_times, extra_lot, max_allow_num,
    #   batch_enabled, batch_action, batch_count, total_lot_limit, batch_levels}, ...]
    config_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 配置版本：每次保存 +1。热推时随快照下发，节点用它做版本门，
    # 迟到的旧版本包直接丢弃，避免乱序热推把新配置覆盖回旧的。
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    # 数据归属：创建者；空 = 管理员名下
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
