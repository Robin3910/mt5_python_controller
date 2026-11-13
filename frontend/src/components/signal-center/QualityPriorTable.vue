<script setup lang="ts">
// 质量分先验表：按「信号类型 × 周期」列出历史成功率（质量分的来源）。
//
// **这一列的「质量分」不是原始胜率，是收缩后的先验分** ——
// 样本少时自动向全局回归，避免「3 笔全赢 = 100 分」把管理员骗了。
// 表格同时给出原始胜率与样本量，用户自己能看出收缩了多少。
import type { SignalQualityBucket } from '@/api/types'

defineProps<{
  rows: SignalQualityBucket[]
}>()

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}`
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${Number(v).toFixed(2)}%`
}

/** 质量分配色：>= 60 绿 / >= 50 琥珀 / 否则红 */
function scoreClass(score: number): string {
  if (score >= 60) return 'good'
  if (score >= 50) return 'mid'
  return 'bad'
}

/** 样本量标签 —— 样本不足必须显式标出来，不能让它看起来和数据充足时一样 */
function sampleTag(level: string): { cls: string; text: string } {
  switch (level) {
    case 'sufficient':
      return { cls: '', text: '' }
    case 'thin':
      return { cls: 'amber', text: '样本少' }
    case 'insufficient':
      return { cls: 'red', text: '样本不足' }
    default:
      return { cls: 'grey', text: '无样本' }
  }
}
</script>

<template>
  <table v-if="rows.length" class="sc-bucket-table">
    <thead>
      <tr>
        <th>类型 · 周期</th>
        <th class="right">样本</th>
        <th class="right">赢</th>
        <th class="right">原始胜率</th>
        <th class="right">质量分</th>
        <th class="right">均盈亏</th>
        <th>样本量</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="b in rows" :key="b.key">
        <td>{{ b.label }}</td>
        <td class="right">{{ b.n }}</td>
        <td class="right">{{ b.wins }}</td>
        <td class="right">{{ fmtPct(b.win_rate) }}</td>
        <td class="right" :class="scoreClass(b.score)"><b>{{ b.score.toFixed(1) }}</b></td>
        <td class="right" :class="b.avg_profit > 0 ? 'good' : b.avg_profit < 0 ? 'bad' : ''">
          {{ fmtMoney(b.avg_profit) }}
        </td>
        <td>
          <span v-if="sampleTag(b.sample_level).text" class="pf-pill" :class="sampleTag(b.sample_level).cls">
            {{ sampleTag(b.sample_level).text }}
          </span>
          <span v-else class="c-dim">充足</span>
        </td>
      </tr>
    </tbody>
  </table>
  <p v-else class="pf-empty">窗口内没有可用的历史信号，暂无质量分先验</p>
</template>

<style scoped>
.sc-bucket-table {
  width: 100%;
  border-collapse: collapse;
}
.sc-bucket-table th,
.sc-bucket-table td {
  padding: 5px 6px;
  font-size: 11px;
  text-align: left;
  border-bottom: 1px solid var(--pf-border-2);
}
.sc-bucket-table th {
  color: var(--pf-dim);
  font-weight: 500;
  font-size: 10px;
}
.sc-bucket-table .right { text-align: right; }
.sc-bucket-table tr:last-child td { border-bottom: none; }

.good { color: var(--pf-green, #22c55e); }
.bad { color: var(--pf-red, #f6465d); }
.mid { color: var(--pf-amber, #f59e0b); }
</style>
