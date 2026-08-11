"""运维面板 ViewModel：从进程态生成可比较快照，供 UI 差分绑定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from models import default_label_from_path, shorten_path

if TYPE_CHECKING:
    from process_manager import ManagedProcess, ProcessManager


def health_zh(health: str | None) -> str:
    return {
        "ok": "正常",
        "starting": "启动中",
        "dead": "已停止",
        "error": "错误",
        "unknown": "未知",
    }.get((health or "").lower(), health or "未知")


def ws_zh(ws_state: str | None) -> str:
    return {
        "starting": "启动中",
        "connecting": "连接中",
        "authenticated": "已鉴权",
        "reconnecting": "重连中",
        "stopping": "停止中",
    }.get((ws_state or "").lower(), ws_state or "无")


@dataclass(frozen=True)
class ListItemVM:
    id: str
    title: str
    path_short: str
    status_line: str
    alive: bool
    health: str
    selected: bool
    daemon: bool


@dataclass(frozen=True)
class ChipVM:
    key: str
    text: str
    tone: str  # health|cyan|muted|accent|warning|dead
    visible: bool = True


@dataclass(frozen=True)
class DetailVM:
    empty: bool
    title: str
    subtitle: str
    daemon: bool
    summary: str
    tasks_text: str
    alive: bool
    health: str
    busy: bool
    chips: tuple[ChipVM, ...]


def process_state_label(mp: "ManagedProcess | None") -> str:
    if mp is None:
        return "已停止"
    if mp.runtime.busy == "starting":
        return "启动中"
    if mp.runtime.busy == "stopping":
        return "停止中"
    return "运行中" if mp.runtime.process_alive else "已停止"


def build_list_item(mp: "ManagedProcess", selected_id: str | None) -> ListItemVM:
    cfg = mp.cfg
    rt = mp.runtime
    title = cfg.name or default_label_from_path(cfg.exe_path)
    state = process_state_label(mp)
    daemon = "  ·  守护" if cfg.daemon else ""
    status_line = f"{state}  ·  {health_zh(rt.health)}{daemon}"
    if rt.version:
        status_line = f"v{rt.version}  ·  {status_line}"
    return ListItemVM(
        id=cfg.id,
        title=title,
        path_short=shorten_path(cfg.exe_path, 40),
        status_line=status_line,
        alive=bool(rt.process_alive),
        health=rt.health or "unknown",
        selected=cfg.id == selected_id,
        daemon=bool(cfg.daemon),
    )


def build_list(manager: "ProcessManager", selected_id: str | None) -> tuple[ListItemVM, ...]:
    items: list[ListItemVM] = []
    for cfg in manager.configs():
        mp = manager.get(cfg.id)
        if mp is None:
            continue
        items.append(build_list_item(mp, selected_id))
    return tuple(items)


def build_detail(mp: "ManagedProcess | None") -> DetailVM:
    if mp is None:
        return DetailVM(
            empty=True,
            title="未选择实例",
            subtitle="请选择左侧实例，查看安全状态与运行快照",
            daemon=False,
            summary="",
            tasks_text="",
            alive=False,
            health="unknown",
            busy=False,
            chips=(ChipVM("empty", "未选择", "dead", True),),
        )
    cfg = mp.cfg
    rt = mp.runtime
    alive = bool(rt.process_alive)
    chips = (
        ChipVM("online", "在线" if alive else "离线", "health"),
        ChipVM("health", f"健康 {health_zh(rt.health)}", "health"),
        ChipVM("ws", f"连接 {ws_zh(rt.ws_state)}", "cyan" if alive else "dead"),
        ChipVM("port", f"端口 {cfg.status_port}", "muted"),
        ChipVM(
            "version",
            f"版本 {rt.version}" if rt.version else "",
            "accent",
            visible=bool(rt.version),
        ),
        ChipVM("daemon", "守护已开", "warning", visible=bool(cfg.daemon)),
        ChipVM(
            "busy",
            "启动中" if rt.busy == "starting" else ("停止中" if rt.busy == "stopping" else ""),
            "warning",
            visible=rt.busy in ("starting", "stopping"),
        ),
    )
    summary = (
        f"标签      {cfg.name}\n"
        f"版本      {rt.version or '-'}\n"
        f"程序路径  {cfg.exe_path}\n"
        f"工作目录  {cfg.cwd}\n"
        f"状态地址  127.0.0.1:{cfg.status_port}    进程号 {rt.pid or '-'}\n"
        f"进程      {'存活' if rt.process_alive else '已退出'}    "
        f"健康 {health_zh(rt.health)}    连接 {ws_zh(rt.ws_state)}\n"
        f"节点编号  {rt.node_id if rt.node_id is not None else '-'}    "
        f"MT5账号 {rt.mt5_login if rt.mt5_login is not None else '-'}    "
        f"运行时长 {rt.uptime_s if rt.uptime_s is not None else '-'}\n"
        f"错误      {rt.last_error or '-'}"
    )
    if not rt.runners:
        tasks_text = "（当前无运行中的策略任务）"
    else:
        lines = []
        for r in rt.runners:
            lines.append(
                f"任务={r.get('task_id')} 魔术号={r.get('magic')} "
                f"{r.get('symbol')} 模式={r.get('mode')} "
                f"方向={r.get('direction')} 停止中={r.get('stopping')} "
                f"已结束={r.get('done')} 模版={r.get('template_id')}"
            )
        tasks_text = "\n".join(lines)
    return DetailVM(
        empty=False,
        title=cfg.name or default_label_from_path(cfg.exe_path),
        subtitle=f"{cfg.exe_path}  ·  工作目录 {cfg.cwd}",
        daemon=bool(cfg.daemon),
        summary=summary,
        tasks_text=tasks_text,
        alive=alive,
        health=rt.health or "unknown",
        busy=bool(rt.busy),
        chips=chips,
    )


def list_order(items: Sequence[ListItemVM]) -> tuple[str, ...]:
    return tuple(i.id for i in items)
