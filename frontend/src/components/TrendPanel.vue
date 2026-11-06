<script setup lang="ts">
// 节点详情「趋势面板」：按币种 / 周期实时查看 EMA + RSI 合成的趋势得分。
// 面板只负责选品种、调参数与展示；指标算法、权重换算与多空判定全在后端
// （trend_indicators），前端不承载任何交易决策。
// 打开面板后按选定间隔轮询 GET /api/nodes/{id}/trend；改参数会立刻带覆盖项重取一次
// （后端行情缓存会拦住重复的终端查询），点保存才写入全局配置（全后台共用）。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import HomeTrendConfig from '@/components/HomeTrendConfig.vue'
import * as echarts from 'echarts/core'
import { CandlestickChart, LineChart } from 'echarts/charts'
import {
  AxisPointerComponent,
  DataZoomInsideComponent,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import TagSelect from '@/components/TagSelect.vue'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import type { TrendConfig, TrendPanelData } from '@/api/types'
import {
  TREND_DEFAULTS,
  TREND_REFRESH_OPTIONS,
  TREND_TIMEFRAMES,
  rsiStateLabel,
  toTrendForm,
  trendLabel,
  trendTagClass,
} from '@/utils/trendConfig'

echarts.use([
  CandlestickChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  AxisPointerComponent,
  DataZoomInsideComponent,
  MarkLineComponent,
  CanvasRenderer,
])

const props = defineProps<{
  nodeId: string
  /** 全局已保存的趋势参数（表单初值；全后台共享） */
  trendConfig?: TrendConfig | null
  /** 候选品种：节点报价 / 持仓 / 按币种配置的并集，由父页面汇总 */
  symbolOptions?: string[]
  /** 无本地记忆时的默认品种（如全局趋势页固定 XAUUSD） */
  defaultSymbol?: string
  /**
   * 品种本地记忆的键前缀。节点详情默认 `trend:symbol`；
   * 顶栏全局页可传独立前缀，避免与详情页上次选中的品种互相覆盖。
   */
  symbolStoragePrefix?: string
  online?: boolean
  /** 总览等嵌入场景：只展示得分条，不加载 K 线与参数表单 */
  compact?: boolean
  /** 首页设计稿外观。与总览 compact、趋势页完整面板分开，不改它们的布局 */
  home?: boolean
}>()

const hub = useHubStore()
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const homeConfigOpen = computed(() => props.home && route.query.config === 'trend')
const showMa5 = ref(true)
const showMa10 = ref(true)
const showMa20 = ref(false)

function openHomeConfig(): void {
  router.push({ name: 'home', query: { config: 'trend' } })
}
function closeHomeConfig(): void {
  router.push({ name: 'home' })
}

const symbol = ref('')
const symbolInput = ref('')
const form = reactive<TrendConfig>(toTrendForm(props.trendConfig))
const refreshMs = ref<number>(TREND_REFRESH_OPTIONS[0].value)
const data = ref<TrendPanelData | null>(null)
const error = ref('')
const loading = ref(false)
const saving = ref(false)
const savedTip = ref('')

const cfg = computed<TrendConfig>(() => data.value?.config || form)
const emaWeightPct = computed(() => Math.round(cfg.value.ema_weight * 100))
const rsiWeightPct = computed(() => Math.round(cfg.value.rsi_weight * 100))

// ---------------------------- 品种 ----------------------------
const storageKey = computed(
  () => `${props.symbolStoragePrefix || 'trend:symbol'}:${props.nodeId}`,
)

function readStoredSymbol(): string {
  try {
    return localStorage.getItem(storageKey.value) || ''
  } catch {
    return ''
  }
}

function pickSymbol(next: string): void {
  const value = (next || '').trim().toUpperCase()
  if (!value || value === symbol.value) return
  symbol.value = value
  symbolInput.value = value
  try {
    localStorage.setItem(storageKey.value, value)
  } catch {
    /* 隐私模式下写不了本地存储，不影响本次查看 */
  }
}

function commitSymbol(): void {
  pickSymbol(symbolInput.value)
}

// ---------------------------- 取数 ----------------------------
function extractError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return detail || '读取趋势数据失败'
}

let inflight = false

async function load(): Promise<void> {
  const sym = symbol.value
  // 上一次还没回来就跳过：面板间隔可能短于一次终端查询的耗时，堆积请求毫无意义
  if (!sym || inflight) return
  inflight = true
  loading.value = true
  try {
    data.value = await hub.fetchNodeTrend(props.nodeId, sym, { ...form })
    error.value = ''
  } catch (e) {
    error.value = extractError(e)
  } finally {
    inflight = false
    loading.value = false
  }
}

let timer: ReturnType<typeof setInterval> | undefined

function restartTimer(): void {
  if (timer) clearInterval(timer)
  timer = undefined
  if (refreshMs.value > 0) timer = setInterval(load, refreshMs.value)
}

let tuneTimer: ReturnType<typeof setTimeout> | undefined

function reloadSoon(): void {
  // 参数用输入框连续调整，防抖后再取，避免每敲一位就发一次请求
  if (tuneTimer) clearTimeout(tuneTimer)
  tuneTimer = setTimeout(load, 300)
}

// ---------------------------- 参数 ----------------------------
watch(
  () => form.ema_weight,
  (value) => {
    // 两项权重之和固定为 1（后端也会归一化），面板只让用户调 EMA 一侧
    const weight = Math.min(1, Math.max(0, Number(value) || 0))
    const paired = Number((1 - weight).toFixed(4))
    if (form.rsi_weight !== paired) form.rsi_weight = paired
  },
)

