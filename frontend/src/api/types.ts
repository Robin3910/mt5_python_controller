// 前后端共享的数据结构定义（与后端 Pydantic 模型一一对应）

export type RiskMonitorMode = 'loop' | 'times'
export type RiskOrderOp = 'any' | 'gt' | 'gte' | 'eq' | 'lte' | 'lt'
export type RiskCloseAction = 'all' | 'buy' | 'sell' | 'hedge'
export type RiskSideAction = 'all' | 'buy' | 'sell'

/** 账户浮盈亏比风控规则 */
export interface FloatPlRatioRule {
  enabled: boolean
  /** 触发比例（%），负数为浮亏侧，正数为浮盈侧 */
  ratio: number
  /** 触达后操作，目前仅清仓全部 */
  action: 'close_all'
  monitor_mode: RiskMonitorMode
  max_times: number
  remaining_times: number
}

/** 账户净值下限风控规则：净值低于 amount（USD）触发清仓 */
export interface EquityMinRule {
  enabled: boolean
  amount: number
  action: 'close_all'
  monitor_mode: RiskMonitorMode
  max_times: number
  remaining_times: number
}

/** 品种盈亏金额 + 订单数条件（可多条） */
export interface SymbolPlOrderItem {
  id: string
  enabled: boolean
  symbol: string
  /** 盈亏金额：正=盈利侧达阈值，负=亏损侧达阈值 */
  pl_amount: number
  order_op: RiskOrderOp
  order_count: number
  close_action: RiskCloseAction
  monitor_mode: RiskMonitorMode
  max_times: number
  remaining_times: number
}

/** 品种浮盈亏保护（可多条）：先触达触发金额进入保护，再收窄到目标金额平仓 */
export interface SymbolPlProtectItem {
  id: string
  enabled: boolean
  symbol: string
  trigger_amount: number
  narrow_amount: number
  monitor_mode: RiskMonitorMode
  max_times: number
  remaining_times: number
}

export interface LotPlTier {
  min_lot: number
  pl_amount: number
}

/** 按持仓手数分档 + 盈亏金额平仓 */
export interface LotPlTiersRule {
  enabled: boolean
  batch_count: number
  close_action: RiskSideAction
  tiers: LotPlTier[]
}

/** 模版1 信号级浮盈亏比（配置不含运行时 remaining_times） */
export interface SignalFloatPlRatio {
  enabled: boolean
  /** 触发比例（%），负数为浮亏侧，正数为浮盈侧。分子=该信号浮盈亏，分母=账户余额 */
  ratio: number
  action: 'close_all'
  monitor_mode: RiskMonitorMode
  max_times: number
}

/** 模版1 信号级分档手数盈亏 */
export type SignalLotPlTiers = LotPlTiersRule

/** 节点账户级风控配置 */
export interface NodeRiskConfig {
  float_pl_ratio: FloatPlRatioRule
  equity_min: EquityMinRule
  symbol_pl_orders: { items: SymbolPlOrderItem[] }
  symbol_pl_protect: { items: SymbolPlProtectItem[] }
  lot_pl_tiers: LotPlTiersRule
}

/** 节点风控执行回报（实时 WS） */
export interface RiskFeedItem {
  ts: number
  rule?: string
  event?: string
  message: string
  success: boolean
  remaining_times?: number
  disabled?: boolean
  current_ratio?: number
  ratio_threshold?: number
}

/** 趋势面板可选 K 线周期（与节点侧 MT5 周期一一对应） */
export type TrendTimeframe = 'M1' | 'M5' | 'M15' | 'M30' | 'H1' | 'H4' | 'D1' | 'W1' | 'MN'

/**
 * 趋势面板参数（权威存库，见后端 trend_indicators.normalize_config）。
 * 只影响趋势得分的展示口径，不参与任何下单与过滤决策。
 */
export interface TrendConfig {
  timeframe: TrendTimeframe
  ema_period: number
  rsi_period: number
  ema_weight: number
  rsi_weight: number
  /** 价格偏离 EMA 达到该百分比即视为方向满分 */
  ema_full_scale_pct: number
  rsi_bull: number
  rsi_bear: number
  rsi_overbought: number
  rsi_oversold: number
  bullish: number
  bearish: number
  /** 面板展示的 K 线根数（计算用的根数由后端按周期自行放大） */
  bars: number
}

