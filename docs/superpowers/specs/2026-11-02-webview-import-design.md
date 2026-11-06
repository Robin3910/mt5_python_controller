# WebView 版导入节点：设计方案

> 状态：已实现（2026-11-02）
> 目标：把 TK 版 `import_dialog.py` 的导入能力搬到 Web 版，
> 底层复用同一套业务逻辑，前端只负责收集参数 + 展示进度。

---

## 1. 为什么要单独抽一层

TK 版的导入逻辑写在 `import_dialog.py` 里，UI 与编排**混在一起**：
`self.after(0, ...)` 更新状态条、`messagebox` 弹确认框、
`self.session_check()` 校验会话。Web 版没法直接复用。

因此做法是：**把编排逻辑抽成 UI 无关的 `import_service.py`**，
TK 版与 Web 版都调它。

```
nodel_client_dashboard/import_dialog.py   ← TK 调用方（UI 无关部分已下沉）
nodel_client_dashboard_web/import_service.py  ← 编排层（新增）
nodel_client_dashboard_web/import_dialog.py   ← Web 调用方（新增，仅做参数收集）
```

`import_service.py` 接收「一个进度回调 + 几个参数」，返回结构化结果，
**不碰任何 UI 组件**。

---

## 2. 关键风险与实测结论

### 2.1 原生目录选择器会挂死（已实测）

pywebview 的 `window.create_file_dialog` 在 winforms 后端里**没有**做
`Invoke` 转发（见 `webview/platforms/winforms.py:866`）。
从后台线程直接调用 → **弹窗不出现，线程永久阻塞**。

实测：

| 调用方式 | 结果 |
|---|---|
| 后台线程直接调用 | 挂死，30 秒无弹窗 |
| `window.native.Invoke(Action(fn))` | **正常弹窗**，选中后正确回传 |

所以原生对话框必须 marshal 到 GUI 线程：

```python
native = window.native          # WinForms BrowserForm 实例
native.Invoke(Action(do_pick))  # 投递到 GUI 线程消息循环
```

这带来一个设计约束：**原生对话框只能在 GUI 线程弹出**，
而桥接层的 HTTP 处理线程是后台线程。所以：
- 前端点「浏览…」→ HTTP 请求 → 桥接层把这个请求**转发**给 GUI 线程 → 阻塞等结果；
- 用户在原生对话框里选完 / 取消 → 结果回传 → HTTP 响应返回。

实现见 `nodel_client_dashboard_web/native_dialogs.py`。

### 2.2 会话审计必须原样保留

导入会改本机文件（覆盖 `node_client.exe`、写 `.env`），属于**受审计操作**。
TK 版用 `session.perform("import_node", cfg, fn)` 提交严格意图审计，
失败会落本地队列并锁住后续操作。

Web 版**必须**走同一条路径，不能因为换了 UI 就绕过审计。
`import_service` 直接接受一个 `perform` 回调，由调用方注入。

### 2.3 MetaTrader5 必须是硬依赖（已实测）

「账号留空 → 读终端当前登录号」依赖 `MetaTrader5` 包。
这是个**静默降级**的依赖：缺了它，面板照常启动、其它功能照常工作，
只有导入节点时读不到账号，界面提示
「本机没有 MetaTrader5 接口，请手工填写账号」。

两个坑都踩过：

| 坑 | 现象 | 修法 |
|---|---|---|
| 开发 venv 没装 MetaTrader5 | import 失败 | `pip install -r requirements.txt`（清单里本就有） |
| spec 没 `collect_all("MetaTrader5")` | 冻结产物里 import 失败 | PyInstaller 不会自动发现 `.pyd` 扩展，必须显式收集 |
| spec 把 numpy 排除了 | `numpy._core.multiarray failed to import` | numpy 是 MetaTrader5 的**硬依赖**，不能排除 |

第三条实测确认：屏蔽 numpy 后 `import MetaTrader5` 直接抛
`ImportError: numpy._core.multiarray failed to import`。

因此 `dashboard_web.spec` 必须 `collect_all("MetaTrader5")` +
`collect_all("numpy")`，且 `excludes` 里不能有 `numpy`。
代价是 exe 从 12MB 涨到 38MB（numpy 带着 OpenBLAS）。

