<script setup lang="ts">
// 策略新建 / 编辑弹窗：表单逻辑、校验与提交
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import FormLabel from '@/components/FormLabel.vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import StrategySignalFields from '@/components/StrategySignalFields.vue'
import { signalSymbolMatches, type StrategySignalDraft } from '@/components/strategySignal'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import type {
  BatchCalcType,
  BatchTimeframe,
  EntryMode,
  GridMode,
  GridSide,
  GridSizingData,
  NodeOut,
  RiskMonitorMode,
  RiskSideAction,
  SignalFloatPlRatio,
  SignalLotPlTiers,
  ManualSignalPayload,
  ManualSignalResult,
  StrategyBatchLevel,
  ManualScatterConfig,
  ManualScatterState,
  StrategyOut,
  StrategyRule,
  StrategyRuntimeHints,
  StrategyTemplateOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    mode: 'create' | 'edit'
    strategyId?: string
    listSearchOptions?: { q?: string }
    /** 从分组列表「编辑策略」进入时传入；有值才显示触发信号 */
    triggerGroup?: { group_id: string; name: string } | null
    /** 锁定策略模版（新建时预选并禁用模版选择）。用于「AI 交易」入口只做 tpl_1。 */
    presetTemplateId?: string
    /** 预填绑定品种（新建时）。配合 presetSymbolLocked 可锁定品种输入 */
    presetSymbol?: string
    /** 锁定品种输入框（品种由调用方决定，如跟随当前选中品种） */
    presetSymbolLocked?: boolean
  }>(),
  {
    strategyId: '',
    listSearchOptions: () => ({}),
    triggerGroup: null,
    presetTemplateId: '',
    presetSymbol: '',
    presetSymbolLocked: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  /**
   * 保存成功。payload 为 null 表示关闭时未保存（保留旧签名兼容）。
   * 携带创建/更新后的策略信息，便于调用方自动建分组并绑定主节点。
   */
  saved: [payload?: { strategyId: string; name: string; symbol: string; templateId: string } | null]
}>()

/** ATR / 波幅统计的已收盘 K 线根数，与后端 BATCH_BAR_PERIOD 一致，固定不可配 */
const BATCH_BAR_PERIOD = 14
const TEMPLATE_1_ID = 'tpl_1'

/**
 * 波幅自适应（纳入当前 K 线实时高低差）的固定参数。
 * 表单不提供开关与输入框：逆势 + 波幅档位自动开启，参数固定为「上限 1000 倍 / 重检 5 秒」。
 * 数值与后端 strategy_templates / 节点端 strategy_rules 的缺省一致。
 */
const RANGE_LIVE_CAP_DEFAULT = 1000
const RANGE_LIVE_REFRESH_DEFAULT = 5
/** M1 档不纳入实时波幅：要让第一笔加仓先成交，M1 档必须保持原样 */
const RANGE_LIVE_SKIP_TIMEFRAME: BatchTimeframe = 'M1'

/** 规则 type，与后端 strategy_templates 对齐 */
const RULE_TYPE_COUNTER = 1
const RULE_TYPE_TREND = 2
const RULE_TYPE_RISK_SIZED = 3
const RULE_TYPE_GRID = 4

/**
 * 档位编辑模式：
 * - redistribute：重新均分区间（没有在跑任务时才安全）
 * - append：已有档位区间不动，新档位接在后面（有在跑任务时必须用这个）
 */
type BatchEditMode = 'redistribute' | 'append'

const hub = useHubStore()
const auth = useAuthStore()
const templates = ref<StrategyTemplateOut[]>([])
const editingTemplateName = ref('')
const saving = ref(false)
const formError = ref('')
/**
 * 保存前的**提醒**（不阻止保存）。
 *
 * 配置无条件热推：字段值一律采纳用户填的值，「加不加 / 怎么加」由节点
 * 自身状态决定。所以「上限低于已开笔数」这类情况不再是错误，只是一条
 * 提醒——告诉用户保存后会怎样，但拦不住他。
 */
const formWarnings = ref<string[]>([])
/**
 * 运行态提示（编辑态从详情接口取）：在跑任务的已开笔数、手动仓是否已开。
 * 用来在保存前拦下「上限低于已开笔数」这类非法修改，并把锁定区标出来。
 */
const runtimeHints = ref<StrategyRuntimeHints | null>(null)
const form = reactive({
  template_id: '',
  name: '',
  symbol: '',
  rules: [] as EditableRule[],
})

const signalDraft = reactive<StrategySignalDraft>({
  symbol: '',
  action: 'BUY',
  volume: 0.1,
  stop_loss: null,
  take_profit: null,
  entry_price: null,
  comment: '',
})

function resetSignalDraft(symbol: string): void {
  signalDraft.symbol = symbol
  signalDraft.action = 'BUY'
  signalDraft.volume = 0.1
  signalDraft.stop_loss = null
  signalDraft.take_profit = null
  signalDraft.entry_price = null
  signalDraft.comment = ''
}

const isEditMode = computed(() => props.mode === 'edit')
/** 锁定模版标识（模板里用于禁用模版选择器） */
const presetTemplateId = computed(() => props.presetTemplateId)

/**
 * 各规则已开笔数（运行态）：键是规则类型。
 * 列表接口没有这个数据，编辑时从详情接口取；取不到就当没有在跑任务。
 */
const addedByRule = computed<Record<number, number>>(() => {
  const raw = runtimeHints.value?.max_added_by_rule || {}
  const out: Record<number, number> = {}
  for (const [key, value] of Object.entries(raw)) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) out[Number(key)] = n
  }
  return out
})

/** 有在跑任务：这时改配置会影响节点侧的真实执行 */
const hasRunningTask = computed(
  () => Number(runtimeHints.value?.running_task_count || 0) > 0,
)

/**
 * 该规则的手动分散仓生命周期状态。
 * none=未配置 / 已被消费摘除；pending=挂单在盘上（还能改）；filled=已成交（锁定）；
 * void=挂单被撤销（本任务内不再自动挂）。
 */
function manualScatterState(ruleType: number): ManualScatterState {
  const states = runtimeHints.value?.manual_state || {}
  const raw = states[String(ruleType)] || states[Number(ruleType)]
  return (raw || 'none') as ManualScatterState
}

/**
 * 该规则的手动分散仓是否已锁死：只有**已成交**才锁。
 * 挂单还在盘上（pending）时客户可以改参数，节点会撤旧单重算重挂。
 */
function manualScatterLocked(ruleType: number): boolean {
  return manualScatterState(ruleType) === 'filled'
}

/** 挂单在盘上：可以改参数（保存后节点撤旧单重算重挂） */
function manualScatterPending(ruleType: number): boolean {
  return manualScatterState(ruleType) === 'pending'
}

/** 挂单被撤销：需要客户重新敲数字 */
function manualScatterVoid(ruleType: number): boolean {
  return manualScatterState(ruleType) === 'void'
}

/**
 * 挂单被撤销的原因文案。
 * 撤销有几种成因，说清楚是哪一种客户才知道下一步该做什么：
 * - removed：客户自己把手动单删了（保存后节点撤掉了那张单）
 * - ladder_tp：阶梯止盈先到，仓位已经收口
 * - replaced：参数调整，节点撤旧单重挂
 * - closed：任务收口
 */
function manualScatterVoidHint(ruleType: number): string {
  const reasons = runtimeHints.value?.manual_off_reason || {}
  const reason = reasons[String(ruleType)] || reasons[Number(ruleType)]
  if (reason === 'removed') {
    return '手动分散仓配置已删除，挂单已撤销：本次不再入场；如需继续请重新填写入场价与止盈价'
  }
  if (reason === 'ladder_tp') {
    return '手动分散仓挂单已被撤销（阶梯止盈先到）：本次不再入场；如需继续请重新填写入场价与止盈价'
  }
  if (reason === 'closed') {
    return '手动分散仓挂单已被撤销（任务收口）'
  }
  if (reason === 'replaced') {
    return '手动分散仓参数已调整，原挂单已撤销并按新参数重新挂单'
  }
  return '手动分散仓挂单已被撤销：本次不再入场；如需继续请重新填写入场价与止盈价'
}

/** 该规则的上限下限：不能低于已开笔数 */
function limitFloor(ruleType: number): number {
  return addedByRule.value[ruleType] || 0
}

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.template_id === form.template_id) || null,
)

const formTemplateLabel = computed(() => {
  if (selectedTemplate.value) return selectedTemplate.value.name
  return editingTemplateName.value || form.template_id || '—'
})

const templateTagOptions = computed(() => {
  const opts = templates.value.map((t) => {
    const isTpl1 = t.template_id === TEMPLATE_1_ID
    const locked = isTpl1 && !auth.canUseAiStrategy
    return {
      value: t.template_id,
      label: t.name + (locked ? '（需 AI 策略权限）' : ''),
      disabled: locked || (isEditMode.value && isTpl1),
    }
  })
  if (isEditMode.value && form.template_id && !selectedTemplate.value) {
    opts.push({ value: form.template_id, label: formTemplateLabel.value, disabled: false })
  }
  return opts
})

/** 当前用户无法使用 AI 加仓策略（需能力或管理员） */
const isTpl1LockedForCurrentUser = computed(() =>
  form.template_id === TEMPLATE_1_ID && !auth.canUseAiStrategy,
)

const MONITOR_MODE_TAGS: TagOption<RiskMonitorMode>[] = [
  { value: 'loop', label: '循环' },
  { value: 'times', label: '指定次数' },
]
const SIDE_TAGS: TagOption<string>[] = [
  { value: 'all', label: '全部' },
  { value: 'buy', label: '多单' },
  { value: 'sell', label: '空单' },
]
const SIDE_ACTION_TAGS: TagOption<RiskSideAction>[] = [
  { value: 'all', label: '全部' },
  { value: 'buy', label: '多单' },
  { value: 'sell', label: '空单' },
]

const RULE_TYPE_LABEL: Record<number, string> = {
  [RULE_TYPE_COUNTER]: '逆势加仓',
  [RULE_TYPE_TREND]: '顺势加仓',
  [RULE_TYPE_RISK_SIZED]: '以损定量趋势单',
  [RULE_TYPE_GRID]: '网格交易',
}

/**
 * 运行态横幅：告诉用户这个策略正在跑、改动会热推到节点，以及哪些地方已经锁死。
 */
const runtimeBanner = computed<{ title: string; lines: string[] } | null>(() => {
  const hints = runtimeHints.value
  if (!hints) return null
  const lines: string[] = []
  const count = Number(hints.running_task_count || 0)
  if (count > 0) {
    lines.push(
      `有 ${count} 个在跑任务，保存后配置会立即热推到节点并按新配置继续执行。`,
    )
    const offline = Number(hints.nodes_offline || 0)
    if (offline > 0) {
      lines.push(`其中 ${offline} 个节点当前离线，重连后会自动拉取最新配置。`)
    }
  }
  for (const [ruleType, added] of Object.entries(addedByRule.value)) {
    const label = RULE_TYPE_LABEL[Number(ruleType)] || `类型${ruleType}`
    lines.push(
      `${label}已开 ${added} 笔：把「最多加仓笔数」改到 ${added} 以下也可以保存，` +
        '保存后本任务不再新增批次，现有仓位继续由止盈止损管理。',
    )
  }
  // 加仓进度：加到第几批、还能再加几笔、下一批按什么参数加
  const progress = hints.add_progress || {}
  for (const [ruleType, p] of Object.entries(progress)) {
    const label = RULE_TYPE_LABEL[Number(ruleType)] || `类型${ruleType}`
    const added = Number(p.added || 0)
    const limit = Number(p.limit || 0)
    const remaining = Number(p.remaining ?? -1)
    const curIdx = Number(p.current_level_index ?? -1)
    const nextIdx = Number(p.next_level_index ?? -1)
    const nextGap = Number(p.next_gap_points || 0)
    const nextVol = Number(p.next_volume || 0)
    const parts: string[] = []
    parts.push(limit > 0 ? `已加 ${added} / ${limit} 笔` : `已加 ${added} 笔（不限次数）`)
    if (curIdx >= 0) parts.push(`当前第 ${curIdx + 1} 批`)
    if (remaining === -1) parts.push('还能继续加')
    else if (remaining > 0) parts.push(`还能再加 ${remaining} 笔`)
    else parts.push('已到上限，不再新增批次')
    if (nextIdx >= 0) {
      parts.push(`下一批 ${nextGap} 点 × ${nextVol} 手`)
    }
    lines.push(`${label}加仓进度：${parts.join('，')}。`)
  }
  const fired = hints.manual_fired || []
  if (fired.length) {
    const labels = fired.map((t) => RULE_TYPE_LABEL[Number(t)] || `类型${t}`)
    lines.push(`${labels.join('、')}的手动分散仓进场后，任务加仓会收口，配置直接锁定。`)
  }
  // 手动单的生命周期：挂单在盘上的还能改，已成交的锁死
  const states = hints.manual_state || {}
  for (const [ruleType, state] of Object.entries(states)) {
    const label = RULE_TYPE_LABEL[Number(ruleType)] || `类型${ruleType}`
    if (state === 'pending') {
      lines.push(
        `${label}的手动分散仓挂单在盘上：入场价 / 止盈 / 止损仍可修改，` +
          '保存后节点会撤掉旧单、按新参数重算手数并重新挂单；' +
          '删除手动单并保存则会撤掉这张挂单。',
      )
    } else if (state === 'filled') {
      lines.push(`${label}的手动分散仓已成交：配置已锁定，之后不能再修改。`)
    } else if (state === 'void') {
      lines.push(`${label}的${manualScatterVoidHint(Number(ruleType))}`)
    }
  }
  if (!lines.length) return null
  return { title: '运行中任务提示', lines }
})

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

