# 控制端（nodel_client_dashboard）UI 风格改造设计

日期：2026-11-01
状态：待用户确认
参考稿：`UI/控制端UI.html`（外加 `UI/H5.html`）

---

## 1. 目标与动机

把控制端桌面 UI 从当前的「玻璃拟态 · 左列表 + 右详情」改造成参考稿的「平面深色 · 顶部 hero + 卡片网格」风格。

动机：参考稿一眼能看全所有节点的运行态（总数、状态分布、每台节点的连接/鉴权/算法交易），而当前布局把这些信息拆散在左右两栏，运维时需要来回扫视。

范围：主窗口 + 登录页 + 全部对话框统一改造。

**非目标（明确不做）：**
- 不改任何业务逻辑、权限校验、审计链路、节点生命周期管理。
- 不改后端 API 与 `node_client` 侧。
- 不做浅色主题 / 主题切换。
- 不新增数据采集（不为了填满 hero 而新起后端请求）。

---

## 2. 现状盘点

### 2.1 现有结构

```
nodel_client_dashboard/
  theme.py            色板 + 控件工厂（glass_card / primary_btn / ...）
  app_ui.py           主窗口：_build_layout / _create_list_row / _bind_list / _bind_detail
  viewmodels.py       ListItemVM / DetailVM / ChipVM，由 process_manager 状态生成
  dashboard_auth_ui.py  登录后鉴权、归属校验、令牌操作
  login_view.py       登录页
  session_ui.py       SessionDialog 基类（带权限校验装饰）
  dialog_window.py    对话框居中 + 构建期遮挡
  import_dialog.py / env_dialog.py / version_dialog.py /
  version_picker.py / connection_dialog.py
```

主窗口现状：
- 顶栏：品牌「亿点点」+ 副标题、版本更新按钮、退出登录、连接配置、三个安全 chip。
- 左侧 340px 固定列表卡：每项一张卡（标题 + 状态点 + 账号 + 路径 + 状态行 + 编辑/移除按钮）。
- 右侧：标题卡（chips）、操作卡（启动/停止/节点配置/刷新健康/打开目录/守护开关）、摘要卡（等宽文本）、被 `if False` 隐藏的策略任务 + 日志区。

### 2.2 参考稿结构

```
header (54px, 扁平面板 + 下边框)
  左：logo + 站名 + 竖分隔线 + 模块名
  右：等宽时钟 + 头像用户 + 设置图标按钮 + 退出登录 ghost 按钮
main (滚动)
  stat-hero：节点总数(大数字) + 状态 chips 组 + 状态分布条 + 3 个指标格
  section-head：「MT5 节点」+ 右侧操作按钮组
  node-grid：3 列卡片
    卡片：状态点 + 节点名 + 状态徽章
          服务器名 + MT5 ID
          3 格信息（连接MT5 / 是否鉴权 / 算法交易）
          运行时长 + 删除图标 + 启停按钮
```

### 2.3 数据可得性对照

| 参考稿元素 | 我们的数据源 | 结论 |
|---|---|---|
| 节点总数 | `manager.configs()` 数量 | 可得 |
| 在线 / 启动中 / 已停止 | `runtime.process_alive` / `runtime.busy` | 可得 |
| 已暂停 | 无此状态（只有 `daemon` 守护语义） | **改为「待审核」**（`approval_status == "pending"`） |
| 状态分布条 | 同上计数 | 可得 |
| 服务器名 `ICMarkets-Live` | 无券商字段 | **改为「MT5 账号 + 节点ID」** |
| MT5 ID | `runtime.node_id` / `runtime.mt5_login` | 可得 |
| 连接MT5 | `runtime.ws_state` | 可得 |
| 是否鉴权 | `runtime.ws_state == "authenticated"` | 可得 |
| 算法交易 | `runtime.algo_trading_allowed` | 可得 |
| 运行时长 | `runtime.uptime_s` | 可得 |
| 今日信号 / 平均延迟 / 异常提醒 | 无信号/延迟数据 | **改为：已连接MT5 / 已鉴权 / 异常提醒**（异常提醒 = `last_error` 非空计数） |

