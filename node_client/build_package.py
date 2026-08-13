"""构建期打包：把产物目录压成可上传到后端的安装包 zip。

    python build_package.py <产物目录> [输出目录]

只由 `build_exe*.bat` 在打包末尾调用。zip 内部是**相对客户端目录的文件树**，与
后端 `client_version.validate_package` 和面板 `apply_package` 的口径一致：onefile
形态是 `node_client.exe` + `version.txt`，onedir 形态是整棵目录树。

`.env` 绝不入包 —— 它承载 NODE_TOKEN 与 MT5 账号，而安装包会上传到后端，任何持有
节点令牌的机器都能下载。改这里的排除规则前先想清楚这一点。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# 主程序名；缺它后端会拒收整个包
ENTRY_NAME = "node_client.exe"

# 随包附带的配置模板：面板导入新节点时用它生成 .env，用户才能看到完整的中文说明
TEMPLATE_NAME = ".env.example"

# 绝不入包的文件名（小写比较）
EXCLUDE_NAMES = frozenset({".env"})

# 绝不入包的目录名（面板生成的版本化备份、Python 缓存）
EXCLUDE_DIRS = frozenset({".backups", "__pycache__"})

# 绝不入包的后缀（旧版单槽备份）
EXCLUDE_SUFFIXES = frozenset({".bak", ".pyc", ".log"})


def should_include(rel_path: str) -> bool:
    """该相对路径是否应打进安装包。"""
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


def package_name(version: str) -> str:
    return f"node_client-{version}.zip"


def read_version(root: Path) -> str:
    """读产物目录旁的 version.txt（由 build_version.py 写入）。"""
    txt = root / "version.txt"
    if not txt.is_file():
        return ""
    text = txt.read_text(encoding="utf-8-sig").strip()
    return text.splitlines()[0].strip() if text else ""


def verify_package(zip_path: Path) -> tuple[bool, str]:
    """自检刚打出的包：必须含主程序，且绝不能含 .env。

    放在构建期而不是上传期，是为了让打包失误当场暴露 —— 上传后才发现 .env 泄露
    就已经晚了。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return False, "package is empty"
    lowered = {Path(n.replace("\\", "/")).name.lower() for n in names}
    if ENTRY_NAME not in lowered:
        return False, f"{ENTRY_NAME} missing from package"
    if EXCLUDE_NAMES & lowered:
        return False, "package must not contain .env"
    return True, ""


def build_package(root: Path, out_dir: Path, *, template: Path | None = None) -> Path:
    """把产物目录打成 zip，返回产物路径。"""
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
    zip_path = out_dir / package_name(version)
    # 重打同名包时先删，避免 append 模式留下上一次的成员
    zip_path.unlink(missing_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(root / rel, rel.as_posix())
        # 模板只在产物目录里没有时补一份，避免覆盖打包流程自己放进去的版本
        if template and template.is_file() and not any(
            p.name.lower() == TEMPLATE_NAME for p in files
        ):
            zf.write(template, TEMPLATE_NAME)

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
    template = Path(__file__).resolve().parent / TEMPLATE_NAME

    try:
        zip_path = build_package(root, out_dir, template=template)
    except (OSError, RuntimeError, FileNotFoundError) as e:
        print(f"Packaging failed: {e}", file=sys.stderr)
        return 1
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"{zip_path}|{size_mb:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
