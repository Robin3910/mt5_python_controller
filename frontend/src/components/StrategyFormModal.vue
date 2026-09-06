<script setup lang="ts">
// 策略新建 / 编辑弹窗：表单逻辑、校验与提交
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  BatchCalcType,
  BatchTimeframe,
  EntryMode,
  GridMode,
  GridSide,
  GridSizingData,
  NodeOut,
  StrategyBatchLevel,
  StrategyOut,
  StrategyRule,
  StrategyTemplateOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    mode: 'create' | 'edit'
    strategyId?: string
    listSearchOptions?: { q?: string }
  }>(),
  {
    strategyId: '',
    listSearchOptions: () => ({}),
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: []
}>()

/** ATR / 波幅统计的已收盘 K 线根数，与后端 BATCH_BAR_PERIOD 一致，固定不可配 */
const BATCH_BAR_PERIOD = 14

/** 规则 type，与后端 strategy_templates 对齐 */
const RULE_TYPE_COUNTER = 1
const RULE_TYPE_TREND = 2
const RULE_TYPE_RISK_SIZED = 3
const RULE_TYPE_GRID = 4

const hub = useHubStore()
const templates = ref<StrategyTemplateOut[]>([])
const editingTemplateName = ref('')
const saving = ref(false)
const formError = ref('')
const form = reactive({
  template_id: '',
  name: '',
  symbol: '',
  rules: [] as EditableRule[],
})

const isEditMode = computed(() => props.mode === 'edit')

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.template_id === form.template_id) || null,
)

const formTemplateLabel = computed(() => {
  if (selectedTemplate.value) return selectedTemplate.value.name
  return editingTemplateName.value || form.template_id || '—'
})

const RULE_TYPE_LABEL: Record<number, string> = {
  [RULE_TYPE_COUNTER]: '逆势加仓',
  [RULE_TYPE_TREND]: '顺势加仓',
  [RULE_TYPE_RISK_SIZED]: '以损定量趋势单',
  [RULE_TYPE_GRID]: '网格交易',
}

/** 仅影响表单展示顺序：顺势加仓在前，逆势加仓在后；提交仍用 form.rules 原序 */
const RULE_DISPLAY_ORDER: Record<number, number> = {
  [RULE_TYPE_TREND]: 0,
  [RULE_TYPE_COUNTER]: 1,
  [RULE_TYPE_RISK_SIZED]: 2,
  [RULE_TYPE_GRID]: 3,
}

function ruleTypeOf(rule: { type: number }): number {
  return Number(rule.type)
}

const displayedRules = computed(() =>
  form.rules
    .map((rule, index) => ({ rule, index }))
    .sort((a, b) => {
      const oa = RULE_DISPLAY_ORDER[ruleTypeOf(a.rule)] ?? ruleTypeOf(a.rule)
      const ob = RULE_DISPLAY_ORDER[ruleTypeOf(b.rule)] ?? ruleTypeOf(b.rule)
      return oa - ob
    }),
)

/** 逆势加仓默认折叠；顺势加仓始终展开。点标题可展开/收起逆势。 */
const counterExpanded = ref(false)

function isRuleFoldable(type: number): boolean {
  return Number(type) === RULE_TYPE_COUNTER
}

function isRuleCollapsed(type: number): boolean {
  return isRuleFoldable(type) && !counterExpanded.value
}

function toggleRuleCollapsed(type: number): void {
  if (!isRuleFoldable(type)) return
  counterExpanded.value = !counterExpanded.value
}

function resetRuleCollapse(): void {
  counterExpanded.value = false
}

