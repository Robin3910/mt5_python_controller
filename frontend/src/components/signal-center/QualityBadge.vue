<script setup lang="ts">
// 质量分徽章：历史同类信号的成功率。
//
// **只展示，不参与任何交易决策**（见 signal_quality 模块 docstring 铁律 2）。
//
// 两个正交维度要分开表达，否则用户会误读：
// - `level`：分数**回退到了哪一档**（类型×周期 / 仅类型 / 全局）；
// - `sample_level`：样本**够不够** —— 样本不足时徽章置灰 + 角标，
//   **不让它看起来和数据充足时一样**（3 笔 100% 胜率不该和 300 笔 90% 一样醒目）。
import { computed } from 'vue'
import type { SignalQuality } from '@/api/types'

const props = withDefaults(
  defineProps<{
    quality: SignalQuality | null | undefined
    /** 紧凑模式：只显示分数，不显示样本角标 */
    compact?: boolean
  }>(),
  { compact: false },
)

/** 色带：≥60 绿 / 50~60 蓝灰 / <50 橙红 */
function toneClass(score: number | null): string {
  if (score === null || score === undefined) return 'grey'
  if (score >= 60) return 'green'
  if (score >= 50) return 'blue'
  return 'amber'
}

const tone = computed(() => toneClass(props.quality?.score ?? null))

/** 样本不足 → 徽章置灰（数据不可靠时不该看起来很确定） */
const weak = computed(
  () =>
    props.quality !== null &&
    props.quality !== undefined &&
    (props.quality.sample_level === 'insufficient' ||
      props.quality.sample_level === 'none'),
)

const sampleBadge = computed(() => {
  const lvl = props.quality?.sample_level
  if (lvl === 'insufficient') return '样本不足'
  if (lvl === 'thin') return '样本少'
  return ''
})

/** 分数取自哪一档 —— 用户要能看懂「这个 58 分凭什么」 */
const basisText = computed(() => {
  const q = props.quality
  if (!q) return ''
  if (q.level === 'type_tf') return q.basis_label || '类型×周期'
  if (q.level === 'type') return `${q.basis_label || '类型'}（跨周期）`
  if (q.level === 'global') return '全部信号（全局兜底）'
  return ''
})

const tip = computed(() => {
  const q = props.quality
  if (!q) return '暂无历史成功率先验'
  const parts: string[] = []
  parts.push(basisText.value)
  parts.push(`历史 ${q.n} 笔 · 胜率 ${q.win_rate.toFixed(1)}%`)
  if (q.avg_profit) {
    const sign = q.avg_profit > 0 ? '+' : ''
    parts.push(`均盈亏 ${sign}${q.avg_profit.toFixed(2)}`)
  }
  if (q.sample_level === 'thin') parts.push('样本偏少，分数已向全局回归')
  if (q.sample_level === 'insufficient') parts.push('样本不足，分数已大幅向全局回归')
  return parts.filter(Boolean).join(' · ')
})
</script>

<template>
  <span class="qf" :title="tip">
    <span v-if="quality && quality.score !== null" class="pf-pill" :class="[tone, weak ? 'qf-weak' : '']">
      {{ quality.score.toFixed(1) }}
    </span>
    <span v-else class="pf-pill grey">暂无历史</span>
    <span v-if="!compact && sampleBadge" class="qf-tag">{{ sampleBadge }}</span>
  </span>
</template>

<style scoped>
.qf {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

/* 样本不足 → 置灰：数据不可靠时不该看起来很确定 */
.qf-weak {
  opacity: 0.55;
}

.qf-tag {
  font-size: 9px;
  color: var(--pf-faint);
  white-space: nowrap;
}
</style>
