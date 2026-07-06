import type {
  DefaultFilterAction,
  FilterDirection,
  FilterInterval,
  FilterRulesConfig,
  NodeDispatchFiltersConfig,
  NodeSymbolDispatchRule,
  SymbolFilterRule,
} from '@/api/types'

export function createEmptyInterval(): FilterInterval {
  return { low: 0, high: 0, allow: ['BUY'] }
}

export function createEmptySymbolRule(): SymbolFilterRule {
  return {
    enabled: true,
    allow_buy: true,
    allow_sell: true,
    dispatch_mode: 'sync',
    position_scope: 'symbol',
    default_action: 'block',
    intervals: [],
  }
}

export function createEmptyNodeSymbolRule(): NodeSymbolDispatchRule {
  return {
    follow_sync: true,
    follow_poll: true,
    lot_mode: 'fixed',
    lot: 0.01,
    poll_order: 0,
  }
}

export function exampleFilterRules(): FilterRulesConfig {
  return {
    XAUUSD: {
      enabled: true,
      allow_buy: true,
      allow_sell: true,
      dispatch_mode: 'sync',
      position_scope: 'symbol',
      default_action: 'block',
      intervals: [
        { low: 2300, high: 2350, allow: ['BUY'] },
        { low: 2350, high: 2400, allow: ['SELL'] },
      ],
    },
  }
}

function normalizeDirection(raw: unknown): FilterDirection | null {
  const v = String(raw ?? '').toUpperCase()
  if (v === 'BUY' || v === 'SELL') return v
  return null
}

function normalizeInterval(raw: unknown): FilterInterval | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const low = Number(o.low)
  const high = Number(o.high)
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null
  const allowRaw = Array.isArray(o.allow) ? o.allow : []
  const allow = allowRaw.map(normalizeDirection).filter((d): d is FilterDirection => d !== null)
  return { low, high, allow }
}

function normalizeDispatchMode(raw: unknown): 'sync' | 'poll' {
  return raw === 'poll' ? 'poll' : 'sync'
}

function normalizePositionScope(raw: unknown): 'symbol' | 'account' {
  return raw === 'account' ? 'account' : 'symbol'
}

function normalizeSymbolRule(raw: unknown): SymbolFilterRule {
  if (!raw || typeof raw !== 'object') return createEmptySymbolRule()
  const o = raw as Record<string, unknown>
  const defaultAction: DefaultFilterAction = o.default_action === 'pass' ? 'pass' : 'block'
  const intervalsRaw = Array.isArray(o.intervals) ? o.intervals : []
  const intervals = intervalsRaw
    .map(normalizeInterval)
    .filter((iv): iv is FilterInterval => iv !== null)
  return {
    enabled: o.enabled !== false,
    allow_buy: o.allow_buy !== false,
    allow_sell: o.allow_sell !== false,
    dispatch_mode: normalizeDispatchMode(o.dispatch_mode),
    position_scope: normalizePositionScope(o.position_scope),
    default_action: defaultAction,
    intervals,
  }
}

function normalizeLotMode(raw: unknown): 'global' | 'fixed' | 'signal' {
  const v = String(raw ?? '').toLowerCase()
  if (v === 'global' || v === 'signal') return v
  return 'fixed'
}

function normalizeNodeSymbolRule(raw: unknown): NodeSymbolDispatchRule {
  if (!raw || typeof raw !== 'object') return createEmptyNodeSymbolRule()
  const o = raw as Record<string, unknown>
  const lotRaw = o.lot
  const lot =
    lotRaw === null || lotRaw === undefined || lotRaw === ''
      ? 0.01
      : Number(lotRaw)
  return {
    follow_sync: o.follow_sync !== false,
    follow_poll: o.follow_poll !== false,
    lot_mode: normalizeLotMode(o.lot_mode),
    lot: Number.isFinite(lot) ? lot : 0.01,
    poll_order: Number.isFinite(Number(o.poll_order)) ? Number(o.poll_order) : 0,
  }
}

/** 将 API 返回的任意对象解析为编辑器可用的结构 */
export function parseFilterRules(raw: unknown): FilterRulesConfig {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: FilterRulesConfig = {}
  for (const [symbol, rule] of Object.entries(raw as Record<string, unknown>)) {
    const key = symbol.trim().toUpperCase()
    if (!key) continue
    out[key] = normalizeSymbolRule(rule)
  }
  return out
}

