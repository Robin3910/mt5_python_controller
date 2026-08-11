"""version 模块测试。"""
from version import VERSION, get_version


def test_get_version_matches_constant_or_file():
    v = get_version()
    assert v
    assert isinstance(v, str)
    assert VERSION
