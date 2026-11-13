<script setup lang="ts">
// 周期质量总览：每个周期一张卡（信号数 / 平均分 / 等级 / 最优信号）。
//
// 点击卡片 → 联动筛选明细表。
import type { AiTimeframeQualityItem } from '@/api/types'
import { LEVEL_TONE } from '@/constants/signalCenter'

defineProps<{
  rows: AiTimeframeQualityItem[]
  selected?: string | null
  loading?: boolean
}>()

const emit = defineEmits<{ (e: 'select', key: string | null): void }>()
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 12px">
      <h3 class="pf-section-title"><span class="pf-bar" />周期质量总览</h3>
      <span class="pf-count">只展示窗口内实际出现过的周期</span>
    </div>

    <div v-if="rows.length" class="ai-tf-grid">
      <button
        v-for="row in rows"
        :key="row.key"
        type="button"
        class="ai-tf-card"
        :class="{ active: selected === row.key }"
        @click="emit('select', selected === row.key ? null : row.key)"
      >
        <p class="ai-tf-name">{{ row.label }}</p>
        <p class="ai-tf-score">
          {{ row.avg_score > 0 ? row.avg_score.toFixed(1) : '—' }}
          <span class="pf-pill" :class="LEVEL_TONE[row.level]">{{ row.advice }}</span>
        </p>
        <p class="ai-tf-sub">{{ row.count }} 条信号</p>
        <p v-if="row.best_signal" class="ai-tf-best">
          最优：{{ row.best_signal.name }}
        </p>
        <p v-else class="ai-tf-best ai-dim">最优：—</p>
      </button>
    </div>
    <p v-else class="pf-empty">暂无数据</p>
  </div>
</template>
