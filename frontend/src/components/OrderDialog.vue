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
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
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
  NodeOut,
  OrderPreset,
  OrderPresetStrategyType,
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
  primaryNode?: NodeOut
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
   *   'preset'  - 点按钮只保存「开仓参数预设」，不发信号、不建分组/策略
   *              （【策略配置】页配置「我下单时的默认值」）
   */
  mode?: 'trigger' | 'save' | 'preset'
  /**
   * 开仓参数预设（我的下单默认值）。
   *
   * 语义：预设是**初始值**，不是强制值 —— 每次开仓时按预设填好，用户当场可改，
   * 但**改动不写回预设**（下次开仓仍回到预设值）。
   *
   * 父级可显式传入；不传时组件内部按 (symbol, strategyType) 从 hub.presets 解析
   * （精确品种优先，其次 '*' 通用模板）。
   */
  preset?: OrderPreset | null
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
      // 止盈价 → 盈亏比联动：重置手动标记与市价入场价快照
      rrManualEdited.value = false
      marketEntrySnapshot.value =
        props.currentMid != null && isFinite(props.currentMid) && props.currentMid > 0
          ? props.currentMid
          : null
      // 「保存预设」模式：不碰分组 / 策略实例（那会产生副作用），
      // 只拉预设列表并回填固定参数。
      if (isPresetMode.value) {
        await loadPresets()
        applyPresetToForm()
        // 默认落在「当前品种」：若已存在通用模板（'*'），默认落在通用模板，
        // 免得用户不小心把品种专属的参数覆盖到通用模板上。
        const sym = (localForm.symbol || '').trim().toUpperCase()
        const st = strategyRef.value as OrderPresetStrategyType
        const hasWildcard = hub.presets.some(
          (p) => p.symbol === '*' && p.strategy_type === st,
        )
        const hasExact = hub.presets.some(
          (p) => p.symbol === sym && p.strategy_type === st,
        )
        presetScope.value = hasWildcard && !hasExact ? 'wildcard' : 'symbol'
      } else {
        // 加载分组详情 + 策略模版 + 已有策略参数
        await loadGroupAndStrategy()
        // 预设回填（放在策略实例参数之后 —— 预设优先级更高）
        await loadPresets()
        applyPresetToForm()
      }
    }
  },
)

/** 「带单」开关：默认关闭（每笔新单都只发主节点），需要时手动打开 */
const carry = defineModel<boolean>('carry', { default: false })

const directionPrice = computed(() => isLimit.value ? localForm.entry_price : isMarket.value ? props.currentMid : null)
const stopEqualsEntry = computed(() => {
  const price = directionPrice.value
  return price != null && price > 0 && price === localForm.sl
})
const derivedAction = computed<ManualAction | null>(() => {
  const price = directionPrice.value
  const sl = localForm.sl
  if (price == null || price <= 0 || !Number.isFinite(price)) return null
  if (sl == null || sl <= 0 || !Number.isFinite(sl) || stopEqualsEntry.value) return null
  return sl < price ? 'BUY' : 'SELL'
})

/** 策略复用执行中的内部 loading（与 props.submitting 区分） */
const strategySubmitting = ref(false)
/** 策略复用执行结果 / 错误回显 */
const lastStrategyError = ref('')

/** 是否仅保存模式：点按钮只保存策略，不发信号 */
const isSaveMode = computed(() => props.mode === 'save')

/**
 * 是否「保存预设」模式：只写开仓参数预设，不建分组、不建/改策略实例、不发信号。
 */
const isPresetMode = computed(() => props.mode === 'preset')

/**
 * 当前生效的预设：父级显式传入的优先，否则按 (品种, 开仓方式) 从 hub.presets 解析。
 *
 * 解析口径与后端 `order_presets.resolve_preset` 一致：精确品种优先，其次 '*' 通用模板。
 */
const activePreset = computed<OrderPreset | null>(() => {
  if (props.preset) return props.preset
  const sym = (localForm.symbol || '').trim().toUpperCase()
  if (!sym) return null
  const st = strategyRef.value as OrderPresetStrategyType
  return hub.resolvePreset(sym, st)
})