### 2.4 参考稿用不上的功能（必须重新安置）

当前主窗口有、参考稿没有：节点配置(env)、刷新健康、打开目录、守护开关、编辑实例、移除实例、绑定后台节点、重置专属令牌、手工添加、手工替换全部、版本更新、导入节点、连接配置、策略任务区、运行日志区。

安置方案见 §4。

---

## 3. 视觉规范（design tokens）

以参考稿实测 CSS 变量为基准（`UI/_mock.css` 的 `:root` 第二段）。

### 3.1 色板

| 语义 | 值 | 用途 |
|---|---|---|
| `BG` | `#0a0e17` | 窗口底色 |
| `PANEL` | `#101826` | 卡片 / 顶栏底色 |
| `PANEL2` | `#151f31` | 卡片内子块、按钮底 |
| `LINE` | `#1e2a3d` | 边框 |
| `LINE_SOFT` | `rgba(148,163,184,.14)` | 内部细分隔 |
| `TEXT` | `#e8ecf4` | 主文字 |
| `DIM` | `#8b96ab` | 次要文字 |
| `MUTE` | `#5a6a82` | 弱化文字、离线态 |
| `GOLD` | `#d9b36c` | **主强调**：主按钮、选中态边框、hero 图标 |
| `GOLD_STRONG` | `#e9c98a` | 金色 hover |
| `GREEN` | `#2fd381` | **健康语义**：在线、已连接、已鉴权、已开启 |
| `AMBER` | `#f2b84b` | 启动中、警告、待审核 |
| `RED` | `#f2645f` | 停止、错误、危险操作 |
| `BLUE` | `#58a6ff` | 次级指标 |
| `VIOLET` | `#a78bfa` | 次级指标 |
| `ON_GOLD` | `#1b1408` | 金色按钮上的文字 |

**用户已决定：金色做主强调（按钮、选中态）；青绿保留为「健康」语义色，不与主按钮竞争。**

现有 `theme.py` 里 `#04140F`（按钮文字）→ 改为 `ON_GOLD`。

### 3.2 形状与排版

| 元素 | 规格 |
|---|---|
| 卡片圆角 | 10px（hero 14px，chip/badge 4~5px，按钮 6px） |
| 按钮 | 高 28px（次级）/ 34px（主操作），字号 12 |
| 字体 | `Segoe UI` / `Segoe UI Semibold`（保持现状），等宽 `Consolas` |
| 阴影 | 参考稿 `0 12px 32px #00000059`；CTk 不支持投影，靠底色层级表达 |
| 背景纹理 | 参考稿 hero 有 32px 网格 + 径向渐变遮罩。CTk 无原生支持 → 用 `CTkCanvas` 手绘（见 §5.3） |

### 3.3 theme.py 改造

`theme.py` 保留全部现有导出函数签名（`glass_card` / `soft_card` / `primary_btn` / `ghost_btn` / `danger_btn` / `status_chip` / `font` / `mono` / `health_color`），只改内部取值与新增几个：

- `glass_card` → 改名为语义更准的 `panel_card`？**不改名**，避免 11 个文件大改。保留名字，实现改为 `PANEL` 底 + `LINE` 边 + 圆角 10。
- 新增 `hero_card()`、`node_card()`、`badge()`、`info_cell()`、`metric_cell()`、`toolbar_btn()`（ghost 小按钮）。
- `primary_btn` 改为金色底 + 深色字；`danger_btn` 改为红色描边 + 浅红底（参考稿 `btn-icon.danger` 风格），主红色实底只保留给顶栏「全部停止」。

---

## 4. 布局设计（方案 B：网格 + 详情弹窗）