/** 逆势加仓默认展开；顺势加仓始终展开。点标题可展开/收起逆势。 */
const counterExpanded = ref(true)

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
  counterExpanded.value = true
}

const RULE_TYPE_HELP: Record<number, string> = {
  [RULE_TYPE_COUNTER]:
    '逆势加仓：以首仓开仓价为锚（若已有更深的逆势仓则取最深一笔），按下一档间距预先挂出一张限价' +
    '（多单挂买限价，空单挂卖限价）。每次只有一张；点数、ATR、波幅、指定价或 SMC 价位变化时改这张单，成交后再挂下一档。' +
    '挂单价落到现价错误一侧，或离现价小于券商最小距离时，这一档改为市价。' +
    '中间的顺势加仓不会抬高或压低逆势锚点。实际手数 = 倍数 × 基础订单手数 + 额外手数。',
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
    '实际加仓手数 = 倍数 × 基础订单手数 + 额外手数；触发距离 = 点数 × Point()。' +
    '模版1 可另配信号级盈亏控制：每个信号独立监控，触发只平该信号仓位。',
  status:
    '关闭后本条规则不会参与监控与加仓；已产生的历史订单不受影响。',
  action:
    '监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '顺势加仓取该方向最近一笔订单为基准；逆势加仓取首仓（或更深的逆势仓）为锚。',
  point:
    '加仓间距点数。顺势以最近一笔为基准，偏离达到「点数 × Point()」后市价加仓。' +
    '逆势以首仓（或更深逆势仓）为锚，按同样点数预先挂限价。Point() 为品种最小价格变动单位。',
  lot_times:
    '加仓倍数。以监控方向最近订单的手数为基准，参与计算：倍数 × 基础手数。',
  extra_lot:
    '额外手数，默认 0。加在倍率结果之后：实际手数 = 倍数 × 基础手数 + 额外手数。',
  max_allow_num:
    '本规则最多允许加仓次数（不含开仓，也不含另一条规则加出去的仓）。达到上限后本条规则不再加仓。',
  batch_enabled:
    '开启后按本规则第几次加仓命中下方档位，使用该档的点数 / 倍数 / 额外手数，' +
    '覆盖上方基础参数。可改批数自动切档，也可手动增删、改加仓笔数。开仓不占格，与另一条规则的档位独立。',
  batch_action:
    '分批加仓独立的监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '可与上方基础监控方向不同。',
  batch_count:
    '分批批数。改批数或加仓笔数会按「第 1 次 ~ 加仓笔数」均分生成档位；' +
    '添加 / 删除档位也会按同一上限重切区间，批数与档位数保持一致。' +
    '启用手动分散仓时，这里显示合计（分散仓 + 1），提交仍只保存分散仓批数。' +
    '有在跑任务时改为「追加」：已有档位区间不动，新档位接在末尾，' +
    '避免重新均分改变后续加仓命中的档位参数。',
  total_lot_limit:
    '本规则最多加仓笔数，不含开仓，与另一条规则独立。同时作为自动切档的末笔。' +
    '例如填 2 表示开仓后本规则最多再加 2 笔。改此项会按批数重新均分档位；添加档位若超过可切份数会顺延该上限。' +
    '启用手动分散仓时，这里显示合计（分散仓加仓笔数 + 1），提交仍只保存分散仓上限。' +
    '填 0 表示不限笔数（不限时不受「不能低于已开笔数」的限制）。' +
    '有在跑任务时，上限可以调大也可以调小（缩减剩余额度），' +
    '但不能低于该规则已开笔数——已开的仓不会被砍掉。',
  batch_level:
    '本档命中的加仓次数区间（从 1 起，含两端），可手改，相邻档会首尾衔接。' +
    '添加 / 删除档位会按加仓笔数重新均分各档区间。' +
    '命中区间时按该档间距判断是否加仓，手数 = 倍数 × 基础手数 + 额外手数。',
  manual_scatter:
    '手动分散仓与分批加仓无关：本规则的分批加仓加满后，手动仓以「限价单」挂到券商，成交价正好是入场价。' +
    '手数在挂出那一刻就按存量仓位反推算死，之后行情怎么走都不重算：' +
    '手数 =（分档第一档目标金额 − 存量仓位到止盈时刻的预估盈亏）÷ 每手止盈。' +
    '存量浮盈要抵扣（少开手动仓），存量浮亏要补上（多开手动仓）；' +
    '存量在止盈时刻的盈亏由存量开仓价决定（历史事实），手动仓的盈利由固定入场价与止盈价决定，' +
    '所以价格走到止盈那一刻，本任务全部仓位的净额刚好等于第一档目标金额。' +
    '成交后手动仓的止盈价成为本魔术号所有仓位的止盈权威，其余仓位的止盈统一改到它。' +
    '视觉上接在最后一档之后（档位号 +1，持仓笔数为末档终点 +1），不参与分批判定。' +
    '逆势还有在途限价单时同样算没加完。分批未启用时不受此限制。' +
    '手数 0 表示挂单时按当时持仓匹配「分档手数盈亏」反推；手改手数后按填写值挂单。' +
    '「止盈联动清仓」默认勾选：该手动单被券商止盈平掉后，节点平掉该信号其余持仓并收口；取消勾选则只影响它自己。' +
    '顺势 / 逆势各只能配一条。保存后若任务已在跑，会立刻下发到节点。',
  manual_entry:
    '这张限价单的挂单价，成交价正好是它。止盈高于入场为多，低于入场为空。' +
    '现价在入场上方就挂 Buy Limit 等回落，在下方就挂 Buy Stop 等反弹；现价已越过止盈则暂不挂（记日志）。',
  manual_tp: '成交后统一同步到本任务全部仓位的止盈价；挂单本身不带止盈。须与入场价不同。',
  manual_sl: '挂在限价单上的止损价；0 表示不设止损。止损必须落在入场价的反方向，否则券商会拒掉整张单，此时节点不带止损。',
  manual_volume:
    '0 = 挂单那一刻，用该魔术号当时已持有的手数去匹配分档手数盈亏，再按价差 × tick 反推；手数在挂出时算死。' +
    '只计已经开出的仓（首单、分批加仓、另一条规则已成交的手动单），不含本条尚未成交的手动分散仓。' +
    '存量在止盈时刻浮盈会抵扣目标金额、浮亏会补进目标金额。' +
    '手改后锁定，不再自动重算。',
  manual_close_all_on_tp:
    '勾选后，这条手动单被券商按止盈价平掉时，节点会平掉该信号（同魔术号）其余全部持仓并结束任务。' +
    '离场原因以 MT5 成交历史为准：止损、人工或程序平仓不触发；成交历史晚到时以平仓侧现价到止盈兜底。' +
    '节点离线期间被止盈的，重连后也会补做。默认勾选；取消勾选则手动单止盈只影响它自己。',
  calc_type:
    '本档加仓间距怎么算：\n' +
    '点数 = 固定间距「点数 × Point()」；\n' +
    '指定价 = 绝对价位，多单须低于锚点、空单须高于锚点；\n' +
    `ATR = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线的平均真实波幅作间距；\n` +
    `波幅 = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线的高低点总跨度（最高价 − 最低价）作间距；\n` +
    'FVG / OB / PDZ / FIBO = 只作标识，节点不计算指标。仅逆势档位可选，' +
    '等 TradingView 按所选 K 线周期推送同类型价位。\n' +
    '顺势：现价偏离达到间距后市价加仓。逆势：按间距预先挂一张限价，间距变化就改这张单；' +
    '价位已经不能挂（穿过现价或近于券商最小距离）时这一档改为市价。',
  batch_smc_timeframe:
    '只用来和 TradingView 推送里的 tf 对应，节点不会按这个周期去读 K 线。' +
    '可选 M1 / M5 / M15 / M30 / H1 / H4 / D1 / W1 / MN。',
  batch_price:
    '本档的指定价位（绝对价格）。逆势时多单需低于参考价、空单需高于参考价；' +
    '顺势方向相反。价位方向不符或留空时本档不会触发。' +
    '逆势时节点把这一档挂成限价/止损单，成交价**正好等于**指定价（不滑点）；' +
    '顺势的指定价仍是「到价后市价成交」，成交价可能有滑点，不保证等于指定价。',
  batch_price_tp:
    '这一档的止盈**参考价**（绝对价格）：**只用来反推这一档的手数**' +
    '（按阶梯止盈第 1 档的目标金额，减去存量仓位到该价时的预估盈亏，再除以每手止盈）。' +
    '这一档**不单独止盈**——成交后出场跟随大部队的阶梯止盈，一块走。' +
    '留空则无法算出手数，该档不会挂单。',
  batch_price_sl:
    '这一档**自己的止损**（绝对价格），0 = 不设。必须落在入场价的反方向，否则券商会拒掉整张挂单。' +
    '成交后这一道止损会锁定，之后改配置不影响已开仓位。' +
    '它与大部队的共同止损（信号盈亏比 × 余额）同时存在，**离现价更近的那道生效**。' +
    '这一道被打掉后不收口：剩余仓位按新的加权均价与阶梯止盈金额重算 TP / SL 继续跑。',
  batch_timeframe:
    `统计 ATR / 波幅用的 K 线周期，固定取最近 ${BATCH_BAR_PERIOD} 根**已收盘** K 线（不含当前未走完的那根）。` +
    '算出的价格距离会换算成点数，再与实际偏离比较。' +
    '波幅口径会自动纳入当前这根未走完 K 线已走出的高低差（M1 档除外）。',
  range_auto:
    '逆势加仓的「波幅」档位自动纳入当前这根未走完 K 线的实时高低差，无需配置。\n' +
    '本档间距 = max(' +
    `${BATCH_BAR_PERIOD} 根已收盘 K 线的高低点总跨度, 当前这根未走完 K 线的实时高低差)。\n` +
    '急跌时实时高低差会迅速变大，间距被自动推远，逆势限价单不会在下跌中位被立刻成交。\n' +
    '行情平稳时与原逻辑完全一致。K 线收盘时实时值会并入已收盘窗口，间距自然交接、不会突然缩回。\n' +
    '仅逆势加仓可用；顺势放大间距等于主动放弃动量。\n' +
    `异常保护固定为上限 ${RANGE_LIVE_CAP_DEFAULT} 倍、重检间隔 ${RANGE_LIVE_REFRESH_DEFAULT} 秒：` +
    '实时波幅超过「基线波幅 × 上限倍数」时判定为数据异常，直接忽略实时值、退回基线；' +
    '重检间隔是刷新实时波幅的节奏，也是限价改单的最小间隔。\n' +
    'M1 档不纳入实时波幅：要让第一笔加仓先成交，M1 档必须保持原样。',
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
  signal_pl:
    '止盈和止损都写到该信号的订单上，由券商到价平仓。不按浮盈抽样平仓。\n' +
    '开仓和加仓的市价单发出时就带上当时算出的价；成交价和预估有偏差时，下一拍再改单对齐。\n' +
    '止损在新开仓和「配置热推」后重算；报价波动不重算。止盈在手数变化时重算。',
  signal_float_pl:
    '用这个百分比的绝对值 × 当时账户余额，得到亏损金额，再反推共同止损价。\n' +
    '-20 和 20 都表示亏损达到余额的 20% 时止损。新开仓和热推改比例后都会重算。',
  signal_lot_pl:
    '只用批次1的盈亏金额绝对值反推共同止盈，不写止损。\n' +
    '该方向总手数达到批次1的「总 lot >=」才开始计算。更高批次不参与这个止盈价。',
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
  Omit<
    StrategyRule,
    'batch_levels' | 'grid_mode' | 'grid_side' | 'breakeven_mode' | 'float_pl_ratio' | 'lot_pl_tiers' | 'manual_scatter'
  >
> & {
  batch_levels: StrategyBatchLevel[]
  grid_mode: GridMode
  grid_side: GridSide
  breakeven_mode: 'once' | 'loop'
  manual_scatter: ManualScatterConfig
}

function defaultManualScatter(): ManualScatterConfig {
  return {
    enabled: false,
    entry_price: 0,
    take_profit: 0,
    stop_loss: 0,
    volume: 0,
    volume_locked: false,
    close_all_on_tp: true,
  }
}

/**
 * 前端新建策略时的默认启用状态：逆势加仓默认打开，顺势加仓默认关闭。
 * 只作用于「按模版载入默认规则 / 恢复默认」；编辑存量策略时保留已保存的状态。
 */
const DEFAULT_RULE_STATUS: Record<number, number> = {
  [RULE_TYPE_COUNTER]: 1,
  [RULE_TYPE_TREND]: 0,
}

/** 按类型套用默认启用状态（未列出的类型保持原值） */
function withDefaultStatus(rules: StrategyRule[]): StrategyRule[] {
  return rules.map((r) => {
    const def = DEFAULT_RULE_STATUS[r.type]
    if (def === undefined) return r
    return { ...r, status: def }
  })
}

function cloneRules(rules: StrategyRule[]): EditableRule[] {
  return rules.map((r) => {
    const cloned: EditableRule = {
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
      batch_levels: (r.batch_levels || []).map((lv) => ({
        ...lv,
        // 老配置没有波幅自适应的三个字段，补默认值
        range_include_live: lv.range_include_live ?? false,
        range_live_cap: lv.range_live_cap ?? RANGE_LIVE_CAP_DEFAULT,
        range_live_refresh: lv.range_live_refresh ?? RANGE_LIVE_REFRESH_DEFAULT,
        // 指定价档位的止盈参考价与自有止损：老配置没有，补 0
        price_tp: lv.price_tp ?? 0,
        price_sl: lv.price_sl ?? 0,
      })),
      manual_scatter: r.manual_scatter?.enabled
        ? { ...defaultManualScatter(), ...r.manual_scatter, enabled: true }
        : defaultManualScatter(),
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
    }
    return cloned
  })
}

type SignalPlForm = {
  float_pl_ratio: SignalFloatPlRatio
  lot_pl_tiers: SignalLotPlTiers
}

function defaultSignalPl(): SignalPlForm {
  return {
    float_pl_ratio: {
      enabled: false,
      ratio: -20,
      action: 'close_all',
      monitor_mode: 'loop',
      max_times: 1,
    },
    lot_pl_tiers: {
      enabled: false,
      batch_count: 2,
      close_action: 'all',
      tiers: [
        { min_lot: 0.1, pl_amount: 50 },
        { min_lot: 0.5, pl_amount: 100 },
      ],
    },
  }
}

function rebuildSignalLotTiers(
  count: number,
  prev: SignalLotPlTiers['tiers'],
): SignalLotPlTiers['tiers'] {
  const n = Math.max(1, Math.min(10, Math.floor(count) || 2))
  const tiers = []
  for (let i = 0; i < n; i++) {
    const old = prev[i]
    tiers.push({
      min_lot: Number(old?.min_lot ?? 0.1 * (i + 1)),
      pl_amount: Number(old?.pl_amount ?? 50 * (i + 1)),
    })
  }
  return tiers
}

function cloneSignalPl(src?: Partial<SignalPlForm> | null): SignalPlForm {
  const base = defaultSignalPl()
  const pl = src?.float_pl_ratio
  const lpt = src?.lot_pl_tiers
  const mode = String(pl?.monitor_mode || 'loop') === 'times' ? 'times' : 'loop'
  const closeAction = ['all', 'buy', 'sell'].includes(String(lpt?.close_action))
    ? (lpt!.close_action as RiskSideAction)
    : 'all'
  const batchCount = Math.max(1, Math.min(10, Math.floor(Number(lpt?.batch_count) || 2)))
  return {
    float_pl_ratio: {
      enabled: Boolean(pl?.enabled),
      ratio: Number(pl?.ratio ?? base.float_pl_ratio.ratio),
      action: 'close_all',
      monitor_mode: mode as RiskMonitorMode,
      max_times: Math.max(1, Math.floor(Number(pl?.max_times) || 1)),
    },
    lot_pl_tiers: {
      enabled: Boolean(lpt?.enabled),
      batch_count: batchCount,
      close_action: closeAction,
      tiers: rebuildSignalLotTiers(batchCount, lpt?.tiers || base.lot_pl_tiers.tiers),
    },
  }
}

function extractSignalPl(rules: Array<{ type: number; status?: number; float_pl_ratio?: SignalFloatPlRatio; lot_pl_tiers?: SignalLotPlTiers }>): SignalPlForm {
  const addon = rules.filter((r) => r.type === RULE_TYPE_COUNTER || r.type === RULE_TYPE_TREND)
  const picked = addon.find((r) => r.status === 1) || addon[0]
  if (!picked) return defaultSignalPl()
  return cloneSignalPl({
    float_pl_ratio: picked.float_pl_ratio,
    lot_pl_tiers: picked.lot_pl_tiers,
  })
}

function applySignalPlToRules(rules: EditableRule[], cfg: SignalPlForm): StrategyRule[] {
  return rules.map((r) => {
    if (r.type !== RULE_TYPE_COUNTER && r.type !== RULE_TYPE_TREND) return r
    return {
      ...r,
      float_pl_ratio: { ...cfg.float_pl_ratio },
      lot_pl_tiers: {
        ...cfg.lot_pl_tiers,
        tiers: cfg.lot_pl_tiers.tiers.map((t) => ({ ...t })),
      },
    }
  })
}

const signalPl = reactive<SignalPlForm>(defaultSignalPl())
const isTpl1 = computed(() => form.template_id === TEMPLATE_1_ID)

/** 分批档位从本规则第 1 次加仓起算（开仓不占格） */
const BATCH_POS_START = 1

/** 加仓间距的计算方式；与后端 strategy_templates.BATCH_CALC_TYPES 对齐 */
const CALC_TYPE_OPTIONS: Array<{ value: BatchCalcType; label: string }> = [
  { value: 'point', label: '点数' },
  { value: 'price', label: '指定价' },
  { value: 'atr', label: 'ATR' },
  { value: 'range', label: '波幅' },
]

/** 只给逆势档位：节点不计算这些指标，只用来匹配 TradingView 推送 */
const SMC_CALC_TYPE_OPTIONS: Array<{ value: BatchCalcType; label: string }> = [
  { value: 'fvg', label: 'FVG' },
  { value: 'ob', label: 'OB' },
  { value: 'pdz', label: 'PDZ' },
  { value: 'fibo', label: 'FIBO' },
]
const SMC_CALC_TYPES: BatchCalcType[] = SMC_CALC_TYPE_OPTIONS.map((o) => o.value)

function calcTypeOptions(ruleType: number): Array<{ value: BatchCalcType; label: string }> {
  if (ruleType === RULE_TYPE_COUNTER) return [...CALC_TYPE_OPTIONS, ...SMC_CALC_TYPE_OPTIONS]
  return CALC_TYPE_OPTIONS
}

function isSmcCalc(calcType: BatchCalcType): boolean {
  return SMC_CALC_TYPES.includes(calcType)
}

/** ATR / 波幅可选周期。不提供「当前图表周期」：节点是独立进程，没有图表上下文 */
const TIMEFRAME_OPTIONS: BatchTimeframe[] = [
  'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN',
]
const TIMEFRAME_TAGS: TagOption<BatchTimeframe>[] = TIMEFRAME_OPTIONS.map((tf) => ({
  value: tf,
  label: tf,
}))

/** 需要读 K 线才能算出间距的方式 */
const BAR_CALC_TYPES: BatchCalcType[] = ['atr', 'range']

function isBarCalc(calcType: BatchCalcType): boolean {
  return BAR_CALC_TYPES.includes(calcType)
}

/**
 * 本档是否自动「纳入当前 K 线实时波幅」：只有逆势 + 波幅口径可用，且排除 M1 档。
 * 顺势放大间距等于主动放弃动量，服务端也会强制关闭；
 * M1 档要保证第一笔加仓先成交，保持原样（只按已收盘 K 线波幅）。
 */
function shouldAutoRangeLive(r: EditableRule, lv: StrategyBatchLevel): boolean {
  return (
    r.type === RULE_TYPE_COUNTER &&
    lv.calc_type === 'range' &&
    lv.timeframe !== RANGE_LIVE_SKIP_TIMEFRAME
  )
}

/** 写入固定的波幅自适应参数：开关按场景，参数一律用固定默认值 */
function applyRangeLiveDefaults(lv: StrategyBatchLevel, enabled: boolean): void {
  lv.range_include_live = enabled
  lv.range_live_cap = RANGE_LIVE_CAP_DEFAULT
  lv.range_live_refresh = RANGE_LIVE_REFRESH_DEFAULT
}

/** 计算方式切换：波幅口径按规则自动开/关实时自适应，其余口径一律关闭 */
function onCalcTypeChange(r: EditableRule, lv: StrategyBatchLevel): void {
  applyRangeLiveDefaults(lv, shouldAutoRangeLive(r, lv))
}

/** K 线周期切换：改成 M1 就退出实时自适应，改回其它周期再按规则打开 */
function onTimeframeChange(r: EditableRule, lv: StrategyBatchLevel): void {
  applyRangeLiveDefaults(lv, shouldAutoRangeLive(r, lv))
}

/**
 * 提交前统一归一波幅自适应参数：
 * 逆势 + 波幅 + 非 M1 的档位恒为「纳入实时」，其余档位一律关闭；
 * 上限倍数 / 重检间隔固定为 1000 / 5，不接受外部输入。
 */
function normalizeRangeLive(rules: EditableRule[]): void {
  for (const r of rules) {
    for (const lv of r.batch_levels || []) {
      applyRangeLiveDefaults(lv, shouldAutoRangeLive(r, lv))
    }
  }
}

/** 将 [1, limit] 均分为 count 段：第 1 次加仓 ~ 本规则加仓笔数上限 */
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
  | 'calc_type'
  | 'point'
  | 'price'
  | 'price_tp'
  | 'price_sl'
  | 'timeframe'
  | 'range_include_live'
  | 'range_live_cap'
  | 'range_live_refresh'
  | 'lot_times'
  | 'extra_lot'
>

function defaultLevelParams(index: number, prev?: StrategyBatchLevel): LevelParams {
  if (prev) {
    return {
      calc_type: prev.calc_type || 'point',
      point: prev.point,
      price: prev.price ?? 0,
      price_tp: prev.price_tp ?? 0,
      price_sl: prev.price_sl ?? 0,
      timeframe: prev.timeframe || 'M5',
      // 存量配置原样带入，实际生效值由提交前的 normalizeRangeLive 统一归一
      range_include_live: prev.range_include_live ?? false,
      range_live_cap: prev.range_live_cap ?? RANGE_LIVE_CAP_DEFAULT,
      range_live_refresh: prev.range_live_refresh ?? RANGE_LIVE_REFRESH_DEFAULT,
      lot_times: prev.lot_times,
      extra_lot: prev.extra_lot,
    }
  }
  return {
    calc_type: 'point',
    point: 100 + index * 100,
    price: 0,
    price_tp: 0,
    price_sl: 0,
    timeframe: 'M5',
    range_include_live: false,
    range_live_cap: RANGE_LIVE_CAP_DEFAULT,
    range_live_refresh: RANGE_LIVE_REFRESH_DEFAULT,
    lot_times: Number((1.1 + index * 0.1).toFixed(2)),
    extra_lot: 0,
  }
}

/**
 * 按 batch_count + total_lot_limit 重建档位；保留已有档的点/倍/手参数。
 *
 * 两种模式：
 * - redistribute：把区间重新均分。适合没有在跑任务时——怎么分都无所谓。
 * - append：已有档位的区间**原样不动**，新档位接在末尾。
 *   有在跑任务时必须用这个：节点按「本规则第几次加仓」命中档位，
 *   重新均分会改变后续加仓命中的档位参数，等于偷偷改了运行中的策略。
 */
function rebuildBatchLevels(r: EditableRule, mode: BatchEditMode = 'redistribute'): void {
  const limit = Math.max(BATCH_POS_START, Math.floor(Number(r.total_lot_limit) || BATCH_POS_START))
  r.total_lot_limit = limit
  const maxCount = limit - BATCH_POS_START + 1
  let count = Math.max(1, Math.floor(Number(r.batch_count) || 1))
  if (count > maxCount) count = maxCount
  r.batch_count = count

  const old = r.batch_levels
  if (mode === 'append') {
    r.batch_levels = appendBatchRanges(old, limit, count)
    return
  }
  r.batch_levels = splitBatchRanges(limit, count).map((range, i) => ({
    ...range,
    ...defaultLevelParams(i, old[i]),
  }))
}

/**
 * append 模式：前 count-1 个档位保持原区间，第 count 个档位吃掉剩下的区间。
 *
 * 已有档位若超出新上限（用户把上限缩小了），末尾的档位被截断——
 * 但被截断的那一段已经开过仓了，节点侧照样按旧配置走，不影响已开仓位。
 */
function appendBatchRanges(
  old: StrategyBatchLevel[],
  limit: number,
  count: number,
): StrategyBatchLevel[] {
  const n = Math.max(1, Math.min(count, Math.max(1, limit - BATCH_POS_START + 1)))
  const out: StrategyBatchLevel[] = []
  let cursor = BATCH_POS_START
  for (let i = 0; i < n; i++) {
    const prev = old[i]
    const base = prev
      ? { ...prev, ...defaultLevelParams(i, prev) }
      : { ...defaultLevelParams(i, undefined) }
    if (i < n - 1) {
      // 已有档位：区间原样保留（只夹在新上限内）
      const from = Math.max(cursor, Math.floor(Number(prev?.pos_from) || cursor))
      const to = Math.min(
        limit,
        Math.max(from, Math.floor(Number(prev?.pos_to) || from)),
      )
      out.push({ ...base, pos_from: from, pos_to: to })
      cursor = to + 1
      continue
    }
    // 最后一个档位：接在前面档位之后，一直铺到上限
    const from = prev
      ? Math.max(cursor, Math.floor(Number(prev.pos_from) || cursor))
      : cursor
    const to = Math.max(from, limit)
    out.push({ ...base, pos_from: from, pos_to: to })
  }
  return out
}

function setBatchEnabled(r: EditableRule, enabled: boolean): void {
  r.batch_enabled = enabled
  if (!enabled) return
  if (!r.batch_count) r.batch_count = 3
  if (!r.total_lot_limit || r.total_lot_limit < BATCH_POS_START) r.total_lot_limit = 10
  rebuildBatchLevels(r, batchEditMode())
}

function onBatchMetaChange(r: EditableRule): void {
  if (!r.batch_enabled) return
  rebuildBatchLevels(r, batchEditMode())
}

/**
 * 当前该用哪种档位编辑模式。
 *
 * 原来是「有在跑任务时一律 append」（重新均分会改变后续加仓命中的档位参数，
 * 等于在运行中偷偷改了策略）。现在配置无条件热推，均分出来的参数一样会生效，
 * 所以两种模式都放开——但界面上要说明均分的影响，让用户自己选。
 */
function batchEditMode(): BatchEditMode {
  return 'redistribute'
}

/**
 * 档位区间没覆盖到新的加仓笔数上限时，自动在末尾补档。
 *
 * 补出来的参数沿用最后一档——用户扩容时最常见的意图就是「照着上一档继续加」。
 * 服务端下发快照时也会做同样的补齐，这里的补是为了让界面上看到的就是最终会下发的。
 */
function ensureBatchCoverage(r: EditableRule): boolean {
  if (!r.batch_enabled) return false
  const limit = Math.floor(Number(r.total_lot_limit) || 0)
  if (limit <= 0) return false
  const levels = r.batch_levels
  if (!levels.length) return false
  const last = levels[levels.length - 1]
  const covered = Math.max(
    ...levels.map((lv) => Math.max(1, Math.floor(Number(lv.pos_to) || 1))),
  )
  if (covered >= limit) return false
  // 末尾补档：沿用最后一档的参数，区间接续
  const cursor = Math.max(covered + 1, 1)
  for (let pos = cursor; pos <= limit; pos++) {
    levels.push({
      ...last,
      pos_from: pos,
      pos_to: pos,
    })
  }
  r.batch_count = levels.length
  return true
}

function syncBatchMetaFromLevels(r: EditableRule): void {
  r.batch_count = r.batch_levels.length
  const last = r.batch_levels[r.batch_levels.length - 1]
  if (!last) return
  const to = Math.max(1, Math.floor(Number(last.pos_to) || 1))
  r.total_lot_limit = to
}

function normalizeLevelRange(lv: StrategyBatchLevel): void {
  const from = Math.max(1, Math.floor(Number(lv.pos_from) || 1))
  lv.pos_from = from
  lv.pos_to = Math.max(from, Math.floor(Number(lv.pos_to) || from))
}

function onLevelRangeChange(r: EditableRule, index: number): void {
  const levels = r.batch_levels
  if (!levels[index]) return
  normalizeLevelRange(levels[index])
  for (let i = index; i < levels.length - 1; i++) {
    const nextFrom = levels[i].pos_to + 1
    levels[i + 1].pos_from = nextFrom
    if (levels[i + 1].pos_to < nextFrom) levels[i + 1].pos_to = nextFrom
  }
  for (let i = index; i > 0; i--) {
    const prevTo = levels[i].pos_from - 1
    if (prevTo < levels[i - 1].pos_from) {
      levels[i].pos_from = levels[i - 1].pos_to + 1
      if (levels[i].pos_to < levels[i].pos_from) levels[i].pos_to = levels[i].pos_from
      break
    }
    levels[i - 1].pos_to = prevTo
  }
  syncBatchMetaFromLevels(r)
}

const BATCH_LEVEL_MAX = 50

/**
 * 删除档位：其余档位**不被重新均分**。
 *
 * 语义（shrink_limit）：
 * - 被删档的区间整段丢弃；
 * - 其余档位保持各自的**长度**，整体往前补位，首尾衔接、不留空洞；
 * - 加仓笔数上限收紧到末档终点。
 *
 * 例：1~1 / 2~2 / 3~3 删中间 → 1~1 / 2~2（上限 3 → 2）。
 * 第一批的「加仓次数」不会被改写成 1~2 之类的更大区间——
 * 长度没变，只是后面的档位往前挪。
 *
 * 有在跑任务时沿用原有 append 行为：已开仓命中的档位区间不能动，
 * 只在尾部追加 / 截断。
 */
function removeBatchLevel(r: EditableRule, index: number): void {
  if (r.batch_levels.length <= 1) return
  r.batch_levels.splice(index, 1)
  r.batch_count = r.batch_levels.length
  if (hasRunningTask.value) {
    rebuildBatchLevels(r, 'append')
    return
  }
  // 剩余档位按各自长度重排：从第 1 次加仓起首尾衔接，被删区间整段丢弃
  let cursor = BATCH_POS_START
  for (const lv of r.batch_levels) {
    const len = Math.max(
      1,
      Math.floor(Number(lv.pos_to) || 0) - Math.floor(Number(lv.pos_from) || 0) + 1,
    )
    lv.pos_from = cursor
    lv.pos_to = cursor + len - 1
    cursor = lv.pos_to + 1
  }
  // 加仓笔数上限收紧到末档终点（被删的那一段直接从上限里扣掉）
  const lastTo = r.batch_levels.length
    ? r.batch_levels[r.batch_levels.length - 1].pos_to
    : BATCH_POS_START
  r.total_lot_limit = Math.max(BATCH_POS_START, lastTo)
}

/**
 * 添加档位：已有档位**不被重新均分**，新档接在末尾。
 *
 * 原来这里走 redistribute（把 [1, 上限] 按新档位数重新均分），
 * 结果「加一档」会把前面所有档位的加仓次数区间全部改掉——
 * 和删除档位是同一个问题。这里改为尾部追加：
 * 新档占用末档之后的加仓次数，末档没铺满上限时新档顺带吃掉剩下的空段。
 */
function addBatchLevel(r: EditableRule): void {
  if (r.batch_levels.length >= BATCH_LEVEL_MAX) return
  const nextCount = r.batch_levels.length + 1
  const maxCount = Math.max(
    1,
    Math.floor(Number(r.total_lot_limit) || BATCH_POS_START) - BATCH_POS_START + 1,
  )
  if (nextCount > maxCount) {
    r.total_lot_limit = BATCH_POS_START + nextCount - 1
  }
  const limit = Math.max(BATCH_POS_START, Math.floor(Number(r.total_lot_limit) || BATCH_POS_START))
  r.batch_count = nextCount
  if (hasRunningTask.value) {
    rebuildBatchLevels(r, 'append')
    return
  }
  const prev = r.batch_levels[r.batch_levels.length - 1]
  // 新档接在末档之后；末档没铺满上限时，新档顺带把剩下的空段一起吃掉（不留空洞）
  const from = prev ? Math.floor(Number(prev.pos_to) || 0) + 1 : BATCH_POS_START
  const to = from <= limit ? limit : from
  r.batch_levels.push({
    ...defaultLevelParams(r.batch_levels.length, prev),
    pos_from: from,
    pos_to: to,
  })
  r.total_lot_limit = to
}

function enableManualScatter(r: EditableRule): void {
  if (r.manual_scatter.enabled) return
  r.manual_scatter = {
    ...defaultManualScatter(),
    enabled: true,
  }
}

function clearManualScatter(r: EditableRule): void {
  r.manual_scatter = defaultManualScatter()
}

function onManualVolumeInput(r: EditableRule): void {
  r.manual_scatter.volume_locked = r.manual_scatter.volume > 0
}

function displayedBatchCount(r: EditableRule): number {
  const n = Math.max(0, Math.floor(Number(r.batch_count) || 0))
  return n + (r.manual_scatter.enabled ? 1 : 0)
}

function displayedTotalLot(r: EditableRule): number {
  const n = Math.max(0, Math.floor(Number(r.total_lot_limit) || 0))
  return n + (r.manual_scatter.enabled ? 1 : 0)
}

function onDisplayedBatchCountChange(r: EditableRule, ev: Event): void {
  const minShown = r.manual_scatter.enabled ? 2 : 1
  const raw = Number((ev.target as HTMLInputElement).value)
  const shown = Math.max(minShown, Math.floor(raw) || minShown)
  r.batch_count = r.manual_scatter.enabled ? shown - 1 : shown
  onBatchMetaChange(r)
}

function onDisplayedTotalLotChange(r: EditableRule, ev: Event): void {
  const minShown = r.manual_scatter.enabled ? BATCH_POS_START + 1 : BATCH_POS_START
  const raw = Number((ev.target as HTMLInputElement).value)
  const shown = Math.max(minShown, Math.floor(raw) || minShown)
  r.total_lot_limit = r.manual_scatter.enabled ? shown - 1 : shown
  onBatchMetaChange(r)
}

function manualScatterLevelNo(r: EditableRule): number {
  return (r.batch_enabled ? r.batch_levels.length : 0) + 1
}

function manualScatterPos(r: EditableRule): number {
  if (r.batch_enabled && r.batch_levels.length) {
    let end = 0
    for (const lv of r.batch_levels) {
      end = Math.max(end, Math.floor(Number(lv.pos_to) || 0))
    }
    end = Math.max(end, Math.floor(Number(r.total_lot_limit) || 0))
    return Math.max(1, end + 1)
  }
  return 1
}

function loadRulesFromTemplate(templateId: string): void {
  const tpl = templates.value.find((t) => t.template_id === templateId)
  // 新建时套用前端的默认启用状态：逆势默认打开、顺势默认关闭
  form.rules = tpl ? cloneRules(withDefaultStatus(tpl.rules)) : []
  Object.assign(signalPl, extractSignalPl(withDefaultStatus(tpl?.rules || [])))
  resetSizing()  // 换模版后规则整组换掉，之前的试算结果不再对应任何规则
}

function resetCreateForm(): void {
  editingTemplateName.value = ''
  formError.value = ''
  // presetTemplateId 优先（AI 交易入口锁定 tpl_1），否则默认第一个模版
  runtimeHints.value = null
  form.template_id = props.presetTemplateId || templates.value[0]?.template_id || ''
  form.name = ''
  form.symbol = props.presetSymbol
  loadRulesFromTemplate(form.template_id)
}

function populateEditForm(s: StrategyOut): void {
  editingTemplateName.value = s.template_name || ''
  formError.value = ''
  runtimeHints.value = null
  form.template_id = s.template_id
  form.name = s.name
  form.symbol = s.symbol
  form.rules = cloneRules(s.rules || [])
  Object.assign(signalPl, extractSignalPl(s.rules || []))
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
  // 运行态提示（已开笔数 / 手动仓是否已开）只在详情接口里有，
  // 拉不到就按「没有在跑任务」处理——后端 PATCH 还会再兜底一次。
  try {
    const detail = await hub.fetchStrategyDetail(id)
    runtimeHints.value = detail.runtime_hints || null
  } catch {
    runtimeHints.value = null
  }
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
  if (props.triggerGroup) resetSignalDraft(form.symbol)
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

watch(
  () => signalPl.lot_pl_tiers.batch_count,
  (n) => {
    const next = Math.max(1, Math.min(10, Math.floor(Number(n) || 2)))
    if (next !== n) {
      signalPl.lot_pl_tiers.batch_count = next
      return
    }
    signalPl.lot_pl_tiers.tiers = rebuildSignalLotTiers(next, signalPl.lot_pl_tiers.tiers)
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

const assistNodeTagOptions = computed(() => [
  { value: '', label: '请选择在线节点' },
  ...onlineNodes.value.map((n) => ({ value: n.node_id, label: nodeLabel(n) })),
])

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
    const ms = r.manual_scatter
    if (ms?.enabled) {
      if (!(ms.entry_price > 0) || !(ms.take_profit > 0)) {
        return `${label}：手动分散仓需要填写大于 0 的入场价与止盈价`
      }
      if (ms.entry_price === ms.take_profit) {
        return `${label}：手动分散仓的入场价与止盈价不能相同`
      }
      if (ms.stop_loss < 0) return `${label}：手动分散仓止损价不能为负`
      if (ms.volume < 0) return `${label}：手动分散仓手数不能为负`
    }
    if (!r.batch_enabled) continue
    if (!['all', 'buy', 'sell'].includes(String(r.batch_action || '').toLowerCase())) {
      return `${label}：分批监控方向非法`
    }
    if (!r.batch_levels.length) return `${label}：启用分批加仓后至少需要一个档位`
    for (const lv of r.batch_levels) normalizeLevelRange(lv)
    syncBatchMetaFromLevels(r)
    if (r.batch_count < 1) return `${label}：分批批数至少为 1`
    if (r.batch_levels.length !== r.batch_count) {
      return `${label}：档位数与批数不一致`
    }
    for (const [i, lv] of r.batch_levels.entries()) {
      const at = `${label} 档位 ${i + 1}`
      if (lv.pos_from < 1) return `${at}：加仓笔数起点至少为 1`
      if (lv.pos_to < lv.pos_from) return `${at}：加仓笔数终点不能小于起点`
      if (!calcTypeOptions(r.type).some((o) => o.value === lv.calc_type)) {
        return `${at}：计算方式非法`
      }
      if (!isSmcCalc(lv.calc_type) && lv.calc_type === 'point' && !(lv.point > 0)) {
        return `${at}：点数需大于 0`
      }
      if (lv.calc_type === 'price' && !(lv.price > 0)) return `${at}：请填写指定价位`
      if (isSmcCalc(lv.calc_type) && r.type !== RULE_TYPE_COUNTER) {
        return `${at}：顺势加仓不支持 FVG / OB / PDZ / FIBO`
      }
      if (
        (isBarCalc(lv.calc_type) || isSmcCalc(lv.calc_type))
        && !TIMEFRAME_OPTIONS.includes(lv.timeframe)
      ) {
        return `${at}：请选择 K 线周期`
      }
      if (lv.point < 0) return `${at}：点数不能为负`
      if (lv.lot_times < 0) return `${at}：倍数不能为负`
      if (lv.extra_lot < 0) return `${at}：手数不能为负`
      // 波幅纳入当前 K 线：由表单自动决定并固定参数，这里只兜底检查一致性
      if (lv.range_include_live && !shouldAutoRangeLive(r, lv)) {
        return `${at}：只有逆势加仓的波幅档位才能纳入当前 K 线波幅`
      }
    }
  }
  return null
}

function validateSignalPl(cfg: SignalPlForm): string | null {
  const r = cfg.float_pl_ratio
  if (!Number.isFinite(r.ratio) || r.ratio === 0) return '信号盈亏比比例不能为 0'
  const lpt = cfg.lot_pl_tiers
  if (!['all', 'buy', 'sell'].includes(lpt.close_action)) return '信号分档平仓动作无效'
  if (!lpt.tiers.length) return '信号分档平仓至少需要 1 个批次'
  for (const [i, tier] of lpt.tiers.entries()) {
    const at = `信号分档批次#${i + 1}`
    if (!Number.isFinite(tier.min_lot) || tier.min_lot < 0) return `${at}手数不能为负`
    if (!Number.isFinite(tier.pl_amount) || tier.pl_amount === 0) return `${at}盈亏金额不能为 0`
  }
  return null
}

function formatSaveError(e: unknown, fallback: string): string {
  const err = e as {
    code?: string
    message?: string
    response?: { data?: { detail?: unknown } }
  }
  if (!err.response) {
    if (err.code === 'ECONNABORTED' || /timeout/i.test(err.message || '')) {
      return '请求超时，请刷新列表确认是否已保存成功'
    }
    return fallback
  }
  const detail = err.response.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object' && 'msg' in item) {
        return String((item as { msg: unknown }).msg || '')
      }
      return ''
    }).filter(Boolean)
    if (parts.length) return parts.join('；')
  }
  // 运行态校验的结构化错误（limit_below_added / sltp_crossed / batch_inconsistent）：
  // 直接把后端给的 message 念出来
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = String((detail as { message: unknown }).message || '').trim()
    if (message) return message
  }
  return fallback
}

