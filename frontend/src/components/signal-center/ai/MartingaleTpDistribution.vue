<script setup lang="ts">
// 马丁止盈分布：AI 逆势加仓「加了几仓最终止盈」的分布。
//
// **三条口径（拍板定的）**：
// 1. 档位 = 最终止盈那一刻的**持仓笔数**（含首仓）；
// 2. **按节点分开统计** —— 每个节点一份统计，**不合并、不取最大值**；
// 3. **止损不进止盈分布** —— 止损 / 持仓中的单元不产生档位，不进分母。
//
// ⚠️ **口径 #2 是「怎么数」，不是「怎么显示」**：
// 同一条信号路由到 3 个账户，就是 3 个独立单元，各自有自己的档位
// （三个账户的加仓节奏不同，合成一个数字两边都不认）。
// 但**展示上不按账户拆开** —— 只画**一条合并后的分布条**，
// 否则一个模块里堆 4 条分布条，横向对比反而看不清整体形态。
//
// 「全部信号 N」是窗口内的**信号总数**，「已止盈 M」才是**分布分母** ——
// 两者口径不同，UI 上必须分开标注，不能放一起让人误读。
import { computed } from 'vue'
import type { AiTpDistribution } from '@/api/types'
import TopSignalList from './TopSignalList.vue'

const props = defineProps<{
  dist: AiTpDistribution | null
  /** 当前选中的档位（1..5，null = 全部） */
  selectedLevel: number | null
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', level: number | null): void
}>()

/** 合并后的档位分布（各账户统计单元之和） */
const levels = computed(() => props.dist?.overall?.buckets ?? [])

/** 当前档位下重点关注的 TOP 5 */
const topRows = computed(() => {
  if (!props.dist) return []
  const key = props.selectedLevel === null ? 'all' : String(props.selectedLevel)
  return props.dist.top_by_level?.[key] ?? []
})

const topTitle = computed(() =>
  props.selectedLevel === null
    ? '全部档位 · 重点信号 TOP 5'
    : `${props.selectedLevel}单止盈 · 重点信号 TOP 5`,
)

/** 状态统计（止损 / 持仓中各多少） */
const stateSummary = computed(() => {
  const s = props.dist?.states ?? {}
  return [
    { key: 'tp', label: '已止盈', value: s.tp ?? 0 },
    { key: 'sl', label: '止损', value: s.sl ?? 0 },
    { key: 'running', label: '持仓中', value: s.running ?? 0 },
  ]
})
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 12px">
      <h3 class="pf-section-title"><span class="pf-bar" />马丁止盈分布</h3>
      <span class="pf-count">
        全部信号 {{ dist?.total_signals ?? 0 }} · 已止盈 {{ dist?.closed_tp_units ?? 0 }}
      </span>
    </div>

    <!-- 口径说明 -->
    <div v-if="dist?.scope" class="pf-scope-note info" style="margin-bottom: 12px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
      <span>{{ dist.scope }}</span>
    </div>

    <!-- 档位按钮组（全部 + 1~5） -->
    <div class="ai-tp-chips">
      <button
        type="button"
        class="pf-seg-item"
        :class="{ active: selectedLevel === null }"
        @click="emit('select', null)"
      >
        全部信号 ({{ dist?.total_signals ?? 0 }})
      </button>
      <button
        v-for="b in levels"
        :key="b.level"
        type="button"
        class="pf-seg-item"
        :class="{ active: selectedLevel === b.level }"
        @click="emit('select', selectedLevel === b.level ? null : b.level)"
      >
        {{ b.label }} ({{ b.count }})
      </button>
    </div>

    <!-- 分布条（全部账户合并成一条） -->
    <div class="ai-tp-body">
      <div class="ai-tp-main">
        <div v-if="levels.length" class="ai-tp-node">
          <p class="ai-tp-node-head">
            <span class="ai-tp-node-name">止盈档位分布</span>
            <span class="pf-count">{{ dist?.closed_tp_units ?? 0 }} 个已止盈单元</span>
          </p>
          <div class="ai-tp-bar">
            <span
              v-for="b in levels"
              :key="b.level"
              class="ai-tp-seg"
              :class="`lv${b.level}`"
              :style="{ width: b.pct + '%' }"
              :title="`${b.label}：${b.count} 个（${b.pct}%）`"
            />
          </div>
          <p class="ai-tp-legend">
            <span v-for="b in levels" :key="b.level">
              <i class="ai-tp-dot" :class="`lv${b.level}`" />{{ b.label }} {{ b.count }}
            </span>
          </p>
        </div>
        <p v-else class="pf-empty">暂无已止盈的统计单元</p>
      </div>

      <!-- 状态汇总 + 该档位的 TOP5 -->
      <div class="ai-tp-side">
        <div class="ai-tp-states">
          <div v-for="s in stateSummary" :key="s.key" class="ai-tp-state">
            <span class="ai-tp-state-k">{{ s.label }}</span>
            <span class="ai-tp-state-v">{{ s.value }}</span>
          </div>
        </div>
        <div style="margin-top: 12px">
          <TopSignalList :title="topTitle" :rows="topRows" hint="按质量分降序" />
        </div>
      </div>
    </div>
  </div>
</template>
