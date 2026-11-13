<script setup lang="ts">
// 绩效面板：信号级 + 持仓级胜率，按周期 / 信号类型 / 入场原因分桶。
//
// **口径是这个面板唯一需要讲清楚的东西**（与后端 `signal_perf` 一一对应）：
//
// | 口径 | 分母 | 判定 |
// |---|---|---|
// | 信号级胜率 | 已收口且**有成交**的信号数 | 该信号全部持仓净盈亏合计 > 0 |
// | 持仓级胜率 | 已收口的持仓数 | 单个持仓净盈亏 > 0 |
//
// 三个必须显式展示的规则（否则用户会把数字读错）：
// 1. **进行中不进分母** —— 单列在「进行中」与「进行中浮盈」里。
//    把进行中的当输，胜率会被压得很低，那不是策略的问题，是口径的问题。
// 2. **零成交不进分母** —— 被拒收 / 全部跳过的信号没有开仓，就没有胜负。
// 3. **不统计档位级胜率** —— 档位信息只在「入场明细」里逐行展示。
//
// **周期维度用「信号级周期」**：来自 TV 告警的 `{{interval}}`，与加仓方式无关。
// 点数 / 指定价加仓不依赖周期，它们的入场级周期恒为空（显示「无周期」），
// 因此**不会出现在任何按周期的分桶里** —— 这是刻意的，见后端 `signal_meta`。
import { computed, ref } from 'vue'
import PerfBucketTable from '@/components/signal-center/PerfBucketTable.vue'
import QualityPriorTable from '@/components/signal-center/QualityPriorTable.vue'
import type { SignalCenterPerformance, SignalCenterQuality } from '@/api/types'

const props = defineProps<{
  performance: SignalCenterPerformance | null
  quality?: SignalCenterQuality | null
  loading?: boolean
}>()

type BucketKind = 'timeframe' | 'type' | 'reason'

const byTimeframe = computed(() => props.performance?.by_timeframe ?? [])
const byType = computed(() => props.performance?.by_signal_type ?? [])
const byReason = computed(() => props.performance?.by_entry_reason ?? [])
const legs = computed(() => props.performance?.legs ?? [])

/** 质量分先验表（按「类型 × 周期」） */
const qualityBuckets = computed(() => props.quality?.buckets ?? [])

/** 展开哪个分桶的明细表 */
const openBucket = ref<{ kind: BucketKind; key: string } | null>(null)

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

function toggleBucket(kind: BucketKind, key: string): void {
  if (openBucket.value?.kind === kind && openBucket.value?.key === key) {
    openBucket.value = null
    return
  }
  openBucket.value = { kind, key }
}

/** 当前展开的桶对应的入场明细 */
const openLegs = computed(() => {
  const ob = openBucket.value
  if (!ob) return []
  if (ob.kind === 'reason') {
    return legs.value.filter((l) => (l.reason || '__none__') === ob.key)
  }
  return []
})

const bucketTitle = computed(() => {
  const ob = openBucket.value
  if (!ob) return ''
  if (ob.kind === 'timeframe') {
    return byTimeframe.value.find((b) => b.key === ob.key)?.label ?? ob.key
  }
  if (ob.kind === 'type') {
    return byType.value.find((b) => b.key === ob.key)?.label ?? ob.key
  }
  return byReason.value.find((b) => b.key === ob.key)?.label ?? ob.key
})
</script>