### 4.1 整体

```
┌──────────────────────────────────────────────────────────┐
│ header (54px): logo 亿点点 | 节点控制台      时钟 用户 设置 退出 │
├──────────────────────────────────────────────────────────┤
│ main (CTkScrollableFrame)                                │
│  ┌────────────────────────────────────────────────────┐  │
│  │ hero: [5] 节点总数   ●在线3 ●启动中1 ●已停止1 …    │  │
│  │        ▓▓▓▓▓▓░░░░░░░░  状态分布条                 │  │
│  │        ──────────────────────────────────────      │  │
│  │        [已连接MT5 3] [已鉴权 2] [异常提醒 1]         │  │
│  └────────────────────────────────────────────────────┘  │
│  MT5 节点                          [全部启动][全部停止][导入节点][手工添加] │
│                                          [全部开守护][全部关守护][手工替换全部] │
│  ┌────────┐ ┌────────┐ ┌────────┐                        │
│  │ 节点卡  │ │ 节点卡  │ │ 节点卡  │  ← 3 列网格            │
│  └────────┘ └────────┘ └────────┘                        │
└──────────────────────────────────────────────────────────┘
```

### 4.2 顶栏

- 左侧：`logo`（现有托盘图标可复用） + 「亿点点」 + 竖分隔线 + 「节点控制台」。
- 副标题里的安全声明（「仅本机回环 · 禁止公网绑定 · 支持 TLS/WS」）不再做 chip，改为 hero 底部一行 DIM 小字 + 顶栏右侧一行 DIM 小字，避免顶栏过载。
- 右侧：等宽时钟（每秒刷新） + 头像 + 用户名 + 设置图标按钮（打开连接配置） + 「退出登录」ghost 按钮。
- 「版本更新」按钮保留在右侧，但当前实现是普通 ghost 按钮；改为：无更新时 ghost，有新版时琥珀描边 + 文案「有新版 vX（N）」，并放到退出按钮左侧。

### 4.3 hero 统计卡

结构对齐参考稿 `.stat-hero`：
- 左：大数字「节点总数」+ 金色 server 图标。
- 右上：状态 chips 组（在线 / 启动中 / 已停止 / 待审核），每 chip 带 7px 圆点。
- 中：状态分布条（按在线/启动中/已停止/待审核占比分段着色）。
- 下：3 个指标格（图标 + 数值 + 标签），上边框分隔。

指标映射（§2.3 已定）：已连接 MT5、已鉴权、异常提醒。

### 4.4 节点卡片

卡片内容严格对齐参考稿：

```
● MT5-ALPHA                                    [运行中]
🖥 账号 88001234            ⛿ ID TID-88001234
┌──────────┐ ┌──────────┐ ┌──────────┐
│ ✓ 连接MT5 │ │ ✓ 是否鉴权 │ │ ✓ 算法交易 │
│  已连接   │ │  已鉴权   │ │  已开启   │
└──────────┘ └──────────┘ └──────────┘
⏱ 3天 14小时                          [🗑] [暂停]
```

- 状态点：8px 圆点，在线绿带辉光，启动中琥珀脉冲（`BusyStatus` 已有动画能力，需复用）。
- 状态徽章：`运行中 / 启动中 / 已停止 / 待审核`，圆角 4，12% 底色 + 30% 边框。
- 信息格三选一：每格带 ✓ / ✗ 图标 + 标签 + 值。
- 底部：运行时长（左）+ 删除图标按钮（红描边）+ 启停按钮（运行中为琥珀「暂停」→ 映射到我们的「停止」；已停止为绿色「启动」）。

**卡片上不做的事**（避免拥挤）：编辑、绑定、令牌、守护开关、打开目录一律不放在卡片上。

### 4.5 交互