export type TrendVerdict = 'bullish' | 'bearish' | 'neutral' | 'unknown'
export type RsiState = TrendVerdict | 'overbought' | 'oversold'

export interface TrendBar {
  time: number
  open: number
  high: number
  low: number
  close: number
}

export interface TrendEmaDetail {
  period: number
  value: number | null
  /** 加权前的原始分（-100 ~ +100） */
  raw: number
  /** 计入权重后的得分 */
  score: number
  /** 该指标的最高贡献（= 权重 × 100） */
  max_score?: number
  deviation_pct: number
  position: 'above' | 'below' | 'equal' | 'unknown'
}

export interface TrendRsiDetail {
  period: number
  value: number | null
  raw: number
  score: number
  max_score?: number
  state: RsiState
}

/** GET /api/nodes/{id}/trend 的响应 */
export interface TrendPanelData {
  node_id: string
  symbol: string
  config: TrendConfig
  /** false 表示 K 线不足、算不出指标（区别于「中性」） */
  ready: boolean
  score: number
  trend: TrendVerdict
  price: number
  /** quote：用的是实时报价；close：报价读不到，回落到最后一根收盘价 */
  price_source: 'quote' | 'close'
  last_close: number
  ema: TrendEmaDetail
  rsi: TrendRsiDetail
  bars: TrendBar[]
  ema_series: Array<number | null>
  rsi_series: Array<number | null>
  bar_time: number
  bar_count: number
  quote?: Partial<QuoteInfo>
  /** true 表示命中后端行情缓存，未重新惊动节点终端 */
  cached: boolean
  fetched_at: number
  updated_at: number
}

export interface NodeOut {
  node_id: string
  name: string
  enabled: boolean
  status: 'online' | 'offline'
  filters?: NodeDispatchFiltersConfig | null
  risk?: NodeRiskConfig | null
  mt5_login: number | null
  mt5_server: string | null
  /** 节点鉴权时上报的客户端版本；旧版本客户端不上报，为空即未知 */
  client_version: string | null
  client_version_at: number | null
  created_at: number
  last_seen: number | null
}

export interface Position {
  ticket: number
  symbol: string
  type: string
  volume: number
  price_open: number
  price_current: number
  sl: number
  tp: number
  profit: number
  magic: number
  comment: string
  time: number
}

export interface QuoteInfo {
  bid: number
  ask: number
  mid: number
  change: number
}

export interface AccountSnapshot {
  node_id?: string
  login?: number | null
  server?: string | null
  balance: number
  equity: number
  margin: number
  free_margin: number
  leverage: number
  positions: Position[]
  prices: Record<string, number>
  quotes?: Record<string, QuoteInfo>
  updated_at: number
  /** 券商 MT5 服务器相对真实 UTC 的偏移（秒）。用于把后台时间显示成终端订单时间。 */
  server_time_offset?: number | null
}

/** 区间方向过滤：单条价格区间允许的开仓方向 */
export type FilterDirection = 'BUY' | 'SELL'

/** 价格不在任何区间时的默认处理 */
export type DefaultFilterAction = 'block' | 'pass'

export interface FilterInterval {
  low: number
  high: number
  allow: FilterDirection[]
}

/** 单个品种的区间过滤与分发规则（键为品种代码，如 XAUUSD，全局 filters） */
export interface SymbolFilterRule {
  /** false：拒收该品种全部信号（含 Webhook 平仓；后台手动平仓除外） */
  enabled: boolean
  /** 是否允许接收做多 (BUY) 信号，默认 true */
  allow_buy: boolean
  /** 是否允许接收做空 (SELL) 信号，默认 true */
  allow_sell: boolean
  /** 该币种信号的分发模式，默认 sync */
  dispatch_mode: 'sync' | 'poll'
  /** 持仓判定范围，默认 symbol */
  position_scope: 'symbol' | 'account'
  default_action: DefaultFilterAction
  /** 是否启用该品种的全局手数（节点手数策略为「跟随中控台」时生效） */
  lot_enabled: boolean
  /** 该品种全局手数 */
  lot: number
  intervals: FilterInterval[]
}

export type FilterRulesConfig = Record<string, SymbolFilterRule>