watch(() => ({ ...form }), reloadSoon, { deep: true })
watch(symbol, load)
watch(refreshMs, restartTimer)

watch(
  () => props.trendConfig,
  (next) => {
    // 保存成功后父页面会刷新节点列表，这里同步回后端夹取后的真实生效值
    if (next) Object.assign(form, toTrendForm(next))
  },
)

watch(
  () => props.symbolOptions,
  (options) => {
    if (symbol.value) return
    const fallback = (props.defaultSymbol || '').trim().toUpperCase() || options?.[0]
    if (fallback) pickSymbol(fallback)
  },
)

function resetToDefaults(): void {
  Object.assign(form, TREND_DEFAULTS)
}

async function save(): Promise<boolean> {
  saving.value = true
  try {
    const saved = await hub.saveTrendConfig({ ...form })
    Object.assign(form, toTrendForm(saved))
    savedTip.value = '已保存为全局默认参数（所有节点共用）'
    setTimeout(() => (savedTip.value = ''), 2500)
    return true
  } catch (e) {
    error.value = extractError(e)
    return false
  } finally {
    saving.value = false
  }
}

async function saveHomeConfig(): Promise<void> {
  if (auth.isAdmin) {
    const ok = await save()
    if (!ok) return
  }
  closeHomeConfig()
}

// ---------------------------- 格式化 ----------------------------
function fmtPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return Math.abs(value) >= 100 ? value.toFixed(2) : value.toFixed(5)
}

function fmtSigned(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`
}

function scoreClass(value: number | null | undefined): string {
  if (!value) return ''
  return value > 0 ? 'is-bull' : 'is-bear'
}

const positionLabel = computed(() => {
  const position = data.value?.ema.position
  if (position === 'above') return '价格在 EMA 上方'
  if (position === 'below') return '价格在 EMA 下方'
  if (position === 'equal') return '价格与 EMA 重合'
  return '—'
})

/** 得分刻度条：分区宽度随多空阈值配置变化，指针位置随当前得分变化 */
const scale = computed(() => {
  const bearish = Math.min(0, Math.max(-100, cfg.value.bearish))
  const bullish = Math.max(0, Math.min(100, cfg.value.bullish))
  const at = (value: number) => ((value + 100) / 200) * 100
  const score = Math.min(100, Math.max(-100, data.value?.score ?? 0))
  return {
    bear: `${at(bearish)}%`,
    neutral: `${at(bullish) - at(bearish)}%`,
    bull: `${100 - at(bullish)}%`,
    marker: `${at(score)}%`,
    bearish,
    bullish,
  }
})

const HOME_TIMEFRAMES: Record<string, string> = {
  M1: '1分钟',
  M5: '5分钟',
  M15: '15分钟',
  M30: '30分钟',
  H1: '1小时',
  H4: '4小时',
  D1: '日线',
  W1: '周线',
  MN: '月线',
}
const homeTimeframe = computed(() => HOME_TIMEFRAMES[cfg.value.timeframe] || cfg.value.timeframe)
const homeRunLabel = computed(() => {
  const trend = data.value?.trend
  if (trend === 'bullish') return '偏多运行'
  if (trend === 'bearish') return '偏空运行'
  if (trend === 'neutral') return '中性'
  if (trend === 'unknown') return '数据不足'
  return ''
})
/** 得分 -100~100 线性映射成多头占比，空头为剩余。未就绪时不填数。 */
const homeBias = computed(() => {
  const ready = data.value
  if (!ready?.ready || !Number.isFinite(ready.score)) return null
  const score = Math.min(100, Math.max(-100, ready.score))
  const bull = (score + 100) / 2
  return { bull, bear: 100 - bull }
})
/** 现价相对上一根已收盘 K 线收盘价。 */
const homeDelta = computed(() => {
  const ready = data.value
  if (!ready || ready.bars.length < 2 || !Number.isFinite(ready.price)) return null
  const prev = ready.bars[ready.bars.length - 2]?.close
  if (!prev) return null
  const delta = ready.price - prev
  return { delta, pct: (delta / prev) * 100 }
})
const homeMoveDigits = computed(() => (Math.abs(data.value?.price ?? 0) >= 100 ? 2 : 5))

function fmtHomeMove(value: number, digits: number): string {
  const abs = Math.abs(value).toFixed(digits)
  if (value > 0) return `+${abs}`
  if (value < 0) return `-${abs}`
  return abs
}

// ---------------------------- 图表 ----------------------------
const CHART_COLORS = {
  up: '#10b981',
  down: '#ef4444',
  ema: '#fbbf24',
  rsi: '#3b82f6',
  grid: 'rgba(100, 130, 200, 0.16)',
  muted: '#7b8ba8',
}

const chartEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function barLabel(seconds: number, timeframe: string): string {
  const at = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
  if (timeframe === 'D1' || timeframe === 'W1' || timeframe === 'MN') {
    return `${at.getFullYear()}-${day}`
  }
  return `${day} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

function buildOption(d: TrendPanelData) {
  const labels = d.bars.map((b) => barLabel(b.time, d.config.timeframe))
  // echarts K 线的数据顺序固定为 [开, 收, 最低, 最高]
  const candles = d.bars.map((b) => [b.open, b.close, b.low, b.high])
  const threshold = (value: number, color: string, type: 'dashed' | 'dotted') => ({
    yAxis: value,
    lineStyle: { color, type, width: 1, opacity: 0.7 },
    label: { show: true, formatter: `${value}`, color: CHART_COLORS.muted, fontSize: 9 },
  })
  return {
    animation: false,
    backgroundColor: 'transparent',
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#1b2740' } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(100, 130, 200, 0.24)',
      textStyle: { color: '#e8edf5', fontSize: 11 },
    },
    grid: [
      { left: 8, right: 18, top: 14, height: '54%', containLabel: true },
      { left: 8, right: 18, top: '72%', height: '20%', containLabel: true },
    ],
    xAxis: [
      {
        type: 'category',
        data: labels,
        gridIndex: 0,
        axisLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisTick: { show: false },
        axisLabel: { show: false },
      },
      {
        type: 'category',
        data: labels,
        gridIndex: 1,
        axisLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisTick: { show: false },
        axisLabel: { color: CHART_COLORS.muted, fontSize: 10, hideOverlap: true },
      },
    ],
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
        axisLine: { show: false },
        axisLabel: { color: CHART_COLORS.muted, fontSize: 10 },
      },
      {
        min: 0,
        max: 100,
        interval: 50,
        gridIndex: 1,
        splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
        axisLine: { show: false },
        axisLabel: { color: CHART_COLORS.muted, fontSize: 10 },
      },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      {
        type: 'candlestick',
        name: 'K 线',
        data: candles,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: CHART_COLORS.up,
          color0: CHART_COLORS.down,
          borderColor: CHART_COLORS.up,
          borderColor0: CHART_COLORS.down,
        },
      },
      {
        type: 'line',
        name: `EMA(${d.config.ema_period})`,
        data: d.ema_series,
        xAxisIndex: 0,
        yAxisIndex: 0,
        showSymbol: false,
        smooth: true,
        lineStyle: { color: CHART_COLORS.ema, width: 1.6 },
      },
      {
        type: 'line',
        name: `RSI(${d.config.rsi_period})`,
        data: d.rsi_series,
        xAxisIndex: 1,
        yAxisIndex: 1,
        showSymbol: false,
        smooth: true,
        lineStyle: { color: CHART_COLORS.rsi, width: 1.4 },
        markLine: {
          symbol: 'none',
          silent: true,
          data: [
            threshold(d.config.rsi_overbought, CHART_COLORS.down, 'dashed'),
            threshold(d.config.rsi_bull, CHART_COLORS.muted, 'dotted'),
            threshold(d.config.rsi_bear, CHART_COLORS.muted, 'dotted'),
            threshold(d.config.rsi_oversold, CHART_COLORS.up, 'dashed'),
          ],
        },
      },
    ],
  }
}

