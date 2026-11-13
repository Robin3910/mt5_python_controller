<script setup lang="ts">
// 绩效分桶表：把一个维度（周期 / 类型 / 入场原因）的聚合行渲染成表格。
//
// 单独拆成一个组件，是因为三个维度要用**完全相同**的表头与口径说明 ——
// 复制三份表格模板必然会出现「某一行少显示一列」的不一致。
import type { SignalPerfBucket } from '@/api/types'

defineProps<{
  rows: SignalPerfBucket[]
}>()

const emit = defineEmits<{
  (e: 'toggle', key: string): void
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

/** 胜率配色：>= 60 绿 / >= 45 琥珀 / 否则红 */
function rateClass(rate: number): string {
  if (rate >= 60) return 'good'
  if (rate >= 45) return 'mid'
  return 'bad'
}

function moneyClass(v: number): string {
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}
</script>

<template>
  <table v-if="rows.length" class="sc-bucket-table">
    <thead>
      <tr>
        <th>维度</th>
        <th class="right">信号数</th>
        <th class="right">信号级胜率</th>
        <th class="right">信号净盈亏</th>
        <th class="right">持仓数</th>
        <th class="right">持仓级胜率</th>
        <th class="right">持仓净盈亏</th>
        <th class="right">进行中</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="b in rows" :key="b.key" class="sc-clickable" @click="emit('toggle', b.key)">
        <td>{{ b.label }}</td>
        <td class="right">{{ b.signals_total }}</td>
        <td class="right" :class="rateClass(b.signal_win_rate)">{{ fmtPct(b.signal_win_rate) }}</td>
        <td class="right" :class="moneyClass(b.signal_profit)">{{ fmtMoney(b.signal_profit) }}</td>
        <td class="right">{{ b.positions_total }}</td>
        <td class="right" :class="rateClass(b.position_win_rate)">{{ fmtPct(b.position_win_rate) }}</td>
        <td class="right" :class="moneyClass(b.position_profit)">{{ fmtMoney(b.position_profit) }}</td>
        <td class="right">{{ b.signals_running }}</td>
      </tr>
    </tbody>
  </table>
  <p v-else class="pf-empty">暂无数据</p>
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
.sc-clickable { cursor: pointer; }
.sc-clickable:hover td { background: rgba(37, 99, 235, 0.08); }

.good { color: var(--pf-green, #22c55e); }
.bad { color: var(--pf-red, #f6465d); }
.mid { color: var(--pf-amber, #f59e0b); }
</style>
