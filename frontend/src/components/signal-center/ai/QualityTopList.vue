<script setup lang="ts">
// 重点关注 · 质量 TOP 5 —— **原型复刻**（信号质量总览模块的第 4 个子区块）。
//
// 原型一行 5 列：排名 · 信号名(+周期徽章/分类/止盈) · 质量分 · 决策指导 · 箭头。
import type { AiSignalRow } from '@/api/types'
import { LEVEL_TONE, tpLevelLabel } from '@/constants/signalCenter'

defineProps<{
  rows: AiSignalRow[]
  loading?: boolean
}>()

const emit = defineEmits<{ (e: 'select', signalId: string): void }>()
</script>

<template>
  <div class="ai-q-sub ai-q-top">
    <div class="ai-q-sub-head">
      <p class="ai-q-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" />
        </svg>
        重点关注 · 质量 TOP {{ rows.length }}
      </p>
      <span class="ai-q-sub-hint">按质量分排序 · 点击查看详情</span>
    </div>

    <div v-if="rows.length" class="ai-q-top-list">
      <button
        v-for="(row, i) in rows"
        :key="row.signal_id"
        type="button"
        class="ai-q-top-row"
        @click="emit('select', row.signal_id)"
      >
        <span class="ai-q-top-rank" :class="{ top: i < 3 }">{{ i + 1 }}</span>
        <span class="ai-q-top-main">
          <span class="ai-q-top-name">{{ row.name }}</span>
          <span class="ai-q-top-meta">
            <span class="ai-q-top-tf">{{ row.timeframe_label }}</span>
            <span class="ai-q-top-cat">{{ row.type_group }}</span>
            <span v-if="row.tp_level" class="ai-q-top-tp">{{ tpLevelLabel(row.tp_level) }}</span>
          </span>
        </span>
        <span class="pf-pill" :class="LEVEL_TONE[row.level]">
          {{ row.score !== null ? row.score.toFixed(1) : '—' }}
        </span>
        <span class="ai-q-top-advice" :class="row.level === 'high' ? 'ai-good' : 'ai-dim'">
          {{ row.advice }}
        </span>
        <svg class="ai-q-top-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 6l6 6-6 6" />
        </svg>
      </button>
    </div>
    <p v-else class="pf-empty">当前档位下暂无信号</p>
  </div>
</template>