function showSaveError(msg: string, type: 'warning' | 'error' = 'warning'): void {
  formError.value = msg
  ElMessage({ type, message: msg, showClose: true, duration: 5000 })
}

interface PreparedStrategy {
  name: string
  symbol: string
  rules: StrategyRule[]
}

/**
 * 收集保存前的提醒（不阻止保存）。
 *
 * 配置无条件热推之后，这些情况不再是「非法修改」，只是「保存后会发生什么」：
 * - 上限低于已开笔数 → 本任务不再新增批次，现有仓位继续由止盈止损管理
 * - 档位没覆盖到新的上限 → 自动补档（沿用最后一档参数）
 * - 重切了档位 → 后续加仓命中的参数会变，已开仓位不受影响
 */
function collectWarnings(rules: EditableRule[]): string[] {
  const out: string[] = []
  for (const r of rules) {
    const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
    if (isRiskSized(r) || isGrid(r)) continue
    const floor = limitFloor(r.type)
    const limit = Math.floor(Number(r.total_lot_limit) || 0)
    if (floor > 0 && limit > 0 && limit < floor) {
      out.push(
        `${label}：已开 ${floor} 笔，新上限 ${limit} 笔低于已开笔数；` +
          '保存后本任务不再新增批次，现有仓位继续由止盈止损管理（不平仓）。',
      )
    }
    if (r.batch_enabled) {
      const levels = r.batch_levels || []
      if (levels.length && limit > 0) {
        const covered = Math.max(
          ...levels.map((lv) => Math.max(1, Math.floor(Number(lv.pos_to) || 1))),
        )
        if (covered < limit) {
          out.push(
            `${label}：档位只覆盖到第 ${covered} 次加仓，新上限 ${limit} 笔；` +
              '已自动补档（沿用最后一档的参数），保存后节点会按补出来的档位继续加。',
          )
        }
      }
      // 指定价档位：缺止盈参考价就算不出手数，节点不会挂这一档的单。
      // 这里只提示不拦保存（保存路径上不再有拦截），由用户决定要不要补。
      levels.forEach((lv, i) => {
        if (lv.calc_type !== 'price') return
        if (!(lv.price > 0)) return
        if (!(Number(lv.price_tp) > 0)) {
          out.push(
            `${label} 档位 ${i + 1}：指定价档位没有填止盈参考价，` +
              '节点无法反推手数，这一档不会挂单（其它档位照常）。',
          )
        }
      })
    }
  }
  return out
}