const RULE_TYPE_HELP: Record<number, string> = {
  [RULE_TYPE_COUNTER]:
    '逆势加仓：以首仓开仓价为锚（若已有更深的逆势仓则取最深一笔），价格朝不利方向偏离达到「点数 × Point()」后触发加仓。' +
    '中间的顺势加仓不会抬高/压低逆势锚点；须先回到首仓不利侧达到阈值，才会开始逆势加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
  [RULE_TYPE_TREND]:
    '顺势加仓：以监控方向最近一笔订单为基准，价格朝有利方向偏离达到「点数 × Point()」后触发加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
  [RULE_TYPE_RISK_SIZED]:
    '以损定量趋势单：不使用信号手数，而是按「风险金额 ÷ 止损距离」反推总手数，' +
    '底仓先市价成交（止盈为 0），剩余仓位拆成多笔分散仓市价单并按盈亏比挂止盈。' +
    '所有订单共用信号那一个止损价，因此打到止损的总亏损始终等于风险金额。' +
    '信号必须携带止损价（sl），否则本策略不参与分发。',
  [RULE_TYPE_GRID]:
    '网格交易：在价格区间内按等差或等比切格，' +
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
    '顺势加仓取该方向最近一笔订单为基准；逆势加仓取首仓（或更深的逆势仓）为锚。',
  point:
    '加仓触发点数。顺势以最近一笔、逆势以首仓（或更深逆势仓）为基准，' +
    '价格偏离达到「点数 × Point()」后开始执行加仓。Point() 为品种最小价格变动单位。',
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
  entry_mode:
    '开仓方式：\n' +
    '市价 = 信号一到，底仓与分散仓全部市价打齐，各单开仓价相同；\n' +
    '限价 = 底仓与分散仓全部挂在信号给的入场价（同一点位），' +
    '挂上后一直等到成交（GTC）。\n' +
    '限价模式要求信号携带入场价（limit_price / price 字段），否则该策略不参与分发。\n' +
    '止损共用信号 sl，分散仓仍按入场价铺阶梯止盈；手数按入场价到止损价的距离反推，与市价同一口径。',
  risk_amount:
    '本次交易愿意承担的亏损金额（账户货币，通常是美元）。\n' +
    '总手数 = 风险金额 ÷ 每手止损亏损，其中每手止损亏损由止损距离与品种合约规格算出。\n' +
    '市价模式下止损距离按止损触发侧的报价算（多单看买价、空单看卖价）；\n' +
    '限价模式下按挂单价到止损价的距离（各档相同）。\n' +
    '手数按品种步长向下取整，因此实际风险只会小于该值，不会超出。',
  rr_ratio:
    '盈亏比。止盈距离 = 止损距离 × 该值，止盈只挂在分散仓上（底仓止盈为 0）。\n' +
    '分散仓按等分阶梯逐档兑现：第 i 单止盈 = 开仓价 + 止盈距离 × i ÷ 分散仓单数，\n' +
    '只有最远一档吃满该盈亏比，前面各档按比例提前落袋。\n' +
    '填 0 表示分散仓也不设止盈，仅靠止损与人工干预出场。',
  base_ratio:
    '底仓占总手数的百分比，止盈为 0。\n' +
    '市价模式立即成交；限价模式挂在信号给的入场价。\n' +
    '剩余仓位交给下方的分散仓单数拆开；分散仓单数为 0 时底仓即全仓。',
  add_batches:
    '剩余仓位拆成几笔分散仓。0 表示不拆分，总手数一次性由底仓成交。\n' +
    '市价模式与底仓一并市价打出；限价模式与底仓挂在同一个入场价' +
    '（订单数 = 1 + 分散仓单数）。\n' +
    '分散仓严格等手数，除不尽的余量不下单，因此实下总手数可能略少于反推值。\n' +
    '若剩余手数不足以让每笔都达到品种最小手数，节点会自动减少单数。',
  max_total_lot:
    '总手数硬上限，0 表示不额外限制（仍受品种最大手数约束）。\n' +
    '被上限截断时实际风险会小于风险金额。',
  breakeven_enabled:
    '开启后，浮盈达到「止损距离 × 倍数」时把该任务全部持仓的止损移到持仓加权均价，' +
    '此后这笔交易最差打平。',
  breakeven_times:
    '保本触发倍数 N：浮盈价格距离 ≥ 止损距离 × N 时移动止损。\n' +
    '例如止损距离 300 点、N=2，则浮盈 600 点后止损挪到均价。',
  breakeven_mode:
    '保本监控方式：\n' +
    '按次 = 达标移动一次后不再监控；\n' +
    '循环 = 持续监控，若止损被拉回或均价变化导致尚未到位，可再次移动到新的加权均价。',
  price_lower: '网格价格区间下限。须大于 0，且小于上限。',
  price_upper: '网格价格区间上限。须大于下限。',
  grid_count: '网格数量（2–200）。区间会被切成该数量的格子（N+1 条网格线）。',
  grid_mode:
    '分格方式：\n等差 = 每格价格间距相等；\n等比 = 每格涨跌幅比例相等（适合宽区间）。',
  grid_side:
    '网格方向：\n只做多 = 跌买涨卖；\n只做空 = 涨卖跌买。',
  lot_per_grid: '每一格买入/卖出的手数。',
  trigger_price: '触发价。填 0 表示信号到达后立即启动；否则等现价触及（穿越或落到）该价才建网格，与多空方向无关。',
  stop_lower:
    '下沿终止价，须低于区间下限；填 0 表示不设。' +
    '多头时为止损；空头时为止盈。',
  stop_upper:
    '上沿终止价，须高于区间上限；填 0 表示不设。' +
    '多头时为止盈；空头时为止损。',
  stop_loss_long: '止损价，须低于区间下限；填 0 表示不设。多头网格跌破此价终止。',
  stop_profit_long: '止盈价，须高于区间上限；填 0 表示不设。多头网格涨破此价终止。',
  stop_loss_short: '止损价，须高于区间上限；填 0 表示不设。空头网格涨破此价终止。',
  stop_profit_short: '止盈价，须低于区间下限；填 0 表示不设。空头网格跌破此价终止。',
  close_on_stop:
    '触发止损/止盈或收到终止指令时是否清掉该任务全部持仓。\n' +
    '关闭后：停止网格交易与监控推进，但保留已有持仓；任务进入「已脱离」非终态，' +
    '占位不释放，直到这批仓位被外部平光才真正收口。',
  prefill_enabled:
    '开启后启动时按「仍有盈利空间的格位 × 每格手数」市价建底仓：\n' +
    '多头买入卖出价仍高于现价的格；空头开空平仓价仍低于现价的格。\n' +
    '关闭则只挂网格、等穿越再开仓；多头上涨时可能无货可卖。',
  grid_total_lot_limit:
    '全部格位合计手数上限，0=不额外限制。初始建仓也会受此约束。\n' +
    '若填写，须不小于每格手数，否则策略无法保存 / 无法参与分发。',
  trailing_up:
    '向上追踪：价格越过区间外沿时网格不停机，' +
    '整个区间连同止损价 / 止盈价一起平移一格，继续在新区间吃差价。\n' +
    '多头网格追涨（突破上限上移），空头网格追跌（跌破下限下移）。\n' +
    '注意止盈价也会同步上移，所以开启追踪后止盈基本不会触发，两者通常只用其一。',
  assist_enabled:
    '按 ATR 与风险预算试算网格数量与每格手数。\n' +
    '只在配置时算一次，结果需点「应用建议」才写入上方字段；网格运行时不读这组参数。\n' +
    '关闭后已填的试算参数会保留，只是不再校验与试算。',
  assist_node:
    'ATR 与合约规格从哪台节点的 MT5 终端读取。节点只作行情源，不会写进策略配置；\n' +
    '换节点可能因券商后缀、历史深度与计价货币不同而算出略有差异的建议值。',
  assist_timeframe:
    `试算 ATR 用的 K 线周期，固定取最近 ${BATCH_BAR_PERIOD} 根**已收盘** K 线。\n` +
    '网格格距要反映区间级别的波动，通常比加仓档位取更大的周期。',
  assist_atr_mult:
    '格距 = ATR × 该倍数。倍数越小格子越密、单格利润越薄，满仓手数也越大。\n' +
    '改动倍数会清空下面的「格距」，让 ATR 重新决定。',
  assist_spacing:
    '实际用于推算格数的格距。取行情后会自动填入 ATR × 倍数的结果，可手改；\n' +
    '非 0 时以此处为准（不再看 ATR 倍数），改完重新试算即按改后的值算格数与手数。',
  assist_max_loss:
    '满仓被打到止损时最多可接受的亏损金额（账户货币），用于反推每格手数。\n' +
    '必须先设好本方向的止损价：多头看区间下沿、空头看区间上沿。',
  trailing_max:
    '最多允许平移多少格，0 表示不限。\n' +
    '不限时只要不触发止损，网格会一直跟着行情滚动。',
}

