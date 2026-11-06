"""运维面板 ViewModel：从进程态生成可比较快照，供 UI 差分绑定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from models import default_label_from_path, shorten_path

if TYPE_CHECKING:
    from process_manager import ManagedProcess, ProcessManager


ALGO_TRADING_OFF_LABEL = "算法交易未开启"

# 连接态里算「已连上 MT5」的几种：已鉴权 / 连接中 / 重连中
_CONNECTED_WS_STATES = ("authenticated", "connecting", "reconnecting")


def format_uptime(seconds: float | None) -> str:
    """运行时长文案。None=未知显示 --；0=刚启动显示 0天 0小时。"""
    if seconds is None:
        return "--"
    total_hours = int(seconds) // 3600
    days, hours = divmod(total_hours, 24)
    return f"{days}天 {hours}小时"


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
    # 当前 MT5 交易账号：优先用 runtime 上报的实时账号，未上报时退回配置中的期望账号。
    mt5_login: int = 0
    algo_trading_disabled: bool = False
    # —— 卡片网格需要的字段 ——
    node_id: str = ""
    # 券商服务器名（如 ICMarkets-Live）；后端没给时留空。
    server_label: str = ""
    # 交易账号 ID 胶囊里的数字（参考稿的「ID」位）；没有则留空。
    account_label: str = ""
    uptime_text: str = "--"
    mt5_connected: bool = False
    mt5_authenticated: bool = False
    algo_trading_ok: bool = False
    algo_trading_known: bool = False
    badge_text: str = "已停止"
    badge_tone: str = "mute"
    pending_review: bool = False
    # 该节点今天的信号数与平均延迟（毫秒）；None = 后端没给这项数据。
    today_signals: int = 0
    avg_latency_ms: float | None = None


@dataclass(frozen=True)
class HeroVM:
    """顶部 hero 统计：总数、四个互斥状态、三个关键指标。

    三个指标与参考稿对齐：**今日信号 / 平均延迟 / 异常提醒**。
    前两项来自后端 `/api/node-dashboard/stats`（按本机可见节点聚合），
    第三项是 `last_error` 非空的节点数。
    """

    total: int
    online: int
    starting: int
    stopped: int
    pending: int
    # 今日信号：今天（UTC+8 自然日）下发到本机可见节点的信号条数。
    today_signals: int = 0
    # 平均延迟（毫秒）：sent_at → finished_at 的平均耗时；None = 没有可算的样本。
    avg_latency_ms: float | None = None
    alerts: int = 0


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


def algo_trading_disabled_for(saved: bool, live: bool | None) -> bool:
    """运行中的终端读数优先。没有读数时才用导入时记下的结果。"""
    if live is False:
        return True
    if live is True:
        return False
    return bool(saved)


def process_state_label(mp: "ManagedProcess | None") -> str:
    if mp is None:
        return "已停止"
    if mp.runtime.busy == "starting":
        return "启动中"
    if mp.runtime.busy == "stopping":
        return "停止中"
    return "运行中" if mp.runtime.process_alive else "已停止"


def badge_for(cfg, rt) -> tuple[str, str]:
    """节点状态徽章：(文案, 色调)。

    判定顺序与 `build_hero` 的四状态计数**必须一致** —— 否则卡片上的徽章
    和 hero 分布条会互相矛盾（同一个实例被算进两个桶）。
    """
    if rt.busy == "starting":
        return "启动中", "starting"
    if rt.busy == "stopping":
        return "停止中", "starting"
    if rt.process_alive:
        return "运行中", "online"
    if cfg.approval_status == "pending":
        return "待审核", "pending"
    return "已停止", "offline"


def build_list_item(
    mp: "ManagedProcess",
    selected_id: str | None,
    stats: dict | None = None,
) -> ListItemVM:
    """单个实例的卡片 ViewModel。

    `stats` 是后端 `/api/node-dashboard/stats` 里**该节点**的一条：
    券商服务器名、今日信号数、平均延迟。缺这一节点时传 None（显示占位符）。
    """
    cfg = mp.cfg
    rt = mp.runtime
    title = cfg.name or default_label_from_path(cfg.exe_path)
    state = process_state_label(mp)
    daemon = "  ·  守护" if cfg.daemon else ""
    status_line = f"{state}  ·  {health_zh(rt.health)}{daemon}"
    if cfg.approval_status == "pending":
        status_line += "  ·  待管理员审核开通"
    elif cfg.approval_status == "unbound":
        status_line += "  ·  需管理员绑定"
    if rt.version:
        status_line = f"v{rt.version}  ·  {status_line}"
    mt5_login = int(rt.mt5_login) if rt.mt5_login else int(cfg.mt5_login or 0)
    badge_text, badge_tone = badge_for(cfg, rt)
    node_id = str(rt.node_id or cfg.node_id or "")
    st = stats or {}
    latency = st.get("avg_latency_ms")
    return ListItemVM(
        id=cfg.id,
        title=title,
        path_short=shorten_path(cfg.exe_path, 40),
        status_line=status_line,
        alive=bool(rt.process_alive),
        health=rt.health or "unknown",
        selected=cfg.id == selected_id,
        daemon=bool(cfg.daemon),
        mt5_login=mt5_login,
        algo_trading_disabled=algo_trading_disabled_for(
            cfg.algo_trading_disabled, rt.algo_trading_allowed,
        ),
        node_id=node_id,
        # 券商服务器名（参考稿的 meta-server 位）；后端没给就留空。
        server_label=str(st.get("mt5_server") or "").strip(),
        # 「ID」位放交易账号 ID（参考稿的 mt5-id-inline 位）。
        account_label=str(mt5_login) if mt5_login else "",
        uptime_text=format_uptime(rt.uptime_s),
        mt5_connected=rt.ws_state in _CONNECTED_WS_STATES,
        mt5_authenticated=rt.ws_state == "authenticated",
        algo_trading_ok=rt.algo_trading_allowed is True,
        algo_trading_known=rt.algo_trading_allowed is not None,
        badge_text=badge_text,
        badge_tone=badge_tone,
        pending_review=cfg.approval_status == "pending",
        today_signals=int(st.get("today_signals") or 0),
        avg_latency_ms=float(latency) if latency is not None else None,
    )


def build_list(
    manager: "ProcessManager",
    selected_id: str | None,
    stats: dict | None = None,
) -> tuple[ListItemVM, ...]:
    items: list[ListItemVM] = []
    for cfg in manager.configs():
        mp = manager.get(cfg.id)
        if mp is None:
            continue
        node_id = str(mp.runtime.node_id or cfg.node_id or "")
        items.append(build_list_item(mp, selected_id, (stats or {}).get(node_id)))
    return tuple(items)


def build_hero(manager: "ProcessManager", stats: dict | None = None) -> HeroVM:
    """顶部 hero 统计。

    四个状态互斥（在线 + 启动中 + 待审核 + 已停止 == 总数），判定复用
    `badge_for` —— 与卡片徽章同源，避免条形图和卡片互相矛盾。

    今日信号与平均延迟来自后端 `/api/node-dashboard/stats`（按 node_id 索引），
    只统计**本机可见**的节点；后端没给数据时显示 0 / 「--」。
    """
    total = online = starting = stopped = pending = 0
    alerts = 0
    today_signals = 0
    latencies: list[float] = []
    stats = stats or {}
    for cfg in manager.configs():
        mp = manager.get(cfg.id)
        if mp is None:
            continue
        rt = mp.runtime
        total += 1
        _text, tone = badge_for(cfg, rt)
        if tone == "online":
            online += 1
        elif tone == "starting":
            starting += 1
        elif tone == "pending":
            pending += 1
        else:
            stopped += 1
        node_id = str(rt.node_id or cfg.node_id or "")
        st = stats.get(node_id) or {}
        today_signals += int(st.get("today_signals") or 0)
        latency = st.get("avg_latency_ms")
        if latency is not None:
            latencies.append(float(latency))
        if rt.last_error:
            alerts += 1
    return HeroVM(
        total=total,
        online=online,
        starting=starting,
        stopped=stopped,
        pending=pending,
        today_signals=today_signals,
        avg_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        alerts=alerts,
    )


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
        f"进程      {'存活' if rt.process_alive else '已退出'}    "
        f"进程号 {rt.pid or '-'}    "
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