/** 节点 filters：按币种配置分发参与、手数策略与轮询顺序 */
export interface NodeSymbolDispatchRule {
  follow_sync: boolean
  follow_poll: boolean
  lot_mode: 'global' | 'fixed' | 'signal'
  lot: number | null
  poll_order: number
}

export type NodeDispatchFiltersConfig = Record<string, NodeSymbolDispatchRule>

// 全局节点接入令牌（所有节点共享，存于「账户设置」）
export interface NodeTokenInfo {
  token: string
  updated_at: number
}

export interface NodeCreatePayload {
  // 留空时后端会自动生成 "{序号}-{mt5_login}"（序号从 1 起按节点位置递增）
  name?: string
  mt5_login: number
  filters?: NodeDispatchFiltersConfig | null
}

export interface NodeUpdatePayload {
  name?: string
  enabled?: boolean
  filters?: NodeDispatchFiltersConfig | null
  risk?: NodeRiskConfig | null
}

export interface CloseRequest {
  target: 'all' | 'symbol' | 'ticket'
  symbol?: string | null
  ticket?: number | null
}

export interface CloseBatchResult {
  status: string
  sent: string[]
  failed: Array<{ node_id: string; reason: string }>
  target: string
  strategies_total?: number
}

/** 手动触发的信号方向；CLOSE 仅 strategy 模型开放（终止分组内进行中的策略任务） */
export type ManualSignalAction = 'BUY' | 'SELL' | 'CLOSE'

// 后台手动触发的信号（复用 Webhook 分发流程）
export interface ManualSignalPayload {
  symbol: string
  action: ManualSignalAction
  /** 开仓（BUY / SELL）必填；CLOSE 不需要手数 */
  volume?: number
  /** 处理模型：normal（按币种分发，默认）/ strategy（按分组分发） */
  model?: SignalModel
  stop_loss?: number
  take_profit?: number
  /** 限价开仓的挂单价（对应 Webhook 的 limit_price）；只有配成限价的模版2 会用它 */
  entry_price?: number
  comment?: string
  /** 策略模版定向（仅 strategy 模型）：只发给绑定了这些模版的分组；省略或空数组 = 不限制 */
  template_ids?: string[]
  /** 分组定向（仅 strategy 模型）：只发给 ID 在列表内的分组；省略或空数组 = 不限制 */
  group_ids?: string[]
}

/** Webhook model 字段：normal = 按币种分发（默认），strategy = 按分组分发 */
export type SignalModel = 'normal' | 'strategy'

/** 分组分发模式：与中控台一致的两种模式，但作用于整个分组而非单个币种 */
export type GroupDispatchMode = 'sync' | 'poll'

/** 分组成员节点（含在线状态） */
export interface GroupNodeRef {
  node_id: string
  name: string | null
  mt5_login: number | null
  enabled: boolean
  status: 'online' | 'offline'
  sort_order: number
}

export interface GroupOut {
  group_id: string
  name: string
  enabled: boolean
  dispatch_mode: GroupDispatchMode
  /** 趋势风控：开仓前按全局趋势参数对各节点算信号品种趋势（默认关） */
  trend_risk_enabled: boolean
  /** 限价挂单监听：把节点 MT5 上手动挂的带关键字限价单转成 strategy 信号（默认关） */
  limit_watch_enabled: boolean
  /** 订单注释包含该关键字即视为触发单；空字符串表示不限注释；未填时新建默认 limit */
  limit_watch_keyword: string
  /** 一对一绑定的策略 ID；未绑定为 null */
  strategy_id?: string | null
  /** 绑定策略名称（展示用） */
  strategy_name?: string | null
  remark: string | null
  created_at: number
  nodes: GroupNodeRef[]
  node_count: number
  /** 有效节点数（已启用 + 在线） */
  online_node_count: number
  /** 该分组已处理的信号主任务数 */
  signal_count: number
  /** 进行中主任务数（pending/dispatching/running） */
  active_task_count: number
  /** 已开限价监听时附带的最近日志（新→旧）；未开启为空 */
  limit_watch_logs?: LimitWatchLogOut[]
}

export type LimitWatchLogEvent =
  | 'rejected'
  | 'cancel_failed'
  | 'ok'
  | 'dispatch_rejected'
  | 'duplicate'

