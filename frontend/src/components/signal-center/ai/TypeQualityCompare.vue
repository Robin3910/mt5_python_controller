<script setup lang="ts">
// 信号类型质量对比：每个业务分类一张卡，点击可联动筛选下面的明细表。
//
// **按业务分类聚合**（结构分析 = ob + fvg + pdz 三个码合一张卡）——
// 原型就是这么分的。点击后把整个**分类**传给父组件，由后端展开成
// `signal_type IN (...)` 过滤（只取一个码会漏掉另外两个）。
import type { AiTypeQualityItem } from '@/api/types'
import { LEVEL_TONE } from '@/constants/signalCenter'

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
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 12px">
      <h3 class="pf-section-title"><span class="pf-bar" />信号类型质量对比</h3>
      <span class="pf-count">点击卡片可筛选下方明细</span>
    </div>

    <div v-if="rows.length" class="ai-type-grid">
      <button
        v-for="row in rows"
        :key="row.key"
        type="button"
        class="ai-type-card"
        :class="{ active: selected === row.key }"
        @click="onClick(row)"
      >
        <p class="ai-type-name">{{ row.label }}</p>
        <p class="ai-type-score">
          {{ row.avg_score > 0 ? row.avg_score.toFixed(1) : '—' }}
          <span class="pf-pill" :class="LEVEL_TONE[row.level]">{{ row.advice }}</span>
        </p>
        <p class="ai-type-sub">{{ row.count }} 条 · 高可靠 {{ row.high_count }} 条</p>
      </button>
    </div>
    <p v-else class="pf-empty">暂无数据</p>
  </div>
</template>