/**
 * 「恢复预设」按钮是否可用：当前值与预设值不一致才有意义。
 *
 * 这是「预设是初始值」的心智补齐：会话里改乱了固定参数，一键回滚到预设值。
 */
const canRestorePreset = computed(() => {
  const p = activePreset.value
  if (!p) return false
  if (isGrid.value) {
    return (
      gridParams.grid_mode !== p.grid_mode ||
      Number(gridParams.max_allow_loss) !== p.max_allow_loss ||
      gridParams.trailing_up !== p.trailing_up
    )
  }
  return (
    Number(riskParams.risk_amount) !== p.risk_amount ||
    Number(riskParams.rr_ratio) !== p.rr_ratio ||
    Number(riskParams.base_ratio) !== p.base_ratio ||
    Number(riskParams.breakeven_times) !== p.breakeven_times
  )
})

/** 把固定参数恢复到预设值 */
function restorePreset(): void {
  const p = activePreset.value
  if (!p) return
  if (isGrid.value) {
    gridParams.grid_mode = p.grid_mode
    gridParams.max_allow_loss = p.max_allow_loss
    gridParams.trailing_up = p.trailing_up
  } else {
    riskParams.risk_amount = p.risk_amount
    riskParams.rr_ratio = p.rr_ratio
    riskParams.base_ratio = p.base_ratio
    riskParams.breakeven_times = p.breakeven_times
    // 恢复预设 = 重新采用预设的盈亏比，止盈价反算的联动重新生效
    rrManualEdited.value = false
  }
  ElMessage.success('已恢复为预设参数')
}

/**
 * 预设回填：把预设的固定参数写进表单。
 *
 * **只回填固定参数**，动态价一律留空（止损价 / 止盈价 / 挂单价；网格的区间与止损价）
 * —— 这些随行情变化，每次开仓现填。
 *
 * 调用时机：弹窗打开、策略实例参数加载完成之后（loadGroupAndStrategy 末尾）。
 * 放在其后是为了让「策略实例里的规则」先填好，预设再覆盖它 —— 预设优先级更高。
 */
function applyPresetToForm(): void {
  const p = activePreset.value
  if (!p) return
  if (isGrid.value) {
    gridParams.grid_mode = p.grid_mode
    gridParams.max_allow_loss = p.max_allow_loss
    gridParams.trailing_up = p.trailing_up
  } else {
    riskParams.risk_amount = p.risk_amount
    riskParams.rr_ratio = p.rr_ratio
    riskParams.base_ratio = p.base_ratio
    riskParams.breakeven_times = p.breakeven_times
    // 预设回填时 tp 尚未填（动态价留空），impliedRr 为 null，不会覆盖 rr_ratio
    rrManualEdited.value = false
  }
}

/**
 * 是否展示「带单」开关：交易弹窗里始终展示（保存模式不发信号，不展示）。
 * 关闭 = 只发给主节点；打开 = 发给分组里的全部节点（含跟单节点）。
 * follow_leader 能力决定的是「能不能出现在带单广场被别人跟」，不决定这个开关出不出现。
 */
const canCarry = computed(() => !isSaveMode.value && !isPresetMode.value)

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

/**
 * 每个开仓方式对应的策略模版 ID。
 *
 * 市价 / 限价共用模版2（趋势 · 以损定量），网格用模版3（网格交易）。
 * 与后端 `strategy_templates.TEMPLATE_RULE_TYPES` 一致。
 */
const STRATEGY_TEMPLATE_ID: Record<OrderStrategy, string> = {
  trend_market: 'tpl_2',
  trend_limit: 'tpl_2',
  grid: 'tpl_3',
}

/**
 * 当前开仓方式**应该**绑定的模版 ID。
 *
 * 用途：守卫「拿到的策略实例是不是这一路的策略」。
 * 三个开仓方式各自有独立的分组 / 策略实例（分组名带 strategy_type 前缀），
 * 一旦拿串了，用错模版的规则去 PATCH 会被后端按模版-规则一致性拒收
 * （如「策略模版 tpl_3 不允许规则类型 3」）。
 */
