# node_client 本机运维面板

Python + CustomTkinter 桌面程序：管理本机多个 `node_client.exe` 的启停、守护、健康检查、策略任务快照与日志。

**使用教程**（导入节点、日常启停、版本更新、排障）见仓库根目录 [`运维面板使用教程.md`](../运维面板使用教程.md)。本节面向开发运行与打包。

## 开发运行

```bat
cd nodel_client_dashboard
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 使用

首先使用后台账号登录；后端地址读取程序目录 `.env` 的 `APP_URL`，不在登录页填写。已开启 2FA 时需输入验证码，普通账号需拥有「节点」菜单权限。密码与 JWT 只在内存。普通用户只管理本人节点，管理员可管理全部本机实例。

普通用户导入、添加新节点后，须由管理员在节点页确认启用、在用户权限页分配给申请人，两项齐全才开通（顺序不限）。开通后面板自动写入专属节点令牌；丢失或迁移时，本人登录并二次确认后重签。

1. 点击「导入节点」下载兼容客户端并申请开通；已有客户端可「手工添加」，工作目录默认取其同目录
2. 开通后「启动」会注入 `LOCAL_STATUS_PORT` 与预期 MT5 账号，实际终端账号缺失或不符时客户端退出；**无窗口后台**拉起节点，stdout/stderr **实时**写入 `logs/<节点标识符>/YYYY-MM-DD.log`（按天轮转；节点标识符 = 标签 + 实例 id 前 8 位）
3. 「停止」优先 `POST /stop`，超时再 terminate/kill
4. 打开「守护进程」后，子进程退出会按 `restart_delay_s`（默认 5s）自动拉起；可用「全部开守护 / 全部关守护」批量切换
5. 「手工替换全部」用兼容的新 `node_client.exe`（随附 `client_capabilities.json` 与 `version.txt`）更新当前有权管理的实例：停 → 备份 → 覆盖 → 再启
6. 右侧查看健康摘要、runners 任务表、版本与日志尾部（界面为快照差分绑定，刷新不整页重建）

> 运维面板**仅允许单实例**：再次启动会提示并切到已有窗口。
>
> **崩溃自恢复**：默认由看门狗拉起；异常退出后间隔 5s 自动重启，最多 3 次（连续稳定运行 ≥60s 后计数清零）。日志见 `logs/dashboard_supervisor.log`。可用环境变量 `DASHBOARD_RESTART_MAX` / `DASHBOARD_RESTART_DELAY_S` 调整；`DASHBOARD_NO_SUPERVISOR=1` 关闭看门狗。
>
> 后台启动无法在控制台交互输入 MT5 账号：请先登录终端（或设 `MT5_MOCK=true`）。
>
> 关闭或最小化窗口会进入系统托盘（不退出）；托盘右键「显示主窗口 / 退出」。退出不会强杀已启动的 node_client。从最小化/托盘恢复时会自动重绘，避免 CustomTkinter 文字叠影。
>
> 面板重启后须登录，才按 `instances.json` 保存的 `status_port` 接管有权管理的客户端。运行中的旧实例按实际 MT5 账号匹配；缺失绑定的旧实例由管理员手工绑定。启动要求客户端支持账号绑定能力，旧客户端先更新。
>
> 断网、JWT 失效或退出登录时锁定交互，正在运行的节点与此前开启的守护继续。后台确认节点/用户禁用、权限撤回或归属变化后，停止未来自动重启，保留当前进程。面板进程重启后重新登录才恢复守护。
>
> 用户操作先取得后台授权并写入操作审计；结果上报失败会脱敏排队并锁定新操作。自动重启事件可在断网期间排队，恢复后补传。登录凭据和守护能力均不保存到磁盘。

## 打包

```bat
build_exe.bat
```

产物两份：

- `dist\node_client_dashboard.exe`（`instances.json` / `panel_config.json` / `logs/` 运行时写在 exe 同目录）
- `packages\node_client_dashboard-<版本>.zip` —— 拷到新机器解压即用的分发包

版本号每次打包自动生成，规则 `${数字版本号}-${年月日时分秒}`（如 `1.0.0-20260813155913`）。数字版本号取 `version.py` 的 `BASE_VERSION`，只在发大版本时手工改；时间戳由构建脚本生成，所以同一天连打几次也不会撞号。版本号一式两份：`_build_info.py` 冻结进 exe，`version.txt` 写在产物目录旁，窗口标题会显示它。

压缩包内部带一层 `node_client_dashboard-<版本>\` 目录，解压不会把文件铺一地。打包时把源码目录的 `.env`（只放 `APP_URL`）复制到 `dist\`，并打进压缩包，与 exe 同目录。**面板的运行期数据绝不入包**：排除 `panel_config.json`、`instances.json`、日志、备份及审计补传队列。面板配置仅保存后端地址、上次用户名和旧版兼容配置；密码、JWT、2FA 中间令牌和守护能力不落盘。打包后自检主程序及排除项，否则构建失败。

## 测试

```bat
pip install pytest
pytest -q
```
