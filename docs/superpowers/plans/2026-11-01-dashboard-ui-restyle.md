# 控制端 UI 风格改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `nodel_client_dashboard` 桌面 UI 从「玻璃拟态 · 左列表 + 右详情」改造成参考稿 `UI/控制端UI.html` 的「平面深色 · 顶部 hero + 卡片网格」风格。

**Architecture:** 分四层推进 —— (1) `theme.py` 单点换掉全部色板与控件工厂，登录页和 5 个对话框自动继承新皮肤；(2) `viewmodels.py` 补齐 hero 统计与卡片所需的 ViewModel 字段；(3) 抽出 `hero_panel.py` / `node_card.py` / `node_detail_dialog.py` 三个新模块承载新布局，保留 `app_ui.py` 现有差分刷新机制；(4) 收尾处理手绘图元残影与全量回归。

**Tech Stack:** Python 3.13、customtkinter 6.0.0（CTk 6.0 已原生支持 `CTkCanvas`）、tkinter `tk.Menu`（右键菜单）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-01-dashboard-ui-restyle-design.md`

---

## Global Constraints

- Python 3.13 / customtkinter 6.0.0。CTk 6.0 起 `CTkCanvas` 为独立类，构造签名 `CTkCanvas(master, width=..., height=..., bg=..., highlightthickness=0, bd=0)`。
- 新色板（唯一事实来源，`theme.py`）：
  - `BG = "#0a0e17"`、`PANEL = "#101826"`、`PANEL2 = "#151f31"`、`LINE = "#1e2a3d"`
  - `GOLD = "#d9b36c"`、`GOLD_STRONG = "#e9c98a"`、`ON_GOLD = "#1b1408"`
  - `GREEN = "#2fd381"`、`AMBER = "#f2b84b"`、`RED = "#f2645f"`、`BLUE = "#58a6ff"`、`VIOLET = "#a78bfa"`
  - `TEXT = "#e8ecf4"`、`DIM = "#8b96ab"`、`MUTE = "#5a6a82"`
- 旧色板名保留为别名，**不得删除、不得改名**（11 个文件在 import）：`BG_ELEVATED→PANEL2`、`GLASS→PANEL`、`GLASS_SOFT→PANEL2`、`GLASS_BORDER→LINE`、`GLASS_BORDER_ACTIVE→GOLD`、`ACCENT→GOLD`、`CYAN→BLUE`、`TEXT_MUTED→DIM`、`TEXT_DIM→MUTE`、`DANGER→RED`、`WARNING→AMBER`、`OK→GREEN`、`DEAD→MUTE`、`CHIP_BG="#0d1524"`。
- 圆角：卡片 10、hero 14、按钮 6、chip/badge 4~5（`glass_card` 现状 16 → 10）。
- 主按钮金色底 + `ON_GOLD` 文字；青绿 `GREEN` 只用于健康语义（在线/已连接/已鉴权/已开启），不用于主操作按钮。
- **不改动任何业务逻辑**：权限校验、审计、节点生命周期、后端 API 一律不变。
- **不新增数据采集**：hero 的三个指标只能来自已有的 `runtime` 字段。
- 现有差分刷新机制（`_bind_list` / `_apply_list_row` / `_bind_detail`）必须保留，不得改成整体重建。
- Tk 原生 `messagebox` / `simpledialog` 样式不统一，接受，不自绘。

## Review Focus

以下五类输入/状况规格书暗示了、但没有任务专门覆盖，是最容易咬人的地方。按可能性排序：

1. **实例数为 0 或 1**：hero 状态分布条不能出现除零 / 负宽度；网格列数计算不能因除零崩溃；卡片网格空态要有提示文案。
2. **窗口宽度跨列数断点**：1180 / 820 两个断点来回拖动，卡片不能重叠、不能留空洞、不能丢卡片。
3. **`uptime_s` 为 `None` 与 `0` 的区别**：`None` 表示未知（显示 `--`），`0` 表示刚启动（显示 `0天 0小时`）——不能都当假值处理。
4. **`ws_state` 缺失或非预期值**：`None` / `""` / 未知字符串时，「是否鉴权」格必须显示为否，不能抛异常或显示「已鉴权」。
5. **Tk 原生弹窗与自定义弹窗混用时输入权**：详情弹窗打开期间主窗口被 `grab_set` 锁住，弹窗内再触发 `messagebox` 后焦点必须回到弹窗而不是主窗口。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `theme.py`（改） | 全部色板 token、字体、控件工厂（卡片/按钮/chip/徽章/信息格/指标格） |
| `viewmodels.py`（改） | `HeroVM`、`ListItemVM` 卡片字段、`build_hero()` |
| `hero_panel.py`（新建） | 顶部 hero 统计卡：总数 + 状态 chips + 分布条 + 3 指标格 + 网格纹理 |
| `node_card.py`（新建） | 单张节点卡控件（网格单元）+ 差分更新 |
| `node_detail_dialog.py`（新建） | 节点详情弹窗（承接原右侧详情的全部内容与操作） |
| `app_ui.py`（改） | 主窗口重排：顶栏 / hero / 工具条 / 网格；双击开弹窗；右键菜单 |
| `login_view.py`（改） | 登录页皮肤（多数靠 theme.py 自动生效） |
| `test_theme.py`（新建） | 色板与控件工厂断言 |
| `test_viewmodels.py`（改） | `HeroVM` / `ListItemVM` 新字段断言 |
| `test_node_card.py`（新建） | 卡片差分更新、空态、断点重排 |
| `test_hero_panel.py`（新建） | hero 统计计算、零实例、分布条比例 |

---

## Task 1: theme.py 色板与控件工厂换肤

**Files:**
- Modify: `nodel_client_dashboard/theme.py`
- Create: `nodel_client_dashboard/test_theme.py`

**Interfaces:**
- Consumes: 无（起点任务）
- Produces:
  - `theme.BG/PANEL/PANEL2/LINE/GOLD/GOLD_STRONG/ON_GOLD/GREEN/AMBER/RED/BLUE/VIOLET/TEXT/DIM/MUTE`
  - `theme.glass_card(master, **kwargs) -> ctk.CTkFrame`（签名不变，实现换新）
  - `theme.soft_card(master, **kwargs) -> ctk.CTkFrame`（签名不变）
  - `theme.primary_btn(master, text, command=None, width=96, **kwargs) -> ctk.CTkButton`（签名不变）
  - `theme.ghost_btn(master, text, command=None, width=96, **kwargs) -> ctk.CTkButton`（签名不变）
  - `theme.danger_btn(master, text, command=None, width=96, **kwargs) -> ctk.CTkButton`（签名不变）
  - `theme.status_chip(master, text, color) -> ctk.CTkLabel`（签名不变）
  - `theme.health_color(health, alive) -> str`（签名不变，语义色改为新色板）
  - `theme.badge(master, text, color) -> ctk.CTkLabel`（新增）
  - `theme.info_cell(master, label, value, ok) -> ctk.CTkFrame`（新增）
  - `theme.metric_cell(master, value, label, accent) -> ctk.CTkFrame`（新增）
  - `theme.toolbar_btn(master, text, command=None, width=96, **kwargs) -> ctk.CTkButton`（新增，高 28）

- [ ] **Step 1: 写失败测试**

创建 `nodel_client_dashboard/test_theme.py`：

```python
"""主题 token 与控件工厂：新色板、圆角、金色主按钮。"""
from __future__ import annotations

import tkinter as tk

import pytest

import theme


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    r.withdraw()
    yield r
    r.destroy()


def test_palette_values():
    assert theme.BG == "#0a0e17"
    assert theme.PANEL == "#101826"
    assert theme.PANEL2 == "#151f31"
    assert theme.LINE == "#1e2a3d"
    assert theme.GOLD == "#d9b36c"
    assert theme.GOLD_STRONG == "#e9c98a"
    assert theme.ON_GOLD == "#1b1408"
    assert theme.GREEN == "#2fd381"
    assert theme.AMBER == "#f2b84b"
    assert theme.RED == "#f2645f"
    assert theme.TEXT == "#e8ecf4"
    assert theme.DIM == "#8b96ab"
    assert theme.MUTE == "#5a6a82"


def test_legacy_names_are_aliases():
    """旧名保留且指向新色板，避免 11 个 import 点同时改。"""
    assert theme.BG_ELEVATED == theme.PANEL2
    assert theme.GLASS == theme.PANEL
    assert theme.GLASS_SOFT == theme.PANEL2
    assert theme.GLASS_BORDER == theme.LINE
    assert theme.GLASS_BORDER_ACTIVE == theme.GOLD
    assert theme.ACCENT == theme.GOLD
    assert theme.CYAN == theme.BLUE
    assert theme.TEXT_MUTED == theme.DIM
    assert theme.TEXT_DIM == theme.MUTE
    assert theme.DANGER == theme.RED
    assert theme.WARNING == theme.AMBER
    assert theme.OK == theme.GREEN
    assert theme.DEAD == theme.MUTE


