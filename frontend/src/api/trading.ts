import httpClient from './client'

export type TradingStrategyType = 'trend_market' | 'trend_limit' | 'grid'

export interface QuickTriggerRequest {
  node_id: string
  strategy_type: TradingStrategyType
  symbol: string
  volume: number
  sl?: number | null
  tp?: number | null
  entry_price?: number | null
  /** 网格模式：all(默认 long) / buy / sell；趋势模式由后端自动判断 */
  direction?: 'all' | 'buy' | 'sell' | null
  remark?: string | null
}

export interface QuickTriggerResult {
  group_id: string
  strategy_id: string
  result: {
    status: 'accepted' | 'rejected' | 'duplicate'
    signal_id?: string
    groups?: number
    targets?: number
    reason?: string
    mode?: string
  }
}

export async function quickTrigger(req: QuickTriggerRequest): Promise<QuickTriggerResult> {
  const { data } = await httpClient.post<QuickTriggerResult>('/api/trading/quick-trigger', req)
  return data
}