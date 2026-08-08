// 趋势面板参数的默认值与展示文案。
// 默认值与后端 trend_indicators.DEFAULTS 一一对应，仅用于「恢复默认」与节点尚无配置
// 时的表单初值；范围夹取与关系修正一律由后端负责，前端不重复实现判定规则。
import type { RsiState, TrendConfig, TrendTimeframe, TrendVerdict } from '@/api/types'

export const TREND_DEFAULTS: TrendConfig = {
  timeframe: 'M15',
  ema_period: 50,
  rsi_period: 14,
  ema_weight: 0.7,
  rsi_weight: 0.3,
  ema_full_scale_pct: 0.2,
  rsi_bull: 60,
  rsi_bear: 40,
  rsi_overbought: 70,
  rsi_oversold: 30,
  bullish: 20,
  bearish: -20,
  bars: 120,
}

export const TREND_TIMEFRAMES: { value: TrendTimeframe; label: string }[] = [
  { value: 'M1', label: '1 分钟' },
  { value: 'M5', label: '5 分钟' },
  { value: 'M15', label: '15 分钟' },
  { value: 'M30', label: '30 分钟' },
  { value: 'H1', label: '1 小时' },
  { value: 'H4', label: '4 小时' },
  { value: 'D1', label: '日线' },
  { value: 'W1', label: '周线' },
  { value: 'MN', label: '月线' },
]

/** 面板轮询间隔选项；0 = 暂停自动刷新 */
export const TREND_REFRESH_OPTIONS: { value: number; label: string }[] = [
  { value: 3000, label: '3 秒' },
  { value: 5000, label: '5 秒' },
  { value: 10000, label: '10 秒' },
  { value: 30000, label: '30 秒' },
  { value: 0, label: '暂停' },
]

const TREND_LABELS: Record<TrendVerdict, string> = {
  bullish: '多头',
  bearish: '空头',
  neutral: '中性',
  unknown: '数据不足',
}

const RSI_STATE_LABELS: Record<RsiState, string> = {
  overbought: '超买',
  oversold: '超卖',
  bullish: '偏多',
  bearish: '偏空',
  neutral: '中性',
  unknown: '—',
}

export function timeframeLabel(timeframe: string): string {
  return TREND_TIMEFRAMES.find((t) => t.value === timeframe)?.label || timeframe
}

export function trendLabel(verdict: TrendVerdict): string {
  return TREND_LABELS[verdict] || verdict
}

export function rsiStateLabel(state: RsiState): string {
  return RSI_STATE_LABELS[state] || state
}

/** 趋势结论对应的 tag 配色（沿用全局 tag 修饰类） */
export function trendTagClass(verdict: TrendVerdict): string {
  if (verdict === 'bullish') return 'green'
  if (verdict === 'bearish') return 'red'
  if (verdict === 'unknown') return ''
  return 'blue'
}

/** 把节点已保存的配置补齐成完整表单值（缺项回落默认） */
export function toTrendForm(raw?: Partial<TrendConfig> | null): TrendConfig {
  return { ...TREND_DEFAULTS, ...(raw || {}) }
}
