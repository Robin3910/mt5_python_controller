<script setup lang="ts">
// **信号中心 · 旧版（保留，不挂路由）**
//
// 这个文件是 AI 信号管理中心上线前的原信号中心页面，**整体保留、一行功能未删**。
//
// 保留它的原因：原页面里的【绩效】【路由】两块功能（`PerformancePanel.vue` /
// `RoutingPanel.vue`）是**已实现但暂时隐藏**的功能，未来要恢复时只需把路由指回本组件。
//
// **当前状态**：路由 `/events` 与 `/signal-center` 都指向新页面
// `SignalCenterView.vue`（原型复刻版）。本组件**不在路由表里** ——
// 想恢复时把 `frontend/src/router/index.ts` 里这两条路由的 component
// 从 `@/views/SignalCenterView.vue` 改回 `@/views/SignalCenterLegacyView.vue` 即可。
//
// 原页面：把「信号 → 受理 → 分发 → 节点处理 → 成交」这条链路
// 从原来分散在【事件】【分组管理】【成交明细】三个页面，收敛成一条可下钻的时间线。
//
// 设计要点（与 spec 对齐）：
// 1. 七个频道（概览 / 信号流 / 节点命中 / 策略任务 / 时延 / 绩效 / 路由）共享同一套筛选条件；
// 2. 筛选条件是「全局的」，切 tab 不重置（用户在对比不同维度）；
// 3. 分页用游标，不用页码（实时数据写入会让 offset 漂移）；
// 4. 盈亏口径分区展示：策略任务的 realized_profit 与成交侧净盈亏不合并；
// 5. **绩效与路由是两个不同口径的面板**，各自标注口径，不混在一张表里。
import { computed, onMounted, ref, watch } from 'vue'
import SignalFilters from '@/components/signal-center/SignalFilters.vue'
import SignalHero from '@/components/signal-center/SignalHero.vue'
import SignalOverviewCharts from '@/components/signal-center/SignalOverviewCharts.vue'
import SignalStreamTable from '@/components/signal-center/SignalStreamTable.vue'
import NodeHitCards from '@/components/signal-center/NodeHitCards.vue'
import StrategyTaskPanel from '@/components/signal-center/StrategyTaskPanel.vue'
import LatencyPanel from '@/components/signal-center/LatencyPanel.vue'
import PerformancePanel from '@/components/signal-center/PerformancePanel.vue'
import RoutingPanel from '@/components/signal-center/RoutingPanel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  SignalCenterLatency,
  SignalCenterNodes,
  SignalCenterOverview,
  SignalCenterPerformance,
  SignalCenterQuality,
  SignalCenterQuery,
  SignalCenterStream,
  SignalCenterTasks,
  SignalStreamItem,
} from '@/api/types'

const hub = useHubStore()

type TabKey = 'overview' | 'stream' | 'nodes' | 'tasks' | 'latency' | 'performance' | 'routing'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'stream', label: '信号流' },
  { key: 'nodes', label: '节点命中' },
  { key: 'tasks', label: '策略任务' },
  { key: 'latency', label: '时延' },
  { key: 'performance', label: '绩效' },
  { key: 'routing', label: '路由' },
]

const tab = ref<TabKey>('overview')

/** 全局筛选：切 tab 保留，方便横向对比 */
const query = ref<SignalCenterQuery>({ window: '30d' })

const overview = ref<SignalCenterOverview | null>(null)
const stream = ref<SignalCenterStream>({ items: [], next_cursor: null })
const nodes = ref<SignalCenterNodes | null>(null)
const tasks = ref<SignalCenterTasks | null>(null)
const latency = ref<SignalCenterLatency | null>(null)
const performance = ref<SignalCenterPerformance | null>(null)
const quality = ref<SignalCenterQuality | null>(null)

const loadingOverview = ref(false)
const loadingStream = ref(false)
const loadingNodes = ref(false)
const loadingTasks = ref(false)
const loadingLatency = ref(false)
const loadingPerformance = ref(false)

const streamItems = computed<SignalStreamItem[]>(() => stream.value.items ?? [])

/** 节点 / 分组下拉的候选项（用名称，不用裸 id） */
const nodeOptions = computed(() =>
  hub.nodes.map((n) => ({ node_id: n.node_id, name: n.name })),
)
const groupOptions = computed(() =>
  hub.groups.map((g) => ({ group_id: g.group_id, name: g.name })),
)

function onFilterUpdate(patch: Partial<SignalCenterQuery>): void {
  query.value = { ...query.value, ...patch }
}

/** 筛选变化：概览 / 节点 / 任务 / 时延 / 绩效都要重算，信号流重置到第一页 */
watch(
  () => ({ ...query.value }),
  () => {
    void loadOverview()
    void loadNodes()
    void loadTasks()
    void loadLatency()
    void loadPerformance()
    void loadQuality()
    void resetStream()
  },
  { deep: true },
)

