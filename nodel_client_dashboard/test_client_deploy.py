"""客户端部署 / 版本读取测试。"""
from pathlib import Path

from client_deploy import read_version_near, replace_exe_file


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