/** 分组限价监听日志（落库 + 后台 WS 实时推送） */
export interface LimitWatchLogOut {
  id: number
  ts: number | null
  group_id: string
  group_name?: string | null
  node_id: string
  ticket: number
  symbol?: string | null
  action?: string | null
  volume?: number | null
  price?: number | null
  sl?: number | null
  tp?: number | null
  comment?: string | null
  event: LimitWatchLogEvent | string
  message: string
  detail?: Record<string, unknown> | null
}

export interface PaginatedLimitWatchLogs {
  items: LimitWatchLogOut[]
  total: number
  page: number
  page_size: number
}

/** 分组一键平仓结果：终止该分组未收口子任务并按魔术号平仓 */
export interface GroupCloseResult {
  group_id: string
  group_name?: string | null
  dispatch_mode?: string
  task_id?: number | null
  /** 已向在线节点下发 strategy_stop 的子任务数 */
  targets: number
  /** 离线强制收口并排队待重连补发的子任务数 */
  forced?: number
  /** closing=已下发；skipped=无任务；failed=目标均离线 */
  status: string
  reason?: string | null
}

export interface GroupCreatePayload {
  name: string
  enabled?: boolean
  dispatch_mode?: GroupDispatchMode
  trend_risk_enabled?: boolean
  limit_watch_enabled?: boolean
  limit_watch_keyword?: string
  remark?: string | null
  /** 一对一绑定策略；空 / null = 不绑定 */
  strategy_id?: string | null
  node_ids?: string[]
}

export interface GroupUpdatePayload {
  name?: string
  enabled?: boolean
  dispatch_mode?: GroupDispatchMode
  trend_risk_enabled?: boolean
  limit_watch_enabled?: boolean
  limit_watch_keyword?: string
  remark?: string | null
  /** 传入空字符串或 null 表示解除绑定 */
  strategy_id?: string | null
  /** 传入即整体替换成员列表 */
  node_ids?: string[]
}

/** 分批档位的加仓间距计算方式 */
export type BatchCalcType = 'point' | 'price' | 'atr' | 'range'

/** ATR / 波幅可选的 K 线周期 */
export type BatchTimeframe = 'M1' | 'M5' | 'M15' | 'M30' | 'H1' | 'H4' | 'D1' | 'W1' | 'MN'

/** 分批加仓档位：持仓笔数区间内的加仓间距 / 倍数 */
export interface StrategyBatchLevel {
  pos_from: number
  pos_to: number
  /** 间距计算方式：point=点数 / price=指定价位 / atr=ATR / range=K线波幅 */
  calc_type: BatchCalcType
  /** calc_type=point 时的触发点数 */
  point: number
  /** calc_type=price 时的绝对价位 */
  price: number
  /** calc_type=atr / range 时统计用的 K 线周期 */
  timeframe: BatchTimeframe
  lot_times: number
  extra_lot: number
}

/** 以损定量的补仓方向（历史字段，模版2 已改为分散仓市价） */
export type EntryDirection = 'pullback' | 'breakout'

/**
 * 以损定量的开仓方式：market 信号一到市价打齐 / limit 在信号入场价挂限价等成交。
 * 限价模式要求信号携带入场价（limit_price / price）。
 */
export type EntryMode = 'market' | 'limit'

/** 网格模式：arithmetic 等差 / geometric 等比 */
export type GridMode = 'arithmetic' | 'geometric'

/** 网格方向：long 只做多 / short 只做空 */
export type GridSide = 'long' | 'short'

/**
 * 策略规则，字段按 type 分组使用（与后端独立模型对齐）：
 * type=1 逆势加仓 / type=2 顺势加仓（模版1）；type=3 以损定量趋势单（模版2）；
 * type=4 网格交易（模版3）。
 * 后端会按 type 只保留该类型的字段，因此另一组字段可以留空。
 */
