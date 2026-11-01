# 交易页（/trading）分支脑图

> 覆盖【交易】界面的全部情况分支：主节点选取、报价卡片、三种开仓弹窗、信号提交与后端校验、策略分发、详情页签、平仓与停止、以及权限边界。

本文件由 `scripts/export_trading_branch_map.mjs` 从 Canvas 源文件自动导出，Canvas 版本：`.cursor/projects/d-work-space-mt5-python-controller/canvases/trading-page-branch-map.canvas.tsx`。

共 60 条可测分支，分 10 个一级模块。

## 目录

1. **① 入口 / 主节点选取** — 5 条分支
2. **② 报价卡 / 品种选择** — 5 条分支
3. **③ 开仓弹窗（OrderDialog）** — 14 条分支
4. **④ 前端提交与结果提示** — 4 条分支
5. **⑤ 后端 quick-trigger 校验** — 5 条分支
6. **⑥ 信号处理通用流程** — 5 条分支
7. **⑦ 分发引擎（group_dispatcher）** — 10 条分支
8. **⑧ 底部四个详情 Tab** — 4 条分支
9. **⑨ 平仓 / 停止策略** — 3 条分支
10. **⑩ 权限与鉴权** — 5 条分支

## 分支总览

| 编号 | 分支 | 界面表现 | 后端行为 | 测试要点 |
| --- | --- | --- | --- | --- |
| `1.1` | ① 入口 / 主节点选取 › 无主节点 → 全页空态 | 只渲染「尚未设置主节点」+ 描述 + 【前往节点页】按钮；报价卡、底部三按钮、状态条、详情面板全部不渲染 | — | 把唯一节点 is_primary 置否 → 刷新交易页应只见空态；点击按钮跳到 /nodes |
| `1.2` | ① 入口 / 主节点选取 › 管理员主节点口径 | 仅认 owner_user_id == 当前管理员 且 is_primary && enabled；名下没有主节点就显示空态，不借用他人主节点 | 与 node_service.set_primary / _clear_primary_for_owner 一致 | A 管理员名下节点为主、B 用户名下节点也为主 → 管理员交易页应显示空态而非借用 B |
| `1.3` | ① 入口 / 主节点选取 › 普通用户主节点口径 | is_primary && enabled（后端 /api/nodes 已按 owner 过滤，客户端不再收紧） | — | 普通用户多个主节点时取第一个；自己的主节点禁用后应回落空态 |
| `1.4` | ① 入口 / 主节点选取 › 在线 / 离线双态 | 在线：状态条绿点「在线」；离线：灰点「离线」+ 底部三按钮 disabled + 黄色提示「主节点离线，无法触发」 | quick-trigger 再次校验：manager.is_node_online 不通过 → 400「主节点不在线」 | 断开节点连接后：按钮置灰；强行点按 openDialog → warning「主节点不在线，无法触发」且弹窗不打开 |
| `1.5` | ① 入口 / 主节点选取 › 轮询与快照容错 | onMounted 拉节点列表 + 一次账户快照；此后每 5s 拉一次快照；失败静默忽略不弹错 | — | 让账户快照接口 500，交易页应保持可用、无数值抖动、无错误弹窗 |
| `2.1` | ② 报价卡 / 品种选择 › 品种来源与排序 | 取 quotes 中 bid/ask/mid 任一 > 0 的品种 ∪ 持仓中的品种，去重转大写后按字母序；白名单为空 | — | 节点同时上报 EURUSD 报价 + XAUUSD 持仓 → 两个 chip 都在且已排序 |
| `2.2` | ② 报价卡 / 品种选择 › 无任何报价 | 显示「主节点尚未上报任何品种报价」，不渲染品种切换 chips | — | 节点上线但未上报品种 → 报价区显示该文案，页面其余部分正常 |
| `2.3` | ② 报价卡 / 品种选择 › 当前品种消失 → 自动回退 | watch(availableSymbols)：当前 symbol 不在列表内时自动切到第一个可用品种 | — | 选中 EURUSD 后节点撤掉该品种报价 → 界面应自动切到剩余第一个，不出现空白报价 |
| `2.4` | ② 报价卡 / 品种选择 › 价格格式化 | JPY 结尾 3 位小数、其它 5 位；0/undefined/NaN → 「—」；变动显示 ±xx.xx，正绿负红 | — | USDJPY vs EURUSD 小数位；缺 mid 时中间价显示「—」 |
| `2.5` | ② 报价卡 / 品种选择 › 品种持仓摘要卡 | 仅当该品种有持仓时显示「N 笔 · X 手」+ 浮盈（带正负号，0 记为不显示 +） | — | 多品种持仓时切换 chip，摘要随品种联动 |
| `3.0` | ③ 开仓弹窗（OrderDialog） › 实际可见字段（注意多处已注释隐藏） | 分组 / 开仓品种 / 开仓手数 / 方向 / 止盈价 / 备注 / 手动重算 的输入框当前已被注释隐藏；市价=止损价+风险参数，限价=入场价+止损价+风险参数，网格=完整网格参数集 | — | 回归确认：这些字段确实不可见，且弹窗不会因缺手数而阻断（市价手数由 risk_amount 反算） |
| `3.1` | ③ 开仓弹窗（OrderDialog） › openDialog 前置与表单重置 | 无主节点或离线 → warning 且不打开；打开时重置 form（symbol=当前品种、volume=0.01、sl/tp/entry=0、direction=all、remark 空） | 预拉 GET /api/groups，按 {策略类型}-{用户名}-{MT5号}-{品种} 匹配 group_id；失败则 group_id=null | 连续开三个策略按钮，每次表单应完全重置，不残留上一次输入 |
| `3.2` | ③ 开仓弹窗（OrderDialog） › 分组与策略加载（loadGroupAndStrategy） | 先按 groupId 找分组 → 找不到按 groupName 找 → 还找不到则自动建组（主节点加入）；建组失败显示「自动创建分组失败」，触发按钮保持禁用 | POST /api/groups；首次触发会同时创建「分组 + 策略」 | 同一 (类型,用户,登录号,品种) 第二次触发应复用分组与策略，不重复创建 |
| `3.3` | ③ 开仓弹窗（OrderDialog） › 策略参数回填 | 分组已有 strategy_id → 从启用中的 type=3 规则回填 riskParams（风险金额/盈亏比/底仓占比/保本倍数），从 type=4 回填网格参数；无策略则用模版 tpl_2 / tpl_3 首条规则默认值 | GET /api/strategies/templates、GET /api/strategies | 改了风险参数后重开弹窗应保留；新建时默认值应为 100 / 2.5 / 30 / 0 |
| `3.4` | ③ 开仓弹窗（OrderDialog） › 方向自动推导（derivedAction） | 限价：入场价 > 止损 → BUY 多；< 止损 → SELL 空。市价：止损 < 当前 mid → BUY；止损 > mid → SELL。无法判定时触发按钮禁用，副标题提示「请填写入场价与止损价」/「请填写止损价并等待报价」 | — | 改止损价到 mid 上下两侧，按钮副标题应在 BUY 多 / SELL 空 之间切换 |
| `3.5` | ③ 开仓弹窗（OrderDialog） › 趋势参数校验（validateRiskParams） | 风险金额 > 0、盈亏比 ≥ 0、底仓占比 1~100、保本倍数 ≥ 0；不通过时弹窗底部红框显示原因 | — | 逐个置 0 / 101 / 负数，确认每条都给出对应中文提示且按钮禁用 |
| `3.6` | ③ 开仓弹窗（OrderDialog） › 网格参数校验（validateGridParams） | 区间下限 > 0、上限 > 下限、网格数量 2~200、模式 ∈ 等差/等比、方向 ∈ 只做多/只做空、每格手数 > 0 | — | 网格数量填 1 或 201、区间上下限填反，均应被拦 |
| `3.7` | ③ 开仓弹窗（OrderDialog） › 网格试算（自动） | 条件满足后 debounce 500ms 自动试算，8s 超时；试算中显示「试算中…」，结果回写网格数量与每格手数；无主节点显示「未设置主节点，无法自动试算」；条件不满足静默清空 | GET /api/nodes/{id}/grid_sizing（timeframe=M1、atr_mult=0.6、spacing=0） | 只做多时必须填止损价才触发试算；只做空时必须填止盈价 |
| `3.8` | ③ 开仓弹窗（OrderDialog） › 网格试算失败降级 | ready=false 或每格手数为 0 → 显示 warnings 首条或「该品种试算失败，可手动填写网格参数」；【↻ 重算】按钮可手动再试 | 后端 409（行情读取失败）/ 400（品种代码非法） | 对不支持的品种试算 → 应给出提示且仍允许手动填网格数量后触发 |
| `3.9` | ③ 开仓弹窗（OrderDialog） › 网格条件字段联动 | 方向切换会改变「止损价 / 止盈价」标签文案；「向上追踪」开启后才出现「最大平移格数」；「初始建仓」开关每次开弹窗强制复位为关 | — | 开向上追踪 → 出现最大平移格数行；关掉即消失 |
| `3.10` | ③ 开仓弹窗（OrderDialog） › 带单开关（carry） | 开 → 信号推给本分组全部节点（含跟单员）；关 → 只推主节点 | payload.node_ids = [主节点]，后端按 node_ids 收窄最终下发集合 | 关闭带单后应只有主节点收到信号；同分组其它任务不受影响 |
| `3.11` | ③ 开仓弹窗（OrderDialog） › 触发提交（市价 / 限价） | 建或更新策略 → 限价额外打开限价监听（limit_watch_enabled + limit_watch_node_ids）→ 打开信号并发 → 继承跟单节点 → 发出信号 | POST/PATCH /api/strategies、PATCH /api/groups、POST /api/groups/{id}/adopt-followers | 限价触发后分组应自动开启限价监听；中间步骤失败只提示不阻断信号下发 |
| `3.12` | ③ 开仓弹窗（OrderDialog） › 触发提交（网格） | 建或更新 type=4 网格规则 → 打开信号并发 → 继承跟单节点 → 发出信号；顶层 action 由 grid_side 决定 | 网格方向只做多 → action=BUY；只做空 → action=SELL | 只做空网格发单后节点侧应按 SELL 执行 |
| `3.13` | ③ 开仓弹窗（OrderDialog） › 弹窗关闭与重开 | X / 取消 → 关闭并清空错误；重开时试算结果与网格预填被清空 | — | 触发一次报错后关闭重开，不应残留上次的错误文案 |
| `4.1` | ④ 前端提交与结果提示 › 策略复用路径（manual-signal） | accepted / duplicate → 关闭弹窗；rejected → 弹窗内红框显示 reason；网络异常 → 显示后端 detail | POST /api/console/manual-signal（model=strategy，group_ids=[当前分组]） | 构造一个会被拒的场景（如分组禁用）确认红框文案可读 |
| `4.2` | ④ 前端提交与结果提示 › 一键触发路径（quick-trigger） | accepted → 成功提示「X 个目标节点」；duplicate → 警告「重复信号被抑制（5 秒内已有相同信号）」；rejected → lastError 展示 | POST /api/trading/quick-trigger | 同一参数 5 秒内连点两次 → 第二次必须走 duplicate 分支 |
| `4.3` | ④ 前端提交与结果提示 › 手数与空值兜底 | 手数未填 / 0 / NaN / 负数 → 强制 0.01；sl、tp ≤ 0 → 传 null；非网格模式 direction 传 null | 后端「开仓信号必须填写手数」 | 清空手数提交，不应被后端拦截 |
| `4.4` | ④ 前端提交与结果提示 › 结果文案分级 | targets > 0 → success「已触发 BUY XAUUSD：命中 N 个分组，M 个节点任务开始执行」；targets = 0 → warning「已受理但未下发：命中 N 个分组（原因）」；其他 → info「已提交：status」 | 后端返回 tasks[].reason 汇总 | 分组内无有效节点时，提示应指出具体原因（成员未启用/不在线） |
| `5.1` | ⑤ 后端 quick-trigger 校验 › 主节点四道校验 | 400「主节点不存在或未启用」/ 403「主节点不属于当前用户」/ 400「主节点不在线」/ 400「主节点未绑定 MT5 登录号」 | trading_api.quick_trigger 第 1 步 | 管理员触发他人主节点 → 403；主节点解绑 MT5 登录号 → 400 |
| `5.2` | ⑤ 后端 quick-trigger 校验 › 限价必填入场价 | 400「限价开仓必须填写入场价」 | entry_price 缺失或 ≤ 0 | 直接调接口不带 entry_price 验证 |
| `5.3` | ⑤ 后端 quick-trigger 校验 › 网格八项必填校验 | direction 非法 / 区间下限缺失 / 区间上限缺失 / 上限 ≤ 下限 / 网格数 < 2 / 每格手数 ≤ 0 / 模式非法 / 方向非法，各自 400 | trading_api 第 3 步 | 逐项置空/置非法值各打一次接口，核对文案 |
| `5.4` | ⑤ 后端 quick-trigger 校验 › 策略 / 分组创建与绑定 | 400「创建策略失败：…」/「更新网格策略失败：…」/「创建分组失败：…」/「绑定策略失败：…」 | 命名规则 {strategy_type}-{username}-{login}-{symbol}，同品种不同策略类型互不污染 | 同一用户两个不同品种应生成两组独立 group+strategy |
| `5.5` | ⑤ 后端 quick-trigger 校验 › 趋势方向推导与异常 | 400「无法获取主节点当前报价，请稍后再试」/「趋势策略必须填写止损价」/「入场价与止损价相同，无法判断方向」 | 市价 ref=mid、限价 ref=entry_price；ref > sl → BUY，ref < sl → SELL | 入场价与止损价填相同值 → 应报「无法判断方向」而非静默成单 |
| `6.1` | ⑥ 信号处理通用流程 › 解析与校验失败 | 400「cannot parse signal」（并记录 rejected）/ 400「invalid signal: <原因>」 | parser.parse / parser.validate_signal | 字段类型异常时的报错可读性 |
| `6.2` | ⑥ 信号处理通用流程 › 定向字段校验 | 400（分组/模版/节点定向非法） | webhook._validate_targeting | 普通用户点名他人分组 → 404「分组不存在」 |
| `6.3` | ⑥ 信号处理通用流程 › 5 秒幂等去重 | 返回 duplicate，前端提示「重复信号被抑制」 | 指纹 = model:action:symbol:volume:sl:tp:template_ids:group_ids:node_ids | 仅改 node_ids 的信号应视为不同信号，不被误判重复 |
| `6.4` | ⑥ 信号处理通用流程 › 引擎选择 | 503「service not ready」（分发器未就绪） | model=strategy → group_dispatcher；normal → dispatcher | 交易页固定走 strategy 模型 |
| `6.5` | ⑥ 信号处理通用流程 › 操作审计留痕 | 交易页触发会写入操作审计（category=console，action=quick_trigger / manual_signal） | persist.audit | 在【操作审计】页应能查到本次触发记录与结果状态 |
| `7.1` | ⑦ 分发引擎（group_dispatcher） › CLOSE 分支 | 无匹配活动子任务 → rejected「无匹配任务：…」 | CLOSE 只终止分组内进行中的策略任务，不做账户级全平 | 分组无任务时发 CLOSE 应被明确拒绝 |
| `7.2` | ⑦ 分发引擎（group_dispatcher） › 候选分组筛选（8 条落选原因） | 指定的分组不存在 / 分组已禁用 / 未绑定策略 / 绑定的策略不存在 / 绑定策略已禁用 / 模版定向不符 / 策略品种与信号品种不符 / 准入不通过 | 品种匹配容忍 XAUUSD / XAUUSDm 前缀差异 | 策略绑 EURUSD 却发 XAUUSD 信号 → 应在分发前被拦，不下发节点 |
| `7.3` | ⑦ 分发引擎（group_dispatcher） › 规则启用态准入 | 「策略没有启用中的规则」/「网格策略没有启用中的网格规则」/「趋势策略没有启用中的以损定量规则」 | group_rules.no_active_rule_reason | 把策略规则全部停用后触发 → 应命中这条 |
| `7.4` | ⑦ 分发引擎（group_dispatcher） › 以损定量准入 | 缺 sl / 风险金额 ≤ 0 / 限价缺入场价 / BUY 且入场价 ≤ 止损 / SELL 且入场价 ≥ 止损 | group_rules._reject_risk_sized | 限价 BUY 但入场价低于止损 → 明确提示「需高于止损价」 |
| `7.5` | ⑦ 分发引擎（group_dispatcher） › 网格准入 | 区间非法 / 每格手数 ≤ 0 / 总手数上限 < 每格手数 / 只做多不收 SELL / 只做空不收 BUY | group_rules._reject_grid | 网格只做多却发 SELL → 分发前拦截 |
| `7.6` | ⑦ 分发引擎（group_dispatcher） › 无匹配分组 → 整体拒收 | rejected，前端红框显示「无匹配分组：<逐条原因>」 | dispatch 返回 mode=rejected，targets=0 | 所有候选都被拒时，提示应列出全部原因而非第一条 |
| `7.7` | ⑦ 分发引擎（group_dispatcher） › 建任务后的分组级跳过 | task skipped：分组已禁用 / 分组无成员节点 / 分组无有效节点（成员均未启用或不在线）；主任务创建失败 → failed | group_rules.group_skip_reason | 把主节点下线后触发 → 任务状态应为 skipped 而非 failed |
| `7.8` | ⑦ 分发引擎（group_dispatcher） › 带单与节点定向收窄 | 有跟单成员 + 带单员仍有能力 → owner 成员 + 跟单成员；带单员被撤销能力 → 退回 owner 成员；管理员名下分组（owner 为空）→ 不可带单 | group_dispatcher._effective_with_follows | 撤销带单员能力后，跟单节点应不再收到新信号，但已在跑的任务继续跑 |
| `7.9` | ⑦ 分发引擎（group_dispatcher） › 逐节点下发四态 | sent 成功 / skipped 节点忙（分组内已有进行中子任务）/ skipped 趋势风控拦截 / offline 下发失败 | 趋势风控文案：「趋势风控：信号 BUY，当前趋势为<多/空/震荡/数据不足>（得分 ±x.x），已拦截」 | 开启趋势风控分组后逆势信号 → 节点侧应被跳过，信号记录里带风控原因 |
| `7.10` | ⑦ 分发引擎（group_dispatcher） › 零下发汇总 | 任务 skipped，原因区分：全部被趋势风控拦截 / 全部节点忙 / 节点均不可用 | group_rules.summarize_no_target | 三种零下发场景各验一次汇总文案 |
| `8.1` | ⑧ 底部四个详情 Tab › 当前持仓 | 无持仓 → 「暂无持仓」；有持仓按 ticket 倒序；桌面表格 / ≤640px 切卡片列表；方向标签 多(buy,红) / 空(sell,绿)，其它显示原值或「—」 | — | 桌面与手机两种宽度下布局都要验证；浮盈正负号与配色 |
| `8.2` | ⑧ 底部四个详情 Tab › 资产 | 净值 / 浮动盈亏 / 余额 / 已用保证金 / 可用保证金 / 杠杆 1:N；净值与浮盈带正负号；3 列 → ≤640px 2 列 → ≤420px 1 列 | AccountSnapshot 字段缺失时全部回落为 0 | 快照缺字段时不应显示 NaN |
| `8.3` | ⑧ 底部四个详情 Tab › 历史订单 | 加载中转圈 / 「暂无历史订单」/ 表格；状态着色：filled·success·done·executed=绿，pending·dispatched·queued=黄，rejected·failed·error·skipped=红 | GET /api/nodes/{id}/dispatches?page=&page_size=50 | 每页 50 条，« ‹ › » 翻页；切换主节点自动回第 1 页；请求失败只留空态不弹错 |
| `8.4` | ⑧ 底部四个详情 Tab › 吸筹策略（运行中网格任务） | 来源 = 名称以 grid- 开头的分组 → GET /api/groups/{id}/signals?status=active；每 5s 静默轮询，切走停轮询，分组数变化时重拉；节点名超过 2 个显示「A / B 等 N 节点」 | 同一 signal_id 多节点下发合并为一个任务，持仓/挂单/盈亏按 dispatch 求和 | 新建网格分组后本 Tab 应能拉到；轮询中显示「刷新中…」且不闪 loading |
| `9.1` | ⑨ 平仓 / 停止策略 › 单笔持仓平仓 | 无主节点 → warning「主节点不存在」；缺 ticket → warning「该持仓缺少 ticket，无法平仓」；重复点击被忽略（按钮转圈禁用）；确认框取消则放弃 | POST /api/nodes/{id}/close（target=ticket）；节点离线 → 409 | 平仓成功提示后应立即刷新快照；节点离线时的错误提示要能看出是离线 |
| `9.2` | ⑨ 平仓 / 停止策略 › 平仓的连带影响 | — | 平仓会连带停止该节点上同魔术号的策略任务，避免网格重新挂格 | 网格运行中平掉某格持仓，观察是否被重新加仓 |
| `9.3` | ⑨ 平仓 / 停止策略 › 停止吸筹任务 | 任务终态（done/failed/skipped）或无活跃 dispatch → 按钮禁用「该任务已结束」；确认框文案含品种、节点、节点数；结果提示「已下发停止 XAUUSD · 成功 N · 离线 M」 | POST /api/groups/{id}/dispatches/{did}/close（逐 dispatch 并发下发 strategy_stop） | 只停当前 task，同分组其它 task 不受影响；节点离线时应计入「离线」计数 |
| `10.1` | ⑩ 权限与鉴权 › 交易菜单权限 | 无 trading 菜单权限 → 菜单不出现 / 路由被拦 | require_menu(MENU_TRADING) → 403 | 用无权限账号访问 /trading 与 /api/trading/quick-trigger |
| `10.2` | ⑩ 权限与鉴权 › 普通用户信号模型限制 | 403「普通用户只能手动触发 strategy 信号」 | console_api._scope_to_own_groups | 普通用户只能发 strategy 模型 |
| `10.3` | ⑩ 权限与鉴权 › 分组归属收窄 | 404「分组不存在：…」（点名他人分组）/ 400「你还没有分组，无法手动触发信号」 | 未点名分组则自动注入本人全部分组 | 普通用户无任何分组时触发应报「你还没有分组」 |
| `10.4` | ⑩ 权限与鉴权 › 动作与手数约束 | 400「动作非法：…（应为 BUY / SELL / CLOSE）」/ 400「处理模型非法：…」/ 400「CLOSE 仅支持 strategy 模型…」/ 400「开仓信号必须填写手数」 | console_api._build_signal_payload | 四条约束各打一次接口核对文案 |
| `10.5` | ⑩ 权限与鉴权 › 分组写操作归属 | 404（非本人分组，操作者不是 owner） | owned_group_or_404（停止任务、继承跟单节点） | 跨用户操作分组应 404 而不是 403（不泄露存在性） |

