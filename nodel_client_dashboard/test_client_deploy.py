"""客户端部署 / 版本读取 / 版本化备份与回滚测试。"""
import zipfile
from pathlib import Path

from client_deploy import (
    DOWNGRADE,
    SAME,
    UPGRADE,
    apply_package,
    backup_current,
    backup_dir_for,
    classify_update,
    compare_versions,
    count_pending_upgrades,
    extract_package,
    is_protected,
    list_local_backups,
    read_version_near,
    replace_exe_file,
    restore_backup,
)


def test_read_version_near(tmp_path: Path):
    exe = tmp_path / "node_client.exe"
    exe.write_bytes(b"old")
    assert read_version_near(exe) == ""
    (tmp_path / "version.txt").write_text("1.2.3\n", encoding="utf-8")
    assert read_version_near(exe) == "1.2.3"


def test_replace_exe_file_backup(tmp_path: Path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "node_client.exe"
    dst = dst_dir / "node_client.exe"
    src.write_bytes(b"new-bin")
    (src_dir / "version.txt").write_text("9.9.9\n", encoding="utf-8")
    dst.write_bytes(b"old-bin")
    (dst_dir / "version.txt").write_text("1.0.0\n", encoding="utf-8")

    ok, msg = replace_exe_file(source_exe=src, target_exe=dst, backup=True)
    assert ok and msg == "ok"
    assert dst.read_bytes() == b"new-bin"
    assert (dst_dir / "version.txt").read_text(encoding="utf-8").startswith("9.9.9")
    assert dst.with_suffix(".exe.bak").read_bytes() == b"old-bin"


# ----------------------------- 版本比较 -----------------------------

def test_compare_versions_pads_and_falls_back():
    assert compare_versions("1.1", "1.1.0") == 0
    assert compare_versions("1.2.0", "1.10.0") < 0
    assert compare_versions("2.0.0", "1.9.9") > 0
    assert compare_versions("alpha", "beta") < 0


def test_classify_update_handles_unknown_side():
    assert classify_update("1.0.0", "1.1.0") == UPGRADE
    assert classify_update("1.1.0", "1.0.0") == DOWNGRADE
    assert classify_update("1.1.0", "1.1.0") == SAME
    # 读不到本机版本时不能误判成升级
    assert classify_update("", "1.1.0") == "unknown"


def test_count_pending_upgrades_counts_only_older_instances():
    versions = ["1.0.0", "1.1.0", "1.2.0"]
    assert count_pending_upgrades(versions, "1.1.0") == 1
    assert count_pending_upgrades(versions, "1.3.0") == 3
    # 目标版本比谁都旧：这是降级，不该提示有更新
    assert count_pending_upgrades(versions, "0.9.0") == 0


def test_count_pending_upgrades_ignores_unreadable_versions():
    """版本号读不出来的实例不计入，否则顶栏提示会一直亮着。"""
    assert count_pending_upgrades(["", "  "], "1.1.0") == 0
    assert count_pending_upgrades(["", "1.0.0"], "1.1.0") == 1


def test_count_pending_upgrades_without_target():
    """后端还没发布任何版本时无从比较。"""
    assert count_pending_upgrades(["1.0.0"], "") == 0


def test_count_pending_upgrades_ignores_padding_difference():
    """1.1 与 1.1.0 是同一个版本，不能算成待更新。"""
    assert count_pending_upgrades(["1.1"], "1.1.0") == 0


def test_is_protected_covers_env_only():
    assert is_protected(".env")
    assert is_protected("sub/.ENV")
    assert not is_protected("node_client.exe")
    assert not is_protected("version.txt")


# ----------------------------- 安装目录 -----------------------------

def _install(tmp_path: Path, version: str = "1.0.0") -> Path:
    """造一个已安装的客户端目录，返回 exe 路径。"""
    d = tmp_path / "install"
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "node_client.exe"
    exe.write_bytes(b"old-bin")
    (d / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    (d / ".env").write_text("NODE_TOKEN=keep-me\n", encoding="utf-8")
    return exe


def _package(tmp_path: Path, version: str = "2.0.0") -> Path:
    """造一个解压后的安装包目录。"""
    d = tmp_path / "pkg"
    (d / "sub").mkdir(parents=True, exist_ok=True)
    (d / "node_client.exe").write_bytes(b"new-bin")
    (d / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    (d / "sub" / "lib.dll").write_bytes(b"lib")
    # 包里带 .env 是典型的打包失误，覆盖时必须忽略
    (d / ".env").write_text("NODE_TOKEN=from-package\n", encoding="utf-8")
    return d


def test_backup_current_stores_exe_and_version_together(tmp_path: Path):
    exe = _install(tmp_path, "1.0.0")
    ok, version, err = backup_current(exe)
    assert ok, err
    assert version == "1.0.0"

    bak = backup_dir_for(exe, "1.0.0")
    assert (bak / "node_client.exe").read_bytes() == b"old-bin"
    # version.txt 必须一起备份，否则回滚后版本号会与实际程序对不上
    assert (bak / "version.txt").read_text(encoding="utf-8").startswith("1.0.0")
    assert list_local_backups(exe) == ["1.0.0"]


def test_apply_package_overwrites_tree_but_keeps_env(tmp_path: Path):
    exe = _install(tmp_path, "1.0.0")
    pkg = _package(tmp_path, "2.0.0")

    ok, msg = apply_package(pkg, exe, backup=True)
    assert ok, msg
    assert exe.read_bytes() == b"new-bin"
    assert read_version_near(exe) == "2.0.0"
    assert (exe.parent / "sub" / "lib.dll").read_bytes() == b"lib"
    # .env 是节点身份，覆盖它等于把节点搞下线
    assert (exe.parent / ".env").read_text(encoding="utf-8") == "NODE_TOKEN=keep-me\n"
    # 覆盖前自动备份了旧版本，供回滚使用
    assert list_local_backups(exe) == ["1.0.0"]


def test_restore_backup_round_trips_version(tmp_path: Path):
    exe = _install(tmp_path, "1.0.0")
    apply_package(_package(tmp_path, "2.0.0"), exe, backup=True)
    assert read_version_near(exe) == "2.0.0"

    ok, msg = restore_backup(exe, "1.0.0")
    assert ok, msg
    assert exe.read_bytes() == b"old-bin"
    assert read_version_near(exe) == "1.0.0"
    assert (exe.parent / ".env").read_text(encoding="utf-8") == "NODE_TOKEN=keep-me\n"
    # 回滚前把 2.0.0 也存了一份，可以再滚回去
    assert set(list_local_backups(exe)) == {"1.0.0", "2.0.0"}

    ok, _ = restore_backup(exe, "9.9.9")
    assert not ok


def test_extract_package_filters_zip_slip(tmp_path: Path):
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("node_client.exe", b"bin")
        zf.writestr("sub/lib.dll", b"lib")
        zf.writestr("../escaped.exe", b"evil")
        zf.writestr("/abs.exe", b"evil")

    dest = tmp_path / "out"
    ok, msg = extract_package(zip_path, dest)
    assert ok, msg
    assert (dest / "node_client.exe").read_bytes() == b"bin"
    assert (dest / "sub" / "lib.dll").read_bytes() == b"lib"
    # 上跳与绝对路径成员被丢弃，不会落到目标目录之外
    assert not (tmp_path / "escaped.exe").exists()
    assert list(dest.rglob("*.exe")) == [dest / "node_client.exe"]


def test_extract_package_rejects_non_zip(tmp_path: Path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    ok, msg = extract_package(bad, tmp_path / "out")
    assert not ok and "zip" in msg