/** 解析节点 filters（仅参与同步/轮询开关） */
export function parseNodeDispatchFilters(raw: unknown): NodeDispatchFiltersConfig {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: NodeDispatchFiltersConfig = {}
  for (const [symbol, rule] of Object.entries(raw as Record<string, unknown>)) {
    const key = symbol.trim().toUpperCase()
    if (!key) continue
    out[key] = normalizeNodeSymbolRule(rule)
  }
  return out
}

/** 保存前序列化（去掉空品种键、规范大小写） */
export function serializeFilterRules(rules: FilterRulesConfig): FilterRulesConfig {
  const out: FilterRulesConfig = {}
  for (const [symbol, rule] of Object.entries(rules)) {
    const key = symbol.trim().toUpperCase()
    if (!key) continue
    out[key] = {
      enabled: rule.enabled !== false,
      allow_buy: rule.allow_buy !== false,
      allow_sell: rule.allow_sell !== false,
      dispatch_mode: rule.dispatch_mode === 'poll' ? 'poll' : 'sync',
      position_scope: rule.position_scope === 'account' ? 'account' : 'symbol',
      default_action: rule.default_action === 'pass' ? 'pass' : 'block',
      intervals: (rule.intervals ?? []).map((iv) => ({
        low: Number(iv.low),
        high: Number(iv.high),
        allow: (iv.allow ?? [])
          .map((d) => String(d).toUpperCase())
          .filter((d): d is FilterDirection => d === 'BUY' || d === 'SELL'),
      })),
    }
  }
  return out
}

export function serializeNodeDispatchFilters(rules: NodeDispatchFiltersConfig): NodeDispatchFiltersConfig {
  const out: NodeDispatchFiltersConfig = {}
  for (const [symbol, rule] of Object.entries(rules)) {
    const key = symbol.trim().toUpperCase()
    if (!key) continue
    out[key] = {
      follow_sync: rule.follow_sync !== false,
      follow_poll: rule.follow_poll !== false,
      lot_mode: rule.lot_mode === 'global' || rule.lot_mode === 'signal' ? rule.lot_mode : 'fixed',
      lot: rule.lot_mode === 'fixed' ? Number(rule.lot ?? 0.01) : null,
      poll_order: Number.isFinite(Number(rule.poll_order)) ? Number(rule.poll_order) : 0,
    }
  }
  return out
}

export function validateFilterRules(rules: FilterRulesConfig): string[] {
  const errors: string[] = []
  const seen = new Set<string>()

  for (const [symbol, rule] of Object.entries(rules)) {
    const key = symbol.trim().toUpperCase()
    if (!key) {
      errors.push('存在空的品种名称，请填写或删除')
      continue
    }
    if (seen.has(key)) {
      errors.push(`品种 ${key} 重复配置`)
      continue
    }
    seen.add(key)

    rule.intervals.forEach((iv, idx) => {
      const label = `${key} 第 ${idx + 1} 条区间`
      if (!Number.isFinite(iv.low) || !Number.isFinite(iv.high)) {
        errors.push(`${label}：价格上下限必须为有效数字`)
        return
      }
      if (iv.low >= iv.high) {
        errors.push(`${label}：价格下限必须小于上限`)
      }
      if (!iv.allow?.length) {
        errors.push(`${label}：请至少勾选一个允许方向（做多/做空）`)
      }
    })
  }

  return errors
}

export function validateNodeDispatchFilters(rules: NodeDispatchFiltersConfig): string[] {
  const errors: string[] = []
  const seen = new Set<string>()
  for (const [symbol, rule] of Object.entries(rules)) {
    const key = symbol.trim().toUpperCase()
    if (!key) {
      errors.push('存在空的品种名称，请填写或删除')
      continue
    }
    if (seen.has(key)) {
      errors.push(`品种 ${key} 重复配置`)
      continue
    }
    seen.add(key)
    if (rule.lot_mode === 'fixed' && (!Number.isFinite(rule.lot) || (rule.lot ?? 0) <= 0)) {
      errors.push(`${key}：固定手数模式下请填写有效手数`)
    }
  }
  return errors
}
