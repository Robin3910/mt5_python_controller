"""version / build_version 模块测试：构建版本号 `${数字版本号}-${年月日时分秒}`。"""
import re
from datetime import datetime

import pytest

import build_version as bv
import version as v

# 与 backend/app/client_version.py 的 _VERSION_RE 同口径：版本号会进数据库主键与磁盘文件名
_BACKEND_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")


def _nums(s: str) -> tuple[int, ...]:
    """按后端 parse_version 的方式取数字段。"""
    return tuple(int(x) for x in re.findall(r"\d+", s))


def test_make_version_appends_stamp():
    assert v.make_version("1.1.0", datetime(2026, 8, 13, 9, 5, 7)) == "1.1.0-20260813090507"


def test_make_version_fits_backend_constraints():
    built = v.make_version(v.BASE_VERSION)
    assert _BACKEND_RE.match(built), built
    assert len(built) <= v.MAX_VERSION_LEN


def test_make_version_orders_by_build_time():
    # 后端按数字段逐位比较，晚构建的必须更大，否则批量更新会被判成降级
    early = v.make_version("1.1.0", datetime(2026, 8, 13, 9, 0, 0))
    late = v.make_version("1.1.0", datetime(2026, 8, 13, 9, 0, 1))
    assert _nums(early) < _nums(late)
    # 源码直跑上报的裸数字版本号，排在任何构建产物之前（后端会给缺位补零）
    assert _nums("1.1.0") < _nums(early)
    assert _nums(late) < _nums(v.make_version("1.2.0", datetime(2020, 1, 1)))


def test_normalize_base_rejects_non_numeric():
    assert v.normalize_base(" v1.2 ") == "1.2"
    for bad in ("", "1.1.0-20260813090507", "1.1.0rc1", "abc", "1..2"):
        assert v.normalize_base(bad) == "", bad


def test_make_version_rejects_bad_or_overlong_base():
    with pytest.raises(ValueError):
        v.make_version("1.1.0-beta")
    # 数字版本号过长会把整串顶出后端 32 字符上限
    with pytest.raises(ValueError):
        v.make_version("1234.5678.9012.3456")


def test_sidecar_version_reads_first_line_without_bom(tmp_path):
    (tmp_path / "version.txt").write_text("\ufeff1.2.3-20260813090507\n附注\n", encoding="utf-8")
    assert v._sidecar_version(tmp_path) == "1.2.3-20260813090507"


def test_sidecar_version_missing_file_returns_empty(tmp_path):
    assert v._sidecar_version(tmp_path) == ""


def test_get_version_prefers_sidecar_then_embedded_then_base(monkeypatch):
    monkeypatch.setattr(v, "_sidecar_version", lambda: "9.9.9-20260813090507")
    monkeypatch.setattr(v, "embedded_version", lambda: "1.0.0-20250101000000")
    assert v.get_version() == "9.9.9-20260813090507"

    monkeypatch.setattr(v, "_sidecar_version", lambda: "")
    assert v.get_version() == "1.0.0-20250101000000"

    monkeypatch.setattr(v, "embedded_version", lambda: "")
    assert v.get_version() == v.BASE_VERSION


def test_render_build_info_is_importable_python():
    ns: dict = {}
    exec(bv.render_build_info("1.1.0-20260813090507", "2026-08-13T09:05:07"), ns)
    assert ns["BUILD_VERSION"] == "1.1.0-20260813090507"
    assert ns["BUILT_AT"] == "2026-08-13T09:05:07"


def test_write_sidecar_matches_get_version(tmp_path):
    bv.write_sidecar(tmp_path, "1.1.0-20260813090507")
    assert v._sidecar_version(tmp_path) == "1.1.0-20260813090507"