function prepareStrategy(): PreparedStrategy | null {
  if (!form.template_id) {
    showSaveError('请选择策略模版')
    return null
  }
  const name = form.name.trim()
  if (!name) {
    showSaveError('请填写策略名称')
    return null
  }
  const symbol = form.symbol.trim().toUpperCase()
  if (!symbol) {
    showSaveError('请填写绑定品种')
    return null
  }
  // 先就地归一波幅自适应（逆势 + 波幅 + 非 M1 恒为「纳入实时」，参数固定 1000 / 5），
// 这样校验与界面看到的都是同一份最终值
  normalizeRangeLive(form.rules)
  const rulesErr = validateRules(form.rules)
  if (rulesErr) {
    showSaveError(rulesErr)
    return null
  }
  // 档位没覆盖到新的上限 → 自动补档（不阻止保存，补完让用户看得见）
  for (const r of form.rules) {
    if (isRiskSized(r) || isGrid(r)) continue
    ensureBatchCoverage(r)
  }
  formWarnings.value = collectWarnings(form.rules)
  if (isTpl1.value) {
    const plErr = validateSignalPl(signalPl)
    if (plErr) {
      showSaveError(plErr)
      return null
    }
  }
  const cloned = cloneRules(form.rules)
  const rules = isTpl1.value
    ? applySignalPlToRules(cloned, signalPl)
    : cloned
  return { name, symbol, rules }
}

