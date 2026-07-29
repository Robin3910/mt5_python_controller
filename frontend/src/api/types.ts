// 前后端共享的数据结构定义（与后端 Pydantic 模型一一对应）

export interface NodeOut {
  node_id: string
  name: string
  enabled: boolean
  status: 'online' | 'offline'
  filters?: NodeDispatchFiltersConfig | null
  mt5_login: number | null
  mt5_server: string | null
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
  // 留空时后端会自动生成 "node-{mt5_login}"
  name?: string
  mt5_login: number
  filters?: NodeDispatchFiltersConfig | null
}

export interface NodeUpdatePayload {
  name?: string
  enabled?: boolean
  filters?: NodeDispatchFiltersConfig | null
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
}

// 中控台手动触发的开仓信号（复用 Webhook 分发流程）
export interface ManualSignalPayload {
  symbol: string
  action: 'BUY' | 'SELL'
  volume: number
  /** 处理模型：normal（按币种分发，默认）/ strategy（按分组分发） */
  model?: SignalModel
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
}

export interface GroupCreatePayload {
  name: string
  enabled?: boolean
  dispatch_mode?: GroupDispatchMode
  remark?: string | null
  /** 一对一绑定策略；空 / null = 不绑定 */
  strategy_id?: string | null
  node_ids?: string[]
}

export interface GroupUpdatePayload {
  name?: string
  enabled?: boolean
  dispatch_mode?: GroupDispatchMode
  remark?: string | null
  /** 传入空字符串或 null 表示解除绑定 */
  strategy_id?: string | null
  /** 传入即整体替换成员列表 */
  node_ids?: string[]
}

/** 分批加仓档位：持仓笔数区间内的点数 / 倍数 */
export interface StrategyBatchLevel {
  pos_from: number
  pos_to: number
  /** point=点数 */
  calc_type: string
  point: number
  lot_times: number
  extra_lot: number
}

/** 策略加仓规则：1=逆势加仓，2=顺势加仓（与后端独立模型字段对齐） */
export interface StrategyRule {
  type: number
  /** 0=关闭，1=启用 */
  status: number
  /** 监控方向 all | buy | sell */
  action: string
  point: number
  lot_times: number
  extra_lot: number
  max_allow_num: number
  /** 是否启用分批加仓 */
  batch_enabled?: boolean
  /** 分批监控方向 all | buy | sell */
  batch_action?: string
  /** 分批批数 */
  batch_count?: number
  /** 总手数上限，0=不限制 */
  total_lot_limit?: number
  batch_levels?: StrategyBatchLevel[]
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

/** 主任务在单个节点上的下发与完成情况 */
export interface GroupTaskDispatchRecord {
  id: number
  node_id: string
  node_name: string | null
  decided_vol: number | null
  status: string
  skip_reason: string | null
  retcode: number | null
  order: number | null
  deal: number | null
  price: number | null
  error: string | null
  magic: number | null
  dispatched_at: number | null
  finished_at: number | null
}

/** 分组信号主任务（信号信息 + 下发数据 + 各节点处理情况） */
export interface GroupSignalTaskRecord {
  task_id: number
  magic: number | null
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
  dispatch_mode: GroupDispatchMode
  payload: Record<string, unknown> | null
  node_ids: string[]
  node_count: number
  status: string
  skip_reason: string | null
  finished_at: number | null
  dispatches: GroupTaskDispatchRecord[]
}

export interface PaginatedGroupSignals {
  items: GroupSignalTaskRecord[]
  total: number
  page: number
  page_size: number
}

// 手动触发接口返回（与 /webhook 响应同构，字段视 status 而定）
export interface ManualSignalResult {
  status: string // accepted / duplicate / rejected
  signal_id?: string
  action?: string
  symbol?: string
  volume?: number
  mode?: string
  targets?: number
  reason?: string
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
  /** 信号来源：tradingview（外部 Webhook）/ manual（中控台手动触发）；空按 TradingView 展示 */
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
