# WebView 版运维面板

TK 版（`nodel_client_dashboard/`）的 **WebView2 重写版**。两者完全独立并存：
TK 版一个字节都没改，随时可以回退。

```
nodel_client_dashboard/      TK 版（customtkinter）—— 原样保留，作为回退
nodel_client_dashboard_web/  WebView 版（本目录）
frontend-console/            Vue 3 + Vite + TS 前端
```

两个版本通过 `paths.data_dir()` 指向**同一份数据**（`instances.json` /
`panel_config.json` / `.env`），所以在两边切换看到的实例列表完全一致。

---

## 架构

```
┌─────────────────────────────────────────────────┐
│  nodel_client_dashboard_web/main.py              │
│                                                 │
│   ┌──────────────────┐   ┌───────────────────┐  │
│   │ pywebview 窗口    │   │ DashboardBridge   │  │
│   │ (WebView2)       │◄──┤ ThreadingHTTPServer│  │
│   │ 1440×900         │   │ 127.0.0.1:<port>   │  │
│   └──────────────────┘   └───────────────────┘  │
│         │                        │              │
│         │ HTTP + SSE             │ 共享 store   │
│         ▼                        ▼              │
│   frontend-console/dist    instances.json      │
└─────────────────────────────────────────────────┘
```

- **HTTP**：REST 风格动作接口（登录、启动/停止节点、批量操作…）
- **SSE**：`/api/events` 单向推送快照，前端不需要轮询
- **会话令牌**：桥接层启动时生成随机 token，通过 URL query 传给前端，
  所有 API 调用都要带 `?token=<tok>`；前端只与 `127.0.0.1` 通信

---

## 开发

### 1. 后端（桥接层）

```powershell
cd nodel_client_dashboard_web
..\.venv_webview\Scripts\python.exe serve_dev.py
```

输出：

```
[dev] bridge on 127.0.0.1:6110
[dev] open: http://127.0.0.1:6110/?token=<tok>
```

浏览器直接打开这个 URL 即可调试（走的是 WebView2 之外的普通浏览器，
样式与交互一致，方便用 DevTools）。

### 2. 前端

```powershell
cd frontend-console
npm install
npm run dev
```

Vite dev server 需要把 API 代理到桥接层。开发时推荐的做法是：
让 `serve_dev.py` 独占端口，前端用 `vite build` + 桥接层托管（见下），
或者在 `vite.config.ts` 里配 `server.proxy` 指向桥接层地址。

### 3. 跑起来看

```powershell
cd nodel_client_dashboard_web
..\.venv_webview\Scripts\python.exe app_web.py
```

---

## 构建产物

```powershell
cd frontend-console
npm run build          # 产物在 frontend-console/dist
```

```powershell
cd nodel_client_dashboard_web
.\build_exe.bat
```

`build_exe.bat` 会：装依赖 → 构建前端 → 打版本戳 → PyInstaller onefile 打包 →
写 `version.txt`。产物：

```
nodel_client_dashboard_web/dist/亿点点控制台 - WebView.exe   (~12 MB)
```

### spec 里的两个坑（已处理，改动时注意）

1. **PyInstaller `datas` 的第二项是目标「目录」，不是目标文件路径。**
   传文件路径会解包成 `dist/assets/index-xxx.js/index-xxx.js`，
   于是目录判断 `is_dir()` 失败，前端直接退化成占位页。
2. **必须打包 `webview/lib/*.dll`、`clr_loader` 的 `ClrLoader.dll`、
   `pythonnet` 的 `Python.Runtime.dll`**，否则冻结后 CLR 找不到
   WebView2 托管程序集，窗口直接白屏。

---

## 打包后自检

```powershell
cd nodel_client_dashboard_web\dist
.\亿点点控制台 - WebView.exe --selftest report.json
```

不开窗口，验证「前端产物可提供 + 页面能挂载 + 样式已生效」，
结果写进 `report.json`：

