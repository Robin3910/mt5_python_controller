import type {
  DefaultFilterAction,
  FilterDirection,
  FilterInterval,
  FilterRulesConfig,
  SymbolFilterRule,
} from '@/api/types'

export function createEmptyInterval(): FilterInterval {
  return { low: 0, high: 0, allow: ['BUY'] }
}

export function createEmptySymbolRule(): SymbolFilterRule {
  return { enabled: true, default_action: 'block', intervals: [] }
}

export function exampleFilterRules(): FilterRulesConfig {
  return {
    XAUUSD: {
      enabled: true,
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
    default_action: defaultAction,
    intervals,
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

/** 保存前序列化（去掉空品种键、规范大小写） */
export function serializeFilterRules(rules: FilterRulesConfig): FilterRulesConfig {
  const out: FilterRulesConfig = {}
  for (const [symbol, rule] of Object.entries(rules)) {
    const key = symbol.trim().toUpperCase()
    if (!key) continue
    out[key] = {
      enabled: rule.enabled !== false,
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
