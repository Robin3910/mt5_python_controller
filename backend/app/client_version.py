"""客户端版本管理的纯规则：版本解析比较、更新判定、安装包结构校验。

不依赖 FastAPI / SQLAlchemy / 配置，可脱离 Web 栈单测；落盘、鉴权与编排都在
`client_version_api.py`。运维面板侧的同类判定复用同一套语义
（见 `nodel_client_dashboard/version_service.py`）。
"""
from __future__ import annotations

import re
import time

# 安装包内必须存在的主程序；缺它说明传错了包
REQUIRED_ENTRY = "node_client.exe"

# 覆盖安装时永不替换的文件：.env 承载节点身份（NODE_TOKEN / MT5 账号），覆盖即掉线
PROTECTED_NAMES = frozenset({".env"})

# 发布指针在 system_setting 表里的 key
RELEASE_KEY = "client_release"

# 版本号既是主键也是磁盘文件名的一部分，收窄字符集以杜绝路径穿越
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")
_NUM_RE = re.compile(r"\d+")

UPGRADE = "upgrade"
DOWNGRADE = "downgrade"
SAME = "same"
UNKNOWN = "unknown"


def normalize_version(raw: str) -> str:
    """规范化版本号（去空白与单个 v 前缀）；非法字符一律返回空串。"""
    v = re.sub(r"^[vV]", "", (raw or "").strip()).strip()
    return v if _VERSION_RE.match(v) else ""


def parse_version(raw: str) -> tuple[int, ...]:
    """取版本串里的数字段用于比较；无数字段返回空元组。"""
    return tuple(int(x) for x in _NUM_RE.findall(raw or ""))


def compare_versions(a: str, b: str) -> int:
    """比较版本：a < b 返回 -1，相等 0，a > b 返回 1。

    数字段逐位比较，位数不齐的补零（1.1 与 1.1.0 视为同版本）；两边都取不出
    数字段时退化为字符串比较，保证任意输入都有确定次序。
    """
    pa, pb = parse_version(a), parse_version(b)
    if not pa and not pb:
        sa, sb = (a or "").strip(), (b or "").strip()
        return (sa > sb) - (sa < sb)
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return (pa > pb) - (pa < pb)


def classify_update(current: str, target: str) -> str:
    """判定 current → target 属于升级 / 降级 / 相同；任一侧未知返回 unknown。

    降级是高危动作（可能把已修复的问题带回线上），调用方据此追加二次确认。
    """
    cur, tgt = (current or "").strip(), (target or "").strip()
    if not cur or not tgt:
        return UNKNOWN
    diff = compare_versions(cur, tgt)
    if diff < 0:
        return UPGRADE
    return DOWNGRADE if diff > 0 else SAME


def version_from_txt(text: str) -> str:
    """按 node_client/version.py:get_version() 的规则从 version.txt 取版本。"""
    stripped = (text or "").lstrip("\ufeff").strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def member_basename(name: str) -> str:
    """取 zip 成员的文件名部分（兼容 zip 里出现的反斜杠分隔符）。"""
    return (name or "").replace("\\", "/").rsplit("/", 1)[-1]


def is_safe_member(name: str) -> bool:
    """成员名是否可安全解压：拒绝绝对路径、盘符前缀与 .. 上跳（zip slip）。"""
    n = (name or "").replace("\\", "/").strip()
    if not n or n.startswith("/"):
        return False
    parts = n.split("/")
    if ":" in parts[0]:
        return False
    return ".." not in parts


def is_protected(name: str) -> bool:
    """覆盖安装时该文件是否必须保留目标机上的原件。"""
    return member_basename(name).lower() in PROTECTED_NAMES


def find_member(names: list[str], filename: str) -> str:
    """在成员清单里按文件名（不区分大小写）找一项，返回原始成员名；找不到返回空串。"""
    want = filename.lower()
    for n in names:
        if member_basename(n).lower() == want:
            return n
    return ""


def validate_package(names: list[str]) -> tuple[bool, str]:
    """校验安装包成员清单，返回 (是否通过, 不通过的原因)。"""
    files = [n for n in names if n and not n.endswith("/")]
    if not files:
        return False, "安装包内没有文件"
    for n in files:
        if not is_safe_member(n):
            return False, f"安装包含非法路径：{n}"
    if not find_member(files, REQUIRED_ENTRY):
        return False, f"安装包内未找到 {REQUIRED_ENTRY}"
    return True, ""


def build_release(version: str, current: dict | None) -> dict:
    """生成新的发布指针。

    把被替换掉的版本记进 previous —— 服务端「回滚」就是把它换回来，因此同版本
    重复发布时不能覆盖 previous，否则回滚会退无可退。
    """
    prev = (current or {}).get("version") or ""
    if prev == version:
        prev = (current or {}).get("previous") or ""
    return {"version": version, "previous": prev, "updated_at": time.time()}


def rollback_release(current: dict | None) -> dict | None:
    """把发布指针回退到 previous；没有可回退的历史版本时返回 None。"""
    cur = current or {}
    prev = (cur.get("previous") or "").strip()
    if not prev:
        return None
    return {"version": prev, "previous": cur.get("version") or "", "updated_at": time.time()}
