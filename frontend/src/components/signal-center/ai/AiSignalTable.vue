<script setup lang="ts">
// 信号明细表：原型下半屏。
//
// 列：周期 / 信号 / 方向 / 品种 / 触发时间 / 马丁止盈 / 质量分 / 可靠性 / 决策指导
//
// **分页用页码**（不是游标）：原型是「第 N / M 页 + 上一页/下一页 + 每页 10/20/50 条」。
// 信号是历史数据、写入频率低，offset 漂移可接受 —— 不为此引入第二套分页语义。
import { computed } from 'vue'
import type { AiSignalList } from '@/api/types'
import { LEVEL_TONE, fmtTime, tpLevelLabel, tpStateLabel } from '@/constants/signalCenter'

const props = defineProps<{
  list: AiSignalList | null
  loading?: boolean
  /** 当前选中的周期（联动筛选） */
  selectedTimeframe: string | null
  /** 当前选中的业务分类（联动筛选） */
  selectedGroup: string | null
  /** 当前选中的止盈档位（联动筛选，null = 不限） */
  selectedLevel: number | null
  pageSize: number
}>()

const emit = defineEmits<{
  (e: 'page', page: number): void
  (e: 'page-size', size: number): void
  (e: 'clear-filter'): void
}>()

const items = computed(() => props.list?.items ?? [])
const page = computed(() => props.list?.page ?? 1)
const pages = computed(() => props.list?.pages ?? 1)
const total = computed(() => props.list?.total ?? 0)

const PAGE_SIZES = [10, 20, 50]

/** 当前生效的联动筛选（用于顶部提示条） */
const activeFilters = computed(() => {
  const out: string[] = []
  if (props.selectedTimeframe) out.push(`周期 ${props.selectedTimeframe}`)
  if (props.selectedGroup) out.push(`类型 ${props.selectedGroup}`)
  if (props.selectedLevel !== null) out.push(`${props.selectedLevel}单止盈`)
  return out
})

function go(delta: number): void {
  const next = page.value + delta
  if (next < 1 || next > pages.value) return
  emit('page', next)
}
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 12px">
      <h3 class="pf-section-title"><span class="pf-bar" />信号明细</h3>
      <span class="pf-count">共 {{ total }} 条</span>
    </div>

    <!-- 联动筛选提示 + 每页条数 -->
    <div class="ai-table-bar">
      <div class="ai-table-filters">
        <span v-if="!activeFilters.length" class="ai-dim">未筛选 · 展示全部信号</span>
        <template v-else>
          <span class="ai-dim">已筛选：</span>
          <span v-for="f in activeFilters" :key="f" class="pf-pill blue">{{ f }}</span>
          <button type="button" class="pf-btn pf-btn-mini" @click="emit('clear-filter')">
            清除筛选
          </button>
        </template>
      </div>
      <div class="pf-seg">
        <button
          v-for="s in PAGE_SIZES"
          :key="s"
          type="button"
          class="pf-seg-item mini"
          :class="{ active: pageSize === s }"
          @click="emit('page-size', s)"
        >
          {{ s }} 条/页
        </button>
      </div>
    </div>

    <!-- 表格 -->
    <div class="ai-table-wrap">
      <table class="ai-table">
        <thead>
          <tr>
            <th>周期</th>
            <th>信号</th>
            <th>方向</th>
            <th>品种</th>
            <th>触发时间</th>
            <th>马丁止盈</th>
            <th>质量分</th>
            <th>可靠性</th>
            <th>决策指导</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.signal_id">
            <td>{{ row.timeframe_label }}</td>
            <td class="ai-cell-name">{{ row.name }}</td>
            <td>
              <span
                class="pf-pill"
                :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'red' : 'grey'"
              >
                {{ row.action_label }}
              </span>
            </td>
            <td class="c-mono">{{ row.symbol || '—' }}</td>
            <td class="c-mono ai-dim">{{ fmtTime(row.received_at) }}</td>
            <td>
              <span v-if="row.tp_level" class="pf-pill blue">{{ tpLevelLabel(row.tp_level) }}</span>
              <span v-else class="ai-dim">{{ tpStateLabel(row.tp_state) }}</span>
            </td>
            <td>
              <span v-if="row.score !== null" class="ai-score" :class="LEVEL_TONE[row.level]">
                {{ row.score.toFixed(1) }}
              </span>
              <span v-else class="ai-dim">暂无历史</span>
            </td>
            <td>
              <span class="pf-pill" :class="LEVEL_TONE[row.level]">
                {{ row.level === 'none' ? '—' : row.level === 'high' ? '高' : row.level === 'mid' ? '中' : '低' }}
              </span>
            </td>
            <td :class="row.level === 'high' ? 'ai-good' : 'ai-dim'">{{ row.advice }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!items.length && !loading" class="pf-empty">该条件下暂无信号</p>
    </div>

    <!-- 分页 -->
    <div class="ai-pager">
      <span class="ai-dim">第 {{ page }} / {{ pages }} 页</span>
      <div class="ai-pager-btns">
        <button type="button" class="pf-btn pf-btn-mini" :disabled="page <= 1" @click="go(-1)">
          上一页
        </button>
        <button type="button" class="pf-btn pf-btn-mini" :disabled="page >= pages" @click="go(1)">
          下一页
        </button>
      </div>
    </div>
  </div>
</template>