export interface StrategyRule {
  type: number
  /** 0=关闭，1=启用 */
  status: number
  /** 监控方向 all | buy | sell */
  action: string
  // --- type=1 / 2：加仓类 ---
  point?: number
  lot_times?: number
  extra_lot?: number
  max_allow_num?: number
  /** 是否启用分批加仓 */
  batch_enabled?: boolean
  /** 分批监控方向 all | buy | sell */
  batch_action?: string
  /** 分批批数 */
  batch_count?: number
  /** 总手数上限，0=不限制 */
  total_lot_limit?: number
  batch_levels?: StrategyBatchLevel[]
  /** 信号级盈亏比（模版1 顺势/逆势写入相同值） */
  float_pl_ratio?: SignalFloatPlRatio
  /** 信号级分档手数盈亏（模版1 顺势/逆势写入相同值） */
  lot_pl_tiers?: SignalLotPlTiers
  // --- type=3：以损定量趋势单 ---
  /** 风险金额（账户货币） */
  risk_amount?: number
  /** 盈亏比：止盈距离 = 止损距离 × 该值；分散仓按等分阶梯挂，末档才吃满 */
  rr_ratio?: number
  /** 底仓占总手数的百分比（底仓止盈为 0） */
  base_ratio?: number
  /** 分散仓单数，0=底仓即全仓 */
  add_batches?: number
  /** 总手数上限，0=只受单笔上限约束 */
  max_total_lot?: number
  /** 是否启用保本触发 */
  breakeven_enabled?: boolean
  /** 浮盈达到止损距离 × 该倍数时把止损移到保本 */
  breakeven_times?: number
  /** 保本监控：once=按次 / loop=循环 */
  breakeven_mode?: 'once' | 'loop'
  /**
   * 开仓方式。limit 下底仓与分散仓全部挂在信号入场价（同一点位），止损距离与
   * 手数口径与市价相同；挂单一直等到成交（GTC）。
   */
  entry_mode?: EntryMode
  // --- type=4：网格交易 ---
  /** 网格区间下限 */
  price_lower?: number
  /** 网格区间上限 */
  price_upper?: number
  /** 网格数量（2-200） */
  grid_count?: number
  grid_mode?: GridMode
  grid_side?: GridSide
  /** 每格手数 */
  lot_per_grid?: number
  /** 触发价，0=立即启动 */
  trigger_price?: number
  /** 下沿终止价（须低于区间下限），0=不设；多头为止损、空头为止盈 */
  stop_lower?: number
  /** 上沿终止价（须高于区间上限），0=不设；多头为止盈、空头为止损 */
  stop_upper?: number
  /** 终止时是否清仓 */
  close_on_stop?: boolean
  /** 是否按现价上方格位初始建仓 */
  prefill_enabled?: boolean
  /** 向上追踪：价格越过区间外沿时整个网格连同止损止盈平移一格 */
  trailing_up?: boolean
  /** 最大平移格数，0=不限 */
  trailing_max?: number
  // --- type=4 的配置期试算助手：只记录建议值怎么算出来的，执行层不读 ---
  /** 是否启用试算，默认关闭 */
  assist_enabled?: boolean
  /** 试算 ATR 所用的 K 线周期 */
  assist_timeframe?: BatchTimeframe
  /** 格距 = ATR × 该倍数 */
  assist_atr_mult?: number
  /** 手改后的格距，0=沿用 ATR × 倍数的结果 */
  assist_spacing?: number
  /** 试算用的最大可接受亏损（账户货币） */
  assist_max_loss?: number
}

/** 试算提示：warn=需注意，info=仅说明口径 */
export interface GridSizingWarning {
  code: string
  level: 'warn' | 'info'
  message: string
}

/** GET /api/nodes/{id}/grid_sizing 的响应：网格数量与每格手数的建议值 */
export interface GridSizingData {
  node_id: string
  symbol: string
  timeframe: BatchTimeframe
  quote: Record<string, number>
  cached: boolean
  fetched_at: number
  updated_at: number
  /** false 表示连网格数量都算不出（区间非法 / K 线不足），原因看 warnings */
  ready: boolean
  atr: number
  spacing: number
  /** 格距来源：atr=按倍数算 / manual=手改覆盖 */
  spacing_source: 'atr' | 'manual'
  grid_count: number
  /** 0 表示手数算不出（缺合约规格 / 缺止损 / 低于最小手数），原因看 warnings */
  lot_per_grid: number
  /** 满仓打止损的估算亏损 */
  worst_loss: number
  /** 满仓总手数 */
  worst_lot: number
  levels: number[]
  warnings: GridSizingWarning[]
}

export interface StrategyTemplateOut {
  template_id: string
  name: string
  description: string
  rules: StrategyRule[]
}

export interface StrategyOut {
  strategy_id: string
  name: string
  template_id: string
  template_name: string
  symbol: string
  enabled: boolean
  rules: StrategyRule[]
  remark: string | null
  created_at: number
}

