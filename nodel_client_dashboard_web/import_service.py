"""导入节点的编排逻辑（UI 无关）。

与 TK 版 `import_dialog.py` 的区别：这里不碰任何 UI 组件
（没有 tkinter、没有 `after`、没有 `messagebox`），只接收参数、
调用进度回调、返回结构化结果。TK 版与 Web 版共用这套编排。

导入流程（与 TK 版一致）：
    ① 校验目录（含 terminal64.exe）
    ② 解析账号（留空则读终端当前登录号）
    ③ 申请节点（拿 node_id）
    ④ 下载安装包 → 校验 sha256
    ⑤ 解压到临时目录
    ⑥ 停掉该目录里正在运行的客户端
    ⑦ 覆盖部署（旧版本先备份）
    ⑧ 写 .env
    ⑨ 注册实例

失败语义：部署失败会保留备份，可回滚；任何一步失败都不会把半成品
注册进实例列表。
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import env_file as ef
import version_service as vs
from client_binding import require_binding
from client_deploy import apply_package, extract_package, read_version_near
from models import InstanceConfig
from terminal_account import (
    TerminalAccountError,
    enroll_import_login,
    read_algo_trading_allowed,
    read_terminal_account,
    typed_login,
)

# 进度回调：(阶段标识, 阶段说明, 进度百分比或 None, 附加日志行)
ProgressFn = Callable[[str, str, float | None, str], None]

# 受审计操作的执行器：由调用方注入，桥接层传 session.perform
PerformFn = Callable[..., Any]


class ImportError_(RuntimeError):
    """导入过程中的可读错误。"""


@dataclass
class ImportRequest:
    """一次导入请求的参数。"""

    directory: str
    name: str = ""
    version: str = ""
    sha256: str = ""
    account_raw: str = ""
    write_env: bool = True
    ws_url: str = ""


@dataclass
class ImportResult:
    """导入结果。"""

    ok: bool = False
    config: InstanceConfig | None = None
    version: str = ""
    env_written: bool = False
    env_overwritten: bool = False
    algo_trading_disabled: bool = False
    message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)


# 步骤标识（前端用来显示步骤列表）
STEP_VALIDATE = "validate"
STEP_ENROLL = "enroll"
STEP_DOWNLOAD = "download"
STEP_EXTRACT = "extract"
STEP_STOP = "stop"
STEP_DEPLOY = "deploy"
STEP_ENV = "env"
STEP_REGISTER = "register"

STEP_LABELS = {
    STEP_VALIDATE: "校验目录",
    STEP_ENROLL: "申请节点",
    STEP_DOWNLOAD: "下载安装包",
    STEP_EXTRACT: "校验并解压",
    STEP_STOP: "停止旧客户端",
    STEP_DEPLOY: "部署到 MT5 目录",
    STEP_ENV: "写入准备配置",
    STEP_REGISTER: "注册实例",
}


def check_directory(directory: str) -> dict[str, Any]:
    """校验 MT5 目录，返回可展示的检查结果。

    不抛异常——前端要用这些信息做实时提示。
    """
    raw = (directory or "").strip()
    if not raw:
        return {"ok": False, "message": "请先选择 MT5 安装目录", "terminal": ""}

    path = Path(raw)
    if not path.is_dir():
        return {"ok": False, "message": f"目录不存在：{path}", "terminal": ""}

    terminal = ef.find_terminal(path)
    if terminal is None:
        names = " / ".join(ef.TERMINAL_NAMES)
        return {
            "ok": False,
            "message": (
                f"该目录没有 {names}，不是 MT5 安装目录。"
                "客户端必须与终端同目录才能连上 MT5。"
            ),
            "terminal": "",
        }

    exe_path = path / "node_client.exe"
    notes = [f"已识别 MT5 终端：{terminal.name}"]
    existing_version = ""
    if exe_path.is_file():
        existing_version = read_version_near(exe_path) or ""
        notes.append(
            f"该目录已有客户端 v{existing_version or '未知'}（导入会先备份再覆盖）"
        )
    env_exists = ef.env_path(path).is_file()
    if env_exists:
        notes.append("已有 .env（勾选自动写入时将覆盖）")

    return {
        "ok": True,
        "message": "　".join(notes),
        "terminal": terminal.name,
        "terminal_path": str(terminal),
        "existing_version": existing_version,
        "env_exists": env_exists,
        "suggested_name": path.name,
    }


def resolve_account(directory: str, account_raw: str) -> tuple[int, str]:
    """解析 MT5 账号：填写了正整数就用它，否则读终端当前登录号。"""
    typed = typed_login(account_raw)
    if typed is not None:
        return typed, ""
    try:
        account = read_terminal_account(directory)
    except TerminalAccountError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"无法读取 MT5 终端账号：{exc}") from exc
    if not isinstance(account, dict):
        raise ImportError_("终端未登录，请先在 MT5 登录，或手工填写账号")
    login = account.get("login")
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise ImportError_("终端未登录，请先在 MT5 登录，或手工填写账号")
    return int(login), str(account.get("server") or "")


def summarize(request: ImportRequest, *, login: int) -> dict[str, Any]:
    """导入前的变更摘要，供前端展示确认。"""
    directory = Path(request.directory)
    terminal = ef.find_terminal(directory)
    env_exists = ef.env_path(directory).is_file()

    lines = [
        f"将把客户端 v{request.version} 部署到：",
        f"  {directory}",
        "",
        f"实例标签：{request.name or directory.name}",
        f"MT5 终端：{terminal.name if terminal else '?'}",
        f"MT5 账号：{login}",
    ]
    if request.write_env and env_exists:
        lines += [
            "",
            "该目录已有 .env，将整份覆盖：",
            f"  MANAGER_WS_URL={request.ws_url}",
            "  NODE_TOKEN=（面板连接配置）",
            "  其余项来自安装包 .env.example（旧文件中的自定义键不会保留）",
        ]
    elif request.write_env:
        lines += [
            "",
            "将自动生成 .env：",
            f"  MANAGER_WS_URL={request.ws_url}",
            "  NODE_TOKEN=（面板连接配置）",
        ]
    else:
        lines += ["", "不写入 .env —— 导入后需自行配置，否则节点无法接入。"]
    lines += ["", "是否继续？"]
    return {"ok": True, "text": "\n".join(lines), "env_exists": env_exists}


def _write_env_result(directory: Path, text: str) -> tuple[bool, str]:
    """写 .env 并返回 (ok, message)。

    `ef.write_env` 本身返回 None，直接拿它做 `ok, msg = ...` 会抛
    "cannot unpack non-iterable NoneType object"。这里统一包一层，
    保证写失败时也能把原因带给用户。
    """
    try:
        ef.write_env(directory, text)
    except OSError as exc:
        return False, f"写入 .env 失败：{exc}"
    return True, "ok"


def _as_pair(value: Any, action: str) -> tuple[bool, str]:
    """把受审计操作的返回值归一成 (ok, message)。

    `perform` 原样返回被调函数的返回值。不同动作的函数有的返回
    `(bool, str)`，有的返回 None（例如 `ef.write_env`）。
    直接写 `ok, msg = perform(...)` 遇到 None 就会抛
    "cannot unpack non-iterable NoneType object"——用户完全看不懂。
    这里统一兜住：None 视作成功（函数确实执行完了），
    形状不对则给出可读错误。
    """
    if value is None:
        return True, "ok"
    if isinstance(value, tuple) and len(value) == 2:
        ok, msg = value
        return bool(ok), str(msg)
    if isinstance(value, bool):
        return value, "ok" if value else "操作未成功"
    return False, f"{action} 返回了无法识别的结果：{value!r}"


def run_import(
    request: ImportRequest,
    *,
    target: vs.BackendTarget,
    perform: PerformFn,
    enroll: Callable[[InstanceConfig, int, str], None],
    stop_for_install: Callable[[str], None],
    register: Callable[[InstanceConfig], None],
    progress: ProgressFn | None = None,
) -> ImportResult:
    """执行导入。`perform` 是受审计操作的执行器（通常是 session.perform）。

    `enroll` / `stop_for_install` / `register` 由调用方注入，
    这样本模块不依赖 ProcessManager 与 DashboardSession 具体实现。
    """
    report = progress or (lambda *_a, **_k: None)

    def step(key: str, pct: float | None = None, note: str = "") -> None:
        report(key, STEP_LABELS.get(key, key), pct, note)

    directory = Path(request.directory)
    exe_path = directory / "node_client.exe"
    result = ImportResult(version=request.version)

    # —— ① 校验目录 ——
    step(STEP_VALIDATE, 0.0)
    check = check_directory(request.directory)
    if not check["ok"]:
        raise ImportError_(str(check["message"]))
    report(STEP_VALIDATE, STEP_LABELS[STEP_VALIDATE], 0.05, str(check["message"]))

    # —— ② 解析账号 ——
    login, server = resolve_account(request.directory, request.account_raw)
    name = request.name.strip() or directory.name
    cfg = InstanceConfig.create(
        name=name, exe_path=str(exe_path), cwd=str(directory)
    )

    # —— ③ 申请节点 ——
    step(STEP_ENROLL, 0.08)
    enroll(cfg, login, server)
    report(STEP_ENROLL, STEP_LABELS[STEP_ENROLL], 0.12, f"已申请节点，账号 {login}")

    with tempfile.TemporaryDirectory(prefix="node_client_import_") as tmp:
        tmpdir = Path(tmp)
        pkg = tmpdir / f"node_client-{request.version}.zip"

        # —— ④ 下载 ——
        step(STEP_DOWNLOAD, 0.15)
        report(
            STEP_DOWNLOAD,
            STEP_LABELS[STEP_DOWNLOAD],
            0.15,
            f"正在下载 v{request.version}，预计需要 3-4 分钟，请不要操作鼠标！",
        )

        def on_progress(done: int, total: int) -> None:
            if total > 0:
                pct = 0.15 + 0.45 * (done / total)
                note = f"{done * 100 // total}%"
            else:
                pct = None
                note = f"{done // 1024 // 1024} MB"
            report(STEP_DOWNLOAD, STEP_LABELS[STEP_DOWNLOAD], pct, note)

        vs.download_package(target, request.version, pkg, on_progress=on_progress)

        if request.sha256 and not vs.verify_sha256(pkg, request.sha256):
            raise ImportError_("安装包校验和不匹配，已中止导入")
        report(STEP_DOWNLOAD, STEP_LABELS[STEP_DOWNLOAD], 0.60, "下载完成并通过校验")

        # —— ⑤ 解压 ——
        step(STEP_EXTRACT, 0.62)
        extract_dir = tmpdir / "extracted"
        ok, msg = extract_package(pkg, extract_dir)
        if not ok:
            raise ImportError_(msg)
        report(STEP_EXTRACT, STEP_LABELS[STEP_EXTRACT], 0.70, "解压完成")

        # —— ⑥ 停旧进程 ——
        step(STEP_STOP, 0.72)
        stop_for_install(str(directory))
        report(STEP_STOP, STEP_LABELS[STEP_STOP], 0.78, "已停止该目录中正在运行的客户端")

        # —— ⑦ 部署 ——
        step(STEP_DEPLOY, 0.80)
        report(
            STEP_DEPLOY,
            STEP_LABELS[STEP_DEPLOY],
            0.80,
            "正在部署到 MT5 目录，请不要操作鼠标！等待自动导入完成！",
        )
        require_binding(extract_dir / "node_client.exe")
        ok, msg = _as_pair(
            perform(
                "import_node",
                cfg,
                lambda: apply_package(extract_dir, exe_path, backup=True),
            ),
            "部署",
        )
        if not ok:
            raise ImportError_(str(msg))
        report(STEP_DEPLOY, STEP_LABELS[STEP_DEPLOY], 0.90, "部署完成")

        # —— ⑧ 写 .env ——
        template = ""
        example = extract_dir / ".env.example"
        if example.is_file():
            template = example.read_text(encoding="utf-8-sig", errors="replace")

        if request.write_env:
            step(STEP_ENV, 0.92)
            old, _ = ef.read_env(directory)
            result.env_overwritten = bool(old)
            updates = {
                "MANAGER_WS_URL": request.ws_url,
                "DASHBOARD_EXPECTED_MT5_LOGIN": str(cfg.mt5_login),
            }
            ok, msg = _as_pair(
                perform(
                    "edit_env",
                    cfg,
                    lambda: _write_env_result(
                        directory, ef.update_env_text(old or template, updates)
                    ),
                ),
                "写入 .env",
            )
            if not ok:
                raise ImportError_(str(msg))
            result.env_written = True
            report(STEP_ENV, STEP_LABELS[STEP_ENV], 0.96, "准备配置已写入")

        cfg.algo_trading_disabled = read_algo_trading_allowed(directory) is False
        result.algo_trading_disabled = cfg.algo_trading_disabled

    # —— ⑨ 注册实例 ——
    step(STEP_REGISTER, 0.98)
    register(cfg)
    report(STEP_REGISTER, STEP_LABELS[STEP_REGISTER], 1.0, "实例已加入列表")

    installed = read_version_near(exe_path) or request.version
    result.version = installed
    result.config = cfg
    result.ok = True
    return result
