<script setup lang="ts">
// 重点关注列表：质量 TOP 5 / 止盈档位 TOP 5。
//
// 同一个组件复用于两处 —— 上半屏是「质量 TOP 5」，
// 马丁止盈分布下方是「某档位 · 重点信号 TOP 5」。
import type { AiSignalRow } from '@/api/types'
import { LEVEL_TONE, fmtTime, tpLevelLabel } from '@/constants/signalCenter'

defineProps<{
  title: string
  rows: AiSignalRow[]
  /** 副标题（口径说明） */
  hint?: string
  loading?: boolean
}>()

const emit = defineEmits<{ (e: 'select', signalId: string): void }>()
</script>

<template>
  <div>
    <div class="pf-section-head" style="margin-bottom: 10px">
      <h3 class="pf-section-title"><span class="pf-bar" />{{ title }}</h3>
      <span v-if="hint" class="pf-count">{{ hint }}</span>
    </div>

    <div v-if="rows.length" class="ai-top-list">
      <button
        v-for="(row, i) in rows"
        :key="row.signal_id"
        type="button"
        class="ai-top-item"
        @click="emit('select', row.signal_id)"
      >
        <span class="ai-top-rank">{{ i + 1 }}</span>
        <span class="ai-top-main">
          <span class="ai-top-name">{{ row.name }}</span>
          <span class="ai-top-meta">
            {{ row.symbol || '—' }} · {{ row.timeframe_label }} · {{ fmtTime(row.received_at) }}
          </span>
        </span>
        <span v-if="row.tp_level" class="pf-pill blue">{{ tpLevelLabel(row.tp_level) }}</span>
        <span class="pf-pill" :class="LEVEL_TONE[row.level]">{{ row.score?.toFixed(1) ?? '—' }}</span>
      </button>
    </div>
    <p v-else class="pf-empty">暂无数据</p>
  </div>
</template>
