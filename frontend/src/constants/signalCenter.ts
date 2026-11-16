// AI 信号管理中心的**展示层常量与拼装函数**。
//
// **口径全部由后端决定**（`signal_meta` / `signal_quality`）：
// 质量分怎么算、等级怎么分、类型怎么归类，前端一律**原样展示**，
// 这里只管「怎么显示」—— 前端自己再算一遍必然与后端对不上。

/** 可靠性等级 → 中文标签（绝对阈值：≥60 高 / 50-60 中 / <50 低） */
export const LEVEL_LABELS: Record<string, string> = {
  high: '高',
  mid: '中',
  low: '低',
  none: '暂无',
}

/** 可靠性等级 → 决策指导 */
export const LEVEL_ADVICE: Record<string, string> = {
  high: '关注',
  mid: '观察',
  low: '暂不参与',
  none: '暂无历史',
}

/** 等级 → 色带类名（配合 `.ai-dist-bar` / `.ai-score` 的高亮色） */
export const LEVEL_TONE: Record<string, string> = {
  high: 'green',
  mid: 'blue',
  low: 'amber',
  none: 'grey',
}

// ─────────────────────────────────────────────────────────────
// 图表配色（原型复刻用）
// ─────────────────────────────────────────────────────────────

/**
 * 可靠性等级 → 图表色（环形图 / 图例条）。
 *
 * 原型的三档配色是「高绿 / 中琥珀 / 低红」，
 * 与 `LEVEL_TONE`（列表徽章用的语义色）**故意分开**：
 * 徽章是标签，图表是面积，两套语义不能混。
 */
export const LEVEL_CHART_COLORS: Record<string, string> = {
  high: '#22c55e',
  mid: '#f59e0b',
  low: '#f6465d',
  none: '#4a5568',
}

/**
 * 质量分区间 → 图表色（堆叠条 / 计数格）。
 *
 * key 与后端 `signal_ai_center.SCORE_BUCKETS` 一一对应，
 * 改后端区间必须同步改这里。
 */
export const SCORE_BAND_COLORS: Record<string, string> = {
  gte90: '#22c55e',
  '80_89': '#3b82f6',
  '70_79': '#f59e0b',
  lt70: '#f6465d',
}

/**
 * 质量分 → 文字配色类名。
 *
 * 阈值与后端等级阈值一致（≥60 高 / ≥50 中 / 其余低），
 * **不用**原型文案里的 85 / 70 —— 那跟我们的等级口径对不上，
 * 同一个数字在两块地方显示不同颜色会让人以为算错了。
 */
export function scoreTone(score: number): string {
  if (score >= 60) return 'green'
  if (score >= 50) return 'amber'
  return 'red'
}

/** 质量分 → 条形填充配色类名（`.ai-bar-green` / `.ai-bar-amber` / `.ai-bar-red`） */
export function scoreBarTone(score: number): string {
  if (score >= 60) return 'green'
  if (score >= 50) return 'amber'
  return 'red'
}

/** 档位标签：1 单止盈 ~ 5 单止盈 */
export function tpLevelLabel(level: number | null | undefined): string {
  if (level === null || level === undefined) return '—'
  return `${level}单止盈`
}

/** 时间戳 → MM-DD HH:mm */
export function fmtTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 止盈状态 → 中文标签 */
export function tpStateLabel(state: string | null | undefined): string {
  switch (state) {
    case 'tp':
      return '已止盈'
    case 'sl':
      return '止损'
    case 'running':
      return '持仓中'
    default:
      return '无数据'
  }
}