export interface StrategyCreatePayload {
  template_id: string
  name: string
  symbol: string
  enabled?: boolean
  remark?: string | null
  /** 自定义规则；不传则使用模版默认值 */
  rules?: StrategyRule[]
}

export interface StrategyUpdatePayload {
  name?: string
  symbol?: string
  enabled?: boolean
  remark?: string | null
  rules?: StrategyRule[]
}

/** 节点信号任务：策略执行的实际单元，自持魔术号 */
/** 状态流转：pending/sent/opened/running/closing/done/failed/skipped/offline */
export interface GroupTaskDispatchRecord {
  id: number
  node_id: string
  node_name: string | null
  symbol: string | null
  decided_vol: number | null
  status: string
  skip_reason: string | null
  retcode: number | null
  order: number | null
  deal: number | null
  price: number | null
  error: string | null
  magic: number | null
  /** 当前该魔术号的持仓笔数 */
  position_count: number
  /** 当前该魔术号的未成交挂单笔数；限价开仓在成交前只有它 */
  pending_orders: number
  /** 已加仓次数 */
  add_count: number
  total_orders: number
  total_volume: number
  realized_profit: number
  finish_reason: string | null
  dispatched_at: number | null
  opened_at: number | null
  last_report_at: number | null
  finished_at: number | null
}

/** 分组信号主任务：分发记录；魔术号在各节点子任务上，主任务不持有 */
export interface GroupSignalTaskRecord {
  task_id: number
  signal_id: string
  group_id: string
  group_name: string | null
  created_at: number | null
  action: string | null
  symbol: string | null
  volume: number | null
  sl: number | null
  tp: number | null
  comment: string | null
  source_ip: string | null
  raw_payload: string | null
  /** 触发时绑定的策略 */
  strategy_id: string | null
  strategy_name: string | null
  dispatch_mode: GroupDispatchMode
  payload: Record<string, unknown> | null
  node_ids: string[]
  node_count: number
  /** pending/dispatching/running/done/partial/failed/skipped */
  status: string
  skip_reason: string | null
  /** 策略托管汇总（各子任务累加） */
  total_orders: number
  total_volume: number
  realized_profit: number
  opened_at: number | null
  finished_at: number | null
  dispatches: GroupTaskDispatchRecord[]
}

export interface PaginatedGroupSignals {
  items: GroupSignalTaskRecord[]
  total: number
  page: number
  page_size: number
}

/** 节点策略子任务执行事件（开仓 / 加仓 / 平仓等关联订单） */
export interface GroupTaskEventRecord {
  id: number
  task_id: number
  node_id: string
  magic: number | null
  created_at: number | null
  /**
   * open / add_counter / add_trend / grid_add / grid_shift /
   * close_partial / close_all / error / resume
   */
  event_type: string
  symbol: string | null
  action: string | null
  volume: number | null
  price: number | null
  order_ticket: number | null
  position_count: number | null
  total_volume: number | null
  profit: number | null
  /** 开单原因：人读的一句话说明 */
  message: string | null
  /** 计算依据明细：偏离点数、阈值、手数公式等逐项参数 */
  detail: GroupTaskEventDetail | null
}