const expectedTemplateId = computed<string>(() => STRATEGY_TEMPLATE_ID[strategyRef.value])

/**
 * 策略实例的模版是否与当前开仓方式匹配。
 *
 * 三个开仓方式各自独立：市价/限价 → tpl_2（以损定量），网格 → tpl_3（网格）。
 * 模版决定该策略实例能接受哪些规则类型（后端 validate_rules_for_template 强校验），
 * 模版对不上就绝不能拿它去 PATCH 规则。
 */
function strategyTemplateMatches(strat: StrategyOutType | null | undefined): boolean {
  if (!strat) return false
  const tpl = String(strat.template_id || '').trim()
  if (!tpl) return false
  return tpl === expectedTemplateId.value
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
    (isSaveMode.value || props.canSubmit) &&
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
    (isSaveMode.value || props.canSubmit) &&
    !strategySubmitting.value &&
    !strategyLoading.value &&
    !!readyGridSizing.value &&
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
  grid_mode: 'arithmetic' as GridMode,  // 网格模式
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

/** 网格数量和手数仅来自当前参数的试算结果。 */
const gridSizing = reactive<{
  data: GridSizingData | null
  loading: boolean
  error: string
}>({
  data: null,
  loading: false,
  error: '',
})
const readyGridSizing = computed(() => {
  const result = gridSizing.data
  return result?.ready && result.lot_per_grid > 0 ? result : null
})

/** 试算 debounce 计时器 */
let gridSizingTimer: ReturnType<typeof setTimeout> | null = null
/** 试算请求 AbortController（用于超时/防竞态） */
let gridSizingAbort: AbortController | null = null

/** 行情源节点：主节点（用户已选过的 is_primary && enabled） */
const primaryNode = computed(() => props.primaryNode || null)

/** 策略参数加载中 */
const strategyLoading = ref(false)

/**
 * 切换开仓方式（Tab）→ 重新解析分组 / 策略实例，并按该方式的预设回填固定参数。
 *
 * 三个方式各有一份预设（市价 / 限价 / 网格），切 Tab 必须换一份，
 * 否则会把市价的「以损定量 / 盈亏比」带进网格。
 *
 * **切 Tab 必须重新解析分组 / 策略实例**：三个开仓方式各有**独立**的分组与策略实例
 * （分组名带 strategy_type 前缀）。不重新解析就会拿着上一路的策略实例去保存新规则 ——
 * 后端按「模版-规则一致性」拒收（如「策略模版 tpl_3 不允许规则类型 3」）。
 */
watch(strategyRef, async () => {
  if (isPresetMode.value) {
    await loadPresets()
    applyPresetToForm()
    return
  }
  if (!props.modelValue) return
  // 等父级把新的 groupName 传下来（groupName 由 strategyType 派生，
  // 父级重渲染后 props 才会更新），否则会拿着上一路的分组名去查。
  await nextTick()
  // 切 Tab → 换分组 / 策略实例。先清掉上一路的缓存，避免请求返回前误用旧实例。
  currentGroup.value = null
  currentStrategy.value = null
  await loadGroupAndStrategy()
  await loadPresets()
  applyPresetToForm()
})

/**
 * 拉取我的开仓参数预设。
 *
 * 服务端读取时会顺带做一次老用户种子回填（从已有策略实例推导预设），
 * 所以这里拿到的列表可能比「用户手动配过的」更多。
 *
 * 拉不到不报错 —— 没有预设时按「走现有 group→strategy→rule 兜底」处理。
 */
async function loadPresets(): Promise<void> {
  try {
    await hub.fetchPresets()
  } catch {
    /* 拉不到预设不影响主流程：固定参数回落到策略实例 / 模版默认值 */
  }
}

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
    const groups = (await hub.listGroups()).filter(g => g.owner_user_id === auth.me?.user_id)
    let grp: GroupOut | null = null
    // **优先按 groupName 找**：三个开仓方式各有独立分组（分组名带 strategy_type 前缀），
    // groupName 由父级按当前 Tab 响应式算出，切换 Tab 时会跟着变。
    // props.groupId 是父级在「打开弹窗那一刻」解析的，Tab 切换后就是上一路的陈旧值，
    // 所以它只能当作兜底，且必须校验名字对得上，否则会拿错分组 → 拿错策略实例。
    if (props.groupName) {
      grp = groups.find((g) => g.name === props.groupName) ?? null
    }
    if (!grp && props.groupId) {
      const byId = groups.find((g) => g.group_id === props.groupId) ?? null
      // 名字对得上才认（说明这个 id 恰好就是当前 Tab 的分组）
      if (byId && (!props.groupName || byId.name === props.groupName)) {
        grp = byId
      }
    }
    if (!grp && props.groupName && (isMarket.value || isLimit.value || isGrid.value)) {
      try {
        if (!primaryNode.value) throw new Error('请先设置自己的主节点')
        grp = await hub.createGroup({ name: props.groupName, enabled: true, node_ids: [primaryNode.value.node_id] })
        lastStrategyError.value = ''
      } catch (e) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        lastStrategyError.value = err?.response?.data?.detail || err?.message || '自动创建分组失败'
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
      // **模版一致性守卫**：分组上绑的策略实例必须属于当前开仓方式对应的模版。
      // 拿错模版（比如市价开仓却绑到了网格策略 tpl_3）时，
      // 后端 validate_rules_for_template 会拒收（如「策略模版 tpl_3 不允许规则类型 3」）。
      // 这里直接视为「没有可复用策略」→ 后面走新建流程。
      let templateMismatch = false
      if (strat && !strategyTemplateMatches(strat)) {
        lastStrategyError.value =
          `分组绑定的策略模版（${strat.template_id}）与当前开仓方式不匹配，已按新策略处理`
        strat = null
        templateMismatch = true
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
          gridParams.grid_mode = gridRule.grid_mode ?? 'arithmetic'
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
      } else {
        // 没有可复用策略（首次开仓 / 模版错配）：回落到模版默认值。
        // 模版错配时也必须回落 —— 否则表单里还留着上一轮（别的模版）的参数。
        if (!templateMismatch) currentStrategy.value = null
        applyTemplateDefaults()
      }
    } else {
      currentStrategy.value = null
      // 没有已有策略时，市价/限价用模版2（以损定量）第一条规则的默认值
      applyTemplateDefaults()
    }
  } finally {
    strategyLoading.value = false
  }
}

