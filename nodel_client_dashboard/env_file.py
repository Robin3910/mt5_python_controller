"""node_client `.env` 的读写与字段元数据（纯函数，供面板对话框调用）。

写回采用**就地合并**而不是整份重写：`.env.example` 里的中文注释是节点配置的主要
说明来源，用户也可能自己加了字段；整份重写会把这些全丢掉。因此 `update_env_text`
只改动目标键所在的那一行，其余原样保留。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ENV_FILENAME = ".env"

# MT5 终端可执行名；客户端必须与它同目录（见 node_client/mt5_discover.py）
TERMINAL_NAMES = ("terminal64.exe", "terminal.exe")

TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class EnvField:
    """一个可在面板里编辑的配置项。"""
    key: str
    label: str
    kind: str = "text"        # text | int | float | bool | choice
    help: str = ""
    choices: tuple[str, ...] = ()
    secret: bool = False      # 界面上是否掩码显示
    required: bool = False


# 面板表单暴露的字段。刻意不覆盖 .env 的全部键：
# LOCAL_STATUS_PORT 由面板启动实例时用环境变量注入，写进 .env 只会造成误解。
ENV_FIELDS: tuple[EnvField, ...] = (
    EnvField(
        "MANAGER_WS_URL", "后端网关地址", "text",
        "节点接收指令的 WebSocket 地址；生产经 nginx/TLS 时用 wss://",
        required=True,
    ),
    EnvField(
        "NODE_TOKEN", "节点接入令牌", "text",
        "所有节点共享，后台「配置 → 节点令牌」可复制",
        secret=True, required=True,
    ),
    EnvField(
        "MT5_MOCK", "使用模拟 MT5", "bool",
        "开启后不连真实终端，仅用于联调",
    ),
    EnvField(
        "WATCH_SYMBOLS", "本地观察列表", "text",
        "逗号分隔；会与中控台已配置品种、当前持仓品种合并后上报报价",
    ),
    EnvField("DEFAULT_SLIPPAGE", "默认滑点", "int"),
    EnvField("DEFAULT_MAGIC", "默认魔术号", "int"),
    EnvField("HEARTBEAT_INTERVAL", "心跳间隔（秒）", "int"),
    EnvField(
        "ACCOUNT_REPORT_INTERVAL", "账户上报间隔（秒）", "float",
        "支持小数，例如 0.5；下限 0.05，越小 MT5 快照与上报越频繁",
    ),
    EnvField("RECONNECT_MIN", "重连退避下限（秒）", "int"),
    EnvField("RECONNECT_MAX", "重连退避上限（秒）", "int"),
    EnvField("AUTH_TIMEOUT", "鉴权超时（秒）", "int"),
    EnvField(
        "STRATEGY_SAMPLE_INTERVAL", "策略采样间隔（秒）", "float",
        "越小加仓判定越及时，MT5 调用越频繁；下限 0.05",
    ),
    EnvField("STRATEGY_IDLE_INTERVAL", "策略保活间隔（秒）", "float"),
    EnvField(
        "STRATEGY_EMPTY_CONFIRM", "空仓确认轮数", "int",
        "判定持仓已全平所需的连续确认轮数，用于容忍 MT5 偶发返回不完整持仓",
    ),
    EnvField(
        "LOG_LEVEL", "日志级别", "choice", "",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
)


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本为键值字典；忽略注释与空行，去掉值两侧的引号。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        out[key.strip()] = _unquote(value.strip())
    return out


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _line_key(line: str) -> str:
    """取该行定义的键名；注释行、空行与非赋值行返回空串。"""
    s = line.strip().lstrip("\ufeff")
    if not s or s.startswith("#") or "=" not in s:
        return ""
    return s.split("=", 1)[0].strip()


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """把 updates 合并进 .env 文本，保留注释、空行、原有顺序与未涉及的键。

    已存在的键改值不改位置；新键追加到末尾。值为 None 的键会被跳过（不改动）。
    """
    pending = {k: v for k, v in (updates or {}).items() if v is not None}
    lines = (text or "").splitlines()
    out: list[str] = []

    for line in lines:
        key = _line_key(line)
        if key and key in pending:
            out.append(f"{key}={_quote_if_needed(pending.pop(key))}")
        else:
            out.append(line)

    if pending:
        # 与既有内容留一个空行，避免新键紧贴上一段注释造成误读
        if out and out[-1].strip():
            out.append("")
        for key, value in pending.items():
            out.append(f"{key}={_quote_if_needed(value)}")

    result = "\n".join(out)
    return result if result.endswith("\n") else result + "\n"


def _quote_if_needed(value: str) -> str:
    """含前后空白或 # 的值必须加引号，否则回读时会被截断或错解析。"""
    v = "" if value is None else str(value)
    if v != v.strip() or "#" in v:
        return f'"{v}"'
    return v