- **单击卡片**：选中（`selected` 态：金色边框 + 轻微提亮），与当前 `_selected_id` 语义一致，驱动详情弹窗内容与右侧……（无右侧）。
- **双击卡片**：打开详情弹窗 `NodeDetailDialog`。
- **卡片右键**：弹出上下文菜单（编辑 / 绑定后台节点 / 重置专属令牌 / 打开目录 / 刷新健康 / 移除）。CTk 无原生菜单 → 用 `tk.Menu` 弹出（`tkinter.Menu` 可用）。**决策点：右键菜单 vs 全部塞进详情弹窗。**

  我倾向：**详情弹窗承载全部操作，右键菜单只保留高频三项（打开目录、刷新健康、移除）**。理由：避免两处入口语义重复。

- 底部工具条按钮全部走批量操作，行为与现状一致。

### 4.6 详情弹窗 `NodeDetailDialog`

复用 `session_ui.SessionDialog` + `dialog_window.show_centered`，宽度 720、高度 560，内容分区：

1. 头部：节点名 + 状态徽章 + 账号/ID/版本（沿用 `ChipVM` 系列）。
2. 操作行：启动 / 停止 / 节点配置 / 刷新健康 / 打开目录 / 守护开关（`CTkSwitch`）。
3. 摘要区：现 `DetailVM.summary` 的等宽文本块。
4. 策略任务 + 运行日志：把当前 `if False` 隐藏的代码块迁到这里（保持默认收起状态，行为不变）。
5. 底部：编辑实例 / 绑定后台节点 / 重置专属令牌 / 移除实例 / 关闭。

弹窗内的权限校验沿用 `session.perform(...)`，弹窗打开时对实例做 `view_status` 审计。

### 4.7 响应式网格

列数按窗口宽度切换：≥1180px → 3 列；≥820px → 2 列；否则 1 列。在 `<Configure>` 上重排（已有卡片直接 `grid` 重定位，不重建）。

---

## 5. 实现要点

### 5.1 viewmodels.py 扩展

新增：

```python
@dataclass(frozen=True)
class HeroVM:
    total: int
    online: int
    starting: int
    stopped: int
    pending: int          # 待审核
    connected_mt5: int
    authenticated: int
    alerts: int           # last_error 非空
```

`build_hero(manager) -> HeroVM`。
`ListItemVM` 扩展字段：`node_id`、`server_label`、`uptime_s`、`mt5_connected`、`mt5_authenticated`、`algo_trading`、`badge_text`、`badge_tone`、`status_dot_color`。
`ListItemVM` 目前被 `test_viewmodels.py` 覆盖 → 改字段必须同步改测试。

### 5.2 app_ui.py 改造

- `_build_layout`：改为 顶栏 + 滚动主体（hero + section head + 工具条 + grid）。
- `_create_list_row` → `_create_node_card`：从「列表卡」改成「网格卡」，差分更新逻辑 `_apply_list_row` 保留。
- `_bind_list`：按 grid 布局重排，宽度变化时调整列数。
- `_bind_detail`：不再渲染右侧面板，改为维护当前选中节点的 `DetailVM`，由弹窗订阅刷新。
- 新增 `_open_detail_dialog()`。
- `_tick_ui` / `_safe_refresh` 保持不变（差分刷新机制不变，这是现有代码的强项，务必保留）。

### 5.3 手绘图元（CTkCanvas）

需要手绘的只有两处，其余用 CTk 原生控件：
- hero 背景网格纹理（32px 网格 + 径向遮罩）→ `CTkCanvas` 叠在 hero 底部，`create_line` 画网格线。
- 状态点辉光 / 脉冲 → `CTkCanvas` 画圆 + 动画（复用 `busy_status.BusyStatus` 的定时器思路）。

**风险**：CTkCanvas 在 Windows 最小化恢复后有残影问题（现有代码已有 `_repair_ui_after_restore` 处理）。手绘图元需纳入该修复流程，否则恢复后 hero 背景会留残影。

### 5.4 登录页与对话框