async function loadOverview(): Promise<void> {
  loadingOverview.value = true
  try {
    overview.value = await hub.fetchSignalCenterOverview(query.value)
  } finally {
    loadingOverview.value = false
  }
}

async function loadNodes(): Promise<void> {
  loadingNodes.value = true
  try {
    nodes.value = await hub.fetchSignalCenterNodes(query.value)
  } finally {
    loadingNodes.value = false
  }
}

async function loadTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    tasks.value = await hub.fetchSignalCenterTasks(query.value)
  } finally {
    loadingTasks.value = false
  }
}

async function loadLatency(): Promise<void> {
  loadingLatency.value = true
  try {
    latency.value = await hub.fetchSignalCenterLatency(query.value)
  } finally {
    loadingLatency.value = false
  }
}

async function loadPerformance(): Promise<void> {
  loadingPerformance.value = true
  try {
    performance.value = await hub.fetchSignalCenterPerformance(query.value)
  } finally {
    loadingPerformance.value = false
  }
}

/** 质量分先验表（信号流里「质量」那一列的来源） */
async function loadQuality(): Promise<void> {
  try {
    quality.value = await hub.fetchSignalCenterQuality(query.value)
  } catch {
    quality.value = null
  }
}

async function resetStream(): Promise<void> {
  loadingStream.value = true
  try {
    stream.value = await hub.fetchSignalCenterStream(query.value, null, 20)
  } finally {
    loadingStream.value = false
  }
}

/** 游标翻页：带上前一次的 next_cursor 追加，而不是按页码重查 */
async function loadMoreStream(): Promise<void> {
  if (!stream.value.next_cursor) return
  loadingStream.value = true
  try {
    const next = await hub.fetchSignalCenterStream(
      query.value,
      stream.value.next_cursor,
      20,
    )
    stream.value = {
      items: [...streamItems.value, ...(next.items ?? [])],
      next_cursor: next.next_cursor,
    }
  } finally {
    loadingStream.value = false
  }
}

async function refreshAll(): Promise<void> {
  await Promise.all([
    loadOverview(), loadNodes(), loadTasks(), loadLatency(),
    loadPerformance(), loadQuality(), resetStream(),
  ])
}

onMounted(async () => {
  // 节点 / 分组候选要等 hub 拉完，否则筛选下拉会是空的
  if (!hub.nodes.length) await hub.fetchNodes()
  if (!hub.groups.length) await hub.fetchGroups()
  await refreshAll()
})
</script>

<template>
  <div class="pf-page sc-page">
    <!-- 页头 -->
    <section class="pf-card">
      <div class="pf-card-head">
        <div class="pf-card-head-main">
          <span class="pf-card-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 12h4l3 8 4-16 3 8h4" />
            </svg>
          </span>
          <div>
            <p class="pf-card-title">信号中心</p>
            <p class="pf-card-sub">
              信号受理 → 分发 → 节点处理 → 成交，全链路统一视图（仅管理员可见）
            </p>
          </div>
        </div>
        <button type="button" class="pf-btn" :disabled="loadingOverview" @click="refreshAll">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-3-6.7M21 3v6h-6" />
          </svg>
          刷新
        </button>
      </div>
    </section>

    <!-- 筛选条（全 tab 共享） -->
    <SignalFilters
      :query="query"
      :nodes="nodeOptions"
      :groups="groupOptions"
      @update="onFilterUpdate"
    />

    <!-- 频道切换 -->
    <div class="pf-tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        class="pf-tab"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- 概览 -->
    <div v-show="tab === 'overview'">
      <SignalHero :overview="overview" :loading="loadingOverview" />
      <div style="margin-top: 12px">
        <SignalOverviewCharts :overview="overview" />
      </div>
    </div>

    <!-- 信号流 -->
    <div v-show="tab === 'stream'">
      <SignalStreamTable
        :items="streamItems"
        :loading="loadingStream"
        :has-more="!!stream.next_cursor"
        @load-more="loadMoreStream"
      />
    </div>

    <!-- 节点命中 -->
    <div v-show="tab === 'nodes'">
      <NodeHitCards :items="nodes?.items ?? []" :loading="loadingNodes" />
    </div>

    <!-- 策略任务 -->
    <div v-show="tab === 'tasks'">
      <StrategyTaskPanel :tasks="tasks" :loading="loadingTasks" />
    </div>

    <!-- 时延 -->
    <div v-show="tab === 'latency'">
      <LatencyPanel :latency="latency" :loading="loadingLatency" />
    </div>

    <!-- 绩效 -->
    <div v-show="tab === 'performance'">
      <PerformancePanel
        :performance="performance"
        :quality="quality"
        :loading="loadingPerformance"
      />
    </div>

    <!-- 路由 -->
    <div v-show="tab === 'routing'">
      <RoutingPanel :items="streamItems" :loading="loadingStream" />
    </div>
  </div>
</template>

<style scoped>
.sc-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
</style>
