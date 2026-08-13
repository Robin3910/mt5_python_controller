"""构建期版本戳：生成 `${数字版本号}-${年月日时分秒}` 并落盘。

只由 `build_exe*.bat` 在打包时调用，运行时不参与导入：

    python build_version.py stamp        # 写 _build_info.py，stdout 打印本次版本号
    python build_version.py write <目录>  # 把本次版本号写进 <目录>\\version.txt

拆成两步是因为两个落点的时机不同：`_build_info.py` 必须赶在 PyInstaller 分析之前
就位才能被冻结进 exe，而 `version.txt` 要等产物目录建好之后才能写。第二步以第一步
的产物为准，保证 exe 内外是同一个版本号。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from version import BASE_VERSION, embedded_version, make_version

BUILD_INFO_PATH = Path(__file__).resolve().parent / "_build_info.py"

_TEMPLATE = '''"""打包时自动生成，请勿手工编辑或提交（见 build_version.py）。"""
BUILD_VERSION = "{version}"
BUILT_AT = "{built_at}"
'''


def render_build_info(version: str, built_at: str) -> str:
    """渲染 `_build_info.py` 的内容。"""
    return _TEMPLATE.format(version=version, built_at=built_at)


def stamp(base: str = BASE_VERSION, now: datetime | None = None) -> str:
    """生成本次构建的版本号并写入 `_build_info.py`，返回版本号。"""
    at = now or datetime.now()
    version = make_version(base, at)
    BUILD_INFO_PATH.write_text(
        render_build_info(version, at.isoformat(timespec="seconds")), encoding="utf-8"
    )
    return version


def write_sidecar(out_dir: Path, version: str) -> Path:
    """把版本号写成产物目录旁的 `version.txt`。"""
    path = out_dir / "version.txt"
    path.write_text(version + "\n", encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    # 提示一律用英文：调用方是 cmd 里的 .bat，中文在默认代码页下会显示成乱码
    cmd = argv[0] if argv else ""
    if cmd == "stamp":
        try:
            print(stamp())
        except ValueError as e:
            print(f"Invalid BASE_VERSION in version.py: {e}", file=sys.stderr)
            return 1
        return 0
    if cmd == "write":
        if len(argv) < 2:
            print("Usage: python build_version.py write <dir>", file=sys.stderr)
            return 2
        version = embedded_version()
        if not version:
            print("_build_info.py not found, run 'stamp' first.", file=sys.stderr)
            return 1
        out_dir = Path(argv[1])
        if not out_dir.is_dir():
            print(f"Output dir not found: {out_dir}", file=sys.stderr)
            return 1
        write_sidecar(out_dir, version)
        print(version)
        return 0
    print("Usage: python build_version.py stamp | write <dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
