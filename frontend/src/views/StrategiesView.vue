<script setup lang="ts">
// 策略管理页：基于模版新建策略、绑定品种，选模版后可自定义规则参数
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  BatchCalcType,
  BatchTimeframe,
  EntryDirection,
  GridMode,
  GridSide,
  StrategyBatchLevel,
  StrategyOut,
  StrategyRule,
  StrategyTemplateOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

/** ATR / 波幅统计的已收盘 K 线根数，与后端 BATCH_BAR_PERIOD 一致，固定不可配 */
const BATCH_BAR_PERIOD = 14

/** 规则 type，与后端 strategy_templates 对齐 */
const RULE_TYPE_COUNTER = 1
const RULE_TYPE_TREND = 2
const RULE_TYPE_RISK_SIZED = 3
const RULE_TYPE_GRID = 4

const hub = useHubStore()
const router = useRouter()

const searchQuery = ref('')
const appliedQuery = ref('')
const loading = ref(false)
const templates = ref<StrategyTemplateOut[]>([])

function currentSearchOptions(): { q?: string } {
  return appliedQuery.value ? { q: appliedQuery.value } : {}
}

async function loadStrategies(): Promise<void> {
  loading.value = true
  try {
    await hub.fetchStrategies(currentSearchOptions())
  } finally {
    loading.value = false
  }
}

async function runSearch(): Promise<void> {
  appliedQuery.value = searchQuery.value.trim()
  await loadStrategies()
}

onMounted(async () => {
  await Promise.all([
    loadStrategies(),
    hub.fetchStrategyTemplates().then((list) => {
      templates.value = list
    }),
  ])
})

const RULE_TYPE_LABEL: Record<number, string> = {
  [RULE_TYPE_COUNTER]: '逆势加仓',
  [RULE_TYPE_TREND]: '顺势加仓',
  [RULE_TYPE_RISK_SIZED]: '以损定量趋势单',
  [RULE_TYPE_GRID]: '网格交易',
}

const RULE_TYPE_HELP: Record<number, string> = {
  [RULE_TYPE_COUNTER]:
    '逆势加仓：以监控方向最近一笔订单为基准，价格朝不利方向偏离达到「点数 × Point()」后触发加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
  [RULE_TYPE_TREND]:
    '顺势加仓：以监控方向最近一笔订单为基准，价格朝有利方向偏离达到「点数 × Point()」后触发加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
  [RULE_TYPE_RISK_SIZED]:
    '以损定量趋势单：不使用信号手数，而是按「风险金额 ÷ 止损距离」反推总手数，' +
    '底仓先市价成交，剩余仓位按间距分批补齐。所有批次共用信号那一个止损价，' +
    '因此无论补进几批，打到止损的总亏损始终等于风险金额。' +
    '信号必须携带止损价（sl），否则本策略不参与分发。',
  [RULE_TYPE_GRID]:
    '网格交易（复刻币安现货手动网格）：在价格区间内按等差或等比切格，' +
    '下跌穿越网格线买入、上涨穿越卖出对应格，反复吃差价。' +
    '空仓是正常运行态，任务不会因持仓归零而结束；只由止损价 / 止盈价 / 终止信号收口。',
}

function isRiskSized(rule: { type: number }): boolean {
  return rule.type === RULE_TYPE_RISK_SIZED
}

function isGrid(rule: { type: number }): boolean {
  return rule.type === RULE_TYPE_GRID
}

const FIELD_HELP = {
  template: '选择策略模版后，会载入模版默认规则；可在下方自定义各参数后再创建。',
  name: '策略名称，全局唯一。便于在列表与审计中辨识。',
  symbol: '绑定品种代码，如 XAUUSD。策略规则仅作用于该品种。',
  rules:
    '每条规则独立配置。关闭「启用」后该规则不会执行。' +
    '实际加仓手数 = 倍数 × 基础订单手数 + 额外手数；触发距离 = 点数 × Point()。',
  status:
    '关闭后本条规则不会参与监控与加仓；已产生的历史订单不受影响。',
  action:
    '监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '系统会按该方向取最近一笔订单作为加仓基准。',
  point:
    '加仓触发点数。实时以监控方向最近订单为基准，价格偏离达到「点数 × Point()」后开始执行加仓。' +
    'Point() 为品种最小价格变动单位。',
  lot_times:
    '加仓倍数。以监控方向最近订单的手数为基准，参与计算：倍数 × 基础手数。',
  extra_lot:
    '额外手数，默认 0。加在倍率结果之后：实际手数 = 倍数 × 基础手数 + 额外手数。',
  max_allow_num:
    '最大允许加仓次数。达到次数上限后，本条规则不再继续加仓。',
  batch_enabled:
    '开启后按当前持仓笔数命中下方档位，使用该档的点数 / 倍数 / 额外手数，' +
    '覆盖上方基础参数。档位由批数与总手数自动生成，不可手动增删。',
  batch_action:
    '分批加仓独立的监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '可与上方基础监控方向不同。',
  batch_count:
    '分批批数。修改后会按「第 2 笔 ~ 总手数」自动均分生成对应档位区间，不可手动添加档位。',
  total_lot_limit:
    '分批总手数上限，同时作为档位末笔上限。' +
    '系统会把第 2 笔到该上限均分到各档；达到上限后不再继续分批加仓。',
  batch_level:
    '持仓笔数区间由批数与总手数自动计算，只读。' +
    '命中区间时按该档的间距判断是否加仓，手数 = 倍数 × 基础手数 + 额外手数。',
  calc_type:
    '本档加仓间距怎么算：\n' +
    '点数 = 固定间距，偏离达到「点数 × Point()」触发；\n' +
    '指定价 = 到价触发，多单要求价位低于参考价、空单要求高于参考价；\n' +
    `ATR = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线的平均真实波幅作间距；\n` +
    `波幅 = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线中最大的高低波幅作间距。`,
  batch_price:
    '本档的指定价位（绝对价格）。逆势时多单需低于参考价、空单需高于参考价；' +
    '顺势方向相反。价位方向不符或留空时本档不会触发。',
  batch_timeframe:
    `统计 ATR / 波幅用的 K 线周期，固定取最近 ${BATCH_BAR_PERIOD} 根**已收盘** K 线（不含当前未走完的那根）。` +
    '算出的价格距离会换算成点数，再与实际偏离比较。',
  risk_amount:
    '本次交易愿意承担的亏损金额（账户货币，通常是美元）。\n' +
    '总手数 = 风险金额 ÷ 每手止损亏损，其中每手止损亏损由止损距离与品种合约规格算出。\n' +
    '手数按品种步长向下取整，因此实际风险只会小于该值，不会超出。',
  rr_ratio:
    '盈亏比。止盈距离 = 止损距离 × 该值，止盈价由节点在底仓成交后按真实成交价挂出。\n' +
    '填 0 表示不设止盈，仅靠止损与人工干预出场。',
  base_ratio:
    '底仓占总手数的百分比，底仓以市价立即成交。\n' +
    '剩余仓位交给下方的补仓批数分批补齐；补仓批数为 0 时底仓即全仓。',
  add_batches:
    '剩余仓位分几批补齐。0 表示不分批，总手数一次性由底仓成交。\n' +
    '若剩余手数不足以让每批都达到品种最小手数，节点会自动减少批数。',
  entry_direction:
    '补仓的价格方向：\n' +
    '回撤补仓 = 价格朝不利方向走时补齐，摊低持仓均价（常规用法）；\n' +
    '突破加仓 = 价格朝有利方向走时补齐，顺势追势。',
  batch_gap_points:
    '相邻批次的触发间距（点），以底仓实际成交价为起点逐批累加。\n' +
    '回撤补仓的触发价必须留在止损之内、突破加仓必须留在止盈之内，' +
    '所以间距超出可用区间时节点会自动压缩，避免最远那批永远不成交。',
  max_total_lot:
    '总手数硬上限，0 表示不额外限制（仍受品种最大手数约束）。\n' +
    '被上限截断时实际风险会小于风险金额。',
  breakeven_enabled:
    '开启后，浮盈达到「止损距离 × 倍数」时把该任务全部持仓的止损移到持仓加权均价，' +
    '此后这笔交易最差打平。只会触发一次。',
  breakeven_times:
    '保本触发的倍数 N：浮盈价格距离 ≥ 止损距离 × N 时移动止损。\n' +
    '例如止损距离 300 点、N=1，则浮盈 300 点后止损挪到均价。',
  price_lower: '网格价格区间下限。须大于 0，且小于上限。',
  price_upper: '网格价格区间上限。须大于下限。',
  grid_count: '网格数量（2–200）。区间会被切成该数量的格子（N+1 条网格线）。',
  grid_mode:
    '分格方式：\n等差 = 每格价格间距相等；\n等比 = 每格涨跌幅比例相等（适合宽区间）。',
  grid_side:
    '网格方向：\n只做多 = 跌买涨卖（复刻币安现货）；\n只做空 = 涨卖跌买；\n跟随信号 = 按触发信号的 BUY/SELL 决定方向。',
  lot_per_grid: '每一格买入/卖出的手数。对应币安网格的「投资额」换算结果。',
  trigger_price: '触发价。填 0 表示信号到达后立即启动；否则等到价触及该价才建网格。',
  stop_lower: '止损价，须低于区间下限；填 0 表示不设。多头网格跌破此价终止。',
  stop_upper: '止盈价，须高于区间上限；填 0 表示不设。多头网格涨破此价终止。',
  close_on_stop: '触发止损/止盈时是否清掉该任务全部持仓。关闭则只停止监控、保留持仓。',
  prefill_enabled:
    '开启后启动时按「现价上方格位数 × 每格手数」市价买入底仓（复刻币安现货网格），' +
    '否则价格上涨时无货可卖、上半部分网格失效。',
  grid_total_lot_limit: '全部格位合计手数上限，0=不额外限制。初始建仓也会受此约束。',
  trailing_up:
    '向上追踪（复刻币安同名功能）：价格越过区间外沿时网格不停机，' +
    '整个区间连同止损价 / 止盈价一起平移一格，继续在新区间吃差价。\n' +
    '多头网格追涨（突破上限上移），空头网格追跌（跌破下限下移）。\n' +
    '注意止盈价也会同步上移，所以开启追踪后止盈基本不会触发，两者通常只用其一。',
  trailing_max:
    '最多允许平移多少格，0 表示不限。\n' +
    '不限时只要不触发止损，网格会一直跟着行情滚动。',
}

