"""busy_status：转圈状态指示与进度节流。

不依赖 Tk 也能测的部分（帧表、字节格式化、节流规则）全部用纯函数验证；
Tk 相关的部分用假标签对象验证「只写一次、动画代数自检、销毁后不写」。
"""
from __future__ import annotations

import tkinter as tk

import pytest

from busy_status import FRAMES, BusyStatus, ThrottledProgress, format_bytes, spin_text


class _FakeLabel:
    """够用的标签替身：记录写入过的文字与颜色。"""

    def __init__(self, exists: bool = True) -> None:
        self.text = ""
        self.color: str | None = None
        self._exists = exists
        self.writes: list[str] = []

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.text = kwargs["text"]
            self.writes.append(kwargs["text"])
        if "text_color" in kwargs:
            self.color = kwargs["text_color"]

    def winfo_exists(self) -> bool:
        return self._exists


class _FakeOwner:
    """假的定时器宿主：手动推进帧。"""

    def __init__(self) -> None:
        self.pending: list = []
        self.cancelled: list = []

    def after(self, _ms, fn):
        self.pending.append(fn)
        return f"timer-{len(self.pending)}"

    def after_cancel(self, timer) -> None:
        self.cancelled.append(timer)

    def tick(self) -> None:
        """执行当前排队的回调（一次一帧）。"""
        if not self.pending:
            return
        fn = self.pending.pop(0)
        fn()


# ---------------------------------------------------------------- 纯函数


def test_spin_text_cycles_frames():
    assert spin_text("下载中", 0).endswith("下载中")
    assert spin_text("下载中", 0).startswith(FRAMES[0])
    # 帧号超出帧数时循环取模
    assert spin_text("x", len(FRAMES)) == spin_text("x", 0)
    assert spin_text("x", len(FRAMES) + 1) == spin_text("x", 1)


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
    # 负数不炸
    assert format_bytes(-1) == "0 B"


# ---------------------------------------------------------------- BusyStatus


def test_busy_status_writes_spinning_text():
    label = _FakeLabel()
    owner = _FakeOwner()
    busy = BusyStatus(label, owner=owner)

    busy.start("正在下载")
    assert label.text.startswith(FRAMES[0])
    assert "正在下载" in label.text

    owner.tick()
    assert label.text.startswith(FRAMES[1])


def test_busy_status_restart_does_not_leave_two_animations():
    label = _FakeLabel()
    owner = _FakeOwner()
    busy = BusyStatus(label, owner=owner)

    busy.start("第一阶段")
    busy.start("第二阶段")
    # 旧动画的回调即使被触发，也不能覆盖新动画的文案
    owner.tick()
    assert "第二阶段" in label.text


def test_busy_status_update_keeps_animation_running():
    label = _FakeLabel()
    owner = _FakeOwner()
    busy = BusyStatus(label, owner=owner)

    busy.start("下载中")
    owner.tick()
    busy.update("解压中")
    # 动画没停：仍在转，且文案换成了新阶段
    assert busy.running is True
    assert "解压中" in label.text
    owner.tick()
    assert label.text.startswith(FRAMES[2])


def test_busy_status_update_without_animation_writes_plain_text():
    label = _FakeLabel()
    busy = BusyStatus(label, owner=_FakeOwner())

    busy.update("静态文案")
    # 没在转的时候写纯文本：一个静止的转圈字符看起来像卡住了
    assert label.text == "静态文案"


def test_busy_status_stop_writes_final_text_and_stops():
    label = _FakeLabel()
    owner = _FakeOwner()
    busy = BusyStatus(label, owner=owner)

    busy.start("处理中")
    busy.stop("✓ 完成", "#2EE6A8")
    assert label.text == "✓ 完成"
    assert label.color == "#2EE6A8"
    assert busy.running is False

    # 停掉后残留的回调不能再写
    before = label.text
    owner.tick()
    assert label.text == before


def test_busy_status_does_not_write_to_destroyed_label():
    label = _FakeLabel(exists=False)
    owner = _FakeOwner()
    busy = BusyStatus(label, owner=owner)

    busy.start("处理中")
    owner.tick()  # 窗口已销毁：不能抛异常
    assert busy.running is False


def test_busy_status_clear_empties_label():
    label = _FakeLabel()
    busy = BusyStatus(label, owner=_FakeOwner())
    busy.start("处理中")
    busy.clear()
    assert label.text == ""
    assert busy.running is False


def test_busy_status_cancels_pending_timer_on_stop():
    """停掉动画要撤掉待执行的帧回调 —— 否则窗口销毁后回调照样触发并报错。"""
    owner = _FakeOwner()
    busy = BusyStatus(_FakeLabel(), owner=owner)
    busy.start("处理中")
    assert owner.pending, "应该排了一个帧回调"
    busy.stop("完成")
    assert owner.cancelled, "stop 应该撤掉待执行的回调"


def test_busy_status_cancel_survives_destroyed_owner():
    """宿主窗口已销毁时，after_cancel 抛 TclError 也不能影响流程。"""
    class _DeadOwner:
        def after(self, _ms, _fn):
            raise tk.TclError("invalid command name")

        def after_cancel(self, _timer):
            raise tk.TclError("invalid command name")

    busy = BusyStatus(_FakeLabel(), owner=_DeadOwner())
    busy.start("处理中")   # 排不上定时器也不该炸
    busy.stop("完成")
    assert busy.running is False


# ---------------------------------------------------------------- 进度节流


def test_throttled_progress_notifies_once_per_percent():
    seen: list[tuple[int, int]] = []
    throttle = ThrottledProgress(lambda done, total: seen.append((done, total)))

    # 1000 字节总量，每 10 字节一次回调：只有整数百分点变化才通知
    for done in range(0, 1001, 10):
        throttle.report(done, 1000)
    assert seen, "应该有进度通知"
    pcts = [done * 100 // 1000 for done, _ in seen]
    assert pcts == sorted(pcts)
    assert len(pcts) == len(set(pcts)), "同一个百分点不该重复通知"
    assert pcts[-1] == 100


def test_throttled_progress_without_total_uses_interval():
    seen: list[tuple[int, int]] = []
    throttle = ThrottledProgress(lambda done, total: seen.append((done, total)), min_interval=60.0)

    throttle.report(1024, 0)
    assert len(seen) == 1
    # 间隔内的重复上报被丢掉
    throttle.report(2048, 0)
    assert len(seen) == 1
    # 间隔到期后恢复通知
    throttle._last_at -= 61.0
    throttle.report(3072, 0)
    assert len(seen) == 2
    assert seen[-1] == (3072, 0)


def test_throttled_progress_reset():
    seen: list[tuple[int, int]] = []
    throttle = ThrottledProgress(lambda done, total: seen.append((done, total)))
    throttle.report(500, 1000)
    throttle.reset()
    throttle.report(500, 1000)
    assert len(seen) == 2, "重置后同一百分比应重新通知"


# ---------------------------------------------------------------- Tk 集成


@pytest.mark.skipif(not tk.Tk, reason="需要 Tk 环境")
def test_busy_status_with_real_tk_label():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    label = tk.Label(root, text="")
    try:
        busy = BusyStatus(label, owner=root)
        busy.start("正在下载")
        assert "正在下载" in label.cget("text")
        busy.stop("完成")
        assert label.cget("text") == "完成"
    finally:
        root.destroy()
