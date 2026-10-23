<script setup lang="ts">
// 近 7 日收益柱状图（跟单详情用）。
//
// 组件只负责**画图**，不做任何统计：数据由后端按与页面绩效**同一套口径**
// （主节点 + 出场类成交的 net_profit）算好传进来。前端再算一遍必然与
// 页面头部的「累计收益」对不上 —— 那会直接摧毁整个页面的数据可信度。
//
// 口径要点：
// - 固定 7 个自然日，**不随页面统计窗口（7d/30d/90d/all）变化**；
// - 日期由后端按 UTC+8 分好，前端直接显示，不再本地换算（时区不同会整体错位一天）；
// - 没有成交的日子也占位（profit=0），X 轴必须连续。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, MarkLineComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { FollowDailyProfitPoint } from '@/api/types'

echarts.use([
  BarChart,
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  CanvasRenderer,
])

const props = withDefaults(
  defineProps<{
    /** 近 N 日的每日净盈亏（已按日期升序） */
    points?: FollowDailyProfitPoint[]
    /** 图表高度（px） */
    height?: number
  }>(),
  { points: () => [], height: 150 },
)

/** 与全局主题一致的配色 */
const COLORS = {
  up: '#10b981',
  down: '#ef4444',
  zero: 'rgba(123, 139, 168, 0.35)',
  grid: 'rgba(100, 130, 200, 0.16)',
  muted: '#7b8ba8',
  text: '#e8edf5',
}

const chartEl = ref<HTMLDivElement>()
/** 外层容器：始终存在，用它做尺寸监听（图表元素本身是 v-if，尺寸为 0 时不可靠） */
const wrapEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

const points = computed(() => props.points || [])

/** 合计（7 日总净盈亏）—— 与页面头部数字同源 */
const total = computed(() => points.value.reduce((sum, p) => sum + (p.profit || 0), 0))

/**
 * 有没有可展示的数据。
 *
 * 「全 0」有两种含义：真的一分钱没赚，或者**根本没有成交数据**。
 * 这里只判断有没有数据点传进来；真正的「无数据」由父组件判断
 * （后端会同时给 performance.data_available），避免把「有数据但全亏」画成空图。
 */
const hasData = computed(() => points.value.length > 0)

/**
 * 金额格式化：符号在货币符号**之前**（-$123.45 / +$123.45）。
 *
 * 与页面其它地方（fmtProfit）保持同一写法，负号在 $ 后面在深色背景上容易看漏方向。
 */
function fmtAmount(v: number): string {
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return sign + '$' + Math.abs(v).toFixed(2)
}

function buildOption(): echarts.EChartsCoreOption {
  const labels = points.value.map((p) => p.date)
  const values = points.value.map((p) => p.profit || 0)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 4, right: 6, top: 10, bottom: 4, containLabel: true },
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
        const p = points.value[first.dataIndex]
        if (!p) return ''
        const lines = [
          `<div style="font-weight:600">${p.date}</div>`,
          `<div>净盈亏 <b>${fmtAmount(p.profit || 0)}</b></div>`,
        ]
        if (p.deals) lines.push(`<div style="color:${COLORS.muted}">出场 ${p.deals} 笔</div>`)
        return lines.join('')
      },
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
        formatter: (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)),
      },
    },
    series: [
      {
        type: 'bar',
        name: '每日收益',
        data: values,
        barMaxWidth: 26,
        // 正绿负红，零值灰 —— 一眼看出哪几天在亏
        itemStyle: {
          color: (params: { value: number }) => {
            const v = Number(params.value)
            if (v > 0) return COLORS.up
            if (v < 0) return COLORS.down
            return COLORS.zero
          },
          borderRadius: [3, 3, 0, 0],
        },
        // 0 轴：正负柱自然分居上下，先看到这条线才知道哪边是赚
        markLine: {
          silent: true,
          symbol: 'none',
          label: { show: false },
          lineStyle: { color: 'rgba(100, 130, 200, 0.35)', width: 1 },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  }
}

/**
 * 确保图表实例存在且已按当前尺寸初始化。
 *
 * 关键：**容器尺寸为 0 时不能 init**。跟单页的手机端详情（`.m-sheet`）
 * 与桌面端详情（`.follow-grid`）是**同一份模板的两套布局**，其中一套
 * 在当前断点下是 `display:none`。在隐藏容器上 init 会得到 0 宽的画布，
 * 等它显示出来时图表是空白的 —— 必须等有尺寸再初始化。
 */
function ensureChart(): echarts.ECharts | undefined {
  if (!chartEl.value) return undefined
  if (!chart) {
    const { offsetWidth, offsetHeight } = chartEl.value
    if (!offsetWidth || !offsetHeight) return undefined
    chart = echarts.init(chartEl.value)
  }
  return chart
}

function render() {
  if (!hasData.value) {
    chart?.clear()
    return
  }
  const c = ensureChart()
  if (!c) return
  c.setOption(buildOption(), true)
}

// flush: 'post' —— 图表元素是 v-if，数据到位后 DOM 还没 patch 时
// chartEl.value 仍是 null。用 post 才能拿到新挂载的元素。
watch(points, () => render(), { deep: true, flush: 'post' })

onMounted(() => {
  render()
  // 监听**外层容器**而不是图表元素：图表元素是 v-if，隐藏时尺寸为 0，
  // 用它做监听会漏掉「从隐藏变成显示」的那次尺寸变化。
  if (wrapEl.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      // 容器刚有尺寸（显示出来了）-> 初始化并画图；已有实例 -> 跟随缩放
      if (!chart) render()
      else chart.resize()
    })
    resizeObserver.observe(wrapEl.value)
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = undefined
})
</script>

<template>
  <div ref="wrapEl" class="pbc-wrap">
    <div class="pbc-head">
      <span class="pbc-title">近 7 日收益</span>
      <!-- 没有数据点时不显示合计：$0.00 会被误读成「赚了 0 块」，
           而真实语义是「连这 7 天的数据都没有」 -->
      <span v-if="hasData" class="pbc-total" :class="total >= 0 ? 'pos' : 'neg'">{{ fmtAmount(total) }}</span>
    </div>

    <div v-if="hasData" ref="chartEl" class="pbc-chart" :style="{ height: `${height}px` }" />
    <p v-else class="muted pbc-empty">暂无数据</p>
  </div>
</template>

<style scoped>
.pbc-wrap {
  min-width: 0;
}

.pbc-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.pbc-title {
  font-size: 12px;
  color: var(--muted);
}

/* 合计金额：与页面其它收益数字同款配色 */
.pbc-total {
  font-size: 13px;
  font-weight: 700;
}
.pbc-total.pos { color: var(--green); }
.pbc-total.neg { color: var(--red); }

.pbc-chart {
  width: 100%;
}

.pbc-empty {
  margin: 0;
  padding: 18px 0;
  text-align: center;
  font-size: 12px;
}
</style>