/** 补仓方向选项，与后端 ENTRY_DIRECTIONS 对齐 */
const ENTRY_DIRECTION_OPTIONS: Array<{ value: EntryDirection; label: string }> = [
  { value: 'pullback', label: '回撤补仓' },
  { value: 'breakout', label: '突破加仓' },
]

const GRID_MODE_OPTIONS: Array<{ value: GridMode; label: string }> = [
  { value: 'arithmetic', label: '等差' },
  { value: 'geometric', label: '等比' },
]

const GRID_SIDE_OPTIONS: Array<{ value: GridSide; label: string }> = [
  { value: 'long', label: '只做多' },
  { value: 'short', label: '只做空' },
  { value: 'follow', label: '跟随信号' },
]

/**
 * 表单内规则：所有字段都已填充，便于直接 v-model 绑定。
 * 各组字段都会补齐，提交后由后端按 type 只保留对应的一组。
 */
type EditableRule = Required<Omit<StrategyRule, 'batch_levels' | 'grid_mode' | 'grid_side' | 'entry_direction'>> & {
  batch_levels: StrategyBatchLevel[]
  entry_direction: EntryDirection
  grid_mode: GridMode
  grid_side: GridSide
}

function cloneRules(rules: StrategyRule[]): EditableRule[] {
  return rules.map((r) => ({
    type: r.type,
    status: r.status,
    action: r.action,
    point: r.point ?? 100,
    lot_times: r.lot_times ?? 1,
    extra_lot: r.extra_lot ?? 0,
    max_allow_num: r.max_allow_num ?? 5,
    batch_enabled: r.batch_enabled ?? false,
    batch_action: r.batch_action ?? 'all',
    batch_count: r.batch_count ?? 0,
    total_lot_limit: r.total_lot_limit ?? 0,
    batch_levels: (r.batch_levels || []).map((lv) => ({ ...lv })),
    risk_amount: r.risk_amount ?? 300,
    rr_ratio: r.rr_ratio ?? 2.5,
    base_ratio: r.base_ratio ?? 30,
    add_batches: r.add_batches ?? 2,
    entry_direction: r.entry_direction ?? 'pullback',
    batch_gap_points: r.batch_gap_points ?? 100,
    max_total_lot: r.max_total_lot ?? 0,
    breakeven_enabled: r.breakeven_enabled ?? false,
    breakeven_times: r.breakeven_times ?? 1,
    price_lower: r.price_lower ?? 0,
    price_upper: r.price_upper ?? 0,
    grid_count: r.grid_count ?? 10,
    grid_mode: r.grid_mode ?? 'arithmetic',
    grid_side: r.grid_side ?? 'long',
    lot_per_grid: r.lot_per_grid ?? 0.01,
    trigger_price: r.trigger_price ?? 0,
    stop_lower: r.stop_lower ?? 0,
    stop_upper: r.stop_upper ?? 0,
    close_on_stop: r.close_on_stop ?? true,
    prefill_enabled: r.prefill_enabled ?? true,
    trailing_up: r.trailing_up ?? false,
    trailing_max: r.trailing_max ?? 0,
  }))
}

/** 分批档位从第 2 笔起算（第 1 笔为首单） */
const BATCH_POS_START = 2

/** 加仓间距的计算方式；与后端 strategy_templates.BATCH_CALC_TYPES 对齐 */
const CALC_TYPE_OPTIONS: Array<{ value: BatchCalcType; label: string }> = [
  { value: 'point', label: '点数' },
  { value: 'price', label: '指定价' },
  { value: 'atr', label: 'ATR' },
  { value: 'range', label: '波幅' },
]

/** ATR / 波幅可选周期。不提供「当前图表周期」：节点是独立进程，没有图表上下文 */
const TIMEFRAME_OPTIONS: BatchTimeframe[] = [
  'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN',
]

/** 需要读 K 线才能算出间距的方式 */
const BAR_CALC_TYPES: BatchCalcType[] = ['atr', 'range']

function isBarCalc(calcType: BatchCalcType): boolean {
  return BAR_CALC_TYPES.includes(calcType)
}

/** 将 [2, limit] 均分为 count 段，对齐 MTcommander「批数 × 总手数」切档 */
function splitBatchRanges(
  limit: number,
  count: number,
): Array<{ pos_from: number; pos_to: number }> {
  const end = Math.max(BATCH_POS_START, Math.floor(limit) || BATCH_POS_START)
  const total = end - BATCH_POS_START + 1
  const n = Math.max(1, Math.min(Math.floor(count) || 1, total))
  const base = Math.floor(total / n)
  const rem = total % n
  const ranges: Array<{ pos_from: number; pos_to: number }> = []
  let cur = BATCH_POS_START
  for (let i = 0; i < n; i++) {
    const size = base + (i < rem ? 1 : 0)
    const to = cur + size - 1
    ranges.push({ pos_from: cur, pos_to: to })
    cur = to + 1
  }
  return ranges
}

type LevelParams = Pick<
  StrategyBatchLevel,
  'calc_type' | 'point' | 'price' | 'timeframe' | 'lot_times' | 'extra_lot'
>

function defaultLevelParams(index: number, prev?: StrategyBatchLevel): LevelParams {
  if (prev) {
    return {
      calc_type: prev.calc_type || 'point',
      point: prev.point,
      price: prev.price ?? 0,
      timeframe: prev.timeframe || 'M5',
      lot_times: prev.lot_times,
      extra_lot: prev.extra_lot,
    }
  }
  return {
    calc_type: 'point',
    point: 100 + index * 100,
    price: 0,
    timeframe: 'M5',
    lot_times: Number((1.1 + index * 0.1).toFixed(2)),
    extra_lot: 0,
  }
}

/** 按 batch_count + total_lot_limit 重建档位；保留已有档的点/倍/手参数 */
function rebuildBatchLevels(r: EditableRule): void {
  const limit = Math.max(BATCH_POS_START, Math.floor(Number(r.total_lot_limit) || BATCH_POS_START))
  r.total_lot_limit = limit
  const maxCount = limit - BATCH_POS_START + 1
  let count = Math.max(1, Math.floor(Number(r.batch_count) || 1))
  if (count > maxCount) count = maxCount
  r.batch_count = count

  const old = r.batch_levels
  r.batch_levels = splitBatchRanges(limit, count).map((range, i) => ({
    ...range,
    ...defaultLevelParams(i, old[i]),
  }))
}