/** 保本监控方式，与后端 BREAKEVEN_MODES 对齐 */
const BREAKEVEN_MODE_OPTIONS: Array<{ value: 'once' | 'loop'; label: string }> = [
  { value: 'once', label: '按次' },
  { value: 'loop', label: '循环' },
]

const ENTRY_MODE_OPTIONS: Array<{ value: EntryMode; label: string }> = [
  { value: 'market', label: '市价' },
  { value: 'limit', label: '限价' },
]

const GRID_MODE_OPTIONS: Array<{ value: GridMode; label: string }> = [
  { value: 'arithmetic', label: '等差' },
  { value: 'geometric', label: '等比' },
]

const GRID_SIDE_OPTIONS: Array<{ value: GridSide; label: string }> = [
  { value: 'long', label: '只做多' },
  { value: 'short', label: '只做空' },
]

/** 网格试算默认周期，与后端 GRID_ASSIST_TIMEFRAME 一致 */
const GRID_ASSIST_TIMEFRAME: BatchTimeframe = 'H1'

/**
 * 表单内规则：所有字段都已填充，便于直接 v-model 绑定。
 * 各组字段都会补齐，提交后由后端按 type 只保留对应的一组。
 */
type EditableRule = Required<
  Omit<StrategyRule, 'batch_levels' | 'grid_mode' | 'grid_side' | 'breakeven_mode'>
