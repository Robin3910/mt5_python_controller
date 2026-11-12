"""带单频道展示规则，不执行 I/O 或交易决策。"""


def channel_level(node_count: int, running_days: int) -> tuple[int, str]:
    for level, nodes, days, label in (
        (5, 100, 90, "金牌带单员"), (4, 60, 30, "铂金带单员"),
        (3, 30, 0, "黄金带单员"), (2, 10, 0, "白银带单员"),
    ):
        if node_count >= nodes and running_days >= days:
            return level, label
    return 1, "青铜带单员"


def matches(row: dict, keyword: str) -> bool:
    return keyword.strip().casefold() in " ".join(str(row.get(key) or "") for key in (
        "applicant_name", "follower_user_id", "name", "node_id", "mt5_login",
        "group_name", "strategy_name", "symbol",
    )).casefold()