/**
 * 用模版默认值回填固定参数。
 *
 * 没有可复用策略实例时（首次开仓 / 分组绑的策略模版错配）调用：
 * 市价/限价 → 模版2（以损定量）的第一条规则；网格 → 模版3（网格）的第一条规则。
 */
function applyTemplateDefaults(): void {
  // 市价/限价用模版2（以损定量）第一条规则的默认值
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
      gridParams.grid_mode = gRule.grid_mode ?? 'arithmetic'
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

// ── 网格试算 ──
//
// 用法：
//   1. 监听 gridParams 关键字段 + symbol，debounce 500ms 自动调 triggerGridSizing
//   2. 仅当当前试算同时给出格数和手数时，允许提交网格规则
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

function resetGridSizing(): void {
  if (gridSizingTimer !== null) clearTimeout(gridSizingTimer)
  gridSizingTimer = null
  gridSizingAbort?.abort()
  gridSizingAbort = null
  gridSizing.data = null
  gridSizing.error = ''
  gridSizing.loading = false
}

/** 自动试算带 8 秒超时，旧请求的结果不能更新当前表单。 */
async function triggerGridSizing(): Promise<void> {
  resetGridSizing()
  if (!props.modelValue || !canRunGridSizing.value) return
  const node = primaryNode.value
  if (!node) {
    gridSizing.error = '未找到主节点，无法试算；请在「节点」页设置主节点'
    return
  }
  const ctrl = new AbortController()
  gridSizingAbort = ctrl
  const timer = setTimeout(() => ctrl.abort(), 8000)
  gridSizing.loading = true
  const symbol = (localForm.symbol || '').trim().toUpperCase()
  try {
    const data = await hub.fetchGridSizing(node.node_id, symbol, {
      timeframe: 'M1',
      atr_mult: 0.6,
      spacing: 0,
      price_lower: Number(gridParams.price_lower) || 0,
      price_upper: Number(gridParams.price_upper) || 0,
      grid_mode: gridParams.grid_mode,
      grid_side: derivedGridSide.value ?? 'long',
      stop_lower: gridStopLower.value,
      stop_upper: gridStopUpper.value,
      max_loss: Number(gridParams.max_allow_loss) || 0,
      close_on_stop: true,
      trailing_up: gridParams.trailing_up,
      total_lot_limit: 0,
    }, ctrl.signal)
    if (gridSizingAbort !== ctrl) return
    gridSizing.data = data
    if (!readyGridSizing.value) {
      gridSizing.error = data.warnings.find(w => w.level === 'warn')?.message || '试算失败，请检查参数后重试'
    }
  } catch (e: unknown) {
    if (gridSizingAbort !== ctrl) return
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    gridSizing.error = ctrl.signal.aborted ? '试算超时，请重试' : detail || '试算失败，请重试'
  } finally {
    clearTimeout(timer)
    if (gridSizingAbort === ctrl) {
      gridSizingAbort = null
      gridSizing.loading = false
    }
  }
}

/** 监听 gridParams 关键字段变化，自动 debounce 试算 */
watch(
  [
    () => props.modelValue,
    isPresetMode,
    isGrid,
    () => primaryNode.value?.node_id,
    () => gridParams.price_lower,
    () => gridParams.price_upper,
    () => gridParams.grid_mode,
    () => gridParams.trailing_up,
    () => gridParams.max_allow_loss,
    gridStopLower,
    gridStopUpper,
    () => (localForm.symbol || '').trim().toUpperCase(),
  ],
  () => {
    resetGridSizing()
    if (!props.modelValue || isPresetMode.value || !canRunGridSizing.value) return
    gridSizing.loading = true
    gridSizingTimer = setTimeout(() => void triggerGridSizing(), 500)
  },
)
onBeforeUnmount(resetGridSizing)

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
  if (!GRID_MODE_OPTIONS.some((o) => o.value === gridParams.grid_mode)) return '网格模式非法'
  // 止损价决定方向：落在区间下方=只做多，落在区间上方=只做空
  if (!derivedGridSide.value) {
    const sp = Number(gridParams.stop_price) || 0
    if (!(sp > 0)) return '止损价需大于 0'
    return '止损价须在价格区间之外：低于区间下限为只做多，高于区间上限为只做空'
  }
  // 每格手数由自动试算按最大亏损反推，所以最大亏损必须填
  if (!(gridParams.max_allow_loss > 0)) return '以损定量需大于 0'
  return null
}