def test_primary_btn_is_gold(root):
    btn = theme.primary_btn(root, "启动")
    assert btn.cget("fg_color") == theme.GOLD
    assert btn.cget("text_color") == theme.ON_GOLD
    assert btn.cget("corner_radius") == 6


def test_ghost_btn_is_transparent_with_line_border(root):
    btn = theme.ghost_btn(root, "退出登录")
    assert btn.cget("fg_color") == "transparent"
    assert btn.cget("border_color") == theme.LINE


def test_glass_card_radius_is_10(root):
    card = theme.glass_card(root)
    assert card.cget("corner_radius") == 10
    assert card.cget("fg_color") == theme.PANEL
    assert card.cget("border_color") == theme.LINE


def test_badge_and_cells_build(root):
    b = theme.badge(root, "运行中", theme.GREEN)
    assert b.cget("text_color") == theme.GREEN
    cell = theme.info_cell(root, "连接MT5", "已连接", True)
    assert cell.winfo_exists()
    m = theme.metric_cell(root, "3", "已连接MT5", theme.BLUE)
    assert m.winfo_exists()


def test_health_color_uses_new_semantics():
    assert theme.health_color("ok", True) == theme.GREEN
    assert theme.health_color("error", True) == theme.RED
    assert theme.health_color("starting", True) == theme.AMBER
    assert theme.health_color("ok", False) == theme.MUTE
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_theme.py -v`
Expected: FAIL —— `AttributeError: module 'theme' has no attribute 'PANEL'`（多个用例失败）

- [ ] **Step 3: 改写 theme.py**

把 `theme.py` 的色板段替换为：

```python
# —— 色板（参考稿实测值：平面深蓝 + 金色主强调 + 状态语义色）——
BG = "#0a0e17"
PANEL = "#101826"
PANEL2 = "#151f31"
LINE = "#1e2a3d"
LINE_SOFT = "#1e2a3d"

GOLD = "#d9b36c"          # 主强调：主按钮、选中态
GOLD_STRONG = "#e9c98a"   # 金色 hover
ON_GOLD = "#1b1408"       # 金色底上的文字
GREEN = "#2fd381"         # 健康语义：在线 / 已连接 / 已鉴权 / 已开启
AMBER = "#f2b84b"         # 启动中 / 警告 / 待审核
RED = "#f2645f"           # 停止 / 错误 / 危险操作
BLUE = "#58a6ff"
VIOLET = "#a78bfa"

TEXT = "#e8ecf4"
DIM = "#8b96ab"
MUTE = "#5a6a82"
CHIP_BG = "#0d1524"

# —— 旧名别名：11 个文件在用，勿删 ——
BG_ELEVATED = PANEL2
GLASS = PANEL
GLASS_SOFT = PANEL2
GLASS_BORDER = LINE
GLASS_BORDER_ACTIVE = GOLD
ACCENT = GOLD
ACCENT_DIM = GOLD
ACCENT_SOFT = "#241d10"
CYAN = BLUE
TEXT_MUTED = DIM
TEXT_DIM = MUTE
DANGER = RED
DANGER_DIM = "#4a2328"
WARNING = AMBER
OK = GREEN
DEAD = MUTE
```

`glass_card` / `soft_card` 的 `corner_radius` 改 10，`soft_card` 的底色改 `PANEL2`。
`primary_btn`：`fg_color=GOLD`、`hover_color=GOLD_STRONG`、`text_color=ON_GOLD`、`corner_radius=6`、`height=32`。
`ghost_btn`：`fg_color="transparent"`、`border_color=LINE`、`text_color=DIM`、`hover_color=PANEL2`、`text_color` hover 时 `TEXT`、`corner_radius=6`。
`danger_btn`：`fg_color="transparent"`、`border_color=RED`、`text_color=RED`、`hover_color="#2a1418"`、`corner_radius=6`。
`status_chip`：底色 `CHIP_BG`、圆角 5、`height=24`、`fg_color` 保持实底。
`health_color`：返回 `GREEN`/`RED`/`AMBER`/`MUTE`/`BLUE`（`ok`→GREEN、`error`→RED、`starting`/`unknown`→AMBER、其它→BLUE；`alive=False`→MUTE）。

新增三个工厂函数（`badge` / `info_cell` / `metric_cell`）与 `toolbar_btn`：

```python
def badge(master, text: str, color: str) -> ctk.CTkLabel:
    """状态徽章：12% 底 + 30% 边框，小圆角。"""
    return ctk.CTkLabel(
        master, text=f"  {text}  ", text_color=color,
        fg_color=_mix(color, 0.12), corner_radius=4,
        font=font(11, "bold"), height=20,
    )


def info_cell(master, label: str, value: str, ok: bool) -> ctk.CTkFrame:
    """卡片内的三格信息（连接MT5 / 是否鉴权 / 算法交易）之一。"""
    ...


def metric_cell(master, value: str, label: str, accent: str) -> ctk.CTkFrame:
    """hero 底部的三个指标格。"""
    ...


def toolbar_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """工具条小按钮：高 28、字号 12。"""
    ...
```

`_mix(color, ratio)` 是一个模块级私有函数：把 `#rrggbb` 与背景色按比例混合成半透明色（参考稿的 `#2fd3811f` 写法在 Tk 里需要实色，故手工混合）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_theme.py -v`
Expected: PASS，7 passed

- [ ] **Step 5: 跑全量测试确认没打破别的**

Run: `cd nodel_client_dashboard && python -m pytest -q`
Expected: 与基线相同 —— `2 failed, 235 passed`。两个失败用例是**改动前就存在**的基线失败（`test_managed_spawn_failure_is_reported_as_failed_daemon_event`、`test_owner_filter_actual_legacy_account_matching_and_full_inventory_save`），不属本次范围，不要试图修复，也不要让失败数增加。

- [ ] **Step 6: 提交**

```bash
git add nodel_client_dashboard/theme.py nodel_client_dashboard/test_theme.py
git commit -m "feat(dashboard): 主题换肤为平面深色 + 金色主强调"
```

---

## Task 2: viewmodels 补齐 hero 统计与卡片字段

**Files:**
- Modify: `nodel_client_dashboard/viewmodels.py`
- Modify: `nodel_client_dashboard/test_viewmodels.py`

**Interfaces:**
- Consumes: `models.RuntimeState`（字段 `process_alive` / `busy` / `ws_state` / `algo_trading_allowed` / `uptime_s` / `node_id` / `mt5_login` / `last_error` / `health`）、`models.InstanceConfig`（字段 `approval_status` / `mt5_login` / `node_id`）
- Produces:
  - `viewmodels.HeroVM(total, online, starting, stopped, pending, connected_mt5, authenticated, alerts)` — frozen dataclass
  - `viewmodels.build_hero(manager) -> HeroVM`
  - `viewmodels.ListItemVM` 新增字段（全部有默认值，追加在末尾）：`node_id: str = ""`、`server_label: str = ""`、`uptime_text: str = "--"`、`mt5_connected: bool = False`、`mt5_authenticated: bool = False`、`algo_trading_ok: bool = False`、`algo_trading_known: bool = False`、`badge_text: str = "已停止"`、`badge_tone: str = "mute"`、`pending_review: bool = False`
  - `viewmodels.format_uptime(seconds: float | None) -> str`（新增，公开）

- [ ] **Step 1: 写失败测试**

在 `test_viewmodels.py` 末尾追加：

