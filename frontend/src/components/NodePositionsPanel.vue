<script setup lang="ts">
import { reactive } from 'vue'
import type { Position } from '@/api/types'

defineProps<{
  positions: Position[]
  showClose?: boolean
}>()

const emit = defineEmits<{ close: [ticket: number] }>()

// 手机端持仓行：点击展开/收起补充字段（止损止盈、魔术号、时间）
const posExpanded = reactive<Record<number, boolean>>({})
function togglePos(ticket: number): void {
  posExpanded[ticket] = !posExpanded[ticket]
}

function fmt(n: number | undefined | null): string {
  return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function fmtTime(ms: number | undefined | null): string {
  return ms ? new Date(ms).toLocaleString() : '—'
}
</script>

<template>
  <div>
    <div v-if="positions.length" class="pos-list mobile-only">
      <div v-for="p in positions" :key="p.ticket" class="pos-item card">
        <div class="pos-row" @click="togglePos(p.ticket)">
          <div class="pos-main">
            <span class="pos-symbol">{{ p.symbol }}</span>
            <span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type === 'BUY' ? '多' : '空' }}</span>
            <span class="pos-vol">{{ p.volume }}</span>
          </div>
          <div class="pos-px">
            <span class="pos-px-from">{{ p.price_open }}</span>
            <span class="pos-px-arrow">→</span>
            <span class="pos-px-to">{{ p.price_current }}</span>
          </div>
          <span class="pos-profit" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</span>
          <button v-if="showClose" class="btn-sm btn-ghost pos-close" @click.stop="emit('close', p.ticket)">平</button>
        </div>
        <div v-if="posExpanded[p.ticket]" class="pos-detail">
          <span>止损 <b>{{ p.sl || '—' }}</b></span>
          <span>止盈 <b>{{ p.tp || '—' }}</b></span>
          <span>魔术 <b>{{ p.magic }}</b></span>
          <span>时间 <b>{{ fmtTime(p.time * 1000) }}</b></span>
        </div>
      </div>
    </div>
    <div v-else class="card card-pad muted mobile-only" style="font-size: 13px">无持仓</div>

    <div class="card card-pad table-scroll desktop-only">
      <table v-if="positions.length">
        <thead>
          <tr>
            <th>品种</th><th>方向</th><th class="right">手数</th><th class="right">开仓价</th>
            <th class="right">现价</th><th class="right">止损</th><th class="right">止盈</th><th class="right">盈亏</th><th class="right">魔术号</th><th>时间</th><th v-if="showClose"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in positions" :key="p.ticket">
            <td>{{ p.symbol }}</td>
            <td><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></td>
            <td class="right">{{ p.volume }}</td>
            <td class="right">{{ p.price_open }}</td>
            <td class="right">{{ p.price_current }}</td>
            <td class="right">{{ p.sl || '—' }}</td>
            <td class="right">{{ p.tp || '—' }}</td>
            <td class="right" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</td>
            <td class="right muted">{{ p.magic }}</td>
            <td class="muted" style="font-size: 12px">{{ fmtTime(p.time * 1000) }}</td>
            <td v-if="showClose" class="right"><button class="btn-sm btn-ghost" @click="emit('close', p.ticket)">平</button></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted" style="font-size: 13px; padding: 8px 0">无持仓</div>
    </div>
  </div>
</template>

<style scoped>
/* ── 手机端紧凑持仓行：一行展示一个仓位 ── */
.pos-list {
  display: none;
}
@media (max-width: 768px) {
  .pos-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
}
.pos-item {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}
.pos-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}
.pos-main {
  display: flex;
  align-items: center;
  gap: 5px;
  flex: 0 0 auto;
  min-width: 0;
}
.pos-symbol {
  font-size: 13px;
  font-weight: 700;
  font-family: var(--mono);
  white-space: nowrap;
}
.pos-vol {
  font-size: 11px;
  color: var(--muted);
  font-family: var(--mono);
  white-space: nowrap;
}
.pos-px {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 4px;
  font-family: var(--mono);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
}
.pos-px-from { color: var(--muted); }
.pos-px-arrow { color: var(--muted); opacity: 0.6; font-size: 10px; }
.pos-px-to { color: var(--text); }
.pos-profit {
  flex: 0 0 auto;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  text-align: right;
  min-width: 56px;
}
.pos-close {
  flex: 0 0 auto;
  padding: 3px 8px;
  font-size: 11px;
  line-height: 1.4;
}
.pos-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--glass-border);
  font-size: 11px;
  color: var(--muted);
}
.pos-detail b {
  color: var(--text);
  font-weight: 600;
  font-family: var(--mono);
}
</style>
