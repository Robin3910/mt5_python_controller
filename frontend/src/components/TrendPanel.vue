<script setup lang="ts">
// 节点详情「趋势面板」：按币种 / 周期实时查看 EMA + RSI 合成的趋势得分。
// 面板只负责选品种、调参数与展示；指标算法、权重换算与多空判定全在后端
// （trend_indicators），前端不承载任何交易决策。
// 打开面板后按选定间隔轮询 GET /api/nodes/{id}/trend；改参数会立刻带覆盖项重取一次
// （后端行情缓存会拦住重复的终端查询），点保存才写入全局配置（全后台共用）。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
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
import { useHubStore } from '@/stores/hub'
import type { TrendConfig, TrendPanelData } from '@/api/types'
import {
  TREND_DEFAULTS,
  TREND_REFRESH_OPTIONS,
  TREND_TIMEFRAMES,
  rsiStateLabel,
  timeframeLabel,
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
}>()

const hub = useHubStore()

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

async function save(): Promise<void> {
  saving.value = true
  try {
    const saved = await hub.saveTrendConfig({ ...form })
    Object.assign(form, toTrendForm(saved))
    savedTip.value = '已保存为全局默认参数（所有节点共用）'
    setTimeout(() => (savedTip.value = ''), 2500)
  } catch (e) {
    error.value = extractError(e)
  } finally {
    saving.value = false
  }
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

function fmtTime(seconds: number | null | undefined): string {
  if (!seconds) return '—'
  return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
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
      { left: 60, right: 18, top: 14, height: '54%' },
      { left: 60, right: 18, top: '72%', height: '20%' },
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

watch(data, (next) => {
  if (!next || !next.bars.length) return
  if (!chart && chartEl.value) chart = echarts.init(chartEl.value)
  chart?.setOption(buildOption(next))
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
  <div :class="{ 'trend-compact': compact }">
    <!-- 趋势总览 -->
    <div class="card card-pad" :style="{ marginBottom: compact ? 0 : '16px' }">
      <div class="row between" style="align-items: flex-start">
        <div>
          <strong>{{ data?.symbol || symbol || '—' }}</strong>
          <span class="muted trend-sub">{{ timeframeLabel(cfg.timeframe) }}</span>
        </div>
        <div class="right">
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

      <div class="trend-foot muted">
        <span>权重：EMA {{ emaWeightPct }}% + RSI {{ rsiWeightPct }}%</span>
        <span>阈值：多头 &gt; {{ cfg.bullish }} ｜ 中性 {{ cfg.bearish }}~{{ cfg.bullish }} ｜ 空头 &lt; {{ cfg.bearish }}</span>
        <span>最后更新：{{ fmtTime(data?.updated_at) }}</span>
        <span>K 线时间：{{ fmtTime(data?.bar_time) }}</span>
      </div>

      <div v-if="compact" class="trend-compact-tools">
        <label class="trend-field">
          <span class="trend-field-k">周期</span>
          <select v-model="form.timeframe" class="trend-select">
            <option v-for="t in TREND_TIMEFRAMES" :key="t.value" :value="t.value">
              {{ t.label }}
            </option>
          </select>
        </label>
        <button class="btn-sm btn-ghost" :disabled="loading || !symbol" @click="load">
          {{ loading ? '读取中…' : '刷新' }}
        </button>
      </div>
      <div v-if="compact && symbolOptions?.length" class="trend-chips">
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
      <p v-if="!online" class="trend-warn">节点当前离线，无法读取实时行情。</p>
      <p v-else-if="error" class="trend-warn">{{ error }}</p>
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
            <select v-model="form.timeframe" class="trend-select">
              <option v-for="t in TREND_TIMEFRAMES" :key="t.value" :value="t.value">
                {{ t.label }}
              </option>
            </select>
          </label>
          <label class="trend-field">
            <span class="trend-field-k">自动刷新</span>
            <select v-model.number="refreshMs" class="trend-select">
              <option v-for="r in TREND_REFRESH_OPTIONS" :key="r.value" :value="r.value">
                {{ r.label }}
              </option>
            </select>
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

    <!-- 参数 -->
    <div v-if="!compact" class="card card-pad" style="margin-bottom: 16px">
      <div class="row between" style="align-items: flex-start; margin-bottom: 12px">
        <div>
          <strong>参数</strong>
          <p class="muted trend-hint" style="margin: 4px 0 0">
            改动即时生效于本次查看；点「保存」写入全局配置（不分节点/币种），下次打开与其他管理员都沿用
          </p>
        </div>
        <div class="row" style="gap: 8px">
          <button class="btn-sm btn-ghost" @click="resetToDefaults">恢复默认</button>
          <button class="btn-primary btn-sm" :disabled="saving" @click="save">
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

.trend-input,
.trend-select {
  background: rgba(12, 18, 32, 0.6);
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  padding: 7px 10px;
  min-width: 116px;
}
.trend-input:focus,
.trend-select:focus { border-color: var(--primary); outline: none; }
.trend-input:disabled { color: var(--muted); }

.trend-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.trend-chip {
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: 999px;
  color: var(--muted);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 11px;
  padding: 4px 10px;
  transition: var(--transition);
}
.trend-chip:hover { color: var(--text); border-color: var(--glass-highlight); }
.trend-chip.active {
  background: rgba(0, 212, 170, 0.12);
  border-color: var(--border-bright);
  color: var(--primary);
}

.trend-hint { font-size: 12px; margin: 10px 0 0; line-height: 1.5; }
.trend-warn { color: var(--gold); font-size: 12px; margin: 10px 0 0; }
.trend-compact-tools { display: flex; align-items: flex-end; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.trend-compact .trend-score { font-size: 26px; }
.trend-compact .trend-chips { margin-top: 8px; }
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

@media (max-width: 720px) {
  .trend-chart { height: 320px; }
  .trend-foot { gap: 4px 12px; }
}
</style>