async function writeStrategy(
  prepared: PreparedStrategy,
): Promise<{ strategyId: string; name: string; symbol: string; templateId: string } | null> {
  const actionLabel = isEditMode.value ? '保存' : '创建'
  try {
    if (isEditMode.value) {
      const sid = props.strategyId || ''
      await hub.updateStrategy(
        sid,
        { name: prepared.name, symbol: prepared.symbol, rules: prepared.rules },
        props.listSearchOptions,
      )
      return {
        strategyId: sid,
        name: prepared.name,
        symbol: prepared.symbol,
        templateId: form.template_id,
      }
    } else {
      const created = await hub.createStrategy(
        {
          template_id: form.template_id,
          name: prepared.name,
          symbol: prepared.symbol,
          rules: prepared.rules,
        },
        props.listSearchOptions,
      )
      return {
        strategyId: created.strategy_id,
        name: prepared.name,
        symbol: prepared.symbol,
        templateId: form.template_id,
      }
    }
  } catch (e: unknown) {
    showSaveError(formatSaveError(e, `${actionLabel}失败，请稍后重试`), 'error')
    return null
  }
}

async function save(): Promise<void> {
  const prepared = prepareStrategy()
  if (!prepared) return
  saving.value = true
  formError.value = ''
  try {
    const tplName = formTemplateLabel.value
  const enabledCount = form.rules.filter((r) => r.status === 1).length
    const actionLabel = isEditMode.value ? '保存' : '创建'
    if (
      !(await confirmAction(
        `确认${actionLabel}策略「${prepared.name}」？\n\n模版：${tplName}\n绑定品种：${prepared.symbol}\n启用规则：${enabledCount} / ${form.rules.length}`,
      ))
    ) {
      return
    }
    const written = await writeStrategy(prepared)
    if (!written) return
    emit('saved', written)
    close()
  } finally {
    saving.value = false
  }
}

function signalDraftError(): string | null {
  if (!signalDraft.symbol.trim()) return '请填写信号品种'
  if (signalDraft.action !== 'CLOSE' && !(Number(signalDraft.volume) > 0)) {
    return '开仓信号必须填写大于 0 的手数'
  }
  return null
}

function reportGroupTrigger(payload: ManualSignalPayload, res: ManualSignalResult): void {
  const head = `${payload.action} ${payload.symbol}`
  if (res.status === 'accepted') {
    const groups = res.groups ?? 0
    const targets = res.targets ?? 0
    if (payload.action === 'CLOSE') {
      if (targets > 0) {
        ElMessage.success(`已下发终止指令：命中 ${groups} 个分组，${targets} 个节点任务开始平仓`)
      } else {
        ElMessage.warning(`命中 ${groups} 个分组，但没有进行中的策略任务需要终止`)
      }
      return
    }
    const detail = `命中 ${groups} 个分组，${targets} 个节点收到下发`
    if (targets > 0) {
      ElMessage.success(`已触发 ${head}：${detail}`)
      return
    }
    const taskRows = Array.isArray(res.tasks) ? res.tasks : []
    const reasons = [
      ...new Set(taskRows.map((t) => (t.reason || '').trim()).filter(Boolean)),
    ]
    const hint = reasons.length ? reasons.join('；') : '分组无有效节点，或未能下发到任何节点'
    ElMessage.warning(`${head} 已受理但未下发：${detail}（${hint}）`)
    return
  }
  if (res.status === 'duplicate') {
    ElMessage.warning(`重复信号被抑制：5 秒内已有相同参数的 ${head} 策略信号`)
    return
  }
  if (res.status === 'rejected') {
    showSaveError(res.reason || '信号被拒收')
    return
  }
  ElMessage.info(`已提交：${res.status}`)
}