统一改造范围：
- `login_view.py`：卡片改 `PANEL` 底 + 圆角 10 + `LINE` 边；标签 DIM；主按钮金色；副标题「使用后台账户，按本人节点权限进行管理」。
- `import_dialog.py` / `env_dialog.py` / `version_dialog.py` / `version_picker.py` / `connection_dialog.py` / `app_ui.py` 内的 `EditInstanceDialog`：把 `glass_card` / `primary_btn` 等换成新 theme 实现（因为函数签名不变，这部分几乎零改动，只需改 theme.py 就自动生效）。
- `messagebox` / `simpledialog` 是 Tk 原生弹窗，样式无法统一 —— **接受不一致**（不做自绘弹窗，避免风险）。

### 5.5 其他需要跟着改的地方

- `tray_icon.py` 的图标填充色 `#2EE6A8` → 保留或改金色，需与图标资源一致（决策：保留绿色托盘图标不改，减少无谓改动）。
- `app_ui.py` 里的 `#04140F` → `ON_GOLD`。
- `test_busy_status.py` / `test_dashboard_ui_auth.py` 里硬编码的 `#2EE6A8` → 改成引用 theme 常量。

---

## 6. 实施顺序

1. **theme.py 全面改版**（色板 + 控件工厂 + 新增 widget 工厂）。此步单独可验收，视觉立刻变。
2. **viewmodels.py** 补 `HeroVM` 与 `ListItemVM` 字段 + `build_hero`，同步改测试。
3. **主窗口重排**：顶栏 → hero → section head + 工具条 → 3 列网格；`_create_node_card` / `_apply_list_row` 改造。
4. **详情弹窗** `NodeDetailDialog`，承接原右侧详情全部内容与操作。
5. **手绘图元**（hero 网格、状态点动画）。
6. **登录页 + 各对话框**统一。
7. **残影修复与回归**：最小化/恢复、托盘、多实例、批量操作。

每步完成后都可运行 `nodel_client_dashboard` 下的 pytest 验收。

---

## 7. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 网格 + 滚动容器下 CTk 差分更新错位 | 卡片重叠 / 空白 | 复用现有差分机制，仅在 `_bind_list` 里重排；列数变化只调 `grid` 参数不重建控件 |
| CTkCanvas 手绘残影 | 最小化恢复后 hero/状态点花屏 | 纳入 `_repair_ui_after_restore` 的 `_force_ctk_redraw` 遍历范围 |
| 卡片信息密度超载 | 3 列 340px 下文字换行 | 卡内固定高度（信息格三列等宽），长路径走 `shorten_path` 截断 + tooltip |
| 全部操作移入弹窗 | 高频操作多一次点击 | 卡片上保留 启动/停止 + 打开目录三键；其余进弹窗 |
| 11 个文件引用 theme 工厂函数 | 改 theme 波及面大 | 保持函数签名与导出名不变，只改实现 |
| `ListItemVM` 字段扩展 | `test_viewmodels.py` 失败 | 同批改测试 |

---

## 8. 留给实现者的决定（已由我拍板）

1. 参考稿「已暂停」→ 用「待审核」代替（我们没有暂停态）。
2. 参考稿「服务器名」→ 用「MT5 账号 + 节点ID」代替（没有券商字段，不虚构）。
3. hero 三个指标 → 已连接MT5 / 已鉴权 / 异常提醒。
4. 右键菜单只放 打开目录 / 刷新健康 / 移除；其余操作统一在详情弹窗。
5. 卡片上保留 启动/停止 + 删除；删除需二次确认（沿用现状 `messagebox.askyesno`）。
6. Tk 原生 messagebox 样式不统一，接受。
7. 托盘图标颜色不变。

## 9. 未决问题

- 用户是否希望 hero 的「异常提醒」可点击跳转到异常节点列表？（当前无此列表，功能上要新增，本次不做。）
