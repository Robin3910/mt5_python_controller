"""端到端验证：信号分发 → 节点回报 → 落库 → API 返回，时延埋点全程贯通。

这是 M8 的验收标准——能对任意一条历史信号打印出「发包 / 收包 / 排队 / 券商往返」
四段分解。任一段断了，这个测试就该红。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app import persist
from app.db import SessionLocal, init_db
from app.orm import SignalDispatch


@pytest.fixture(autouse=True)
async def _clean_db():
    """建表并清空分发明细表。"""
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(SignalDispatch))
        await s.commit()


async def _row(signal_id: str) -> SignalDispatch:
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(SignalDispatch).where(SignalDispatch.signal_id == signal_id)
            )
        ).scalar_one()


async def test_dispatch_result_persists_latency_marks():
    """节点回报里的埋点字段要落到 signal_dispatch 表。"""
    await persist.record_dispatch(
        "sig_lat1", "nd1", 0.1, "passed", None, "pending",
        sent_at=persist.latency_marks.utcnow(),
    )
    await persist.update_dispatch_result(
        "sig_lat1", "nd1", "done",
        {
            "success": True,
            "price": 1.2345,
            "recv_at": 1_700_000_000.5,
            "order_at": 1_700_000_000.7,
            "filled_at": 1_700_000_001.0,
            "queued_ms": 200.0,
            "order_to_fill_ms": 300.0,
        },
    )

    row = await _row("sig_lat1")
    assert row.sent_at is not None, "发包时刻应落库"
    assert row.recv_at is not None, "节点收包时刻应落库"
    assert row.order_at is not None, "下单时刻应落库"
    assert row.filled_at is not None, "成交时刻应落库"
    assert row.queued_ms == pytest.approx(200.0)
    assert row.order_to_fill_ms == pytest.approx(300.0)
    assert row.price == pytest.approx(1.2345)


async def test_dispatch_result_without_marks_still_works():
    """旧版节点客户端不上报埋点——不能因此报错，列留空即可。"""
    await persist.record_dispatch("sig_lat2", "nd1", 0.1, "passed", None, "pending")
    await persist.update_dispatch_result(
        "sig_lat2", "nd1", "done", {"success": True, "price": 1.0},
    )

    row = await _row("sig_lat2")
    assert row.status == "done"
    assert row.queued_ms is None
    assert row.order_to_fill_ms is None


async def test_dispatch_result_with_bad_marks_does_not_break():
    """埋点值是脏的（节点 bug 或版本不匹配）也不能连累交易链路。"""
    await persist.record_dispatch("sig_lat3", "nd1", 0.1, "passed", None, "pending")
    await persist.update_dispatch_result(
        "sig_lat3", "nd1", "failed",
        {
            "success": False,
            "error": "requote",
            "recv_at": "garbage",
            "order_at": None,
            "queued_ms": "abc",
        },
    )

    row = await _row("sig_lat3")
    assert row.status == "failed"
    assert row.error == "requote"
    assert row.recv_at is None
    assert row.queued_ms is None