```json
{
  "dist_dir": "C:\\...\\Temp\\_MEIxxxx\\frontend-console\\dist",
  "dist_ready": true,
  "dom": {
    "mounted": true,
    "hasUser": true,
    "hasPass": true,
    "loginFrame": true,
    "bodyBg": "rgb(10, 14, 23)"
  },
  "ok": true
}
```

---

## 界面空白时怎么排查

打包后的 exe 是 GUI 子系统，**stdout/stderr 全部无处可去**。
用户看到的就是一个空白窗口，什么线索都没有。
所以面板从启动那一刻起就把日志写进文件：

```
<exe 同级目录>\logs\dashboard.log
```

### 方式一：命令行体检（推荐先做这个）

```powershell
cd nodel_client_dashboard_web\dist
.\亿点点控制台 - WebView.exe --diagnose
```

逐条检查白屏的每一类成因，并把结论写到
`logs\dashboard_diagnose.json`：

| 检查项 | 白屏成因 |
|---|---|
| `webview2_runtime` | 目标机没装 Edge WebView2 Runtime |
| `dotnet` | .NET Framework 缺失或低于 4.6.2，CLR 加载不了 WebView2 程序集 |
| `frontend_dist` | 前端产物没打进 exe（或没构建） |
| `webview_package` | 打包漏了 `webview/lib/*.dll`、`ClrLoader.dll`、`Python.Runtime.dll` |
| `port_binding` | 本机回环端口不可用，HTTP 桥起不来 |
| `config_files` | `instances.json` / `.env` 缺失（不影响白屏，但会让人以为坏了） |

体检全绿但界面仍空白 —— 基本就是前端 JS 崩了，
看 `dashboard.log` 里的 `[前端]` 段。

### 方式二：界面里看

顶栏 ⚙ → 系统设置 → **启动诊断** 页签，
直接显示体检结论、环境检查项与已捕获的前端脚本错误。

### 方式三：看日志

`logs\dashboard.log` 记录了启动全过程：

```
[INFO] dashboard.startup: 启动：python=3.13.14 frozen=True
[INFO] dashboard.startup:   构建版本：1.0.0-2026...
[INFO] dashboard.web: pywebview 5.x 已加载
[INFO] dashboard.web: 桥接层已就绪：http://127.0.0.1:xxxxx
[ERROR] dashboard.frontend: [前端] ERROR (http://.../index.js:12:5) | TypeError: ...
```

前端脚本错误由 `frontend-console/src/api/errors.ts` 采集
（`window.onerror` + `unhandledrejection` + Vue `errorHandler`），
通过 `/api/client-log` 回传给本机桥落盘 —— 打包窗口里按不出 F12，
不这么收集就永远看不到前端错误。

---

## 目标机器要求

| 依赖 | 说明 |
|---|---|
| Edge WebView2 Runtime | Win11 22H2+ 自带；Win10 可能需从微软官网安装 |
| .NET Framework 4.8 | Windows 自带 |
| 无需 .NET Core / Python | 冻结产物自带运行时 |

---

## 测试

```powershell
cd nodel_client_dashboard_web
..\.venv_webview\Scripts\python.exe -m pytest test_dashboard_bridge.py -q
```

覆盖：token 校验、SSE 推送、静态资源兜底、对话框接口鉴权。

视觉验收脚本（会真的开窗口并截图）：

```powershell
..\.venv_webview\Scripts\python.exe e2e_check.py     # 登录页
..\.venv_webview\Scripts\python.exe shot_console.py  # 主控制台 + 设置弹窗
```

---

## 已知取舍

- **导入节点的完整流程尚未 Web 化。** TK 版的导入对话框要处理目录选择、
  解压、停进程、替换 exe、写 `.env`、审核绑定，是一条长事务。
  Web 版目前只做了「读取/编辑 `.env`」「查看版本信息」这类只读或轻量操作。
  导入流程仍建议走 TK 版。
- **`.env` 里的后端地址不在界面上改**，跟 TK 版保持一致（读程序目录 `.env`）。
- **批量操作是「尽力而为」**：部分节点失败不影响其余，
  结果逐条返回，前端需要展示每个节点的成败。
