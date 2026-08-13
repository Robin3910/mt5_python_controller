"""node_client 版本号：`${数字版本号}-${年月日时分秒}`。

数字版本号 `BASE_VERSION` 发版时手工改，时间戳由 `build_version.py` 在打包时生成，
所以每打一次包版本号都会变，不需要手工递增。构建版本一式两份落地：`_build_info.py`
随 exe 冻结进包，`version.txt` 写在产物目录旁供面板与后端核对。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

# 发大版本时改此常量；只写数字与点，时间戳由构建脚本追加
BASE_VERSION = "1.1.0"

# 时间戳格式：年月日时分秒
STAMP_FORMAT = "%Y%m%d%H%M%S"

# 后端 client_version._VERSION_RE 限死总长 ≤32，超长会被当成非法版本号拒收
MAX_VERSION_LEN = 32

_BASE_RE = re.compile(r"^\d+(?:\.\d+)*$")


def normalize_base(raw: str) -> str:
    """校验数字版本号（形如 `1.1.0`，允许单个 v 前缀）；非法一律返回空串。"""
    b = re.sub(r"^[vV]", "", (raw or "").strip()).strip()
    return b if _BASE_RE.match(b) else ""


def make_version(base: str = BASE_VERSION, now: datetime | None = None) -> str:
    """组装构建版本号 `${数字版本号}-${年月日时分秒}`。"""
    b = normalize_base(base)
    if not b:
        raise ValueError(f"数字版本号只能由数字与点组成（如 1.1.0）：{base!r}")
    v = f"{b}-{(now or datetime.now()).strftime(STAMP_FORMAT)}"
    if len(v) > MAX_VERSION_LEN:
        raise ValueError(f"版本号超过 {MAX_VERSION_LEN} 字符，请缩短数字版本号：{v}")
    return v


def embedded_version() -> str:
    """打包时冻结进包的构建版本；源码直跑时该模块不存在，返回空串。"""
    try:
        from _build_info import BUILD_VERSION  # type: ignore[import-not-found]
    except ImportError:
        return ""
    return (BUILD_VERSION or "").strip()


def _sidecar_version(base_dir: Path | None = None) -> str:
    """读产物目录旁路的 `version.txt`；读不到返回空串。"""
    try:
        if base_dir is None:
            frozen = getattr(sys, "frozen", False)
            base_dir = Path(sys.executable if frozen else __file__).resolve().parent
        txt = base_dir / "version.txt"
        if txt.is_file():
            text = txt.read_text(encoding="utf-8-sig").strip()
            if text:
                return text.splitlines()[0].strip()
    except OSError:
        pass
    return ""


def get_version() -> str:
    """运行时版本：旁路 `version.txt` > 冻结进包的构建版本 > 裸数字版本号。

    旁路文件优先，是为了让面板「只换 exe 与 version.txt」的替换方式立刻生效，也与
    面板侧 `read_version_near()` 的口径一致；两者都缺（源码直跑）时退回 `BASE_VERSION`，
    此时没有时间戳，后端会视为比任何构建产物都旧的版本。
    """
    return _sidecar_version() or embedded_version() or BASE_VERSION
