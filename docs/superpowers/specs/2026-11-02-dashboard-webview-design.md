# 控制端 Dashboard 迁移到 WebView 的方案设计

**状态**：待决策（尚未写任何实现代码）
**日期**：2026-11-02
**背景**：当前 `nodel_client_dashboard` 用 Python + customtkinter 实现 UI，虽然已经按参考稿 `UI/控制端UI.html` 做了样式对齐，但无法做到「和 HTML 一模一样」。用户提出改用 WebView 方案。

---

## 1. 结论先行

**建议走 WebView 方案，但要用「独立 Vue 应用 + 本机 HTTP 桥」的方式，而不是「把现有前端整个塞进 pywebview」。**

核心判断依据是三条实测事实：

1. **业务逻辑已经是 Tk-free 的**——`viewmodels.py` / `dashboard_session.py` / `auth_service.py` / `process_manager.py` 等 17 个核心模块**全部不 import tkinter**，UI 只是它们之上的一层壳。这是最关键的前提：换壳不换骨。
2. **参考稿的 CSS 可以原样搬**——`ref_app.css` 里的 `.login-root` / `.stat-hero` / `.node-card` 等组件规则全部位于调色板 `:root` 之后，是自包含的一段，可以整段复制进新项目，不需要反推。
3. **pywebview 的 WebView2 后端有真实依赖**——它依赖 `pythonnet` + `.NET Framework 4.6.2+`（本机有 4.8.4772，满足）+ WebView2 Runtime（本机 156.0.4314.58，已装）。可行，但会把 .NET 运行时拖进 PyInstaller 打包链路。

---

## 2. 现状盘点

### 2.1 Tk-free 边界（这是最重要的发现）

以下 17 个模块**不依赖 Tk**，可以直接被 WebView 后端复用：

| 模块 | 行数 | 职责 |
|---|---|---|
| `viewmodels.py` | 315 | `ListItemVM` / `HeroVM` / `DetailVM` 快照生成 |
| `dashboard_session.py` | 381 | 会话、审计票据、权限、锁 |
| `auth_service.py` | 130 | 后端 HTTP API 封装 |
| `process_manager.py` | 1059 | 节点进程生命周期 |
| `client_deploy.py` | 582 | 客户端部署/版本读取 |
| `version_service.py` | 248 | 版本查询/下载 |
| `env_file.py` | 275 | node_client `.env` 读写 |
| `store.py` | 199 | instances.json / panel_config.json 持久化 |
| `models.py` | 124 | `InstanceConfig` |
| `terminal_account.py` | 108 | 读取 MT5 终端账号 |
| `node_log.py` | 100 | 日志读取 |
| `recovery.py` | 201 | 看门狗自恢复 |
| `single_instance.py` | 142 | 单实例互斥 |
| `env_policy.py` / `client_binding.py` / `admin_elevate.py` | 166 | 策略/绑定/提权 |

**Tk 相关的只有 14 个文件**，其中真正承担视觉的是：`theme.py`(554)、`app_ui.py`(1644)、`login_view.py`(430)、`node_card.py`(410)、`hero_panel.py`(260)、`node_detail_dialog.py`(272)、`canvas_fx.py`(178)。

### 2.2 参考稿 CSS 可复用性（已实测）

`.superpowers/sdd/ref_app.css` 共 105394 字符，结构是：

```
[0      – 4765 )  Tailwind preflight + 工具类 + 亮色 :root
[4765   – 37293)  工具类续
[37293  – 64216)  安全区变量 :root
[64216  – 105394) ← 应用自己的暗色调色板 :root + 全部组件样式
```

实测所有目标组件规则都落在最后一段：

| 选择器 | 位置 | 是否在调色板之后 |
|---|---|---|
| `.login-root` | 65008 | 是 |
| `.login-card` | 66062 | 是 |
| `.console-header` | 75183 | 是 |
| `.badge` | 77661 | 是 |
| `.section-head` | 78319 | 是 |
| `.stat-hero` | 78601 | 是 |
| `.stat-chip` | 80473 | 是 |
| `.node-card` | 83490 | 是 |

暗色调色板（可直接搬）：

```css
:root{
  --bg:#0a0e17; --panel:#101826; --panel-2:#151f31; --line:#1e2a3d;
  --line-soft:rgba(148,163,184,.14); --text:#e8ecf4; --dim:#8b96ab; --mute:#5a6a82;
  --gold:#d9b36c; --gold-strong:#e9c98a; --green:#2fd381; --red:#f2645f;
  --amber:#f2b84b; --blue:#58a6ff; --violet:#a78bfa;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace
}
```

**结论**：从 offset 64216 截取到文件末尾，就是一份自包含、可直接使用的暗色样式表。不需要反推任何设计。