function sma(closes: number[], period: number): Array<number | null> {
  return closes.map((_, index) => {
    if (index < period - 1) return null
    let sum = 0
    for (let j = 0; j < period; j++) sum += closes[index - j]
    return sum / period
  })
}

/** 首页 K 线：主图 + MA5/MA10。接口没有成交量，不画量柱。 */
function buildHomeOption(d: TrendPanelData) {
  const labels = d.bars.map((b) => barLabel(b.time, d.config.timeframe))
  const candles = d.bars.map((b) => [b.open, b.close, b.low, b.high])
  const closes = d.bars.map((b) => b.close)
  const last = d.price
  const prev = d.bars.length >= 2 ? d.bars[d.bars.length - 2].close : last
  const priceText = Math.abs(last) >= 100 ? last.toFixed(2) : last.toFixed(5)
  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(12, 18, 32, 0.94)',
      borderColor: 'rgba(34, 48, 74, 0.9)',
      textStyle: { color: '#E6EBF4', fontSize: 11 },
    },
    grid: { left: 8, right: 8, top: 16, bottom: 24, containLabel: true },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
      axisTick: { show: false },
      axisLabel: { color: '#5B6B85', fontSize: 10, hideOverlap: true },
    },
    yAxis: {
      scale: true,
      position: 'right',
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)', type: 'dashed' } },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#5B6B85', fontSize: 10 },
    },
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        type: 'candlestick',
        name: 'K线',
        data: candles,
        itemStyle: {
          color: '#22c55e',
          color0: '#ef4444',
          borderColor: '#22c55e',
          borderColor0: '#ef4444',
        },
        markLine: {
          symbol: 'none',
          silent: true,
          data: [{
            yAxis: last,
            lineStyle: { color: 'rgba(230,235,244,0.2)', type: 'dashed', width: 1 },
            label: {
              show: true,
              position: 'end',
              formatter: priceText,
              color: '#06281a',
              backgroundColor: last >= prev ? '#34d399' : '#f87171',
              padding: [2, 6],
              borderRadius: 4,
              fontSize: 11,
            },
          }],
        },
      },
      ...([
        showMa5.value ? { name: 'MA5', color: '#eab308', period: 5 } : null,
        showMa10.value ? { name: 'MA10', color: '#60a5fa', period: 10 } : null,
        showMa20.value ? { name: 'MA20', color: '#c084fc', period: 20 } : null,
      ].filter((item): item is { name: string; color: string; period: number } => item != null).map((item) => ({
        type: 'line',
        name: item.name,
        data: sma(closes, item.period),
        showSymbol: false,
        lineStyle: { color: item.color, width: 1.4 },
      }))),
    ],
  }
}

function paintChart(next: TrendPanelData): void {
  if (!chartEl.value) return
  if (!chart) {
    chart = echarts.init(chartEl.value)
    if (!resizeObserver && typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => chart?.resize())
      resizeObserver.observe(chartEl.value)
    }
  }
  if (props.home) chart.setOption(buildHomeOption(next), { replaceMerge: ['series'] })
  else chart.setOption(buildOption(next))
}

watch(data, (next) => {
  if (!next || !next.bars.length) return
  paintChart(next)
})