/** 事件的计算依据明细。字段随 kind 不同 */
export interface GroupTaskEventDetail {
  kind?:
    | 'open'
    | 'add'
    | 'risk_sized_plan'
    | 'risk_sized_add'
    | 'risk_sized_distribute'
    | 'risk_sized_reject'
    | 'risk_sized_limit_filled'
    | 'breakeven'
    | 'grid_plan'
    | 'grid_fill'
    | 'grid_close'
    | 'grid_shift'
    | 'account_risk'
    | 'close_reason'
  // —— 加仓（kind=add）——
  rule_type?: number
  rule_type_label?: string
  rule_index?: number
  level_index?: number | null
  batch?: boolean
  direction?: string
  base_price?: number
  price?: number
  point?: number
  deviation?: number
  threshold?: number
  base_volume?: number
  lot_times?: number
  extra_lot?: number
  volume?: number
  volume_formula?: string
  position_count?: number
  add_count?: number
  next_position_no?: number
  limit_kind?: string
  limit_value?: number
  error?: string
  // —— 首单（kind=open）——
  signal_id?: string
  task_id?: number
  magic?: number
  symbol?: string
  action?: string
  stop_loss?: number | null
  take_profit?: number | null
  signal_comment?: string | null
  strategy_id?: string
  strategy_name?: string
  template_id?: string
  rule_count?: number
  enabled_rule_count?: number
  // —— 以损定量 / 保本 / 网格（共用扩展字段）——
  risk_amount?: number
  risk_used?: number
  rr_ratio?: number
  sl_distance?: number
  sl_points?: number
  loss_per_lot?: number
  /** 按风险金额反推的总手数；等分后实下可能略少 */
  planned_lot?: number
  total_lot?: number
  /** 分散仓等分除不尽、放弃不开的手数 */
  dropped_lot?: number | null
  lot_formula?: string
  base_ratio?: number
  distribute_volume?: number
  add_batches?: number
  order_count?: number
  /** 止损触发侧报价（多单 bid / 空单 ask），止损距离以此为准 */
  risk_price?: number
  spread?: number | null
  /** 开仓方式：market 市价打齐 / limit 挂限价等成交 */
  entry_mode?: EntryMode
  entry_mode_label?: string
  /** 限价模式下相邻两档挂单价的间隔；同价挂单后恒为空 */
  ladder_step?: number | null
  /** 该档的挂单价（限价模式的分散仓事件） */
  limit_price?: number | null
  /** 限价单成交后的等待时长（秒） */
  waited_seconds?: number
  /** 阶梯止盈相邻两档的间隔 */
  tp_step?: number | null
  /** 阶梯最远一档（吃满盈亏比） */
  tp_full?: number | null
  entry_direction?: string
  entry_direction_label?: string
  gap_points?: number
  gap_capped?: boolean
  breakeven_enabled?: boolean
  breakeven_times?: number
  breakeven_mode?: string
  breakeven_mode_label?: string
  batch_index?: number
  batch_total?: number
  trigger_price?: number
  entry_price?: number
  avg_price?: number
  favorable?: number
  reason?: string
  // —— 被动平仓原因（kind=close_reason）——
  message?: string
  sl?: number
  tp?: number
  so?: number
  manual?: number
  expert?: number
  other?: number
  total?: number
  // —— 账户风控平仓（kind=account_risk）——
  rule?: string
  rule_label?: string
  close_action?: string
  close_action_label?: string
  message_core?: string
  monitor_mode?: string
  item_id?: string
  ratio_threshold?: number
  current_ratio?: number
  floating_pl?: number
  balance?: number
  equity?: number
  amount_threshold?: number
  pl_amount?: number
  current_pl?: number
  close_count?: number
  trigger_amount?: number
  narrow_amount?: number
  tier_index?: number
  min_lot?: number
  current_lot?: number
  // —— 网格 ——
  side?: string
  grid_side?: string
  grid_side_label?: string
  grid_mode?: string
  grid_mode_label?: string
  price_lower?: number
  price_upper?: number
  grid_count?: number
  lot_per_grid?: number
  total_lot_limit?: number
  stop_lower?: number
  stop_upper?: number
  close_on_stop?: boolean
  prefill_enabled?: boolean
  trailing_up?: boolean
  trailing_max?: number | null
  levels?: number[]
  level_price?: number
  exit_price?: number
  holding_count?: number
  prefill_levels?: number[]
  waiting_trigger?: boolean
  ticket?: number | null
  // —— 网格平移（kind=grid_shift）——
  /** 平移格数：正=上移 / 负=下移 */
  steps?: number
  /** 累计平移格数 */
  shift_count?: number
  /** 平移前的区间 */
  from_lower?: number
  from_upper?: number
  /** 被挤出网格、已兑现的格位 */
  dropped_levels?: number[]
  /** 该持仓的格位已被平移挤出网格 */
  out_of_grid?: boolean
}

// 手动触发接口返回（与 /webhook 响应同构，字段视 status 而定）
export interface ManualSignalResult {
  status: string // accepted / duplicate / rejected
  signal_id?: string
  action?: string
  symbol?: string
  volume?: number
  /** 处理模型：normal / strategy */
  model?: SignalModel
  /** normal：close / poll / sync；strategy：group / group_close / rejected */
  mode?: string
  /** strategy 链路命中的分组数 */
  groups?: number
  targets?: number
  reason?: string
  /** strategy 链路各分组的处理结果 */
  tasks?: ManualSignalGroupTask[]
}

