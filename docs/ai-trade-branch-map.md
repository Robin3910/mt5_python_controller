# AI 交易 功能分支脑图

> 路由 /ai-trade，菜单码 ai_trade，权限 MENU_AI_TRADE（assignable，人人有）；左栏 AI 机器人广场 + 右栏「机器人详情 / 我的 AI 跟单」二态切换

本文件对照当前实现整理（后端 `backend/app/ai_follow_api.py`、`backend/app/follow_rules.py`、前端 `frontend/src/views/AiTradeView.vue`）。

共 66 条可测分支，分 10 个一级模块。

## 目录

1. **① 页面骨架与加载** — 6 条分支
2. **② AI 机器人广场（左栏）** — 7 条分支
3. **③ 机器人详情（右栏）** — 7 条分支
4. **④ 加入 AI 跟单（join）** — 8 条分支
5. **⑤ 我的 AI 跟单** — 6 条分支
6. **⑥ 移出 AI 跟单（leave）** — 6 条分支
7. **⑦ 跟单市场与互斥** — 7 条分支
8. **⑧ 分发侧口径（跟单员如何收到信号）** — 7 条分支
9. **⑨ 节点准入与生命周期清理** — 6 条分支
10. **⑩ 权限与审计** — 6 条分支

## 分支总览

