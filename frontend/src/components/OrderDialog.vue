<script setup lang="ts">
/**
 * 开仓弹窗（手机端友好）
 *  - 顶部：标题 + 副标题 + 关闭
 *  - 策略 Tab（图标 + 文字）：切换不同参数集
 *  - 参数区：单列卡片列表，每行 label + 副说明 + 右侧输入（带单位/占位）
 *  - 节标题（左侧蓝色竖线）
 *  - 底部：左下「带单 / 策略复用」模式选择 + 右下主操作按钮
 *  - 移动端：撑满全屏（无圆角、贴顶贴底），大屏保持居中卡片
 *
 *  - 「带单」默认关闭：点提交走原 quickTrigger（normal 模型，按币种分发），且只投递主节点；
 *    需要下发到分组全部节点（含跟单员）时手动打开。
 *  - 「带单」开启 + 「策略复用」开启时，点提交走 strategy 模型 + 当前分组的 group_id，
 *    与 StrategyFormModal.triggerBoundGroup 行为一致，但**不持久化**模版参数，
 *    仅用弹窗里手填的 volume/sl/tp/entry_price/remark 作为本次信号值。
 */
import { computed, reactive, ref, watch } from 'vue'
import { ElDialog, ElMessage } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
import { useHubStore } from '@/stores/hub'
import { useAuthStore } from '@/stores/auth'
import type {
  GridMode,
  GridSizingData,
  GridSide,
  ManualSignalAction,
  ManualSignalPayload,
  ManualSignalResult,
  GroupOut,
  StrategyOut as StrategyOutType,
  StrategyRule,
  StrategyTemplateOut,
} from '@/api/types'

const hub = useHubStore()
const auth = useAuthStore()

export type OrderStrategy = 'trend_market' | 'trend_limit' | 'grid'
export type GridDirection = 'all' | 'buy' | 'sell'
export type ManualAction = Extract<ManualSignalAction, 'BUY' | 'SELL'>

export interface OrderForm {
  symbol: string
  volume: number
  sl: number | null
  tp: number | null
  entry_price: number | null
  direction: GridDirection
  remark: string
}

const props = defineProps<{
  modelValue: boolean
  strategy: OrderStrategy
  /** 父级表单（reactive 对象）。组件内通过 localForm 持有副本并双向同步 */
  form: OrderForm & { grid_step?: number | null; grid_layers?: number | null }
  groupName: string
  /** 「策略复用」开关需要的 group_id：父级按 dialogGroupName 在 hub.groups 里查到的真实 ID */
  groupId?: string | null
  submitting: boolean
  lastError: string
  canSubmit: boolean
  /**
   * 当前品种 mid 报价（市价开仓时用于自动算方向：SL < mid → BUY；SL > mid → SELL）。
   * 父级未注入或为 null 时，按钮置灰并提示"等待报价"。
   */
  currentMid?: number | null
  /**
   * 模式：
   *   'trigger' - 点按钮保存策略并 emit 'trigger-strategy' 发信号（TradingView 等管理员场景）
   *   'save'    - 点按钮只保存策略参数，不发信号（UserStrategiesView 等用户场景）
   */
  mode?: 'trigger' | 'save'
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'update:strategy', v: OrderStrategy): void
  (e: 'update:form', v: OrderForm & { grid_step?: number | null; grid_layers?: number | null }): void
  (e: 'cancel'): void
  /**
   * 「策略复用」触发：父级负责实际调 hub.triggerManualSignal(payload)。
   * payload 已经构造好（model=strategy, group_ids=[groupId]），
   * 父级只需要 catch 异常并把成功/失败回写到 props.lastError 即可。
   */
  (e: 'trigger-strategy', payload: ManualSignalPayload): void
}>()

// ── 派生 ──
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const strategyRef = computed({
  get: () => props.strategy,
  set: (v) => emit('update:strategy', v),
})

// 本地表单：用户操作改这里，emit 给父级写回
const localForm = reactive<OrderForm & { grid_step?: number | null; grid_layers?: number | null }>({
  symbol: props.form.symbol,
  volume: props.form.volume,
  sl: props.form.sl,
  tp: props.form.tp,
  entry_price: props.form.entry_price,
  direction: props.form.direction,
  remark: props.form.remark,
  grid_step: props.form.grid_step,
  grid_layers: props.form.grid_layers,
})

// 父级 form 引用变化（不是字段）时同步，例如父级重新创建 form
// 父级 form 变化时同步到 localForm（深监听：父级修改 form.symbol / sl 等字段时也能同步）
watch(
  () => props.form,
  (v) => {
    localForm.symbol = v.symbol
    localForm.volume = v.volume
    localForm.sl = v.sl
    localForm.tp = v.tp
    localForm.entry_price = v.entry_price
    localForm.direction = v.direction
    localForm.remark = v.remark
    localForm.grid_step = v.grid_step
    localForm.grid_layers = v.grid_layers
  },
  { deep: true },
)

// localForm 任一字段变化 → emit 给父级 Object.assign 写回
watch(
  localForm,
  () => {
    emit('update:form', { ...localForm })
  },
  { deep: true },
)

// 弹窗打开时（visible 从 false → true）自动重置各状态
watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      carry.value = false
      lastStrategyError.value = ''
      // 每次打开清空试算结果，避免上一笔的旧数据混入
      gridSizing.data = null
      gridSizing.error = ''
      gridSizing.loading = false
      // 止盈价 → 盈亏比联动：重置手动标记与市价入场价快照
      rrManualEdited.value = false
      marketEntrySnapshot.value =
        props.currentMid != null && isFinite(props.currentMid) && props.currentMid > 0
          ? props.currentMid
          : null
      // 加载分组详情 + 策略模版 + 已有策略参数
      await loadGroupAndStrategy()
    }
  },
)

/** 「带单」开关：默认关闭（每笔新单都只发主节点），需要时手动打开 */
const carry = defineModel<boolean>('carry', { default: false })

/**
 * 市价开仓的方向由前端根据「止损价 vs 当前 mid」自动判定：
 *   sl < mid → BUY（多）
 *   sl > mid → SELL（空）
 * 没有 mid 报价时返回 null（按钮置灰，提示"等待报价"）。
 */
const derivedAction = computed<ManualAction | null>(() => {
  // 限价：按入场价 vs 止损价判断方向
  if (isLimit.value) {
    const ep = localForm.entry_price
    const sl = localForm.sl
    if (ep == null || !isFinite(ep) || ep <= 0) return null
    if (sl == null || !isFinite(sl) || sl <= 0) return null
    return ep > sl ? 'BUY' : 'SELL'
  }
  // 市价：按当前中间价 vs 止损价判断方向
  if (!isMarket.value) return null
  const mid = props.currentMid
  const sl = localForm.sl
  if (mid == null || !mid || !isFinite(mid)) return null
  if (sl == null || !sl || !isFinite(sl) || sl <= 0) return null
  return sl < mid ? 'BUY' : 'SELL'
})

/** 策略复用执行中的内部 loading（与 props.submitting 区分） */
const strategySubmitting = ref(false)
/** 策略复用执行结果 / 错误回显 */
const lastStrategyError = ref('')

/** 是否仅保存模式：点按钮只保存策略，不发信号 */
const isSaveMode = computed(() => props.mode === 'save')

/**
 * 是否展示「带单」开关：交易弹窗里始终展示（保存模式不发信号，不展示）。
 * 关闭 = 只发给主节点；打开 = 发给分组里的全部节点（含跟单节点）。
 * follow_leader 能力决定的是「能不能出现在带单广场被别人跟」，不决定这个开关出不出现。
 */
const canCarry = computed(() => !isSaveMode.value)

/**
 * 我是否已暂停【带单】市场。
 *
 * 暂停后，即使「带单」开关打开，信号也**只下发主节点** —— 收窄在服务端做
 * （服务端按分组绑定的策略模版取用对应市场的暂停标志），覆盖 webhook /
 * 限价监听等所有触发路径，前端开关绕不过去。
 *
 * 这个弹窗只发趋势（tpl_2）/ 网格（tpl_3）策略，都属带单市场；
 * AI 智能加仓（tpl_1）在【AI 交易】页触发，那边有独立的 ai_follow_paused。
 */
const pausedInMarket = computed<string>(() => (auth.me?.follow_paused ? '带单' : ''))

const STRATEGIES: { key: OrderStrategy; label: string }[] = [
  { key: 'trend_market', label: '市价开仓' },
  { key: 'trend_limit', label: '限价开仓' },
  { key: 'grid', label: '智能吸筹' },
]

/** 策略中文名（底部脚注用） */
const STRATEGY_LABEL: Record<OrderStrategy, string> = {
  trend_market: '市价开仓',
  trend_limit: '限价开仓',
  grid: '智能吸筹',
}

const GRID_MODE_OPTIONS: readonly { value: GridMode; label: string }[] = [
  { value: 'arithmetic', label: '等差' },
  { value: 'geometric', label: '等比' },
]

const isGrid = computed(() => strategyRef.value === 'grid')
const isLimit = computed(() => strategyRef.value === 'trend_limit')
const isMarket = computed(() => strategyRef.value === 'trend_market')
/**
 * 「策略复用」按钮启用条件：分组已就绪 + 策略参数通过校验 + 前端能根据 SL / 报价 判定方向。
 * （props.groupId 可能为 null：首次触发时父级没拉到 group，OrderDialog 会按 groupName 自动建分组）
 */
const canTriggerStrategy = computed(
  () =>
    !!currentGroup.value &&
    !props.submitting &&
    !strategySubmitting.value &&
    !strategyLoading.value &&
    !validateRiskParams() &&
    derivedAction.value !== null,
)

/** 网格策略触发按钮启用条件：分组就绪 + 参数校验通过 */
const canTriggerGridStrategy = computed(
  () =>
    !!currentGroup.value &&
    !strategySubmitting.value &&
    !strategyLoading.value &&
    !validateGridParams(),
)

// ── 策略参数编辑（市价开仓专用） ──
/**
 * 策略模版列表（从 hub 加载）
 * 市价开仓 = 以损定量趋势单（template_id 对应模版2，即风险量化模版）
 * 其规则数组中 type=3（以损定量）的规则包含：risk_amount / rr_ratio / base_ratio / breakeven_times
 */
const templates = ref<StrategyTemplateOut[]>([])
const currentGroup = ref<GroupOut | null>(null)
const currentStrategy = ref<StrategyOutType | null>(null)