watch(homeConfigOpen, async (open) => {
  if (!props.home) return
  if (open) {
    resizeObserver?.disconnect()
    resizeObserver = undefined
    chart?.dispose()
    chart = undefined
    return
  }
  await nextTick()
  if (data.value?.bars.length) paintChart(data.value)
})

watch([showMa5, showMa10, showMa20], () => {
  if (!props.home || !data.value?.bars.length) return
  paintChart(data.value)
})

onMounted(() => {
  const initial =
    readStoredSymbol()
    || (props.defaultSymbol || '').trim().toUpperCase()
    || props.symbolOptions?.[0]
    || ''
  if (initial) pickSymbol(initial)
  if (!props.compact && chartEl.value) {
    chart = echarts.init(chartEl.value)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => chart?.resize())
      resizeObserver.observe(chartEl.value)
    }
  }
  restartTimer()
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (tuneTimer) clearTimeout(tuneTimer)
  resizeObserver?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div v-if="home" class="home-trend">
    <HomeTrendConfig
      v-if="homeConfigOpen"
      :form="form"
      :refresh-ms="refreshMs"
      :ma5="showMa5"
      :ma10="showMa10"
      :ma20="showMa20"
      :saving="saving"
      :is-admin="auth.isAdmin"
      :error="error"
      @update:refresh-ms="refreshMs = $event"
      @update:ma5="showMa5 = $event"
      @update:ma10="showMa10 = $event"
      @update:ma20="showMa20 = $event"
      @save="saveHomeConfig"
      @reset="resetToDefaults"
      @back="closeHomeConfig"
    />
    <template v-else>
    <section class="home-card">
      <div class="home-card-head">
        <p class="home-kicker">标的趋势 · 多空值</p>
        <span v-if="homeRunLabel" class="home-run" :class="data?.trend">
          <i></i>{{ homeRunLabel }}
        </span>
      </div>
      <div v-if="symbolOptions?.length" class="home-symbols">
        <button
          v-for="s in symbolOptions"
          :key="s"
          type="button"
          class="home-sym"
          :class="{ active: s === symbol }"
          @click="pickSymbol(s)"
        >{{ s }}</button>
      </div>
      <p v-else class="home-hint">该节点暂无报价与持仓品种。</p>

      <div class="home-price-row">
        <div>
          <div class="home-price">{{ fmtPrice(data?.price) }}</div>
          <div v-if="homeDelta" class="home-delta" :class="homeDelta.delta >= 0 ? 'is-up' : 'is-down'">
            <span>{{ fmtHomeMove(homeDelta.delta, homeMoveDigits) }}</span>
            <span class="home-delta-pct">{{ fmtHomeMove(homeDelta.pct, 2) }}%</span>
          </div>
        </div>
        <button type="button" class="home-config-hint" @click="openHomeConfig">
          权重 / 阈值 / 更新时间<br>请在配置页查看
        </button>
      </div>

      <div v-if="homeBias" class="home-bias">
        <div class="home-bias-top">
          <span class="is-up">多头 {{ homeBias.bull.toFixed(1) }}</span>
          <span class="is-down">空头 {{ homeBias.bear.toFixed(1) }}</span>
        </div>
        <div class="home-bias-track">
          <span class="bull" :style="{ width: `${homeBias.bull}%` }"></span>
          <span class="bear" :style="{ width: `${homeBias.bear}%` }"></span>
        </div>
        <div class="home-bias-foot">
          <span>多空比 {{ homeBias.bull.toFixed(1) }} : {{ homeBias.bear.toFixed(1) }}</span>
        </div>
      </div>
      <p v-else-if="data" class="home-hint">
        已收盘 K 线只有 {{ data.bar_count }} 根，不足以算出 EMA({{ cfg.ema_period }}) 与
        RSI({{ cfg.rsi_period }})，请缩短周期或减小指标周期。
      </p>
      <p v-else class="home-hint">选择品种后显示趋势得分。</p>
      <p v-if="!online" class="home-warn">节点当前离线，无法读取实时行情。</p>
      <p v-else-if="error" class="home-warn">{{ error }}</p>
    </section>

    <section class="home-card">
      <div class="home-card-head">
        <div class="home-kline-title">
          <span>K线 · {{ data?.symbol || symbol || '—' }}</span>
          <span v-if="showMa5" class="home-legend"><i class="ma5"></i>MA5</span>
          <span v-if="showMa10" class="home-legend"><i class="ma10"></i>MA10</span>
          <span v-if="showMa20" class="home-legend"><i class="ma20"></i>MA20</span>
        </div>
        <button type="button" class="home-tf" @click="openHomeConfig">
          {{ homeTimeframe }}
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z"/></svg>
        </button>
      </div>
      <div ref="chartEl" class="home-chart"></div>
      <p v-if="!data?.bars?.length" class="home-hint">暂无 K 线数据。</p>
      <p class="home-kline-note">周期在配置页设置</p>
    </section>
    </template>
  </div>
  <div v-else :class="{ 'trend-compact': compact }">

    <!-- 品种 / 周期 / 刷新 -->
    <div v-if="!compact" class="card card-pad" style="margin-bottom: 16px">
      <div class="row between" style="align-items: flex-start">
        <div class="row" style="gap: 10px">
          <label class="trend-field">
            <span class="trend-field-k">品种</span>
            <input
              v-model="symbolInput"
              class="trend-input"
              placeholder="如 XAUUSD"
              @keyup.enter="commitSymbol"
              @blur="commitSymbol"
            />
          </label>
          <label class="trend-field">
            <span class="trend-field-k">周期</span>
            <TagSelect v-model="form.timeframe" :options="TREND_TIMEFRAMES" aria-label="周期" />
          </label>
          <label class="trend-field">
            <span class="trend-field-k">自动刷新</span>
            <TagSelect v-model="refreshMs" :options="TREND_REFRESH_OPTIONS" aria-label="自动刷新" />
          </label>
        </div>
        <button class="btn-sm btn-ghost" :disabled="loading || !symbol" @click="load">
          {{ loading ? '读取中…' : '立即刷新' }}
        </button>
      </div>

      <div v-if="symbolOptions?.length" class="trend-chips">
        <button
          v-for="s in symbolOptions"
          :key="s"
          class="trend-chip"
          :class="{ active: s === symbol }"
          @click="pickSymbol(s)"
        >
          {{ s }}
        </button>
      </div>
      <p v-if="!symbolOptions?.length" class="muted trend-hint">
        该节点暂无报价与持仓品种可选，直接输入品种代码即可查看。
      </p>
    </div>
    
    <!-- 趋势总览 -->
    <div class="card card-pad" :style="{ marginBottom: compact ? 0 : '16px' }">
      <div class="row between" style="align-items: flex-start">
        
      <div v-if="compact" class="trend-compact-tools">
        <!-- 品种芯片：标题与芯片分层。标题之前混在 chips 容器里，
             会和按钮并排显示，按钮放大后更明显。 -->
        <div class="trend-compact-block">
          <span v-if="symbolOptions?.length" class="trend-field-k">标的趋势 - 多空值</span>
          <!-- 芯片行：当前价与芯片并排在同一行 -->
          <div class="trend-compact-row">
            <div v-if="symbolOptions?.length" class="trend-chips">
              <button
                v-for="s in symbolOptions"
                :key="s"
                class="trend-chip"
                :class="{ active: s === symbol }"
                @click="pickSymbol(s)"
              >
                {{ s }}
              </button>
            </div>
            <!-- 当前价：紧凑模式下与品种芯片同一行，靠右 -->
            <div class="trend-compact-price">
              <div class="trend-price">{{ fmtPrice(data?.price) }}</div>
              <div class="muted trend-sub trend-compact-price-k">
                当前价<template v-if="data?.price_source === 'close'">（报价缺失，取收盘价）</template>
              </div>
            </div>
          </div>
        </div>
        <!-- <label class="trend-field">
          <span class="trend-field-k">周期</span>
          <TagSelect v-model="form.timeframe" :options="TREND_TIMEFRAMES" aria-label="周期" />
        </label> -->
        <!-- <button class="btn-sm btn-ghost" :disabled="loading || !symbol" @click="load">
          {{ loading ? '读取中…' : '刷新' }}
        </button> -->
      </div>
        <!-- <div>
          <strong>{{ data?.symbol || symbol || '—' }}</strong>
          <span class="muted trend-sub">{{ timeframeLabel(cfg.timeframe) }}</span>
        </div> -->
        <div v-if="!compact" class="right">
          <div class="trend-price">{{ fmtPrice(data?.price) }}</div>
          <div class="muted trend-sub">
            当前价<template v-if="data?.price_source === 'close'">（报价缺失，取收盘价）</template>
          </div>
        </div>
      </div>

      <div v-if="data && data.ready" class="trend-score-wrap">
        <div class="row" style="gap: 10px">
          <span class="trend-score" :class="scoreClass(data.score)">{{ fmtSigned(data.score) }}</span>
          <span class="tag" :class="trendTagClass(data.trend)">{{ trendLabel(data.trend) }}</span>
        </div>
        <div class="trend-scale">
          <div class="trend-scale-track">
            <span class="trend-zone bear" :style="{ width: scale.bear }"></span>
            <span class="trend-zone neutral" :style="{ width: scale.neutral }"></span>
            <span class="trend-zone bull" :style="{ width: scale.bull }"></span>
          </div>
          <span class="trend-marker" :style="{ left: scale.marker }"></span>
        </div>
        <div class="trend-scale-legend muted">
          <span>-100</span>
          <span>空头 &lt; {{ scale.bearish }}</span>
          <span>中性</span>
          <span>多头 &gt; {{ scale.bullish }}</span>
          <span>+100</span>
        </div>
      </div>
      <p v-else-if="data" class="muted trend-hint">
        已收盘 K 线只有 {{ data.bar_count }} 根，不足以算出 EMA({{ cfg.ema_period }}) 与
        RSI({{ cfg.rsi_period }})，请缩短周期或减小指标周期。
      </p>
      <p v-else class="muted trend-hint">选择品种后显示趋势得分。</p>

      <!-- <div class="trend-foot muted">
        <span>权重：EMA {{ emaWeightPct }}% + RSI {{ rsiWeightPct }}%</span>
        <span>阈值：多头 &gt; {{ cfg.bullish }} ｜ 中性 {{ cfg.bearish }}~{{ cfg.bullish }} ｜ 空头 &lt; {{ cfg.bearish }}</span>
        <span>最后更新：{{ fmtTime(data?.updated_at) }}</span>
        <span>K 线时间：{{ fmtTime(data?.bar_time) }}</span>
      </div> -->


      <p v-if="!online" class="trend-warn">节点当前离线，无法读取实时行情。</p>
      <p v-else-if="error" class="trend-warn">{{ error }}</p>
    </div>

    <div v-if="compact" class="card card-pad trend-chart-card trend-compact-tools">
      <!-- <div class="row between" style="margin-bottom: 8px">
        <strong style="font-size: 13px">K 线与指标</strong>
        <span class="muted trend-sub">
          上：K 线 + EMA ｜ 下：RSI（虚线为超买超卖，点线为偏多偏空）
        </span>
      </div> -->
      <div ref="chartEl" class="trend-chart"></div>
      <p v-if="!data?.bars?.length" class="muted trend-hint">暂无 K 线数据。</p>
    </div>

    <!-- 指标详情 -->
    <div v-if="!compact && data && data.ready" class="trend-metrics">
      <div class="card card-pad trend-metric">
        <div class="row between">
          <strong>EMA({{ data.ema.period }})</strong>
          <span class="tag blue">权重 {{ emaWeightPct }}%</span>
        </div>
        <div class="trend-metric-score" :class="scoreClass(data.ema.score)">
          {{ fmtSigned(data.ema.score) }}
        </div>
        <div class="muted trend-sub">最高贡献 ±{{ data.ema.max_score ?? emaWeightPct }}</div>
        <div class="trend-metric-rows">
          <div class="trend-kv"><span class="muted">当前价</span><span>{{ fmtPrice(data.price) }}</span></div>
          <div class="trend-kv"><span class="muted">EMA</span><span>{{ fmtPrice(data.ema.value) }}</span></div>
          <div class="trend-kv">
            <span class="muted">偏离</span>
            <span :class="scoreClass(data.ema.deviation_pct)">{{ fmtSigned(data.ema.deviation_pct, 3) }}%</span>
          </div>
          <div class="trend-kv"><span class="muted">满分偏离</span><span>{{ cfg.ema_full_scale_pct }}%</span></div>
        </div>
        <div class="trend-metric-note" :class="scoreClass(data.ema.score)">{{ positionLabel }}</div>
      </div>

      <div class="card card-pad trend-metric">
        <div class="row between">
          <strong>RSI({{ data.rsi.period }})</strong>
          <span class="tag blue">权重 {{ rsiWeightPct }}%</span>
        </div>
        <div class="trend-metric-score" :class="scoreClass(data.rsi.score)">
          {{ fmtSigned(data.rsi.score) }}
        </div>
        <div class="muted trend-sub">最高贡献 ±{{ data.rsi.max_score ?? rsiWeightPct }}</div>
        <div class="trend-metric-rows">
          <div class="trend-kv"><span class="muted">当前值</span><span>{{ data.rsi.value?.toFixed(2) ?? '—' }}</span></div>
          <div class="trend-kv"><span class="muted">状态</span><span>{{ rsiStateLabel(data.rsi.state) }}</span></div>
          <div class="trend-kv"><span class="muted">偏多 / 偏空</span><span>{{ cfg.rsi_bull }} / {{ cfg.rsi_bear }}</span></div>
          <div class="trend-kv"><span class="muted">超买 / 超卖</span><span>{{ cfg.rsi_overbought }} / {{ cfg.rsi_oversold }}</span></div>
        </div>
        <div class="trend-metric-note muted">
          {{ cfg.rsi_bear }}~{{ cfg.rsi_bull }} 为中性区，此区间内 RSI 不贡献得分
        </div>
      </div>
    </div>

    <!-- K 线与指标走势 -->
    <div v-if="!compact" class="card card-pad trend-chart-card">
      <div class="row between" style="margin-bottom: 8px">
        <strong style="font-size: 13px">K 线与指标</strong>
        <span class="muted trend-sub">
          上：K 线 + EMA ｜ 下：RSI（虚线为超买超卖，点线为偏多偏空）
        </span>
      </div>
      <div ref="chartEl" class="trend-chart"></div>
      <p v-if="!data?.bars?.length" class="muted trend-hint">暂无 K 线数据。</p>
    </div>

    <!-- 参数 -->
    <div v-if="!compact" class="card card-pad" style="margin-bottom: 16px">
      <div class="row between" style="align-items: flex-start; margin-bottom: 12px">
        <div>
          <strong>参数</strong>
          <p v-if="auth.isAdmin" class="muted trend-hint" style="margin: 4px 0 0">
            改动即时生效于本次查看；点「保存」写入全局配置（不分节点/币种），下次打开与其他管理员都沿用
          </p>
          <p v-else class="muted trend-hint" style="margin: 4px 0 0">
            改动只作用于本次查看；全局参数由管理员维护
          </p>
        </div>
        <div class="row" style="gap: 8px">
          <button class="btn-sm btn-ghost" @click="resetToDefaults">恢复默认</button>
          <button v-if="auth.isAdmin" class="btn-primary btn-sm" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
      <p v-if="savedTip" class="trend-saved">{{ savedTip }}</p>

      <div class="trend-form">
        <label class="trend-field">
          <span class="trend-field-k">EMA 周期</span>
          <input v-model.number="form.ema_period" type="number" min="2" max="400" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 周期</span>
          <input v-model.number="form.rsi_period" type="number" min="2" max="100" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">EMA 权重</span>
          <input v-model.number="form.ema_weight" type="number" min="0" max="1" step="0.05" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 权重（自动）</span>
          <input :value="form.rsi_weight" type="number" class="trend-input" disabled />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">满分偏离 %</span>
          <input v-model.number="form.ema_full_scale_pct" type="number" min="0.01" max="10" step="0.05" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">多头阈值</span>
          <input v-model.number="form.bullish" type="number" min="0" max="99" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">空头阈值</span>
          <input v-model.number="form.bearish" type="number" min="-99" max="0" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 偏多起始</span>
          <input v-model.number="form.rsi_bull" type="number" min="50" max="99" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 偏空起始</span>
          <input v-model.number="form.rsi_bear" type="number" min="1" max="50" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 超买</span>
          <input v-model.number="form.rsi_overbought" type="number" min="50" max="100" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">RSI 超卖</span>
          <input v-model.number="form.rsi_oversold" type="number" min="0" max="50" class="trend-input" />
        </label>
        <label class="trend-field">
          <span class="trend-field-k">显示 K 线根数</span>
          <input v-model.number="form.bars" type="number" min="30" max="500" step="10" class="trend-input" />
        </label>
      </div>
    </div>
  </div>
