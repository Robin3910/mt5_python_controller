"""构建期打包：把面板产物目录压成便于分发的压缩包。

    python build_package.py <产物目录> [输出目录]

只由 `build_exe.bat` 在打包末尾调用。与 node_client 的安装包不同，这个包**没有**
自动安装通道，是给人手工解压用的，所以内部**带一层 `node_client_dashboard-<版本>/`
目录**，解压时不会把文件铺一地。

**面板的运行期数据绝不入包**：`panel_config.json` 存着 NODE_TOKEN，`instances.json`
记录本机每个实例的绝对路径，两者都写在 exe 同目录（也就是 `dist\\`）—— 开发机上只要
在 `dist\\` 里跑过一次面板就会生成它们，跟着打进分发包等于把令牌和本机拓扑一起发出去。
改这里的排除规则前先想清楚这一点。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# 主程序名；缺它说明构建没成功
ENTRY_NAME = "node_client_dashboard.exe"

# 绝不入包的文件名（小写比较）
# panel_config.json：后端地址与 NODE_TOKEN；instances.json：本机实例清单与路径
EXCLUDE_NAMES = frozenset({
    "panel_config.json",
    "instances.json",
    ".dashboard.lock",
})

# 绝不入包的目录名（运行日志、Python 缓存）
EXCLUDE_DIRS = frozenset({"logs", "__pycache__"})

# 绝不入包的后缀（替换时留下的备份、日志）
EXCLUDE_SUFFIXES = frozenset({".bak", ".pyc", ".log"})

# 随包附带的说明文档
DOC_NAME = "README.md"


def should_include(rel_path: str) -> bool:
    """该相对路径是否应打进分发包。"""
    parts = [p for p in Path(rel_path).parts if p not in (".", "")]
    if not parts:
        return False
    if any(p.lower() in EXCLUDE_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name.lower() in EXCLUDE_NAMES:
        return False
    return Path(name).suffix.lower() not in EXCLUDE_SUFFIXES


def collect_files(root: Path) -> list[Path]:
    """收集产物目录下应入包的文件（返回相对路径，稳定排序便于复现构建）。"""
    out = [
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and should_include(str(p.relative_to(root)))
    ]
    return sorted(out, key=lambda p: str(p).lower())


def package_stem(version: str) -> str:
    """压缩包主名，同时也是解压后那层目录的名字。"""
    return f"node_client_dashboard-{version}"


def package_name(version: str) -> str:
    return f"{package_stem(version)}.zip"


def read_version(root: Path) -> str:
    """读产物目录旁的 version.txt（由 build_version.py 写入）。"""
    txt = root / "version.txt"
    if not txt.is_file():
        return ""
    text = txt.read_text(encoding="utf-8-sig").strip()
    return text.splitlines()[0].strip() if text else ""


def verify_package(zip_path: Path) -> tuple[bool, str]:
    """自检刚打出的包：必须含主程序，且绝不能含面板的运行期数据。

    放在构建期而不是分发前，是为了让打包失误当场暴露 —— 包发出去之后才发现
    `panel_config.json` 里的令牌跟着走了，就已经晚了。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return False, "package is empty"
    lowered = {Path(n.replace("\\", "/")).name.lower() for n in names}
    if ENTRY_NAME not in lowered:
        return False, f"{ENTRY_NAME} missing from package"
    leaked = sorted(EXCLUDE_NAMES & lowered)
    if leaked:
        return False, f"package must not contain {', '.join(leaked)}"
    return True, ""


def build_package(root: Path, out_dir: Path, *, doc: Path | None = None) -> Path:
    """把产物目录打成 zip（内部带一层版本目录），返回产物路径。"""
    root = root.resolve()
    out_dir = out_dir.resolve()
    # 输出目录若在产物目录内部，下一次构建会把上一次的 zip 当成产物文件收进包里
    if out_dir == root or root in out_dir.parents:
        raise ValueError(f"output dir must be outside the build dir: {out_dir}")

    version = read_version(root)
    if not version:
        raise FileNotFoundError(f"version.txt not found in {root}, run build_version.py first")

    files = collect_files(root)
    if not any(p.name.lower() == ENTRY_NAME for p in files):
        raise FileNotFoundError(f"{ENTRY_NAME} not found in {root}")

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = package_stem(version)
    zip_path = out_dir / package_name(version)
    # 重打同名包时先删，避免 append 模式留下上一次的成员
    zip_path.unlink(missing_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(root / rel, f"{stem}/{rel.as_posix()}")
        # 说明文档只在产物目录里没有时补一份
        if doc and doc.is_file() and not any(p.name.lower() == DOC_NAME.lower() for p in files):
            zf.write(doc, f"{stem}/{DOC_NAME}")

    ok, reason = verify_package(zip_path)
    if not ok:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"package self-check failed: {reason}")
    return zip_path


def main(argv: list[str]) -> int:
    # 提示一律用英文：调用方是 cmd 里的 .bat，中文在默认代码页下会显示成乱码
    if not argv:
        print("Usage: python build_package.py <build_dir> [out_dir]", file=sys.stderr)
        return 2
    root = Path(argv[0])
    if not root.is_dir():
        print(f"Build dir not found: {root}", file=sys.stderr)
        return 1
    out_dir = Path(argv[1]) if len(argv) > 1 else root.parent
    doc = Path(__file__).resolve().parent / DOC_NAME

    try:
        zip_path = build_package(root, out_dir, doc=doc)
    except (OSError, RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Packaging failed: {e}", file=sys.stderr)
        return 1
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"{zip_path}|{size_mb:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
