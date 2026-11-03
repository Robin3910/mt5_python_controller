<script setup lang="ts">
// 信号中心 Hero：大数字 + 状态药丸 + 分布条 + 指标格。
//
// **只展示，不计算**：所有数字都来自后端 `signal_stats.overview`，
// 前端再算一遍必然与信号流列表对不上。
//
// 两个成功率口径必须在界面上分开标注（这是最容易看错的地方）：
// - 信号级成功率：成功信号 / 非拒收信号
// - 分发级成功率：投递成功 / 投递总数
import { computed } from 'vue'
import type { SignalCenterOverview } from '@/api/types'

const props = defineProps<{
  overview: SignalCenterOverview | null
  loading?: boolean
}>()

/** 状态 → 配色（与全局语义色一致） */
const STATUS_COLORS: Record<string, string> = {
  done: '#22c55e',
  partial: '#f59e0b',
  dispatching: '#2563eb',
  pending: '#2563eb',
  rejected: '#f6465d',
  failed: '#f6465d',
  duplicate: '#a78bfa',
}

const STATUS_LABELS: Record<string, string> = {
  done: '完成',
  partial: '部分成功',
  dispatching: '分发中',
  pending: '等待',
  rejected: '已拒收',
  failed: '失败',
  duplicate: '重复',
}

const totals = computed(() => props.overview?.totals ?? null)

/** 分布条：按状态计数切成多段 */
const distSegments = computed(() => {
  const items = props.overview?.status_distribution ?? []
  const total = items.reduce((sum, i) => sum + i.count, 0)
  if (!total) return []
  return items.map((i) => ({
    status: i.status,
    count: i.count,
    label: STATUS_LABELS[i.status] ?? i.status,
    color: STATUS_COLORS[i.status] ?? '#6b7a93',
    pct: (i.count / total) * 100,
  }))
})

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

/** 药丸的语义色（class 后缀） */
function pillClass(status: string): string {
  if (status === 'done') return 'green'
  if (status === 'partial') return 'amber'
  if (status === 'rejected' || status === 'failed') return 'red'
  if (status === 'dispatching' || status === 'pending') return 'blue'
  return 'grey'
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(1)}%`
}
</script>

<template>
  <div class="pf-hero">
    <div class="pf-hero-top">
      <div>
        <p class="pf-hero-label">窗口内信号数</p>
        <p class="pf-hero-value">
          {{ totals?.signals ?? 0 }}<span class="pf-hero-unit">条</span>
        </p>
      </div>

      <!-- 状态药丸组 -->
      <div v-if="distSegments.length" class="pf-hero-chips">
        <span
          v-for="seg in distSegments"
          :key="seg.status"
          class="pf-pill"
          :class="pillClass(seg.status)"
        >
          <span class="pf-pill-dot" />
          {{ statusLabel(seg.status) }} {{ seg.count }}
        </span>
      </div>
    </div>

    <!-- 分布条 -->
    <div v-if="distSegments.length" class="pf-dist-bar">
      <div
        v-for="seg in distSegments"
        :key="seg.status"
        class="pf-dist-seg"
        :style="{ width: `${seg.pct}%`, background: seg.color }"
        :title="`${seg.label} ${seg.count} 条（${seg.pct.toFixed(1)}%）`"
      />
    </div>

    <div v-if="distSegments.length" class="pf-dist-legend">
      <span v-for="seg in distSegments" :key="seg.status" class="pf-dist-legend-item">
        <span class="pf-dist-dot" :style="{ background: seg.color }" />
        {{ seg.label }} {{ seg.count }}
      </span>
    </div>

    <!-- 指标格 -->
    <div class="pf-metrics pf-hero-metrics">
      <div class="pf-metric">
        <p class="pf-metric-k">
          <span class="pf-metric-icon ok">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M20 6 9 17l-5-5" />
            </svg>
          </span>
          信号级成功率
        </p>
        <p class="pf-metric-v">{{ fmtPct(totals?.signal_success_rate) }}</p>
        <p class="pf-metric-sub">分母 = 非拒收信号</p>
      </div>

      <div class="pf-metric">
        <p class="pf-metric-k">
          <span class="pf-metric-icon info">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 8v4l3 2" />
            </svg>
          </span>
          分发级成功率
        </p>
        <p class="pf-metric-v">{{ fmtPct(totals?.dispatch_success_rate) }}</p>
        <p class="pf-metric-sub">分母 = 投递总数</p>
      </div>

      <div class="pf-metric">
        <p class="pf-metric-k">
          <span class="pf-metric-icon warn">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
            </svg>
          </span>
          拦截 / 拒收
        </p>
        <p class="pf-metric-v">
          {{ (totals?.skipped ?? 0) + (totals?.rejected ?? 0) }}
        </p>
        <p class="pf-metric-sub">
          跳过 {{ totals?.skipped ?? 0 }} · 拒收 {{ totals?.rejected ?? 0 }}
        </p>
      </div>

      <div class="pf-metric">
        <p class="pf-metric-k">
          <span class="pf-metric-icon bad">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 9v4M12 17h.01" />
              <circle cx="12" cy="12" r="9" />
            </svg>
          </span>
          去重抑制
        </p>
        <!-- 本期不采集（决策点 3）：显示「—」而不是 0，避免被读成「一次都没去重」 -->
        <p class="pf-metric-v">
          {{ totals?.duplicate_collected ? totals.duplicate : '—' }}
        </p>
        <p class="pf-metric-sub">
          {{ totals?.duplicate_collected ? '窗口内被抑制次数' : '未采集' }}
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pf-metric-sub {
  margin: 2px 0 0;
  font-size: 9px;
  color: var(--pf-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