/** 策略参数编辑状态（仅市价开仓时渲染） */
const riskParams = reactive({
  risk_amount: 100,   // 风险金额（美元）
  rr_ratio: 2.5,     // 盈亏比
  base_ratio: 30,     // 底仓占比 %
  breakeven_times: 0, // 保本触发倍数（0=不启用）
})

/* ── 止盈价 → 盈亏比联动 ──
 * 客户一旦填了止盈价（选填），就等于指定了目标价，实际盈亏比不再等于配置里的
 * rr_ratio —— 策略是按 rr_ratio 铺阶梯止盈的（最远一档 = 入场价 ± 止损距离 ×
 * rr_ratio），所以这里自动反算「实际盈亏比」并同步进 riskParams.rr_ratio，
 * 让阶梯最后一档正好落在客户填的止盈价上。
 *
 *   限价：入场价 = 挂单价（entry_price）
 *   市价：入场价 = 客户填写止盈/止损那一刻的报价快照
 *         （不跟随行情跳动，否则盈亏比会一直抖）
 */
const marketEntrySnapshot = ref<number | null>(null)

/** 数字取值：空串 / NaN / null 一律按 0 处理 */
function toNum(v: unknown): number {
  const n = Number(v)
  return isFinite(n) ? n : 0
}

/** 推算用的入场价 */
const entryPriceForRr = computed<number | null>(() => {
  if (isLimit.value) {
    const ep = toNum(localForm.entry_price)
    return ep > 0 ? ep : null
  }
  if (isMarket.value) {
    // 用客户编辑那一刻的报价快照（不跟随行情跳动，否则盈亏比会一直抖）
    return marketEntrySnapshot.value
  }
  return null
})

/** 反算出的实际盈亏比（止盈距离 ÷ 止损距离）；算不出来时为 null */
const impliedRr = computed<number | null>(() => {
  const tp = toNum(localForm.tp)
  const sl = toNum(localForm.sl)
  const entry = entryPriceForRr.value
  if (tp <= 0 || sl <= 0 || entry == null) return null
  const risk = Math.abs(entry - sl)
  const reward = Math.abs(tp - entry)
  if (risk <= 0 || reward <= 0) return null
  return Math.round((reward / risk) * 100) / 100
})

/** 推算出的开仓方向（与 derivedAction 同口径，只是用推算入场价） */
const rrDirection = computed<'BUY' | 'SELL' | null>(() => {
  const sl = toNum(localForm.sl)
  const entry = entryPriceForRr.value
  if (sl <= 0 || entry == null) return null
  return sl < entry ? 'BUY' : 'SELL'
})

/** 止盈方向是否与开仓方向一致（做多要止盈在入场价上方，做空相反） */
const tpSideOk = computed<boolean | null>(() => {
  const tp = toNum(localForm.tp)
  const entry = entryPriceForRr.value
  const dir = rrDirection.value
  if (tp <= 0 || entry == null || dir == null) return null
  return dir === 'BUY' ? tp > entry : tp < entry
})

/** 客户手动改过盈亏比 → 不再自动覆盖，只提示差异 */
const rrManualEdited = ref(false)

/** 把反算出的实际盈亏比同步进策略参数 */
function syncRrFromTp(): void {
  const rr = impliedRr.value
  if (rr == null || rrManualEdited.value) return
  if (Math.abs(toNum(riskParams.rr_ratio) - rr) < 0.005) return
  riskParams.rr_ratio = rr
}

/** 客户点了「改用 1:x」：覆盖手动值，回到止盈价反算出来的盈亏比 */
function applyImpliedRr(): void {
  const rr = impliedRr.value
  if (rr == null) return
  riskParams.rr_ratio = rr
  rrManualEdited.value = false
}

// 客户改止盈价 → 视为重新指定目标价，允许再次自动同步
watch(
  () => localForm.tp,
  () => {
    rrManualEdited.value = false
  },
)

// 客户改止盈/止损/挂单价 → 刷新市价入场价快照 + 同步盈亏比
watch(
  () => [localForm.tp, localForm.sl, localForm.entry_price],
  () => {
    if (isMarket.value) {
      const mid = props.currentMid
      marketEntrySnapshot.value = mid != null && isFinite(mid) && mid > 0 ? mid : marketEntrySnapshot.value
    }
    syncRrFromTp()
  },
)

// 报价后到时补一次快照（客户还没动价格，先有个入场价可用）
watch(
  () => props.currentMid,
  (mid) => {
    if (!isMarket.value) return
    if (marketEntrySnapshot.value != null) return
    if (mid != null && isFinite(mid) && mid > 0) marketEntrySnapshot.value = mid
  },
)

/** 盈亏比提示（info / warn / error 三种） */
interface RrNotice {
  kind: 'info' | 'warn' | 'error'
  text: string
  /** 可选的「一键改用」动作 */
  actionLabel?: string
  action?: () => void
}

const rrNotice = computed<RrNotice | null>(() => {
  if (isGrid.value) return null
  const tp = toNum(localForm.tp)
  if (tp <= 0) return null
  const sl = toNum(localForm.sl)
  if (sl <= 0) {
    return { kind: 'warn', text: '已填写止盈价，请再填写止损价以便推算实际盈亏比。' }
  }
  const entry = entryPriceForRr.value
  if (entry == null) {
    return {
      kind: 'warn',
      text: isMarket.value
        ? '已填写止盈价，等待当前报价以推算实际盈亏比。'
        : '已填写止盈价，请填写挂单价以便推算实际盈亏比。',
    }
  }
  if (tpSideOk.value === false) {
    const dir = rrDirection.value === 'BUY' ? '做多' : '做空'
    return {
      kind: 'error',
      text: `止盈价 ${tp} 与开仓方向（${dir}）相反，无法构成有效盈亏比，请检查止盈价。`,
    }
  }
  const rr = impliedRr.value
  if (rr == null) return null
  const cur = toNum(riskParams.rr_ratio)
  const basis = `（入场价 ${entry}、止损价 ${sl}）`
  if (Math.abs(cur - rr) < 0.01) {
    return rrManualEdited.value
      ? { kind: 'info', text: `当前盈亏比 1:${cur} 与止盈价推算结果一致 ${basis}。` }
      : { kind: 'info', text: `已按止盈价自动同步盈亏比为 1:${rr} ${basis}。` }
  }
  return {
    kind: 'warn',
    text: `止盈价对应的实际盈亏比为 1:${rr} ${basis}，与当前配置的 1:${cur} 不一致；提交后将按 1:${cur} 铺阶梯止盈。`,
    actionLabel: `改用 1:${rr}`,
    action: applyImpliedRr,
  }
})

/**
 * 网格策略参数编辑状态（仅网格模式时渲染）。
 *
 * 网格方向**不再由客户选择**，而是由「止损价 vs 价格区间」自动推导：
 *   止损价 < 区间下限 → 只做多（止损落在区间下方）
 *   止损价 > 区间上限 → 只做空（止损落在区间上方）
 * 两者之间（落在区间内）属于非法几何，保存时会被后端判为未设。
 *
 * 因此这里只保留一个 stop_price（客户填的止损价），后端需要的
 * stop_lower / stop_upper 由 derivedGridSide 映射得到。
 */
const gridParams = reactive({
  price_lower: 0 as number | null,      // 区间下限
  price_upper: 0 as number | null,      // 区间上限
  grid_count: 10,                        // 网格数量（自动试算回写）
  grid_mode: 'arithmetic' as GridMode,  // 网格模式
  lot_per_grid: 0.01,                  // 每格手数（自动试算回写）
  trigger_price: 0 as number | null,    // 触发价（默认 0 = 立即启动）
  stop_price: 0 as number | null,       // 止损价（客户填写；方向由此推导）
  take_profit: 0 as number | null,      // 止盈价（界面上不展示，沿用存量规则的值）
  trailing_up: false,                  // 向上追踪（默认关闭）
  trailing_max: 0,                      // 最大平移格数（0=不限）
  max_allow_loss: 0,                     // 最大可接受亏损
})

/**
 * 网格方向（自动推导）：止损价在区间下方=只做多，在区间上方=只做空。
 * 无法判定时返回 null（止损价未填 / 区间非法 / 止损落在区间内）。
 */
const derivedGridSide = computed<GridSide | null>(() => {
  const pl = Number(gridParams.price_lower) || 0
  const pu = Number(gridParams.price_upper) || 0
  const sp = Number(gridParams.stop_price) || 0
  if (!(pl > 0) || !(pu > pl) || !(sp > 0)) return null
  if (sp < pl) return 'long'
  if (sp > pu) return 'short'
  return null
})

/** 推导方向的中文标签（只读展示用，客户不再选择） */
const derivedGridSideLabel = computed(() => {
  const side = derivedGridSide.value
  if (side === 'long') return '多头'
  if (side === 'short') return '空头'
  return '待判定'
})

/**
 * 映射到后端规则的终止价：
 *   只做多 → stop_lower = 止损价（区间下方），stop_upper = 止盈价
 *   只做空 → stop_upper = 止损价（区间上方），stop_lower = 止盈价
 * 与 node_client/grid_trading.terminate_reason 的方向语义一致。
 * 止盈价界面已隐藏，新策略为 0（不设）；存量规则加载时保留原值，避免改一次参数
 * 就把原来的止盈抹掉。
 */
const gridStopLower = computed(() =>
  derivedGridSide.value === 'long'
    ? Number(gridParams.stop_price) || 0
    : Number(gridParams.take_profit) || 0,
)
const gridStopUpper = computed(() =>
  derivedGridSide.value === 'short'
    ? Number(gridParams.stop_price) || 0
    : Number(gridParams.take_profit) || 0,
)

/** 网格试算结果（自动反算 grid_count / lot_per_grid） */
const gridSizing = reactive<{
  data: GridSizingData | null
  loading: boolean
  error: string
}>({
  data: null,
  loading: false,
  error: '',
})

/** 试算 debounce 计时器 */
let gridSizingTimer: ReturnType<typeof setTimeout> | null = null
/** 试算请求 AbortController（用于超时/防竞态） */
let gridSizingAbort: AbortController | null = null

/** 行情源节点：主节点（用户已选过的 is_primary && enabled） */
const primaryNode = computed(() => hub.nodes.find((n) => n.is_primary && n.enabled) || null)

/** 策略参数加载中 */
const strategyLoading = ref(false)

/**
 * 弹窗打开时加载分组详情（含 strategy_id）+ 策略模版列表。
 * 如果该分组已有绑定的策略，从 hub.strategies 里取当前值填入 riskParams。
 *
 * 市价/限价模式需要走 strategy 模型（要求真实 group_id）：
 *   找不到（首次触发，按 props.groupName 命名还没建过分组）→ 立刻创建一个新分组。
 *   市价/限价各用各自的 groupName（父级按 strategyType 拼出），新建出的分组互不污染。
 */
