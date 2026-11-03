<script setup lang="ts">
// 信号中心筛选条。
//
// **四个接口共用同一套筛选语义**（后端 `signal_stats.SignalFilters`），
// 所以筛选状态由父组件持有，这里只负责编辑并 emit 变更。
import { computed } from 'vue'
import type { SignalCenterQuery } from '@/api/types'

const props = defineProps<{
  query: SignalCenterQuery
  nodes?: { node_id: string; name?: string | null }[]
  groups?: { group_id: string; name?: string | null }[]
}>()

const emit = defineEmits<{
  (e: 'update', patch: Partial<SignalCenterQuery>): void
}>()

/** 时间窗口：与全站统计口径一致（7d / 30d / 90d / all），不另立一套 */
const WINDOWS = [
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
  { value: '90d', label: '90 天' },
  { value: 'all', label: '全部' },
]

const SOURCES = [
  { value: '', label: '全部来源' },
  { value: 'tradingview', label: 'TradingView' },
  { value: 'manual', label: '手动触发' },
  { value: 'limit_watch', label: '限价监听' },
]

const MODELS = [
  { value: '', label: '全部链路' },
  { value: 'normal', label: '按币种分发' },
  { value: 'strategy', label: '按分组分发' },
]

const STATUSES = [
  { value: '', label: '全部状态' },
  { value: 'dispatching', label: '分发中' },
  { value: 'done', label: '完成' },
  { value: 'partial', label: '部分成功' },
  { value: 'failed', label: '失败' },
  { value: 'rejected', label: '已拒收' },
]

const currentWindow = computed(() => props.query.window ?? '30d')

function setWindow(v: string): void {
  emit('update', { window: v })
}

function setField(key: keyof SignalCenterQuery, value: string): void {
  emit('update', { [key]: value || null } as Partial<SignalCenterQuery>)
}

function onKeyword(e: Event): void {
  const v = (e.target as HTMLInputElement).value
  emit('update', { keyword: v.trim() || null })
}

function clearAll(): void {
  emit('update', {
    window: '30d',
    source: null,
    model: null,
    symbol: null,
    action: null,
    status: null,
    node_id: null,
    group_id: null,
    keyword: null,
  })
}

const hasFilter = computed(() => {
  const q = props.query
  return Boolean(
    q.source || q.model || q.symbol || q.action || q.status ||
    q.node_id || q.group_id || q.keyword,
  )
})
</script>

<template>
  <div class="pf-card">
    <!-- 时间窗口 -->
    <div class="pf-filter-bar" style="margin-bottom: 10px">
      <div class="pf-filter-group">
        <span class="pf-filter-label">窗口</span>
        <div class="pf-seg">
          <button
            v-for="w in WINDOWS"
            :key="w.value"
            class="pf-seg-item"
            :class="{ active: currentWindow === w.value }"
            @click="setWindow(w.value)"
          >
            {{ w.label }}
          </button>
        </div>
      </div>

      <button
        v-if="hasFilter"
        class="pf-btn pf-btn-mini"
        style="margin-left: auto"
        @click="clearAll"
      >
        清除筛选
      </button>
    </div>

    <!-- 其它筛选 -->
    <div class="pf-filter-bar">
      <select
        class="pf-filter-input select"
        :value="query.source ?? ''"
        @change="setField('source', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="s in SOURCES" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>

      <select
        class="pf-filter-input select"
        :value="query.model ?? ''"
        @change="setField('model', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="m in MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
      </select>

      <select
        class="pf-filter-input select"
        :value="query.status ?? ''"
        @change="setField('status', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="s in STATUSES" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>

      <select
        v-if="nodes?.length"
        class="pf-filter-input select"
        :value="query.node_id ?? ''"
        @change="setField('node_id', ($event.target as HTMLSelectElement).value)"
      >
        <option value="">全部节点</option>
        <option v-for="n in nodes" :key="n.node_id" :value="n.node_id">
          {{ n.name || n.node_id }}
        </option>
      </select>

      <select
        v-if="groups?.length"
        class="pf-filter-input select"
        :value="query.group_id ?? ''"
        @change="setField('group_id', ($event.target as HTMLSelectElement).value)"
      >
        <option value="">全部分组</option>
        <option v-for="g in groups" :key="g.group_id" :value="g.group_id">
          {{ g.name || g.group_id }}
        </option>
      </select>

      <input
        class="pf-filter-input"
        style="flex: 1 1 140px"
        type="search"
        placeholder="搜索信号号 / 品种 / 备注"
        :value="query.keyword ?? ''"
        @change="onKeyword"
      />
    </div>
  </div>
</template>