async function triggerBoundGroup(): Promise<void> {
  const group = props.triggerGroup
  if (!group || !isEditMode.value) return
  const draftErr = signalDraftError()
  if (draftErr) {
    showSaveError(draftErr)
    return
  }
  const prepared = prepareStrategy()
  if (!prepared) return
  const signalSymbol = signalDraft.symbol.trim().toUpperCase()
  if (!signalSymbolMatches(prepared.symbol, signalSymbol)) {
    showSaveError(`信号品种 ${signalSymbol} 与策略绑定品种 ${prepared.symbol} 不一致，该分组不会收到信号`)
    return
  }
  const extra = signalDraft.action === 'CLOSE'
    ? '说明：终止该分组进行中的策略任务并平仓'
    : `手数：${signalDraft.volume}`
  if (
    !(await confirmAction(
      `确认向分组「${group.name}」触发 ${signalDraft.action} ${signalSymbol}？\n\n将先保存当前策略，再只向该分组发送信号。\n${extra}`,
      '确认触发信号',
    ))
  ) {
    return
  }
  saving.value = true
  formError.value = ''
  try {
    if (!(await writeStrategy(prepared))) return
    emit('saved')
    const payload: ManualSignalPayload = {
      symbol: signalSymbol,
      action: signalDraft.action,
      model: 'strategy',
      group_ids: [group.group_id],
    }
    if (signalDraft.action !== 'CLOSE') {
      payload.volume = Number(signalDraft.volume)
      if (signalDraft.stop_loss) payload.stop_loss = signalDraft.stop_loss
      if (signalDraft.take_profit) payload.take_profit = signalDraft.take_profit
      if (signalDraft.entry_price) payload.entry_price = signalDraft.entry_price
      const comment = signalDraft.comment.trim()
      if (comment) payload.comment = comment
    }
    try {
      const res = await hub.triggerManualSignal(payload)
      reportGroupTrigger(payload, res)
    } catch (e: unknown) {
      showSaveError(formatSaveError(e, '策略已保存，但触发信号失败，请稍后重试'), 'error')
    }
  } finally {
    saving.value = false
  }
}

function resetRuleToTemplate(idx: number): void {
  const tplRule = selectedTemplate.value?.rules?.[idx]
  if (!tplRule || !form.rules[idx]) return
  Object.assign(form.rules[idx], cloneRules(withDefaultStatus([tplRule]))[0])
  delete sizing[idx]
}
</script>