async function loadGroupAndStrategy(): Promise<void> {
  strategyLoading.value = true
  try {
    // 拉模版列表（用于获取以损定量规则的默认骨架）
    templates.value = await hub.fetchStrategyTemplates()
    // 网格试算需要主节点作为行情源；如未加载过则补拉一次
    if (!hub.nodes.length) {
      try {
        await hub.fetchNodes()
      } catch {
        /* 拉不到节点列表只是不能试算，不影响主流程 */
      }
    }
    // 拉分组列表并找当前分组（含 strategy_id）
    const groups = await hub.listGroups()
    let grp = groups.find((g) => g.group_id === props.groupId) ?? null
    // 兜底：按 groupName 找（市价/限价各自独立命名），可能 listGroups 缓存里没这个 groupId
    if (!grp && props.groupName) {
      grp = groups.find((g) => g.name === props.groupName) ?? null
    }
    // 首次开仓：按 props.groupName 自动建分组（市价/限价各自独立）。
    // 注意：limit_watch_enabled 必须等 strategy_id 绑上之后才能开（否则后端
    // _require_trend_strategy 会拒），所以这里只建空策略分组；监听开关放到
    // triggerMarketStrategy 里"建/绑策略成功之后"再 PATCH 上去。
    if (!grp && (isMarket.value || isLimit.value) && props.groupName) {
      // 主节点自动加入分组
      const primaryNode = hub.nodes.find((n) => n.is_primary && n.enabled)
      const node_ids = primaryNode ? [primaryNode.node_id] : []
      try {
        grp = await hub.createGroup({
          name: props.groupName,
          enabled: true,
          node_ids,
        })
        lastStrategyError.value = ''
      } catch (e) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '自动创建分组失败'
        grp = null
      }
    }
    // 网格首次开仓：同样按 props.groupName 自动建分组（主节点加入，限价监听关闭）
    if (!grp && isGrid.value && props.groupName) {
      const primaryNode = hub.nodes.find((n) => n.is_primary && n.enabled)
      const node_ids = primaryNode ? [primaryNode.node_id] : []
      try {
        grp = await hub.createGroup({ name: props.groupName, enabled: true, node_ids })
        lastStrategyError.value = ''
      } catch (e) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '自动创建分组失败'
        grp = null
      }
    }
    currentGroup.value = grp
    // 如果有 strategy_id，尝试从 hub.strategies 拿当前值
    const sid = grp?.strategy_id
    if (sid) {
      let strat: StrategyOutType | null = null
      // 先在本地缓存找
      const cached = hub.strategies.find((s) => s.strategy_id === sid)
      if (cached) {
        strat = cached
      } else {
        // 拉全量策略再找
        await hub.fetchStrategies()
        const found = hub.strategies.find((s) => s.strategy_id === sid)
        strat = found ?? null
      }
      currentStrategy.value = strat
      // 填入 riskParams（从以损定量规则取第一个启用的）
      if (strat) {
        const rule = (strat.rules || []).find(
          (r: StrategyRule) => r.type === 3 && r.status === 1,
        )
        if (rule) {
          riskParams.risk_amount = rule.risk_amount ?? 100
          riskParams.rr_ratio = rule.rr_ratio ?? 2.5
          riskParams.base_ratio = rule.base_ratio ?? 30
          riskParams.breakeven_times = rule.breakeven_times ?? 0
        }
        // 填入 gridParams（从网格规则取第一个启用的）
        const gridRule = (strat.rules || []).find(
          (r: StrategyRule) => r.type === 4 && r.status === 1,
        )
        if (gridRule) {
          gridParams.price_lower = gridRule.price_lower ?? 0
          gridParams.price_upper = gridRule.price_upper ?? 0
          gridParams.grid_count = gridRule.grid_count ?? 10
          gridParams.grid_mode = gridRule.grid_mode ?? 'arithmetic'
          gridParams.lot_per_grid = gridRule.lot_per_grid ?? 0.01
          gridParams.trigger_price = gridRule.trigger_price ?? 0
          // 存量规则的 stop_lower / stop_upper 按方向还原成「止损价 + 止盈价」：
          // 只做多 → stop_lower 是止损、stop_upper 是止盈；只做空 → 反之。
          gridParams.stop_price =
            gridRule.grid_side === 'short'
              ? gridRule.stop_upper ?? 0
              : gridRule.stop_lower ?? 0
          gridParams.take_profit =
            gridRule.grid_side === 'short'
              ? gridRule.stop_lower ?? 0
              : gridRule.stop_upper ?? 0
          gridParams.trailing_up = gridRule.trailing_up ?? false
          gridParams.trailing_max = gridRule.trailing_max ?? 0
          gridParams.max_allow_loss = gridRule.assist_max_loss ?? 0
        }
      }
    } else {
      currentStrategy.value = null
      // 没有已有策略时，市价/限价用模版2（以损定量）第一条规则的默认值
      const tpl2 = templates.value.find((t) => t.template_id === 'tpl_2')
      const rule = (tpl2?.rules || []).find((r) => r.type === 3)
      if (rule) {
        riskParams.risk_amount = rule.risk_amount ?? 100
        riskParams.rr_ratio = rule.rr_ratio ?? 2.5
        riskParams.base_ratio = rule.base_ratio ?? 30
        riskParams.breakeven_times = rule.breakeven_times ?? 0
      }
      // 网格用模版3第一条规则的默认值
      if (isGrid.value) {
        const tpl3 = templates.value.find((t) => t.template_id === 'tpl_3')
        const gRule = (tpl3?.rules || []).find((r) => r.type === 4)
        if (gRule) {
          gridParams.price_lower = gRule.price_lower ?? 0
          gridParams.price_upper = gRule.price_upper ?? 0
          gridParams.grid_count = gRule.grid_count ?? 10
          gridParams.grid_mode = gRule.grid_mode ?? 'arithmetic'
          gridParams.lot_per_grid = gRule.lot_per_grid ?? 0.01
          gridParams.trigger_price = gRule.trigger_price ?? 0
          gridParams.stop_price =
            gRule.grid_side === 'short' ? gRule.stop_upper ?? 0 : gRule.stop_lower ?? 0
          gridParams.take_profit =
            gRule.grid_side === 'short' ? gRule.stop_lower ?? 0 : gRule.stop_upper ?? 0
          gridParams.trailing_up = gRule.trailing_up ?? false
          gridParams.trailing_max = gRule.trailing_max ?? 0
          gridParams.max_allow_loss = gRule.assist_max_loss ?? 0
        }
      }
    }
  } finally {
    strategyLoading.value = false
  }
}

// ── 网格试算 ──
//
// 用法：
//   1. 监听 gridParams 关键字段 + symbol，debounce 500ms 自动调 triggerGridSizing
//   2. 试算成功 → gridSizing.data.ready 时把 grid_count / lot_per_grid 写回 gridParams
//   3. 试算过程与结果**不对客户展示**（UI 已隐藏），只用于算出网格数量与每格手数
//
// 写死的入参（不暴露在弹窗里）：
//   - timeframe = 'M1'
//   - atr_mult  = 0.6
//   - spacing   = 0（让后端按 ATR×倍数算）
//   - total_lot_limit = 0（不限）
//
// 行情源：用户已设置的主节点（is_primary && enabled）。没有则无法试算。

/** 试算前置条件：symbol 合法 + 区间合法 + 止损价能判定方向 + 最大亏损 > 0 */
const canRunGridSizing = computed(() => {
  if (!isGrid.value) return false
  const sym = (localForm.symbol || '').trim()
  if (!sym) return false
  const pl = Number(gridParams.price_lower) || 0
  const pu = Number(gridParams.price_upper) || 0
  if (!(pl > 0) || !(pu > pl)) return false
  if (!(Number(gridParams.max_allow_loss) > 0)) return false
  // 止损价必须落在区间外（下方=只做多 / 上方=只做空），否则方向无法判定
  if (!derivedGridSide.value) return false
  return true
})

/** 自动试算（debounce 后调用）：带 8s 超时。返回 Promise 以便保存前等待结果。 */
function triggerGridSizing(withDebounce: boolean): Promise<void> {
  if (!isGrid.value) return Promise.resolve()
  if (!canRunGridSizing.value) return Promise.resolve()
  if (withDebounce) {
    if (gridSizingTimer) clearTimeout(gridSizingTimer)
    return new Promise<void>((resolve) => {
      gridSizingTimer = setTimeout(() => {
        void triggerGridSizing(false).finally(() => resolve())
      }, 500)
    })
  }
  const node = primaryNode.value
  if (!node) {
    gridSizing.error = '未找到主节点，无法试算；请在「节点」页设置主节点'
    return Promise.resolve()
  }
  // 取消上一笔未完成的请求
  if (gridSizingAbort) gridSizingAbort.abort()
  const ctrl = new AbortController()
  gridSizingAbort = ctrl
  const timer = setTimeout(() => ctrl.abort(), 8000)
  gridSizing.loading = true
  gridSizing.error = ''
  const symbol = (localForm.symbol || '').trim().toUpperCase()
  return hub
    .fetchGridSizing(node.node_id, symbol, {
      timeframe: 'M1',
      atr_mult: 0.6,
      spacing: 0,
      // 全部转 Number：防止 Vue v-model.number 清空后产生空字符串导致后端报错
      price_lower: Number(gridParams.price_lower) || 0,
      price_upper: Number(gridParams.price_upper) || 0,
      grid_mode: gridParams.grid_mode,
      grid_side: derivedGridSide.value ?? 'long',
      stop_lower: gridStopLower.value,
      stop_upper: gridStopUpper.value,
      max_loss: Number(gridParams.max_allow_loss) || 0,
      prefill_enabled: false,
      close_on_stop: true,
      trailing_up: gridParams.trailing_up,
      total_lot_limit: 0,
    })
    .then((data) => {
      if (ctrl.signal.aborted) return
      gridSizing.data = data
      // 试算结果：覆盖 grid_count / lot_per_grid
      if (data.ready) {
        if (data.grid_count >= 2) gridParams.grid_count = data.grid_count
        if (data.lot_per_grid > 0) {
          gridParams.lot_per_grid = data.lot_per_grid
        } else {
          // 手数算不出来时**不要沿用上一轮的值**（可能是按更大的亏损预算算的），
          // 落到最小手数，保证保存下来的策略不会超出用户给的亏损上限。
          gridParams.lot_per_grid = 0.01
        }
      }
      if (!data.ready || data.lot_per_grid === 0) {
        const msgs = (data.warnings || [])
          .filter((w) => w.level === 'warn')
          .map((w) => w.message)
        gridSizing.error = msgs[0] || '该品种试算失败，请检查区间与止损价后重试'
      } else {
        gridSizing.error = ''
      }
    })
    .catch((e: unknown) => {
      if (ctrl.signal.aborted) return
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      gridSizing.error = detail || (e as Error).message || '试算失败'
    })
    .finally(() => {
      clearTimeout(timer)
      if (gridSizingAbort === ctrl) gridSizingAbort = null
      gridSizing.loading = false
    })
}