```python
def test_format_uptime_distinguishes_none_and_zero():
    from viewmodels import format_uptime
    assert format_uptime(None) == "--"
    assert format_uptime(0) == "0天 0小时"
    assert format_uptime(3600) == "0天 1小时"
    assert format_uptime(3600 * 24 * 3 + 3600 * 14) == "3天 14小时"


def test_hero_counts_each_state():
    from viewmodels import build_hero
    mgr = ProcessManager()
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    b = InstanceConfig.create(name="b", exe_path="/x/b.exe", cwd="/x")
    c = InstanceConfig.create(name="c", exe_path="/x/c.exe", cwd="/x")
    d = InstanceConfig.create(name="d", exe_path="/x/d.exe", cwd="/x")
    e = InstanceConfig.create(name="e", exe_path="/x/e.exe", cwd="/x")
    ma, mb, mc, md, me = (mgr.add(x) for x in (a, b, c, d, e))
    ma.runtime = RuntimeState(process_alive=True, health="ok", ws_state="authenticated")
    mb.runtime = RuntimeState(process_alive=True, health="ok", ws_state="connecting")
    mc.runtime = RuntimeState(process_alive=False, busy="starting", health="starting")
    md.runtime = RuntimeState(process_alive=False, health="dead")
    e.approval_status = "pending"
    hero = build_hero(mgr)
    assert hero.total == 5
    assert hero.online == 2
    assert hero.starting == 1
    assert hero.stopped == 1
    assert hero.pending == 1
    assert hero.connected_mt5 == 2
    assert hero.authenticated == 1
    assert hero.alerts == 0


def test_hero_states_are_mutually_exclusive():
    """四个状态互斥：在线+启动中+已停止+待审核 必须等于总数。

    否则分布条的比例和会超过 1，条形图画错。
    """
    from viewmodels import build_hero
    mgr = ProcessManager()
    # 同一实例同时满足「在线」和「待审核」两个条件
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    a.approval_status = "pending"
    mp = mgr.add(a)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    # 同时满足「已停止」和「待审核」
    b = InstanceConfig.create(name="b", exe_path="/x/b.exe", cwd="/x")
    b.approval_status = "pending"
    mb = mgr.add(b)
    mb.runtime = RuntimeState(process_alive=False, health="dead")
    hero = build_hero(mgr)
    assert hero.online + hero.starting + hero.stopped + hero.pending == hero.total
    assert hero.online == 1
    assert hero.pending == 1


def test_card_badge_tone_matches_hero_counting():
    """卡片徽章的状态判定必须与 hero 分布条一致。

    一个实例同时「在线」且「待审核」时，卡片显示运行中，hero 也必须算进 online。
    """
    from viewmodels import build_hero, build_list
    mgr = ProcessManager()
    a = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    a.approval_status = "pending"
    mp = mgr.add(a)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    item = build_list(mgr, None)[0]
    assert item.badge_tone == "online"
    assert item.badge_text == "运行中"
    hero = build_hero(mgr)
    assert hero.online == 1
    assert hero.pending == 0


def test_hero_alerts_count_last_error():
    from viewmodels import build_hero
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(process_alive=True, health="error", last_error="连接断开")
    assert build_hero(mgr).alerts == 1


def test_hero_zero_instances():
    from viewmodels import build_hero
    hero = build_hero(ProcessManager())
    assert hero.total == 0
    assert hero.online == 0


def test_card_fields_reflect_runtime():
    from viewmodels import build_list
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x", status_port=18765)
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(
        process_alive=True, health="ok", ws_state="authenticated",
        algo_trading_allowed=True, uptime_s=3600 * 25, node_id="TID-1", mt5_login=88001234,
    )
    item = build_list(mgr, None)[0]
    assert item.mt5_connected is True
    assert item.mt5_authenticated is True
    assert item.algo_trading_ok is True
    assert item.algo_trading_known is True
    assert item.uptime_text == "1天 1小时"
    assert item.node_id == "TID-1"
    assert item.server_label == "账号 88001234"
    assert item.badge_text == "运行中"
    assert item.badge_tone == "online"


def test_card_algo_unknown_is_not_ok():
    from viewmodels import build_list
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="a", exe_path="/x/a.exe", cwd="/x")
    mp = mgr.add(cfg)
    mp.runtime = RuntimeState(process_alive=True, health="ok")
    item = build_list(mgr, None)[0]
    assert item.algo_trading_known is False
    assert item.algo_trading_ok is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_viewmodels.py -v`
Expected: FAIL —— `ImportError: cannot import name 'build_hero'`

- [ ] **Step 3: 实现**

`viewmodels.py` 新增：

```python
def format_uptime(seconds: float | None) -> str:
    """运行时长文案。None=未知显示 --；0=刚启动显示 0天 0小时。"""
    if seconds is None:
        return "--"
    total_hours = int(seconds) // 3600
    days, hours = divmod(total_hours, 24)
    return f"{days}天 {hours}小时"


@dataclass(frozen=True)
class HeroVM:
    total: int
    online: int
    starting: int
    stopped: int
    pending: int
    connected_mt5: int
    authenticated: int
    alerts: int
```

`build_hero(manager)`：遍历 `manager.configs()`，用 `manager.get(cfg.id)` 拿 `runtime`。

**四个状态必须互斥**，否则分布条的比例和会超过 1。判定顺序（先命中先算）：

1. `starting`：`runtime.busy in ("starting", "stopping")`
2. `online`：`runtime.process_alive` 且 `busy == ""`
3. `pending`：`cfg.approval_status == "pending"`（且不在 1/2 里）
4. `stopped`：其余

这样 `online + starting + pending + stopped == total` 恒成立。

其余指标：`connected_mt5` = `runtime.ws_state` 在 `("authenticated", "connecting", "reconnecting")`；`authenticated` = `ws_state == "authenticated"`；`alerts` = `runtime.last_error` 非空。

`ListItemVM` 追加字段（带默认值）。`build_list_item` 中填充：
- `node_id` = `rt.node_id or cfg.node_id or ""`
- `server_label` = `f"账号 {login}"`（`login` 取 `rt.mt5_login` 或 `cfg.mt5_login`，无则 `"账号 -"`）
- `uptime_text` = `format_uptime(rt.uptime_s)`
- `mt5_connected` = `rt.ws_state in ("authenticated", "connecting", "reconnecting")`
- `mt5_authenticated` = `rt.ws_state == "authenticated"`
- `algo_trading_known` = `rt.algo_trading_allowed is not None`
- `algo_trading_ok` = `rt.algo_trading_allowed is True`
- `badge_text` / `badge_tone`：状态优先级**必须与 `build_hero` 的判定顺序一致**（否则卡片上的徽章和 hero 的分布条对不上）：`busy in ("starting","stopping")`→("启动中"/"停止中","starting")；`rt.process_alive`→("运行中","online")；`cfg.approval_status=="pending"`→("待审核","pending")；否则→("已停止","offline")。
- `pending_review` = `cfg.approval_status == "pending"`

- [ ] **Step 4: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_viewmodels.py -v`
Expected: PASS（含原有 2 个用例 + 新增 8 个，共 10 个）

- [ ] **Step 5: 提交**

```bash
git add nodel_client_dashboard/viewmodels.py nodel_client_dashboard/test_viewmodels.py
git commit -m "feat(dashboard): viewmodels 增加 hero 统计与卡片字段"
```

---

## Task 3: hero_panel.py 顶部统计卡

**Files:**
- Create: `nodel_client_dashboard/hero_panel.py`
- Create: `nodel_client_dashboard/test_hero_panel.py`

**Interfaces:**
- Consumes: `viewmodels.HeroVM`、`theme.PANEL/PANEL2/LINE/GOLD/GREEN/AMBER/RED/MUTE/TEXT/DIM`、CTk 6.0 的 `ctk.CTkCanvas`
- Produces:
  - `hero_panel.HeroPanel(master, **kwargs) -> ctk.CTkFrame`
    - `.update(hero: HeroVM) -> None` —— 就地更新，不重建控件
    - `.canvas -> ctk.CTkCanvas` —— 供残影修复遍历
  - `hero_panel.distribution_segments(hero: HeroVM) -> list[tuple[str, float]]`（模块级，公开，便于测试）

- [ ] **Step 1: 写失败测试**

创建 `nodel_client_dashboard/test_hero_panel.py`：

```python
"""hero 统计卡：分布比例、零实例不崩、更新不重建控件。"""
from __future__ import annotations

import tkinter as tk

import pytest

from viewmodels import HeroVM


def test_distribution_segments_zero_total_is_empty():
    from hero_panel import distribution_segments
    hero = HeroVM(0, 0, 0, 0, 0, 0, 0, 0)
    assert distribution_segments(hero) == []


def test_distribution_segments_sum_to_one():
    from hero_panel import distribution_segments
    hero = HeroVM(5, 3, 1, 0, 1, 3, 2, 0)
    segs = distribution_segments(hero)
    assert abs(sum(ratio for _c, ratio in segs) - 1.0) < 1e-9
    assert dict(segs)["online"] == pytest.approx(0.6)


def test_distribution_skips_zero_count_states():
    from hero_panel import distribution_segments
    hero = HeroVM(4, 2, 0, 2, 0, 0, 0, 0)
    segs = dict(distribution_segments(hero))
    assert "starting" not in segs
    assert "pending" not in segs


def test_hero_panel_updates_in_place():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    root.withdraw()
    from hero_panel import HeroPanel
    panel = HeroPanel(root)
    first = panel._total_label
    panel.update(HeroVM(5, 3, 1, 1, 0, 3, 2, 0))
    assert panel._total_label is first          # 未重建
    assert "5" in panel._total_label.cget("text")
    root.destroy()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_hero_panel.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'hero_panel'`

- [ ] **Step 3: 实现 hero_panel.py**

模块结构：

```python
"""顶部 hero 统计卡：节点总数 + 状态分布 + 三个关键指标。"""
from __future__ import annotations

