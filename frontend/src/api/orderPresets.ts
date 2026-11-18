/**
 * 开仓参数预设 API（我的下单默认值）。
 *
 * 语义：在【我的 → 策略配置】里预设好固定参数，之后每次去【交易】页真实开仓时，
 * 弹窗自动把这些参数填好；用户只填随行情变化的实时价格。
 *
 * **手数不在这里** —— 手数由节点按「风险金额 ÷ 止损距离」反推，或由网格试算
 * 按「最大亏损预算」反推，用户不填手数。
 */
import api from './client'
import type { OrderPreset, OrderPresetStrategyType } from './types'

/** PUT 请求体：字段级「传什么改什么」，不传的字段保持原值 */
export interface OrderPresetUpdate {
  symbol: string
  strategy_type: OrderPresetStrategyType
  risk_amount?: number
  rr_ratio?: number
  base_ratio?: number
  breakeven_times?: number
  grid_mode?: 'arithmetic' | 'geometric'
  max_allow_loss?: number
  trailing_up?: boolean
}

/** 列出我的全部预设 */
export const fetchOrderPresets = async (): Promise<OrderPreset[]> => {
  const { data } = await api.get<{ presets: OrderPreset[] }>('/api/me/order-presets')
  return data.presets
}

/** 保存一条预设（upsert） */
export const saveOrderPreset = async (payload: OrderPresetUpdate): Promise<OrderPreset> => {
  const { data } = await api.put<{ preset: OrderPreset }>('/api/me/order-presets', payload)
  return data.preset
}

/** 删除一条预设 */
export const deleteOrderPreset = async (
  symbol: string,
  strategyType: OrderPresetStrategyType,
): Promise<boolean> => {
  const { data } = await api.delete<{ ok: boolean }>(
    `/api/me/order-presets/${encodeURIComponent(symbol)}/${encodeURIComponent(strategyType)}`,
  )
  return data.ok
}

/**
 * 解析出一条预设：精确品种优先，其次 `'*'` 通用模板。
 *
 * 与后端 `order_presets.resolve_preset` 同口径 —— 前端先本地找，找不到再让
 * 后端解析（后端会做一次种子回填，前端拿到的列表可能比内存里的新）。
 */
export function resolvePreset(
  presets: OrderPreset[],
  symbol: string,
  strategyType: OrderPresetStrategyType,
): OrderPreset | null {
  const sym = (symbol || '').trim().toUpperCase()
  if (!sym) return null
  const exact = presets.find((p) => p.symbol === sym && p.strategy_type === strategyType)
  if (exact) return exact
  return presets.find((p) => p.symbol === '*' && p.strategy_type === strategyType) ?? null
}