function setBatchEnabled(r: EditableRule, enabled: boolean): void {
  r.batch_enabled = enabled
  if (!enabled) return
  if (!r.batch_count) r.batch_count = 3
  if (!r.total_lot_limit || r.total_lot_limit < BATCH_POS_START) r.total_lot_limit = 10
  rebuildBatchLevels(r)
}

function onBatchMetaChange(r: EditableRule): void {
  if (!r.batch_enabled) return
  rebuildBatchLevels(r)
}

// ---- 新建 / 编辑 ----
const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const editingTemplateName = ref('')
const saving = ref(false)
const formError = ref('')
const form = reactive({
  template_id: '',
  name: '',
  symbol: '',
  rules: [] as EditableRule[],
})

const isEditMode = computed(() => formMode.value === 'edit')

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.template_id === form.template_id) || null,
)

const formTemplateLabel = computed(() => {
  if (selectedTemplate.value) return selectedTemplate.value.name
  return editingTemplateName.value || form.template_id || '—'
})

function loadRulesFromTemplate(templateId: string): void {
  const tpl = templates.value.find((t) => t.template_id === templateId)
  form.rules = tpl ? cloneRules(tpl.rules) : []
}

watch(
  () => form.template_id,
  (id) => {
    // 仅新建时切换模版会重载默认规则；编辑不允许改模版
    if (showForm.value && !isEditMode.value && id) loadRulesFromTemplate(id)
  },
)

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  editingTemplateName.value = ''
  formError.value = ''
  form.template_id = templates.value[0]?.template_id || ''
  form.name = ''
  form.symbol = ''
  loadRulesFromTemplate(form.template_id)
  showForm.value = true
}

function openEdit(s: StrategyOut): void {
  formMode.value = 'edit'
  editingId.value = s.strategy_id
  editingTemplateName.value = s.template_name || ''
  formError.value = ''
  form.template_id = s.template_id
  form.name = s.name
  form.symbol = s.symbol
  form.rules = cloneRules(s.rules || [])
  showForm.value = true
}

/** 以损定量趋势单的参数校验 */
function validateRiskSized(r: EditableRule, label: string): string | null {
  if (!(r.risk_amount > 0)) return `${label}：风险金额需大于 0`
  if (r.rr_ratio < 0) return `${label}：盈亏比不能为负`
  if (!(r.base_ratio > 0) || r.base_ratio > 100) return `${label}：底仓比例需在 1 ~ 100 之间`
  if (r.add_batches < 0) return `${label}：补仓批数不能为负`
  if (!ENTRY_DIRECTION_OPTIONS.some((o) => o.value === r.entry_direction)) {
    return `${label}：补仓方向非法`
  }
  if (r.add_batches > 0 && !(r.batch_gap_points > 0)) {
    return `${label}：分批补仓需填写大于 0 的批次间距`
  }
  if (r.max_total_lot < 0) return `${label}：总手数上限不能为负`
  if (r.breakeven_enabled && !(r.breakeven_times > 0)) {
    return `${label}：保本触发倍数需大于 0`
  }
  return null
}

/** 网格交易的参数校验 */
function validateGrid(r: EditableRule, label: string): string | null {
  if (!(r.price_lower > 0)) return `${label}：区间下限需大于 0`
  if (!(r.price_upper > r.price_lower)) return `${label}：区间上限须大于下限`
  if (r.grid_count < 2 || r.grid_count > 200) return `${label}：网格数量需在 2 ~ 200`
  if (!GRID_MODE_OPTIONS.some((o) => o.value === r.grid_mode)) {
    return `${label}：网格模式非法`
  }
  if (r.grid_mode === 'geometric' && !(r.price_lower > 0)) {
    return `${label}：等比网格要求区间下限大于 0`
  }
  if (!GRID_SIDE_OPTIONS.some((o) => o.value === r.grid_side)) {
    return `${label}：网格方向非法`
  }
  if (!(r.lot_per_grid > 0)) return `${label}：每格手数需大于 0`
  if (r.total_lot_limit < 0) return `${label}：总手数上限不能为负`
  if (r.trigger_price < 0) return `${label}：触发价不能为负`
  if (r.stop_lower > 0 && r.stop_lower >= r.price_lower) {
    return `${label}：止损价须低于区间下限`
  }
  if (r.stop_upper > 0 && r.stop_upper <= r.price_upper) {
    return `${label}：止盈价须高于区间上限`
  }
  if (r.trailing_max < 0) return `${label}：最大平移格数不能为负`
  return null
}

/** 去掉尾随零的定长格式化 */
function trimNum(value: number, digits = 6): string {
  return value.toFixed(digits).replace(/\.?0+$/, '')
}

/** 网格间距 / 预估占用手数提示 */
function gridHint(r: EditableRule): string {
  const n = r.grid_count || 0
  const lower = r.price_lower || 0
  const upper = r.price_upper || 0
  if (!(upper > lower) || n < 2) return '请填写合法的价格区间与网格数量'
  const gap =
    r.grid_mode === 'geometric'
      ? `${(((upper / lower) ** (1 / n) - 1) * 100).toFixed(4)}%`
      : trimNum((upper - lower) / n)
  const lot = r.lot_per_grid || 0
  const maxLot = lot * n
  const modeLabel = GRID_MODE_OPTIONS.find((o) => o.value === r.grid_mode)?.label || r.grid_mode
  return (
    `${modeLabel}间距 ≈ ${gap}；满仓约 ${trimNum(maxLot, 4)} 手` +
    (r.total_lot_limit > 0 ? `（上限 ${r.total_lot_limit}）` : '')
  )
}

/** 向上追踪的效果预览：平移一格后的新区间 */
function trailingHint(r: EditableRule): string {
  const n = r.grid_count || 0
  const lower = r.price_lower || 0
  const upper = r.price_upper || 0
  if (!(upper > lower) || n < 2) return '填好价格区间与网格数量后可预览平移效果'
  const down = r.grid_side === 'short'
  const geometric = r.grid_mode === 'geometric'
  const ratio = (upper / lower) ** (1 / n)
  const step = (upper - lower) / n
  const move = (v: number): number =>
    geometric ? (down ? v / ratio : v * ratio) : down ? v - step : v + step
  return (
    `${down ? '跌破下限' : '突破上限'}后网格${down ? '下移' : '上移'}一格 → ` +
    `[${trimNum(move(lower))}, ${trimNum(move(upper))}]，止损 / 止盈同步；` +
    (r.trailing_max > 0 ? `最多平移 ${r.trailing_max} 格` : '不限平移次数')
  )
}

function validateRules(rules: EditableRule[]): string | null {
  if (!rules.length) return '请至少配置一条规则'
  for (const r of rules) {
    const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
    if (!['all', 'buy', 'sell'].includes(String(r.action || '').toLowerCase())) {
      return `${label}：监控方向非法`
    }
    if (isRiskSized(r)) {
      const err = validateRiskSized(r, label)
      if (err) return err
      continue
    }
    if (isGrid(r)) {
      const err = validateGrid(r, label)
      if (err) return err
      continue
    }
    if (r.point < 0) return `${label}：点数不能为负`
    if (r.lot_times < 0) return `${label}：倍数不能为负`
    if (r.extra_lot < 0) return `${label}：手数不能为负`
    if (r.max_allow_num < 0) return `${label}：次数不能为负`
    if (!r.batch_enabled) continue
    if (!['all', 'buy', 'sell'].includes(String(r.batch_action || '').toLowerCase())) {
      return `${label}：分批监控方向非法`
    }
    if (r.batch_count < 1) return `${label}：分批批数至少为 1`
    if (r.total_lot_limit < BATCH_POS_START) {
      return `${label}：总手数上限需 ≥ ${BATCH_POS_START}（档位从第 ${BATCH_POS_START} 笔起）`
    }
    if (!r.batch_levels.length) return `${label}：启用分批加仓后至少需要一个档位`
    const last = r.batch_levels[r.batch_levels.length - 1]
    if (r.batch_levels.length !== r.batch_count) {
      return `${label}：档位数与批数不一致，请调整总手数或批数后重试`
    }
    if (last.pos_to !== Math.floor(r.total_lot_limit)) {
      return `${label}：档位末笔需等于总手数上限`
    }
    for (const [i, lv] of r.batch_levels.entries()) {
      const at = `${label} 档位 ${i + 1}`
      if (!CALC_TYPE_OPTIONS.some((o) => o.value === lv.calc_type)) {
        return `${at}：计算方式非法`
      }
      if (lv.calc_type === 'point' && !(lv.point > 0)) return `${at}：点数需大于 0`
      if (lv.calc_type === 'price' && !(lv.price > 0)) return `${at}：请填写指定价位`
      if (isBarCalc(lv.calc_type) && !TIMEFRAME_OPTIONS.includes(lv.timeframe)) {
        return `${at}：请选择 K 线周期`
      }
      if (lv.point < 0) return `${at}：点数不能为负`
      if (lv.lot_times < 0) return `${at}：倍数不能为负`
      if (lv.extra_lot < 0) return `${at}：手数不能为负`
    }
  }
  return null
}