### 2.3 现有前端项目的关系

`frontend/` 是**另一个产品**（后台管理 Web 端，Vue 3 + Element Plus + ECharts），有 23 个视图、自己的登录页和路由。它面向的是后端 Web 用户，不是本机运维控制台。

- 它的 `LoginView.vue` 是账号密码 + 2FA 两步式，语义上和面板登录一致，但视觉完全不同。
- 它的 `styles.css` 调色板是**亮色玻璃拟态**（`--bg:#060a12`、`--panel:rgba(14,22,40,.72)`、`--gold:#fbbf24`），和参考稿的**平面暗色金色**是两套东西。
- 它没有 `.console-root` / `.stat-hero` / `.node-grid` 任何一个类。

**所以不能直接复用 `frontend/`。** 但可以复用它的**工程配置**（vite、TypeScript、构建脚本）和**登录交互范式**。

---

## 3. 三个候选方案

### 方案 A：pywebview + 独立 Vue 控制台应用（推荐）

**形态**

```
┌─────────────────────────────────────────────────┐
│ nodel_client_dashboard.exe (Python 主进程)        │
│   ├─ DashboardSession / ProcessManager / ...      │  ← 现有 Tk-free 逻辑，不动
│   ├─ 本机 HTTP 桥 (127.0.0.1:<随机端口>)          │  ← 新增
│   │    GET  /api/state      → 快照 JSON            │
│   │    POST /api/action/:x  → 执行操作             │
│   │    GET  /api/stream     → SSE 推送增量          │
│   └─ pywebview 窗口 (WebView2)                     │
│        └─ http://127.0.0.1:<port>/  →  Vue 控制台   │
└─────────────────────────────────────────────────┘
```

**新增项目**：`frontend-console/`（Vue 3 + Vite + TS，无 UI 框架依赖）

```
frontend-console/
  src/
    views/LoginView.vue      # 照抄参考稿 .login-* 结构
    views/ConsoleView.vue     # 照抄参考稿 .console-* 结构
    api/client.ts             # fetch 封装，走本机桥
    stores/console.ts         # Pinia，存快照
    styles/console.css        # 从 ref_app.css 64216 截取
```

**权衡**

| 优点 | 缺点 |
|---|---|
| 视觉可以做到和 HTML **像素级一致**（同 CSS） | 引入 pywebview + pythonnet + .NET 依赖 |
| 业务逻辑一行不用改 | 需要重写全部 UI 层（约 2000 行 Tk → 约 1500 行 Vue） |
| 前端热更新，开发迭代快 | 打包体积增加（WebView2 DLL + .NET 依赖） |
| 动画/过渡/响应式全部原生支持 | 多一个本机 HTTP 端口（安全面需处理） |
| 现有 4 个对话框可以改成 Web 路由 | Tk 的 `messagebox` / `filedialog` 需换成 Web 方案 |

**关于 pywebview 的依赖细节**（已实测）：

- `pywebview 6.2.1`，Windows 上默认走 `edgechromium` 后端。
- 该后端 `import clr`，且会设 `PYTHONNET_RUNTIME=coreclr`（注意：这要求 .NET Core，而 `clr_loader` 的 `default` 在 Windows 上是 `netfx`）。本机装了 .NET Core 6/8/9 三个 runtime，OK。
- `Microsoft.Web.WebView2.WinForms.dll` 的目标框架是 **.NET Framework 4.6.2+**，本机 4.8.4772 满足。
- WebView2 Runtime 已装（156.0.4314.58）。
- pythonnet wheel 只有 0.7 MB，`clr_loader` 提供 `netfx`/`coreclr` 两种后端。

---

### 方案 B：pywebview + 直接嵌入现有 `frontend/`

**形态**：把 `frontend/` 构建产物塞进 pywebview，复用 Vue 路由、Pinia、axios。

**为什么不推荐**

1. **产品语义不同**。`frontend/` 是给后端 Web 管理员用的多页面系统（23 个视图、菜单权限、WS 实时推送、TradingView 图表）。本机控制台只需要**一屏**：顶栏 + hero + 节点网格。硬塞进来等于把整个后台管理端拖进一个本机窗口。
2. **视觉冲突**。`frontend/styles.css` 的亮色玻璃拟态和参考稿的平面暗色金色是两套设计体系，要改就得改 56 KB 的全局样式 + 23 个视图。而参考稿的 CSS 是自包含的一段，直接用反而更省事。
3. **依赖膨胀**。Element Plus + ECharts + axios 全都进包，控制台根本用不到。
4. **构建耦合**。`frontend/` 的 dev proxy 指向 `localhost:8000`（后端），和本机桥是两套服务，容易混。

**唯一优点**：不用新建前端工程。但代价是把「控制端 UI 改版」变成「改整个后台前端」。