| 编号 | 分支 | 界面表现 | 后端行为 | 测试要点 |
| --- | --- | --- | --- | --- |
| `1.1` | ① 页面骨架与加载 › 路由与菜单 | 顶栏「AI 交易」入口，路径 /ai-trade | MENU_AI_TRADE = 'ai_trade'，MenuDef(assignable=True) → 内置 user 角色默认可见 | 普通用户默认能看到「AI 交易」Tab；无 ai_trade 菜单权限时路由被守卫重定向 |
| `1.2` | ① 页面骨架与加载 › 两栏布局与响应式 | 左 minmax(280px,380px) 广场 + 右 1fr 详情/我的跟单；≤960px 断点塌成单列 | — | 窄屏下两个面板应上下排列而非挤压 |
| `1.3` | ① 页面骨架与加载 › 初始并发加载 | onMounted 同时 loadRobots() 与 loadMine()，互不阻塞 | GET /api/ai-follow/robots、GET /api/ai-follow/me | 广场接口慢时「我的 AI 跟单」应先出现，不被拖住 |
| `1.4` | ① 页面骨架与加载 › 45s 轮询替代 WS | 页面可见时每 45s 重拉「我的 AI 跟单」；页面不可见时不轮询 | 跟单员收不到 WS 事件（event_visible 只认 owner） | 切到别的标签页时应停止轮询 |
| `1.5` | ① 页面骨架与加载 › 加载中 / 失败提示 | 加载中显示「加载中…」；失败 ElMessage.error(后端 detail \|\| 兜底文案) | errText() 统一取 response.data.detail | 接口 500 时应弹出「加载机器人广场失败」而不是白屏 |
| `1.6` | ① 页面骨架与加载 › 广场内搜索 | 占位「搜索用户名 / 显示名 / 主节点名 / MT5 号」，回车触发查询 | GET /api/ai-follow/robots?q=，对 username/display_name/主节点名/mt5_login 做包含匹配 | 输入主节点 MT5 号应能命中 |
| `2.1` | ② AI 机器人广场（左栏） › 准入五条件 | 只列出真正能对外接单的机器人主人 | ai_strategy **且** follow_leader + is_active + 有主节点 + ≥1 个已开放 AI 跟单的 tpl_1 分组 | 只持一个能力的主人不应出现在广场 |
| `2.2` | ② AI 机器人广场（左栏） › 未开放开关的分组不进广场 | 分组 accept_ai_follow=false 时，主人不出现在列表 | _ai_groups_of(..., only_open=True) 过滤 accept_ai_follow | 关掉开关后刷新列表该主人应消失 |
| `2.3` | ② AI 机器人广场（左栏） › 卡片内容 | 显示名 / @用户名 + 主节点行 + 风险上限行 + AI分组数 / 被跟节点数 / 已跟标记 | primary_node、ai_group_count、follower_node_count、my_follow_group_count、my_follow_node_count | 跟过一次后卡片应出现「已跟 N 组 / N 节点」绿标 |
| `2.4` | ② AI 机器人广场（左栏） › 风险上限随卡片展示 | 卡片第一眼显示「批次 N · 手数 X」，取各分组里的最大值 | max_allow_num / total_lot_limit 取各分组 max | 跟单员不控手数，必须先看到风险量级再决定跟谁 |
| `2.5` | ② AI 机器人广场（左栏） › 排序：已跟的排前面 | 我已跟过的机器人优先，其余按 username 排 | sort(key=(my_follow_group_count == 0, username)) | 已跟过的机器人应出现在列表靠前位置 |
| `2.6` | ② AI 机器人广场（左栏） › 空态 | 「暂无机器人」 | — | 全场无机器人时应显示该文案而非空白 |
| `2.7` | ② AI 机器人广场（左栏） › 点击打开详情 | 点击卡片 → detailLoading → 拉详情；失败时 detail 置 null 并提示「加载机器人详情失败」 | GET /api/ai-follow/robots/{leader_user_id} | 点不存在的机器人应提示 404 文案并回到列表 |
| `3.1` | ③ 机器人详情（右栏） › 风险上限条 | 详情页顶部独立一条：批次上限 / 总手数上限 | detail.robot.max_allow_num / total_lot_limit | 与广场卡片上的数值应一致 |
| `3.2` | ③ 机器人详情（右栏） › 可跟 AI 分组（多选） | 列出可跟分组：名称 + 品种 · 策略名 · 单槽执行 · 批次/手数 · 加仓倍数 | detail.groups 中 selectable=true 的分组；selectedGroupIds 参与提交 | 勾选后才进 group_ids，不勾不提交 |
| `3.3` | ③ 机器人详情（右栏） › 不可选分组整体不渲染 | 分组禁用 / 策略已停用的分组在界面上**完全看不到**（既不可勾选，也没有原因文案） | 前端 selectableGroups = groups.filter(g => g.selectable)；后端仍返回 selectable=false + unselectable_reason | 停用策略后该分组应从列表消失；若期望看到原因，需前端补渲染不可选区 |
| `3.4` | ③ 机器人详情（右栏） › 单槽执行提示 | 分组行固定显示「单槽执行」；接口固定 signal_concurrent_enabled=false | AI 智能加仓策略不支持信号并发（supports_signal_concurrent 只放行 tpl_2/tpl_3） | 同一节点同时只跑一单 |
| `3.5` | ③ 机器人详情（右栏） › 我的节点（可多选） | 复选框：节点名 + MT5 号 + 在线/离线；主节点已被剔除 | 后端 _viewable_follow_nodes（自己的 + 启用 + 非主节点） | 主节点不应出现在可跟节点列表里 |
| `3.6` | ③ 机器人详情（右栏） › 已跟过的节点标记 | 该机器人下已跟的节点显示琥珀色「已跟过」标签 | alreadyFollowingNodeIds 由 /api/ai-follow/me 聚合 | 再次提交时会走「部分节点已跟」二次确认 |
| `3.7` | ③ 机器人详情（右栏） › 提交条 + 三条风险提示 | 【确认跟单】按钮 + 警示：手数不缩放 / 策略实时同步 / 移出不自动平仓 | POST /api/ai-follow/join | 三条提示必须同时可见，缺一条即为回归 |
| `4.1` | ④ 加入 AI 跟单（join） › 校验顺序固定 | 越权与不存在一律 4xx，不透露对方名下有什么分组 | 主人有效性 → 主节点存在 → 节点归属/主节点 → 分组可跟单性 | 请求不存在的 leader_user_id 应 404 |
| `4.2` | ④ 加入 AI 跟单（join） › 节点拒绝原因 | skipped 里逐条给原因：主节点不能用于跟单 / 节点已停用 / 节点不属于你 / 已在此分组跟单 | follow_rules.node_join_reject_reason | 把主节点塞进 node_ids 应被拒且原因可读 |
| `4.3` | ④ 加入 AI 跟单（join） › 分组静默跳过 | 不可跟的分组不进 joined | followable.get(gid) 命中不到 → continue（市场外或 accept_ai_follow=false） | 混入 tpl_3 分组 ID 时应 400 且不写入任何关系 |
| `4.4` | ④ 加入 AI 跟单（join） › 幂等：已跟过的不重复写 | 已存在的跟单关系计入 already_node_ids | add_following 唯一键 (group_id, node_id)，已存在返回 False | 同一节点重复提交两次，关系表应只有一行 |
| `4.5` | ④ 加入 AI 跟单（join） › 部分节点已跟 → 二次确认 | 「所选节点中包含已跟单该机器人的节点，会跳过重复项。是否继续？」 | 前端 alreadyFollowingNodeIds 交集检测 | 选【继续】应照常提交并跳过重复项 |
| `4.6` | ④ 加入 AI 跟单（join） › 确认跟单二次确认 | 弹窗写明三条风险 + 【确认跟单】/【取消】 | ElMessageBox.confirm，customClass ai-join-confirm | 取消不应产生任何写入 |
| `4.7` | ④ 加入 AI 跟单（join） › 成功提示与刷新 | 「已加入 N 个分组」；随后刷新广场 + 我的跟单 + 当前详情 | res.joined.length；refreshAll() + 条件重拉 detail | 提交后三处状态都应刷新 |
| `4.8` | ④ 加入 AI 跟单（join） › 操作审计 | 每次跟单写一条审计 | persist.audit(action='join_ai_follow', category='ai_follow') | 【操作审计】页按 ai_follow 分类能筛出本次跟单 |
| `5.1` | ⑤ 我的 AI 跟单 › 按机器人聚合 | 后端返回 following[]（每个机器人一块）+ summary | GET /api/ai-follow/me | 跟了 2 个机器人应显示 2 个块 |
| `5.2` | ⑤ 我的 AI 跟单 › 前端按 node_id 去重 | 同一机器人名下多个分组里的同一节点只显示一行 | myFollowing computed 按 node_id 建 Map | 同一节点跟了两个分组时不应出现两行 |
| `5.3` | ⑤ 我的 AI 跟单 › 聚合口径 | 活跃任务 = 各分组之和；加入时间取最早；手数上限取最大 | active_task_count 累加、joined_at 取 min、batch_limit 取 max | 重复跟同一节点应保留第一次的加入时间 |
| `5.4` | ⑤ 我的 AI 跟单 › 不可跟标灰（当前只有粗粒度标签） | 机器人被撤能力 / 换主节点 / 分组禁用 / 关闭开关时，头部标琥珀色「已下架」；具体原因文案**未渲染**，且此时移出按钮变成「等待清理」而无法自行清理 | 后端按 marketable_reason + AI 特有「该机器人已关闭 AI 跟单」给出 unmarketable_reason；前端只读 marketable 布尔值 | 主人被撤能力后应能自行移出 —— 当前 UI 挡掉了移出入口，与后端「清理不需要能力」的语义不一致 |
| `5.5` | ⑤ 我的 AI 跟单 › 节点行信息 | 分组名 / 节点名 + MT5 / 活跃任务 / 手数上限 + 【移出】 | batch_limit 取自策略 max_allow_num | 行内应能看到还剩多少加仓空间（风险敞口） |
| `5.6` | ⑤ 我的 AI 跟单 › 空态 | 「暂无 AI 跟单」 | — | 没跟过时应显示该文案而非空白 |
| `6.1` | ⑥ 移出 AI 跟单（leave） › 移出二次确认 | 「移出后关系解除，不再下发新信号。已有任务不会自动平仓」+【移出（不停止在跑任务）】/【取消】 | ElMessageBox.confirm，customClass ai-leave-confirm | 取消不应解除关系 |
| `6.2` | ⑥ 移出 AI 跟单（leave） › 在跑任务不停止 | 移出只解绑关系，不下发平仓指令 | close_tasks 默认 false；_stop_following_subtasks 仅在 close_tasks=true 时调用 | 移出后机器人再触发一次信号，该节点不应收到 |
| `6.3` | ⑥ 移出 AI 跟单（leave） › 不要求主人仍有能力 | 主人被撤能力后，跟单员仍能自行移出 | leave 不做 can_host_ai_robot 校验 | 对方被撤能力后仍应能点移出并成功 |
| `6.4` | ⑥ 移出 AI 跟单（leave） › closing / forced / warning 提示 | 「已移出 N 个节点」+「N 个任务正在平仓」+「N 个离线节点已排队补发平仓」 | res.closing / res.forced / res.warning | 离线节点移出应出现补发平仓提示 |
| `6.5` | ⑥ 移出 AI 跟单（leave） › 移出后刷新 | 刷新广场 + 我的跟单；若正看该机器人详情再重拉一次 | Promise.all([loadRobots(), loadMine()]) + 条件重拉 detail | 移出后详情页的「已跟过」标记应消失 |
| `6.6` | ⑥ 移出 AI 跟单（leave） › 移出后不再接新信号 | 关系解除后后续分发不再覆盖该节点 | 分组缓存 followers 移除 → _effective_with_follows 不再纳入 | 同上：移出后该节点不应再产生子任务 |
| `7.1` | ⑦ 跟单市场与互斥 › 两个市场互斥 | 【带单】= tpl_2 趋势 + tpl_3 网格；【AI 交易】= tpl_1 AI 智能加仓 | FOLLOW_MARKET_TEMPLATE_IDS / AI_MARKET_TEMPLATE_IDS；一个分组只绑一个策略 → 天然互斥 | tpl_1 分组在 /api/follow/join 里写不进 |
| `7.2` | ⑦ 跟单市场与互斥 › 单一判定入口 | 列表 / 详情 / join 三处共用 followable_groups | follow_rules.followable_groups(market=..., strategy_templates_map=...) | 改一次白名单，三个入口同时生效 |
| `7.3` | ⑦ 跟单市场与互斥 › 白名单而非黑名单 | 未来新增模版默认不外露 | template_in_market 命中白名单才算 | 新增 tpl_4 若未加白名单应默认不可跟 |
| `7.4` | ⑦ 跟单市场与互斥 › 模版未知不判死 | 未绑策略 / 策略被删除的分组保持既有可跟单行为 | 用 template_out_of_market：只在「模版已知且不在市场内」时拒绝 | 删掉策略后分组仍可跟，但分发侧不会下发 |
| `7.5` | ⑦ 跟单市场与互斥 › accept_ai_follow 显式开关 | 分组级开关，默认关；只对绑定 tpl_1 的分组有意义 | node_group.accept_ai_follow + 分组编辑页开关（仅 AI 策略分组显示） | 新建分组的 accept_ai_follow 默认为 false |
| `7.6` | ⑦ 跟单市场与互斥 › 开关在写入侧校验 | 未开放的分组直接调 API 也写不进关系 | join 里 followable 只取 accept_ai_follow=true 的分组 | 绕过 UI 调 API 也应 400 |
| `7.7` | ⑦ 跟单市场与互斥 › 跨市场继承隔离 | adopt-followers 只继承与目标分组**同市场**的来源分组 | groups.py：目标模版定 market → 来源按 template_in_market 收窄 | 在 AI 分组触发继承，不应把网格分组的跟单节点拉进来 |
| `8.1` | ⑧ 分发侧口径（跟单员如何收到信号） › 参与者 = owner 成员 + 跟单成员 | 跟单员加入的节点同样接收信号，且稳定排在 owner 成员之后 | group_rules.participant_ids（去重保序） | 分组里同时有 owner 成员和跟单成员时两者都应收到 |
| `8.2` | ⑧ 分发侧口径（跟单员如何收到信号） › 有效节点 = 存在 + 启用 + 在线 | 跟单节点离线则本轮不接收，poll 模式自动跳下一个 | group_rules.effective_node_ids | 跟单节点离线时分组仍能正常开仓 |
| `8.3` | ⑧ 分发侧口径（跟单员如何收到信号） › 主人失去能力 → 退回 owner 口径 | 跟单节点一律不再接收新信号，已在跑的任务不受影响，自然跑完 | group_dispatcher._effective_with_follows → owner_effective_node_ids | 撤销能力后触发信号，跟单节点不应产生子任务 |
| `8.4` | ⑧ 分发侧口径（跟单员如何收到信号） › 未知模版 → 同样退回 owner 口径 | 两个市场都不收的分组，跟单节点不接收新信号（保守） | follow_rules.market_for_template 为空集 → 降级 | 把分组策略换成未知模版后跟单节点不应收到信号 |
| `8.5` | ⑧ 分发侧口径（跟单员如何收到信号） › 单槽：忙闲占位按 (group_id, node_id) | 跟单员之间不互相挤占——各跟单员节点各有独立单槽；真正的限制是同一节点同时只跑一单 | store.acquire_group_node_busy(group_id, node_id) | 两个跟单员跟同一分组应能同时各跑一单 |
| `8.6` | ⑧ 分发侧口径（跟单员如何收到信号） › 策略热推实时同步 | 机器人主人改参数 → 在跑任务被即时改写，跟单员无法锁定 | rewrite_running_strategy_snapshots + push_strategy_updates（按 strategy_id 覆盖非终态子任务） | 主人改 lot_times 后跟单员在跑任务应立刻生效 |
| `8.7` | ⑧ 分发侧口径（跟单员如何收到信号） › 手数不缩放 | 跟单员按机器人设定的完整手数执行（风险文案写在 UI） | group_rules.resolve_volume(signal.volume) | 机器人设 1 手时跟单节点也应按 1 手执行 |
| `9.1` | ⑨ 节点准入与生命周期清理 › 主节点永不可跟单 | 主节点同时是机器人载体，保持独立才有「机器人 / 跟单」的方向性 | join 时 node_join_reject_reason 判 is_primary；前端也本地剔除 | 主节点塞进 join 请求应被拒 |
| `9.2` | ⑨ 节点准入与生命周期清理 › 停用节点不可跟 | 已停用节点给出「节点已停用」 | node_join_reject_reason 判 enabled | 停用某节点后它不应出现在可跟列表 |
| `9.3` | ⑨ 节点准入与生命周期清理 › 跟单节点升主节点 → **被拒绝** | 升主后不再具备跟单资格；跟单关系是持久状态，不能静默清掉，所以硬拦截让用户先自己移出 | node_service.set_primary → precheck_following_blocking（409 code=primary_switch_following_blocked） | 把已跟单的节点设为主节点应被拒（409），且我的跟单里应仍有它 |
| `9.4` | ⑨ 节点准入与生命周期清理 › 节点删除 / 归属转移 → 清跟单关系 | 不留永远不会被分发的脏关系 | node_service.delete_node、users_api.assign_nodes → remove_node_followings | 把跟单节点转移给别的用户后，我的跟单里应不再有它 |
| `9.5` | ⑨ 节点准入与生命周期清理 › 用户删除 → 清其全部跟单关系 | 防止指向已删除用户的脏关系 | users_api.delete_user → follow_service.remove_user_followings | 删除用户后其跟单关系应被级联清掉 |
| `9.6` | ⑨ 节点准入与生命周期清理 › 分组删除 → 级联删跟单行 | 分组没了关系也不能留 | group_service.delete_group 事务内级联 | 机器人主人删除分组后，跟单员侧不应残留该分组条目 |
| `10.1` | ⑩ 权限与审计 › 方向性约定：跟单人人可做 | 能进【AI 交易】菜单即可加入跟单，不需要任何额外能力 | 五个路由只校验 require_menu(MENU_AI_TRADE) | 无任何能力的普通用户应能正常跟别人的 AI 机器人 |
| `10.2` | ⑩ 权限与审计 › 方向性约定：机器人主人需两能力 | 我能否被别人跟，由 ai_strategy 且 follow_leader 共同决定 | permissions.can_host_ai_robot(principal) | 只给一个能力则不出现在广场 |
| `10.3` | ⑩ 权限与审计 › 能力撤销立即生效 | 撤销后立刻从广场消失，但存量关系保留、在跑任务跑完 | 按用户名现算 principal（带缓存），避免撤销后仍对外可见 | 撤销能力后刷新广场该主人应消失 |
| `10.4` | ⑩ 权限与审计 › 管理员自动放行 | 管理员天然具备全部能力 | can_capability 内部 is_admin 分支 | 管理员应在广场里看到自己 |
| `10.5` | ⑩ 权限与审计 › 不泄露对方分组 | 越权与不存在返回同一个原因 | node_join_reject_reason 不返回「对方分组是否存在」类信息 | 用他人 node_id 探测不应得到可区分的报错 |
| `10.6` | ⑩ 权限与审计 › 审计分类 ai_follow | 加入 / 移出各写一条审计，分类与带单分开 | persist.audit(category='ai_follow', action='join_ai_follow' / 'leave_ai_follow') | 【操作审计】页按 ai_follow 分类能筛出 AI 跟单操作 |

