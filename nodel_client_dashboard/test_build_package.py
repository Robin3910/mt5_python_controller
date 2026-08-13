"""构建期打包测试：分发包结构与面板运行期数据的排除。"""
import zipfile
from pathlib import Path

import pytest

import build_package as bp

VERSION = "1.0.0-20260813150000"
STEM = f"node_client_dashboard-{VERSION}"


def _build_dir(tmp_path: Path, version: str = VERSION) -> Path:
    """造一个面板产物目录，并带上只有在 dist 里跑过面板才会出现的运行期数据。"""
    root = tmp_path / "dist"
    root.mkdir(parents=True, exist_ok=True)
    (root / "node_client_dashboard.exe").write_bytes(b"MZ fake")
    (root / "version.txt").write_text(version + "\n", encoding="utf-8")
    (root / "panel_config.json").write_text('{"node_token": "real-secret"}', encoding="utf-8")
    (root / "instances.json").write_text('[{"exe_path": "C:\\\\mt5"}]', encoding="utf-8")
    logs = root / "logs" / "node-1"
    logs.mkdir(parents=True)
    (logs / "2026-08-13.log").write_text("noise", encoding="utf-8")
    return root


def test_should_include_excludes_runtime_data():
    assert bp.should_include("node_client_dashboard.exe")
    assert bp.should_include("version.txt")
    assert bp.should_include("README.md")
    assert bp.should_include("_internal/base_library.zip")

    # 后端地址与 NODE_TOKEN，绝不入包
    assert not bp.should_include("panel_config.json")
    # 本机实例清单与绝对路径
    assert not bp.should_include("instances.json")
    assert not bp.should_include(".dashboard.lock")
    assert not bp.should_include("logs/node-1/2026-08-13.log")
    assert not bp.should_include("node_client_dashboard.exe.bak")
    assert not bp.should_include("__pycache__/x.pyc")
    assert not bp.should_include("run.log")
    assert not bp.should_include("")


def test_build_package_excludes_runtime_data(tmp_path: Path):
    zip_path = bp.build_package(_build_dir(tmp_path), tmp_path / "out")

    assert zip_path.name == f"{STEM}.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert f"{STEM}/node_client_dashboard.exe" in names
    assert f"{STEM}/version.txt" in names
    # 令牌与本机拓扑绝不能跟着分发包走
    assert not any(Path(n).name == "panel_config.json" for n in names)
    assert not any(Path(n).name == "instances.json" for n in names)
    assert not any("/logs/" in n for n in names)


def test_build_package_wraps_in_version_folder(tmp_path: Path):
    """包内带一层目录，解压时不会把文件铺一地。"""
    zip_path = bp.build_package(_build_dir(tmp_path), tmp_path / "out")
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    assert names, "package should not be empty"
    assert all(n.startswith(f"{STEM}/") for n in names)


def test_build_package_preserves_onedir_tree(tmp_path: Path):
    root = _build_dir(tmp_path)
    internal = root / "_internal"
    (internal / "tcl").mkdir(parents=True)
    (internal / "python313.dll").write_bytes(b"dll")
    (internal / "tcl" / "init.tcl").write_text("x", encoding="utf-8")

    zip_path = bp.build_package(root, tmp_path / "out")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    # 目录结构用 posix 分隔符，解压侧才能按文件树还原
    assert f"{STEM}/_internal/python313.dll" in names
    assert f"{STEM}/_internal/tcl/init.tcl" in names


def test_build_package_adds_readme(tmp_path: Path):
    doc = tmp_path / "README.md"
    doc.write_text("# 运维面板\n", encoding="utf-8")
    zip_path = bp.build_package(_build_dir(tmp_path), tmp_path / "out", doc=doc)
    with zipfile.ZipFile(zip_path) as zf:
        assert f"{STEM}/README.md" in zf.namelist()


def test_build_package_keeps_doc_already_in_build_dir(tmp_path: Path):
    root = _build_dir(tmp_path)
    (root / "README.md").write_text("FROM_BUILD_DIR", encoding="utf-8")
    doc = tmp_path / "README.md"
    doc.write_text("FROM_TEMPLATE", encoding="utf-8")

    zip_path = bp.build_package(root, tmp_path / "out", doc=doc)
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(f"{STEM}/README.md").decode("utf-8").strip() == "FROM_BUILD_DIR"


def test_build_package_requires_version_txt(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "node_client_dashboard.exe").write_bytes(b"MZ")
    with pytest.raises(FileNotFoundError, match="version.txt"):
        bp.build_package(root, tmp_path / "out")


def test_build_package_requires_entry(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "version.txt").write_text(VERSION + "\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="node_client_dashboard.exe"):
        bp.build_package(root, tmp_path / "out")


def test_build_package_overwrites_same_version(tmp_path: Path):
    """重打同一版本时不能把上一次的成员残留在包里。"""
    root = _build_dir(tmp_path)
    out = tmp_path / "out"
    stale = root / "stale.txt"
    stale.write_text("x", encoding="utf-8")
    bp.build_package(root, out)
    stale.unlink()

    zip_path = bp.build_package(root, out)
    with zipfile.ZipFile(zip_path) as zf:
        assert f"{STEM}/stale.txt" not in zf.namelist()


def test_build_package_rejects_output_inside_build_dir(tmp_path: Path):
    """输出目录在产物目录内部时，下次构建会把上次的 zip 收进包，必须拦住。"""
    root = _build_dir(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        bp.build_package(root, root / "packages")
    with pytest.raises(ValueError, match="outside"):
        bp.build_package(root, root)


def test_verify_package_rejects_leaked_config_and_missing_entry(tmp_path: Path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr(f"{STEM}/node_client_dashboard.exe", b"MZ")
    assert bp.verify_package(good) == (True, "")

    leaked = tmp_path / "leaked.zip"
    with zipfile.ZipFile(leaked, "w") as zf:
        zf.writestr(f"{STEM}/node_client_dashboard.exe", b"MZ")
        zf.writestr(f"{STEM}/panel_config.json", '{"node_token": "secret"}')
    ok, reason = bp.verify_package(leaked)
    assert not ok and "panel_config.json" in reason

    no_entry = tmp_path / "no_entry.zip"
    with zipfile.ZipFile(no_entry, "w") as zf:
        zf.writestr(f"{STEM}/README.md", "hi")
    ok, reason = bp.verify_package(no_entry)
    assert not ok and "node_client_dashboard.exe" in reason


def test_read_version_and_package_name(tmp_path: Path):
    root = _build_dir(tmp_path, "2.0.0-20260101000000")
    assert bp.read_version(root) == "2.0.0-20260101000000"
    assert bp.package_name("2.0.0-20260101000000") == (
        "node_client_dashboard-2.0.0-20260101000000.zip"
    )
    assert bp.read_version(tmp_path / "nope") == ""