</template>

<style scoped>
.trend-field { display: flex; flex-direction: column; gap: 4px; }
.trend-field-k { color: var(--muted); font-size: 12px; }

.trend-input {
  background: rgba(12, 18, 32, 0.6);
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  padding: 7px 10px;
  min-width: 116px;
}
.trend-input:focus { border-color: var(--primary); outline: none; }
.trend-input:disabled { color: var(--muted); }

.trend-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.trend-chip {
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  color: var(--muted);
  cursor: pointer;
  font-family: var(--mono);
  /* 放大：11px → 14px。11px 在手机上几乎看不清，芯片文字是主要信息载体。 */
  font-size: 14px;
  font-weight: 600;
  /* 内边距同步放大，撑出更明确的可点区域 */
  padding: 8px 16px;
  /* 触摸目标 ≥ 40px，避免「看得见但点不准」 */
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  touch-action: manipulation;
  transition: var(--transition);
}
.trend-chip:hover { color: var(--text); border-color: var(--glass-highlight); }
.trend-chip.active {
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
  border-color: transparent;
  color: #ffffff;
  box-shadow: 0 0 12px rgba(37, 99, 234, 0.32);
}
.trend-chip.active:hover {
  color: #ffffff;
  background: linear-gradient(135deg, #3b82f6 0%, var(--primary) 100%);
}

.trend-hint { font-size: 12px; margin: 10px 0 0; line-height: 1.5; }
.trend-warn { color: var(--gold); font-size: 12px; margin: 10px 0 0; }
.trend-compact-tools {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  margin-top: 12px;
  flex-wrap: wrap;
}
/* 芯片 + 当前价：同一行并排，芯片可滚动，当前价靠右固定 */
.trend-compact-row {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
/* 品种块：内部纵向排列（标题 + 芯片），整体作为一个 flex item */
.trend-compact-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  flex: 1 1 240px;
}
/* 当前价：紧凑模式下贴在品种芯片行右侧 */
.trend-compact-price {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex: 0 0 auto;
  text-align: right;
}
/* 芯片区占据剩余空间并可滚动，当前列不参与滚动 */
.trend-compact-row > .trend-chips {
  flex: 1 1 auto;
  min-width: 0;
}
.trend-compact-price-k { margin-left: 0; }.trend-compact .trend-score { font-size: 26px; }
.trend-compact .trend-chips { margin-top: 0; }
.trend-saved { color: var(--primary); font-size: 12px; margin: 0 0 10px; }
.trend-sub { font-size: 12px; margin-left: 8px; }
.trend-price { font-family: var(--mono); font-size: 20px; font-weight: 700; }

.trend-score-wrap { margin-top: 18px; }
.trend-score { font-family: var(--mono); font-size: 30px; font-weight: 700; line-height: 1; }
.is-bull { color: var(--green); }
.is-bear { color: var(--red); }

.trend-scale { position: relative; margin: 16px 0 6px; height: 12px; }
.trend-scale-track {
  display: flex;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  margin-top: 2px;
}
.trend-zone { display: block; height: 100%; }
.trend-zone.bear { background: linear-gradient(90deg, rgba(239, 68, 68, 0.75), rgba(239, 68, 68, 0.3)); }
.trend-zone.neutral { background: rgba(123, 139, 168, 0.28); }
.trend-zone.bull { background: linear-gradient(90deg, rgba(16, 185, 129, 0.3), rgba(16, 185, 129, 0.75)); }
.trend-marker {
  position: absolute;
  top: -2px;
  width: 3px;
  height: 16px;
  margin-left: -1.5px;
  border-radius: 2px;
  background: var(--text);
  box-shadow: 0 0 8px rgba(232, 237, 245, 0.6);
  transition: left var(--transition);
}
.trend-scale-legend {
  display: flex;
  justify-content: space-between;
  font-family: var(--mono);
  font-size: 10px;
}

.trend-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 12px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed var(--glass-border);
}

