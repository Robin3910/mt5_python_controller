<script setup lang="ts">
// 概览图表组：趋势（柱+折线双轴）/ 来源环形 / 品种 Top / 拦截原因 Top。
//
// **图表只做可视化**，不做统计：数据全部来自后端 `signal_stats.overview`。
// 若前端自己按 daily 数组重算成功率，趋势图就会与 Hero 的数字对不上。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { SignalCenterOverview } from '@/api/types'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = defineProps<{
  overview: SignalCenterOverview | null
}>()

const COLORS = {
  blue: '#2563eb',
  green: '#22c55e',
  amber: '#f59e0b',
  red: '#f6465d',
  purple: '#a78bfa',
  grid: 'rgba(100, 130, 200, 0.16)',
  muted: '#7b8ba8',
  text: '#e8edf5',
}

const trendEl = ref<HTMLDivElement>()
const sourceEl = ref<HTMLDivElement>()
const symbolEl = ref<HTMLDivElement>()
const skipEl = ref<HTMLDivElement>()

let trendChart: echarts.ECharts | undefined
let sourceChart: echarts.ECharts | undefined
let symbolChart: echarts.ECharts | undefined
let skipChart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

const daily = computed(() => props.overview?.daily ?? [])
const bySource = computed(() => props.overview?.by_source ?? [])
const bySymbol = computed(() => props.overview?.by_symbol ?? [])
const skipReasons = computed(() => props.overview?.skip_reasons ?? [])

const SOURCE_LABELS: Record<string, string> = {
  tradingview: 'TradingView',
  manual: '手动触发',
  limit_watch: '限价监听',
}

const SOURCE_COLORS: Record<string, string> = {
  tradingview: COLORS.blue,
  manual: COLORS.amber,
  limit_watch: COLORS.purple,
}

function init(el: HTMLDivElement | undefined, existing?: echarts.ECharts): echarts.ECharts | undefined {
  if (!el) return undefined
  if (existing) return existing
  const { offsetWidth, offsetHeight } = el
  if (!offsetWidth || !offsetHeight) return undefined
  return echarts.init(el)
}

/**
 * 趋势图：柱（信号量）+ 折线（成功率），双 Y 轴。
 *
 * 折线读的是后端给的 `success_rate`（信号级），不是前端用 done/accepted 重算的
 * ——两者在「部分成功」的口径上可能不同，重算会和 Hero 对不上。
 */
function buildTrend(): echarts.EChartsCoreOption {
  const labels = daily.value.map((d) => d.date)
  const counts = daily.value.map((d) => d.signals)
  const rates = daily.value.map((d) => d.success_rate)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 4, right: 4, top: 26, bottom: 4, containLabel: true },
    legend: {
      data: ['信号量', '成功率'],
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
    yAxis: [
      {
        type: 'value',
        scale: true,
        splitLine: { lineStyle: { color: COLORS.grid, type: 'dashed' } },
        axisLine: { show: false },
        axisLabel: { color: COLORS.muted, fontSize: 9 },
      },
      {
        type: 'value',
        max: 100,
        splitLine: { show: false },
        axisLine: { show: false },
        axisLabel: { color: COLORS.muted, fontSize: 9, formatter: '{value}%' },
      },
    ],
    series: [
      {
        type: 'bar',
        name: '信号量',
        data: counts,
        barMaxWidth: 22,
        itemStyle: { color: COLORS.blue, borderRadius: [3, 3, 0, 0] },
      },
      {
        type: 'line',
        name: '成功率',
        yAxisIndex: 1,
        data: rates,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2, color: COLORS.green },
        itemStyle: { color: COLORS.green },
      },
    ],
  }
}

function buildSource(): echarts.EChartsCoreOption {
  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(100, 130, 200, 0.24)',
      textStyle: { color: COLORS.text, fontSize: 11 },
      formatter: '{b}: {c} 条 ({d}%)',
    },
    legend: {
      bottom: 0,
      textStyle: { color: COLORS.muted, fontSize: 10 },
      itemWidth: 10,
      itemHeight: 10,
    },
    series: [
      {
        type: 'pie',
        radius: ['48%', '72%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        label: { show: false },
        labelLine: { show: false },
        data: bySource.value.map((s) => ({
          name: SOURCE_LABELS[s.source] ?? s.source,
          value: s.count,
          itemStyle: { color: SOURCE_COLORS[s.source] ?? COLORS.blue },
        })),
      },
    ],
  }
}

