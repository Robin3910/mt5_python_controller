<script setup lang="ts">
// 节点详情「概览」里的历史盈亏：汇总数字 + 按日收益柱状图。
//
// 数据源是 **MT5 真实成交**（mt5_deal 表，节点上报），
// 覆盖策略任务、普通信号分发、终端手工单全部来源 —— 与只看策略任务
// 累计值的口径不同，这是「账户真实盈亏」。
//
// 口径要点：
// - 统计口径全部在后端 deal_stats 一处实现，前端**不再算一遍** ——
//   前端自己算必然与明细页对不上，那会直接摧毁页面的数据可信度；
// - 胜率按**持仓级**聚合（position_id），不是按成交笔数；
// - 「没有数据」与「有数据但全亏」是两件事，用 data_available 区分。
//
// 组件自己拉数据（父组件只给 nodeId），这样切到概览时按需请求，
// 且图表库不进节点详情页首屏 chunk（与 TrendPanel 同样的处理方式）。
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { NodePerformance } from '@/api/types'

// 图表组件异步加载：echarts 不进节点详情页首屏 chunk（与 TrendPanel 同样处理）
const ProfitBarChart = defineAsyncComponent(() => import('@/components/ProfitBarChart.vue'))

const props = withDefaults(
  defineProps<{
    nodeId: string
    /** 统计窗口：7d / 30d / 90d / all */
    window?: string
    /** 柱状图天数（固定，不随统计窗口变化） */
    days?: number
  }>(),
  { window: '30d', days: 7 },
)

const hub = useHubStore()
// 窗口是本地状态：props 只作为初值，用户切换后由本地 ref 驱动
const windowKey = ref(props.window)
const data = ref<NodePerformance | null>(null)
const loading = ref(false)
const failed = ref(false)

const WINDOW_OPTIONS = [
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
  { value: '90d', label: '90 天' },
  { value: 'all', label: '全部' },
]

const summary = computed(() => data.value?.summary ?? null)
const hasData = computed(() => summary.value?.data_available === true)

function fmtAmount(v: number): string {
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return `${sign}$${Math.abs(v ?? 0).toFixed(2)}`
}

function fmtPct(v: number): string {
  return `${(v ?? 0).toFixed(2)}%`
}

async function load(): Promise<void> {
  if (!props.nodeId) return
  loading.value = true
  failed.value = false
  try {
    const res = await hub.fetchNodePerformance(props.nodeId, windowKey.value, props.days)
    if (res) {
      data.value = res
    } else {
      failed.value = true
    }
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.nodeId, windowKey.value, props.days],
  () => {
    void load()
  },
)

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="perf-panel">
    <div class="row between perf-head">
      <div>
        <strong>历史盈亏</strong>
        <p class="muted perf-desc">MT5 真实成交的已实现盈亏（含策略 / 信号 / 手工单）</p>
      </div>
      <div class="perf-windows">
        <button
          v-for="opt in WINDOW_OPTIONS"
          :key="opt.value"
          class="perf-win"
          :class="{ active: windowKey === opt.value }"
          :disabled="loading"
          @click="windowKey = opt.value"
        >
          {{ opt.label }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="muted perf-state">加载中…</div>
    <div v-else-if="failed" class="muted perf-state">读取历史盈亏失败，请稍后重试</div>
    <div v-else-if="!hasData" class="muted perf-state">
      该节点还没有上报成交记录（需节点客户端开启成交同步）
    </div>
    <template v-else>
      <div class="perf-stats">
        <div class="perf-stat">
          <span class="k">净盈亏</span>
          <span class="v" :class="(summary?.total_profit ?? 0) >= 0 ? 'profit-pos' : 'profit-neg'">
            {{ fmtAmount(summary?.total_profit ?? 0) }}
          </span>
        </div>
        <div class="perf-stat">
          <span class="k">收益率</span>
          <span
            class="v"
            :class="(summary?.profit_rate ?? 0) >= 0 ? 'profit-pos' : 'profit-neg'"
          >
            {{ summary?.profit_rate_valid ? fmtPct(summary?.profit_rate ?? 0) : '—' }}
          </span>
        </div>
        <div class="perf-stat">
          <span class="k">胜率</span>
          <span class="v">{{ fmtPct(summary?.win_rate ?? 0) }}</span>
        </div>
        <div class="perf-stat">
          <span class="k">交易</span>
          <span class="v">{{ summary?.total_trades ?? 0 }} 笔</span>
        </div>
      </div>
      <div class="perf-sub">
        <span>盈 {{ summary?.win_trades ?? 0 }}</span>
        <span>亏 {{ summary?.lose_trades ?? 0 }}</span>
        <span>手续费 {{ fmtAmount(summary?.commission_total ?? 0) }}</span>
        <span>库存费 {{ fmtAmount(summary?.swap_total ?? 0) }}</span>
      </div>
      <ProfitBarChart
        :points="data?.daily ?? []"
        :days="days"
        :height="170"
      />
    </template>
  </div>
</template>

<style scoped>
.perf-head {
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}
.perf-desc {
  margin: 4px 0 0;
  font-size: 12px;
}
.perf-windows {
  display: flex;
  gap: 4px;
  flex: 0 0 auto;
}
.perf-win {
  padding: 3px 9px;
  font-size: 11px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--glass-border);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.perf-win.active {
  color: var(--text);
  border-color: var(--accent);
}
.perf-win:disabled {
  opacity: 0.5;
  cursor: default;
}
.perf-state {
  font-size: 13px;
  padding: 18px 0;
  text-align: center;
}
.perf-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 8px;
}
.perf-stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.perf-stat .k {
  font-size: 11px;
  color: var(--muted);
}
.perf-stat .v {
  font-family: var(--mono);
  font-size: 15px;
  font-weight: 700;
}
.perf-sub {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 12px;
}
@media (max-width: 640px) {
  .perf-stats {
    grid-template-columns: repeat(2, 1fr);
  }
  .perf-head {
    flex-direction: column;
    gap: 8px;
  }
}
</style>