import customtkinter as ctk

from theme import (AMBER, BLUE, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2,
                   RED, TEXT, font, glass_card, metric_cell, mono, status_chip)
from viewmodels import HeroVM

_STATE_ORDER = (("online", GREEN), ("starting", AMBER), ("offline", MUTE), ("pending", GOLD))


def distribution_segments(hero: HeroVM) -> list[tuple[str, float]]:
    """按状态占比返回 [(状态, 比例)]，占比为 0 的状态不出现。总数为 0 时返回空表。"""
    counts = {
        "online": hero.online,
        "starting": hero.starting,
        "offline": hero.stopped,
        "pending": hero.pending,
    }
    if hero.total <= 0:
        return []
    return [(k, counts[k] / hero.total) for k, _c in _STATE_ORDER if counts[k] > 0]
```

`HeroPanel(ctk.CTkFrame)` 内部布局：
- 自身 `fg_color=PANEL`、`border_width=1`、`border_color=LINE`、`corner_radius=14`。
- `self.canvas = ctk.CTkCanvas(self, height=90, bg=PANEL, highlightthickness=0, bd=0)`，叠在 hero 顶部，`place(x=0, y=0, relwidth=1, relheight=0.5)`，`create_line` 画 32px 网格（颜色 `#1e2a3d`），并在右上角画一圈淡金色径向渐变（用若干同心 `create_oval` 递减 alpha）。网格重绘在 `<Configure>` 上做（`_redraw_texture`）。
- 主行（左）：金色 server 图标标签 + 大数字 `self._total_label`（字号 30）+ 标签「节点总数」。
- 右上：4 个 `status_chip`（在线 / 启动中 / 已停止 / 待审核），各自文案由 `update()` 填。
- 中部：`self._dist_frame`（`fg_color=PANEL2`，高 6，圆角 3），内含 4 个 `self._dist_segments[i]` 小 frame（各占宽度比例，`place(relwidth=ratio)`）。
- 下部：分隔线 + 3 个 `metric_cell`（已连接MT5 / 已鉴权 / 异常提醒）。

`update(hero)`：更新数字、各 chip 文案、分布条各段宽度（用 `place(relwidth=ratio)`，`relx` 累加；比例为 0 的段 `place_forget()`）、三个指标数值。**只改文案与位置，不 destroy/recreate 控件。**

纹理重绘函数 `_redraw_texture(self, _event=None) -> None`：先 `self.canvas.delete("all")`，再按 32px 间距 `create_line` 画横竖网格线（颜色 `#182338`），宽高取自 `canvas.winfo_width()`/`winfo_height()`，≤1 时直接返回（尚未完成布局）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_hero_panel.py -v`
Expected: PASS，4 passed

- [ ] **Step 5: 提交**

```bash
git add nodel_client_dashboard/hero_panel.py nodel_client_dashboard/test_hero_panel.py
git commit -m "feat(dashboard): 新增 hero 统计卡组件"
```

---

## Task 4: node_card.py 节点卡片

**Files:**
- Create: `nodel_client_dashboard/node_card.py`
- Create: `nodel_client_dashboard/test_node_card.py`

**Interfaces:**
- Consumes: `viewmodels.ListItemVM`、`theme` 全部工厂
- Produces:
  - `node_card.NodeCard(master, vm: ListItemVM, *, on_select=None, on_open=None, on_start=None, on_stop=None, on_remove=None, on_context=None) -> ctk.CTkFrame`
    - `.update(vm: ListItemVM) -> None` —— 差分更新，不重建控件
    - `.vm -> ListItemVM` —— 当前快照
    - `.card -> ctk.CTkFrame` —— 外框（供选中态换边框色）
    - `.selected -> bool`
  - `node_card.set_grid_columns(frame: ctk.CTkFrame, columns: int) -> None` —— 网格列数重排工具

- [ ] **Step 1: 写失败测试**

创建 `nodel_client_dashboard/test_node_card.py`：

```python
"""节点卡：差分更新不重建控件、状态文案切换、空态不崩。"""
from __future__ import annotations

import tkinter as tk

import pytest

from models import InstanceConfig
from process_manager import ProcessManager
from viewmodels import build_list


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    r.withdraw()
    yield r
    r.destroy()


def _vm(root, **kw):
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="n1", exe_path="/x/n1.exe", cwd="/x")
    mp = mgr.add(cfg)
    for k, v in kw.items():
        setattr(mp.runtime, k, v)
    return build_list(mgr, None)[0]


