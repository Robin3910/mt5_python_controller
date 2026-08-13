"""构建期打包测试：安装包结构与 .env 排除。"""
import zipfile
from pathlib import Path

import pytest

import build_package as bp


def _onefile_build(tmp_path: Path, version: str = "1.1.0-20260813103000") -> Path:
    """造一个 onefile 形态的产物目录。"""
    root = tmp_path / "dist"
    root.mkdir(parents=True, exist_ok=True)
    (root / "node_client.exe").write_bytes(b"MZ fake")
    (root / "version.txt").write_text(version + "\n", encoding="utf-8")
    # 构建脚本会把本机 .env 复制到产物目录，它绝不能进包
    (root / ".env").write_text("NODE_TOKEN=real-secret\n", encoding="utf-8")
    return root


def test_should_include_excludes_env_and_noise():
    assert bp.should_include("node_client.exe")
    assert bp.should_include("version.txt")
    assert bp.should_include("_internal/base_library.zip")
    assert bp.should_include(".env.example")

    # 节点身份，绝不入包
    assert not bp.should_include(".env")
    # 面板生成的版本化备份与旧单槽备份
    assert not bp.should_include(".backups/1.0.0/node_client.exe")
    assert not bp.should_include("node_client.exe.bak")
    assert not bp.should_include("__pycache__/x.pyc")
    assert not bp.should_include("run.log")
    assert not bp.should_include("")


def test_build_package_excludes_env_and_keeps_entry(tmp_path: Path):
    root = _onefile_build(tmp_path)
    zip_path = bp.build_package(root, tmp_path / "out")

    assert zip_path.name == "node_client-1.1.0-20260813103000.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "node_client.exe" in names
    assert "version.txt" in names
    # 真实令牌绝不能被打进上传到后端的包里
    assert ".env" not in names
    assert not any(n.endswith("/.env") for n in names)


def test_build_package_adds_env_example_template(tmp_path: Path):
    root = _onefile_build(tmp_path)
    template = tmp_path / ".env.example"
    template.write_text("MANAGER_WS_URL=ws://localhost:8000/ws/node\n", encoding="utf-8")

    zip_path = bp.build_package(root, tmp_path / "out", template=template)
    with zipfile.ZipFile(zip_path) as zf:
        assert ".env.example" in zf.namelist()
        body = zf.read(".env.example").decode("utf-8")
    assert "MANAGER_WS_URL" in body


def test_build_package_keeps_template_already_in_build_dir(tmp_path: Path):
    """产物目录自带模板时不被外部模板覆盖。"""
    root = _onefile_build(tmp_path)
    (root / ".env.example").write_text("FROM_BUILD_DIR=1\n", encoding="utf-8")
    template = tmp_path / ".env.example"
    template.write_text("FROM_TEMPLATE=1\n", encoding="utf-8")

    zip_path = bp.build_package(root, tmp_path / "out", template=template)
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(".env.example").decode("utf-8").strip() == "FROM_BUILD_DIR=1"


def test_build_package_preserves_onedir_tree(tmp_path: Path):
    root = _onefile_build(tmp_path)
    internal = root / "_internal"
    internal.mkdir()
    (internal / "python313.dll").write_bytes(b"dll")
    (internal / "numpy").mkdir()
    (internal / "numpy" / "core.pyd").write_bytes(b"pyd")

    zip_path = bp.build_package(root, tmp_path / "out")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    # 目录结构用 posix 分隔符，解压侧才能按文件树还原
    assert "_internal/python313.dll" in names
    assert "_internal/numpy/core.pyd" in names


def test_build_package_requires_version_txt(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "node_client.exe").write_bytes(b"MZ")
    with pytest.raises(FileNotFoundError, match="version.txt"):
        bp.build_package(root, tmp_path / "out")


def test_build_package_requires_entry(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "version.txt").write_text("1.0.0\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="node_client.exe"):
        bp.build_package(root, tmp_path / "out")


def test_build_package_overwrites_same_version(tmp_path: Path):
    """重打同一版本时不能把上一次的成员残留在包里。"""
    root = _onefile_build(tmp_path)
    out = tmp_path / "out"
    stale = root / "stale.txt"
    stale.write_text("x", encoding="utf-8")
    bp.build_package(root, out)
    stale.unlink()

    zip_path = bp.build_package(root, out)
    with zipfile.ZipFile(zip_path) as zf:
        assert "stale.txt" not in zf.namelist()


def test_build_package_rejects_output_inside_build_dir(tmp_path: Path):
    """输出目录在产物目录内部时，下次构建会把上次的 zip 收进包，必须拦住。"""
    root = _onefile_build(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        bp.build_package(root, root / "packages")
    with pytest.raises(ValueError, match="outside"):
        bp.build_package(root, root)


def test_verify_package_rejects_env_and_missing_entry(tmp_path: Path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("node_client.exe", b"MZ")
    assert bp.verify_package(good) == (True, "")

    leaked = tmp_path / "leaked.zip"
    with zipfile.ZipFile(leaked, "w") as zf:
        zf.writestr("node_client.exe", b"MZ")
        zf.writestr(".env", "NODE_TOKEN=secret")
    ok, reason = bp.verify_package(leaked)
    assert not ok and ".env" in reason

    no_entry = tmp_path / "no_entry.zip"
    with zipfile.ZipFile(no_entry, "w") as zf:
        zf.writestr("readme.txt", "hi")
    ok, reason = bp.verify_package(no_entry)
    assert not ok and "node_client.exe" in reason


def test_read_version_and_package_name(tmp_path: Path):
    root = _onefile_build(tmp_path, "2.0.0-20260101000000")
    assert bp.read_version(root) == "2.0.0-20260101000000"
    assert bp.package_name("2.0.0-20260101000000") == "node_client-2.0.0-20260101000000.zip"
    assert bp.read_version(tmp_path / "nope") == ""


def test_package_version_stays_within_backend_limit():
    """包名里的版本号必须能通过后端的版本号校验（≤32 字符、字符集受限）。"""
    from version import MAX_VERSION_LEN, make_version

    version = make_version("1.1.0")
    assert len(version) <= MAX_VERSION_LEN
    assert bp.package_name(version).endswith(".zip")
