import pytest

from app.ai_channel_rules import channel_level


@pytest.mark.parametrize("nodes, days, robots, expected", [
    (0, 0, 0, (1, "智能交易频道")),
    (0, 0, 1, (2, "智能交易频道")),
    (19, 400, 1, (2, "智能交易频道")),
    (20, 0, 0, (3, "优选频道")),
    (50, 59, 0, (3, "优选频道")),
    (50, 60, 0, (4, "认证频道")),
    (100, 179, 1, (4, "认证频道")),
    (100, 180, 1, (5, "专业频道")),
    (200, 364, 1, (5, "专业频道")),
    (199, 365, 1, (5, "专业频道")),
    (200, 365, 1, (6, "旗舰频道")),
])
def test_channel_level_uses_confirmed_node_and_day_thresholds(nodes, days, robots, expected):
    assert channel_level(nodes, days, robots) == expected
