"""普通用户的配置保护规则，独立于表单与原始文本入口。"""
from __future__ import annotations

from env_file import parse_env_text
import re

PROTECTED_ENV_KEYS = frozenset({"NODE_TOKEN", "MANAGER_WS_URL", "DASHBOARD_EXPECTED_MT5_LOGIN", "MT5_LOGIN", "MT5_ACCOUNT", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "NODE_ID"})
_ENV_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z_0-9]*))\s*=", re.IGNORECASE)


def _assignment(line):
    match = _ENV_ASSIGNMENT.match(line)
    key = next((value for value in match.groups() if value is not None), "").upper() if match else ""
    return key, match


def protected_changes(before: str, after: str) -> set[str]:
    old, new = _normalized(before), _normalized(after)
    return {key for key in PROTECTED_ENV_KEYS if old.get(key) != new.get(key)}


def redact_env(text: str) -> str:
    lines = []
    for line in text.splitlines():
        key, _ = _assignment(line)
        lines.append(f"{key}=（由面板管理）" if key in PROTECTED_ENV_KEYS else line)
    return "\n".join(lines) + "\n"


def _normalized(text: str) -> dict:
    normalized = []
    for line in text.splitlines():
        key, match = _assignment(line)
        normalized.append(key + "=" + line[match.end():] if match else line)
    return {key.upper(): value for key, value in parse_env_text("\n".join(normalized)).items()}


def env_values(text: str) -> dict:
    return _normalized(text)


def merge_redacted_env(original: str, edited: str) -> str:
    """允许编辑普通字段；保护行必须保留占位且次数不变，再恢复原秘密行。"""
    def is_protected(line):
        key, match = _assignment(line)
        return bool(match and key in PROTECTED_ENV_KEYS)
    redacted_lines = [line for line in redact_env(original).splitlines() if is_protected(line)]
    edited_lines = [line for line in edited.splitlines() if is_protected(line)]
    if edited_lines != redacted_lines:
        raise ValueError("后端地址、节点令牌与账户绑定的保护行不可修改、增删或重复")
    secrets = iter(line for line in original.splitlines() if is_protected(line))
    restored = [next(secrets) if is_protected(line) else line for line in edited.splitlines()]
    result = "\n".join(restored) + "\n"
    if protected_changes(original, result):
        raise ValueError("保护字段校验失败")
    return result