/** 监听 gridParams 关键字段变化，自动 debounce 试算 */
watch(
  () => [
    gridParams.price_lower,
    gridParams.price_upper,
    gridParams.grid_mode,
    gridParams.trailing_up,
    gridParams.max_allow_loss,
    gridParams.stop_price,
    (localForm.symbol || '').trim().toUpperCase(),
  ],
  () => {
    if (!isGrid.value) return
    if (!canRunGridSizing.value) {
      // 条件不满足时静默清空结果，不报错
      gridSizing.data = null
      gridSizing.error = ''
      return
    }
    void triggerGridSizing(true)
  },
)

/** 校验策略参数（市价/限价均走 strategy 模型，均需校验 riskParams） */
function validateRiskParams(): string | null {
  if (!isMarket.value && !isLimit.value) return null
  if (!(riskParams.risk_amount > 0)) return '风险金额需大于 0'
  if (riskParams.rr_ratio < 0) return '盈亏比不能为负'
  if (!(riskParams.base_ratio > 0) || riskParams.base_ratio > 100) {
    return '底仓比例需在 1 ~ 100 之间'
  }
  if (riskParams.breakeven_times < 0) return '保本倍数不能为负'
  return null
}

/**
 * 用 riskParams 构建以损定量规则，构造 strategy patch payload。
 * strategy_id 有值 → PATCH；无值 → POST 创建新策略。
 * rules 数组以模版2骨架为基础，只替换以损定量规则里的 4 个字段。
 */
function buildRiskStrategyPayload(): StrategyRule[] | null {
  const tpl2 = templates.value.find((t) => t.template_id === 'tpl_2')
  if (!tpl2?.rules?.length) {
    lastStrategyError.value = '未找到以损定量模版规则，请确认模版配置'
    return null
  }
  // 用模版2第一条以损定量规则的骨架，替换 4 个可编辑字段
  const base = tpl2.rules[0]
  return [
    {
      ...base,
      type: 3,
      status: 1,
      risk_amount: riskParams.risk_amount,
      rr_ratio: riskParams.rr_ratio,
      base_ratio: riskParams.base_ratio,
      breakeven_enabled: riskParams.breakeven_times > 0,
      breakeven_times: riskParams.breakeven_times,
      // 以下字段固定用默认值，不开放编辑
      action: base.action ?? 'all',
      // entry_mode 与 strategyType 一一对应：市价=market, 限价=limit
      // （限价/市价各自独立策略，不复用——entry_mode 一旦定下来就被该 strategy 锁死）
      entry_mode: isLimit.value ? 'limit' : 'market',
      add_batches: base.add_batches ?? 10,
      max_total_lot: base.max_total_lot ?? 0,
      breakeven_mode: base.breakeven_mode ?? 'once',
    },
  ]
}

/** 网格策略参数校验 */
function validateGridParams(): string | null {
  const pl = gridParams.price_lower ?? 0
  const pu = gridParams.price_upper ?? 0
  if (!(pl > 0)) return '区间下限需大于 0'
  if (!(pu > pl)) return '区间上限须大于下限'
  if (gridParams.grid_count < 2 || gridParams.grid_count > 200) return '网格数量需在 2 ~ 200'
  if (!GRID_MODE_OPTIONS.some((o) => o.value === gridParams.grid_mode)) return '网格模式非法'
  // 止损价决定方向：落在区间下方=只做多，落在区间上方=只做空
  if (!derivedGridSide.value) {
    const sp = Number(gridParams.stop_price) || 0
    if (!(sp > 0)) return '止损价需大于 0'
    return '止损价须在价格区间之外：低于区间下限为只做多，高于区间上限为只做空'
  }
  // 每格手数由自动试算按最大亏损反推，所以最大亏损必须填
  if (!(gridParams.max_allow_loss > 0)) return '以损定量需大于 0'
  if (!(gridParams.lot_per_grid > 0)) return '每格手数需大于 0'
  return null
}

/**
 * 用 gridParams 构建网格规则（type=4），构造 strategy patch payload。
 */
function buildGridStrategyPayload(): StrategyRule[] | null {
  const tpl3 = templates.value.find((t) => t.template_id === 'tpl_3')
  if (!tpl3?.rules?.length) {
    lastStrategyError.value = '未找到网格交易模版规则，请确认模版配置'
    return null
  }
  // 用模版3第一条网格规则的骨架，只替换网格相关字段
  const base = tpl3.rules[0]
  const side = derivedGridSide.value ?? 'long'
  return [
    {
      ...base,
      type: 4,
      status: 1,
      action: side === 'short' ? 'SELL' : 'BUY',
      price_lower: gridParams.price_lower ?? 0,
      price_upper: gridParams.price_upper ?? 0,
      grid_count: gridParams.grid_count,
      grid_mode: gridParams.grid_mode,
      grid_side: side,
      lot_per_grid: gridParams.lot_per_grid,
      trigger_price: gridParams.trigger_price ?? 0,
      stop_lower: gridStopLower.value,
      stop_upper: gridStopUpper.value,
      close_on_stop: true,
      prefill_enabled: false,
      trailing_up: gridParams.trailing_up,
      trailing_max: gridParams.trailing_max,
      assist_enabled: false,
      assist_max_loss: gridParams.max_allow_loss > 0 ? gridParams.max_allow_loss : 0,
    },
  ]
}

function close() {
  emit('cancel')
  emit('update:modelValue', false)
}

/** 手数兜底：未填 / 0 / NaN → 默认 0.01。
 *  市价模式下 volume 不进 payload（后端按 risk_amount 反算），
 *  但仍统一归一化字段，保证 UI 与下游读取一致。*/
function normalizeVolume(): void {
  const v = localForm.volume
  if (v == null || isNaN(v) || v <= 0) {
    localForm.volume = 0.01
  }
}

// ── 策略复用：构造 payload + 上抛给父级触发 ──
//
// 关键约束（与 StrategyFormModal.triggerBoundGroup 保持一致）：
//   1. 必须有 group_id
//   2. 必须有合法 volume > 0
//   3. 弹窗 symbol 必须与分组绑定品种匹配（容忍 XAUUSD / XAUUSDm 前缀差异）
//   4. sl 在 trend_market / trend_limit 下必填；grid 不要求
//
// 触发前会用按钮文字里展示摘要（手数 + SL/TP/entry），不二次确认弹窗
// —— 因为策略复用走的是「对自己已经持有的分组」单次编辑，与 StrategyFormModal 那种
// 「先保存模版再触发」不同，这里没有副作用风险，提交即生效。

/**
 * 市价开仓触发流程（默认走 strategy 模型）：
 *  1. 用 riskParams 构建 rules，PATCH 已策略 / POST 新策略
 *  2. 构造 ManualSignalPayload（不带 volume，由后端按 risk_amount / sl 反算）
 *  3. emit 给 TradingView，后者调 hub.triggerManualSignal(payload)
 */
/**
 * 触发入口：网格走 triggerGridStrategy，市价/限价走 triggerMarketStrategy。
 */
async function triggerStrategy(): Promise<void> {
  if (isGrid.value) {
    await triggerGridStrategy()
  } else {
    await triggerMarketStrategy()
  }
}

/** 仅保存策略（不发信号）。成功时关闭弹窗并提示 */
async function saveStrategyOnly(): Promise<void> {
  if (isGrid.value) {
    await saveGridStrategyOnly()
  } else {
    await saveRiskStrategyOnly()
  }
}