---

### 方案 C：保留 Tk，把参考稿改写成 Tk 能表达的样子（现状延续）

**形态**：继续用 `theme.py` + `canvas_fx.py` 手绘逼近。

**为什么不推荐**

用户已经明确说过「改版后的 UI 我不满意，有办法做得像 html 那样一模一样的吗？」——这就是对方案 C 的否定。根因是：

- CSS 的 `linear-gradient`、`mask-image`、`box-shadow`、`flex` 布局在 Tk 里没有对应物，只能用 Canvas 手绘逼近，**永远做不到一模一样**。
- 这次登录页输入框消失的 bug（`pack_propagate(False)` 锁死高度）就是 Tk 手写布局的固有风险——每加一个元素都要重新验证一遍几何。

---

## 4. 推荐方案的关键设计决策

### 4.1 桥接协议：HTTP + SSE，而不是纯 `js_api`

pywebview 提供 `js_api`（Python 对象直接注入 JS）。**不推荐**作为主通道：

- `js_api` 调用是同步阻塞的，Python 侧耗时操作会卡住 UI 线程。
- 双向推送（节点状态每 2 秒刷新）用 `js_api` 需要轮询，反而更绕。
- `js_api` 需要 WebView2 的额外权限配置。

**采用**：

| 用途 | 机制 | 说明 |
|---|---|---|
| 读快照 | `GET /api/state` | 返回 `HeroVM` + `ListItemVM[]` + 会话信息 |
| 执行操作 | `POST /api/action/<name>` | 复刻现有 `session.perform(action, cfg, fn)` 的审计语义 |
| 增量推送 | `GET /api/stream` (SSE) | 节点状态变化时推送 `{type:'patch', ...}` |
| 文件选择 | `POST /api/pick-file` | 替代 Tk `filedialog`（走 `webview.create_file_dialog`，pywebview 原生支持） |

**SSE 而非 WebSocket 的理由**：单向推送足够，且 SSE 在 HTTP/1.1 下无需额外协议栈，Python 侧可以用标准库实现（`http.server` + 手写 SSE），不必引入 `flask-sock`/`uvicorn`。

### 4.2 安全边界

本机桥是**敏感面**，必须收紧：

- 只监听 `127.0.0.1`，端口从 `ports.find_free_port()` 取。
- 每个请求校验一个**启动时随机生成的会话令牌**（放在 URL query 或自定义 header）。没有令牌的一律 403。
- WebView 加载的页面用 `http://127.0.0.1:<port>/?t=<token>`，令牌由 Python 侧注入。
- 禁用 `ALLOW_DOWNLOADS`、不开远程调试端口。
- **不允许绑定公网**——这与现有 `node_client/local_status.py` 的纪律一致。

### 4.3 打包影响

`dashboard.spec` 需要新增：

```python
datas += [(str(Path("frontend-console/dist").resolve()), "frontend-console/dist")]
hiddenimports += ["clr", "clr_loader", "webview", "webview.platforms.edgechromium"]
```

并且要 `collect_all("webview")` 拿 `Microsoft.Web.WebView2.*.dll` 和 `WebView2Loader.dll`。

**风险点**：`pythonnet` 的 `clr_loader` 在 PyInstaller onefile 下需要能找到 `hostfxr`/`coreclr`。这条链路**必须在方案 A 落地时先做一个最小 spike 验证**，不要等写完 UI 才发现打包不了。

### 4.4 Tk 的去留

**建议：保留 Tk 作为降级路径**，而不是一步切干净。

理由：
- `tray_icon.py`（pystray）已经是 Tk-free 的，WebView2 窗口也能有托盘。
- 但 `filedialog` / `messagebox` / `simpledialog` 这些 Tk 原生弹窗在 WebView 里要重做。
- 保留 Tk 意味着**可以按页面灰度切换**：先做登录页（WebView），验证打包和通信链路，再逐步迁移主控制台。

---

## 5. 迁移路径（分阶段，每阶段可独立验收）

### 阶段 0：Spike（先验证最大的不确定性）

**目标**：证明 `pywebview + WebView2 + PyInstaller onefile` 这条路在本机能跑通。

内容：
- 最小 pywebview 窗口，加载一个静态 HTML。
- 用 `build_exe.bat` 打成 onefile exe，在干净环境跑起来。
- 验证：窗口显示、WebView2 加载成功、exe 体积可接受。

**验收标准**：打包后的 exe 能独立启动并显示页面。**这一步不通过，整个方案 A 就要重新评估。**

### 阶段 1：桥接骨架 + 登录页