/** 清空交易记录结果 */
export interface PurgeTradeLogsResult {
  deleted: Record<string, number>
  redis_cleared: number
  total_deleted: number
  /** 清空前已下发终止指令的策略子任务数 */
  strategies_stopped: number
  /** 因节点离线未能下发终止指令的子任务数（其 MT5 持仓需人工确认） */
  strategies_unreachable: number
}

/** strategy 链路中单个分组的下发结果 */
export interface ManualSignalGroupTask {
  group_id: string
  group_name?: string | null
  dispatch_mode?: GroupDispatchMode
  task_id?: number | null
  targets: number
  status: string
  reason?: string | null
}

export interface HubEvent {
  ts: number
  text: string
  kind: 'info' | 'ok' | 'warn'
}

// 单节点的「信号 + 本节点处理」明细（来自 GET /api/nodes/{id}/dispatches）
export interface NodeDispatchRecord {
  id: number // 分发明细行唯一 ID（同一 signal_id 可能有多条）
  signal_id: string
  // —— 信号原始数据 ——
  symbol: string | null
  action: string | null
  volume: number | null
  sl: number | null
  tp: number | null
  comment: string | null
  source_ip: string | null
  parsed_ok: boolean | null
  dispatch_mode: string | null
  signal_status: string | null
  received_at: number | null
  raw_payload: string | null
  // —— 本节点处理情况 ——
  decided_vol: number | null
  gate_result: string
  skip_reason: string | null
  status: string
  retcode: number | null
  order: number | null
  deal: number | null
  price: number | null
  error: string | null
  dispatched_at: number | null
  finished_at: number | null
}

export interface PaginatedNodeDispatches {
  items: NodeDispatchRecord[]
  total: number
  page: number
  page_size: number
}

export interface SignalEventDispatch {
  id: number
  node_id: string
  node_name: string | null
  decided_vol: number | null
  gate_result: string
  skip_reason: string | null
  status: string
  retcode: number | null
  order: number | null
  deal: number | null
  price: number | null
  error: string | null
  dispatched_at: number | null
  finished_at: number | null
}

export interface SignalEventRecord {
  signal_id: string
  received_at: number | null
  source_ip: string | null
  raw_payload: string | null
  action: string | null
  symbol: string | null
  volume: number | null
  sl: number | null
  tp: number | null
  comment: string | null
  parsed_ok: boolean
  dispatch_mode: string | null
  status: string
  /** 信号来源：tradingview / manual / limit_watch；空按 TradingView 展示 */
  source: string | null
  /** 处理模型：normal（按币种分发）/ strategy（按分组分发）；空按 normal 展示 */
  model: string | null
  dispatches: SignalEventDispatch[]
}

export interface PaginatedSignalEvents {
  items: SignalEventRecord[]
  total: number
  page: number
  page_size: number
}

export interface AuditRecord {
  id: number
  ts: number | null
  operator: string
  action: string
  target: string | null
  params: Record<string, unknown> | null
  result: string
  ip: string | null
  category: string | null
  before: unknown
  after: unknown
}

export interface PaginatedAudits {
  items: AuditRecord[]
  total: number
  page: number
  page_size: number
}

export interface NodeFeedItem {
  signal_id: string
  ts: number
  symbol?: string
  action?: string
  status: string
  volume?: number | null
  sl?: number | null
  tp?: number | null
  price?: number | null
  raw_payload?: string | null
  order?: number | null
  error?: string
  reason?: string
  detail?: string
}

// ----------------------------- 客户端版本管理 -----------------------------

/** 客户端安装包版本条目 */
export interface ClientVersionOut {
  version: string
  filename: string
  size: number
  sha256: string
  notes: string | null
  uploaded_by: string
  created_at: number
  /** 是否为当前发布版本 */
  is_current: boolean
  /** 已上报运行该版本的节点数 */
  node_count: number
}

/** 当前发布指针；previous 是服务端回滚的落点 */
export interface ClientReleaseOut {
  version: string
  previous: string
  updated_at: number
}

export interface ClientVersionListOut {
  items: ClientVersionOut[]
  release: ClientReleaseOut
  /** 未上报版本的节点数（旧客户端或从未上线） */
  unknown_node_count: number
}