/**
 * 用 gridParams 构建网格规则（type=4），构造 strategy patch payload。
 */
function buildGridStrategyPayload(): StrategyRule[] | null {
  const sizing = readyGridSizing.value
  if (!sizing) return null
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
      grid_count: sizing.grid_count,
      grid_mode: gridParams.grid_mode,
      grid_side: side,
      lot_per_grid: sizing.lot_per_grid,
      trigger_price: gridParams.trigger_price ?? 0,
      stop_lower: gridStopLower.value,
      stop_upper: gridStopUpper.value,
      close_on_stop: true,
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

/**
 * 「保存预设」时的品种维度：
 *   - 'symbol'：只对当前品种生效（存具体 symbol）
 *   - 'wildcard'：通用模板，所有品种共用一套（存 '*'）
 *
 * 只在「保存预设」模式下有意义。
 */
const presetScope = ref<'symbol' | 'wildcard'>('symbol')

/**
 * 「保存预设」：只写开仓参数预设。
 *
 * 与保存策略的两点根本区别：
 *   1. **不碰分组 / 策略实例** —— 预设是纯前端的「我下单时的默认值」，
 *      建组建策略有副作用（热推、快照冻结），不该在这里发生；
 *   2. **只存固定参数** —— 止损价 / 止盈价 / 挂单价（网格：区间上下限 / 止损价）
 *      每次现填，不进预设。
 *
 * 保存的品种维度：
 *   - 「保存预设」模式下，symbol 为 '*' 时存通用模板；否则存当前品种。
 */
async function savePresetOnly(): Promise<void> {
  const st = strategyRef.value as OrderPresetStrategyType
  const sym = (localForm.symbol || '').trim().toUpperCase()
  if (isGrid.value) {
    if (!(gridParams.max_allow_loss > 0)) {
      lastStrategyError.value = '以损定量需大于 0'
      return
    }
    if (!GRID_MODE_OPTIONS.some((o) => o.value === gridParams.grid_mode)) {
      lastStrategyError.value = '网格模式非法'
      return
    }
  } else {
    const rpErr = validateRiskParams()
    if (rpErr) { lastStrategyError.value = rpErr; return }
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    const payload: Parameters<typeof hub.savePreset>[0] = {
      symbol: presetScope.value === 'wildcard' ? '*' : sym,
      strategy_type: st,
    }
    if (isGrid.value) {
      payload.grid_mode = gridParams.grid_mode
      payload.max_allow_loss = gridParams.max_allow_loss
      payload.trailing_up = gridParams.trailing_up
    } else {
      payload.risk_amount = riskParams.risk_amount
      payload.rr_ratio = riskParams.rr_ratio
      payload.base_ratio = riskParams.base_ratio
      payload.breakeven_times = riskParams.breakeven_times
    }
    await hub.savePreset(payload)
    strategySubmitting.value = false
    emit('update:modelValue', false)
    ElMessage.success('预设已保存，下次开仓将自动填入')
  } catch (e) {
    strategySubmitting.value = false
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastStrategyError.value = err?.response?.data?.detail || err?.message || '保存预设失败'
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
    // **模版一致性守卫**：只有模版与当前开仓方式一致的策略实例才允许被 PATCH。
    // 模版对不上（如市价开仓却拿到了网格策略 tpl_3）时，后端
    // validate_rules_for_template 会拒收（如「策略模版 tpl_3 不允许规则类型 3」），
    // 所以这里视作「没有可复用策略」→ 走新建分支。
    let strategyId = strategyTemplateMatches(currentStrategy.value)
      ? currentStrategy.value!.strategy_id
      : null
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
          (s) =>
            s.name === newName &&
            s.owner_user_id === currentGroup.value?.owner_user_id &&
            // 模版必须与当前开仓方式一致，否则 PATCH 会被后端拒收
            s.template_id === expectedTemplateId.value,
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
          template_id: tpl2?.template_id || expectedTemplateId.value,
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
  const rules = buildGridStrategyPayload()
  if (!rules) return
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    // **模版一致性守卫**：只有模版与当前开仓方式一致的策略实例才允许被 PATCH。
    // 模版对不上（如市价开仓却拿到了网格策略 tpl_3）时，后端
    // validate_rules_for_template 会拒收（如「策略模版 tpl_3 不允许规则类型 3」），
    // 所以这里视作「没有可复用策略」→ 走新建分支。
    let strategyId = strategyTemplateMatches(currentStrategy.value)
      ? currentStrategy.value!.strategy_id
      : null
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
          (s) =>
            s.name === gridName &&
            s.owner_user_id === currentGroup.value?.owner_user_id &&
            // 模版必须与当前开仓方式一致（网格 = tpl_3），否则 PATCH 会被后端拒收
            s.template_id === expectedTemplateId.value,
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
          template_id: tpl3?.template_id || expectedTemplateId.value,
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
      if (r.adopted[0].status === 'approved') {
        ElMessage.success(`已自动批准 ${r.adopted.length} 个跟单节点`)
      } else {
        ElMessage.info(`已生成 ${r.adopted.length} 条跟单申请，审核通过后生效`)
      }
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
    // **模版一致性守卫**：只有模版与当前开仓方式一致的策略实例才允许被 PATCH。
    // 模版对不上（如市价开仓却拿到了网格策略 tpl_3）时，后端
    // validate_rules_for_template 会拒收（如「策略模版 tpl_3 不允许规则类型 3」），
    // 所以这里视作「没有可复用策略」→ 走新建分支。
    let strategyId = strategyTemplateMatches(currentStrategy.value)
      ? currentStrategy.value!.strategy_id
      : null
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
          (s) =>
            s.name === newName &&
            s.owner_user_id === currentGroup.value?.owner_user_id &&
            // 模版必须与当前开仓方式一致，否则 PATCH 会被后端拒收
            s.template_id === expectedTemplateId.value,
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
          template_id: tpl2?.template_id || expectedTemplateId.value,
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
    if (!primaryNode.value) throw new Error('请先设置自己的主节点')
    if (!carry.value) {
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
  const rules = buildGridStrategyPayload()
  if (!rules) return
  if (!currentGroup.value) {
    lastStrategyError.value = '分组未就绪，请稍后重试或刷新页面'
    return
  }
  strategySubmitting.value = true
  lastStrategyError.value = ''
  try {
    // **模版一致性守卫**：只有模版与当前开仓方式一致的策略实例才允许被 PATCH。
    // 模版对不上（如市价开仓却拿到了网格策略 tpl_3）时，后端
    // validate_rules_for_template 会拒收（如「策略模版 tpl_3 不允许规则类型 3」），
    // 所以这里视作「没有可复用策略」→ 走新建分支。
    let strategyId = strategyTemplateMatches(currentStrategy.value)
      ? currentStrategy.value!.strategy_id
      : null
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
          (s) =>
            s.name === gridName &&
            s.owner_user_id === currentGroup.value?.owner_user_id &&
            // 模版必须与当前开仓方式一致（网格 = tpl_3），否则 PATCH 会被后端拒收
            s.template_id === expectedTemplateId.value,
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
          template_id: tpl3?.template_id || expectedTemplateId.value,
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
    if (!primaryNode.value) throw new Error('请先设置自己的主节点')
    if (!carry.value) {
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
            {{ isPresetMode ? '开仓参数预设' : '创建交易' }}
            <span class="od-title-dot">·</span>
            <span class="od-title-sym">{{ (localForm.symbol || '').toUpperCase() }}</span>
          </span>
          <span class="od-title-sub">
            {{ isPresetMode ? '这里配的是你下单时的默认参数，开仓弹窗会自动带入' : '开仓参数配置' }}
          </span>
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
        <!-- 「保存预设」模式：动态价（止盈 / 挂单 / 止损）不展示也不入预设 ——
             这些随行情变化，每次开仓现填。 -->
        <template v-if="!isPresetMode">
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
        </template>

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
        <div class="od-section" style="border-left-color: #22C55E">
          {{ isPresetMode ? '智能吸筹预设参数' : '智能吸筹参数' }}
        </div>

        <!-- 「保存预设」模式：只暴露 3 个固定项（网格模式 / 以损定量 / 向上追踪）。
             区间上下限与止损价随行情变化，每次开仓现填，不进预设。 -->
        <template v-if="isPresetMode">
          <div class="od-row">
            <div class="od-row-text">
              <div class="od-row-label">网格模式</div>
              <div class="od-row-sub">等差为每格等距，等比为每格等比例</div>
            </div>
            <div class="od-row-control">
              <select v-model="gridParams.grid_mode" class="od-select">
                <option v-for="o in GRID_MODE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
          </div>
          <div class="od-row">
            <div class="od-row-text">
              <div class="od-row-label">以损定量</div>
              <div class="od-row-sub">网格整体的最大可接受亏损，超出会缩减每格手数</div>
            </div>
            <div class="od-row-control od-input-affix">
              <input v-model.number="gridParams.max_allow_loss" class="od-input" type="number" step="10" min="0" placeholder="如 100" />
              <span class="od-affix">USD</span>
            </div>
          </div>
          <div class="od-row">
            <div class="od-row-text">
              <div class="od-row-label">向上追踪</div>
              <div class="od-row-sub">行情上涨时网格整体上移，不追跌</div>
            </div>
            <div class="od-row-control">
              <button
                type="button"
                class="pf-switch"
                :class="{ on: gridParams.trailing_up }"
                role="switch"
                :aria-checked="gridParams.trailing_up"
                aria-label="向上追踪"
                @click="gridParams.trailing_up = !gridParams.trailing_up"
              ></button>
            </div>
          </div>
        </template>

        <template v-else>
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

        <p v-if="gridSizing.loading" role="status" class="od-footnote">正在试算网格…</p>
        <div v-else-if="gridSizing.error" class="od-error" role="alert">
          {{ gridSizing.error }}
          <button type="button" class="btn-ghost btn-sm" @click="triggerGridSizing">重试试算</button>
        </div>
        </template>
      </template>

      <!-- 策略参数（市价/限价均走 strategy 模型，按 riskParams 实时计算手数）
           渲染条件：市价或限价开仓 + 有 currentGroup。
           不再要求「策略复用」开关开启——开了就是走 strategy 模型，
           没开就只展示参数，不影响原 quickTrigger 路径。
           「策略复用」开关决定提交时是 save 策略 + 触发 strategy 信号，
           还是仅用参数走 quickTrigger。-->
      <div v-if="(isMarket || isLimit) && strategyLoading && !isPresetMode" class="od-loading">
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

      <!-- 「保存预设」模式：适用范围 + 恢复预设 -->
      <template v-if="isPresetMode">
        <div class="od-section" style="border-left-color: #64748B">适用范围</div>
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">应用到</div>
            <div class="od-row-sub">通用模板对所有品种生效；当前品种只对这一品种生效</div>
          </div>
          <div class="od-row-control">
            <select v-model="presetScope" class="od-select">
              <option value="symbol">当前品种 {{ (localForm.symbol || '').toUpperCase() }}</option>
              <option value="wildcard">通用模板（所有品种）</option>
            </select>
          </div>
        </div>
      </template>

      <p v-if="!isPresetMode && stopEqualsEntry" class="od-error" role="alert">
        止损价不能等于{{ isLimit ? '挂单价' : '当前报价' }}
      </p>

      <!-- 恢复预设：会话里改乱了固定参数，一键回滚到预设值 -->
      <div v-if="!isPresetMode && canRestorePreset" class="od-notice is-info">
        <span class="od-notice-text">
          当前参数已与预设不一致。预设只作为开仓时的初始值，本次改动不会写回预设。
        </span>
        <button type="button" class="od-notice-btn" @click="restorePreset">恢复预设</button>
      </div>

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
          <!-- 「保存预设」模式：只写预设，不发信号、不建分组/策略 -->
          <button
            v-if="isPresetMode"
            class="od-btn-primary"
            :disabled="strategySubmitting"
            @click="savePresetOnly"
          >
            <span class="od-btn-primary-main">{{ strategySubmitting ? '保存中…' : '保存预设' }}</span>
          </button>
          <!-- 市价 / 限价：统一走 triggerStrategy（strategy 模型），前端推导 BUY/SEL方向并展示 -->
          <template v-else-if="isMarket || isLimit">
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
  max-height: calc(100dvh - 200px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
  overflow-y: auto;
}
.order-dialog .el-overlay {
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(2px);
}

/* ── 手机端：撑满全屏、贴顶贴底、无圆角 ── */
@media (max-width: 600px) {
  /* .order-dialog {
    margin: 0 !important;
  } */
  .order-dialog .el-dialog {
    width: 100vw !important;
    max-width: 100vw !important;
    /* dvh：standalone 下 100vh 会多出一截空白或压住底部手势条。
       顶部/底部各留出安全区，body 高度同步减去这两个值。 */
    height: 100vh !important;
    height: 100dvh !important;
    max-height: 100vh !important;
    max-height: 100dvh !important;
    border-radius: 0 !important;
    margin: 0 !important;
    padding-top: env(safe-area-inset-top, 0px);
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }
  .order-dialog .el-dialog__body {
    max-height: calc(100vh - 168px);
    max-height: calc(100dvh - 168px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
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