def read_env(cwd: str | Path) -> tuple[str, dict[str, str]]:
    """读取实例目录下的 .env，返回 (原始文本, 键值字典)；不存在返回空。"""
    path = env_path(cwd)
    if not path.is_file():
        return "", {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return "", {}
    return text, parse_env_text(text)


def write_env(cwd: str | Path, text: str) -> None:
    """整份写入 .env（调用方应先用 update_env_text 合并）。"""
    path = env_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def env_path(cwd: str | Path) -> Path:
    return Path(cwd) / ENV_FILENAME


def as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in TRUE_WORDS


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def validate_field(field: EnvField, value: str) -> str:
    """校验单个字段，返回中文错误说明；通过返回空串。"""
    v = (value or "").strip()
    if field.required and not v:
        return f"{field.label} 不能为空"
    if not v:
        return ""
    if field.kind == "int":
        try:
            int(v)
        except ValueError:
            return f"{field.label} 必须是整数"
    elif field.kind == "float":
        try:
            float(v)
        except ValueError:
            return f"{field.label} 必须是数字"
    elif field.kind == "choice" and field.choices and v.upper() not in field.choices:
        return f"{field.label} 只能是 {' / '.join(field.choices)}"
    if field.key == "MANAGER_WS_URL":
        scheme = urlparse(v).scheme.lower()
        if scheme not in ("ws", "wss"):
            return "后端网关地址必须以 ws:// 或 wss:// 开头"
        if not urlparse(v).netloc:
            return "后端网关地址缺少主机名"
    return ""


def validate_env(values: dict[str, str]) -> list[str]:
    """按字段元数据校验整份配置，返回所有错误说明。"""
    errors: list[str] = []
    for field in ENV_FIELDS:
        err = validate_field(field, values.get(field.key, ""))
        if err:
            errors.append(err)
    return errors


def ws_url_from_backend_base(base: str) -> str:
    """由后端 HTTP 基址反推节点网关地址：http://host → ws://host/ws/node。

    与 version_service.backend_base_from_ws_url 互为逆运算，导入新节点时用它
    生成 .env，省掉用户手抄一遍地址。
    """
    raw = (base or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(
        parsed.scheme.lower(), ""
    )
    if not scheme or not parsed.netloc:
        return ""
    return urlunparse((scheme, parsed.netloc, "/ws/node", "", "", ""))


def find_terminal(directory: str | Path) -> Path | None:
    """在目录里找 MT5 终端；找不到返回 None。"""
    if not directory:
        return None
    root = Path(directory)
    for name in TERMINAL_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def is_mt5_dir(directory: str | Path) -> bool:
    """该目录是否是 MT5 安装目录（客户端必须放进这里才能发现终端）。"""
    return find_terminal(directory) is not None


def build_initial_env(
    template_text: str,
    *,
    ws_url: str,
    node_token: str,
) -> str:
    """为新导入的节点生成初始 .env（在模板基础上填入地址与令牌）。"""
    return update_env_text(
        template_text or "",
        {"MANAGER_WS_URL": ws_url, "NODE_TOKEN": node_token},
    )


def write_import_env(
    cwd: str | Path,
    template_text: str,
    *,
    ws_url: str,
    node_token: str,
) -> bool:
    """导入时写入 .env：不存在则生成，已存在则整份覆盖为同一份初始内容。

    覆盖内容与首次导入相同：以安装包 `.env.example` 为模板，填入面板的后端地址与令牌。
    返回 True 表示覆盖了已有文件。版本更新路径仍不碰 `.env`（见 `client_deploy`）。
    """
    existed = env_path(cwd).is_file()
    write_env(
        cwd,
        build_initial_env(template_text, ws_url=ws_url, node_token=node_token),
    )
    return existed