/** 市价/限价：只保存策略，不发信号 */
async function saveRiskStrategyOnly(): Promise<void> {
  const rpErr = validateRiskParams()
  if (rpErr) { lastStrategyError.value = rpErr; return }
  const rules = buildRiskStrategyPayload()
  if (!rules) return
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    let strategyId = currentStrategy.value?.strategy_id
    if (strategyId) {
      await hub.updateStrategy(strategyId, {
        name: currentStrategy.value!.name,
        symbol: (localForm.symbol || '').trim().toUpperCase(),
        rules,
      })
    } else {
      const tpl2 = templates.value.find((t) => t.template_id === 'tpl_2')
      const newName = `${currentGroup.value.name}-${strategyRef.value}-策略`
      const before = [...hub.strategies]
      let existed: StrategyOutType | undefined
      try {
        await hub.fetchStrategies({ q: newName })
        existed = (hub.strategies || []).find(
          (s) => s.name === newName && s.owner_user_id === currentGroup.value?.owner_user_id,
        )
      } finally {
        hub.strategies = before
      }
      if (existed) {
        await hub.updateStrategy(existed.strategy_id, {
          name: newName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = existed.strategy_id
        await hub.updateGroup(currentGroup.value.group_id, { strategy_id: existed.strategy_id })
      } else {
        const created = await hub.createStrategy({
          template_id: tpl2?.template_id ?? '',
          name: newName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = created.strategy_id
        if (strategyId) {
          await hub.updateGroup(currentGroup.value.group_id, { strategy_id: strategyId })
        }
      }
    }
    strategySubmitting.value = false
    emit('update:modelValue', false)
    ElMessage.success('策略参数已保存')
  } catch (e) {
    strategySubmitting.value = false
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value = err?.response?.data?.detail || err?.message || '保存策略失败'
  }
}

/** 网格：只保存策略，不发信号 */
async function saveGridStrategyOnly(): Promise<void> {
  const gpErr = validateGridParams()
  if (gpErr) { lastStrategyError.value = gpErr; return }
  // 保存前等一次试算落定，保证写入的 grid_count / lot_per_grid 是按当前参数算出来的
  await triggerGridSizing(false)
  const rules = buildGridStrategyPayload()
  if (!rules) return
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    let strategyId = currentStrategy.value?.strategy_id
    if (strategyId) {
      await hub.updateStrategy(strategyId, {
        name: currentStrategy.value!.name,
        symbol: (localForm.symbol || '').trim().toUpperCase(),
        rules,
      })
    } else {
      const tpl3 = templates.value.find((t) => t.template_id === 'tpl_3')
      const gridName = `${currentGroup.value.name}-grid-策略`
      const before = [...hub.strategies]
      let existed: StrategyOutType | undefined
      try {
        await hub.fetchStrategies({ q: gridName })
        existed = (hub.strategies || []).find(
          (s) => s.name === gridName && s.owner_user_id === currentGroup.value?.owner_user_id,
        )
      } finally {
        hub.strategies = before
      }
      if (existed) {
        await hub.updateStrategy(existed.strategy_id, {
          name: gridName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = existed.strategy_id
        await hub.updateGroup(currentGroup.value.group_id, { strategy_id: existed.strategy_id })
      } else {
        const created = await hub.createStrategy({
          template_id: tpl3?.template_id ?? '',
          name: gridName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = created.strategy_id
        if (strategyId) {
          await hub.updateGroup(currentGroup.value.group_id, { strategy_id: strategyId })
        }
      }
    }
    strategySubmitting.value = false
    emit('update:modelValue', false)
    ElMessage.success('网格策略参数已保存')
  } catch (e) {
    strategySubmitting.value = false
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value = err?.response?.data?.detail || err?.message || '保存网格策略失败'
  }
}

/**
 * 跟单节点继承：把本带单员其它带单源分组里**已存在**的跟单节点一并写入当前分组。
 *
 * 分组名按 `${策略类型}-${用户名}-${MT5号}-${品种}` 生成，任一维度变化即全新分组，
 * 所以换品种 / 换策略类型都会走到这里（跨策略类型继承）。
 *
 * 三条关键口径：
 * - **必须等 strategy_id 绑上**：跟单关系对没有策略的分组没有意义，
 *   而 loadGroupAndStrategy 建组阶段 strategy_id 还是 null。
 * - **无条件调用，不看 carry 开关**：分组成员是持久状态，carry 只管单条信号的投递范围；
 *   与 limit_watch / signal_concurrent 的无条件 PATCH 同一口径。
 *   幂等，顺带修复「功能上线前已存在但无跟单节点」的存量分组。
 * - **失败不阻断**：信号照常下发（主节点正常开仓，跟单节点这轮不跟），仅提示。
 */
async function syncFollowersIntoGroup(): Promise<void> {
  if (!currentGroup.value) return
  const groupId = currentGroup.value.group_id
  try {
    const r = await hub.adoptFollowers(groupId)
    if (r.adopted?.length) {
      const refreshed = (await hub.listGroups()).find((g) => g.group_id === groupId)
      if (refreshed) currentGroup.value = refreshed
    }
  } catch (e) {
    // 继承失败不影响信号下发：仅给用户提示，不抛出
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value =
      err?.response?.data?.detail || err?.message || '跟单节点同步失败，信号已照常下发'
  }
}

/**
 * 市价开仓专用：先保存策略（含 4 个 riskParams），再发信号。
 * signal payload 的 volume 来自策略反算（由后端根据 risk_amount / sl 算出），
 * 所以 signal payload 里不填 volume，让后端取策略里的 risk_amount。
 */
async function triggerMarketStrategy(): Promise<void> {
  const rpErr = validateRiskParams()
  if (rpErr) { lastStrategyError.value = rpErr; return }
  const rules = buildRiskStrategyPayload()
  if (!rules) return
  // 限价 / 市价首次触发时 props.groupId 可能为 null（父级按 groupName 找不到现成分组），
  // loadGroupAndStrategy 已经按 props.groupName 自动建好 currentGroup；这里兜底再确认一次。
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    let strategyId = currentStrategy.value?.strategy_id
    if (strategyId) {
      // 已存在策略：PATCH 更新（限价/市价各用各的 groupName，绑定的 strategy 不会互相污染）
      await hub.updateStrategy(strategyId, {
        name: currentStrategy.value!.name,
        symbol: (localForm.symbol || '').trim().toUpperCase(),
        rules,
      })
    } else {
      // 新策略：POST 创建。命名带 strategyType 前缀，避免市价/限价/网格之间重名冲突
      // 若同名已存在（owner 维度），改 PATCH 覆盖，避免 409
      const tpl2 = templates.value.find((t) => t.template_id === 'tpl_2')
      const newName = `${currentGroup.value.name}-${strategyRef.value}-策略`
      const before = [...hub.strategies]
      let existed: StrategyOutType | undefined
      try {
        await hub.fetchStrategies({ q: newName })
        existed = (hub.strategies || []).find(
          (s) => s.name === newName && s.owner_user_id === currentGroup.value?.owner_user_id,
        )
      } finally {
        hub.strategies = before
      }
      if (existed) {
        await hub.updateStrategy(existed.strategy_id, {
          name: newName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = existed.strategy_id
        await hub.updateGroup(currentGroup.value.group_id, { strategy_id: existed.strategy_id })
      } else {
        const created = await hub.createStrategy({
          template_id: tpl2?.template_id ?? '',
          name: newName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = created.strategy_id
        // 绑定新策略到分组（用 currentGroup.group_id，而非 props.groupId；
        // 父级在首次开仓时可能还没拉到 group_id）
        if (strategyId) {
          await hub.updateGroup(currentGroup.value.group_id, { strategy_id: strategyId })
        }
      }
    }
    // 限价开仓：补一次"打开限价监听" PATCH。
    // 必须在 strategy_id 绑上之后才能开（后端 _require_trend_strategy 会校验）。
    // loadGroupAndStrategy 阶段 strategy_id 还是 null，故监听开关当时不能传。
    // 已存在的分组若已开启，此 PATCH 是幂等的；用户手动关过监听则会被重新打开，
    // 这是"首次限价开仓自动准备"的语义，符合预期。
    if (isLimit.value && currentGroup.value) {
      const groupId = currentGroup.value.group_id
      const memberIds = (currentGroup.value.nodes || []).map((n) => n.node_id)
      try {
        await hub.updateGroup(groupId, {
          limit_watch_enabled: true,
          limit_watch_keyword: '',
          limit_watch_node_ids: memberIds,
        })
        const refreshed = (await hub.listGroups()).find((g) => g.group_id === groupId)
        if (refreshed) currentGroup.value = refreshed
      } catch (e) {
        // 监听开关失败不影响信号下发：仅给用户提示，不抛出
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '打开限价监听失败'
      }
    }
    // 信号并发：策略建/绑成功后自动开启（市价/限价用模版2，网格用模版3，均支持并发）。
    // 必须在 strategy_id 绑上之后才能开（update_group:346 会校验策略类型）。
    // 已存在的分组若已开启，此 PATCH 是幂等的；用户手动关过则会被重新打开。
    if (currentGroup.value) {
      const groupId = currentGroup.value.group_id
      try {
        await hub.updateGroup(groupId, { signal_concurrent_enabled: true })
        const refreshed = (await hub.listGroups()).find((g) => g.group_id === groupId)
        if (refreshed) currentGroup.value = refreshed
      } catch (e) {
        // 信号并发失败不影响信号下发：仅给用户提示，不抛出
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '打开信号并发失败'
      }
    }
    // 跟单节点继承（跨策略类型 / 品种）：strategy_id 已绑上，此时才认领跟单节点。
    await syncFollowersIntoGroup()
    // 发信号：市价/限价走 strategy 模型，action 由前端推导。
    // 限价时传 entry_price（后端会转成 limit_price，用于到价触发）。
    const action = derivedAction.value!
    const payload: ManualSignalPayload = {
      symbol: (localForm.symbol || '').trim().toUpperCase(),
      action,
      model: 'strategy',
      group_ids: [currentGroup.value.group_id],
    }
    if (localForm.sl && localForm.sl > 0) payload.stop_loss = localForm.sl
    // 止盈价：随信号带过去（后台会落库）。以损定量策略实际是按 rr_ratio 铺
    // 阶梯止盈的，所以前端已经把 rr_ratio 同步成「止盈价反算出来的盈亏比」，
    // 最远一档正好落在客户填的止盈价上。
    if (localForm.tp && localForm.tp > 0) payload.take_profit = localForm.tp
    if (isLimit.value && localForm.entry_price && localForm.entry_price > 0) {
      payload.entry_price = localForm.entry_price
    }
    // 带单关闭时，信号只推给主节点（不发给分组的其它成员，包括跟单节点）。
    // 没有主节点就不带「node_ids」——按分组参与者处理，让用户报警，而不是悄悄不给主节点。
    if (!carry.value && primaryNode.value) {
      payload.node_ids = [primaryNode.value.node_id]
    }
    const remark = (localForm.remark || '').trim()
    if (remark) payload.comment = remark
    normalizeVolume()
    payload.volume = localForm.volume
    console.log(payload)
    emit('trigger-strategy', payload)
  } catch (e) {
    strategySubmitting.value = false
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value = err?.response?.data?.detail || err?.message || '保存策略失败'
  }
}

/**
 * 网格策略触发：保存网格规则（type=4）后发 signal。
 */
async function triggerGridStrategy(): Promise<void> {
  const gpErr = validateGridParams()
  if (gpErr) { lastStrategyError.value = gpErr; return }
  // 保存前等一次试算落定，保证写入的 grid_count / lot_per_grid 是按当前参数算出来的
  await triggerGridSizing(false)
  const rules = buildGridStrategyPayload()
  if (!rules) return
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    let strategyId = currentStrategy.value?.strategy_id
    if (strategyId) {
      await hub.updateStrategy(strategyId, {
        name: currentStrategy.value!.name,
        symbol: (localForm.symbol || '').trim().toUpperCase(),
        rules,
      })
    } else {
      const tpl3 = templates.value.find((t) => t.template_id === 'tpl_3')
      const gridName = `${currentGroup.value.name}-grid-策略`
      // 同名策略已存在（owner 维度）则改为 PATCH，避免 409
      // 加载一次按名字过滤的列表（保存前后还原缓存，不污染 hub.strategies）
      const before = [...hub.strategies]
      let existed: StrategyOutType | undefined
      try {
        await hub.fetchStrategies({ q: gridName })
        existed = (hub.strategies || []).find(
          (s) => s.name === gridName && s.owner_user_id === currentGroup.value?.owner_user_id,
        )
      } finally {
        hub.strategies = before
      }
      if (existed) {
        await hub.updateStrategy(existed.strategy_id, {
          name: gridName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = existed.strategy_id
        await hub.updateGroup(currentGroup.value.group_id, { strategy_id: existed.strategy_id })
      } else {
        const created = await hub.createStrategy({
          template_id: tpl3?.template_id ?? '',
          name: gridName,
          symbol: (localForm.symbol || '').trim().toUpperCase(),
          rules,
        })
        strategyId = created.strategy_id
        if (strategyId) {
          await hub.updateGroup(currentGroup.value.group_id, { strategy_id: strategyId })
        }
      }
    }
    // 信号并发：网格用模版3（支持并发），策略建/绑成功后自动开启。
    // 已存在的分组若已开启，此 PATCH 是幂等的。
    if (currentGroup.value) {
      try {
        await hub.updateGroup(currentGroup.value.group_id, { signal_concurrent_enabled: true })
        const refreshed = (await hub.listGroups()).find(
          (g) => g.group_id === currentGroup.value!.group_id,
        )
        if (refreshed) currentGroup.value = refreshed
      } catch (e) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value =
          err?.response?.data?.detail || err?.message || '打开信号并发失败'
      }
    }
    // 跟单节点继承（跨策略类型 / 品种）：strategy_id 已绑上，此时才认领跟单节点。
    await syncFollowersIntoGroup()
    // 网格信号：方向由策略内的 grid_side 决定；payload 顶层 action 必须是大写
    // （后端 console_api._build_manual_signal 会校验 BUY / SELL / CLOSE）
    // 顶层 volume 后端会强制要求显式传入（每格手数已在 strategy.rules 内由策略链路消费）
    const gridAction: ManualSignalAction = derivedGridSide.value === 'short' ? 'SELL' : 'BUY'
    const payload: ManualSignalPayload = {
      symbol: (localForm.symbol || '').trim().toUpperCase(),
      action: gridAction,
      model: 'strategy',
      group_ids: [currentGroup.value.group_id],
      volume: 0.01,
    }
    // 带单关闭时，网格信号也只推给主节点
    if (!carry.value && primaryNode.value) {
      payload.node_ids = [primaryNode.value.node_id]
    }
    const remark = (localForm.remark || '').trim()
    if (remark) payload.comment = remark
    console.log(payload)
    emit('trigger-strategy', payload)
  } catch (e) {
    strategySubmitting.value = false
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value = err?.response?.data?.detail || err?.message || '保存网格策略失败'
  }
}

/** 父级调用 strategy 回写结果 */
function onStrategyResult(res: ManualSignalResult): void {
  strategySubmitting.value = false
  // 网格方向由止损价推导（derivedAction 只覆盖市价/限价），这里按模式取正确的动作
  const action: ManualAction = isGrid.value
    ? derivedGridSide.value === 'short'
      ? 'SELL'
      : 'BUY'
    : derivedAction.value ?? 'BUY'
  reportStrategyResult(res, action, (localForm.symbol || '').toUpperCase())
}
/** 父级调用 strategy 回写错误 */
function onStrategyError(message: string): void {
  strategySubmitting.value = false
  lastStrategyError.value = message
}

defineExpose({ onStrategyResult, onStrategyError })

/** 与 StrategyFormModal.reportGroupTrigger 行为对齐的轻量版提示 */
function reportStrategyResult(res: ManualSignalResult, action: string, symbol: string): void {
  const head = `${action} ${symbol}`
  if (res.status === 'accepted') {
    const groups = res.groups ?? 0
    const targets = res.targets ?? 0
    if (targets > 0) {
      ElMessage.success(`已触发 ${head}：命中 ${groups} 个分组，${targets} 个节点任务开始执行`)
    } else {
      const taskRows = Array.isArray(res.tasks) ? res.tasks : []
      const reasons = [
        ...new Set(taskRows.map((t) => (t.reason || '').trim()).filter(Boolean)),
      ]
      const hint = reasons.length ? reasons.join('；') : '分组无有效节点，或未能下发到任何节点'
      ElMessage.warning(`${head} 已受理但未下发：命中 ${groups} 个分组（${hint}）`)
    }
    return
  }
  if (res.status === 'duplicate') {
    ElMessage.warning(`重复信号被抑制：5 秒内已有相同参数的 ${head} 策略信号`)
    return
  }
  if (res.status === 'rejected') {
    lastStrategyError.value = res.reason || '信号被拒收'
    return
  }
  ElMessage.info(`已提交：${res.status}`)
}
</script>

<template>
  <ElDialog
    v-model="visible"
    width="760px"
    :close-on-click-modal="false"
    :show-close="false"
    align-center
    class="order-dialog"
    :style="{ '--order-radius': '16px' }"
  >
    <template #header>
      <div class="od-header">
        <div class="od-title">
          <span class="od-title-main">
            创建交易
            <span class="od-title-dot">·</span>
            <span class="od-title-sym">{{ (localForm.symbol || '').toUpperCase() }}</span>
          </span>
          <span class="od-title-sub">开仓参数配置</span>
        </div>
        <button class="od-close" aria-label="关闭" @click="close" title="关闭">×</button>
      </div>
      <div class="od-tabs" role="tablist">
        <button
          v-for="s in STRATEGIES"
          :key="s.key"
          class="od-tab"
          :class="{ active: strategy === s.key }"
          role="tab"
          :aria-selected="strategy === s.key"
          @click="strategyRef = s.key"
        >
          <span class="od-tab-icon" aria-hidden="true">
            <svg v-if="s.key === 'trend_market'" viewBox="0 0 24 24"><path fill="currentColor" d="M11 21h-1l1-7H7.5c-.58 0-.57-.32-.38-.66l.07-.12C8.48 10.94 10.42 7.54 13 3h1l-1 7h3.5c.49 0 .56.33.47.51l-.07.15C12.96 17.55 11 21 11 21z"/></svg>
            <svg v-else-if="s.key === 'trend_limit'" viewBox="0 0 24 24"><path fill="currentColor" d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>
            <svg v-else viewBox="0 0 24 24"><path fill="currentColor" d="M21 8c-1.45 0-2.26 1.44-1.93 2.51l-3.55 3.56c-.3-.09-.74-.09-1.04 0l-2.55-2.55C12.27 10.45 11.46 9 10 9c-1.45 0-2.27 1.44-1.93 2.52l-4.56 4.55C2.44 15.74 1 16.55 1 18c0 1.1.9 2 2 2 1.45 0 2.26-1.44 1.93-2.51l4.55-4.56c.3.09.74.09 1.04 0l2.55 2.55C12.73 16.55 13.54 18 15 18c1.45 0 2.27-1.44 1.93-2.52l3.56-3.55C22.56 12.26 24 11.45 24 10c0-1.1-.9-2-2-2z"/></svg>
          </span>
          <span class="od-tab-label">{{ s.label }}</span>
        </button>
      </div>
    </template>

    <div class="od-body">
      <!-- 基础信息（分组）：非市价模式才显示。市价开仓的分组信息在底部 footer 已通过 groupName / 触发按钮体现 -->
      <!-- <div v-if="!isMarket && !isLimit" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label">分组</div>
          <div class="od-row-sub">执行此次开仓的目标分组</div>
        </div>
        <div class="od-row-control readonly" :class="{ muted: !groupId }">
          {{ groupName }}
          <span v-if="!groupId" class="od-hint-inline">（未在分组页创建）</span>
        </div>
      </div> -->

      <!-- 方向（市价/限价 由后端按 SL vs 当前价自动判断，UI 不展示此行；网格仍需手动选） -->
      <!-- <div v-if="isGrid" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label required">方向（必填）</div>
          <div class="od-row-sub">网格策略只做多/只做空</div>
        </div>
        <div class="od-row-control">
          <select v-model="localForm.direction" class="od-select">
            <option value="all">多头 all</option>
            <option value="buy">多头 buy</option>
            <option value="sell">空头 sell</option>
          </select>
        </div>
      </div> -->

      <!-- 开仓参数（网格模式隐藏整个节；市价/限价均显示） -->
      <div
        v-if="!isGrid"
        class="od-section"
        :style="{ borderLeftColor: isLimit ? '#F59E0B' : '#2563EB' }"
      >
        {{ isLimit ? '限价开仓参数' : '市价开仓参数' }}
      </div>

      <!-- 品种（非市价才显示输入框） -->
      <!-- <div v-if="!isMarket && !isLimit" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label">开仓品种</div>
          <div class="od-row-sub">如 XAUUSD / EURUSD</div>
        </div>
        <div class="od-row-control">
          <input v-model="localForm.symbol" class="od-input" placeholder="如 XAUUSD" />
        </div>
      </div> -->

      <!-- 手数（非市价才显示；市价开仓手数由策略的 risk_amount / sl 反算） -->
      <!-- <div v-if="!isMarket && !isLimit" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label">开仓手数</div>
          <div class="od-row-sub">0.01 手起，按 0.01 步进</div>
        </div>
        <div class="od-row-control od-stepper">
          <button class="od-step" type="button" @click="stepVolume(-0.01)">−</button>
          <input v-model.number="localForm.volume" class="od-input center" type="number" step="0.01" min="0.01" />
          <button class="od-step" type="button" @click="stepVolume(0.01)">+</button>
        </div>
      </div> -->

      <template v-if="!isGrid">
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">以损定量</div>
            <div class="od-row-sub">按单笔最大亏损额计算手数</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="riskParams.risk_amount" class="od-input" type="number" min="1" step="10" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">底仓比例</div>
            <div class="od-row-sub">底仓占总仓位比例</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="riskParams.base_ratio" class="od-input" type="number" min="1" max="100" step="1" />
            <span class="od-affix">%</span>
          </div>
        </div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">盈亏比</div>
            <div class="od-row-sub">
              止盈与止损的比例，如 1:2.5
              <template v-if="impliedRr != null">
                <br /><span class="od-sub-strong">按止盈价推算为 1:{{ impliedRr }}</span>
              </template>
            </div>
          </div>
          <div class="od-row-control od-input-affix">
            <input
              v-model.number="riskParams.rr_ratio"
              class="od-input"
              type="number"
              min="0"
              step="0.1"
              placeholder="如 2.5"
              @input="rrManualEdited = true"
            />
          </div>
        </div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">保本触发</div>
            <div class="od-row-sub">价格达到止损距离的几倍后，将止损移至保本价</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="riskParams.breakeven_times" class="od-input" type="number" min="0" step="0.5" />
            <span class="od-affix">倍</span>
          </div>
        </div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">止盈价格</div>
            <div class="od-row-sub">开仓后挂出的止盈价格，默认不填</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="localForm.tp" class="od-input" type="number" step="0.0001" min="0" placeholder="如 4250.00" />
          </div>
        </div>
        <div v-if="isLimit" class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">挂单价格<span class="od-req">必选</span></div>
            <div class="od-row-sub">限价单触发开仓的委托价格</div>
          </div>
          <div class="od-row-control od-input-affix" :class="{ need: !localForm.entry_price }">
            <input v-model.number="localForm.entry_price" class="od-input" type="number" step="0.0001" min="0" placeholder="如 4150.00" />
          </div>
        </div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">止损价格<span class="od-req">必选</span></div>
            <div class="od-row-sub">开仓后挂出的止损价格</div>
          </div>
          <div class="od-row-control od-input-affix" :class="{ need: !localForm.sl }">
            <input v-model.number="localForm.sl" class="od-input" type="number" step="0.0001" min="0" placeholder="如 4140.00" />
          </div>
        </div>

        <!-- 止盈价提示：填了止盈价后，实际盈亏比已被止盈价「重新定义」，
             这里明确告诉客户当前用的是哪一个，以及不一致时可一键改用。 -->
        <div v-if="rrNotice" class="od-notice" :class="`is-${rrNotice.kind}`">
          <span class="od-notice-text">{{ rrNotice.text }}</span>
          <button
            v-if="rrNotice.action && rrNotice.actionLabel"
            type="button"
            class="od-notice-btn"
            @click="rrNotice.action()"
          >
            {{ rrNotice.actionLabel }}
          </button>
        </div>
      </template>

      <!-- 网格策略参数 -->
      <template v-if="isGrid">
        <div class="od-section" style="border-left-color: #22C55E">智能吸筹参数</div>
        <!-- 网格方向：由「止损价 vs 价格区间」自动判定，只读展示，客户不需要选 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">网格方向</div>
            <div class="od-row-sub">由止损价位置自动判定</div>
          </div>
          <div class="od-row-control readonly" :class="{ muted: !derivedGridSide }">
            {{ derivedGridSideLabel }}
          </div>
        </div>
        <!-- 区间下限 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">价格下限<span class="od-req">必选</span></div>
            <div class="od-row-sub">
              {{ derivedGridSide === 'short' ? '网格的最低价位（跌到此处平空兑现）' : '网格的最低价位（跌到此处开始买入）' }}
            </div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.price_lower" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3000.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <!-- 区间上限 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">价格上限<span class="od-req">必选</span></div>
            <div class="od-row-sub">
              {{ derivedGridSide === 'short' ? '网格的最高价位（涨到此处开空）' : '网格的最高价位（涨到此处卖出兑现）' }}
            </div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.price_upper" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3300.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <!-- 止损价（决定网格方向：低于区间下限=只做多，高于区间上限=只做空） -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">止损价格<span class="od-req">必选</span></div>
            <div class="od-row-sub">
              填在价格区间下方 → 只做多；填在价格区间上方 → 只做空
            </div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.stop_price" class="od-input" type="number" step="0.0001" min="0" placeholder="如 2950.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>

        <!-- 最大可接受亏损 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">以损定量</div>
            <div class="od-row-sub">按单笔最大亏损额计算手数</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.max_allow_loss" class="od-input" type="number" step="10" min="0" placeholder="如 100" />
            <span class="od-affix">USD</span>
          </div>
        </div>

        <!--
          自动试算（后台执行，结果回写 grid_count / lot_per_grid）：
          按需求不对客户展示 —— 网格数量 / 网格模式 / 每格手数 / 初始建仓 /
          触发价 / 止盈价 / 向上追踪 全部隐藏，参数由试算与默认值给出。
          试算失败时给一行提示，避免客户以为手数是按「以损定量」算出来的。
        -->
        <div v-if="gridSizing.error" class="od-error">
          {{ gridSizing.error }}
        </div>
      </template>

      <!-- 策略参数（市价/限价均走 strategy 模型，按 riskParams 实时计算手数）
           渲染条件：市价或限价开仓 + 有 currentGroup。
           不再要求「策略复用」开关开启——开了就是走 strategy 模型，
           没开就只展示参数，不影响原 quickTrigger 路径。
           「策略复用」开关决定提交时是 save 策略 + 触发 strategy 信号，
           还是仅用参数走 quickTrigger。-->
      <div v-if="(isMarket || isLimit) && strategyLoading" class="od-loading">
        <span class="od-loading-dot"></span>
        <span class="od-loading-dot"></span>
        <span class="od-loading-dot"></span>
        加载策略数据…
      </div>

      <!-- 网格策略参数（走 strategy 模型） -->
      <template v-if="isGrid && currentGroup">
        <div v-if="strategyLoading" class="od-loading">
          <span class="od-loading-dot"></span>
          <span class="od-loading-dot"></span>
          <span class="od-loading-dot"></span>
          加载策略数据…
        </div>
        <template v-else>
          <div v-if="!currentGroup" class="od-error">
            分组未就绪，请稍后重试
          </div>
        </template>
      </template>

      <!-- 备注（市价模式隐藏，limit/grid 仍展示） -->
      <!-- <div v-if="!isMarket && !isLimit" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label">备注</div>
          <div class="od-row-sub">可选，64 字以内</div>
        </div>
        <div class="od-row-control">
          <input v-model="localForm.remark" class="od-input" maxlength="64" placeholder="可选" />
        </div>
      </div> -->

      <div v-if="lastError || lastStrategyError" class="od-error">
        {{ lastError || lastStrategyError }}
      </div>
      <!-- <div v-else class="od-hint">提示：首次触发会按 (策略类型-用户名-MT5 登录号-品种) 自动创建「分组+策略」并把主节点加入；后续直接复用。</div> -->

      <!-- 底部脚注（原型 Oh 的 parameter 摘要行）-->
      <p class="od-footnote">
        {{ STRATEGY_LABEL[strategy] }} · 以损定量 {{ isGrid ? gridParams.max_allow_loss : riskParams.risk_amount }} USD
      </p>
    </div>

    <template #footer>
      <div class="od-footer">
        <!-- 「带单」开关：开启 → 信号下发到分组全部节点（含跟单员）；关闭 → 仅主节点。 -->
        <div v-if="canCarry" class="od-carry">
          <span class="od-carry-label">带单</span>
          <button
            type="button"
            class="pf-switch"
            :class="{ on: carry }"
            role="switch"
            :aria-checked="carry"
            aria-label="带单"
            @click="carry = !carry"
          ></button>
        </div>
        <!-- 暂停提示：主人点了「暂停带单 / 暂停 AI 跟单」后，
             这个开关打开也不会下发给跟单节点 —— 收窄在**服务端**做，
             覆盖 webhook / 限价监听等所有触发路径，前端开关绕不过去。
             这里说明清楚，免得主人以为开关失灵。 -->
        <div v-if="canCarry && pausedInMarket" class="od-carry-note">
          你已暂停{{ pausedInMarket }}，本次信号仍只下发主节点。
        </div>
        <div class="od-actions">
          <!-- 市价 / 限价：统一走 triggerStrategy（strategy 模型），前端推导 BUY/SEL方向并展示 -->
          <template v-if="isMarket || isLimit">
            <!-- 仅保存模式 -->
            <button
              v-if="isSaveMode"
              class="od-btn-primary"
              :disabled="!canTriggerStrategy || strategySubmitting"
              @click="saveStrategyOnly"
            >
              <span class="od-btn-primary-main">{{ strategySubmitting ? '保存中…' : '保存' }}</span>
            </button>
            <!-- 触发模式 -->
            <button
              v-else
              class="od-btn-primary"
              :disabled="!canTriggerStrategy"
              @click="triggerStrategy"
            >
              <span class="od-btn-primary-main">{{ strategySubmitting ? '提交中…' : STRATEGY_LABEL[strategy] }}</span>
            </button>
          </template>
          <!-- 网格：走 strategy 模型，与市价/限价统一 -->
          <template v-else>
            <!-- 仅保存模式 -->
            <button
              v-if="isSaveMode"
              class="od-btn-primary"
              :disabled="!canTriggerGridStrategy || strategySubmitting"
              @click="saveStrategyOnly"
            >
              <span class="od-btn-primary-main">{{ strategySubmitting ? '保存中…' : '保存' }}</span>
            </button>
            <!-- 触发模式 -->
            <button
              v-else
              class="od-btn-primary"
              :disabled="!canTriggerGridStrategy || strategySubmitting"
              @click="triggerStrategy"
            >
              <span class="od-btn-primary-main">{{ strategySubmitting ? '提交中…' : STRATEGY_LABEL[strategy] }}</span>
            </button>
          </template>
        </div>
      </div>
    </template>
  </ElDialog>
</template>

<style>
/* 不加 scoped：覆盖 el-dialog 内部结构 */

/* 整个 dialog 内的边框统一淡化、透明感更强 */
.order-dialog {
  --el-border-color-lighter: rgba(34, 48, 74, 0.6);
  --el-border-color: rgba(34, 48, 74, 1);
  --el-bg-color: #0d1626;
  --el-bg-color-overlay: #0d1626;
  --el-text-color-primary: #e6ebf4;
  --el-text-color-regular: #b8c4d9;
  --el-text-color-secondary: #8b96a8;
  --el-fill-color-blank: #0a0f1a;
  --el-fill-color-light: #16213a;
  --el-color-primary: #2563eb;
}
.order-dialog .el-dialog__header {
  padding: 14px 16px 12px;
  margin-right: 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.order-dialog .el-dialog__body {
  padding: 12px 16px 0;
}
.order-dialog .el-dialog__footer {
  padding: 12px 16px 14px;
  border-top: 1px solid var(--el-border-color-lighter);
  background: transparent;
}
.order-dialog .el-dialog {
  width: min(760px, calc(100vw - 32px)) !important;
  border-radius: 16px;
  overflow: hidden;
  max-height: 88vh;
  margin: 6vh auto;
  background: #0d1626;
  border: 1px solid #1e2b47;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}
.order-dialog .el-dialog__body {
  max-height: calc(100vh - 200px);
  overflow-y: auto;
}
.order-dialog .el-overlay {
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(2px);
}

/* ── 手机端：撑满全屏、贴顶贴底、无圆角 ── */
@media (max-width: 600px) {
  .order-dialog {
    margin: 0 !important;
  }
  .order-dialog .el-dialog {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100vh !important;
    max-height: 100vh !important;
    border-radius: 0 !important;
    margin: 0 !important;
  }
  .order-dialog .el-dialog__body {
    max-height: calc(100vh - 168px);
  }
}
</style>

<style scoped>
/* ── Header ── */
.od-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.od-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.od-title-main {
  font-size: 14px;
  font-weight: 650;
  color: #E6EBF4;
}
.od-title-dot { color: #5B6B85; margin: 0 4px; }
.od-title-sym { color: #3B82F6; }
.od-title-sub {
  font-size: 10px;
  color: #5B6B85;
}
.od-close {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: #0a0f1a;
  color: var(--pf-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s, transform 0.1s;
  flex-shrink: 0;
  font-size: 14px;
}
.od-close:hover {
  background: #16213a;
  color: var(--pf-text);
  border-color: #2b3a5c;
}
.od-close:active { transform: scale(0.94); }
.od-close:focus-visible {
  outline: 2px solid rgba(37, 99, 235, 0.45);
  outline-offset: 2px;
}

/* ── Body ── */
.od-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-bottom: 6px;
}

/* ── Tabs（原型：三个等宽分段按钮）── */
.od-tabs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.od-tab {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  height: 44px;
  border: 1px solid #22304A;
  background: #0A0F1A;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 650;
  color: #8B96A8;
  cursor: pointer;
}
.od-tab:hover:not(.active) {
  border-color: #2b3a5c;
  color: var(--pf-text);
}
.od-tab.active {
  background: var(--pf-blue);
  border-color: transparent;
  color: #fff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.4);
}
.od-tab-icon { display: flex; width: 16px; height: 16px; }
.od-tab-icon svg { width: 16px; height: 16px; }
.od-tab-label { letter-spacing: 0.02em; }

/* ── Row（label 左 / control 右）── */
.od-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid #16213a;
}
.od-row:last-child { border-bottom: none; }
.od-row-text { min-width: 0; flex: 1 1 auto; }
.od-row-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--pf-text);
  line-height: 1.3;
}
.od-req {
  margin-left: 6px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.15);
  color: #FBBF24;
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
  padding: 2px 4px;
  vertical-align: 1px;
}
.od-row-sub {
  font-size: 10px;
  color: var(--pf-dim);
  margin-top: 2px;
  line-height: 1.5;
}
.od-row-control {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  min-width: 0;
  gap: 6px;
}
.od-row-control.readonly {
  padding: 6px 10px;
  background: #0a0f1a;
  border: 1px solid var(--pf-border-2);
  border-radius: 8px;
  font-size: 12px;
  color: var(--pf-text);
  font-family: var(--mono);
}
.od-row-control.muted { color: var(--pf-dim); }
.od-hint-inline {
  font-size: 10px;
  color: #f6465d;
  margin-left: 6px;
}

.od-select,
.od-input {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--pf-border-2);
  background: #0a0f1a;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--pf-text);
  outline: none;
  transition: border-color 0.15s;
  min-width: 0;
}
.od-input { width: 110px; }
.od-input.center { text-align: center; }
.od-select { width: 130px; }
.od-select:focus,
.od-input:focus { border-color: var(--pf-blue); }
.od-input::placeholder { color: var(--pf-faint); }

/* 单选按钮组（网格模式 / 网格方向） */
.od-seg {
  display: inline-flex;
  align-items: center;
  gap: 0;
  height: 32px;
  padding: 2px;
  background: #0a0f1a;
  border: 1px solid var(--pf-border-2);
  border-radius: 8px;
}
.od-seg-btn {
  appearance: none;
  border: 0;
  background: transparent;
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  color: var(--pf-muted);
  padding: 0 10px;
  height: 100%;
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
  white-space: nowrap;
}
.od-seg-btn:hover:not(.active) { color: var(--pf-text); }
.od-seg-btn.active {
  background: var(--pf-blue);
  color: #fff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.35);
}
.od-seg-btn:focus-visible {
  outline: 2px solid rgba(37, 99, 235, 0.35);
  outline-offset: 1px;
}

/* ── 网格试算提示 ── */
.od-grid-sizing {
  margin: 2px 0 8px;
  padding: 8px 10px;
  background: rgba(37, 99, 235, 0.08);
  border: 1px solid rgba(37, 99, 235, 0.25);
  border-radius: 8px;
  font-size: 10px;
  color: var(--pf-muted);
}
.od-grid-sizing-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 18px;
}
.od-grid-sizing-state {
  flex: 1 1 auto;
  line-height: 1.5;
  word-break: break-word;
}
.od-grid-sizing-state.muted { color: var(--pf-dim); }
.od-grid-sizing-state.ok { color: #4ade80; }
.od-grid-sizing-state.warn { color: #fbbf24; }
.od-grid-sizing-reload {
  flex: 0 0 auto;
  appearance: none;
  border: 0;
  background: transparent;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  color: var(--pf-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, color 0.15s;
}
.od-grid-sizing-reload:hover:not(:disabled) {
  background: rgba(37, 99, 235, 0.15);
  color: #60a5fa;
}
.od-grid-sizing-reload:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}
.od-reload.spin {
  display: inline-block;
  animation: od-reload-spin 0.8s linear infinite;
}
@keyframes od-reload-spin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}
.od-grid-sizing-warns {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
  font-size: 10px;
  line-height: 1.5;
}
.od-grid-sizing-warns li.lvl-warn { color: #fbbf24; }
.od-grid-sizing-warns li.lvl-info { color: var(--pf-dim); }
/* ── 数字输入 + 单位（原型 Yt：h-8 / 右对齐 / 等宽数字）── */
.od-input-affix {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px 0 0;
  background: #0a0f1a;
  border: 1px solid var(--pf-border-2);
  border-radius: 8px;
  transition: border-color 0.15s;
}
.od-input-affix:focus-within { border-color: #2563EB; }
.od-input-affix.need {
  border-color: #F59E0B;
  background: #1A1505;
  box-shadow: 0 0 12px rgba(245, 158, 11, 0.45);
}
.od-input-affix.need:focus-within {
  border-color: #FBBF24;
  box-shadow: 0 0 16px rgba(251, 191, 36, 0.6);
}
.od-input-affix.need .od-input { color: #FDE68A; }
.od-input-affix.need .od-input::placeholder { color: rgba(245, 158, 11, 0.5); }
.od-input-affix .od-input {
  border: none;
  outline: none;
  background: transparent;
  padding: 0 8px;
  height: 32px;
  flex: 1 1 auto;
  width: 96px;
  text-align: right;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--pf-text);
}
.od-affix {
  font-size: 10px;
  color: var(--pf-dim);
  letter-spacing: 0.04em;
}

.od-stepper {
  display: inline-flex;
  align-items: center;
  gap: 0;
  border: 1px solid var(--pf-border-2);
  border-radius: 8px;
  overflow: hidden;
  background: #0a0f1a;
}
.od-step {
  width: 28px;
  height: 32px;
  border: none;
  background: transparent;
  font-size: 15px;
  font-weight: 600;
  color: var(--pf-muted);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.od-step:hover {
  background: #16213a;
  color: var(--pf-text);
}
.od-stepper .od-input {
  border: none;
  border-left: 1px solid var(--pf-border-2);
  border-right: 1px solid var(--pf-border-2);
  border-radius: 0;
  width: 64px;
  height: 32px;
  box-shadow: none;
  font-family: var(--mono);
  text-align: center;
}

/* ── Section header（原型：左侧色条 + 小标题）── */
.od-section {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--pf-text);
  letter-spacing: 0.02em;
  padding-left: 8px;
  border-left: 3px solid var(--pf-blue);
  margin-top: 4px;
}

/* ── Hint / Error ── */
.od-hint {
  font-size: 10px;
  color: var(--pf-dim);
  background: var(--pf-soft);
  border: 1px solid var(--pf-border-2);
  padding: 8px 10px;
  border-radius: 8px;
  line-height: 1.6;
}
.od-error {
  font-size: 11px;
  color: #f87171;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.4);
  padding: 8px 10px;
  border-radius: 8px;
  line-height: 1.5;
}

/* ── 止盈价 → 盈亏比联动提示 ── */
.od-notice {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  font-size: 11px;
  padding: 8px 10px;
  border-radius: 8px;
  line-height: 1.5;
}
.od-notice-text { flex: 1 1 auto; min-width: 0; }
.od-notice.is-info {
  color: #93c5fd;
  background: rgba(37, 99, 235, 0.12);
  border: 1px solid rgba(37, 99, 235, 0.35);
}
.od-notice.is-warn {
  color: #fbbf24;
  background: rgba(245, 158, 11, 0.12);
  border: 1px solid rgba(245, 158, 11, 0.4);
}
.od-notice.is-error {
  color: #f87171;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.4);
}
.od-notice-btn {
  flex: 0 0 auto;
  height: 24px;
  padding: 0 10px;
  border-radius: 6px;
  border: 1px solid currentColor;
  background: transparent;
  color: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
.od-notice-btn:hover { background: rgba(255, 255, 255, 0.08); }

/* 盈亏比副说明里的推算值 */
.od-sub-strong { color: var(--pf-text); font-weight: 600; }

/* Loading dots animation */
.od-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px;
  color: var(--pf-dim);
  font-size: 12px;
}
.od-loading-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--pf-blue);
  animation: od-bounce 1.4s ease-in-out infinite both;
}
.od-loading-dot:nth-child(1) { animation-delay: -0.32s; }
.od-loading-dot:nth-child(2) { animation-delay: -0.16s; }
@keyframes od-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

/* ── 底部脚注（原型 Oh 的参数摘要行）── */
.od-footnote {
  margin: 4px 0 0;
  padding-top: 10px;
  border-top: 1px solid #16213a;
  font-size: 10px;
  line-height: 1.6;
  color: var(--pf-faint);
}

/* ── Footer ── */
.od-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.od-carry {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: var(--pf-muted);
}
.od-carry-label { font-weight: 600; color: var(--pf-text); font-size: 12px; }
.od-carry-sub { font-size: 10px; }

/* 暂停提示：主人已暂停接单时，「带单」开关看着能开但实际只发主节点。
   摆在这里说清楚，免得主人以为开关失灵。 */
.od-carry-note {
  font-size: 10px;
  color: #fbbf24;
  line-height: 1.5;
  max-width: 220px;
}

.od-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex: 0 0 auto;
}

.od-btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 38px;
  padding: 0 22px;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 700;
  color: #ffffff;
  background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.4);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.15s, opacity 0.15s;
  white-space: nowrap;
}
.od-btn-primary:hover { box-shadow: 0 4px 14px rgba(37, 99, 235, 0.5); }
.od-btn-primary:active { transform: translateY(1px); }
.od-btn-primary:disabled { opacity: 0.55; cursor: not-allowed; box-shadow: none; }
.od-btn-primary-main { font-size: 13px; }
.od-btn-primary-sub {
  font-size: 10px;
  font-weight: 500;
  opacity: 0.85;
  letter-spacing: 0.02em;
  padding-left: 8px;
  border-left: 1px solid rgba(255, 255, 255, 0.35);
}
.od-btn-primary-sub.muted { opacity: 0.7; border-left-color: rgba(255, 255, 255, 0.2); }
</style>