"""转圈状态指示：把一个标签变成 loading 动画。

导入节点对话框原先自带一套 Braille 风车（`_BUSY_FRAMES` + `_start_busy_status`），
版本更新对话框却只有静态文字「更新中…」，用户分不清程序是在跑还是卡死了。
两者统一到本模块：帧表、节流规则、以及「旧动画自检失效」的并发处理都收在这里，
调用方只管 start / update / stop。

三个关键约束：

1. **动画代数（token）**：每次 start / stop 都自增计数，定时回调发现代数变了就
   静默退出。否则「换阶段时再 start 一次」会留下两个 after 循环互相覆盖文案。
2. **update 不重启动画**：只换文案，动画继续转，阶段切换时不会闪一下。
3. **窗口销毁后不写控件**：写之前先 `winfo_exists()`，再包一层 `tk.TclError`。
   对话框被关掉后，遗留的 after 回调照样会触发一次。

`owner` 决定用谁来排定时器：传对话框（而不是标签）能沿用 `SessionDialog.after`
的会话代次检查 —— 登录失效后动画自然停掉，不会继续往旧会话的界面上写。
"""
from __future__ import annotations

import time
import tkinter as tk

# 状态条 loading 动画：8 帧 Braille 旋转（风车型），跨 Windows/macOS/Linux 都能显示。
# 100ms 一帧才像"在转"；再慢就感觉卡顿。
FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧")
TICK_MS = 100


def spin_text(text: str, frame: int = 0) -> str:
    """把文案和动画帧拼成一行；frame 超出帧数时循环取模。"""
    return f"{FRAMES[frame % len(FRAMES)]}  {text}"


class BusyStatus:
    """把一个标签变成转圈状态指示器。

    - `start(text)` 开启动画；重复调用会作废旧动画，不会出现两个循环互相覆盖。
    - `update(text)` 只换文案，动画不中断。
    - `stop(text, color)` 停掉动画并写入终态文案；`text` 为空时保留标签原样。
    """

    def __init__(self, label, *, owner=None, tick_ms: int = TICK_MS) -> None:
        self._label = label
        self._owner = owner if owner is not None else label
        self._tick_ms = tick_ms
        self._token = 0
        self._text = ""
        self._color: str | None = None
        self._frame = 0
        self._running = False
        self._timer: str | None = None

    # ---------------------------------------------------------------- 内部

    def _cancel_timer(self) -> None:
        """撤掉待执行的帧回调。

        窗口被销毁时，Tk 会删掉该窗口的 after 脚本；残留回调触发时报
        `invalid command name ...` 打到 stderr，还会把测试的输出弄脏。
        """
        if self._timer is None:
            return
        try:
            self._owner.after_cancel(self._timer)
        except (tk.TclError, RuntimeError, TypeError):
            pass
        self._timer = None

    def _schedule(self, fn):
        self._cancel_timer()
        try:
            self._timer = self._owner.after(self._tick_ms, fn)
        except (tk.TclError, RuntimeError):
            self._timer = None
            return None
        return self._timer

    def _render(self, token: int) -> None:
        """动画帧回调。代数不匹配说明已被新的 start/stop 取代 → 静默退出。"""
        if token != self._token or not self._running:
            return
        try:
            if not self._label.winfo_exists():
                self._running = False
                return
            kwargs = {"text": spin_text(self._text, self._frame)}
            if self._color:
                kwargs["text_color"] = self._color
            self._label.configure(**kwargs)
        except tk.TclError:
            self._running = False
            return
        self._frame += 1
        self._schedule(lambda: self._render(token))

    # ---------------------------------------------------------------- 对外

    @property
    def running(self) -> bool:
        return self._running

    def start(self, text: str, color: str | None = None) -> None:
        """开启动画并显示首帧。"""
        self._token += 1
        self._cancel_timer()
        self._text = text
        self._color = color
        self._frame = 0
        self._running = True
        self._render(self._token)

    def update(self, text: str, color: str | None = None) -> None:
        """换文案。动画若在跑就继续转；没在跑则只写一次静态文案。

        动画在跑时也立即写一次（帧号不变），这样阶段切换不用等下一帧才显示；
        之后每一帧仍会重写，转圈照常。

        没在跑的时候写纯文本（不带转圈字符）—— 一个静止的转圈字符看起来
        恰恰像「卡住了」。
        """
        self._text = text
        if color is not None:
            self._color = color
        try:
            if not self._label.winfo_exists():
                return
            if self._running:
                kwargs = {"text": spin_text(self._text, self._frame)}
            else:
                kwargs = {"text": text}
            if self._color:
                kwargs["text_color"] = self._color
            self._label.configure(**kwargs)
        except tk.TclError:
            self._running = False

    def stop(self, text: str = "", color: str | None = None) -> None:
        """停掉动画。给了 text 就写入终态文案（失败时用红色，成功时用强调色）。"""
        self._token += 1
        self._running = False
        self._cancel_timer()
        if not text:
            return
        try:
            kwargs = {"text": text}
            if color:
                kwargs["text_color"] = color
            self._label.configure(**kwargs)
        except tk.TclError:
            pass

    def clear(self) -> None:
        """停动画并清空标签文案。"""
        self._token += 1
        self._running = False
        self._cancel_timer()
        try:
            self._label.configure(text="")
        except tk.TclError:
            pass

    def destroy(self) -> None:
        """只停动画，不动标签文案（控件马上要销毁时用）。"""
        self._token += 1
        self._running = False
        self._cancel_timer()


def format_bytes(size: int) -> str:
    """字节数转人类可读文本。"""
    value = max(0, int(size))
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


class ThrottledProgress:
    """把「高频进度回调」节流成「低频 UI 更新」。

    下载是 64KB 一块，几百 MB 的包能触发几千次回调。若每次都 `after(0, ...)`，
    Tk 事件队列会被灌满，界面反而更卡。所以只在两个条件之一成立时才通知：

    - 百分比发生了变化（整数百分点）；
    - 距上次通知已超过 `min_interval` 毫秒（用于没有总量的场景）。

    `notify` 由调用方提供，负责把文案写到界面（通常已经切回主线程）。
    """

    def __init__(self, notify, *, min_interval: float = 0.2) -> None:
        self._notify = notify
        self._min_interval = min_interval
        self._last_pct: int | None = None
        self._last_at: float = 0.0

    def report(self, done: int, total: int) -> bool:
        """上报一次进度；返回 True 表示这次触发了通知。"""
        now = time.monotonic()
        if total > 0:
            # 有总量：整数百分点变化就通知一次
            pct = int(done * 100 // total)
            if pct == self._last_pct:
                return False
            self._last_pct = pct
            self._last_at = now
            self._notify(done, total)
            return True
        # 无总量：按最小间隔节流
        if now - self._last_at < self._min_interval:
            return False
        self._last_at = now
        self._notify(done, total)
        return True

    def reset(self) -> None:
        self._last_pct = None
        self._last_at = 0.0