- 抽 `dashboard_bridge.py`：把 `DashboardSession` / `ProcessManager` 的只读快照暴露成 HTTP JSON。
- 新建 `frontend-console/`（Vue 3 + Vite + TS）。
- 移植登录页：直接用参考稿 `.login-*` 的 HTML + CSS。
- 通信：`GET /api/state`、`POST /api/login`。

**验收**：登录页视觉与 `UI/控制端UI.html` 像素级一致；能真实登录并拿到 token。

### 阶段 2：主控制台

- 移植 `.console-header` / `.stat-hero` / `.section-head` / `.node-grid` / `.node-card`。
- SSE 推送节点状态。
- 卡片操作（启停/删除/详情）走 `POST /api/action/*`，**必须复用现有 `session.perform()` 审计语义**。

**验收**：hero 数字与 `build_hero()` 一致；卡片状态与 `build_list()` 一致；启停操作有审计票据。

### 阶段 3：四个对话框的 Web 化

现状：`import_dialog.py`(579)、`version_dialog.py`(592)、`env_dialog.py`(285)、`connection_dialog.py`(84)。

改成 Vue 组件 + 桥接口。文件选择用 `webview.create_file_dialog`。

### 阶段 4：切换与清理

- 打包配置切换到 WebView 主路径。
- Tk 版保留在 `legacy/` 或加 `--tk-ui` 开关，用于应急回退。
- 稳定后删除 Tk UI 层。

---

## 6. 风险清单

| 风险 | 等级 | 说明 | 缓解 |
|---|---|---|---|
| **PyInstaller 打包 .NET 依赖失败** | 高 | `clr_loader` 需要定位 `hostfxr`/`coreclr`，onefile 环境下路径易错 | **阶段 0 spike 先验证**，这是 go/no-go |
| **本机 HTTP 桥成为攻击面** | 高 | 端口暴露给本机任意进程 | 随机端口 + 每次启动随机令牌 + 只监听 127.0.0.1 |
| **重写 2000 行 Tk UI 的返工** | 中 | 视觉细节可能反复调 | 分阶段，每阶段独立验收；参考稿 CSS 直接搬，不要反推 |
| **对话框重做成本被低估** | 中 | 4 个对话框 + 文件选择 + 确认框 | 阶段 3 单独排期 |
| **`session.perform()` 审计语义在 Web 侧走样** | 中 | 漏掉审计票据 = 权限绕过 | 桥接口**只能**转发到 `perform()`，不允许直接调 `ProcessManager` |
| **打包体积增长** | 低 | +WebView2 DLL + .NET 依赖 | 实测后再评估；必要时换 `--onedir` |
| **Tk 依赖无法完全移除** | 低 | `tray_icon` 已是 Tk-free，但 `pystray` 用 `win32` API，无妨 | — |

---

## 7. 需要你拍板的问题

1. **要不要先做阶段 0 的 spike？** 我建议做。这是整个方案里唯一有「做不成」风险的一步（打包 .NET 依赖），花半天时间能换掉最大的不确定性。

2. **本机桥用 HTTP 还是 `js_api`？** 我建议 HTTP + SSE（见 4.1），代价是多一个端口，收益是双向通信清晰、耗时操作不卡 UI。

3. **前端建独立工程 `frontend-console/`，还是并进现有 `frontend/`？** 我建议独立工程（见 3.2 的四条理由），但如果你希望维护单一前端仓库，也可以改成 `frontend/src/console/` 子目录 + 独立 vite 入口。

4. **Tk 版是保留降级还是直接删？** 我建议保留到阶段 4 之后再删。

5. **要不要改现有的 `frontend/`？** 我的方案完全不动它。但如果你本来就打算统一两端视觉，那是另一个更大的项目，需要单独讨论。

---

## 附：实测事实速查

| 事实 | 值 | 来源 |
|---|---|---|
| WebView2 Runtime | 156.0.4314.58 已安装 | `HKLM\...\EdgeUpdate\Clients` |
| .NET Framework | 4.8.4772.0（需 ≥4.6.2） | `clr.dll` FileVersion |
| .NET Core runtime | 6.0.36 / 8.0.16 / 9.0.5 | `dotnet --list-runtimes` |
| pywebview 最新版 | 6.2.1 | PyPI |
| pythonnet | 3.2.1，cp313 有 wheel | PyPI |
| pythonnet 体积 | 0.7 MB（`Python.Runtime.dll` 0.45 MB） | wheel 解包 |
| Node / npm | v22.17.0 / 10.9.2 | `node --version` |
| `frontend/dist` 体积 | 78 文件 / 1.9 MB | `Measure-Object` |
| 参考稿 CSS 组件段 | offset 64216 → 105394 | `ref_app.css` |
| Tk-free 核心模块 | 17 个 / 约 4600 行 | 逐文件 grep |
| Tk UI 层代码量 | 约 2000 行（6 个文件） | 行数统计 |
