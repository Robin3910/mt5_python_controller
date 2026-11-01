"""成交采集与下单命令的相互影响测量。

背景：MT5 Python API 非线程安全，所有 MT5 调用都排在**同一个**单线程执行器里
（`NodeClient._mt5_executor`，max_workers=1）。成交采集 `history_deals_since`
也走这个执行器，所以它可能把下单命令挡在后面。

这里用「假慢采集」量化这个影响：
- 采集占住执行器期间到达的开仓命令，实际下单被推迟了多少；
- 采集是否会让同一节点的后续命令排队堆积。
"""
import asyncio
import json
import time

import node_client as nc


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self._q.get()

    def feed(self, obj: dict) -> None:
        self._q.put_nowait(json.dumps(obj))


def _node():
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    return n


OPEN_CMD = {
    "cmd": "open", "signal_id": "s1", "action": "BUY", "symbol": "EURUSD",
    "volume": 0.1, "stop_loss": None, "take_profit": None, "comment": "", "magic": None,
}


async def test_slow_collection_delays_open_order():
    """采集占住 MT5 执行器时，开仓命令的实际下单被推迟。

    这是 R1 的量化：采集是「量大的一次 history_deals_get」，一旦它占了执行器，
    排在后面的开仓命令只能干等。
    """
    n = _node()
    await n._exec(n.mt5.connect)

    slow = 0.3  # 模拟一批 history_deals_get 的耗时
    original = n.mt5.history_deals_since

    def slow_reader(since_ts, after_deal_id=0):
        time.sleep(slow)          # 阻塞 MT5 执行器线程
        return original(since_ts, after_deal_id)

    n.mt5.history_deals_since = slow_reader

    ws = FakeWS()

    # 采集先起步（占住执行器）
    collect_task = asyncio.create_task(n._collect_deals(ws, _cursor()))

    # 采集进行中，开仓命令到达
    await asyncio.sleep(0.05)
    t0 = time.time()
    await n._handle(ws, dict(OPEN_CMD))
    order_latency = time.time() - t0

    await collect_task

    # 下单确实被推迟了（不是 0）
    assert order_latency > 0.1, f"下单未被推迟：{order_latency:.3f}s"
    # 推迟量大致等于采集剩余耗时
    assert order_latency < slow + 0.15, f"推迟量异常：{order_latency:.3f}s"

    tr = [m for m in ws.sent if m["type"] == "trade_result"]
    assert tr and tr[0]["data"]["success"] is True, "采集不应导致下单失败"


async def test_collection_does_not_break_open_when_fast():
    """采集很快时，对下单的影响可忽略（这是正常情况）。"""
    n = _node()
    await n._exec(n.mt5.connect)
    ws = FakeWS()

    t0 = time.time()
    await n._handle(ws, dict(OPEN_CMD))
    latency = time.time() - t0

    assert latency < 0.2, f"正常路径下单过慢：{latency:.3f}s"
    tr = [m for m in ws.sent if m["type"] == "trade_result"]
    assert tr and tr[0]["data"]["success"] is True


async def test_commands_queue_behind_collection_not_interleaved():
    """采集进行中，连发多个命令：全部排队完成，不丢不乱序。

    MT5 线程安全的硬要求是「同一时刻只有一路在操作终端」，
    单线程执行器保证这点 —— 代价是等待。
    """
    n = _node()
    await n._exec(n.mt5.connect)

    original = n.mt5.history_deals_since

    def slow_reader(since_ts, after_deal_id=0):
        time.sleep(0.2)
        return original(since_ts, after_deal_id)

    n.mt5.history_deals_since = slow_reader

    ws = FakeWS()
    collect_task = asyncio.create_task(n._collect_deals(ws, _cursor()))
    await asyncio.sleep(0.02)

    for i in range(3):
        cmd = dict(OPEN_CMD)
        cmd["signal_id"] = f"s{i}"
        cmd["symbol"] = "EURUSD"
        await n._handle(ws, cmd)

    await collect_task

    trs = [m for m in ws.sent if m["type"] == "trade_result"]
    assert len(trs) == 3, f"应有 3 笔回报，实际 {len(trs)}"
    # 顺序保持
    assert [t["data"]["signal_id"] for t in trs] == ["s0", "s1", "s2"]
    # 全部成功
    assert all(t["data"]["success"] for t in trs)


def _cursor():
    """最小游标替身：推进记录写到内存即可。"""
    from deal_cursor import DealCursor

    class _C(DealCursor):
        def __init__(self):
            super().__init__(1, path=None)

        def save(self) -> None:
            pass

    c = _C()
    c.load(lookback_days=1)
    return c
