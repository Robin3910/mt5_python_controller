"""信号拦截 / 拒收原因的**结构化编码**（skip_code / reject_code）。

为什么需要这一层：
    跳过原因此前全部是中文自由文本，散落在 `rules.py` / `group_rules.py` /
    `dispatcher.py` / `group_dispatcher.py` 各处。要做「拦截原因 Top N」这类统计，
    就得用正则去解析中文 —— 一旦有人改了文案，统计立刻漂移。

    所以：**编码是数据，文字是展示**。
    写入点负责写 `code`；`reason` 仍然照旧写中文，只用于人读展示。
    统计侧一律按 `code` 聚合，永远不碰中文。

两套编码：
    - SKIP_CODES：信号已受理、但在**逐节点投递**环节被拦下（分发明细级）
    - REJECT_CODES：信号在**受理之前**就被整体拒收（信号级）

新增编码的规矩：
    1. 只增不改（改了就等于让历史统计失真）；
    2. 新增必须同时补 `SKIP_CODE_LABELS` 的人读标签；
    3. 未知 / 历史空值一律归 `unknown`，不要塞进语义桶。

本模块**不参与任何交易判断**：写失败、写漏、写错都只影响统计，
绝不允许影响信号的下发与成交。写入点统一用 `safe_code` 包装。
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 逐节点投递环节的跳过编码（signal_dispatch / group_task_dispatch.skip_code）
# ---------------------------------------------------------------------------
SKIP_CONFIG_MISSING = "config_missing"      # 节点未配置该品种
SKIP_LOGIN_MISMATCH = "login_mismatch"      # MT5 登录号与节点绑定不符
SKIP_RISK_TREND = "risk_trend"              # 趋势风控拦截
SKIP_RISK_POSITION = "risk_position"        # 持仓过滤
SKIP_RISK_INTERVAL = "risk_interval"        # 区间 / 方向过滤
SKIP_CONFLICT_BUSY = "conflict_busy"        # 并发占用（节点忙 / 在途订单）
SKIP_UNAVAILABLE = "unavailable"            # 目标不可用（离线 / 分组禁用 / 无成员）
SKIP_ADMISSION_RULE = "admission_rule"      # 准入不通过（规则未启用 / 方向不接受）
SKIP_ADMISSION_PARAM = "admission_param"    # 参数缺失（缺 sl / 缺入场价 / 区间非法）
SKIP_MISMATCH_SCOPE = "mismatch_scope"      # 定向不匹配（品种 / 模版 / 分组）
SKIP_LINK_OFFLINE = "link_offline"          # 终端与券商断联，发单前拦下
SKIP_ERROR_MT5 = "error_mt5"                # 执行失败（MT5 retcode / error）
SKIP_UNKNOWN = "unknown"                    # 兜底

SKIP_CODES: tuple[str, ...] = (
    SKIP_CONFIG_MISSING,
    SKIP_LOGIN_MISMATCH,
    SKIP_RISK_TREND,
    SKIP_RISK_POSITION,
    SKIP_RISK_INTERVAL,
    SKIP_CONFLICT_BUSY,
    SKIP_UNAVAILABLE,
    SKIP_ADMISSION_RULE,
    SKIP_ADMISSION_PARAM,
    SKIP_MISMATCH_SCOPE,
    SKIP_LINK_OFFLINE,
    SKIP_ERROR_MT5,
    SKIP_UNKNOWN,
)

# 人读标签（前端直接用；新增 code 必须补标签）
SKIP_CODE_LABELS: dict[str, str] = {
    SKIP_CONFIG_MISSING: "配置缺失",
    SKIP_LOGIN_MISMATCH: "登录号不符",
    SKIP_RISK_TREND: "趋势风控",
    SKIP_RISK_POSITION: "持仓过滤",
    SKIP_RISK_INTERVAL: "区间过滤",
    SKIP_CONFLICT_BUSY: "并发占用",
    SKIP_UNAVAILABLE: "目标不可用",
    SKIP_ADMISSION_RULE: "准入不通过",
    SKIP_ADMISSION_PARAM: "参数缺失",
    SKIP_MISMATCH_SCOPE: "定向不匹配",
    SKIP_LINK_OFFLINE: "链路断连",
    SKIP_ERROR_MT5: "执行失败",
    SKIP_UNKNOWN: "其它",
}

# ---------------------------------------------------------------------------
# 信号整体拒收编码（signal_history.reject_code）
# ---------------------------------------------------------------------------
REJECT_NO_MATCH_GROUP = "no_match_group"    # 无匹配分组
REJECT_NO_ACTIVE_TASK = "no_active_task"    # CLOSE 时无匹配进行中任务
REJECT_SYMBOL_NOT_CONFIGURED = "symbol_not_configured"  # 品种未在节点配置中
REJECT_SYMBOL_DISABLED = "symbol_disabled"  # 品种在中控台被禁用
REJECT_DUPLICATE = "duplicate"              # 5 秒幂等去重（本期不采集，仅占位）
REJECT_INVALID_PAYLOAD = "invalid_payload"  # 解析失败 / 字段非法
REJECT_UNKNOWN = "unknown"

REJECT_CODES: tuple[str, ...] = (
    REJECT_NO_MATCH_GROUP,
    REJECT_NO_ACTIVE_TASK,
    REJECT_SYMBOL_NOT_CONFIGURED,
    REJECT_SYMBOL_DISABLED,
    REJECT_DUPLICATE,
    REJECT_INVALID_PAYLOAD,
    REJECT_UNKNOWN,
)

REJECT_CODE_LABELS: dict[str, str] = {
    REJECT_NO_MATCH_GROUP: "无匹配分组",
    REJECT_NO_ACTIVE_TASK: "无匹配任务",
    REJECT_SYMBOL_NOT_CONFIGURED: "品种未配置",
    REJECT_SYMBOL_DISABLED: "品种已禁用",
    REJECT_DUPLICATE: "重复抑制",
    REJECT_INVALID_PAYLOAD: "参数非法",
    REJECT_UNKNOWN: "其它",
}


def skip_label(code: Optional[str]) -> str:
    """skip_code 的人读标签；未知 / 空值归「其它」。"""
    return SKIP_CODE_LABELS.get(str(code or "").strip().lower(), SKIP_CODE_LABELS[SKIP_UNKNOWN])


def reject_label(code: Optional[str]) -> str:
    """reject_code 的人读标签；未知 / 空值归「其它」。"""
    return REJECT_CODE_LABELS.get(str(code or "").strip().lower(), REJECT_CODE_LABELS[REJECT_UNKNOWN])


def safe_code(code: Optional[str]) -> Optional[str]:
    """把写入点传来的 code 收敛成合法值；非法值一律归 unknown（绝不抛异常）。"""
    if not code:
        return None
    c = str(code).strip().lower()
    if c in SKIP_CODES:
        return c
    return SKIP_UNKNOWN


# ---------------------------------------------------------------------------
# 历史数据回填：把存量中文 skip_reason 归类到 code
# ---------------------------------------------------------------------------
# 顺序敏感：先匹配到的先赢。规则与中文文案的稳定前缀绑定，
# 只用于**一次性回填**，不承担运行时判断。
_BACKFILL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SKIP_CONFIG_MISSING, ("节点未配置",)),
    (SKIP_LOGIN_MISMATCH, ("登录号", "login")),
    # 链路断连必须排在 SKIP_UNAVAILABLE 之前：它的文案里也含「断连」，
    # 若被后面那条先匹配到，就会被归成「目标不可用」。
    (SKIP_LINK_OFFLINE, ("断连", "链路离线", "与券商断连")),
    (SKIP_RISK_TREND, ("趋势风控",)),
    (SKIP_RISK_POSITION, ("持仓过滤",)),
    (SKIP_RISK_INTERVAL, ("区间", "方向")),
    (SKIP_CONFLICT_BUSY, ("并发控制", "节点忙", "已有在途", "进行中")),
    (SKIP_UNAVAILABLE, ("未在线", "离线", "无有效节点", "无成员节点", "分组已禁用", "不可用")),
    (SKIP_ADMISSION_PARAM, ("需要止损", "需要入场价", "区间", "每格手数", "总手数上限", "风险金额")),
    (SKIP_ADMISSION_RULE, ("没有启用", "不接受", "准入", "已禁用", "策略不存在")),
    (SKIP_MISMATCH_SCOPE, ("品种不匹配", "模版", "分组不存在", "不属于")),
)


def classify_skip_reason(reason: Optional[str]) -> str:
    """把中文跳过原因归类成 skip_code（供历史回填与兜底使用）。

    运行时写入点应直接写 code；这个函数只在历史回填 / 兜底场景出现。
    """
    text = str(reason or "").strip()
    if not text:
        return SKIP_UNKNOWN
    low = text.lower()
    for code, keys in _BACKFILL_RULES:
        for k in keys:
            if k in low:
                return code
    # MT5 错误码（形如 "MT5 错误 10019" / "invalid volume"）→ 执行失败
    if re.search(r"(mt5|invalid|failed|error)", low):
        return SKIP_ERROR_MT5
    return SKIP_UNKNOWN


def classify_group_skip_reason(reason: Optional[str]) -> str:
    """把「分组级」的跳过原因归类成 skip_code。

    分组级与节点级是两回事：分组级跳过一次 = 整条信号在这个分组没下发。
    """
    text = str(reason or "").strip()
    if not text:
        return SKIP_UNKNOWN
    if "已禁用" in text:
        return SKIP_ADMISSION_RULE
    if "无成员节点" in text:
        return SKIP_MISMATCH_SCOPE
    if "无有效节点" in text or "未启用或不在线" in text or "主节点当前不可用" in text:
        return SKIP_UNAVAILABLE
    if "暂停" in text:
        return SKIP_ADMISSION_RULE
    return classify_skip_reason(text)


def classify_reject_reason(reason: Optional[str]) -> str:
    """把中文拒收原因归类成 reject_code。"""
    text = str(reason or "").strip()
    if not text:
        return REJECT_UNKNOWN
    low = text.lower()
    if "无匹配分组" in text:
        return REJECT_NO_MATCH_GROUP
    if "无匹配任务" in text:
        return REJECT_NO_ACTIVE_TASK
    if "已禁用" in text:
        return REJECT_SYMBOL_DISABLED
    if "未配置" in text or "未在中控台配置" in text:
        return REJECT_SYMBOL_NOT_CONFIGURED
    if "重复" in text or "duplicate" in low:
        return REJECT_DUPLICATE
    if "cannot parse" in low or "invalid signal" in low:
        return REJECT_INVALID_PAYLOAD
    return REJECT_UNKNOWN
