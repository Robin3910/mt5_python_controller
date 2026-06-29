// 前后端共享的数据结构定义（与后端 Pydantic 模型一一对应）

export interface NodeOut {
  node_id: string
  name: string
  enabled: boolean
  status: 'online' | 'offline'
  lot_mode: 'global' | 'fixed' | 'signal'
  lot: number | null
  follow_sync: boolean
  follow_poll: boolean
  poll_order: number
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

export interface LotConfig {
  enabled: boolean
  value: number
}

export interface DispatchConfig {
  mode: 'sync' | 'poll'
  position_scope: 'symbol' | 'account'
}

// 全局节点接入令牌（所有节点共享，存于「账户设置」）
export interface NodeTokenInfo {
  token: string
  updated_at: number
}

export interface NodeCreatePayload {
  // 留空时后端会自动生成 "node-{mt5_login}"
  name?: string
  mt5_login: number
  lot_mode?: string
  lot?: number | null
  follow_sync?: boolean
  follow_poll?: boolean
  poll_order?: number
  filters?: Record<string, unknown> | null
}

export interface NodeUpdatePayload {
  name?: string
  enabled?: boolean
  lot_mode?: string
  lot?: number | null
  follow_sync?: boolean
  follow_poll?: boolean
  poll_order?: number
  filters?: Record<string, unknown> | null
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
