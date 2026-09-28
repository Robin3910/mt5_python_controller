import type { ManualSignalAction } from '@/api/types'

export interface StrategySignalDraft {
  symbol: string
  action: ManualSignalAction
  volume: number | null
  stop_loss: number | null
  take_profit: number | null
  entry_price: number | null
  comment: string
}

function normalizeSymbolKey(symbol: string): string {
  return (symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
}

/** 与分组分发一致：去符号后互相以前缀匹配（XAUUSD / XAUUSDm）。 */
export function signalSymbolMatches(strategySymbol: string, signalSymbol: string): boolean {
  const a = normalizeSymbolKey(strategySymbol)
  const b = normalizeSymbolKey(signalSymbol)
  if (!a || !b) return false
  return a.startsWith(b) || b.startsWith(a)
}
