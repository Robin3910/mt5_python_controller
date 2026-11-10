"""频道展示规则；交易准入仍由现有跟单规则负责。"""


def channel_level(node_count: int, running_days: int, running_robots: int) -> tuple[int, str]:
    for level, nodes, days, label in (
        (6, 200, 365, "旗舰频道"), (5, 100, 180, "专业频道"),
        (4, 50, 60, "认证频道"), (3, 20, 0, "优选频道"),
    ):
        if node_count >= nodes and running_days >= days:
            return level, label
    return (2 if running_robots else 1), "智能交易频道"


def application_matches(row: dict, keyword: str) -> bool:
    text = " ".join(str(value or "") for value in (
        row["robot_name"], row["group_id"], row["applicant_name"], row["follower_user_id"],
        row["symbol"], *(node.get("mt5_login") for node in row["nodes"]),
        *(node.get("name") for node in row["nodes"]),
    ))
    return keyword.strip().casefold() in text.casefold()
