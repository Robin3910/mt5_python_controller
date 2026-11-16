<script setup lang="ts">
// 信号类型质量对比 —— **原型复刻**：每个业务分类**一行**（不是卡片网格）。
//
// **按业务分类聚合**（结构分析 = ob + fvg + pdz 三个码合一行）——
// 原型就是这么分的。点击后把整个**分类**传给父组件，由后端展开成
// `signal_type IN (...)` 过滤（只取一个码会漏掉另外两个）。
//
// 行内 5 列（对齐原型）：分类名 · 均分条 · 均分 · 条数/高可靠 · 决策指导
import type { AiTypeQualityItem } from '@/api/types'
import { scoreTone, scoreBarTone } from '@/constants/signalCenter'
const props = defineProps<{
  rows: AiTypeQualityItem[]
  /** 当前选中的业务分类 */
  selected?: string | null
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', group: string | null): void
}>()

function onClick(row: AiTypeQualityItem): void {
  // 再点一次同样的 → 取消筛选
  emit('select', props.selected === row.key ? null : row.key)
}
</script>

<template>
  <div class="ai-q-sub">
    <div class="ai-q-sub-head">
      <p class="ai-q-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 7h18M3 12h18M3 17h18" />
        </svg>
        信号类型质量对比
      </p>
      <button
        type="button"
        class="ai-q-cat-all"
        :class="{ active: !selected }"
        @click="emit('select', null)"
      >
        全部类型
      </button>
    </div>

    <div v-if="rows.length" class="ai-q-cat-list">
      <button
        v-for="row in rows"
        :key="row.key"
        type="button"
        class="ai-q-cat-row"
        :class="{ active: selected === row.key }"
        @click="onClick(row)"
      >
        <span class="ai-q-cat-name">{{ row.label }}</span>
        <span class="ai-q-cat-track">
          <span
            class="ai-q-cat-fill"
            :class="`ai-bar-${scoreBarTone(row.avg_score)}`"
            :style="{ width: Math.min(100, row.avg_score) + '%' }"
          />
        </span>
        <span class="ai-q-cat-score" :class="`ai-score.${scoreTone(row.avg_score)}`">
          {{ row.avg_score > 0 ? row.avg_score.toFixed(1) : '—' }}
        </span>
        <span class="ai-q-cat-meta">
          {{ row.count }} 条 · 高可靠
          <em :class="{ good: row.high_count > 0 }">{{ row.high_count }}</em>
        </span>
      </button>
    </div>
    <p v-else class="pf-empty">暂无数据</p>
  </div>
</template>