function buildSymbol(): echarts.EChartsCoreOption {
  const items = bySymbol.value.slice(0, 10)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 4, right: 34, top: 8, bottom: 4, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(100, 130, 200, 0.24)',
      textStyle: { color: COLORS.text, fontSize: 11 },
    },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: COLORS.grid, type: 'dashed' } },
      axisLine: { show: false },
      axisLabel: { color: COLORS.muted, fontSize: 9 },
    },
    yAxis: {
      type: 'category',
      data: items.map((i) => i.symbol).reverse(),
      axisLine: { lineStyle: { color: COLORS.grid } },
      axisTick: { show: false },
      axisLabel: { color: COLORS.muted, fontSize: 9 },
    },
    series: [
      {
        type: 'bar',
        data: items.map((i) => i.count).reverse(),
        barMaxWidth: 16,
        itemStyle: { color: COLORS.blue, borderRadius: [0, 3, 3, 0] },
        label: {
          show: true,
          position: 'right',
          color: COLORS.muted,
          fontSize: 9,
        },
      },
    ],
  }
}

function buildSkip(): echarts.EChartsCoreOption {
  const items = skipReasons.value.slice(0, 10)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 4, right: 34, top: 8, bottom: 4, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(100, 130, 200, 0.24)',
      textStyle: { color: COLORS.text, fontSize: 11 },
      formatter: (params: unknown) => {
        const arr = params as { dataIndex: number }[]
        const first = arr?.[0]
        if (!first) return ''
        const item = items[first.dataIndex]
        return item ? `${item.label}：${item.count} 次` : ''
      },
    },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: COLORS.grid, type: 'dashed' } },
      axisLine: { show: false },
      axisLabel: { color: COLORS.muted, fontSize: 9 },
    },
    yAxis: {
      type: 'category',
      data: items.map((i) => i.label).reverse(),
      axisLine: { lineStyle: { color: COLORS.grid } },
      axisTick: { show: false },
      axisLabel: { color: COLORS.muted, fontSize: 9 },
    },
    series: [
      {
        type: 'bar',
        data: items.map((i) => i.count).reverse(),
        barMaxWidth: 16,
        itemStyle: { color: COLORS.amber, borderRadius: [0, 3, 3, 0] },
        label: {
          show: true,
          position: 'right',
          color: COLORS.muted,
          fontSize: 9,
        },
      },
    ],
  }
}

function renderAll(): void {
  trendChart = init(trendEl.value, trendChart)
  if (trendChart && daily.value.length) trendChart.setOption(buildTrend(), true)
  else trendChart?.clear()

  sourceChart = init(sourceEl.value, sourceChart)
  if (sourceChart && bySource.value.length) sourceChart.setOption(buildSource(), true)
  else sourceChart?.clear()

  symbolChart = init(symbolEl.value, symbolChart)
  if (symbolChart && bySymbol.value.length) symbolChart.setOption(buildSymbol(), true)
  else symbolChart?.clear()

  skipChart = init(skipEl.value, skipChart)
  if (skipChart && skipReasons.value.length) skipChart.setOption(buildSkip(), true)
  else skipChart?.clear()
}

watch(
  () => props.overview,
  () => renderAll(),
  { deep: true, flush: 'post' },
)

onMounted(() => {
  renderAll()
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      renderAll()
      trendChart?.resize()
      sourceChart?.resize()
      symbolChart?.resize()
      skipChart?.resize()
    })
    for (const el of [trendEl.value, sourceEl.value, symbolEl.value, skipEl.value]) {
      if (el) resizeObserver.observe(el)
    }
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  trendChart?.dispose()
  sourceChart?.dispose()
  symbolChart?.dispose()
  skipChart?.dispose()
  trendChart = undefined
  sourceChart = undefined
  symbolChart = undefined
  skipChart = undefined
})
</script>

<template>
  <div class="pf-grid-2">
    <!-- 趋势 -->
    <div class="pf-card">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />每日信号量与成功率</h3>
        <span class="pf-count">UTC+8 自然日</span>
      </div>
      <div v-if="daily.length" ref="trendEl" class="pf-chart pf-chart-240" />
      <p v-else class="pf-empty">暂无数据</p>
    </div>

    <!-- 来源分布 -->
    <div class="pf-card">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />信号来源分布</h3>
      </div>
      <div v-if="bySource.length" ref="sourceEl" class="pf-chart pf-chart-240" />
      <p v-else class="pf-empty">暂无数据</p>
    </div>

    <!-- 品种 Top -->
    <div class="pf-card">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />品种 Top</h3>
        <span class="pf-count">按信号量</span>
      </div>
      <div v-if="bySymbol.length" ref="symbolEl" class="pf-chart pf-chart-200" />
      <p v-else class="pf-empty">暂无数据</p>
    </div>

    <!-- 拦截原因 Top -->
    <div class="pf-card">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />拦截原因 Top</h3>
        <span class="pf-count">按结构化编码聚合</span>
      </div>
      <div v-if="skipReasons.length" ref="skipEl" class="pf-chart pf-chart-200" />
      <p v-else class="pf-empty">窗口内没有拦截记录</p>
    </div>
  </div>
</template>
