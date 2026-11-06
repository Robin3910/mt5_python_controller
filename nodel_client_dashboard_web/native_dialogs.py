"""GUI 线程上的原生对话框。

为什么需要这一层：
    pywebview 的 `window.create_file_dialog` 在 winforms 后端里**没有**做
    `Invoke` 转发（见 `webview/platforms/winforms.py` 的 `create_file_dialog`）。
    从后台线程直接调用会**挂死**——实测弹窗不出现，线程永久阻塞。

    而桥接层的 HTTP 处理线程是后台线程，前端点「浏览…」正是走 HTTP。
    所以必须把调用 marshal 到 GUI 线程：

        window.native.Invoke(Action(fn))

    `window.native` 是 WinForms 的 `BrowserForm` 实例，
    `Invoke` 会把委托投递到 GUI 线程的消息循环里执行。

注意：`Invoke` 是同步的——调用线程会一直等到 `fn` 返回。
原生对话框是模态的（用户选完才返回），所以这正是想要的语义。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

# 记录当前窗口，供 marshal 使用。set_window() 在窗口创建后调用一次。
_window: Any = None
_window_lock = threading.RLock()


class NativeDialogError(RuntimeError):
    """原生对话框无法展示（窗口未就绪 / GUI 线程 marshal 失败）。"""


def set_window(window: Any) -> None:
    """登记 pywebview 窗口，供后续 marshal 使用。"""
    global _window
    with _window_lock:
        _window = window


def get_window() -> Any:
    """取当前窗口。优先用登记的窗口；没登记就找 pywebview 的第一个窗口。

    自动兜底是必要的：窗口创建与 `set_window()` 调用之间有间隙，
    而 HTTP 请求可能在这个间隙里就来了。
    """
    with _window_lock:
        window = _window
    if window is not None:
        return window

    try:
        import webview

        windows = list(getattr(webview, "windows", []) or [])
        if windows:
            return windows[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def _marshal(fn: Callable[[], Any]) -> Any:
    """把 fn 投递到 GUI 线程执行，并同步等待结果。"""
    window = get_window()
    if window is None:
        raise NativeDialogError("窗口尚未就绪，无法弹出原生对话框")

    native = _wait_for_native(window)
    if native is None:
        raise NativeDialogError("窗口尚未就绪，无法弹出原生对话框")

    try:
        import clr

        clr.AddReference("System.Windows.Forms")
        from System import Action
    except Exception as exc:  # noqa: BLE001
        raise NativeDialogError(f"无法初始化 GUI 线程调用：{exc}") from exc

    box: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            box.append(fn())
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    try:
        native.Invoke(Action(runner))
    except Exception as exc:  # noqa: BLE001
        raise NativeDialogError(f"原生对话框调用失败：{exc}") from exc

    if error:
        raise error[0]
    return box[0] if box else None


def _wait_for_native(window: Any, timeout: float = 10.0) -> Any:
    """等窗口的 WinForms 句柄就绪。

    `window.native` 在 BrowserForm 构造后才被赋值，窗口刚创建时是 None。
    """
    deadline = time.monotonic() + timeout
    native = getattr(window, "native", None)
    while native is None and time.monotonic() < deadline:
        time.sleep(0.1)
        native = getattr(window, "native", None)
    return native


def pick_folder(initial_dir: str = "") -> str | None:
    """弹出系统目录选择器；用户取消返回 None。

    必须在 GUI 线程上真正弹窗，所以这里做 marshal。
    """
    import webview

    def do_pick() -> str | None:
        picked = window_create_file_dialog(initial_dir)
        if not picked:
            return None
        # pywebview 返回 (path,) 或 (path1, path2, ...)
        return str(picked[0]) if picked else None

    def window_create_file_dialog(initial: str) -> Sequence[str] | None:
        dialog_type = getattr(webview, "FileDialog", webview)
        folder = getattr(dialog_type, "FOLDER", webview.FOLDER_DIALOG)
        return get_window().create_file_dialog(folder, directory=initial or "")

    result = _marshal(do_pick)
    return result if isinstance(result, str) else None


def confirm(title: str, message: str) -> bool:
    """弹出系统确认框；用户确定返回 True。"""
    def do_confirm() -> bool:
        return bool(get_window().create_confirmation_dialog(title, message))

    return bool(_marshal(do_confirm))
