<script setup lang="ts">
// 信号中心筛选条。
//
// **所有接口共用同一套筛选语义**（后端 `signal_stats.SignalFilters`），
// 所以筛选状态由父组件持有，这里只负责编辑并 emit 变更。
//
// 后四个维度（周期 / 信号类型 / 路由状态 / 方向）是信号中心路由中枢与
// 绩效面板新增的：
// - 周期 / 类型：TV 告警带来的统计维度；
// - 路由状态：待路由 / 已直连 / 已下发 / 已丢弃；
// - 方向：整合中控台的「只做多 / 只做空」。
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

/** 信号级周期（与后端 `signal_meta.TIMEFRAMES` 同源） */
const TIMEFRAMES = [
  { value: '', label: '全部周期' },
  { value: 'M1', label: 'M1' },
  { value: 'M5', label: 'M5' },
  { value: 'M15', label: 'M15' },
  { value: 'M30', label: 'M30' },
  { value: 'H1', label: 'H1' },
  { value: 'H4', label: 'H4' },
  { value: 'D1', label: 'D1' },
  { value: 'W1', label: 'W1' },
]

/** 首仓信号类型（与后端 `signal_meta.SIGNAL_TYPES` 同源） */
const SIGNAL_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'ob', label: '订单块 OB' },
  { value: 'fvg', label: '公允价值缺口 FVG' },
  { value: 'pdz', label: '溢价折让区 PDZ' },
  { value: 'fibo', label: '斐波那契' },
]

/** 路由状态 */
const ROUTE_STATES = [
  { value: '', label: '全部路由状态' },
  { value: 'pending', label: '待路由' },
  { value: 'auto', label: '已直连' },
  { value: 'routed', label: '已中心下发' },
  { value: 'dropped', label: '已丢弃' },
]

/** 多空方向（整合中控台） */
const DIRECTIONS = [
  { value: '', label: '全部方向' },
  { value: 'buy', label: '只做多' },
  { value: 'sell', label: '只做空' },
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
    timeframe: null,
    signal_type: null,
    route_state: null,
    direction: null,
  })
}

const hasFilter = computed(() => {
  const q = props.query
  return Boolean(
    q.source || q.model || q.symbol || q.action || q.status ||
    q.node_id || q.group_id || q.keyword ||
    q.timeframe || q.signal_type || q.route_state || q.direction,
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

    <!-- 周期 / 类型 / 路由状态 / 方向 -->
    <div class="pf-filter-bar">
      <select
        class="pf-filter-input select"
        :value="query.timeframe ?? ''"
        @change="setField('timeframe', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="t in TIMEFRAMES" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>

      <select
        class="pf-filter-input select"
        :value="query.signal_type ?? ''"
        @change="setField('signal_type', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="t in SIGNAL_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>

      <select
        class="pf-filter-input select"
        :value="query.route_state ?? ''"
        @change="setField('route_state', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="s in ROUTE_STATES" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>

      <select
        class="pf-filter-input select"
        :value="query.direction ?? ''"
        @change="setField('direction', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="d in DIRECTIONS" :key="d.value" :value="d.value">{{ d.label }}</option>
      </select>
    </div>
  </div>
</template>
