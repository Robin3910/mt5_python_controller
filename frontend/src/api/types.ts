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