<template>
  <div>
    <!-- 口径说明：放在最上面，避免用户先看到数字后忽略口径 -->
    <div class="pf-scope-note" style="margin-bottom: 12px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 9v4M12 17h.01" />
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>{{ performance?.scope || '仅统计 AI 智能加仓（tpl_1）的任务' }}</span>
    </div>

    <!-- 总览 -->
    <div class="pf-lat-grid" style="margin-bottom: 12px">
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>信号级胜率</span>
          <span class="pf-pill blue">按信号号计一次</span>
        </p>
        <p class="pf-lat-v">{{ fmtPct(performance?.totals.signal_win_rate) }}</p>
        <p class="pf-lat-sub">
          {{ performance?.totals.signals_win ?? 0 }} / {{ performance?.totals.signals_closed ?? 0 }} 条已收口
        </p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>持仓级胜率</span>
          <span class="pf-pill green">与成交侧同口径</span>
        </p>
        <p class="pf-lat-v">{{ fmtPct(performance?.totals.position_win_rate) }}</p>
        <p class="pf-lat-sub">
          {{ performance?.totals.positions_win ?? 0 }} / {{ performance?.totals.positions_closed ?? 0 }} 笔已收口持仓
        </p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>已收口净盈亏</span>
          <span class="pf-pill">信号级合计</span>
        </p>
        <p class="pf-lat-v" :class="(performance?.totals.signal_profit ?? 0) >= 0 ? 'good' : 'bad'">
          {{ fmtMoney(performance?.totals.signal_profit) }}
        </p>
        <p class="pf-lat-sub">单信号均值 {{ fmtMoney(performance?.totals.avg_signal_profit) }}</p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>进行中</span>
          <span class="pf-pill amber">不进分母</span>
        </p>
        <p class="pf-lat-v">{{ performance?.totals.signals_running ?? 0 }}</p>
        <p class="pf-lat-sub">浮盈 {{ fmtMoney(performance?.totals.running_profit) }}</p>
      </div>
    </div>

    <!-- 三个分桶表 -->
    <div class="pf-grid-2">
      <div class="pf-card">
        <div class="pf-section-head" style="margin-bottom: 10px">
          <h3 class="pf-section-title"><span class="pf-bar" />按信号级周期</h3>
          <span class="pf-count">TV 告警的周期</span>
        </div>
        <PerfBucketTable :rows="byTimeframe" @toggle="(k) => toggleBucket('timeframe', k)" />
      </div>

      <div class="pf-card">
        <div class="pf-section-head" style="margin-bottom: 10px">
          <h3 class="pf-section-title"><span class="pf-bar" />按信号类型</h3>
          <span class="pf-count">首仓结构类型</span>
        </div>
        <PerfBucketTable :rows="byType" @toggle="(k) => toggleBucket('type', k)" />
      </div>
    </div>

    <div class="pf-card" style="margin-top: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />按入场原因</h3>
        <span class="pf-count">每一次入场（首仓 + 逐档加仓）</span>
      </div>
      <PerfBucketTable :rows="byReason" @toggle="(k) => toggleBucket('reason', k)" />
    </div>

    <!-- 质量分先验表：信号流里那一列「质量」的来源 -->
    <div class="pf-card" style="margin-top: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />质量分先验（类型 × 周期）</h3>
        <span class="pf-count">
          全局 {{ quality?.global.n ?? 0 }} 笔 · 胜率 {{ (quality?.global.win_rate ?? 0).toFixed(1) }}%
        </span>
      </div>
      <QualityPriorTable :rows="qualityBuckets" />
      <p class="c-dim" style="font-size: 11px; margin: 8px 0 0">
        质量分 = 该桶的历史信号级胜率，小样本按贝叶斯收缩向全局回归（k=10）；
        仅展示，不参与任何交易决策。
      </p>
    </div>

    <!-- 入场明细（展开某个分桶后展示） -->
    <div v-if="openBucket" class="pf-card" style="margin-top: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />入场明细 · {{ bucketTitle }}</h3>
        <span class="pf-count">仅展示盈亏，不做档位级胜率</span>
      </div>
      <table v-if="openLegs.length" class="sc-leg-table">
        <thead>
          <tr>
            <th>信号</th>
            <th>序号</th>
            <th>入场原因</th>
            <th>周期</th>
            <th class="right">手数</th>
            <th class="right">成交价</th>
            <th class="right">净盈亏</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(l, i) in openLegs" :key="`${l.signal_id}-${l.task_id}-${l.seq}-${i}`">
            <td class="c-mono">{{ l.signal_id }}</td>
            <td>{{ l.seq === 0 ? '首仓' : `第 ${l.seq} 档` }}</td>
            <td>{{ l.reason_label }}</td>
            <td>{{ l.timeframe_label || '无周期' }}</td>
            <td class="right">{{ l.volume ?? '—' }}</td>
            <td class="right">{{ l.price ?? '—' }}</td>
            <td
              class="right"
              :class="(l.realized_profit ?? 0) > 0 ? 'good' : (l.realized_profit ?? 0) < 0 ? 'bad' : ''"
            >
              {{ l.closed ? fmtMoney(l.realized_profit) : '持仓中' }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="pf-empty">该维度暂无入场明细</p>
    </div>
  </div>
</template>

<style scoped>
.sc-leg-table {
  width: 100%;
  border-collapse: collapse;
}
.sc-leg-table th,
.sc-leg-table td {
  padding: 5px 6px;
  font-size: 11px;
  text-align: left;
  border-bottom: 1px solid var(--pf-border-2);
}
.sc-leg-table th {
  color: var(--pf-dim);
  font-weight: 500;
  font-size: 10px;
}
.sc-leg-table .right { text-align: right; }
.sc-leg-table tr:last-child td { border-bottom: none; }

.good { color: var(--pf-green, #22c55e); }
.bad { color: var(--pf-red, #f6465d); }
</style>
