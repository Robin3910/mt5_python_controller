"""成交采集游标：记住「已同步到哪一笔」，重启后不重复也不丢。

游标两个字段：
- `since_ts`：上次同步到的成交时间（Unix 秒）。下次从这里往回看一小段（重叠窗口）。
- `last_deal_id`：上次同步到的最大成交号（MT5 deal ticket）。**真正的去重依据**。

为什么两个都要：
- 只用时间戳 → 同一秒内多笔成交会漏（MT5 的 time 只精确到秒）；
- 只用成交号 → 节点换机 / 券商重置成交号后，新成交的 id 可能比旧的小，会被整段跳过。

所以策略是：**时间戳决定"往哪看"，成交号决定"哪笔算新的"**。
读失败时抛错，绝不推进游标 —— 否则那段历史永久丢失。

游标落本地 JSON 文件（节点侧），键按 MT5 登录号分：一台机器可能绑过多个账号。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("node.deal_cursor")

# 游标文件名（放在程序目录旁，便于运维查看 / 备份）
CURSOR_FILENAME = "deal_cursor.json"


def _state_dir() -> Path:
    """游标文件所在目录：打包后用 exe 目录，源码运行用脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def cursor_path() -> Path:
    return _state_dir() / CURSOR_FILENAME


def _load_all(path: Path) -> dict:
    """读整份游标文件；读不到或格式不对返回空字典（当作首次运行）。"""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("deal cursor file corrupted, starting fresh: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(path: Path, data: dict) -> None:
    """原子写游标文件：先写临时文件再替换，避免进程被杀时留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".deal_cursor_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DealCursor:
    """按 MT5 登录号记录同步游标。

    `last_deal_id` 语义 = **已入库的最大成交号**。新成交号必须严格大于它才算新单。
    """

    def __init__(self, login: int, path: Optional[Path] = None) -> None:
        self.login = int(login or 0)
        self.path = path or cursor_path()
        self._since_ts: float = 0.0
        self._last_deal_id: int = 0
        self._loaded = False

    # ---------------- 持久化 ----------------
    def load(self, *, lookback_days: int = 7) -> None:
        """读游标；首次运行（无记录）时按 lookback_days 回看初始化。

        回看是必须的：节点第一次上线时，账户里已经有历史成交，采集应当从
        "最近 N 天"开始，而不是只从"此刻"开始 —— 否则用户看到的历史订单是空的。
        """
        if self._loaded:
            return
        self._loaded = True
        data = _load_all(self.path)
        entry = data.get(str(self.login)) or {}
        try:
            self._since_ts = float(entry.get("since_ts") or 0.0)
            self._last_deal_id = int(entry.get("last_deal_id") or 0)
        except (TypeError, ValueError):
            self._since_ts = 0.0
            self._last_deal_id = 0
        if self._since_ts <= 0:
            import time as _time
            self._since_ts = _time.time() - max(1, int(lookback_days)) * 86400
            logger.info(
                "deal cursor initialized for login=%s, lookback=%s days",
                self.login, lookback_days,
            )
        else:
            logger.info(
                "deal cursor loaded for login=%s: since_ts=%s last_deal_id=%s",
                self.login, self._since_ts, self._last_deal_id,
            )

    def save(self) -> None:
        """把当前游标写回文件。写失败只告警 —— 采集本身不该因为存盘失败而中断。"""
        data = _load_all(self.path)
        data[str(self.login)] = {
            "since_ts": self._since_ts,
            "last_deal_id": self._last_deal_id,
        }
        try:
            _save_all(self.path, data)
        except OSError:
            logger.warning("failed to persist deal cursor", exc_info=True)

    # ---------------- 读 / 推进 ----------------
    @property
    def since_ts(self) -> float:
        return self._since_ts

    @property
    def last_deal_id(self) -> int:
        return self._last_deal_id

    def advance(self, *, since_ts: float, last_deal_id: int) -> None:
        """推进游标。

        调用方**只在确认这批成交已成功上报**之后才调它 —— 读失败或发送失败时
        必须保持原游标，否则这批成交就再也不会被采集到。
        """
        self._since_ts = max(self._since_ts, float(since_ts))
        self._last_deal_id = max(self._last_deal_id, int(last_deal_id))
        self.save()