> & {
  batch_levels: StrategyBatchLevel[]
  grid_mode: GridMode
  grid_side: GridSide
  breakeven_mode: 'once' | 'loop'
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
    risk_amount: r.risk_amount ?? 100,
    rr_ratio: r.rr_ratio ?? 2.5,
    base_ratio: r.base_ratio ?? 30,
    add_batches: r.add_batches ?? 10,
    max_total_lot: r.max_total_lot ?? 0,
    breakeven_enabled: r.breakeven_enabled ?? true,
    breakeven_times: r.breakeven_times ?? 2,
    breakeven_mode: r.breakeven_mode === 'loop' ? 'loop' : 'once',
    // 缺字段的历史配置一律按市价，保证旧策略行为不变
    entry_mode: r.entry_mode === 'limit' ? 'limit' : 'market',
    price_lower: r.price_lower ?? 0,
    price_upper: r.price_upper ?? 0,
    grid_count: r.grid_count ?? 10,
    grid_mode: r.grid_mode ?? 'arithmetic',
    // 旧配置若仍为 follow，编辑时回落到只做多（选项已移除）
    grid_side: r.grid_side === 'short' ? 'short' : 'long',
    lot_per_grid: r.lot_per_grid ?? 0.01,
    trigger_price: r.trigger_price ?? 0,
    stop_lower: r.stop_lower ?? 0,
    stop_upper: r.stop_upper ?? 0,
    close_on_stop: r.close_on_stop ?? true,
    prefill_enabled: r.prefill_enabled ?? true,
    trailing_up: r.trailing_up ?? false,
    trailing_max: r.trailing_max ?? 0,
    assist_enabled: r.assist_enabled ?? false,
    assist_timeframe: r.assist_timeframe ?? GRID_ASSIST_TIMEFRAME,
    assist_atr_mult: r.assist_atr_mult ?? 1,
    assist_spacing: r.assist_spacing ?? 0,
    assist_max_loss: r.assist_max_loss ?? 0,
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

function loadRulesFromTemplate(templateId: string): void {
  const tpl = templates.value.find((t) => t.template_id === templateId)
  form.rules = tpl ? cloneRules(tpl.rules) : []
  resetSizing()  // 换模版后规则整组换掉，之前的试算结果不再对应任何规则
}

function resetCreateForm(): void {
  editingTemplateName.value = ''
  formError.value = ''
  form.template_id = templates.value[0]?.template_id || ''
  form.name = ''
  form.symbol = ''
  loadRulesFromTemplate(form.template_id)
}

function populateEditForm(s: StrategyOut): void {
  editingTemplateName.value = s.template_name || ''
  formError.value = ''
  form.template_id = s.template_id
  form.name = s.name
  form.symbol = s.symbol
  form.rules = cloneRules(s.rules || [])
}

async function loadEditForm(): Promise<void> {
  const id = (props.strategyId || '').trim()
  if (!id) {
    ElMessage.warning('缺少策略 ID')
    emit('update:modelValue', false)
    return
  }
  let s = hub.strategies.find((item) => item.strategy_id === id)
  if (!s) {
    await hub.fetchStrategies(props.listSearchOptions)
    s = hub.strategies.find((item) => item.strategy_id === id)
  }
  if (!s) {
    ElMessage.error('策略不存在或已删除')
    emit('update:modelValue', false)
    return
  }
  populateEditForm(s)
}

async function onOpen(): Promise<void> {
  resetSizing()
  resetRuleCollapse()
  templates.value = await hub.fetchStrategyTemplates()
  if (props.mode === 'create') {
    resetCreateForm()
  } else {
    await loadEditForm()
  }
  // 试算的行情源节点选择器需要节点列表；策略页可能还没拉过
  if (!hub.nodes.length) {
    try {
      await hub.fetchNodes()
    } catch {
      /* 拉不到就只是选不了行情源，不影响其它表单项 */
    }
  }
}

watch(
  () => props.modelValue,
  (visible) => {
    if (visible) void onOpen()
  },
)

watch(
  () => form.template_id,
  (id) => {
    // 仅新建时切换模版会重载默认规则；编辑不允许改模版
    if (props.modelValue && !isEditMode.value && id) loadRulesFromTemplate(id)
  },
)

function close(): void {
  emit('update:modelValue', false)
}

/** 以损定量趋势单的参数校验 */
function validateRiskSized(r: EditableRule, label: string): string | null {
  if (!(r.risk_amount > 0)) return `${label}：风险金额需大于 0`
  if (r.rr_ratio < 0) return `${label}：盈亏比不能为负`
  if (!(r.base_ratio > 0) || r.base_ratio > 100) return `${label}：底仓比例需在 1 ~ 100 之间`
  if (r.add_batches < 0) return `${label}：分散仓单数不能为负`
  if (r.add_batches > 50) return `${label}：分散仓单数不能超过 50`
  if (r.max_total_lot < 0) return `${label}：总手数上限不能为负`
  if (r.breakeven_enabled && !(r.breakeven_times > 0)) {
    return `${label}：保本触发倍数需大于 0`
  }
  if (r.breakeven_enabled && !BREAKEVEN_MODE_OPTIONS.some((o) => o.value === r.breakeven_mode)) {
    return `${label}：保本监控方式非法`
  }
  if (!ENTRY_MODE_OPTIONS.some((o) => o.value === r.entry_mode)) {
    return `${label}：开仓方式非法`
  }
  return null
}

/** 网格止损/止盈表单项：字段按价格上下沿存储，文案随方向切换。 */
function gridStopFields(side: GridSide | undefined | null): {
  slKey: 'stop_lower' | 'stop_upper'
  tpKey: 'stop_lower' | 'stop_upper'
  slLabel: string
  tpLabel: string
  slHelp: string
  tpHelp: string
  lowerName: string
  upperName: string
} {
  if (side === 'short') {
    return {
      slKey: 'stop_upper',
      tpKey: 'stop_lower',
      slLabel: '止损价',
      tpLabel: '止盈价',
      slHelp: FIELD_HELP.stop_loss_short,
      tpHelp: FIELD_HELP.stop_profit_short,
      lowerName: '止盈价',
      upperName: '止损价',
    }
  }
  return {
    slKey: 'stop_lower',
    tpKey: 'stop_upper',
    slLabel: '止损价',
    tpLabel: '止盈价',
    slHelp: FIELD_HELP.stop_loss_long,
    tpHelp: FIELD_HELP.stop_profit_long,
    lowerName: '止损价',
    upperName: '止盈价',
  }
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
  if (r.total_lot_limit > 0 && r.total_lot_limit < r.lot_per_grid) {
    return `${label}：总手数上限须不小于每格手数`
  }
  if (r.trigger_price < 0) return `${label}：触发价不能为负`
  const stops = gridStopFields(r.grid_side)
  // 几何约束与方向无关：下沿价 < 区间下限，上沿价 > 区间上限
  if (r.stop_lower > 0 && r.stop_lower >= r.price_lower) {
    return `${label}：${stops.lowerName}须低于区间下限`
  }
  if (r.stop_upper > 0 && r.stop_upper <= r.price_upper) {
    return `${label}：${stops.upperName}须高于区间上限`
  }
  if (r.trailing_max < 0) return `${label}：最大平移格数不能为负`
  return validateGridAssist(r, label)
}

/**
 * 试算助手的参数校验。只在开关开启时生效，避免存量规则与不用助手的用户被拦。
 * 其中止损价是硬前提：没有止损就无从反推手数，此时给出的任何手数都是假的。
 */
function validateGridAssist(r: EditableRule, label: string): string | null {
  if (r.assist_atr_mult < 0) return `${label}：ATR 倍数不能为负`
  if (r.assist_spacing < 0) return `${label}：格距不能为负`
  if (r.assist_max_loss < 0) return `${label}：最大可接受亏损不能为负`
  if (!r.assist_enabled) return null
  if (!TIMEFRAME_OPTIONS.includes(r.assist_timeframe)) return `${label}：试算周期非法`
  if (!(r.assist_atr_mult > 0) && !(r.assist_spacing > 0)) {
    return `${label}：试算需要 ATR 倍数或手填格距`
  }
  if (r.assist_spacing > 0 && r.assist_spacing >= r.price_upper - r.price_lower) {
    return `${label}：格距需小于区间宽度，否则切不出 2 格`
  }
  if (!(r.assist_max_loss > 0)) {
    return `${label}：试算需要最大可接受亏损金额（或关闭试算开关）`
  }
  const stops = gridStopFields(r.grid_side)
  if (!(r[stops.slKey] > 0)) {
    return `${label}：试算需要先设置${stops.slLabel}（或关闭试算开关）`
  }
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

/**
 * 网格试算的会话态：按规则下标存放。
 * 建议值只是展示，点「应用建议」才写进 grid_count / lot_per_grid；行情源节点也只留
 * 在这里，不随策略落库（策略绑的是分组，不该被某台节点绑死）。
 */
interface SizingState {
  nodeId: string
  loading: boolean
  error: string
  data: GridSizingData | null
  /** 算出该结果时的参数指纹，与当前不一致即视为结果已过期 */
  signature: string
}

const sizing = reactive<Record<number, SizingState>>({})

function sizingState(idx: number): SizingState {
  if (!sizing[idx]) {
    sizing[idx] = { nodeId: '', loading: false, error: '', data: null, signature: '' }
  }
  return sizing[idx]
}

function resetSizing(): void {
  for (const key of Object.keys(sizing)) delete sizing[Number(key)]
}

/** 可作行情源的节点：只有在线节点能回 K 线与合约规格 */
const onlineNodes = computed(() =>
  hub.nodes.filter((n) => hub.statuses[n.node_id] === 'online' || n.status === 'online'),
)

function nodeLabel(n: NodeOut): string {
  const name = n.name || n.node_id
  return n.mt5_login ? `${name} · ${n.mt5_login}` : name
}

/** 试算所需的前置条件；返回原因表示还不能试算 */
function sizingBlocker(r: EditableRule): string {
  if (!(r.price_lower > 0) || !(r.price_upper > r.price_lower)) return '请先填写合法的价格区间'
  const stops = gridStopFields(r.grid_side)
  if (!(r[stops.slKey] > 0)) return `请先填写${stops.slLabel}，否则无法反推每格手数`
  if (!(r.assist_max_loss > 0)) return '请填写最大可接受亏损金额'
  return ''
}

/**
 * 会影响试算结果的字段指纹。
 * 用指纹比对而不是「改动即打标记」，是因为试算成功后要把算出的格距回填给用户微调，
 * 那次回填本身也会改动参数，用监听的写法会把刚出的结果立刻判成过期。
 */
function sizingSignature(r: EditableRule): string {
  return [
    r.price_lower,
    r.price_upper,
    r.grid_mode,
    r.grid_side,
    r.stop_lower,
    r.stop_upper,
    r.prefill_enabled,
    r.close_on_stop,
    r.trailing_up,
    r.total_lot_limit,
    r.assist_timeframe,
    r.assist_atr_mult,
    r.assist_spacing,
    r.assist_max_loss,
  ].join('|')
}

/**
 * 结果是否已过期：区间、止损、方向乃至预填开关一改，之前算出的数字就不再对应当前
 * 配置。过期只拦住「应用」，不清掉结果——让用户仍看得见基于旧参数的那组数字。
 */
function sizingStale(idx: number, r: EditableRule): boolean {
  const state = sizing[idx]
  return !!state?.data && state.signature !== sizingSignature(r)
}

async function runSizing(idx: number, r: EditableRule): Promise<void> {
  const state = sizingState(idx)
  const symbol = form.symbol.trim().toUpperCase()
  if (!symbol) {
    state.error = '请先填写策略绑定的品种'
    return
  }
  if (!state.nodeId) {
    state.error = '请选择一台在线节点作为行情源'
    return
  }
  const blocker = sizingBlocker(r)
  if (blocker) {
    state.error = blocker
    return
  }
  state.loading = true
  state.error = ''
  try {
    const data = await hub.fetchGridSizing(state.nodeId, symbol, {
      timeframe: r.assist_timeframe,
      atr_mult: r.assist_atr_mult,
      spacing: r.assist_spacing,
      max_loss: r.assist_max_loss,
      price_lower: r.price_lower,
      price_upper: r.price_upper,
      grid_mode: r.grid_mode,
      grid_side: r.grid_side,
      stop_lower: r.stop_lower,
      stop_upper: r.stop_upper,
      prefill_enabled: r.prefill_enabled,
      close_on_stop: r.close_on_stop,
      trailing_up: r.trailing_up,
      total_lot_limit: r.total_lot_limit,
    })
    state.data = data
    // 把算出的格距回填成可编辑值，用户可直接微调后重算；改 ATR 倍数会把它清回 0，
    // 否则回填值会一直盖住新的倍数
    if (data.spacing > 0) r.assist_spacing = data.spacing
    state.signature = sizingSignature(r)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    state.error = detail || '试算失败，请确认节点在线且该品种可读'
    state.data = null
  } finally {
    state.loading = false
  }
}

function applySizing(idx: number, r: EditableRule): void {
  const data = sizing[idx]?.data
  if (!data?.ready || sizingStale(idx, r)) return
  if (data.grid_count >= 2) r.grid_count = data.grid_count
  if (data.lot_per_grid > 0) r.lot_per_grid = data.lot_per_grid
  ElMessage.success('已应用建议值，可继续手动微调')
}

/** 试算结果概览：一行说清算出了什么 */
function sizingSummary(data: GridSizingData): string {
  const parts = [
    `ATR(${data.timeframe},${BATCH_BAR_PERIOD}) = ${trimNum(data.atr)}`,
    `格距 ${trimNum(data.spacing)}${data.spacing_source === 'manual' ? '（手改）' : ''}`,
    `${data.grid_count} 格`,
  ]
  if (data.lot_per_grid > 0) {
    parts.push(
      `每格 ${trimNum(data.lot_per_grid, 4)} 手`,
      `满仓 ${trimNum(data.worst_lot, 4)} 手`,
      `最坏亏损 ≈ ${trimNum(data.worst_loss, 2)}`,
    )
  }
  return parts.join(' · ')
}

function validateRules(rules: EditableRule[]): string | null {
  if (!rules.length) return '请至少配置一条规则'
  if (!rules.some((r) => r.status === 1)) return '请至少启用一条规则'
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
          props.strategyId || '',
          { name, symbol, rules },
          props.listSearchOptions,
        )
      } else {
        await hub.createStrategy(
          {
            template_id: form.template_id,
            name,
            symbol,
            rules,
          },
          props.listSearchOptions,
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
    emit('saved')
    close()
  } finally {
    saving.value = false
  }
}

function resetRuleToTemplate(idx: number): void {
  const tplRule = selectedTemplate.value?.rules?.[idx]
  if (!tplRule || !form.rules[idx]) return
  Object.assign(form.rules[idx], cloneRules([tplRule])[0])
  delete sizing[idx]
}
</script>

<template>
  <div v-if="modelValue" class="modal-mask">
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

          <div
            v-for="{ rule: r, index: idx } in displayedRules"
            :key="`${r.type}-${idx}`"
            class="rule-panel"
            :class="{ 'is-collapsed': isRuleCollapsed(r.type) }"
          >
            <div
              class="rule-panel-head"
              :class="{ 'is-foldable': isRuleFoldable(r.type) }"
              @click="toggleRuleCollapsed(r.type)"
            >
              <div class="rule-title-wrap">
                <span v-if="isRuleFoldable(r.type)" class="rule-fold-caret" aria-hidden="true">
                  {{ isRuleCollapsed(r.type) ? '▸' : '▾' }}
                </span>
                <FormLabel
                  class="rule-title-label"
                  :text="RULE_TYPE_LABEL[r.type] || `规则 ${idx + 1}`"
                  :help="RULE_TYPE_HELP[r.type] || FIELD_HELP.rules"
                />
              </div>
              <div class="rule-panel-actions" @click.stop>
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

            <div v-show="!isRuleCollapsed(r.type)" class="rule-panel-body">
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
                    :field-id="`rule-${idx}-entry-mode`"
                    text="开仓方式"
                    :help="FIELD_HELP.entry_mode"
                  />
                  <select :id="`rule-${idx}-entry-mode`" v-model="r.entry_mode">
                    <option v-for="o in ENTRY_MODE_OPTIONS" :key="o.value" :value="o.value">
                      {{ o.label }}
                    </option>
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
                总手数 = 风险金额 {{ r.risk_amount }} ÷ 每手止损亏损（由信号止损价与品种规格算出）；底仓
                {{ r.add_batches ? r.base_ratio : 100 }}%
                {{ r.entry_mode === 'limit' ? '挂在信号入场价' : '市价成交' }}（TP=0）；分散仓第 i 单止盈 =
                开仓价 + 止损距离 × {{ r.rr_ratio }} × i ÷ {{ r.add_batches || 1 }}
              </p>
              <p v-if="r.entry_mode === 'limit'" class="rule-hint warn">
                限价开仓：信号必须携带入场价（limit_price / price），否则本策略不参与分发；
                底仓与分散仓全部挂在该入场价，挂单一直等到成交（GTC），期间该节点该品种不接新信号。
                止损共用信号 sl，分散仓仍按入场价铺阶梯止盈；手数口径与市价相同。
              </p>

              <div class="batch-block">
                <div class="batch-head">
                  <FormLabel text="分散仓" :help="FIELD_HELP.add_batches" />
                  <span v-if="r.add_batches" class="muted batch-count-hint">
                    剩余 {{ 100 - r.base_ratio }}% 等分 {{ r.add_batches }} 单 · 共
                    {{ 1 + r.add_batches }} 单 · 阶梯止盈
                  </span>
                  <span v-else class="muted batch-count-hint">底仓即全仓</span>
                </div>
                <div class="batch-top-grid">
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-add-batches`"
                      text="分散仓单数"
                      :help="FIELD_HELP.add_batches"
                    />
                    <input
                      :id="`rule-${idx}-add-batches`"
                      v-model.number="r.add_batches"
                      type="number"
                      min="0"
                      max="50"
                      step="1"
                    />
                  </div>
                </div>
                <p class="rule-hint">
                  {{
                    r.entry_mode === 'limit'
                      ? '与底仓挂在同一个入场价，一并挂出'
                      : '开仓时与底仓一并市价打出'
                  }}；各单共用信号止损价，分散仓按盈亏比挂止盈，底仓止盈为 0
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
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-breakeven-mode`"
                      text="监控方式"
                      :help="FIELD_HELP.breakeven_mode"
                    />
                    <select :id="`rule-${idx}-breakeven-mode`" v-model="r.breakeven_mode">
                      <option v-for="o in BREAKEVEN_MODE_OPTIONS" :key="o.value" :value="o.value">
                        {{ o.label }}
                      </option>
                    </select>
                  </div>
                </div>
                <p v-if="r.breakeven_enabled" class="rule-hint">
                  浮盈达到止损距离 × {{ r.breakeven_times }} 倍时，把该任务全部持仓的止损移到加权均价；
                  {{ r.breakeven_mode === 'loop' ? '循环监控，止损未到位时可再次移动' : '按次监控，触发一次后停止' }}
                </p>
              </div>
            </template>

            <!-- 网格交易（模版3） -->
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
                    <FormLabel
                      :field-id="`rule-${idx}-stop-sl`"
                      :text="gridStopFields(r.grid_side).slLabel"
                      :help="gridStopFields(r.grid_side).slHelp"
                    />
                    <input
                      :id="`rule-${idx}-stop-sl`"
                      v-model.number="r[gridStopFields(r.grid_side).slKey]"
                      type="number"
                      min="0"
                      step="any"
                    />
                  </div>
                  <div class="field">
                    <FormLabel
                      :field-id="`rule-${idx}-stop-tp`"
                      :text="gridStopFields(r.grid_side).tpLabel"
                      :help="gridStopFields(r.grid_side).tpHelp"
                    />
                    <input
                      :id="`rule-${idx}-stop-tp`"
                      v-model.number="r[gridStopFields(r.grid_side).tpKey]"
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

              <div class="batch-block">
                <div class="batch-head">
                  <FormLabel text="试算网格" :help="FIELD_HELP.assist_enabled" />
                  <input
                    type="checkbox"
                    :checked="r.assist_enabled"
                    aria-label="启用网格试算"
                    @change="r.assist_enabled = ($event.target as HTMLInputElement).checked"
                  />
                </div>
                <template v-if="r.assist_enabled">
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-node`"
                        text="行情源节点"
                        :help="FIELD_HELP.assist_node"
                      />
                      <select :id="`rule-${idx}-assist-node`" v-model="sizingState(idx).nodeId">
                        <option value="">请选择在线节点</option>
                        <option v-for="n in onlineNodes" :key="n.node_id" :value="n.node_id">
                          {{ nodeLabel(n) }}
                        </option>
                      </select>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-tf`"
                        text="K线周期"
                        :help="FIELD_HELP.assist_timeframe"
                      />
                      <select :id="`rule-${idx}-assist-tf`" v-model="r.assist_timeframe">
                        <option v-for="tf in TIMEFRAME_OPTIONS" :key="tf" :value="tf">{{ tf }}</option>
                      </select>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-mult`"
                        text="ATR 倍数"
                        :help="FIELD_HELP.assist_atr_mult"
                      />
                      <input
                        :id="`rule-${idx}-assist-mult`"
                        v-model.number="r.assist_atr_mult"
                        type="number"
                        min="0"
                        step="0.1"
                        @change="r.assist_spacing = 0"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-spacing`"
                        text="格距"
                        :help="FIELD_HELP.assist_spacing"
                      />
                      <input
                        :id="`rule-${idx}-assist-spacing`"
                        v-model.number="r.assist_spacing"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-loss`"
                        text="最大可接受亏损"
                        :help="FIELD_HELP.assist_max_loss"
                      />
                      <input
                        :id="`rule-${idx}-assist-loss`"
                        v-model.number="r.assist_max_loss"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                  </div>

                  <div class="assist-actions">
                    <button
                      type="button"
                      class="btn-sm btn-ghost"
                      :disabled="sizingState(idx).loading"
                      @click="runSizing(idx, r)"
                    >
                      {{ sizingState(idx).loading ? '试算中…' : '取行情并试算' }}
                    </button>
                    <button
                      type="button"
                      class="btn-sm btn-ghost"
                      :disabled="!sizingState(idx).data?.ready || sizingStale(idx, r)"
                      @click="applySizing(idx, r)"
                    >
                      应用建议
                    </button>
                  </div>

                  <p v-if="sizingState(idx).error" class="rule-hint warn">
                    {{ sizingState(idx).error }}
                  </p>
                  <template v-else-if="sizingState(idx).data">
                    <p class="rule-hint">
                      {{ sizingSummary(sizingState(idx).data!) }}
                    </p>
                    <p v-if="sizingStale(idx, r)" class="rule-hint warn">
                      参数已改动，请重新试算后再应用
                    </p>
                    <p
                      v-for="w in sizingState(idx).data!.warnings"
                      :key="w.code + w.message"
                      class="rule-hint"
                      :class="{ warn: w.level === 'warn' }"
                    >
                      {{ w.message }}
                    </p>
                  </template>
                  <p v-else class="rule-hint">
                    {{ sizingBlocker(r) || '选好行情源节点后点「取行情并试算」' }}
                  </p>
                </template>
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
        </div>

        <p v-if="formError" class="form-error" style="margin-top: 10px">{{ formError }}</p>
      </div>

      <div class="modal-footer card-pad" style="padding-top: 0">
        <span></span>
        <div class="row" style="gap: 8px">
          <button class="btn-ghost" :disabled="saving" @click="close">取消</button>
          <button class="btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : isEditMode ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
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

.rule-panel.is-collapsed .rule-panel-head {
  margin-bottom: 0;
}

.rule-panel-head.is-foldable {
  cursor: pointer;
}

.rule-title-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.rule-fold-caret {
  flex-shrink: 0;
  width: 14px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1;
}

.rule-title-wrap :deep(.form-label-wrap) {
  margin-bottom: 0;
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

.rule-hint.warn {
  color: var(--red);
}

.assist-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
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
</style>
