"""version / build_version 模块测试：构建版本号 `${数字版本号}-${年月日时分秒}`。"""
import re
from datetime import datetime

import pytest

import build_version as bv
import version as v


def _nums(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s))


def test_make_version_appends_stamp():
    assert v.make_version("1.0.0", datetime(2026, 8, 13, 15, 0, 0)) == "1.0.0-20260813150000"


def test_make_version_orders_by_build_time():
    """晚构建的版本号必须更大，运维时才能一眼看出哪个面板更新。"""
    early = v.make_version("1.0.0", datetime(2026, 8, 13, 9, 0, 0))
    late = v.make_version("1.0.0", datetime(2026, 8, 13, 9, 0, 1))
    assert _nums(early) < _nums(late)
    assert _nums(late) < _nums(v.make_version("1.1.0", datetime(2020, 1, 1)))


def test_normalize_base_rejects_non_numeric():
    assert v.normalize_base(" v1.2 ") == "1.2"
    for bad in ("", "1.0.0-20260813150000", "1.0.0rc1", "abc", "1..2"):
        assert v.normalize_base(bad) == "", bad


def test_make_version_rejects_bad_or_overlong_base():
    with pytest.raises(ValueError):
        v.make_version("1.0.0-beta")
    with pytest.raises(ValueError):
        v.make_version("1234.5678.9012.3456")


def test_make_version_stays_within_limit():
    built = v.make_version(v.BASE_VERSION)
    assert len(built) <= v.MAX_VERSION_LEN


def test_sidecar_version_reads_first_line_without_bom(tmp_path):
    (tmp_path / "version.txt").write_text("\ufeff1.2.3-20260813150000\n附注\n", encoding="utf-8")
    assert v._sidecar_version(tmp_path) == "1.2.3-20260813150000"


def test_sidecar_version_missing_file_returns_empty(tmp_path):
    assert v._sidecar_version(tmp_path) == ""


def test_get_version_prefers_sidecar_then_embedded_then_base(monkeypatch):
    monkeypatch.setattr(v, "_sidecar_version", lambda: "9.9.9-20260813150000")
    monkeypatch.setattr(v, "embedded_version", lambda: "1.0.0-20250101000000")
    assert v.get_version() == "9.9.9-20260813150000"

    monkeypatch.setattr(v, "_sidecar_version", lambda: "")
    assert v.get_version() == "1.0.0-20250101000000"

    monkeypatch.setattr(v, "embedded_version", lambda: "")
    assert v.get_version() == v.BASE_VERSION


def test_render_build_info_is_importable_python():
    ns: dict = {}
    exec(bv.render_build_info("1.0.0-20260813150000", "2026-08-13T15:00:00"), ns)
    assert ns["BUILD_VERSION"] == "1.0.0-20260813150000"
    assert ns["BUILT_AT"] == "2026-08-13T15:00:00"


def test_write_sidecar_matches_get_version(tmp_path):
    bv.write_sidecar(tmp_path, "1.0.0-20260813150000")
    assert v._sidecar_version(tmp_path) == "1.0.0-20260813150000"