## 脑图结构

```
└─ 交易页 /trading
   ├─ ① 入口 / 主节点选取
   │  ├─ 无主节点 → 全页空态
   │  ├─ 管理员主节点口径
   │  ├─ 普通用户主节点口径
   │  ├─ 在线 / 离线双态
   │  └─ 轮询与快照容错
   ├─ ② 报价卡 / 品种选择
   │  ├─ 品种来源与排序
   │  ├─ 无任何报价
   │  ├─ 当前品种消失 → 自动回退
   │  ├─ 价格格式化
   │  └─ 品种持仓摘要卡
   ├─ ③ 开仓弹窗（OrderDialog）
   │  ├─ 实际可见字段（注意多处已注释隐藏）
   │  ├─ openDialog 前置与表单重置
   │  ├─ 分组与策略加载（loadGroupAndStrategy）
   │  ├─ 策略参数回填
   │  ├─ 方向自动推导（derivedAction）
   │  ├─ 趋势参数校验（validateRiskParams）
   │  ├─ 网格参数校验（validateGridParams）
   │  ├─ 网格试算（自动）
   │  ├─ 网格试算失败降级
   │  ├─ 网格条件字段联动
   │  ├─ 带单开关（carry）
   │  ├─ 触发提交（市价 / 限价）
   │  ├─ 触发提交（网格）
   │  └─ 弹窗关闭与重开
   ├─ ④ 前端提交与结果提示
   │  ├─ 策略复用路径（manual-signal）
   │  ├─ 一键触发路径（quick-trigger）
   │  ├─ 手数与空值兜底
   │  └─ 结果文案分级
   ├─ ⑤ 后端 quick-trigger 校验
   │  ├─ 主节点四道校验
   │  ├─ 限价必填入场价
   │  ├─ 网格八项必填校验
   │  ├─ 策略 / 分组创建与绑定
   │  └─ 趋势方向推导与异常
   ├─ ⑥ 信号处理通用流程
   │  ├─ 解析与校验失败
   │  ├─ 定向字段校验
   │  ├─ 5 秒幂等去重
   │  ├─ 引擎选择
   │  └─ 操作审计留痕
   ├─ ⑦ 分发引擎（group_dispatcher）
   │  ├─ CLOSE 分支
   │  ├─ 候选分组筛选（8 条落选原因）
   │  ├─ 规则启用态准入
   │  ├─ 以损定量准入
   │  ├─ 网格准入
   │  ├─ 无匹配分组 → 整体拒收
   │  ├─ 建任务后的分组级跳过
   │  ├─ 带单与节点定向收窄
   │  ├─ 逐节点下发四态
   │  └─ 零下发汇总
   ├─ ⑧ 底部四个详情 Tab
   │  ├─ 当前持仓
   │  ├─ 资产
   │  ├─ 历史订单
   │  └─ 吸筹策略（运行中网格任务）
   ├─ ⑨ 平仓 / 停止策略
   │  ├─ 单笔持仓平仓
   │  ├─ 平仓的连带影响
   │  └─ 停止吸筹任务
   └─ ⑩ 权限与鉴权
      ├─ 交易菜单权限
      ├─ 普通用户信号模型限制
      ├─ 分组归属收窄
      ├─ 动作与手数约束
      └─ 分组写操作归属
```

## 两条发起通道

弹窗内的「触发」走 `POST /api/console/manual-signal`（model=strategy，带 group_id）；页面旧的一键触发路径走 `POST /api/trading/quick-trigger`（会自建「分组 + 策略」）。两者最终都汇入 `webhook.process_signal`，共用 5 秒幂等去重与 group_dispatcher 分发链路，因此「accepted / duplicate / rejected」三类结果与提示文案完全一致。

## 相关源码位置

- `frontend/src/views/TradingView.vue`（页面主结构、三个策略按钮、详情 Tab、平仓与停止）
- `frontend/src/components/OrderDialog.vue`（弹窗、参数校验、方向推导、网格试算、触发/保存）
- `frontend/src/api/trading.ts`（quick-trigger 客户端）
- `backend/app/trading_api.py`（一键触发接口与全部 400 校验）
- `backend/app/console_api.py`（手动信号、权限收窄、payload 组装）
- `backend/app/webhook.py`（解析、校验、去重、分发选择）
- `backend/app/group_dispatcher.py`（候选分组、逐节点下发、跳过原因）
- `backend/app/group_rules.py`（规则准入与趋势风控文案）
- `backend/app/grid_sizing_api.py`（网格试算接口）