## 脑图结构

```
AI 交易 /ai-trade
├─ ① 页面骨架与加载
│  ├─ 路由与菜单
│  ├─ 两栏布局与响应式
│  ├─ 初始并发加载
│  ├─ 45s 轮询替代 WS
│  ├─ 加载中 / 失败提示
│  └─ 广场内搜索
├─ ② AI 机器人广场（左栏）
│  ├─ 准入五条件
│  ├─ 未开放开关的分组不进广场
│  ├─ 卡片内容
│  ├─ 风险上限随卡片展示
│  ├─ 排序：已跟的排前面
│  ├─ 空态
│  └─ 点击打开详情
├─ ③ 机器人详情（右栏）
│  ├─ 风险上限条
│  ├─ 可跟 AI 分组（多选）
│  ├─ 不可选分组整体不渲染
│  ├─ 单槽执行提示
│  ├─ 我的节点（可多选）
│  ├─ 已跟过的节点标记
│  └─ 提交条 + 三条风险提示
├─ ④ 加入 AI 跟单（join）
│  ├─ 校验顺序固定
│  ├─ 节点拒绝原因
│  ├─ 分组静默跳过
│  ├─ 幂等：已跟过的不重复写
│  ├─ 部分节点已跟 → 二次确认
│  ├─ 确认跟单二次确认
│  ├─ 成功提示与刷新
│  └─ 操作审计
├─ ⑤ 我的 AI 跟单
│  ├─ 按机器人聚合
│  ├─ 前端按 node_id 去重
│  ├─ 聚合口径
│  ├─ 不可跟标灰（当前只有粗粒度标签）
│  ├─ 节点行信息
│  └─ 空态
├─ ⑥ 移出 AI 跟单（leave）
│  ├─ 移出二次确认
│  ├─ 在跑任务不停止
│  ├─ 不要求主人仍有能力
│  ├─ closing / forced / warning 提示
│  ├─ 移出后刷新
│  └─ 移出后不再接新信号
├─ ⑦ 跟单市场与互斥
│  ├─ 两个市场互斥
│  ├─ 单一判定入口
│  ├─ 白名单而非黑名单
│  ├─ 模版未知不判死
│  ├─ accept_ai_follow 显式开关
│  ├─ 开关在写入侧校验
│  └─ 跨市场继承隔离
├─ ⑧ 分发侧口径（跟单员如何收到信号）
│  ├─ 参与者 = owner 成员 + 跟单成员
│  ├─ 有效节点 = 存在 + 启用 + 在线
│  ├─ 主人失去能力 → 退回 owner 口径
│  ├─ 未知模版 → 同样退回 owner 口径
│  ├─ 单槽：忙闲占位按 (group_id, node_id)
│  ├─ 策略热推实时同步
│  └─ 手数不缩放
├─ ⑨ 节点准入与生命周期清理
│  ├─ 主节点永不可跟单
│  ├─ 停用节点不可跟
│  ├─ 跟单节点升主节点 → 自动移出跟单
│  ├─ 节点删除 / 归属转移 → 清跟单关系
│  ├─ 用户删除 → 清其全部跟单关系
│  └─ 分组删除 → 级联删跟单行
└─ ⑩ 权限与审计
   ├─ 方向性约定：跟单人人可做
   ├─ 方向性约定：机器人主人需两能力
   ├─ 能力撤销立即生效
   ├─ 管理员自动放行
   ├─ 不泄露对方分组
   └─ 审计分类 ai_follow
```
