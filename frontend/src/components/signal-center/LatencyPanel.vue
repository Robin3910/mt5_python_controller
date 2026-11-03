<script setup lang="ts">
// 时延面板：端到端分位（跨节点可比）+ 节点内两段（分节点展示）。
//
// **口径是这个面板最容易出错的地方**：
// - 端到端 = finished_at - sent_at，两端都是服务端时钟，跨节点可比；
// - 节点排队 = order_at - recv_at、券商往返 = filled_at - order_at，
//   两端都是节点本地时钟，**只在同一节点内部可比**。
//   所以这两段不做全局汇总分位，只按节点列出 —— 混在一起算等于把
//   不同机器的时钟差加在一起，数字漂亮但完全无意义。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { SignalCenterLatency } from '@/api/types'

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  latency: SignalCenterLatency | null
  loading?: boolean
}>()

const COLORS = {
  blue: '#2563eb',
  green: '#22c55e',
  amber: '#f59e0b',
  grid: 'rgba(100, 130, 200, 0.16)',
  muted: '#7b8ba8',
  text: '#e8edf5',
}

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

const daily = computed(() => props.latency?.daily ?? [])
const byNode = computed(() => props.latency?.by_node ?? [])
const slowNodes = computed(() => props.latency?.slow_nodes ?? [])

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function buildOption(): echarts.EChartsCoreOption {
  const labels = daily.value.map((d) => d.date)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 4, right: 4, top: 26, bottom: 4, containLabel: true },
    legend: {
      data: ['P50', 'P90'],
      textStyle: { color: COLORS.muted, fontSize: 10 },
      top: 0,
      itemWidth: 10,
      itemHeight: 10,
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(100, 130, 200, 0.24)',
      textStyle: { color: COLORS.text, fontSize: 11 },
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: COLORS.grid } },
      axisTick: { show: false },
      axisLabel: { color: COLORS.muted, fontSize: 9, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitLine: { lineStyle: { color: COLORS.grid, type: 'dashed' } },
      axisLine: { show: false },
      axisLabel: {
        color: COLORS.muted,
        fontSize: 9,
        formatter: (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`),
      },
    },
    series: [
      {
        type: 'line',
        name: 'P50',
        data: daily.value.map((d) => d.p50),
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2, color: COLORS.green },
        itemStyle: { color: COLORS.green },
      },
      {
        type: 'line',
        name: 'P90',
        data: daily.value.map((d) => d.p90),
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2, color: COLORS.amber },
        itemStyle: { color: COLORS.amber },
      },
    ],
  }
}

function render(): void {
  if (!chartEl.value || !daily.value.length) {
    chart?.clear()
    return
  }
  if (!chart) {
    const { offsetWidth, offsetHeight } = chartEl.value
    if (!offsetWidth || !offsetHeight) return
    chart = echarts.init(chartEl.value)
  }
  chart.setOption(buildOption(), true)
}

watch(() => props.latency, () => render(), { deep: true, flush: 'post' })

onMounted(() => {
  render()
  if (chartEl.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      if (!chart) render()
      else chart.resize()
    })
    resizeObserver.observe(chartEl.value)
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = undefined
})
</script>

<template>
  <div>
    <!-- 分位总览 -->
    <div class="pf-lat-grid" style="margin-bottom: 12px">
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>端到端 P50</span>
          <span class="pf-pill blue">跨节点可比</span>
        </p>
        <p class="pf-lat-v">{{ fmtMs(latency?.end_to_end.p50) }}</p>
        <p class="pf-lat-sub">样本 {{ latency?.end_to_end.samples ?? 0 }}</p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>端到端 P90</span>
          <span class="pf-pill blue">跨节点可比</span>
        </p>
        <p class="pf-lat-v">{{ fmtMs(latency?.end_to_end.p90) }}</p>
        <p class="pf-lat-sub">样本 {{ latency?.end_to_end.samples ?? 0 }}</p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>端到端 P99</span>
          <span class="pf-pill blue">跨节点可比</span>
        </p>
        <p class="pf-lat-v">{{ fmtMs(latency?.end_to_end.p99) }}</p>
        <p class="pf-lat-sub">服务端时钟</p>
      </div>
      <div class="pf-lat-card">
        <p class="pf-lat-k">
          <span>节点内排队 P50</span>
          <span class="pf-pill amber">仅节点内</span>
        </p>
        <p class="pf-lat-v">{{ fmtMs(latency?.queued_ms.p50) }}</p>
        <p class="pf-lat-sub">不可跨节点求和</p>
      </div>
    </div>

    <!-- 趋势 -->
    <div class="pf-card" style="margin-bottom: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />端到端时延趋势</h3>
        <span class="pf-count">UTC+8 自然日</span>
      </div>
      <div v-if="daily.length" ref="chartEl" class="pf-chart pf-chart-200" />
      <p v-else class="pf-empty">暂无数据</p>
    </div>

    <div class="pf-grid-2">
      <!-- 慢节点 -->
      <div class="pf-card">
        <div class="pf-section-head" style="margin-bottom: 10px">
          <h3 class="pf-section-title"><span class="pf-bar" />慢节点（端到端 P90）</h3>
        </div>
        <template v-if="slowNodes.length">
          <div v-for="n in slowNodes" :key="n.node_id" class="sc-slow-row">
            <span class="c-mono">{{ n.node_id }}</span>
            <span class="sc-slow-bar">
              <span
                class="sc-slow-fill"
                :style="{
                  width: `${Math.min(100, ((n.p90 || 0) / (slowNodes[0].p90 || 1)) * 100)}%`,
                }"
              />
            </span>
            <span class="c-mono">{{ fmtMs(n.p90) }}</span>
          </div>
        </template>
        <p v-else class="pf-empty">暂无数据</p>
      </div>

      <!-- 节点内两段 -->
      <div class="pf-card">
        <div class="pf-section-head" style="margin-bottom: 10px">
          <h3 class="pf-section-title"><span class="pf-bar" />节点内耗时（分节点）</h3>
          <span class="pf-count">仅节点内可比</span>
        </div>
        <template v-if="byNode.length">
          <table class="sc-lat-table">
            <thead>
              <tr>
                <th>节点</th>
                <th class="right">排队 P50</th>
                <th class="right">排队 P90</th>
                <th class="right">券商 P50</th>
                <th class="right">券商 P90</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="n in byNode" :key="n.node_id">
                <td class="c-mono">{{ n.node_id }}</td>
                <td class="right">{{ fmtMs(n.queued_p50) }}</td>
                <td class="right">{{ fmtMs(n.queued_p90) }}</td>
                <td class="right">{{ fmtMs(n.fill_p50) }}</td>
                <td class="right">{{ fmtMs(n.fill_p90) }}</td>
              </tr>
            </tbody>
          </table>
        </template>
        <p v-else class="pf-empty">暂无数据</p>
      </div>
    </div>

    <div class="pf-scope-note" style="margin-top: 12px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 9v4M12 17h.01" />
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>{{ latency?.note }}</span>
    </div>
  </div>
</template>

<style scoped>
.sc-slow-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  font-size: 11px;
  border-bottom: 1px solid var(--pf-border-2);
}
.sc-slow-row:last-child { border-bottom: none; }
.sc-slow-row > span:first-child {
  width: 96px;
  flex-shrink: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sc-slow-row > span:last-child {
  width: 56px;
  flex-shrink: 0;
  text-align: right;
}

.sc-slow-bar {
  flex: 1;
  min-width: 0;
  height: 6px;
  border-radius: 999px;
  background: #0a0f1a;
  overflow: hidden;
}
.sc-slow-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #f59e0b, #f6465d);
}

.sc-lat-table {
  width: 100%;
  border-collapse: collapse;
}
.sc-lat-table th,
.sc-lat-table td {
  padding: 5px 6px;
  font-size: 11px;
  text-align: left;
  border-bottom: 1px solid var(--pf-border-2);
}
.sc-lat-table th {
  color: var(--pf-dim);
  font-weight: 500;
  font-size: 10px;
}
.sc-lat-table .right { text-align: right; }
.sc-lat-table tr:last-child td { border-bottom: none; }
</style>
