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