def test_card_update_does_not_rebuild(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", ws_state="authenticated")
    card = NodeCard(root, vm)
    title = card._name_label
    card.update(vm)
    assert card._name_label is title


def test_card_badge_switches_with_state(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    assert "运行中" in card._badge.cget("text")
    stopped = _vm(root, process_alive=False, health="dead")
    card.update(stopped)
    assert "已停止" in card._badge.cget("text")


def test_card_info_cells_show_values(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", ws_state="authenticated",
             algo_trading_allowed=True)
    card = NodeCard(root, vm)
    assert card._cells[0].value_label.cget("text") == "已连接"
    assert card._cells[1].value_label.cget("text") == "已鉴权"
    assert card._cells[2].value_label.cget("text") == "已开启"


def test_card_uptime_none_shows_dash(root):
    from node_card import NodeCard
    card = NodeCard(root, _vm(root))
    assert card._uptime_label.cget("text") == "--"


def test_set_grid_columns_repositions_without_rebuild(root):
    from node_card import NodeCard, set_grid_columns
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    grid = tk.Frame(root)
    grid.grid_columnconfigure(0, weight=1)
    card.card.grid(row=0, column=0, sticky="nsew")
    set_grid_columns(grid, 3)
    assert int(card.card.grid_info()["column"]) == 0
    set_grid_columns(grid, 2)
    assert int(card.card.grid_info()["column"]) == 0


def test_set_grid_columns_switches_to_two_columns(root):
    from node_card import NodeCard, set_grid_columns
    vm = _vm(root, process_alive=True, health="ok")
    grid = tk.Frame(root)
    cards = [NodeCard(grid, vm) for _ in range(3)]
    for i, c in enumerate(cards):
        c.card.grid(row=0, column=i, sticky="nsew")
    set_grid_columns(grid, 2)
    positions = [(c.card.grid_info()["row"], c.card.grid_info()["column"]) for c in cards]
    assert positions == [(0, 0), (0, 1), (1, 0)]
    # 旧的 uniform 必须清掉，否则第 3 列的权重会串到前两列
    assert grid.grid_columnconfigure(2)["uniform"] in ("", "gridcol")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_node_card.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'node_card'`

- [ ] **Step 3: 实现 node_card.py**

模块导入：`from theme import (AMBER, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2, RED, TEXT, badge, font, ghost_btn, info_cell, mono)`、`from viewmodels import ListItemVM`。

`theme.info_cell` 返回的 frame 上要挂 `value_label` 属性（供测试断言与差分更新使用），`NodeCard` 通过 `cell.value_label` 访问。

`NodeCard.__init__(self, master, vm, *, on_select=None, on_open=None, on_start=None, on_stop=None, on_remove=None, on_context=None)`：
- 外框 `self.card = ctk.CTkFrame(master, fg_color=PANEL, border_width=1, border_color=LINE, corner_radius=10)`。
- 头部：状态点 `self._dot`（`CTkLabel`，文本 `"●"`，宽 8 高 8，用 `fg_color` 做圆点更准：改用 8x8 的 `CTkFrame` + `corner_radius=4`）+ `self._name_label`（名称，14 粗体）+ `self._badge`（`theme.badge`）。
- 元信息行：`self._server_label`（`账号 xxxxx`，12）+ `self._id_label`（`ID xxxxx`，等宽 11）。
- 信息格：`self._cells` 为 3 个 tuple，顺序固定 `("连接MT5", "是否鉴权", "算法交易")`；由 `theme.info_cell` 创建，等宽平分。
- 底部：`self._uptime_label`（左，`⏱` + 文本，11）+ 按钮组：删除（`ghost_btn` 红色描边，宽 40）、启停按钮（`self._toggle_btn`，宽 72，文案「启动」/「停止」/「启动中…」）。
- 交互：单击 `self.card` 触发 `on_select`；双击触发 `on_open`；右键（`<Button-3>`）触发 `on_context`；删除按钮触发 `on_remove`；启停按钮按当前状态触发 `on_start` 或 `on_stop`。
- 点击绑定需递归到所有子控件（沿用 `app_ui._create_list_row` 里的 `_bind` 写法），但**按钮自身除外**（避免按钮点击又触发选中）。

`update(vm)` 差分：
- 名称变化 → 改 `self._name_label` 文案。
- 状态变化 → 改状态点颜色、徽章文案/颜色、启停按钮文案与可用性。
- `server_label` / `node_id` 变化 → 改对应标签。
- 三格信息的 (ok, value) 变化 → 改对应 `value_label` 的文案与颜色。
- `uptime_text` 变化 → 改 `self._uptime_label`。
- **不重建任何控件**（测试 `test_card_update_does_not_rebuild` 守住）。

`set_grid_columns(frame, columns)`：遍历 `frame.grid_slaves()`，按 index 重新 `grid(row=index // columns, column=index % columns, sticky="nsew", padx=6, pady=6)`。

**必须先清掉旧的 `uniform`**：Tk 的 `uniform` 一旦设置就会把该列与同名分组绑定，列数从 3 降到 2 时第 3 列残留的 `uniform='g'` 会把它的权重并到其它列，导致列宽错乱。做法：

```python
def set_grid_columns(frame: ctk.CTkFrame, columns: int) -> None:
    """按新列数重排网格里的卡片：只改位置与权重，不重建控件。"""
    columns = max(1, int(columns))
    existing = frame.grid_columnconfigure
    for col in range(0, 8):
        try:
            frame.grid_columnconfigure(col, weight=0, uniform="")
        except tk.TclError:
            break
    for col in range(columns):
        frame.grid_columnconfigure(col, weight=1, uniform="gridcol")
    slaves = sorted(frame.grid_slaves(), key=lambda w: (w.grid_info()["row"], w.grid_info()["column"]))
    for index, widget in enumerate(slaves):
        widget.grid(row=index // columns, column=index % columns, sticky="nsew", padx=6, pady=6)
```

**已实测验证**（本机 CTk 6.0.0）：
- 网格填充滚动区宽度的前提是外层 `CTkScrollableFrame` 也要 `grid_columnconfigure(0, weight=1)`；否则内层 `CTkFrame` 只有 792px 而滚动区有 1472px，卡片挤在左侧。
- 3 列 → 2 列切换后卡片位置正确（`(0,0) (0,1) (1,0) (1,1) (2,0)`），宽度自动均分。
- `CTkCanvas` 叠加在 `CTkFrame` 上用 `place(x=0, y=0, relwidth=1, relheight=0.5)` 可行，`create_line` / `create_oval` 正常绘制。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_node_card.py -v`
Expected: PASS，6 passed

- [ ] **Step 5: 提交**

```bash
git add nodel_client_dashboard/node_card.py nodel_client_dashboard/test_node_card.py
git commit -m "feat(dashboard): 新增节点卡片组件"
```

---

## Task 5: 主窗口重排为 hero + 网格

**Files:**
- Modify: `nodel_client_dashboard/app_ui.py`（`_build_layout` 重排；删除 `_create_list_row` / `_apply_list_row` / `_sync_action_buttons`；新增 `_create_node_card` / `_apply_node_card` / `_relayout_grid` / `_show_card_menu` / `_build_card_menu` / `_tick_clock` / `_start_by_id` / `_stop_by_id` / `_open_cwd_by_id` / `_refresh_health_by_id`）
- Modify: `nodel_client_dashboard/test_dashboard_ui_auth.py`（新增用例 + `_login` 辅助）

**Interfaces:**
- Consumes: `hero_panel.HeroPanel`、`node_card.NodeCard`、`node_card.set_grid_columns`、`viewmodels.build_hero`
- Produces:
  - `app_ui.DashboardApp._build_layout() -> None`（签名不变，内部重排）
  - `app_ui.DashboardApp._open_detail_dialog() -> None`（新增；双击节点卡时调用）
  - `app_ui.DashboardApp._open_detail_for(instance_id: str) -> None`（新增）
  - `app_ui.DashboardApp._relayout_grid() -> None`（新增；窗口宽度变化时重排列数）
  - `app_ui.DashboardApp._show_card_menu(instance_id: str, event) -> None`（新增；节点卡右键菜单）
  - `app_ui.DashboardApp._build_card_menu(instance_id: str) -> tk.Menu`（新增；构造菜单，测试用）
  - `app_ui.CARD_MENU_ITEMS`（新增；右键菜单项元组 `("打开目录", "刷新健康", "移除实例")`）
  - `app_ui.DashboardApp._tick_clock() -> None`（新增；顶栏时钟每秒刷新）
  - `app_ui.DashboardApp._start_by_id(instance_id: str) -> None` / `_stop_by_id` / `_open_cwd_by_id` / `_refresh_health_by_id`（新增）
  - 保留：`self.list_frame`（改为网格容器，名字不变以免破坏 `_bind_list`）、`self._list_rows`（改为 `dict[str, NodeCard]`）、`self._chip_widgets`、`self._detail_snap`、`self._log_snap`
  - 新增属性：`self.hero_panel`、`self.node_grid`、`self._empty_label`、`self._clock_label`

- [ ] **Step 1: 写失败测试**

**前置事实（已实测）**：`ProcessManager.add()` 会走 `session.perform("add_instance", ...)`，会话未激活时抛 `DashboardAuthError`。而 `desktop` fixture 默认**没有**登录（`app.session.active` 为 `False`）。所以新测试必须先 `establish` + `_authenticated()`：

```python
def _login(app, api):
    """把测试用的 app 变成已登录状态。desktop fixture 默认未登录。"""
    api.principal["is_admin"] = True     # 管理员可拥有任意实例
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
```

在 `test_dashboard_ui_auth.py` 追加：

```python
def test_main_layout_has_hero_and_node_grid(desktop):
    app, api, _tmp, _errs = desktop
    _login(app, api)
    assert hasattr(app, "hero_panel")
    assert hasattr(app, "node_grid")
    assert app.hero_panel is not None


def test_node_cards_are_created_for_instances(desktop, tmp_path):
    app, api, tmp, _errs = desktop
    _login(app, api)
    cfg = InstanceConfig.create(name="card-1", exe_path=str(tmp / "c1.exe"), cwd=str(tmp))
    app.manager.add(cfg)
    app._bind_list()
    assert cfg.id in app._list_rows
    assert isinstance(app._list_rows[cfg.id], app_ui.NodeCard)


def test_grid_relayout_keeps_all_cards(desktop, tmp_path):
    app, api, tmp, _errs = desktop
    _login(app, api)
    for i in range(4):
        cfg = InstanceConfig.create(name=f"n{i}", exe_path=str(tmp / f"n{i}.exe"), cwd=str(tmp))
        app.manager.add(cfg)
    app._bind_list()
    app._relayout_grid()
    app._relayout_grid()
    assert len(app._list_rows) == 4


def test_empty_state_shows_when_no_instances(desktop):
    app, api, _tmp, _errs = desktop
    _login(app, api)
    app._bind_list()
    assert app._empty_label.winfo_exists()


def test_clock_label_shows_current_time(desktop):
    app, api, _tmp, _errs = desktop
    _login(app, api)
    app.update_idletasks()
    text = app._clock_label.cget("text")
    assert len(text) == 19
    assert text[4] == "-" and text[10] == " "


def test_card_context_menu_has_three_items(desktop, tmp_path):
    """右键菜单只放三项：打开目录 / 刷新健康 / 移除。"""
    app, api, tmp, _errs = desktop
    _login(app, api)
    cfg = InstanceConfig.create(name="ctx-1", exe_path=str(tmp / "x1.exe"), cwd=str(tmp))
    app.manager.add(cfg)
    app._bind_list()
    calls = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(app, "_open_cwd_by_id", lambda iid: calls.append(("cwd", iid)))
    try:
        menu = app._build_card_menu(cfg.id)
        menu.invoke(0)          # 第一项 = 打开目录
    finally:
        monkey.undo()
    assert calls == [("cwd", cfg.id)]
    assert app_ui.CARD_MENU_ITEMS == ("打开目录", "刷新健康", "移除实例")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_dashboard_ui_auth.py -v -k "hero or node_cards or relayout"`
Expected: FAIL —— `AttributeError: 'DashboardApp' object has no attribute 'hero_panel'`

同时确认 `_login` 辅助已加入测试文件顶部（Task 5 的 Step 1 已定义，此处直接复用）。

- [ ] **Step 3: 实现主窗口重排**

在 `app_ui.py` 中：

**(a) 顶栏 `_build_layout` 内改造**：保留品牌与右侧按钮区，但改为参考稿结构 —— 左侧「亿点点」+ 竖分隔线 + 「节点控制台」；右侧：等宽时钟 `self._clock_label`（每秒刷新）、用户名标签、`self.version_btn`、「退出登录」、「连接配置」。三个安全 chip（仅本机回环 / 禁止公网绑定 / 支持 TLS/WS）从顶栏移除，改为 hero 下方的 DIM 小字一行。

**(b) 主体**：`self` 的 row 1 放 `body = ctk.CTkScrollableFrame(self, fg_color=BG)`（替代原来的 `body = ctk.CTkFrame` + 内嵌 `left`/`right`）。

**模块级导入**：`app_ui.py` 顶部要 `from hero_panel import HeroPanel` 和 `from node_card import NodeCard, set_grid_columns` —— 测试断言 `isinstance(app._list_rows[cfg.id], app_ui.NodeCard)` 需要模块级可见。`hero_panel` 导入 `theme` 与 `viewmodels`，无循环依赖（`viewmodels` 只依赖 `models`）。
- `self.hero_panel = HeroPanel(body)`，`grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 0))`。
- 安全声明小字一行（`仅本机回环 · 禁止公网绑定 · 支持 TLS/WS`）。
- `section-head` 行：标题「MT5 节点」+ 右侧工具条按钮（全部启动 / 全部停止 / 全部开守护 / 全部关守护 / 导入节点 / 手工添加 / 手工替换全部），复用现有 `_batch_start` / `_batch_stop` / `_batch_daemon_on` / `_batch_daemon_off` / `_import_node` / `_add_instance` / `_batch_replace` 处理器。
- `self.node_grid = ctk.CTkFrame(body, fg_color="transparent")`，`grid(row=2, column=0, sticky="nsew")`，`grid_columnconfigure` 三列等权。

**注意**：`body` 是 `CTkScrollableFrame`，必须 `body.grid_columnconfigure(0, weight=1)`，否则内层 `node_grid` 不会撑满滚动区宽度（实测：不加时内层 792px 而滚动区 1472px，卡片挤在左侧）。

**(c) 删除**原 `left`（列表卡）、`right`（标题卡 / 操作卡 / 摘要卡）的构建代码，以及 `_create_list_row` / `_apply_list_row`。**保留** `_bind_detail` 的数据计算逻辑，但不再渲染到右侧 —— 改为只更新 `self._detail_snap`，供详情弹窗读取。

**(d) 新增**：
```python
def _create_node_card(self, vm: ListItemVM) -> NodeCard:
    """建一张节点卡：单击选中、双击开详情、右键出菜单、启停与删除走卡片按钮。"""
    return NodeCard(
        self.node_grid, vm,
        on_select=self._select,
        on_open=lambda iid=vm.id: self._open_detail_for(iid),
        on_start=lambda iid=vm.id: self._start_by_id(iid),
        on_stop=lambda iid=vm.id: self._stop_by_id(iid),
        on_remove=lambda iid=vm.id: self._remove_instance(iid),
        on_context=lambda iid=vm.id, ev=None: self._show_card_menu(iid, ev),
    )

def _apply_node_card(self, card: NodeCard, vm: ListItemVM) -> None:
    card.update(vm)

def _relayout_grid(self) -> None:
    """按窗口宽度重排网格列数：>=1180 三列，>=820 两列，否则一列。"""
    width = self.winfo_width() or 1200
    columns = 3 if width >= 1180 else (2 if width >= 820 else 1)
    set_grid_columns(self.node_grid, columns)
```

**(e) `_bind_list` 改造**：遍历 `build_list(self.manager, self._selected_id)`，新建/更新卡片，删除多余卡片；卡片用 `self.node_grid.grid(row=..., column=...)` 定位（不再 `pack`）。首次绑定后调用 `_relayout_grid()`。

**(f) `_bind_detail` 改造**：仍计算 `detail = build_detail(mp)`，但只更新 `self._detail_snap`，**不创建任何控件**。原 `_bind_chips` / `_sync_action_buttons` / `summary` 相关的控件引用全部移除或改为在弹窗内实现（Task 6）。

**(g) 启动/停止按 id 的辅助**：
```python
def _start_by_id(self, instance_id: str) -> None:
    self._selected_id = instance_id
    self._start()

def _stop_by_id(self, instance_id: str) -> None:
    self._selected_id = instance_id
    self._stop()
```
（原 `_start` / `_stop` 已从 `self._selected()` 取实例。）

**(h) 选中态**：`NodeCard.update` 里根据 `vm.selected` 切换边框色（`GOLD` vs `LINE`）。

**(i) 顶栏时钟**：新增 `self._clock_label`（等宽字体，`Consolas`），文本形如 `2026-11-01 21:52:30`，每秒刷新。实现：

```python
def _tick_clock(self) -> None:
    """顶栏时钟：每秒走动。"""
    if self._quitting:
        return
    try:
        label = self._clock_label
        if label is not None and label.winfo_exists():
            label.configure(text=time.strftime("%Y-%m-%d %H:%M:%S"))
    except tk.TclError:
        return
    self.after(1000, self._tick_clock)
```

在 `__init__` 的 `self.after(1000, self._tick_ui)` 旁边加 `self.after(1000, self._tick_clock)`。

**(j) 空态**：没有实例时，网格区显示一行提示文案（不崩溃、分布条不除零）。在 `_bind_list` 里当 `items` 为空时显示 `self._empty_label`（`ctk.CTkLabel`，文字「暂无节点 · 点击「导入节点」或「手工添加」开始」，色 `MUTE`），否则 `place_forget()`。

**(k) 右键菜单**：在节点卡上右键弹出 `tk.Menu`，只放三项：打开目录 / 刷新健康 / 移除实例（规格书 §8 第 4 条：其余操作统一在详情弹窗）。

**拆成两个方法**，否则没法测试 —— `tk.Menu.tk_popup` 在 Windows 上会阻塞在内部事件循环里直到用户选择，测试里直接调用会挂起：

```python
# 节点卡右键菜单的项（顺序即菜单顺序）
CARD_MENU_ITEMS = ("打开目录", "刷新健康", "移除实例")


def _build_card_menu(self, instance_id: str) -> tk.Menu:
    """构造节点卡右键菜单：打开目录 / 刷新健康 / 移除。"""
    menu = tk.Menu(self, tearoff=0)
    menu.add_command(label="打开目录", command=lambda: self._open_cwd_by_id(instance_id))
    menu.add_command(label="刷新健康", command=lambda: self._refresh_health_by_id(instance_id))
    menu.add_separator()
    menu.add_command(label="移除实例", command=lambda: self._remove_instance(instance_id))
    return menu

def _show_card_menu(self, instance_id: str, event) -> None:
    """弹出节点卡右键菜单。"""
    menu = self._build_card_menu(instance_id)
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        # 必须释放 grab，否则主窗口会失去输入权（Review Focus 第 5 条）
        menu.grab_release()
```

**注意**：`tk.Menu.entrycget(i, "label")` 在 Tkinter 里不支持 —— 实测抛 `TclError: unknown option "-label"`。要读菜单项标签得用 `menu.entryconfig(i, "label")`（返回 5 元组，最后一项是标签文本），但更稳的做法是把菜单项文案写成模块级常量 `CARD_MENU_ITEMS` 直接断言。测试里用 `menu.invoke(0)` 触发第一项，验证回调被正确接线。

新增两个按 id 的辅助（`_open_cwd` / `_refresh_health` 都从 `self._selected()` 取实例）：
```python
def _open_cwd_by_id(self, instance_id: str) -> None:
    self._selected_id = instance_id
    self._open_cwd()

def _refresh_health_by_id(self, instance_id: str) -> None:
    self._selected_id = instance_id
    self._refresh_health()
```

`NodeCard` 侧：在卡片上绑定 `<Button-3>`，通过构造参数 `on_context=None` 回调到主窗口。**注意**：`tk.Menu.tk_popup` 在 Windows 上会阻塞到用户选择，弹完必须 `grab_release()`，否则主窗口失去输入权（对应 Review Focus 第 5 条）。

**(l) 顶栏安全声明**：三个安全 chip（仅本机回环 / 禁止公网绑定 / 支持 TLS/WS）从顶栏移除，改为 hero 下方的 `MUTE` 色小字一行：`仅本机回环 · 禁止公网绑定 · 支持 TLS/WS`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_dashboard_ui_auth.py -v`
Expected: PASS（新增 6 个用例 + 原有用例全部通过）。若某个原有用例因布局重排而失败，说明它断言了旧布局结构 —— 改成断言新结构，不要删断言。

- [ ] **Step 5: 跑全量测试**

Run: `cd nodel_client_dashboard && python -m pytest -q`
Expected: 与基线一致 —— `2 failed`（Task 1 记录的两个基线失败），失败用例名不得增加。

- [ ] **Step 6: 提交**

```bash
git add nodel_client_dashboard/app_ui.py nodel_client_dashboard/test_dashboard_ui_auth.py
git commit -m "feat(dashboard): 主窗口重排为 hero + 节点网格"
```

---

## Task 6: node_detail_dialog.py 详情弹窗

**Files:**
- Create: `nodel_client_dashboard/node_detail_dialog.py`
- Modify: `nodel_client_dashboard/app_ui.py`（新增 `_open_detail_dialog` / `_open_detail_for`，删除 `_sync_action_buttons` 中对已删控件的引用；`_quit_app` 里关闭弹窗）
- Modify: `nodel_client_dashboard/dashboard_auth_ui.py`（`_show_login` 里关闭残留的详情弹窗）

**Interfaces:**
- Consumes: `viewmodels.DetailVM` / `ChipVM`、`viewmodels.build_detail`、`session_ui.SessionDialog`、`dialog_window.show_centered`、`theme` 工厂、`process_manager.ManagedProcess`
- Produces:
  - `node_detail_dialog.NodeDetailDialog(master, mp, *, on_refresh=None) -> SessionDialog`
    - `.refresh() -> None` —— 重新读 `build_detail(mp)` 并就地更新
    - 属性 `self.detail`、`self.chips`、`self.actions`
  - `app_ui.DashboardApp._open_detail_dialog() -> None`
  - `app_ui.DashboardApp._open_detail_for(instance_id: str) -> None`

- [ ] **Step 1: 写失败测试**

**前置事实（已实测）**：`app.manager.add()` 需要已登录会话；`desktop` fixture 默认未登录。用 Task 5 定义的 `_login(app, api)` 辅助（管理员身份）。

在 `test_dashboard_ui_auth.py` 追加：

```python
def test_detail_dialog_opens_for_instance(desktop, tmp_path):
    app, api, tmp, _errs = desktop
    _login(app, api)
    cfg = InstanceConfig.create(name="det-1", exe_path=str(tmp / "d1.exe"), cwd=str(tmp))
    app.manager.add(cfg)
    app._open_detail_for(cfg.id)
    app.update_idletasks()
    dlg = app._detail_dialog
    assert dlg is not None
    assert "det-1" in dlg.title_label.cget("text")
    dlg.destroy()
    app._detail_dialog = None


def test_detail_dialog_refresh_updates_chips(desktop, tmp_path):
    app, api, tmp, _errs = desktop
    _login(app, api)
    cfg = InstanceConfig.create(name="det-2", exe_path=str(tmp / "d2.exe"), cwd=str(tmp))
    mp = app.manager.add(cfg)
    app._open_detail_for(cfg.id)
    app.update_idletasks()
    dlg = app._detail_dialog
    mp.runtime.process_alive = True
    mp.runtime.health = "ok"
    dlg.refresh()
    assert any(c.key == "online" for c in dlg.detail.chips)
    dlg.destroy()
    app._detail_dialog = None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_dashboard_ui_auth.py -v -k "detail_dialog"`
Expected: FAIL —— `AttributeError: 'DashboardApp' object has no attribute '_detail_dialog'`

- [ ] **Step 3: 实现 node_detail_dialog.py**

```python
"""节点详情弹窗：承接原右侧详情区的全部信息与操作。"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from dialog_window import show_centered
from session_ui import SessionDialog
from theme import (DANGER, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2, RED, TEXT,
                   font, ghost_btn, glass_card, mono, primary_btn, status_chip)
from viewmodels import DetailVM, build_detail


class NodeDetailDialog(SessionDialog):
    """节点详情。宽度 720、高度 560。"""

    _W = 720
    _H = 560

    def __init__(self, master, mp, *, on_refresh=None) -> None:
        super().__init__(master)
        self.title(f"节点详情 · {mp.cfg.name}")
        self.resizable(False, False)
        self.configure(fg_color="transparent")
        self.mp = mp
        self.on_refresh = on_refresh
        self.detail: DetailVM = build_detail(mp)
        self._chip_widgets: dict[str, ctk.CTkLabel] = {}
        self._build()
        self.after(20, self._show_centered)
```

`_build()` 分区（自上而下）：
1. 头部卡（`glass_card`）：`self.title_label`（节点名，18 粗体）+ `self.subtitle_label`（路径 · 工作目录，DIM 12）+ `self.chip_row`（芯片行）。
2. 操作卡：`self.actions` frame —— 启动 / 停止（`primary_btn` / `danger_btn`）/ 节点配置 / 刷新健康 / 打开目录 + 守护进程 `CTkSwitch`。
3. 摘要卡：`self.summary` 标签（等宽 12，左对齐，`text` 来自 `detail.summary`）。
4. 策略任务 + 运行日志：把 `app_ui.py` 中 `if False:` 块里的 `self.tasks` / `self.log_box` 迁到这里，默认收起（`grid_remove`），保留「运行日志」标题 + 「清空显示」按钮。**保持默认隐藏，行为与现状一致。**
5. 底部按钮行：编辑实例 / 绑定后台节点 / 重置专属令牌 / 移除实例 / 关闭。

所有操作回调通过 `self.dashboard`（`SessionDialog` 已把 `master.dashboard` 挂上）调用主窗口的处理器：`self.dashboard._edit_instance(mp.cfg.id)`、`self.dashboard._bind_instance()`、`self.dashboard._rotate_credential()`、`self.dashboard._remove_instance(mp.cfg.id)`、`self.dashboard._edit_node_env()`、`self.dashboard._refresh_health()`、`self.dashboard._open_cwd()`、`self.dashboard._start()`、`self.dashboard._stop()`。

`refresh()`：重新 `self.detail = build_detail(self.mp)`，更新标题/副标题/芯片/摘要，并调用 `self.on_refresh` 若提供。

`_show_centered()`：调用 `show_centered(self, self._W, self._H)`。

- [ ] **Step 4: 在 app_ui.py 接上弹窗**

新增：
```python
self._detail_dialog: "NodeDetailDialog | None" = None

def _open_detail_for(self, instance_id: str) -> None:
    """双击节点卡：打开该节点的详情弹窗。"""
    self._select(instance_id)
    self._open_detail_dialog()

def _open_detail_dialog(self) -> None:
    mp = self._selected()
    if mp is None:
        return
    from node_detail_dialog import NodeDetailDialog
    if self._detail_dialog is not None:
        try:
            self._detail_dialog.destroy()
        except tk.TclError:
            pass
    dlg = NodeDetailDialog(self, mp, on_refresh=self._safe_refresh)
    self._detail_dialog = dlg
```

`self._select(instance_id)` 已存在于 `app_ui.py`，它会设 `self._selected_id` 并刷新列表，无需改动。

**注意**：`SessionDialog.__init__` 里 `self.session = master.session` 且 `master.session.check()`。`SessionDialog` 还要求 `master` 上有 `.dashboard` 属性 —— `DashboardApp` 通过 `DashboardAuthUI._auth_init` 设置了 `self.dashboard = self`，已满足。

弹窗内的操作按钮调用主窗口处理器时，主窗口的 `_start` / `_stop` 取的是 `self._selected()`。由于 `_open_detail_for` 先调了 `_select`，`self._selected_id` 已指向该实例；但用户在弹窗里点「移除」后 `_selected_id` 可能失效 —— 弹窗应在实例被移除时自行关闭。处理方式：`NodeDetailDialog` 在操作回调后检查 `self.dashboard._selected()` 是否仍是 `self.mp.cfg.id`，不是则 `self.destroy()`。

并在 `DashboardApp.__init__` 里初始化 `self._detail_dialog = None`；在 `_quit_app` 里 `if self._detail_dialog is not None: self._detail_dialog.destroy()`（避免退出时残留窗口）。

**注意**：`dashboard_auth_ui._show_login()` 会 `destroy()` 主窗口的所有子控件。详情弹窗是 `CTkToplevel`，不是子控件，不会被销毁 —— 需要在 `_show_login` 里显式关掉它，否则登录页会浮在弹窗之下。加一行：

```python
if getattr(self, "_detail_dialog", None) is not None:
    try:
        self._detail_dialog.destroy()
    except tk.TclError:
        pass
    self._detail_dialog = None
```

**删除** `_sync_action_buttons` 方法及其调用（它引用已删的 `self.btn_start` / `self.btn_stop`），以及 `_bind_detail` 中对 `self.daemon_var` / `self.daemon_switch` / `self.summary` 的引用。弹窗内的启动/停止按钮可用性由弹窗自己根据 `mp.runtime` 判定。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd nodel_client_dashboard && python -m pytest test_dashboard_ui_auth.py -v`
Expected: PASS

- [ ] **Step 6: 跑全量测试**

Run: `cd nodel_client_dashboard && python -m pytest -q`
Expected: 与基线一致，`2 failed`（基线），失败名不增加。

- [ ] **Step 7: 提交**

```bash
git add nodel_client_dashboard/node_detail_dialog.py nodel_client_dashboard/app_ui.py nodel_client_dashboard/dashboard_auth_ui.py nodel_client_dashboard/test_dashboard_ui_auth.py
git commit -m "feat(dashboard): 新增节点详情弹窗，承接原右侧详情"
```

---

## Task 7: 登录页与对话框统一皮肤

**Files:**
- Modify: `nodel_client_dashboard/login_view.py`
- Modify: `nodel_client_dashboard/app_ui.py`（`EditInstanceDialog`）
- Modify: `nodel_client_dashboard/import_dialog.py`、`env_dialog.py`、`version_dialog.py`、`version_picker.py`、`connection_dialog.py`（仅在 Task 1 换肤后仍有硬编码旧色时调整）

**Interfaces:**
- Consumes: `theme` 全部工厂
- Produces: 无新接口；本任务只调整视觉。

- [ ] **Step 1: 找出残留的旧色硬编码**

Run: `cd nodel_client_dashboard && rg -n "#070B14|#0C1424|#121C2E|#162235|#2A3F5F|#2EE6A8|#1A9E72|#143D32|#5CE1FF|#E8EEF8|#8B9BB4|#5C6B82|#FF5C7A|#8B2E40|#F5B942|#6B7A90|#0F1A2C|#04140F"`
Expected: 只剩 `theme.py`（别名定义）与测试文件里的断言。若 `app_ui.py` 等处仍有 `#04140F`（旧主按钮文字色），替换为 `theme.ON_GOLD`。

- [ ] **Step 2: 改登录页**

`login_view.py`：
- 卡片底从 `glass_card` 换新实现（自动生效），卡片圆角 10。
- 标题「亿点点登录」改用 `GOLD`（原为 `ACCENT`，别名后已是金色，确认即可）。
- 输入框 `fg_color=BG_ELEVATED`（→`PANEL2`）、`border_color=LINE`、`corner_radius=6`、`height=34`。
- 标签文字色 `TEXT_MUTED`（→`DIM`）。
- 状态提示文字：`missing` 时 `DANGER`（→`RED`），否则 `TEXT_MUTED`（→`DIM`）。
- 副标题改为「使用后台账户，按本人节点权限进行管理」保持不变。

- [ ] **Step 3: 改主窗口内的编辑对话框**

`app_ui.py` 的 `EditInstanceDialog`：标题「安全编辑」标签用 `GOLD`；输入框圆角 6；`corner_radius=10` 的 `glass_card` 自动生效。

- [ ] **Step 4: 核对各对话框**

打开 `import_dialog.py`、`env_dialog.py`、`version_dialog.py`、`version_picker.py`、`connection_dialog.py`，逐一确认：卡片用 `glass_card`/`soft_card`（自动新皮肤）、按钮用 `primary_btn`/`ghost_btn`/`danger_btn`、输入框圆角 6、`CTkProgressBar` 的 `progress_color` 用 `GOLD`、`CTkOptionMenu` 的按钮色用 `PANEL2`+`LINE`。**只改颜色/圆角/字号，不动逻辑与文案。**

- [ ] **Step 5: 跑全量测试**

Run: `cd nodel_client_dashboard && python -m pytest -q`
Expected: 与基线一致，`2 failed`（基线），失败名不增加。

- [ ] **Step 6: 提交**

```bash
git add nodel_client_dashboard/login_view.py nodel_client_dashboard/app_ui.py nodel_client_dashboard/import_dialog.py nodel_client_dashboard/env_dialog.py nodel_client_dashboard/version_dialog.py nodel_client_dashboard/version_picker.py nodel_client_dashboard/connection_dialog.py
git commit -m "style(dashboard): 登录页与各对话框统一为新皮肤"
```

---

## Task 8: 状态点动画、残影修复与最终回归

**Files:**
- Modify: `nodel_client_dashboard/node_card.py`（状态点动画）
- Modify: `nodel_client_dashboard/app_ui.py`（`_repair_ui_after_restore` 纳入手绘图元）
- Modify: `nodel_client_dashboard/test_busy_status.py`、`test_dashboard_ui_auth.py`（硬编码色值改引用 `theme` 常量）

**Interfaces:**
- Consumes: `busy_status.BusyStatus` 的定时器思路（不复用其类，它是标签动画；这里用 `after` 循环）
- Produces: 无新公开接口。

- [ ] **Step 1: 写失败测试**

在 `test_node_card.py` 追加：

```python
def test_starting_state_pulses(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=False, busy="starting", health="starting")
    card = NodeCard(root, vm)
    assert card._pulse_job is not None
    card.stop_animation()
    assert card._pulse_job is None


def test_online_state_has_no_pulse(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    assert card._pulse_job is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd nodel_client_dashboard && python -m pytest test_node_card.py -v -k pulse`
Expected: FAIL —— `AttributeError: 'NodeCard' object has no attribute '_pulse_job'`

- [ ] **Step 3: 实现状态点脉冲**

`NodeCard`：
- 属性 `self._pulse_job: str | None = None`。
- `update()` 中若新状态是 `starting`（`vm.badge_tone == "starting"`）且当前无脉冲 → 启动循环；否则停止。
- 脉冲循环（用 `self.after(450, ...)`，在颜色之间交替）：状态点在 `GREEN`/`AMBER` 与 `AMBER`/`MUTE` 之间切换透明度。用 `self._dot.configure(fg_color=...)`。
- `stop_animation()`：取消 `self._pulse_job` 并置 `None`。
- 卡片销毁时（override `destroy`）调用 `stop_animation()`。

- [ ] **Step 4: 把手绘图元纳入残影修复**

`app_ui.py` 的 `_repair_ui_after_restore` 中，现有 `_force_ctk_redraw(self)` 遍历所有控件调 `_draw`。`CTkCanvas` 没有 `_draw` 方法，不会被重绘 —— 需要额外处理：

```python
def _redraw_canvases(self, root) -> None:
    """重画 CTkCanvas：CTkCanvas 无 _draw，最小化恢复后需手工重建内容。"""
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            continue
        if isinstance(w, ctk.CTkCanvas):
            redraw = getattr(w, "_on_restore_redraw", None)
            if callable(redraw):
                try:
                    redraw()
                except Exception:  # noqa: BLE001
                    pass
```

在 `hero_panel.HeroPanel` 上绑定 `self.canvas._on_restore_redraw = self._redraw_texture`（在 `_build` 里给 canvas 挂这个属性）。

`_repair_ui_after_restore` 中在 `_force_ctk_redraw(self)` 之后调用 `self._redraw_canvases(self)`。

- [ ] **Step 5: 硬编码色值改引用**

`test_busy_status.py` 与 `test_dashboard_ui_auth.py` 中的 `"#2EE6A8"` 改为 `theme.GREEN`（加 `import theme`）。

- [ ] **Step 6: 跑全量测试**

Run: `cd nodel_client_dashboard && python -m pytest -q`
Expected: 与基线一致 —— `2 failed`（基线：spawn failure / owner filter），失败名不得增加。

- [ ] **Step 7: 手工验收清单**

Run: `cd nodel_client_dashboard && python main.py`

逐项确认：
1. 登录页为平面深色卡片，主按钮金色。
2. 主窗口顶栏：左侧「亿点点 | 节点控制台」，右侧时钟每秒走动、用户名、设置、退出。
3. hero 区：节点总数正确、4 个状态 chip 计数正确、分布条比例正确、3 个指标（已连接MT5 / 已鉴权 / 异常提醒）正确。
4. 节点卡：状态点、徽章、账号、ID、三格信息、运行时长、删除与启停按钮齐备。
5. 单击卡片选中（金色边框），双击弹出详情弹窗。
6. 详情弹窗内：芯片、操作按钮、摘要、策略任务/日志区（默认收起）可见可用。
7. 工具条 7 个批量按钮功能正常。
8. 窗口宽度拖到 700 / 900 / 1400，卡片不重叠、不丢失，列数分别为 1 / 2 / 3。
9. 最小化到托盘再恢复，hero 网格纹理与状态点无残影。
10. 0 个实例时 hero 不崩、网格区有空态提示。

- [ ] **Step 8: 提交**

```bash
git add nodel_client_dashboard/node_card.py nodel_client_dashboard/app_ui.py nodel_client_dashboard/hero_panel.py nodel_client_dashboard/test_busy_status.py nodel_client_dashboard/test_dashboard_ui_auth.py nodel_client_dashboard/test_node_card.py
git commit -m "feat(dashboard): 状态点脉冲动画与手绘图元残影修复"
```

---

## 基线与验收

**基线失败（改动前已存在，与本次改造无关，不得试图修复）：**
- `test_dashboard_integration.py::test_managed_spawn_failure_is_reported_as_failed_daemon_event`
- `test_dashboard_ui_auth.py::test_owner_filter_actual_legacy_account_matching_and_full_inventory_save`

**验收标准：** `python -m pytest -q` 结束时 `2 failed` 且失败用例名与基线完全一致，新增用例全部通过；手工验收清单 10 项全部通过。

**规格书未决项（本次不做）：** hero 的「异常提醒」不可点击、不做异常节点筛选列表。
