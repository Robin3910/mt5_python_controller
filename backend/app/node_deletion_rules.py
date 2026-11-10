"""节点删除准入的纯规则，不发交易命令。"""


def deletion_blockers(account, *, online, now, active, pending, occupied, last_trade_at=None):
    reasons = []
    if not online:
        reasons.append("节点离线")
    readable = (account is not None and isinstance(account.get("positions"), list)
                and isinstance(account.get("orders"), list) and account.get("books_readable") is True)
    updated_at = float((account or {}).get("updated_at") or 0)
    if not readable or not -5 <= now - updated_at <= 60:
        reasons.append("缺少 60 秒内的可读账户快照")
    if account and account.get("positions"):
        reasons.append("仍有持仓")
    if account and account.get("orders"):
        reasons.append("仍有挂单")
    if last_trade_at is not None and updated_at < last_trade_at:
        reasons.append("账户快照早于最近交易回报，请等待新快照")
    for blocked, message in ((active, "仍有运行中的策略任务"), (pending, "仍有在途交易指令"), (occupied, "仍有执行占位")):
        if blocked:
            reasons.append(message)
    return reasons