验证方式：`exe --mt5-probe`（见 `main.py`），跑一次冻结产物，
确认能读到真实终端的登录号。

---

## 3. 导入流程（与 TK 版一致）

```
① 选目录           校验含 terminal64.exe
② 选版本           后端拉版本清单，默认当前发布版
③ 填账号           留空则读终端当前登录号
④ 确认             展示将要发生的变更
⑤ 申请节点         session.enrolled() → 拿到 node_id
⑥ 下载             版本包 → sha256 校验
⑦ 解压             到临时目录
⑧ 停旧进程         该目录里的客户端（含面板没接管的）
⑨ 覆盖部署         apply_package(backup=True)，先备份旧版本
⑩ 写 .env          MANAGER_WS_URL / DASHBOARD_EXPECTED_MT5_LOGIN
⑪ 注册实例         加入 manager + 持久化
```

失败点与回滚语义与 TK 版一致：部署失败会保留备份，可回滚。

---

## 4. 接口设计

### 4.1 同步校验类（普通 REST）

| 接口 | 说明 |
|---|---|
| `GET /api/import/check-dir?path=…` | 校验目录，返回终端名、是否已有客户端/`.env` |
| `GET /api/import/versions` | 后端版本清单（含当前发布版） |
| `GET /api/import/pick-dir` | **打开原生目录选择器**（GUI 线程 marshal） |
| `POST /api/import/prepare` | 账号解析 + 申请节点，返回待导入的实例摘要 |

### 4.2 导入执行（异步任务 + 进度推送）

导入要下载解压，可能几分钟，不能占住 HTTP 连接。

```
POST /api/import/start   → { task_id }        立即返回
GET  /api/import/progress?task_id=…  → { step, pct, log[], done, error }
```

进度走 **轮询**而非 SSE，理由：
- 导入是单用户单任务，不需要广播；
- SSE 通道已经被运行态快照占满，混进去会让前端订阅逻辑变复杂；
- 轮询实现更简单，且导入期间前端本来就要定期刷新。

---

## 5. 前端

新增 `ImportModal.vue`，两阶段：

**阶段一 · 表单**
- 目录输入框 + 「浏览…」按钮（调 `/api/import/pick-dir`）
- 实例标签
- 版本下拉（`/api/import/versions`）
- 账号输入（可留空）
- 「生成准备配置 .env」复选框

**阶段二 · 进度**
- `.import-spinner` 转圈
- `.import-progress-track` / `.import-progress-fill` 进度条
- `.import-steps` 步骤列表（待办 / 进行中 / 完成）
- `.import-log` 日志流

样式全部复用 `reference.css` 里已有的类名
（`.import-screen` `.import-progress-fill` `.import-step` `.import-log`
`.check-list` `.modal-note` `.modal-error` `.dir-input-row` `.browse-btn`），
**不需要新增 CSS**。

---

## 6. 文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `nodel_client_dashboard_web/import_service.py` | 新增 | UI 无关的导入编排 |
| `nodel_client_dashboard_web/import_dialog.py` | 新增 | Web 版参数收集 + 任务调度 |
| `nodel_client_dashboard_web/native_dialogs.py` | 新增 | GUI 线程 marshal 的原生对话框 |
| `nodel_client_dashboard_web/dashboard_bridge.py` | 修改 | 接入导入接口 + 任务表 |
| `frontend-console/src/components/ImportModal.vue` | 新增 | 导入弹窗 |
| `frontend-console/src/api/bridge.ts` | 修改 | 导入相关 API |
| `frontend-console/src/components/ConsoleShell.vue` | 修改 | 「添加节点」按钮接线 |
| `nodel_client_dashboard_web/test_import_service.py` | 新增 | 导入编排单测 |

---

## 7. 与 TK 版的关系

- **不动** `nodel_client_dashboard/import_dialog.py`（TK 版继续可用）。
- `import_service.py` 只放进 web 包，两边各自维护；
  若后续想彻底去重，再把 TK 版的 `_deploy` 也改为调用它。
- 两版共用同一份 `instances.json`，导入结果互相可见。
