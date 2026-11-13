<script setup lang="ts">
// **AI 信号管理中心**（原型复刻版）
//
// 单页多区块看板，1:1 对齐原型（灵光闪应用原型【我的】→【AI信号管理中心】）：
//   页头 → 口径说明 → 4 指标卡 → 信号质量总览 → 类型质量对比 →
//   周期质量总览 → 质量 TOP5 → 马丁止盈分布 → 信号明细表
//
// 旧版的【概览 / 信号流 / 节点命中 / 策略任务 / 时延】**已移除**（原型没有、用户不要）；
// 旧版的【绩效 / 路由】**保留在 `SignalCenterLegacyView.vue`**，暂时不挂路由，未来可恢复。
//
// 设计与口径：`docs/superpowers/specs/2026-11-13-ai-signal-center-prototype-design.md`
import { computed, onMounted, ref, watch } from 'vue'
import AiKpiRow from '@/components/signal-center/ai/AiKpiRow.vue'
import AiPageHeader from '@/components/signal-center/ai/AiPageHeader.vue'
import AiSignalTable from '@/components/signal-center/ai/AiSignalTable.vue'
import MartingaleTpDistribution from '@/components/signal-center/ai/MartingaleTpDistribution.vue'
import QualityOverview from '@/components/signal-center/ai/QualityOverview.vue'
import TimeframeQuality from '@/components/signal-center/ai/TimeframeQuality.vue'
import TopSignalList from '@/components/signal-center/ai/TopSignalList.vue'
import TypeQualityCompare from '@/components/signal-center/ai/TypeQualityCompare.vue'
import { useHubStore } from '@/stores/hub'
import type {
  AiSignalList,
  AiSignalOverview,
  AiTpDistribution,
  SignalCenterQuery,
} from '@/api/types'

const hub = useHubStore()

/** 顶部窗口选择（原型「当前档位」） */
const windowKey = ref('30d')
const query = computed<SignalCenterQuery>(() => ({ window: windowKey.value }))

const WINDOWS: { key: string; label: string }[] = [
  { key: '7d', label: '近 7 天' },
  { key: '30d', label: '近 30 天' },
  { key: '90d', label: '近 90 天' },
  { key: 'all', label: '全部' },
]

const overview = ref<AiSignalOverview | null>(null)
const tpDist = ref<AiTpDistribution | null>(null)
const list = ref<AiSignalList | null>(null)

const loading = ref(false)
const loadingList = ref(false)

// —— 联动筛选（点击周期卡 / 类型卡 / 止盈档位 → 过滤明细表）——
const linkedTimeframe = ref<string | null>(null)
/** 选中的**业务分类**（结构分析 = ob + fvg + pdz），后端按分类展开成 IN 过滤 */
const linkedGroup = ref<string | null>(null)
const linkedLevel = ref<number | null>(null)

const page = ref(1)
const pageSize = ref(10)

/** 联动筛选写进查询条件 */
const listQuery = computed<SignalCenterQuery>(() => ({
  ...query.value,
  timeframe: linkedTimeframe.value ?? undefined,
}))

const scopeLabel = computed(() =>
  WINDOWS.find((w) => w.key === windowKey.value)?.label ?? '全部',
)

const updatedAt = computed(() => overview.value?.updated_at ?? null)

async function loadOverview(): Promise<void> {
  loading.value = true
  try {
    const [ov, dist] = await Promise.all([
      hub.fetchAiSignalOverview(query.value),
      hub.fetchAiTpDistribution(query.value),
    ])
    overview.value = ov
    tpDist.value = dist
  } finally {
    loading.value = false
  }
}

async function loadList(): Promise<void> {
  loadingList.value = true
  try {
    list.value = await hub.fetchAiSignals({
      query: listQuery.value,
      page: page.value,
      page_size: pageSize.value,
      tp_level: linkedLevel.value,
      type_group: linkedGroup.value,
    })
  } finally {
    loadingList.value = false
  }
}

/** 联动筛选一变 → 回到第一页重拉明细 */
function resetAndLoad(): void {
  page.value = 1
  void loadList()
}

watch(windowKey, async () => {
  await loadOverview()
  resetAndLoad()
})

watch([linkedTimeframe, linkedGroup, linkedLevel], resetAndLoad)

function onTypeSelect(group: string | null): void {
  linkedGroup.value = group
}

function onTimeframeSelect(key: string | null): void {
  linkedTimeframe.value = key
}

function onLevelSelect(level: number | null): void {
  linkedLevel.value = level
}

function clearFilters(): void {
  linkedTimeframe.value = null
  linkedGroup.value = null
  linkedLevel.value = null
}

function onPageSize(size: number): void {
  pageSize.value = size
  resetAndLoad()
}

async function refreshAll(): Promise<void> {
  await loadOverview()
  await loadList()
}

onMounted(refreshAll)
</script>

<template>
  <div class="pf-page sc-page">
    <!-- 页头 -->
    <AiPageHeader
      :scope-label="scopeLabel"
      :updated-at="updatedAt"
      :loading="loading"
      :scope="overview?.scope"
      @refresh="refreshAll"
    />

    <!-- 窗口档位 -->
    <div class="pf-seg" style="margin-top: 12px">
      <button
        v-for="w in WINDOWS"
        :key="w.key"
        type="button"
        class="pf-seg-item"
        :class="{ active: windowKey === w.key }"
        @click="windowKey = w.key"
      >
        {{ w.label }}
      </button>
    </div>

    <!-- 四个指标卡 -->
    <AiKpiRow :kpi="overview?.kpi ?? null" />

    <!-- 信号质量总览 -->
    <QualityOverview
      :reliability="overview?.reliability ?? []"
      :score-buckets="overview?.score_buckets ?? []"
      :loading="loading"
    />

    <!-- 信号类型质量对比 -->
    <TypeQualityCompare
      :rows="overview?.by_type ?? []"
      :selected="linkedGroup"
      :loading="loading"
      @select="onTypeSelect"
    />

    <!-- 周期质量总览 -->
    <TimeframeQuality
      :rows="overview?.by_timeframe ?? []"
      :selected="linkedTimeframe"
      :loading="loading"
      @select="onTimeframeSelect"
    />

    <!-- 重点关注 · 质量 TOP 5 -->
    <div class="pf-card">
      <TopSignalList
        title="重点关注 · 质量 TOP 5"
        :rows="overview?.top_signals ?? []"
        hint="按质量分降序"
        :loading="loading"
      />
    </div>

    <!-- 马丁止盈分布 -->
    <MartingaleTpDistribution
      :dist="tpDist"
      :selected-level="linkedLevel"
      :loading="loading"
      @select="onLevelSelect"
    />

    <!-- 信号明细 -->
    <AiSignalTable
      :list="list"
      :loading="loadingList"
      :selected-timeframe="linkedTimeframe"
      :selected-group="linkedGroup"
      :selected-level="linkedLevel"
      :page-size="pageSize"
      @page="(p) => { page = p; void loadList() }"
      @page-size="onPageSize"
      @clear-filter="clearFilters"
    />

    <!-- 页脚口径 -->
    <p v-if="list?.scope" class="ai-footer">{{ list.scope }}</p>
  </div>
</template>

<style scoped>
.sc-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
</style>
