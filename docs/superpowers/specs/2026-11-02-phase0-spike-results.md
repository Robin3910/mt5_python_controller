# Phase 0 Spike 结论：pywebview + WebView2 + PyInstaller onefile

日期：2026-11-02
状态：**通过（Go）**

## 验证矩阵

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | pywebview 在本机起窗口 | ✅ | UA = `Chrome/156.0.0.0 Edg/156.0.0.0` |
| 2 | WebView2 后端可用 | ✅ | `webview/platforms/edgechromium.py` 加载成功，JS bridge 返回 `True` |
| 3 | PyInstaller onefile 打包 | ✅ | 产物 11.5 MB，`Build complete` |
| 4 | 冻结后 exe 能起窗口 | ✅ | `frozen=True`，`js_ok=True`，UA 含 `Edg/156` |
| 5 | 冻结后 exe 绑定 .NET 运行时 | ✅ | `CLR version = 4.0.30319.42000`（**.NET Framework 4.x**） |
| 6 | 关键 DLL 已打包 | ✅ | `Microsoft.Web.WebView2.Core.dll` / `.WinForms.dll` / `WebView2Loader.dll`(win-x64) / `ClrLoader.dll` / `Python.Runtime.dll` 均在 Analysis TOC 内 |
| 7 | 换工作目录仍可运行 | ✅ | 从 `C:\` 运行 exit code 0 |
| 8 | 本机 HTTP + SSE 在 WebView2 里跑通 | ✅ | `api ok` / `sse ok ticks=3` / `static ok` |
| 9 | 冻结后 HTTP+SSE 仍跑通 | ✅ | `spike_bridge.exe` 输出同上 |

## 关键结论

### 1. 不需要在目标机器装 .NET
冻结后的 exe 走 **.NET Framework 4.x** 路径（`CLR 4.0.30319.42000`），
不是 .NET Core。.NET Framework 4.8 随 Windows 10/11 自带，
所以**目标机零 .NET 依赖**。

> 注意：`clr_loader.find_dotnet_root()` 会返回 `C:\Program Files\dotnet`，
> 但那是 netfx 加载器探测 coreclr 时留下的探测结果，**不代表实际依赖**。
> 真正生效的运行时版本以上面的 `CLR version` 为准。

### 2. 目标机需要 WebView2 Runtime
本机已装：`156.0.4314.58`
（注册表 `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-...}`）

Windows 11 22H2+ / Win10 大部分镜像已预装；Win10 LTSC 与部分国产化环境需要随包分发
**Evergreen Bootstrapper**（约 1.8 MB 引导器，运行时本体数百 MB，按需下载）。
安装器需在启动前检测 `pv` 注册表键，缺失时给出明确提示。

### 3. 前端必须走 `http://127.0.0.1:<port>`，不能用 `file://`
`file://` 下 EventSource 会被 CORS 拦掉，SSE 不可用。
所以 HTTP 服务是**必需组件**，不是可选优化。

### 4. 打包要点
`_pyinstaller_hooks_contrib` 自带 `hook-webview.py` 与 `hook-clr_loader.py`，
已覆盖 `webview/lib` 数据文件与 `ClrLoader.dll`。
spec 里额外显式声明是为了保证 `hiddenimports` 里的
`webview.platforms.edgechromium` 被静态分析到（它是动态 import 的）。

排除项：`tkinter` / `PyQt*` / `PySide*` / `matplotlib` / `numpy`，
省约 6 MB（17.4 MB → 11.5 MB）。

### 5. 已知的两个小坑
- `import clr` 成功时走 netfx；失败时才 fallback 到 coreclr。
  所以**不要**在冻结前手动设 `PYTHONNET_RUNTIME=coreclr`，
  否则会强行要求目标机装 .NET Core。
- 窗口销毁瞬间 SSE 连接会被 Windows 报 `WinError 10053 ConnectionAbortedError`。
  桥接层必须捕获 `BrokenPipeError / ConnectionResetError / ConnectionAbortedError`
  三者，否则关闭时刷一屏 traceback。

## 产出物

- `.superpowers/sdd/spike/spike_webview.py` — 最小窗口
- `.superpowers/sdd/spike/spike_bridge.py` — HTTP + SSE + WebView2 完整探针
- `.superpowers/sdd/spike/spike_runtime_kind.py` — 运行时绑定探测
- `.superpowers/sdd/spike/spike.spec` / `spike_bridge.spec` / `spike_kind.spec`

## 下一步
Phase 1：新建 `nodel_client_dashboard_web/`（复制非 UI 业务模块，不动 TK 版）
+ `frontend-console/`（Vue3 + Vite + TS）
