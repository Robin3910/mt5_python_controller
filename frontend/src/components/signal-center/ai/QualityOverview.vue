<script setup lang="ts">
// 信号质量总览 —— **原型复刻**（灵光闪原型【我的】→【AI信号管理中心】）。
//
// 原型里这是一个**大卡片**，内部包含 4 个子区块（自上而下）：
//   1. 可靠性等级分布（环形图 + 图例条）
//   2. 质量分区间分布（堆叠条 + 4 格计数）
//   3. 信号类型质量对比（分类行，可点击联动筛选）
//   4. 重点关注 · 质量 TOP 5（排名列表）
//
// **两个分布是两回事**，别混读：
// - 等级分布：**按绝对阈值**分的高/中等/低三档（85 / 70 分界）；
// - 区间分布：**按分数高低**分的三档（85+ / 70-84 / <70）。
//
// ⚠️ 口径以现有实现为准（`signal_quality`）：等级阈值是 85/70 —— 详见 `docs/superpowers/specs/2026-11-13-ai-signal-center-prototype-design.md`。
import { computed } from 'vue'
import type {
  AiReliabilityItem,
  AiScoreBucket,
  AiSignalRow,
  AiTypeQualityItem,
} from '@/api/types'
import {
  LEVEL_CHART_COLORS,
  SCORE_BAND_COLORS,
  scoreTone,
} from '@/constants/signalCenter'
import QualityTopList from './QualityTopList.vue'
import TypeQualityCompare from './TypeQualityCompare.vue'

const props = defineProps<{
  reliability: AiReliabilityItem[]
  scoreBuckets: AiScoreBucket[]
  /** 信号类型质量对比（业务分类） */
  byType: AiTypeQualityItem[]
  /** 重点关注 · 质量 TOP 5 */
  topSignals: AiSignalRow[]
  /** 平均质量（环形图中心数字） */
  avgQuality: number
  /** 当前选中的业务分类 */
  selectedGroup?: string | null
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', group: string | null): void
}>()

/** 环形图的分母：等级分布的信号总条数 */
const total = computed(() =>
  props.reliability.reduce((sum, r) => sum + r.count, 0),
)

/** 环形图的 conic-gradient（按等级顺序累加角度） */
const donutGradient = computed(() => {
  if (!total.value) return ''
  let acc = 0
  const parts = props.reliability.map((r) => {
    const start = acc
    acc += (r.count / total.value) * 100
    return `${LEVEL_CHART_COLORS[r.level] ?? '#4a5568'} ${start}% ${acc}%`
  })
  return `conic-gradient(${parts.join(',')})`
})

/** 区间分布的分母：四档之和（= 有可用先验的信号条数） */
const bandTotal = computed(() =>
  props.scoreBuckets.reduce((sum, b) => sum + b.count, 0),
)

/** 环形图中心的分值配色 */
const avgTone = computed(() => scoreTone(props.avgQuality))

/**
 * 图例条的占比。
 *
 * 用**本模块自己的分母**（三档等级条数之和），不用后端给的 `pct` ——
 * 后端的 `pct` 分母是窗口内**全部信号**（含「暂无历史」那一档，
 * 它没进 reliability 列表），直接拿来当条长会和环形图对不上。
 */
function share(count: number): number {
  return total.value ? (count / total.value) * 100 : 0
}
</script>

<template>
  <div class="pf-card">
    <!-- 区块标题 -->
    <div class="pf-section-head">
      <h3 class="pf-section-title"><span class="pf-bar" />信号质量总览</h3>
      <span class="pf-count">点击类型可筛选下方明细</span>
    </div>

    <!-- ① 可靠性等级分布 + ② 质量分区间分布 -->
    <div class="ai-q-grid">
      <!-- ① 可靠性等级分布 -->
      <div class="ai-q-panel">
        <p class="ai-q-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3z" />
            <path d="M9 12l2 2 4-4" />
          </svg>
          可靠性等级分布
        </p>

        <div v-if="total" class="ai-q-donut-row">
          <div class="ai-q-donut" :style="{ background: donutGradient }">
            <div class="ai-q-donut-inner">
              <p class="ai-q-donut-v" :class="`ai-score.${avgTone}`">{{ avgQuality.toFixed(1) }}</p>
              <p class="ai-q-donut-k">平均质量</p>
            </div>
          </div>
          <div class="ai-q-legend">
            <div v-for="r in reliability" :key="r.level" class="ai-q-legend-row">
              <i class="ai-q-dot" :style="{ background: LEVEL_CHART_COLORS[r.level] }" />
              <span class="ai-q-legend-label">{{ r.label }}</span>
              <span class="ai-q-bar-track">
                <span
                  class="ai-q-bar-fill"
                  :style="{ width: Math.min(100, share(r.count)) + '%', background: LEVEL_CHART_COLORS[r.level] }"
                />
              </span>
              <span class="ai-q-legend-val">{{ share(r.count).toFixed(1) }}% · {{ r.count }} 条</span>
            </div>
          </div>
        </div>
        <p v-else class="pf-empty">暂无数据</p>
      </div>

      <!-- ② 质量分区间分布 -->
      <div class="ai-q-panel">
        <p class="ai-q-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
          </svg>
          质量分区间分布
        </p>

        <div v-if="bandTotal" class="ai-q-stack">
          <span
            v-for="b in scoreBuckets"
            :key="b.key"
            class="ai-q-stack-seg"
            :style="{ width: (b.count / bandTotal) * 100 + '%', background: SCORE_BAND_COLORS[b.key] ?? '#3f4a5f' }"
            :title="`${b.label} ${b.count} 条`"
          />
        </div>

        <div class="ai-q-cells">
          <div v-for="b in scoreBuckets" :key="b.key" class="ai-q-cell">
            <p class="ai-q-cell-k">{{ b.label }}</p>
            <p class="ai-q-cell-v" :style="{ color: SCORE_BAND_COLORS[b.key] }">{{ b.count }}</p>
            <p class="ai-q-cell-p">{{ bandTotal ? Math.round((b.count / bandTotal) * 100) : 0 }}%</p>
          </div>
        </div>

        <p class="ai-q-note">
          质量分 ≥ 85 为高可靠（重点跟踪），70 - 84 为中等（轻仓观察），低于 70 建议暂不参与。
        </p>
      </div>
    </div>

    <!-- ③ 信号类型质量对比 -->
    <TypeQualityCompare
      :rows="byType"
      :selected="selectedGroup"
      :loading="loading"
      @select="(g) => emit('select', g)"
    />

    <!-- ④ 重点关注 · 质量 TOP 5 -->
    <QualityTopList :rows="topSignals" :loading="loading" />
  </div>
</template>