async function save(): Promise<void> {
  if (!form.template_id) {
    formError.value = '请选择策略模版'
    return
  }
  const name = form.name.trim()
  if (!name) {
    formError.value = '请填写策略名称'
    return
  }
  const symbol = form.symbol.trim().toUpperCase()
  if (!symbol) {
    formError.value = '请填写绑定品种'
    return
  }
  const rulesErr = validateRules(form.rules.map((r) => {
    if (!isRiskSized(r) && !isGrid(r) && r.batch_enabled) rebuildBatchLevels(r)
    return r
  }))
  if (rulesErr) {
    formError.value = rulesErr
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const tplName = formTemplateLabel.value
    const enabledCount = form.rules.filter((r) => r.status === 1).length
    const actionLabel = isEditMode.value ? '保存' : '创建'
    if (
      !(await confirmAction(
        `确认${actionLabel}策略「${name}」？\n\n模版：${tplName}\n绑定品种：${symbol}\n启用规则：${enabledCount} / ${form.rules.length}`,
      ))
    ) {
      return
    }
    try {
      const rules = cloneRules(form.rules)
      if (isEditMode.value) {
        await hub.updateStrategy(
          editingId.value,
          { name, symbol, rules },
          currentSearchOptions(),
        )
      } else {
        await hub.createStrategy(
          {
            template_id: form.template_id,
            name,
            symbol,
            rules,
          },
          currentSearchOptions(),
        )
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      formError.value = err?.response?.data?.detail || `${actionLabel}失败，请稍后重试`
      await ElMessageBox.alert(formError.value, '无法保存', {
        type: 'warning',
        confirmButtonText: '知道了',
      })
      return
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(s: StrategyOut): Promise<void> {
  const next = s.enabled ? '禁用' : '启用'
  if (!(await confirmAction(`确认${next}策略「${s.name}」？`))) return
  await hub.updateStrategy(s.strategy_id, { enabled: !s.enabled }, currentSearchOptions())
}

async function remove(s: StrategyOut): Promise<void> {
  if (!(await confirmAction(`确认删除策略「${s.name}」？\n\n该操作不可恢复。`, '确认删除'))) return
  await hub.deleteStrategy(s.strategy_id, currentSearchOptions())
}

function ruleSummary(rules: StrategyRule[]): string {
  if (!rules.length) return '—'
  const enabled = rules.filter((r) => r.status === 1)
  if (!enabled.length) return '全部关闭'
  return enabled
    .map((r) => {
      const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
      if (isRiskSized(r)) {
        const batches = r.add_batches ?? 0
        return `${label}（风险 ${r.risk_amount ?? 0} · 盈亏比 ${r.rr_ratio ?? 0}${batches ? ` · 分 ${batches} 批补仓` : ''}）`
      }
      if (isGrid(r)) {
        const side = GRID_SIDE_OPTIONS.find((o) => o.value === r.grid_side)?.label || r.grid_side
        const mode = GRID_MODE_OPTIONS.find((o) => o.value === r.grid_mode)?.label || r.grid_mode
        const trailing = r.trailing_up ? ' · 追踪' : ''
        return `${label}（${r.grid_count ?? 0} 格${mode} · ${side} · 每格 ${r.lot_per_grid ?? 0}${trailing}）`
      }
      const levels = r.batch_levels?.length ?? 0
      return r.batch_enabled && levels ? `${label}（分批 ${levels} 档）` : label
    })
    .join(' · ')
}

const ACTION_LABEL: Record<string, string> = {
  all: '全部',
  buy: '多单',
  sell: '空单',
}

const CALC_TYPE_LABEL: Record<BatchCalcType, string> = {
  point: '点数',
  price: '指定价',
  atr: 'ATR',
  range: '波幅',
}

const expandedIds = ref<Set<string>>(new Set())

function toggleExpand(id: string): void {
  const next = new Set(expandedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedIds.value = next
}

function isExpanded(id: string): boolean {
  return expandedIds.value.has(id)
}

function actionLabel(action: string | undefined | null): string {
  const key = String(action || '').toLowerCase()
  return ACTION_LABEL[key] || action || '—'
}

function calcTypeLabel(calcType: BatchCalcType | string | undefined): string {
  const key = (calcType || 'point') as BatchCalcType
  return CALC_TYPE_LABEL[key] || String(calcType || '点数')
}

function entryDirectionLabel(direction: string | undefined): string {
  return ENTRY_DIRECTION_OPTIONS.find((o) => o.value === direction)?.label || direction || '—'
}

/** 规则详情的参数行；各规则类型的字段集合不同，展示由此按 type 分派 */
function ruleDetailRows(r: StrategyRule): Array<{ k: string; v: string }> {
  if (isRiskSized(r)) {
    const batches = r.add_batches ?? 0
    return [
      { k: '监控方向', v: actionLabel(r.action) },
      { k: '风险金额', v: String(r.risk_amount ?? 0) },
      { k: '盈亏比', v: String(r.rr_ratio ?? 0) },
      { k: '底仓', v: `${batches ? (r.base_ratio ?? 0) : 100}%` },
      {
        k: '分批补仓',
        v: batches
          ? `${entryDirectionLabel(r.entry_direction)} ${batches} 批 · 间距 ${r.batch_gap_points ?? 0} 点`
          : '不分批',
      },
      { k: '总手数上限', v: r.max_total_lot ? String(r.max_total_lot) : '不限' },
      {
        k: '保本触发',
        v: r.breakeven_enabled ? `止损距 × ${r.breakeven_times ?? 0} 倍` : '未启用',
      },
    ]
  }
  if (isGrid(r)) {
    const side = GRID_SIDE_OPTIONS.find((o) => o.value === r.grid_side)?.label || r.grid_side || '—'
    const mode = GRID_MODE_OPTIONS.find((o) => o.value === r.grid_mode)?.label || r.grid_mode || '—'
    return [
      { k: '价格区间', v: `${r.price_lower ?? 0} ~ ${r.price_upper ?? 0}` },
      { k: '网格', v: `${r.grid_count ?? 0} 格 · ${mode}` },
      { k: '方向', v: side },
      { k: '每格手数', v: String(r.lot_per_grid ?? 0) },
      { k: '总手数上限', v: r.total_lot_limit ? String(r.total_lot_limit) : '不限' },
      { k: '触发价', v: r.trigger_price ? String(r.trigger_price) : '立即启动' },
      { k: '止损 / 止盈', v: `${r.stop_lower || '不设'} / ${r.stop_upper || '不设'}` },
      { k: '终止清仓', v: r.close_on_stop === false ? '否' : '是' },
      { k: '初始建仓', v: r.prefill_enabled === false ? '关闭' : '开启' },
      {
        k: '向上追踪',
        v: r.trailing_up ? (r.trailing_max ? `开启 · 最多 ${r.trailing_max} 格` : '开启 · 不限') : '关闭',
      },
    ]
  }
  return [
    { k: '监控方向', v: actionLabel(r.action) },
    { k: '触发点数', v: String(r.point ?? 0) },
    { k: '倍数', v: String(r.lot_times ?? 0) },
    { k: '额外手数', v: String(r.extra_lot ?? 0) },
    { k: '最大加仓次数', v: String(r.max_allow_num ?? 0) },
  ]
}

/** 档位间距摘要：按计算方式展示点数 / 价位 / ATR·波幅周期 */
function levelSpacingText(lv: StrategyBatchLevel): string {
  const kind = (lv.calc_type || 'point') as BatchCalcType
  if (kind === 'price') return `价位 ${lv.price ?? 0}`
  if (kind === 'atr') return `ATR(${lv.timeframe || 'M5'}×${BATCH_BAR_PERIOD})`
  if (kind === 'range') return `波幅(${lv.timeframe || 'M5'}×${BATCH_BAR_PERIOD})`
  return `${lv.point ?? 0} 点`
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function resetRuleToTemplate(idx: number): void {
  const tplRule = selectedTemplate.value?.rules?.[idx]
  if (!tplRule || !form.rules[idx]) return
  Object.assign(form.rules[idx], cloneRules([tplRule])[0])
}
</script>

<template>
  <div class="strategies-page">
    <div class="row between page-header">
      <div>
        <div class="h1">策略管理</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          基于策略模版创建实例并绑定品种；模版1 配逆势 / 顺势加仓，模版2 配以损定量趋势单，模版3 配网格交易
        </p>
      </div>
      <div class="row" style="gap: 8px">
        <button class="btn-ghost" @click="router.push('/groups')">← 返回分组</button>
        <button class="btn-primary" @click="openCreate">+ 新增策略</button>
      </div>
    </div>

    <div class="card card-pad" style="margin-bottom: 12px">
      <div class="row" style="gap: 8px">
        <input
          v-model="searchQuery"
          type="search"
          placeholder="搜索策略名称 / 品种…"
          aria-label="搜索策略"
          :disabled="loading"
          style="width: 25%"
          @keydown.enter="runSearch"
        />
        <button class="btn-primary btn-sm" :disabled="loading" @click="runSearch">
          {{ loading ? '搜索中…' : '搜索' }}
        </button>
      </div>
      <p v-if="appliedQuery" class="muted" style="font-size: 12px; margin: 8px 0 0">
        {{ hub.strategies.length ? `找到 ${hub.strategies.length} 个策略` : '无匹配策略' }}
      </p>
    </div>

    <!-- 移动端卡片 -->
    <div class="list-cards mobile-only">
      <div
        v-for="s in hub.strategies"
        :key="s.strategy_id"
        class="list-card card clickable"
        @click="toggleExpand(s.strategy_id)"
      >
        <div class="list-card-head row between">
          <strong>
            <span class="muted" style="margin-right: 6px">{{ isExpanded(s.strategy_id) ? '▾' : '▸' }}</span>
            {{ s.name }}
          </strong>
          <span class="tag" :class="s.enabled ? 'green' : ''">{{ s.enabled ? '已启用' : '已禁用' }}</span>
        </div>
        <div class="list-field"><span class="k">策略 ID</span><span class="v muted" style="font-size: 12px">{{ s.strategy_id }}</span></div>
        <div class="list-field"><span class="k">模版</span><span class="v">{{ s.template_name }}</span></div>
        <div class="list-field"><span class="k">绑定品种</span><span class="v"><code>{{ s.symbol }}</code></span></div>
        <div class="list-field"><span class="k">规则</span><span class="v">{{ ruleSummary(s.rules) }}</span></div>
        <div class="list-field"><span class="k">创建时间</span><span class="v muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</span></div>
        <div v-if="isExpanded(s.strategy_id)" class="strategy-detail" @click.stop>
          <div v-if="s.remark" class="muted" style="font-size: 12px; margin-bottom: 8px">备注：{{ s.remark }}</div>
          <div v-if="!s.rules.length" class="muted" style="font-size: 12px">暂无规则</div>
          <div v-for="(r, ri) in s.rules" :key="ri" class="strategy-rule-block">
            <div class="row between" style="margin-bottom: 8px">
              <strong style="font-size: 13px">{{ RULE_TYPE_LABEL[r.type] || `规则 ${ri + 1}` }}</strong>
              <span class="tag" :class="r.status === 1 ? 'green' : ''">{{ r.status === 1 ? '启用' : '关闭' }}</span>
            </div>
            <div class="kv-grid" style="margin-bottom: 8px">
              <div v-for="kv in ruleDetailRows(r)" :key="kv.k" class="kv">
                <span class="k">{{ kv.k }}</span><span class="v">{{ kv.v }}</span>
              </div>
            </div>
            <template v-if="isRiskSized(r)">
              <div class="muted" style="font-size: 12px">
                手数由风险金额与信号止损价反推，各批共用同一止损
              </div>
            </template>
            <template v-else-if="isGrid(r)">
              <div class="muted" style="font-size: 12px">
                网格空仓是正常运行态；只由止损 / 止盈 / 终止信号收口
              </div>
            </template>
            <template v-else-if="r.batch_enabled">
              <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                分批加仓 · 方向 {{ actionLabel(r.batch_action) }} ·
                {{ r.batch_count ?? 0 }} 批 · 总手数上限 {{ r.total_lot_limit ?? 0 }}
              </div>
              <div v-if="r.batch_levels?.length" class="table-scroll">
                <table class="strategy-levels-table">
                  <thead>
                    <tr>
                      <th>笔数</th>
                      <th>计算</th>
                      <th>间距</th>
                      <th class="right">倍数</th>
                      <th class="right">额外手数</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(lv, li) in r.batch_levels" :key="li">
                      <td>{{ lv.pos_from }}–{{ lv.pos_to }}</td>
                      <td>{{ calcTypeLabel(lv.calc_type) }}</td>
                      <td class="muted" style="font-size: 12px">{{ levelSpacingText(lv) }}</td>
                      <td class="right">{{ lv.lot_times }}</td>
                      <td class="right">{{ lv.extra_lot }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="muted" style="font-size: 12px">暂无分批档位</div>
            </template>
            <div v-else class="muted" style="font-size: 12px">分批加仓：未启用</div>
          </div>
        </div>
        <div class="list-card-actions" @click.stop>
          <button class="btn-sm btn-ghost" @click="openEdit(s)">编辑</button>
          <button class="btn-sm" :class="s.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(s)">
            {{ s.enabled ? '禁用' : '启用' }}
          </button>
          <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
        </div>
      </div>
      <div v-if="!hub.strategies.length && !loading" class="card card-pad muted">
        {{ appliedQuery ? '无匹配策略' : '暂无策略' }}
      </div>
    </div>

    <!-- 桌面端表格 -->
    <div class="card table-scroll desktop-only">
      <table>
        <thead>
          <tr>
            <th style="width: 28px"></th>
            <th>名称</th>
            <th>模版</th>
            <th>绑定品种</th>
            <th>规则</th>
            <th>创建时间</th>
            <th>启用</th>
            <th class="right">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="s in hub.strategies" :key="s.strategy_id">
            <tr class="clickable" @click="toggleExpand(s.strategy_id)">
              <td class="muted">{{ isExpanded(s.strategy_id) ? '▾' : '▸' }}</td>
              <td>
                {{ s.name }}
                <div class="muted" style="font-size: 11px">{{ s.strategy_id }}</div>
              </td>
              <td><span class="tag blue">{{ s.template_name }}</span></td>
              <td><code>{{ s.symbol }}</code></td>
              <td class="muted" style="font-size: 12px">{{ ruleSummary(s.rules) }}</td>
              <td class="muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</td>
              <td @click.stop>
                <button class="btn-sm" :class="s.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(s)">
                  {{ s.enabled ? '已启用' : '已禁用' }}
                </button>
              </td>
              <td class="right" @click.stop>
                <div class="row" style="gap: 6px; justify-content: flex-end">
                  <button class="btn-sm btn-ghost" @click="openEdit(s)">编辑</button>
                  <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
                </div>
              </td>
            </tr>
            <tr v-if="isExpanded(s.strategy_id)" class="detail-row">
              <td></td>
              <td colspan="7">
                <div class="strategy-detail">
                  <div v-if="s.remark" class="muted" style="font-size: 12px; margin-bottom: 10px">
                    备注：{{ s.remark }}
                  </div>
                  <div v-if="!s.rules.length" class="muted" style="font-size: 12px">暂无规则</div>
                  <div v-for="(r, ri) in s.rules" :key="ri" class="strategy-rule-block">
                    <div class="row between" style="margin-bottom: 8px">
                      <strong style="font-size: 13px">{{ RULE_TYPE_LABEL[r.type] || `规则 ${ri + 1}` }}</strong>
                      <span class="tag" :class="r.status === 1 ? 'green' : ''">
                        {{ r.status === 1 ? '启用' : '关闭' }}
                      </span>
                    </div>
                    <div class="kv-grid" style="margin-bottom: 8px">
                      <div v-for="kv in ruleDetailRows(r)" :key="kv.k" class="kv">
                        <span class="k">{{ kv.k }}</span><span class="v">{{ kv.v }}</span>
                      </div>
                    </div>
                    <template v-if="isRiskSized(r)">
                      <div class="muted" style="font-size: 12px">
                        手数由风险金额与信号止损价反推，各批共用同一止损；信号必须携带 sl
                      </div>
                    </template>
                    <template v-else-if="isGrid(r)">
                      <div class="muted" style="font-size: 12px">
                        网格空仓是正常运行态；只由止损 / 止盈 / 终止信号收口
                      </div>
                    </template>
                    <template v-else-if="r.batch_enabled">
                      <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                        分批加仓 · 方向 {{ actionLabel(r.batch_action) }} ·
                        {{ r.batch_count ?? 0 }} 批 · 总手数上限 {{ r.total_lot_limit ?? 0 }}
                      </div>
                      <div v-if="r.batch_levels?.length" class="table-scroll">
                        <table class="strategy-levels-table">
                          <thead>
                            <tr>
                              <th>笔数区间</th>
                              <th>计算方式</th>
                              <th>间距</th>
                              <th class="right">倍数</th>
                              <th class="right">额外手数</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr v-for="(lv, li) in r.batch_levels" :key="li">
                              <td>{{ lv.pos_from }}–{{ lv.pos_to }}</td>
                              <td>{{ calcTypeLabel(lv.calc_type) }}</td>
                              <td class="muted" style="font-size: 12px">{{ levelSpacingText(lv) }}</td>
                              <td class="right">{{ lv.lot_times }}</td>
                              <td class="right">{{ lv.extra_lot }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div v-else class="muted" style="font-size: 12px">暂无分批档位</div>
                    </template>
                    <div v-else class="muted" style="font-size: 12px">分批加仓：未启用</div>
                  </div>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!hub.strategies.length && !loading">
            <td colspan="8" class="muted" style="padding: 18px">
              {{ appliedQuery ? '无匹配策略' : '暂无策略，点击右上角「新增策略」开始配置' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 / 编辑策略弹窗 -->
    <div v-if="showForm" class="modal-mask">
      <div class="card modal modal-lg strategy-form-modal">
        <div class="modal-header card-pad" style="padding-bottom: 0">
          <div class="h1">{{ isEditMode ? '编辑策略' : '新增策略' }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            {{
              isEditMode
                ? '可修改名称、绑定品种与规则参数；模版创建后不可更换'
                : '选择策略模版 → 填写名称 / 品种 → 自定义规则参数后创建'
            }}
          </p>
        </div>

        <div class="modal-body card-pad" style="padding-top: 16px">
          <div class="form-grid">
            <div class="field">
              <FormLabel field-id="strategy-template" text="策略模版" :help="FIELD_HELP.template" />
              <select
                id="strategy-template"
                v-model="form.template_id"
                :disabled="isEditMode"
              >
                <option disabled value="">请选择模版</option>
                <option v-for="t in templates" :key="t.template_id" :value="t.template_id">
                  {{ t.name }}
                </option>
                <option
                  v-if="isEditMode && form.template_id && !selectedTemplate"
                  :value="form.template_id"
                >
                  {{ formTemplateLabel }}
                </option>
              </select>
              <p v-if="selectedTemplate" class="muted" style="font-size: 12px; margin-top: 6px">
                {{ selectedTemplate.description }}
              </p>
              <p v-else-if="isEditMode" class="muted" style="font-size: 12px; margin-top: 6px">
                当前模版：{{ formTemplateLabel }}
              </p>
            </div>

            <div class="field">
              <FormLabel field-id="strategy-name" text="策略名称" :help="FIELD_HELP.name" />
              <input id="strategy-name" v-model="form.name" type="text" maxlength="64" placeholder="例如：黄金逆势策略" />
            </div>

            <div class="field">
              <FormLabel field-id="strategy-symbol" text="绑定品种" :help="FIELD_HELP.symbol" />
              <input
                id="strategy-symbol"
                v-model="form.symbol"
                type="text"
                maxlength="32"
                placeholder="例如：XAUUSD"
                style="text-transform: uppercase"
              />
            </div>
          </div>

          <div v-if="form.rules.length" class="rules-editor">
            <div class="rules-editor-head">
              <FormLabel text="规则参数" :help="FIELD_HELP.rules" />
              <span class="muted" style="font-size: 12px">
                {{ isEditMode ? '可按需调整规则；「恢复默认」将回退到模版默认值' : '切换模版会重新载入默认值' }}
              </span>
            </div>

            <div v-for="(r, idx) in form.rules" :key="`${r.type}-${idx}`" class="rule-panel">
              <div class="rule-panel-head">
                <FormLabel
                  class="rule-title-label"
                  :text="RULE_TYPE_LABEL[r.type] || `规则 ${idx + 1}`"
                  :help="RULE_TYPE_HELP[r.type] || FIELD_HELP.rules"
                />
                <div class="rule-panel-actions">
                  <div class="rule-enable-wrap">
                    <FormLabel text="启用" :help="FIELD_HELP.status" />
                    <input
                      type="checkbox"
                      :checked="r.status === 1"
                      aria-label="启用本条规则"
                      @change="r.status = ($event.target as HTMLInputElement).checked ? 1 : 0"
                    />
                  </div>
                  <button type="button" class="btn-sm btn-ghost" @click="resetRuleToTemplate(idx)">恢复默认</button>
                </div>
              </div>

              <!-- 以损定量趋势单（模版2）：手数由风险金额反推，没有加仓倍数与档位 -->
              <template v-if="isRiskSized(r)">
                <div class="rule-grid">
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-action`" text="监控方向" :help="FIELD_HELP.action" />
                    <select :id="`rule-${idx}-action`" v-model="r.action">
                      <option value="all">全部</option>
                      <option value="buy">多单</option>
                      <option value="sell">空单</option>
                    </select>
                  </div>
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-risk-amount`"
                      text="风险金额"
                      :help="FIELD_HELP.risk_amount"
                    />
                    <input
                      :id="`rule-${idx}-risk-amount`"
                      v-model.number="r.risk_amount"
                      type="number"
                      min="0"
                      step="10"
                    />
                  </div>
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-rr`" text="盈亏比" :help="FIELD_HELP.rr_ratio" />
                    <input
                      :id="`rule-${idx}-rr`"
                      v-model.number="r.rr_ratio"
                      type="number"
                      min="0"
                      step="0.1"
                    />
                  </div>
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-base-ratio`"
                      text="底仓 %"
                      :help="FIELD_HELP.base_ratio"
                    />
                    <input
                      :id="`rule-${idx}-base-ratio`"
                      v-model.number="r.base_ratio"
                      type="number"
                      min="1"
                      max="100"
                      step="5"
                      :disabled="!r.add_batches"
                    />
                  </div>
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-max-total-lot`"
                      text="总手数上限"
                      :help="FIELD_HELP.max_total_lot"
                    />
                    <input
                      :id="`rule-${idx}-max-total-lot`"
                      v-model.number="r.max_total_lot"
                      type="number"
                      min="0"
                      step="0.01"
                    />
                  </div>
                </div>
                <p class="rule-hint">
                  总手数 = 风险金额 {{ r.risk_amount }} ÷ 每手止损亏损（由信号止损价与品种规格算出）；
                  止盈距离 = 止损距离 × {{ r.rr_ratio }}；底仓
                  {{ r.add_batches ? r.base_ratio : 100 }}% 市价成交
                </p>

                <div class="batch-block">
                  <div class="batch-head">
                    <FormLabel text="分批补仓" :help="FIELD_HELP.add_batches" />
                    <span v-if="r.add_batches" class="muted batch-count-hint">
                      剩余 {{ 100 - r.base_ratio }}% 分 {{ r.add_batches }} 批
                    </span>
                    <span v-else class="muted batch-count-hint">底仓即全仓</span>
                  </div>
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-add-batches`"
                        text="补仓批数"
                        :help="FIELD_HELP.add_batches"
                      />
                      <input
                        :id="`rule-${idx}-add-batches`"
                        v-model.number="r.add_batches"
                        type="number"
                        min="0"
                        step="1"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-entry-direction`"
                        text="补仓方向"
                        :help="FIELD_HELP.entry_direction"
                      />
                      <select
                        :id="`rule-${idx}-entry-direction`"
                        v-model="r.entry_direction"
                        :disabled="!r.add_batches"
                      >
                        <option v-for="o in ENTRY_DIRECTION_OPTIONS" :key="o.value" :value="o.value">
                          {{ o.label }}
                        </option>
                      </select>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-gap-points`"
                        text="批次间距"
                        :help="FIELD_HELP.batch_gap_points"
                      />
                      <input
                        :id="`rule-${idx}-gap-points`"
                        v-model.number="r.batch_gap_points"
                        type="number"
                        min="0"
                        step="10"
                        :disabled="!r.add_batches"
                      />
                    </div>
                  </div>
                  <p class="rule-hint">
                    各批共用信号那一个止损价，所以补进几批都不会改变总风险；
                    触发价超出止损（回撤）或止盈（突破）区间时，节点会自动压缩间距
                  </p>
                </div>

                <div class="batch-block">
                  <div class="batch-head">
                    <div class="rule-enable-wrap">
                      <FormLabel text="保本触发" :help="FIELD_HELP.breakeven_enabled" />
                      <input
                        type="checkbox"
                        :checked="r.breakeven_enabled"
                        aria-label="启用保本触发"
                        @change="r.breakeven_enabled = ($event.target as HTMLInputElement).checked"
                      />
                    </div>
                  </div>
                  <div v-if="r.breakeven_enabled" class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-breakeven-times`"
                        text="止损距倍数"
                        :help="FIELD_HELP.breakeven_times"
                      />
                      <input
                        :id="`rule-${idx}-breakeven-times`"
                        v-model.number="r.breakeven_times"
                        type="number"
                        min="0"
                        step="0.5"
                      />
                    </div>
                  </div>
                  <p v-if="r.breakeven_enabled" class="rule-hint">
                    浮盈达到止损距离 × {{ r.breakeven_times }} 倍时，把该任务全部持仓的止损移到加权均价，只触发一次
                  </p>
                </div>
              </template>

              <!-- 网格交易（模版3）：复刻币安现货手动网格 -->
              <template v-else-if="isGrid(r)">
                <div class="rule-grid">
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-price-lower`" text="区间下限" :help="FIELD_HELP.price_lower" />
                    <input
                      :id="`rule-${idx}-price-lower`"
                      v-model.number="r.price_lower"
                      type="number"
                      min="0"
                      step="any"
                    />
                  </div>
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-price-upper`" text="区间上限" :help="FIELD_HELP.price_upper" />
                    <input
                      :id="`rule-${idx}-price-upper`"
                      v-model.number="r.price_upper"
                      type="number"
                      min="0"
                      step="any"
                    />
                  </div>
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-grid-count`" text="网格数量" :help="FIELD_HELP.grid_count" />
                    <input
                      :id="`rule-${idx}-grid-count`"
                      v-model.number="r.grid_count"
                      type="number"
                      min="2"
                      max="200"
                      step="1"
                    />
                  </div>
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-grid-mode`" text="网格模式" :help="FIELD_HELP.grid_mode" />
                    <select :id="`rule-${idx}-grid-mode`" v-model="r.grid_mode">
                      <option v-for="o in GRID_MODE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                    </select>
                  </div>
                  <div class="field">
                    <FormLabel :field-id="`rule-${idx}-grid-side`" text="网格方向" :help="FIELD_HELP.grid_side" />
                    <select :id="`rule-${idx}-grid-side`" v-model="r.grid_side">
                      <option v-for="o in GRID_SIDE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                    </select>
                  </div>
                </div>
                <p class="rule-hint">{{ gridHint(r) }}</p>

                <div class="batch-block">
                  <div class="batch-head">
                    <FormLabel text="每格下单" :help="FIELD_HELP.lot_per_grid" />
                  </div>
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel :field-id="`rule-${idx}-lot-per-grid`" text="每格手数" :help="FIELD_HELP.lot_per_grid" />
                      <input
                        :id="`rule-${idx}-lot-per-grid`"
                        v-model.number="r.lot_per_grid"
                        type="number"
                        min="0"
                        step="0.01"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-grid-total-lot`"
                        text="总手数上限"
                        :help="FIELD_HELP.grid_total_lot_limit"
                      />
                      <input
                        :id="`rule-${idx}-grid-total-lot`"
                        v-model.number="r.total_lot_limit"
                        type="number"
                        min="0"
                        step="0.01"
                      />
                    </div>
                    <div class="field">
                      <div class="rule-enable-wrap">
                        <FormLabel text="初始建仓" :help="FIELD_HELP.prefill_enabled" />
                        <input
                          type="checkbox"
                          :checked="r.prefill_enabled"
                          aria-label="启用初始建仓"
                          @change="r.prefill_enabled = ($event.target as HTMLInputElement).checked"
                        />
                      </div>
                    </div>
                  </div>
                </div>

                <div class="batch-block">
                  <div class="batch-head">
                    <FormLabel text="启停条件" :help="FIELD_HELP.trigger_price" />
                  </div>
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel :field-id="`rule-${idx}-trigger`" text="触发价" :help="FIELD_HELP.trigger_price" />
                      <input
                        :id="`rule-${idx}-trigger`"
                        v-model.number="r.trigger_price"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel :field-id="`rule-${idx}-stop-lower`" text="止损价" :help="FIELD_HELP.stop_lower" />
                      <input
                        :id="`rule-${idx}-stop-lower`"
                        v-model.number="r.stop_lower"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel :field-id="`rule-${idx}-stop-upper`" text="止盈价" :help="FIELD_HELP.stop_upper" />
                      <input
                        :id="`rule-${idx}-stop-upper`"
                        v-model.number="r.stop_upper"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <div class="rule-enable-wrap">
                        <FormLabel text="终止时清仓" :help="FIELD_HELP.close_on_stop" />
                        <input
                          type="checkbox"
                          :checked="r.close_on_stop"
                          aria-label="终止时清仓"
                          @change="r.close_on_stop = ($event.target as HTMLInputElement).checked"
                        />
                      </div>
                    </div>
                  </div>
                  <p class="rule-hint">
                    空仓是正常运行态；任务只由止损价 / 止盈价 / 终止信号收口
                  </p>
                </div>

                <div class="batch-block">
                  <div class="batch-head">
                    <FormLabel text="向上追踪" :help="FIELD_HELP.trailing_up" />
                    <input
                      type="checkbox"
                      :checked="r.trailing_up"
                      aria-label="启用向上追踪"
                      @change="r.trailing_up = ($event.target as HTMLInputElement).checked"
                    />
                  </div>
                  <div v-if="r.trailing_up" class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-trailing-max`"
                        text="最大平移格数"
                        :help="FIELD_HELP.trailing_max"
                      />
                      <input
                        :id="`rule-${idx}-trailing-max`"
                        v-model.number="r.trailing_max"
                        type="number"
                        min="0"
                        step="1"
                      />
                    </div>
                  </div>
                  <p v-if="r.trailing_up" class="rule-hint">
                    {{ trailingHint(r) }}
                  </p>
                </div>
              </template>

              <template v-else>
              <div class="rule-grid">
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-action`" text="监控方向" :help="FIELD_HELP.action" />
                  <select :id="`rule-${idx}-action`" v-model="r.action">
                    <option value="all">全部</option>
                    <option value="buy">多单</option>
                    <option value="sell">空单</option>
                  </select>
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-point`" text="点数" :help="FIELD_HELP.point" />
                  <input :id="`rule-${idx}-point`" v-model.number="r.point" type="number" min="0" step="1" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-lot-times`" text="倍数" :help="FIELD_HELP.lot_times" />
                  <input :id="`rule-${idx}-lot-times`" v-model.number="r.lot_times" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-extra-lot`" text="手数" :help="FIELD_HELP.extra_lot" />
                  <input :id="`rule-${idx}-extra-lot`" v-model.number="r.extra_lot" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-max-allow`" text="次数" :help="FIELD_HELP.max_allow_num" />
                  <input :id="`rule-${idx}-max-allow`" v-model.number="r.max_allow_num" type="number" min="0" step="1" />
                </div>
              </div>
              <p class="rule-hint">
                手数 = {{ r.lot_times }} × 基础手数 + {{ r.extra_lot }}；触发 = {{ r.point }} × Point()
              </p>

              <div class="batch-block">
                <div class="batch-head">
                  <div class="rule-enable-wrap">
                    <FormLabel text="分批加仓" :help="FIELD_HELP.batch_enabled" />
                    <input
                      type="checkbox"
                      :checked="r.batch_enabled"
                      :aria-label="`启用${RULE_TYPE_LABEL[r.type] || '本条规则'}分批加仓`"
                      @change="setBatchEnabled(r, ($event.target as HTMLInputElement).checked)"
                    />
                  </div>
                  <span v-if="r.batch_enabled" class="muted batch-count-hint">
                    共 {{ r.batch_count }} 批
                  </span>
                </div>

                <template v-if="r.batch_enabled">
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-action`"
                        text="分批方向"
                        :help="FIELD_HELP.batch_action"
                      />
                      <select :id="`rule-${idx}-batch-action`" v-model="r.batch_action">
                        <option value="all">全部</option>
                        <option value="buy">多单</option>
                        <option value="sell">空单</option>
                      </select>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-count`"
                        text="批数"
                        :help="FIELD_HELP.batch_count"
                      />
                      <input
                        :id="`rule-${idx}-batch-count`"
                        v-model.number="r.batch_count"
                        type="number"
                        min="1"
                        step="1"
                        @change="onBatchMetaChange(r)"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-total-lot`"
                        text="总手数"
                        :help="FIELD_HELP.total_lot_limit"
                      />
                      <input
                        :id="`rule-${idx}-total-lot`"
                        v-model.number="r.total_lot_limit"
                        type="number"
                        :min="BATCH_POS_START"
                        step="1"
                        @change="onBatchMetaChange(r)"
                      />
                    </div>
                  </div>

                  <div class="batch-levels">
                    <div v-for="(lv, li) in r.batch_levels" :key="li" class="batch-level">
                      <span class="batch-level-no">└{{ li + 1 }}</span>
                      <div class="field batch-range">
                        <FormLabel text="持仓笔数" :help="FIELD_HELP.batch_level" />
                        <div class="batch-range-value" title="由批数与总手数自动生成">
                          {{ lv.pos_from }} ~ {{ lv.pos_to }}
                        </div>
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-calc`"
                          text="计算方式"
                          :help="FIELD_HELP.calc_type"
                        />
                        <select :id="`rule-${idx}-lv-${li}-calc`" v-model="lv.calc_type">
                          <option v-for="o in CALC_TYPE_OPTIONS" :key="o.value" :value="o.value">
                            {{ o.label }}
                          </option>
                        </select>
                      </div>
                      <div v-if="lv.calc_type === 'price'" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-price`"
                          text="指定价"
                          :help="FIELD_HELP.batch_price"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-price`"
                          v-model.number="lv.price"
                          type="number"
                          min="0"
                          step="0.00001"
                        />
                      </div>
                      <div v-else-if="isBarCalc(lv.calc_type)" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-tf`"
                          text="K 线周期"
                          :help="FIELD_HELP.batch_timeframe"
                        />
                        <select :id="`rule-${idx}-lv-${li}-tf`" v-model="lv.timeframe">
                          <option v-for="tf in TIMEFRAME_OPTIONS" :key="tf" :value="tf">
                            {{ tf }}
                          </option>
                        </select>
                      </div>
                      <div v-else class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-point`"
                          text="点数"
                          :help="FIELD_HELP.point"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-point`"
                          v-model.number="lv.point"
                          type="number"
                          min="0"
                          step="1"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-times`"
                          text="倍数"
                          :help="FIELD_HELP.lot_times"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-times`"
                          v-model.number="lv.lot_times"
                          type="number"
                          min="0"
                          step="0.01"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-extra`"
                          text="手数"
                          :help="FIELD_HELP.extra_lot"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-extra`"
                          v-model.number="lv.extra_lot"
                          type="number"
                          min="0"
                          step="0.01"
                        />
                      </div>
                    </div>
                  </div>
                  <p class="rule-hint">
                    档位区间由批数 × 总手数自动切分（第 {{ BATCH_POS_START }} 笔 ~ 第 {{ Math.floor(r.total_lot_limit) }} 笔），不可手动添加；
                    每档的加仓间距可独立选择点数 / 指定价 / ATR / 波幅，ATR 与波幅取该周期最近 {{ BATCH_BAR_PERIOD }} 根已收盘 K 线
                  </p>
                </template>
              </div>
              </template>
            </div>
          </div>

          <p v-if="formError" class="form-error" style="margin-top: 10px">{{ formError }}</p>
        </div>

        <div class="modal-footer card-pad" style="padding-top: 0">
          <span></span>
          <div class="row" style="gap: 8px">
            <button class="btn-ghost" :disabled="saving" @click="showForm = false">取消</button>
            <button class="btn-primary" :disabled="saving" @click="save">
              {{ saving ? '保存中…' : isEditMode ? '保存' : '创建' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  margin-bottom: 14px;
  align-items: flex-start;
}

.rules-editor {
  margin-top: 18px;
}

.rules-editor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.rule-panel {
  margin-bottom: 12px;
  padding: 14px 16px;
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  border: 1px solid var(--glass-border);
}

.rule-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.rule-title {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.rule-title-label :deep(.form-label-row) {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.rule-panel-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.rule-enable-wrap {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.rule-enable-wrap :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.rule-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
}

.rule-grid .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.rule-grid :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.rule-grid :deep(.form-label-row) {
  white-space: nowrap;
}

.rule-grid :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.rule-grid input,
.rule-grid select {
  width: 100%;
  min-width: 0;
}

.rule-hint {
  margin: 10px 0 0;
  font-size: 11px;
  color: var(--muted);
}

.batch-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--glass-border);
}

.batch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.batch-count-hint {
  font-size: 12px;
}

.batch-top-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
  margin-top: 12px;
  max-width: 640px;
}

.batch-levels {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.batch-level {
  display: grid;
  grid-template-columns: 28px minmax(88px, 0.9fr) repeat(4, minmax(0, 1fr));
  gap: 10px;
  align-items: end;
}

.batch-level-no {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  padding-bottom: 8px;
}

.batch-range-value {
  display: flex;
  align-items: center;
  min-height: 34px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--glass-border);
  background: color-mix(in srgb, var(--bg-soft) 70%, transparent);
  font-family: var(--mono);
  font-size: 13px;
  color: var(--muted);
  user-select: none;
}

.batch-top-grid .field,
.batch-level .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.batch-top-grid :deep(.form-label-wrap),
.batch-level :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.batch-top-grid :deep(.form-label-row),
.batch-level :deep(.form-label-row) {
  white-space: nowrap;
}

.batch-top-grid :deep(.field-help-popover),
.batch-level :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.batch-top-grid input,
.batch-top-grid select,
.batch-level input,
.batch-level select {
  width: 100%;
  min-width: 0;
}

@media (max-width: 900px) {
  .rule-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .batch-top-grid {
    grid-template-columns: 1fr 1fr;
    max-width: none;
  }
  .batch-level {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .batch-level-no {
    grid-column: 1 / -1;
    padding-bottom: 0;
  }
}

@media (max-width: 768px) {
  .strategy-form-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .rule-grid {
    grid-template-columns: 1fr 1fr;
  }
  .rules-editor-head {
    flex-wrap: wrap;
  }
  .batch-top-grid {
    grid-template-columns: 1fr;
    max-width: none;
  }
  .batch-level {
    grid-template-columns: 1fr 1fr;
  }
}

.strategy-detail {
  margin: 6px 0 4px;
}

.strategy-rule-block {
  margin-bottom: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  border: 1px solid var(--glass-border);
}

.strategy-rule-block:last-child {
  margin-bottom: 0;
}

.strategy-levels-table {
  width: 100%;
  margin-top: 4px;
}

.strategy-levels-table th,
.strategy-levels-table td {
  padding: 6px 8px;
  font-size: 12px;
}

.list-card .strategy-detail {
  margin: 10px 0;
  padding-top: 8px;
  border-top: 1px dashed var(--glass-border);
}
</style>