.trend-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.trend-metric-score { font-family: var(--mono); font-size: 24px; font-weight: 700; margin-top: 12px; }
.trend-metric-rows { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
.trend-kv { display: flex; justify-content: space-between; font-size: 12px; }
.trend-kv span:last-child { font-family: var(--mono); font-weight: 600; }
.trend-metric-note {
  font-size: 12px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--glass-border);
}

.trend-chart-card { margin-bottom: 16px; }
.trend-chart { width: 100%; height: 420px; }

.trend-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 14px;
}

.home-trend { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.home-card {
  border: 1px solid #1C2740;
  background: #101828;
  border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  padding: 16px;
  min-width: 0;
}
.home-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.home-kicker { margin: 0; color: #8B96A8; font-size: 12px; font-weight: 500; }
.home-run { display: inline-flex; align-items: center; gap: 6px; font-size: 10px; color: #8B96A8; }
.home-run i { width: 6px; height: 6px; border-radius: 50%; background: #8B96A8; }
.home-run.bearish { color: #f87171; }
.home-run.bearish i { background: #ef4444; }
.home-run.bullish { color: #34d399; }
.home-run.bullish i { background: #22c55e; }
.home-symbols {
  display: flex;
  gap: 6px;
  margin-top: 10px;
  overflow-x: auto;
  scrollbar-width: none;
}
.home-symbols::-webkit-scrollbar { display: none; }
.home-sym {
  flex-shrink: 0;
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  backdrop-filter: none;
}
.home-sym:hover { color: #E6EBF4; background: #16213A; }
.home-sym.active,
.home-sym.active:hover {
  background: #2563EB;
  border-color: transparent;
  color: #fff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.4);
}
.home-price-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
}
.home-price {
  font-size: 36px;
  line-height: 1;
  font-weight: 650;
  color: #E6EBF4;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.03em;
}
.home-delta { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.home-delta.is-up { color: #34d399; }
.home-delta.is-down { color: #f87171; }
.home-delta-pct { border-radius: 6px; padding: 1px 6px; background: rgba(255, 255, 255, 0.06); }
.home-config-hint {
  margin: 0;
  max-width: 150px;
  padding: 0;
  border: none;
  border-radius: 0;
  background: transparent;
  text-align: right;
  color: #4A5568;
  font-size: 10px;
  line-height: 1.45;
  backdrop-filter: none;
}
.home-config-hint:hover { background: transparent; color: #8B96A8; }
.home-bias { margin-top: 16px; }
.home-bias-top, .home-bias-foot {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.home-bias-foot { margin-top: 6px; color: #5B6B85; }
.home-bias-track {
  display: flex;
  height: 6px;
  margin-top: 6px;
  border-radius: 999px;
  overflow: hidden;
  background: #0C1220;
}
.home-bias-track .bull { background: #22c55e; }
.home-bias-track .bear { background: #ef4444; }
.is-up { color: #34d399; }
.is-down { color: #f87171; }
.home-hint { margin: 10px 0 0; color: #5B6B85; font-size: 12px; line-height: 1.5; }
.home-warn { margin: 10px 0 0; color: #fbbf24; font-size: 12px; }
.home-kline-title { display: flex; align-items: center; gap: 10px; min-width: 0; color: #E6EBF4; font-size: 13px; font-weight: 600; }
.home-legend { display: inline-flex; align-items: center; gap: 4px; color: #8B96A8; font-size: 11px; font-weight: 500; }
.home-legend i { width: 12px; height: 2px; border-radius: 2px; }
.home-legend .ma5 { background: #eab308; }
.home-legend .ma10 { background: #60a5fa; }
.home-tf {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  height: 28px;
  padding: 0 8px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  font-size: 12px;
  backdrop-filter: none;
}
.home-tf:hover { background: #16213A; color: #E6EBF4; }
.home-tf svg { width: 14px; height: 14px; }
.home-legend .ma20 { background: #c084fc; }
.home-chart { width: 100%; height: 420px; }
.home-kline-note { margin: 4px 0 0; text-align: right; color: #4A5568; font-size: 10px; }
@media (max-width: 1023px) {
  .home-trend { gap: 12px; }
  .home-card { padding: 12px; }
  .home-price { font-size: 32px; }
  .home-chart { height: 300px; }
}

@media (max-width: 720px) {
  .trend-chart { height: 320px; }
  .trend-foot { gap: 4px 12px; }

  /* ── compact 工具区（首页 / 仪表盘嵌入的迷你趋势面板）─────────
     桌面上「品种芯片」与「周期」并排一行；窄屏放不下 9 个周期按钮，
     强行并排会把两者都挤成多行、按钮被压扁。改为纵向堆叠。

     min-width: 0 是必须的：父级 .row 是 flex 容器，默认 min-width:auto
     会让子项被内容（芯片 + 12 个周期按钮）的固有宽度撑开，
     实测在 390px 视口下 .trend-compact-tools 宽 483px > 卡片 357px，
     虽然内容用 overflow 藏住了，但整块仍会溢出卡片边界。 */
  .trend-compact-tools {
    flex-direction: column;
    align-items: stretch;
    gap: 14px;
    width: 100%;
    min-width: 0;
    max-width: 100%;
  }
  .trend-compact-block,
  .trend-compact-tools > .trend-field {
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
    max-width: 100%;
  }

  /* 窄屏下当前价仍固定在芯片行右侧（不换行、不撑满） */
  .trend-compact-price {
    align-items: flex-end;
    flex: 0 0 auto;
    max-width: 45%;
    margin-left: 0;
  }
  /* 芯片行：芯片占满剩余空间并可横向滚动，当前列保持可见 */
  .trend-compact-row {
    width: 100%;
    min-width: 0;
  }
  .trend-compact-row > .trend-chips {
    flex: 1 1 auto;
    min-width: 0;
  }

  /* 周期：横向滑动，避免 9 个按钮撑破卡片换行成三行 */
  .trend-compact-tools .tag-select {
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    /* 去掉最后一个按钮的右侧留白，让它能贴到卡片边缘 */
    padding-bottom: 2px;
  }
  .trend-compact-tools .tag-select::-webkit-scrollbar { display: none; }
  /* TagSelect 是子组件，.tag-select-item 带的是它自己的 scope 属性，
     TrendPanel 的 scoped 样式必须用 :deep() 才能命中，否则这里的
     放大/滑动规则会完全失效（字号仍是 12px、min-height 仍是 0）。 */
  .trend-compact-tools :deep(.tag-select-item) {
    flex-shrink: 0;
    /* 触摸目标 ≥ 40px */
    min-height: 40px;
    padding: 8px 14px;
    font-size: 13px;
  }

  /* 品种芯片：同样允许横向滚动 */
  .trend-chips {
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    padding-bottom: 2px;
    /* 负边距让芯片贴到卡片内边距，滚动时不会左右跳动 */
    margin: 0 -4px;
    padding-left: 4px;
    padding-right: 4px;
  }
  .trend-chips::-webkit-scrollbar { display: none; }
  .trend-chip {
    flex-shrink: 0;
  }
}
</style>
