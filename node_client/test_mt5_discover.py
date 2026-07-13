"""Tests for MT5 terminal discovery next to the running program."""
from pathlib import Path

import pytest

from mt5_discover import discover_mt5_terminal


def test_discover_prefers_terminal64(tmp_path: Path):
    (tmp_path / "terminal.exe").write_bytes(b"x")
    preferred = tmp_path / "terminal64.exe"
    preferred.write_bytes(b"x")
    assert discover_mt5_terminal(tmp_path) == preferred.resolve()


def test_discover_falls_back_to_terminal(tmp_path: Path):
    only = tmp_path / "terminal.exe"
    only.write_bytes(b"x")
    assert discover_mt5_terminal(tmp_path) == only.resolve()


def test_discover_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as ei:
        discover_mt5_terminal(tmp_path)
    msg = str(ei.value)
    assert "未在程序目录找到 MT5 终端" in msg
    assert str(tmp_path.resolve()) in msg