<template>
  <div v-if="modelValue" class="modal-mask">
    <div class="card modal modal-lg strategy-form-modal">
      <div class="modal-header card-pad" style="padding-bottom: 0">
        <div class="h1">{{ isEditMode ? '编辑策略' : '新增策略' }}</div>
        <!-- <p class="muted" style="font-size: 12px; margin: 4px 0 0">
          {{
            isEditMode
              ? '可修改名称、绑定品种与规则参数；模版创建后不可更换'
              : '选择策略模版 → 填写名称 / 品种 → 自定义规则参数后创建'
          }}
        </p> -->
      </div>

      <div class="modal-body card-pad" style="padding-top: 16px">
        <div v-if="triggerGroup" class="strategy-signal-block">
          <div class="strategy-signal-head">
            <strong>触发信号</strong>
            <span class="muted">只发给分组「{{ triggerGroup.name }}」。点击底部「触发信号」会先保存当前策略。</span>
          </div>
          <StrategySignalFields v-model="signalDraft" id-prefix="bound-signal" />
        </div>
        <div class="form-grid">
          <div class="field">
            <FormLabel field-id="strategy-template" text="策略模版" :help="FIELD_HELP.template" />
            <TagSelect
              id="strategy-template"
              v-model="form.template_id"
              :options="templateTagOptions"
              :disabled="isEditMode || !!presetTemplateId"
              aria-label="策略模版"
            />
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
              :readonly="presetSymbolLocked"
              style="text-transform: uppercase"
            />
          </div>
        </div>

        <div v-if="isTpl1" class="signal-pl-editor">
          <div class="rules-editor-head">
            <FormLabel text="信号盈亏控制" :help="FIELD_HELP.signal_pl" />
            <!-- <span class="muted" style="font-size: 12px">每个信号独立监控，触发只平该信号仓</span> -->
            <span class="muted" style="font-size: 12px">止盈按分档金额，止损按盈亏比</span>
          </div>
          <div class="signal-pl-rules">
            <div class="signal-pl-card">
              <div class="signal-pl-head">
                <label class="signal-pl-switch">
                  <input v-model="signalPl.float_pl_ratio.enabled" type="checkbox" />
                  <span>信号盈亏比</span>
                </label>
                <span v-if="signalPl.float_pl_ratio.enabled" class="muted signal-pl-count">
                  {{ signalPl.float_pl_ratio.monitor_mode === 'times'
                    ? `触发 ${signalPl.float_pl_ratio.max_times} 次后停止`
                    : '循环监控' }}
                </span>
              </div>
              <div v-if="signalPl.float_pl_ratio.enabled" class="signal-pl-fields">
                <div class="field">
                  <FormLabel text="盈亏比例 %" :help="FIELD_HELP.signal_float_pl" />
                  <input
                    v-model.number="signalPl.float_pl_ratio.ratio"
                    type="number"
                    step="0.1"
                    class="signal-pl-num"
                    aria-label="信号盈亏比例"
                  />
                </div>
                <div class="field">
                  <FormLabel text="监控方式" :help="FIELD_HELP.signal_float_pl" />
                  <TagSelect
                    v-model="signalPl.float_pl_ratio.monitor_mode"
                    :options="MONITOR_MODE_TAGS"
                    aria-label="监控方式"
                  />
                </div>
                <div v-if="signalPl.float_pl_ratio.monitor_mode === 'times'" class="field">
                  <FormLabel text="指定次数" :help="FIELD_HELP.signal_float_pl" />
                  <input
                    v-model.number="signalPl.float_pl_ratio.max_times"
                    type="number"
                    min="1"
                    class="signal-pl-times"
                    aria-label="指定次数"
                  />
                </div>
              </div>
              <p v-else class="signal-pl-off">
                关闭状态：该信号不按盈亏比自动平仓，下方「分档手数盈亏」仍独立生效
              </p>
            </div>

            <div class="signal-pl-card">
              <div class="signal-pl-head">
                <label class="signal-pl-switch">
                  <input v-model="signalPl.lot_pl_tiers.enabled" type="checkbox" />
                  <span>分档手数盈亏</span>
                </label>
                <span v-if="signalPl.lot_pl_tiers.enabled" class="muted signal-pl-count">
                  {{ signalPl.lot_pl_tiers.batch_count }} 档 · {{ signalPl.lot_pl_tiers.tiers.length }} 条
                </span>
              </div>
              <template v-if="signalPl.lot_pl_tiers.enabled">
                <div class="signal-pl-fields">
                  <div class="field">
                    <FormLabel text="批次数" :help="FIELD_HELP.signal_lot_pl" />
                    <input
                      v-model.number="signalPl.lot_pl_tiers.batch_count"
                      type="number"
                      min="1"
                      max="10"
                      class="signal-pl-times"
                      aria-label="批次数"
                    />
                  </div>
                  <div class="field">
                    <FormLabel text="平仓方向" :help="FIELD_HELP.signal_lot_pl" />
                    <TagSelect
                      v-model="signalPl.lot_pl_tiers.close_action"
                      :options="SIDE_ACTION_TAGS"
                      aria-label="平仓方向"
                    />
                  </div>
                </div>

                <div class="signal-pl-tiers">
                  <div v-for="(tier, idx) in signalPl.lot_pl_tiers.tiers" :key="idx" class="signal-pl-tier">
                    <span class="signal-pl-tier-no">批次 {{ idx + 1 }}</span>
                    <div class="field">
                      <FormLabel text="总 lot >=" :help="FIELD_HELP.signal_lot_pl" />
                      <input
                        v-model.number="tier.min_lot"
                        type="number"
                        min="0"
                        step="0.01"
                        class="signal-pl-num"
                        :aria-label="`批次${idx + 1} 总手数阈值`"
                      />
                    </div>
                    <div class="field">
                      <FormLabel text="盈亏金额 >=" :help="FIELD_HELP.signal_lot_pl" />
                      <input
                        v-model.number="tier.pl_amount"
                        type="number"
                        step="1"
                        class="signal-pl-num"
                        :aria-label="`批次${idx + 1} 盈亏金额阈值`"
                      />
                    </div>
                  </div>
                  <p class="signal-pl-note">
                    按所选方向统计该信号总手数与浮盈亏；负的盈亏金额表示亏损侧达阈值。优先匹配更高批次。
                  </p>
                </div>
              </template>
              <p v-else class="signal-pl-off">
                关闭状态：不按手数分批平仓，上方「信号盈亏比」仍独立生效
              </p>
            </div>
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
                  <TagSelect :id="`rule-${idx}-action`" v-model="r.action" :options="SIDE_TAGS" aria-label="监控方向" />
                </div>
                <div class="field">
                  <FormLabel
                    :field-id="`rule-${idx}-entry-mode`"
                    text="开仓方式"
                    :help="FIELD_HELP.entry_mode"
                  />
                  <TagSelect
                    :id="`rule-${idx}-entry-mode`"
                    v-model="r.entry_mode"
                    :options="ENTRY_MODE_OPTIONS"
                    aria-label="开仓方式"
                  />
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
                    <TagSelect
                      :id="`rule-${idx}-breakeven-mode`"
                      v-model="r.breakeven_mode"
                      :options="BREAKEVEN_MODE_OPTIONS"
                      aria-label="保本方式"
                    />
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
                  <TagSelect :id="`rule-${idx}-grid-mode`" v-model="r.grid_mode" :options="GRID_MODE_OPTIONS" aria-label="网格模式" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-grid-side`" text="网格方向" :help="FIELD_HELP.grid_side" />
                  <TagSelect :id="`rule-${idx}-grid-side`" v-model="r.grid_side" :options="GRID_SIDE_OPTIONS" aria-label="网格方向" />
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
                      <TagSelect
                        :id="`rule-${idx}-assist-node`"
                        v-model="sizingState(idx).nodeId"
                        :options="assistNodeTagOptions"
                        aria-label="行情源节点"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-assist-tf`"
                        text="K线周期"
                        :help="FIELD_HELP.assist_timeframe"
                      />
                      <TagSelect
                        :id="`rule-${idx}-assist-tf`"
                        v-model="r.assist_timeframe"
                        :options="TIMEFRAME_TAGS"
                        aria-label="K线周期"
                      />
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
                  <div class="batch-head-actions">
                    <span v-if="r.batch_enabled" class="muted batch-count-hint">
                      共 {{ displayedBatchCount(r) }} 批
                      <template v-if="r.manual_scatter.enabled">
                        （分散仓 {{ r.batch_count }} · 手动分散仓 1）
                      </template>
                    </span>
                    <button
                      v-if="!r.batch_enabled"
                      type="button"
                      class="btn-sm btn-ghost"
                      :disabled="r.manual_scatter.enabled || manualScatterLocked(r.type)"
                      @click="enableManualScatter(r)"
                    >
                      手动单
                    </button>
                  </div>
                </div>

                <template v-if="r.batch_enabled">
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-action`"
                        text="分批方向"
                        :help="FIELD_HELP.batch_action"
                      />
                      <TagSelect
                        :id="`rule-${idx}-batch-action`"
                        v-model="r.batch_action"
                        :options="SIDE_TAGS"
                        aria-label="分批方向"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-count`"
                        :text="r.manual_scatter.enabled ? '总批数' : '批数'"
                        :help="FIELD_HELP.batch_count"
                      />
                      <input
                        :id="`rule-${idx}-batch-count`"
                        :value="displayedBatchCount(r)"
                        type="number"
                        :min="r.manual_scatter.enabled ? 2 : 1"
                        step="1"
                        @change="onDisplayedBatchCountChange(r, $event)"
                      />
                      <p v-if="r.manual_scatter.enabled" class="muted batch-split-hint">
                        分散仓 {{ r.batch_count }} · 手动分散仓 1
                      </p>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-total-lot`"
                        text="加仓笔数"
                        :help="FIELD_HELP.total_lot_limit"
                      />
                      <input
                        :id="`rule-${idx}-total-lot`"
                        :value="displayedTotalLot(r)"
                        type="number"
                        :min="r.manual_scatter.enabled ? BATCH_POS_START + 1 : BATCH_POS_START"
                        step="1"
                        @change="onDisplayedTotalLotChange(r, $event)"
                      />
                      <p v-if="r.manual_scatter.enabled" class="muted batch-split-hint">
                        分散仓加仓笔数 {{ Math.floor(r.total_lot_limit) }} · 手动分散仓 1
                      </p>
                      <!-- 已开笔数提示：上限可以改到已开笔数以下，只是那样就不再新增批次 -->
                      <p v-if="limitFloor(r.type) > 0" class="muted batch-split-hint">
                        已开 {{ limitFloor(r.type) }} 笔，
                        <template v-if="Math.floor(r.total_lot_limit) > limitFloor(r.type)">
                          还能再开
                          {{ Math.floor(r.total_lot_limit) - limitFloor(r.type) }} 笔
                        </template>
                        <template v-else>保存后本任务不再新增批次，现有仓位继续由止盈止损管理</template>
                      </p>
                    </div>
                  </div>

                  <div class="batch-levels">
                    <div v-for="(lv, li) in r.batch_levels" :key="li" class="batch-level">
                      <span class="batch-level-no">
                        <span class="lv-mark">└{{ li + 1 }}</span>
                        <span class="lv-name">第 {{ li + 1 }} 档</span>
                        <span class="lv-sub">{{ lv.pos_from }}~{{ lv.pos_to }} 次</span>
                      </span>
                      <div class="field batch-range">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-from`"
                          text="加仓次数"
                          :help="FIELD_HELP.batch_level"
                        />
                        <div class="batch-range-inputs">
                          <input
                            :id="`rule-${idx}-lv-${li}-from`"
                            v-model.number="lv.pos_from"
                            type="number"
                            min="1"
                            step="1"
                            aria-label="加仓次数起点"
                            @change="onLevelRangeChange(r, li)"
                          />
                          <span class="muted">~</span>
                          <input
                            :id="`rule-${idx}-lv-${li}-to`"
                            v-model.number="lv.pos_to"
                            type="number"
                            min="1"
                            step="1"
                            aria-label="加仓次数终点"
                            @change="onLevelRangeChange(r, li)"
                          />
                        </div>
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-calc`"
                          text="计算方式"
                          :help="FIELD_HELP.calc_type"
                        />
                        <TagSelect
                          :id="`rule-${idx}-lv-${li}-calc`"
                          v-model="lv.calc_type"
                          :options="calcTypeOptions(r.type)"
                          aria-label="计算方式"
                          @change="onCalcTypeChange(r, lv)"
                        />
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
                      <div v-if="lv.calc_type === 'price'" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-price-tp`"
                          text="止盈参考价"
                          :help="FIELD_HELP.batch_price_tp"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-price-tp`"
                          v-model.number="lv.price_tp"
                          type="number"
                          min="0"
                          step="any"
                        />
                      </div>
                      <div v-if="lv.calc_type === 'price'" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-price-sl`"
                          text="自有止损"
                          :help="FIELD_HELP.batch_price_sl"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-price-sl`"
                          v-model.number="lv.price_sl"
                          type="number"
                          min="0"
                          step="any"
                        />
                      </div>
                      <div v-else-if="isBarCalc(lv.calc_type) || isSmcCalc(lv.calc_type)" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-tf`"
                          text="K 线周期"
                          :help="isSmcCalc(lv.calc_type) ? FIELD_HELP.batch_smc_timeframe : FIELD_HELP.batch_timeframe"
                        />
                        <TagSelect
                          :id="`rule-${idx}-lv-${li}-tf`"
                          v-model="lv.timeframe"
                          :options="TIMEFRAME_TAGS"
                          aria-label="K 线周期"
                          @change="onTimeframeChange(r, lv)"
                        />
                      </div>
                      
                      <div
                        v-if="
                          lv.calc_type !== 'price'
                          && !isBarCalc(lv.calc_type)
                          && !isSmcCalc(lv.calc_type)
                        "
                        class="field"
                      >
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
                      <div class="batch-level-actions">
                        <button
                          type="button"
                          class="btn-sm btn-ghost"
                          :disabled="r.batch_levels.length <= 1"
                          @click="removeBatchLevel(r, li)"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                    <div
                      v-if="r.manual_scatter.enabled"
                      class="batch-level is-manual"
                      :class="{ 'is-locked': manualScatterLocked(r.type) }"
                    >
                      <span class="batch-level-no">
                        <span class="lv-mark">└{{ manualScatterLevelNo(r) }}</span>
                        <span class="lv-name">手动分散仓</span>
                        <span class="lv-sub">第 {{ manualScatterPos(r) }} 笔</span>
                      </span>
                      <!-- 已成交：整块配置锁死，只读展示，不能再改 -->
                      <p v-if="manualScatterLocked(r.type)" class="locked-hint">
                        手动分散仓已成交，配置已锁定不可修改
                      </p>
                      <!-- 挂单在盘上：还能改，保存后节点撤旧单重算重挂 -->
                      <p v-else-if="manualScatterPending(r.type)" class="locked-hint is-editable">
                        手动分散仓挂单在盘上，入场价 / 止盈 / 止损仍可修改；
                        保存后节点会撤掉旧单、按新参数重算手数并重新挂单。
                        删除手动单并保存，则会撤掉盘上这张挂单，本次不再入场
                      </p>
                      <!-- 挂单被撤销：需要客户重新敲数字 -->
                      <p v-else-if="manualScatterVoid(r.type)" class="locked-hint">
                        {{ manualScatterVoidHint(r.type) }}
                      </p>
                      <div class="field batch-range">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-pos`"
                          text="持仓笔数"
                          :help="FIELD_HELP.manual_scatter"
                        />
                        <div class="batch-range-inputs">
                          <input
                            :id="`rule-${idx}-ms-pos`"
                            type="number"
                            :value="manualScatterPos(r)"
                            disabled
                            aria-label="手动分散仓持仓笔数（仅展示）"
                          />
                          <span class="muted">~</span>
                          <input
                            type="number"
                            :value="manualScatterPos(r)"
                            disabled
                            aria-label="手动分散仓持仓笔数终点（仅展示）"
                          />
                        </div>
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-entry`"
                          text="入场价"
                          :help="FIELD_HELP.manual_entry"
                        />
                        <input
                          :id="`rule-${idx}-ms-entry`"
                          v-model.number="r.manual_scatter.entry_price"
                          type="number"
                          min="0"
                          step="any"
                          :disabled="manualScatterLocked(r.type)"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-tp`"
                          text="止盈价"
                          :help="FIELD_HELP.manual_tp"
                        />
                        <input
                          :id="`rule-${idx}-ms-tp`"
                          v-model.number="r.manual_scatter.take_profit"
                          type="number"
                          min="0"
                          step="any"
                          :disabled="manualScatterLocked(r.type)"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-sl`"
                          text="止损价"
                          :help="FIELD_HELP.manual_sl"
                        />
                        <input
                          :id="`rule-${idx}-ms-sl`"
                          v-model.number="r.manual_scatter.stop_loss"
                          type="number"
                          min="0"
                          step="any"
                          :disabled="manualScatterLocked(r.type)"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-vol`"
                          text="手数"
                          :help="FIELD_HELP.manual_volume"
                        />
                        <input
                          :id="`rule-${idx}-ms-vol`"
                          v-model.number="r.manual_scatter.volume"
                          type="number"
                          min="0"
                          step="0.01"
                          placeholder="0=自动"
                          :disabled="manualScatterLocked(r.type)"
                          @change="onManualVolumeInput(r)"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-ms-tp-link`"
                          text="止盈联动"
                          :help="FIELD_HELP.manual_close_all_on_tp"
                        />
                        <label class="manual-link-switch">
                          <input
                            :id="`rule-${idx}-ms-tp-link`"
                            v-model="r.manual_scatter.close_all_on_tp"
                            type="checkbox"
                            :disabled="manualScatterLocked(r.type)"
                          />
                          <span>止盈后清仓该信号</span>
                        </label>
                      </div>
                      <div class="batch-level-actions">
                        <button
                          type="button"
                          class="btn-sm btn-ghost"
                          :disabled="manualScatterLocked(r.type)"
                          @click="clearManualScatter(r)"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  </div>
                  <div class="batch-level-add">
                    <button
                      type="button"
                      class="btn-sm btn-ghost"
                      :disabled="r.batch_levels.length >= BATCH_LEVEL_MAX"
                      @click="addBatchLevel(r)"
                    >
                      + 添加档位
                    </button>
                    <button
                      type="button"
                      class="btn-sm btn-ghost"
                      :disabled="r.manual_scatter.enabled"
                      @click="enableManualScatter(r)"
                    >
                      手动单
                    </button>
                  </div>
                  <!-- <p class="rule-hint">
                    改批数或加仓笔数会按第 {{ BATCH_POS_START }} 次 ~ 第 {{ Math.floor(r.total_lot_limit) }} 次加仓重切区间；
                    添加档位接在末尾、删除档位只丢弃该档区间并收紧上限，已有档位的区间不会被重切；
                    手改某一档加仓次数时，相邻档会首尾衔接。
                    每档间距可独立选点数 / 指定价 / ATR / 波幅，ATR 与波幅取该周期最近 {{ BATCH_BAR_PERIOD }} 根已收盘 K 线
                    <template v-if="r.manual_scatter.enabled">
                      ；手动分散仓展示为第 {{ manualScatterPos(r) }} 笔，不参与切档
                  </template>
                  </p> -->
                </template>
                <div v-else-if="r.manual_scatter.enabled" class="batch-levels">
                  <div class="batch-level is-manual">
                    <span class="batch-level-no">
                      <span class="lv-mark">└{{ manualScatterLevelNo(r) }}</span>
                      <span class="lv-name">手动分散仓</span>
                      <span class="lv-sub">第 {{ manualScatterPos(r) }} 笔</span>
                    </span>
                    <div class="field batch-range">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-pos`"
                        text="持仓笔数"
                        :help="FIELD_HELP.manual_scatter"
                      />
                      <div class="batch-range-inputs">
                        <input
                          :id="`rule-${idx}-ms-pos`"
                          type="number"
                          :value="manualScatterPos(r)"
                          disabled
                          aria-label="手动分散仓持仓笔数（仅展示）"
                        />
                        <span class="muted">~</span>
                        <input
                          type="number"
                          :value="manualScatterPos(r)"
                          disabled
                          aria-label="手动分散仓持仓笔数终点（仅展示）"
                        />
                      </div>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-entry`"
                        text="入场价"
                        :help="FIELD_HELP.manual_entry"
                      />
                      <input
                        :id="`rule-${idx}-ms-entry`"
                        v-model.number="r.manual_scatter.entry_price"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-tp`"
                        text="止盈价"
                        :help="FIELD_HELP.manual_tp"
                      />
                      <input
                        :id="`rule-${idx}-ms-tp`"
                        v-model.number="r.manual_scatter.take_profit"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-sl`"
                        text="止损价"
                        :help="FIELD_HELP.manual_sl"
                      />
                      <input
                        :id="`rule-${idx}-ms-sl`"
                        v-model.number="r.manual_scatter.stop_loss"
                        type="number"
                        min="0"
                        step="any"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-vol`"
                        text="手数"
                        :help="FIELD_HELP.manual_volume"
                      />
                      <input
                        :id="`rule-${idx}-ms-vol`"
                        v-model.number="r.manual_scatter.volume"
                        type="number"
                        min="0"
                        step="0.01"
                        placeholder="0=自动"
                        @change="onManualVolumeInput(r)"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-ms-tp-link`"
                        text="止盈联动"
                        :help="FIELD_HELP.manual_close_all_on_tp"
                      />
                      <label class="manual-link-switch">
                        <input
                          :id="`rule-${idx}-ms-tp-link`"
                          v-model="r.manual_scatter.close_all_on_tp"
                          type="checkbox"
                        />
                        <span>止盈后清仓该信号</span>
                      </label>
                    </div>
                    <div class="batch-level-actions">
                      <button type="button" class="btn-sm btn-ghost" @click="clearManualScatter(r)">
                        删除
                      </button>
                    </div>
                  </div>
                </div>
              </div>
              <div class="rule-grid">
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-action`" text="监控方向" :help="FIELD_HELP.action" />
                  <TagSelect :id="`rule-${idx}-action`" v-model="r.action" :options="SIDE_TAGS" aria-label="监控方向" />
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
            </template>
            </div>
          </div>
        </div>

        <p v-if="formError" class="form-error" style="margin-top: 10px">{{ formError }}</p>
        <div v-if="isTpl1LockedForCurrentUser" class="form-error" style="margin-top: 10px">
          AI 智能加仓策略需管理员授权后才能使用，当前角色未获授权
        </div>

        <!-- 保存提醒：不阻止保存，只说明「保存后会发生什么」 -->
        <div v-if="formWarnings.length" class="form-warnings" style="margin-top: 10px">
          <p class="form-warnings-title">保存提醒</p>
          <ul class="form-warnings-list">
            <li v-for="(w, i) in formWarnings" :key="i">{{ w }}</li>
          </ul>
        </div>

        <!-- 运行态提示：有在跑任务时，配置改动会立刻热推到节点 -->
        <div v-if="runtimeBanner" class="runtime-banner">
          <p class="runtime-banner-title">{{ runtimeBanner.title }}</p>
          <ul class="runtime-banner-list">
            <li v-for="(line, i) in runtimeBanner.lines" :key="i">{{ line }}</li>
          </ul>
        </div>
      </div>

      <div class="modal-footer card-pad" style="padding-top: 0">
        <span></span>
        <div class="row" style="gap: 8px">
          <button class="btn-ghost" :disabled="saving" @click="close">取消</button>
          <button
            v-if="triggerGroup"
            type="button"
            class="btn-success"
            :disabled="saving"
            @click="triggerBoundGroup"
          >
            {{ saving ? '处理中…' : '触发信号' }}
          </button>
          <button class="btn-primary" :disabled="saving || isTpl1LockedForCurrentUser" @click="save">
            {{ saving ? '保存中…' : isTpl1LockedForCurrentUser ? '需 AI 策略权限' : isEditMode ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.strategy-signal-block {
  margin-bottom: 18px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--glass-border);
}

.strategy-signal-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 12px;
  margin-bottom: 12px;
}

.strategy-signal-head .muted {
  font-size: 12px;
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

.rule-panel-body > .batch-block:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.batch-block + .rule-grid {
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

.batch-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.batch-count-hint {
  font-size: 12px;
}

.batch-split-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.4;
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
  grid-template-columns: 28px minmax(132px, 1.1fr) repeat(4, minmax(0, 1fr)) auto;
  gap: 10px;
  align-items: end;
}

/* 手动分散仓比普通档位多一列「止盈联动」 */
.batch-level.is-manual {
  grid-template-columns: 28px minmax(132px, 1.1fr) repeat(4, minmax(0, 1fr)) minmax(0, 1.2fr) auto;
}

/* 手动分散仓已开仓：整块只读，输入框禁用 + 灰底提示 */
.batch-level.is-manual.is-locked {
  position: relative;
  opacity: 0.72;
  background: var(--surface-muted, rgba(127, 127, 127, 0.06));
  border-radius: 6px;
}

.locked-hint {
  grid-column: 1 / -1;
  margin: 0 0 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--warning, #b8860b);
  background: rgba(184, 134, 11, 0.08);
  border-left: 3px solid var(--warning, #b8860b);
  border-radius: 3px;
}

/* 挂单在盘上：还能改，用中性色提示「可编辑」而不是「锁定」 */
.locked-hint.is-editable {
  color: var(--el-color-info, #3884ff);
  background: rgba(56, 132, 255, 0.08);
  border-left-color: var(--el-color-info, #3884ff);
}

/* 运行中任务提示横幅 */
.runtime-banner {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(56, 132, 255, 0.3);
  border-left: 3px solid #3884ff;
  border-radius: 4px;
  background: rgba(56, 132, 255, 0.06);
}

.runtime-banner-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text, inherit);
}

.runtime-banner-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted, #666);
}

/* 保存提醒：不阻止保存，只说明「保存后会发生什么」 */
.form-warnings {
  padding: 10px 12px;
  border: 1px solid rgba(230, 162, 60, 0.35);
  border-left: 3px solid #e6a23c;
  border-radius: 4px;
  background: rgba(230, 162, 60, 0.07);
}

.form-warnings-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text, inherit);
}

.form-warnings-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted, #666);
}

.manual-link-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 34px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.batch-level .manual-link-switch input[type='checkbox'] {
  width: auto;
  margin: 0;
}

.batch-level-no {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  padding-bottom: 8px;
}

.batch-range-inputs {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.batch-range-inputs input {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
}

.batch-level-actions {
  display: flex;
  align-items: center;
  padding-bottom: 1px;
}

.batch-level-add {
  display: flex;
  justify-content: flex-start;
  gap: 8px;
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
  /* 档位横向表格在窄屏必然挤成一团，改为整块卡片 */
  .batch-level,
  .batch-level.is-manual {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px 12px;
    padding: 12px 14px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02);
    align-items: start;
  }
  .batch-level-no {
    grid-column: 1 / -1;
    padding-bottom: 0;
  }
  .batch-level-actions {
    grid-column: 1 / -1;
    padding-bottom: 0;
    justify-content: flex-end;
  }
  /* 间距类字段占满整行，标签不再与输入框错位 */
  .batch-level .field {
    min-width: 0;
  }
  .batch-level :deep(.form-label-row) {
    white-space: normal;
  }
  .batch-level :deep(.tag-select) {
    gap: 6px;
  }
}

@media (max-width: 768px) {
  .strategy-form-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .rules-editor-head {
    flex-wrap: wrap;
  }

  /* ── 手机端：单列 + 更舒展的纵向间距，避免两列挤压 ── */
  .rule-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .batch-top-grid {
    grid-template-columns: 1fr;
    max-width: none;
    gap: 12px;
  }

  /* 规则面板收紧内边距，把空间留给字段 */
  .rule-panel {
    padding: 12px 12px;
    margin-bottom: 10px;
  }
  .rule-panel-head {
    flex-wrap: wrap;
    gap: 8px 10px;
    margin-bottom: 10px;
  }
  .rule-title-wrap {
    min-width: 0;
  }
  .rule-title-label :deep(.form-label-row) {
    white-space: normal;
  }
  /* 「启用 + 恢复默认」独占一行，不再和标题挤在一起 */
  .rule-panel-actions {
    width: 100%;
    justify-content: space-between;
    padding-top: 8px;
    border-top: 1px dashed var(--glass-border);
  }

  /* ── 档位卡片：表头行 + 字段网格 ── */
  .batch-block {
    margin-top: 12px;
    padding-top: 10px;
  }
  .batch-head {
    flex-wrap: wrap;
    gap: 8px 10px;
  }
  .batch-levels {
    gap: 12px;
  }
  .batch-level,
  .batch-level.is-manual {
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 12px;
    border-radius: var(--radius-sm);
  }
  /* 加仓次数（起~止）单独占一行，读起来是「第 1~3 次」而不是两列裸输入框 */
  .batch-level .batch-range {
    grid-column: 1 / -1;
  }
  .batch-range-inputs {
    gap: 8px;
  }
  .batch-range-inputs input {
    padding: 10px;
    text-align: center;
  }
  /* 档位标题行：└1 第 1 档 1~3 次 */
  .batch-level-no {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    width: 100%;
    padding-bottom: 10px;
    margin-bottom: 2px;
    border-bottom: 1px dashed var(--glass-border);
    font-size: 12px;
  }
  .batch-level-no .lv-name {
    color: var(--text);
    font-weight: 600;
  }
  .batch-level-no .lv-sub {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
  }
  .batch-level-no .lv-mark {
    font-family: var(--mono);
    font-size: 12px;
  }
  /* 关键参数（点数 / 倍数 / 手数）并排三列，减少纵向滚动 */
  .batch-level-actions {
    justify-content: stretch;
  }
  .batch-level-actions .btn-sm {
    flex: 1;
    min-height: 40px;
  }
  .batch-level-add {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .batch-level-add .btn-sm {
    min-height: 40px;
  }

  /* ── 触控目标放大到接近 44px ── */
  .rule-grid input,
  .rule-grid select,
  .batch-top-grid input,
  .batch-top-grid select,
  .batch-level input,
  .batch-level select {
    min-height: 42px;
    font-size: 15px; /* 防止 iOS 聚焦时自动放大页面 */
  }
  .rule-grid :deep(.form-label-row),
  .batch-top-grid :deep(.form-label-row),
  .batch-level :deep(.form-label-row) {
    white-space: normal;
    font-size: 12px;
  }
  .rule-grid :deep(.tag-select),
  .batch-top-grid :deep(.tag-select),
  .batch-level :deep(.tag-select) {
    gap: 8px;
  }
  /* 单选标签铺满整行，点击区域更宽 */
  .rule-grid :deep(.tag-select-item),
  .batch-top-grid :deep(.tag-select-item),
  .batch-level :deep(.tag-select-item) {
    flex: 1 1 auto;
    justify-content: center;
    min-height: 40px;
    padding: 8px 10px;
    font-size: 13px;
  }
  /* 帮助图标：放大到 24px（WCAG 2.5.8 最小目标尺寸），不撑高标签行 */
  .field-help-btn {
    width: 24px;
    height: 24px;
    font-size: 12px;
  }
  .rule-enable-wrap {
    gap: 10px;
  }
  .rule-enable-wrap input[type='checkbox'] {
    width: 20px;
    height: 20px;
  }
  .manual-link-switch {
    min-height: 42px;
    white-space: normal;
    font-size: 13px;
  }

  /* ── 帮助气泡：单列布局下铺满字段宽度，长文自动换行 ──
     注意：这里不能用 position: fixed —— 外层 .card 带 backdrop-filter，
     会成为 fixed 的包含块，气泡会锚到卡片底部而不是视口，跑到看不见的地方。 */
  .rule-grid :deep(.field-help-popover),
  .batch-top-grid :deep(.field-help-popover),
  .batch-level :deep(.field-help-popover) {
    position: absolute;
    top: auto;
    left: 0;
    right: 0;
    min-width: 0;
    max-width: none;
    white-space: normal;
    line-height: 1.6;
  }

  /* 提示文案在窄屏放宽行距，长公式不再挤成一行 */
  .rule-hint {
    font-size: 12px;
    line-height: 1.65;
  }
  .batch-count-hint,
  .batch-split-hint {
    font-size: 12px;
  }

  /* 信号盈亏控制的专属样式见文件末尾（随新结构一起定义） */
}

/* 超窄屏（iPhone SE / 小屏安卓）：再降一档密度 */
@media (max-width: 400px) {
  .rule-panel {
    padding: 10px;
  }
  .rule-grid,
  .batch-top-grid,
  .batch-level,
  .batch-level.is-manual {
    gap: 10px;
  }
  .batch-level,
  .batch-level.is-manual {
    padding: 10px;
  }
  .batch-range-inputs {
    gap: 6px;
  }
  .rule-panel-actions {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }
  .rule-enable-wrap {
    justify-content: space-between;
  }
  .batch-level-add {
    grid-template-columns: 1fr;
  }
  .rule-title-label :deep(.form-label-row) {
    font-size: 13px;
  }
}

.signal-pl-editor {
  margin-top: 18px;
}

.signal-pl-rules {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ── 信号盈亏控制（仅 AI 智能加仓模版） ── */
.signal-pl-rules {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 两块功能各自成卡，开关行固定在顶部，展开区不再和开关挤在一行 */
.signal-pl-card {
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  border: 1px solid var(--glass-border);
}

.signal-pl-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 24px;
}

.signal-pl-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  user-select: none;
}

.signal-pl-switch input[type='checkbox'] {
  width: 18px;
  height: 18px;
  margin: 0;
}

.signal-pl-count {
  font-size: 12px;
}

/* 字段网格：桌面三列 / 平板两列 / 手机单列 */
.signal-pl-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
  margin-top: 12px;
}

.signal-pl-fields .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.signal-pl-fields .field :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.signal-pl-fields .field :deep(.form-label-row) {
  white-space: nowrap;
}

.signal-pl-fields :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.signal-pl-num,
.signal-pl-times {
  width: 100%;
  min-width: 0;
}

/* 档位区：每条一行，等宽两列 + 批次号 */
.signal-pl-tiers {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--glass-border);
}

.signal-pl-tier {
  display: grid;
  grid-template-columns: 64px repeat(2, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
}

.signal-pl-tier-no {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  background: var(--glass);
  border: 1px solid var(--glass-border);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
  text-align: center;
  white-space: nowrap;
}

.signal-pl-tier .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.signal-pl-tier .field :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.signal-pl-tier .field :deep(.form-label-row) {
  white-space: nowrap;
}

.signal-pl-tier :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.signal-pl-note,
.signal-pl-off {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}

.signal-pl-off {
  margin-top: 8px;
}

/* 手机端：全部单列，避免标签和输入框被挤成两栏 */
@media (max-width: 768px) {
  .signal-pl-fields {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .signal-pl-card {
    padding: 12px;
  }
  .signal-pl-tier {
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    padding: 10px;
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--glass-border);
  }
  /* 批次号独占一行，作为该档的标题 */
  .signal-pl-tier-no {
    grid-column: 1 / -1;
    min-height: 28px;
    justify-content: center;
  }
  .signal-pl-fields input,
  .signal-pl-tier input {
    min-height: 42px;
    font-size: 15px; /* 防止 iOS 聚焦时自动放大页面 */
  }
  .signal-pl-switch input[type='checkbox'] {
    width: 20px;
    height: 20px;
  }
  .signal-pl-fields :deep(.form-label-row),
  .signal-pl-tier :deep(.form-label-row) {
    white-space: normal;
  }
  .signal-pl-fields :deep(.tag-select-item) {
    flex: 1 1 auto;
    justify-content: center;
    min-height: 40px;
  }
  /* 单列下帮助气泡铺满字段宽度，长文自动换行。
     不能用 position: fixed —— 外层 .card 带 backdrop-filter，
     会成为 fixed 的包含块，气泡会锚到卡片底部而跑出视口。 */
  .signal-pl-fields :deep(.field-help-popover),
  .signal-pl-tier :deep(.field-help-popover) {
    position: absolute;
    top: auto;
    left: 0;
    right: 0;
    min-width: 0;
    max-width: none;
    white-space: normal;
    line-height: 1.6;
  }
}

@media (max-width: 400px) {
  .signal-pl-tier {
    grid-template-columns: 1fr;
  }
}
</style>
