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

1. 点击「手工添加」选择 `node_client.exe`（工作目录默认取其同目录，需已有 `.env`）
2. 「启动」会注入 `LOCAL_STATUS_PORT`，**无窗口后台**拉起节点；stdout/stderr **实时**写入 `logs/<节点标识符>/YYYY-MM-DD.log`（按天轮转；节点标识符 = 标签 + 实例 id 前 8 位）
3. 「停止」优先 `POST /stop`，超时再 terminate/kill
4. 打开「守护进程」后，子进程退出会按 `restart_delay_s`（默认 5s）自动拉起；可用「全部开守护 / 全部关守护」批量切换
5. 「手工替换全部」用新 `node_client.exe`（建议带 `version.txt`）覆盖全部实例：停 → 备份 → 覆盖 → 再启
6. 右侧查看健康摘要、runners 任务表、版本与日志尾部（界面为快照差分绑定，刷新不整页重建）

> 运维面板**仅允许单实例**：再次启动会提示并切到已有窗口。
>
> **崩溃自恢复**：默认由看门狗拉起；异常退出后间隔 5s 自动重启，最多 3 次（连续稳定运行 ≥60s 后计数清零）。日志见 `logs/dashboard_supervisor.log`。可用环境变量 `DASHBOARD_RESTART_MAX` / `DASHBOARD_RESTART_DELAY_S` 调整；`DASHBOARD_NO_SUPERVISOR=1` 关闭看门狗。
>
> 后台启动无法在控制台交互输入 MT5 账号：请先登录终端（或设 `MT5_MOCK=true`）。
>
> 关闭或最小化窗口会进入系统托盘（不退出）；托盘右键「显示主窗口 / 退出」。退出不会强杀已启动的 node_client。从最小化/托盘恢复时会自动重绘，避免 CustomTkinter 文字叠影。
>
> 面板重启后会按 `instances.json` 里保存的 `status_port` **自动接管**仍在跑的客户端（健康/任务/停止/守护可继续管理）。

## 打包

```bat
build_exe.bat
```

产物两份：

- `dist\node_client_dashboard.exe`（`instances.json` / `panel_config.json` / `logs/` 运行时写在 exe 同目录）
- `packages\node_client_dashboard-<版本>.zip` —— 拷到新机器解压即用的分发包

版本号每次打包自动生成，规则 `${数字版本号}-${年月日时分秒}`（如 `1.0.0-20260813155913`）。数字版本号取 `version.py` 的 `BASE_VERSION`，只在发大版本时手工改；时间戳由构建脚本生成，所以同一天连打几次也不会撞号。版本号一式两份：`_build_info.py` 冻结进 exe，`version.txt` 写在产物目录旁，窗口标题会显示它。

压缩包内部带一层 `node_client_dashboard-<版本>\` 目录，解压不会把文件铺一地。**面板的运行期数据绝不入包**：`panel_config.json` 存着 NODE_TOKEN、`instances.json` 记录本机每个实例的绝对路径，只要在 `dist\` 里跑过一次面板它们就会生成，跟着打包等于把令牌和本机拓扑一起发出去。打包完会自检一次，含主程序且不含这两个文件才算通过，否则删掉产物并让构建失败。

## 测试

```bat
pip install pytest
pytest -q
```
