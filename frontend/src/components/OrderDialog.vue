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
import { ElDialog, ElMessage, ElSwitch } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/switch/style/css'
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
      // 初始建仓：每次打开强制重置为关闭（避免已有策略里的旧值干扰）
      gridParams.prefill_enabled = false
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
 * 是否展示「带单」开关：非保存模式（保存不发信号，无需选投递范围）
 * 且持有 follow_leader 能力（管理员自动放行）。
 *
 * 能力语义与后端 permissions.can_be_leader 同口径：跟单方向人人可做，
 * 带单能力只决定「我能否被别人跟」。没有该能力的客户看不到开关，
 * carry 恒为 false，信号仍按原逻辑只投递主节点，行为与隐藏前一致。
 */
const canCarry = computed(() => !isSaveMode.value && auth.canBeFollowLeader)

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

const STRATEGIES: { key: OrderStrategy; label: string; icon: string }[] = [
  { key: 'trend_market', label: '市价开仓', icon: '⚡' },
  { key: 'trend_limit', label: '限价开仓', icon: '◎' },
  { key: 'grid', label: '智能吸筹', icon: '▦' },
]

const GRID_MODE_OPTIONS: readonly { value: GridMode; label: string }[] = [
  { value: 'arithmetic', label: '等差' },
  { value: 'geometric', label: '等比' },
]

const GRID_SIDE_OPTIONS: readonly { value: GridSide; label: string }[] = [
  { value: 'long', label: '只做多' },
  { value: 'short', label: '只做空' },
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

/** 试算结果摘要文本（用于 UI hint） */
const gridSizingHint = computed(() => {
  const d = gridSizing.data
  if (!d?.ready) return ''
  const pl = Number(gridParams.price_lower) || 0
  const pu = Number(gridParams.price_upper) || 0
  const step =
    d.grid_count > 0 ? (pu - pl) / d.grid_count : 0
  const spacing =
    d.grid_count > 0
      ? gridParams.grid_mode === 'geometric'
        ? `${(((pu / pl) ** (1 / d.grid_count) - 1) * 100).toFixed(4)}%`
        : trimNum(step)
      : '0'
  return `M1 ATR ${trimNum(d.atr, 5)} → 间距 ≈ ${spacing}，` +
    `${gridParams.grid_mode === 'geometric' ? '等比' : '等差'} ${d.grid_count} 格，` +
    `每格 ${trimNum(d.lot_per_grid, 4)} 手；满仓约 ${trimNum(d.worst_loss, 2)} 美元`
})

/** 复刻 StrategyFormModal.trimNum：小数尾巴清零 */
function trimNum(n: number, digits = 5): string {
  if (!isFinite(n)) return '0'
  const s = n.toFixed(digits)
  return s.includes('.') ? s.replace(/0+$/, '').replace(/\.$/, '') : s
}

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

/** 网格策略参数编辑状态（仅网格模式时渲染） */
const gridParams = reactive({
  price_lower: 0 as number | null,      // 区间下限
  price_upper: 0 as number | null,      // 区间上限
  grid_count: 10,                        // 网格数量
  grid_mode: 'arithmetic' as GridMode,  // 网格模式
  grid_side: 'long' as GridSide,        // 网格方向
  lot_per_grid: 0.01,                  // 每格手数
  prefill_enabled: false,               // 初始建仓（默认关闭）
  trigger_price: 0 as number | null,    // 触发价（默认 0 = 立即启动）
  stop_lower: 0 as number | null,       // 止损价（多头止损 / 空头止盈）
  stop_upper: 0 as number | null,       // 止盈价（多头止盈 / 空头止损）
  trailing_up: false,                  // 向上追踪（默认关闭）
  trailing_max: 0,                      // 最大平移格数（0=不限）
  max_allow_loss: 0,                     // 最大可接受亏损
})

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
          gridParams.grid_side = gridRule.grid_side ?? 'long'
          gridParams.lot_per_grid = gridRule.lot_per_grid ?? 0.01
          gridParams.prefill_enabled = gridRule.prefill_enabled ?? false
          gridParams.trigger_price = gridRule.trigger_price ?? 0
          gridParams.stop_lower = gridRule.stop_lower ?? 0
          gridParams.stop_upper = gridRule.stop_upper ?? 0
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
          gridParams.grid_side = gRule.grid_side ?? 'long'
          gridParams.lot_per_grid = gRule.lot_per_grid ?? 0.01
          gridParams.prefill_enabled = gRule.prefill_enabled ?? false
          gridParams.trigger_price = gRule.trigger_price ?? 0
          gridParams.stop_lower = gRule.stop_lower ?? 0
          gridParams.stop_upper = gRule.stop_upper ?? 0
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
//   1. 监听 gridParams 关键字段 + symbol，debounce 500ms 自动调 scheduleGridSizing
//   2. UI 上有显式「重算」按钮调 runGridSizing
//   3. 试算成功 → gridSizing.data.ready 时把 grid_count / lot_per_grid 写回 gridParams
//
// 写死的入参（不暴露在弹窗里）：
//   - timeframe = 'M1'
//   - atr_mult  = 0.6
//   - spacing   = 0（让后端按 ATR×倍数算）
//   - total_lot_limit = 0（不限）
//
// 行情源：用户已设置的主节点（is_primary && enabled）。没有则置灰 + 提示。

/** 试算前置条件：symbol 合法 + 区间合法 + 至少一个止损/止盈 + 最大亏损 > 0 */
const canRunGridSizing = computed(() => {
  if (!isGrid.value) return false
  const sym = (localForm.symbol || '').trim()
  if (!sym) return false
  const pl = Number(gridParams.price_lower) || 0
  const pu = Number(gridParams.price_upper) || 0
  if (!(pl > 0) || !(pu > pl)) return false
  if (!(Number(gridParams.max_allow_loss) > 0)) return false
  // 多头：必须填止损价；空头：必须填止盈价
  const sl = Number(gridParams.stop_lower) || 0
  const tp = Number(gridParams.stop_upper) || 0
  if (gridParams.grid_side === 'long' && !(sl > 0)) return false
  if (gridParams.grid_side === 'short' && !(tp > 0)) return false
  return true
})

/** 显式重算（来自「重算」按钮）：立即发请求 */
function runGridSizing(): void {
  void triggerGridSizing(false)
}

/** 自动试算（debounce 后调用）：带 8s 超时 */
function triggerGridSizing(withDebounce: boolean): void {
  if (!isGrid.value) return
  if (!canRunGridSizing.value) return
  if (withDebounce) {
    if (gridSizingTimer) clearTimeout(gridSizingTimer)
    gridSizingTimer = setTimeout(() => void triggerGridSizing(false), 500)
    return
  }
  const node = primaryNode.value
  if (!node) {
    gridSizing.error = '未找到主节点，无法试算；请在「节点」页设置主节点'
    return
  }
  // 取消上一笔未完成的请求
  if (gridSizingAbort) gridSizingAbort.abort()
  const ctrl = new AbortController()
  gridSizingAbort = ctrl
  const timer = setTimeout(() => ctrl.abort(), 8000)
  gridSizing.loading = true
  gridSizing.error = ''
  const symbol = (localForm.symbol || '').trim().toUpperCase()
  hub
    .fetchGridSizing(node.node_id, symbol, {
      timeframe: 'M1',
      atr_mult: 0.6,
      spacing: 0,
      // 全部转 Number：防止 Vue v-model.number 清空后产生空字符串导致后端报错
      price_lower: Number(gridParams.price_lower) || 0,
      price_upper: Number(gridParams.price_upper) || 0,
      grid_mode: gridParams.grid_mode,
      grid_side: gridParams.grid_side,
      stop_lower: Number(gridParams.stop_lower) || 0,
      stop_upper: Number(gridParams.stop_upper) || 0,
      max_loss: Number(gridParams.max_allow_loss) || 0,
      prefill_enabled: gridParams.prefill_enabled,
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
        if (data.lot_per_grid > 0) gridParams.lot_per_grid = data.lot_per_grid
      }
      if (!data.ready || data.lot_per_grid === 0) {
        const msgs = (data.warnings || [])
          .filter((w) => w.level === 'warn')
          .map((w) => w.message)
        gridSizing.error = msgs[0] || '该品种试算失败，可手动填写网格参数'
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
    gridParams.grid_side,
    gridParams.prefill_enabled,
    gridParams.trailing_up,
    gridParams.max_allow_loss,
    gridParams.stop_lower,
    gridParams.stop_upper,
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
  if (!GRID_SIDE_OPTIONS.some((o) => o.value === gridParams.grid_side)) return '网格方向非法'
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
  return [
    {
      ...base,
      type: 4,
      status: 1,
      action: gridParams.grid_side === 'short' ? 'SELL' : 'BUY',
      price_lower: gridParams.price_lower ?? 0,
      price_upper: gridParams.price_upper ?? 0,
      grid_count: gridParams.grid_count,
      grid_mode: gridParams.grid_mode,
      grid_side: gridParams.grid_side,
      lot_per_grid: gridParams.lot_per_grid,
      trigger_price: gridParams.trigger_price ?? 0,
      stop_lower: gridParams.stop_lower ?? 0,
      stop_upper: gridParams.stop_upper ?? 0,
      close_on_stop: true,
      prefill_enabled: gridParams.prefill_enabled,
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
    const gridAction: ManualSignalAction = gridParams.grid_side === 'short' ? 'SELL' : 'BUY'
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
  reportStrategyResult(res, derivedAction.value ?? 'BUY', (localForm.symbol || '').toUpperCase())
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
    width="460px"
    :close-on-click-modal="false"
    :show-close="false"
    align-center
    class="order-dialog"
    :style="{ '--order-radius': '16px' }"
  >
    <template #header>
      <div class="od-header">
        <div class="od-title">
          <span class="od-title-main">创建交易 · {{ localForm.symbol }}</span>
          <!-- <span class="od-title-sub">{{ STRATEGY_LABEL[strategy] }} · 界面演示，不产生真实订单</span> -->
        </div>
        <button class="od-close" aria-label="关闭" @click="close" title="关闭">
          X
        </button>
      </div>
    </template>

    <div class="od-body">
      <!-- 策略切换 Tab -->
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
          <span class="od-tab-icon">{{ s.icon }}</span>
          <span class="od-tab-label">{{ s.label }}</span>
        </button>
      </div>

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
      <div v-if="!isGrid" class="od-section">开仓参数</div>

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

      <!-- 入场价（限价） -->
      <div v-if="isLimit" class="od-row">
        <div class="od-row-text">
          <div class="od-row-label required">入场价（必填）</div>
          <!-- <div class="od-row-sub">价格到达时再触发</div> -->
        </div>
        <div class="od-row-control od-input-affix">
          <input v-model.number="localForm.entry_price" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3150.50" />
          <span class="od-affix">USD</span>
        </div>
      </div>

      <!-- 止损 / 止盈（限价+市价）：市价模式下只展示止损，止盈在底仓上为 0、分散仓按盈亏比阶梯止盈 -->
      <template v-if="!isGrid">
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label required">止损价（必填）</div>
            <!-- <div class="od-row-sub">
              {{ isMarket ? '用于按风险金额反推总手数 + 分散仓按盈亏比挂阶梯止盈' : '同时用于自动判定方向' }}
            </div> -->
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="localForm.sl" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3140.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <div v-if="!isMarket && !isLimit" class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">止盈价</div>
            <div class="od-row-sub">不填则不设止盈</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="localForm.tp" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3180.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
      </template>

      <!-- 网格策略参数（完整版） -->
      <template v-if="isGrid">
        <div class="od-section">网格参数</div>
        <!-- 网格方向（按钮组：多 / 空） -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">网格方向</div>
            <div class="od-row-sub">只做多=跌买涨卖；只做空=涨卖跌买</div>
          </div>
          <div class="od-row-control">
            <div class="od-seg" role="radiogroup" aria-label="网格方向">
              <button
                v-for="o in GRID_SIDE_OPTIONS"
                :key="o.value"
                type="button"
                role="radio"
                class="od-seg-btn"
                :class="{ active: gridParams.grid_side === o.value }"
                :aria-checked="gridParams.grid_side === o.value"
                @click="gridParams.grid_side = o.value"
              >
                {{ o.label }}
              </button>
            </div>
          </div>
        </div>
        <!-- 区间下限 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label required">区间下限（必填）</div>
            <div class="od-row-sub">价格跌破此值后网格终止</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.price_lower" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3000.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <!-- 区间上限 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label required">区间上限（必填）</div>
            <div class="od-row-sub">价格涨破此值后网格终止</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.price_upper" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3300.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <!-- 止损价（方向相关标签） -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">{{ gridParams.grid_side === 'short' ? '止损价（空头）' : '止损价（多头）' }}</div>
            <div class="od-row-sub">{{ gridParams.grid_side === 'short' ? '跌破区间下限触发' : '跌破区间下限触发' }}</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.stop_lower" class="od-input" type="number" step="0.0001" min="0" placeholder="如 2950.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>

        <!-- 最大可接受亏损 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">最大可接受亏损</div>
            <div class="od-row-sub">账户货币，0 = 不限制</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.max_allow_loss" class="od-input" type="number" step="10" min="0" placeholder="如 100" />
            <span class="od-affix">USD</span>
          </div>
        </div>

        <!-- 网格试算提示（自动覆盖网格数量 / 每格手数） -->
        <div class="od-grid-sizing">
          <div class="od-grid-sizing-row">
            <span v-if="gridSizing.loading" class="od-grid-sizing-state">试算中…</span>
            <span v-else-if="!primaryNode" class="od-grid-sizing-state muted">未设置主节点，无法自动试算</span>
            <span v-else-if="!canRunGridSizing" class="od-grid-sizing-state muted">填好区间/止损/最大亏损后自动试算</span>
            <!-- <span v-else-if="gridSizing.error" class="od-grid-sizing-state warn">{{ gridSizing.error }}</span> -->
            <span v-else-if="gridSizing.data?.ready" class="od-grid-sizing-state ok">{{ gridSizingHint }}</span>
            <span v-else class="od-grid-sizing-state muted">等待试算…</span>
            <button
              type="button"
              class="od-grid-sizing-reload"
              :disabled="!canRunGridSizing || gridSizing.loading || !primaryNode"
              title="重新试算"
              aria-label="重新试算"
              @click="runGridSizing"
            >
              <span class="od-reload" :class="{ spin: gridSizing.loading }">↻</span>
            </button>
          </div>
          <ul v-if="gridSizing.data?.warnings?.length" class="od-grid-sizing-warns">
            <li v-for="(w, i) in gridSizing.data.warnings" :key="i" :class="`lvl-${w.level}`">
              {{ w.message }}
            </li>
          </ul>
        </div>

        <!-- 网格数量 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label required">网格数量（必填）</div>
            <div class="od-row-sub">2 ~ 200 格</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.grid_count" class="od-input" type="number" step="1" min="2" max="200" placeholder="如 10" />
            <span class="od-affix">格</span>
          </div>
        </div>
        <!-- 网格模式（按钮组：等差 / 等比） -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">网格模式</div>
            <div class="od-row-sub">等差=间距相等；等比=涨跌幅相等</div>
          </div>
          <div class="od-row-control">
            <div class="od-seg" role="radiogroup" aria-label="网格模式">
              <button
                v-for="o in GRID_MODE_OPTIONS"
                :key="o.value"
                type="button"
                role="radio"
                class="od-seg-btn"
                :class="{ active: gridParams.grid_mode === o.value }"
                :aria-checked="gridParams.grid_mode === o.value"
                @click="gridParams.grid_mode = o.value"
              >
                {{ o.label }}
              </button>
            </div>
          </div>
        </div>

        <!-- 每格手数 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label required">每格手数（必填）</div>
            <div class="od-row-sub">每格下单的手数</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.lot_per_grid" class="od-input" type="number" step="0.01" min="0.01" placeholder="如 0.01" />
            <span class="od-affix">手</span>
          </div>
        </div>

        <!-- 初始建仓 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">初始建仓</div>
            <div class="od-row-sub">按现价上方格位预先开仓</div>
          </div>
          <div class="od-row-control">
            <ElSwitch v-model="gridParams.prefill_enabled" />
          </div>
        </div>
        <!-- 触发价 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">触发价</div>
            <div class="od-row-sub">0 = 立即启动</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.trigger_price" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3100.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>

        <!-- 止盈价（方向相关标签） -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">{{ gridParams.grid_side === 'short' ? '止盈价（空头）' : '止盈价（多头）' }}</div>
            <div class="od-row-sub">{{ gridParams.grid_side === 'short' ? '涨破区间上限触发' : '涨破区间上限触发' }}</div>
          </div>
          <div class="od-row-control od-input-affix">
            <input v-model.number="gridParams.stop_upper" class="od-input" type="number" step="0.0001" min="0" placeholder="如 3350.00" />
            <span class="od-affix">USD</span>
          </div>
        </div>
        <!-- 向上追踪 -->
        <div class="od-row">
          <div class="od-row-text">
            <div class="od-row-label">向上追踪</div>
            <div class="od-row-sub">突破区间外沿时网格整体平移</div>
          </div>
          <div class="od-row-control">
            <ElSwitch v-model="gridParams.trailing_up" />
          </div>
        </div>
        <!-- 最大平移格数（仅追踪开启时显示） -->
        <template v-if="gridParams.trailing_up">
          <div class="od-row">
            <div class="od-row-text">
              <div class="od-row-label">最大平移格数</div>
              <div class="od-row-sub">0 = 不限</div>
            </div>
            <div class="od-row-control od-input-affix">
              <input v-model.number="gridParams.trailing_max" class="od-input" type="number" step="1" min="0" placeholder="如 3" />
              <span class="od-affix">格</span>
            </div>
          </div>
        </template>

      </template>

      <!-- 策略参数（市价/限价均走 strategy 模型，按 riskParams 实时计算手数）
           渲染条件：市价或限价开仓 + 有 currentGroup。
           不再要求「策略复用」开关开启——开了就是走 strategy 模型，
           没开就只展示参数，不影响原 quickTrigger 路径。
           「策略复用」开关决定提交时是 save 策略 + 触发 strategy 信号，
           还是仅用参数走 quickTrigger。-->
      <template v-if="(isMarket || isLimit) && currentGroup">
        <!-- <div class="od-section">策略参数</div> -->
        <!-- 加载状态 -->
        <div v-if="strategyLoading" class="od-loading">
          <span class="od-loading-dot"></span>
          <span class="od-loading-dot"></span>
          <span class="od-loading-dot"></span>
          加载策略数据…
        </div>
        <template v-else>
          <!-- 无分组时的提示（实际上 currentGroup 有值时不会走到这里） -->
          <div v-if="!currentGroup" class="od-error">
            分组未就绪，请稍后重试
          </div>
          <template v-else>
            <!-- 风险金额 -->
            <div class="od-row">
              <div class="od-row-text">
                <div class="od-row-label">风险金额（USD）</div>
                <!-- <div class="od-row-sub">本次交易愿意承担的亏损金额</div> -->
              </div>
              <div class="od-row-control od-input-affix">
                <input
                  v-model.number="riskParams.risk_amount"
                  class="od-input"
                  type="number"
                  min="1"
                  step="10"
                  placeholder="如 100"
                />
                <span class="od-affix">USD</span>
              </div>
            </div>
            <!-- 盈亏比 -->
            <div class="od-row">
              <div class="od-row-text">
                <div class="od-row-label">盈亏比</div>
                <!-- <div class="od-row-sub">止盈 = 止损 × 盈亏比</div> -->
              </div>
              <div class="od-row-control">
                <input
                  v-model.number="riskParams.rr_ratio"
                  class="od-input"
                  type="number"
                  min="0"
                  step="0.1"
                  placeholder="如 2.5"
                />
              </div>
            </div>
            <!-- 底仓占比 -->
            <div class="od-row">
              <div class="od-row-text">
                <div class="od-row-label">底仓占比</div>
                <!-- <div class="od-row-sub">底仓占总手数的百分比</div> -->
              </div>
              <div class="od-row-control od-input-affix">
                <input
                  v-model.number="riskParams.base_ratio"
                  class="od-input"
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  placeholder="如 30"
                />
                <span class="od-affix">%</span>
              </div>
            </div>
            <!-- 保本止损距倍数 -->
            <div class="od-row">
              <div class="od-row-text">
                <div class="od-row-label">保本倍数</div>
                <!-- <div class="od-row-sub">浮盈 ≥ 止损距 × 倍数时保本（0=不启用）</div> -->
              </div>
              <div class="od-row-control">
                <input
                  v-model.number="riskParams.breakeven_times"
                  class="od-input"
                  type="number"
                  min="0"
                  step="0.5"
                  placeholder="如 2"
                />
              </div>
            </div>
            <!-- 手数试算提示 -->
            <!-- <div class="od-hint">
              总手数 = 风险金额 ÷ 每手止损亏损（由信号止损价与品种规格算出）<br />
              底仓 {{ riskParams.base_ratio }}% 先市价成交（TP=0）；<br />
              分散仓按盈亏比 {{ riskParams.rr_ratio }} 倍阶梯止盈
            </div> -->
          </template>
        </template>
      </template>

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
    </div>

    <template #footer>
      <div class="od-footer">
        <!-- 「带单」开关：开启 → 信号下发到分组全部节点（含跟单员）；关闭 → 仅主节点。
             仅对持有 follow_leader 能力的用户展示（无权限的客户看不到，也无法被别人跟）。 -->
        <div v-if="canCarry" class="od-carry">
          <ElSwitch v-model="carry" />
          <span class="od-carry-label">带单</span>
          <!-- <span class="od-carry-sub">{{ carry ? '推到本分组全部节点' : '只推本分组主节点' }}</span> -->
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
              <span class="od-btn-primary-main">{{ strategySubmitting ? '触发中…' : '触发' }}</span>
              <span v-if="derivedAction" class="od-btn-primary-sub">
                {{ derivedAction === 'BUY' ? 'BUY 多' : 'SELL 空' }}
                · {{ (localForm.symbol || '').toUpperCase() }}
              </span>
              <span v-else-if="isLimit" class="od-btn-primary-sub muted">
                请填写入场价与止损价
              </span>
              <span v-else class="od-btn-primary-sub muted">
                请填写止损价并等待报价
              </span>
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
              <span class="od-btn-primary-main">
                {{ strategySubmitting ? '触发中…' : '触发' }}
              </span>
              <span class="od-btn-primary-sub">
                {{ gridParams.grid_side === 'long' ? '多' : '空' }}
                · {{ (localForm.symbol || '').toUpperCase() }}
              </span>
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
  --el-border-color-lighter: rgba(212, 205, 205, 0.06);
  --el-border-color: rgba(231, 218, 218, 0.1);
}
.order-dialog .el-dialog__header {
  padding: 16px 18px 12px;
  margin-right: 0;
  border-bottom: 1px solid var(--el-border-color-lighter, rgba(0, 0, 0, 0.06));
}
.order-dialog .el-dialog__body {
  padding: 12px 18px 0;
}
.order-dialog .el-dialog__footer {
  padding: 12px 18px 16px;
  border-top: 1px solid var(--el-border-color-lighter, rgba(0, 0, 0, 0.06));
  background: transparent;
}
.order-dialog .el-dialog {
  border-radius: var(--order-radius, 16px);
  overflow: hidden;
  max-height: calc(100vh - 32px);
}
.order-dialog .el-dialog__body {
  max-height: calc(100vh - 200px);
  overflow-y: auto;
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
  font-size: 17px;
  font-weight: 700;
  color: var(--el-text-color-primary, #111827);
  letter-spacing: 0.01em;
}
.od-title-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
}
.od-close {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  border: 1px solid rgb(250, 250, 250);
  background: rgba(0, 0, 0, 0.03);
  color: var(--el-text-color-secondary, #6b7280);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s, transform 0.1s;
  flex-shrink: 0;
}
.od-close:hover {
  background: #fef2f2;
  color: #dc2626;
  border-color: #fecaca;
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

/* ── Tabs ── */
.od-tabs {
  display: flex;
  gap: 8px;
  padding: 4px;
  background: var(--el-fill-color-blank, #f3f4f6);
  border-radius: 12px;
  border: 1px solid var(--el-border-color-lighter, #e5e7eb);
}
.od-tab {
  flex: 1 1 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 40px;
  border: none;
  background: transparent;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary, #6b7280);
  cursor: pointer;
  transition: background 0.15s, color 0.15s, box-shadow 0.15s, transform 0.1s;
}
.od-tab:hover { color: var(--el-text-color-primary, #111827); }
.od-tab.active {
  background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
  color: #ffffff;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.32);
}
.od-tab-icon { font-size: 15px; line-height: 1; }
.od-tab-label { letter-spacing: 0.02em; }

/* ── Row（label 左 / control 右）── */
.od-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
  background: var(--el-bg-color, #ffffff);
  border: 1px solid var(--el-border-color-lighter, #e5e7eb);
  border-radius: 12px;
}
.od-row-text { min-width: 0; flex: 1 1 auto; }
.od-row-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary, #111827);
  line-height: 1.3;
}
.od-row-label.required::before {
  content: '*';
  color: #ef4444;
  margin-right: 2px;
}
.od-row-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
  margin-top: 2px;
  line-height: 1.4;
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
  background: var(--el-fill-color-blank, #f3f4f6);
  border-radius: 8px;
  font-size: 13px;
  color: var(--el-text-color-primary, #111827);
  font-family: var(--mono);
}
.od-row-control.muted { color: var(--el-text-color-secondary, #6b7280); }
.od-hint-inline {
  font-size: 11px;
  color: #ef4444;
  margin-left: 6px;
}

.od-select,
.od-input {
  height: 36px;
  padding: 0 10px;
  border: 1px solid var(--el-border-color, #d1d5db);
  background: var(--el-bg-color, #ffffff);
  border-radius: 8px;
  font-size: 14px;
  color: var(--el-text-color-primary, #111827);
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  min-width: 0;
}
.od-input { width: 110px; }
.od-input.center { text-align: center; }
.od-select { width: 130px; }
.od-select:focus,
.od-input:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18);
}

/* 单选按钮组（网格模式 / 网格方向） */
.od-seg {
  display: inline-flex;
  align-items: center;
  gap: 0;
  height: 32px;
  padding: 2px;
  background: var(--el-fill-color-blank, #f3f4f6);
  border: 1px solid var(--el-border-color, #d1d5db);
  border-radius: 8px;
}
.od-seg-btn {
  appearance: none;
  border: 0;
  background: transparent;
  font: inherit;
  font-size: 13px;
  color: var(--el-text-color-regular, #374151);
  padding: 0 14px;
  height: 100%;
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s, box-shadow 0.15s;
  white-space: nowrap;
}
.od-seg-btn:hover:not(.active) {
  color: #2563eb;
}
.od-seg-btn.active {
  background: #ffffff;
  color: #2563eb;
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}
.od-seg-btn:focus-visible {
  outline: 2px solid rgba(37, 99, 235, 0.35);
  outline-offset: 1px;
}

/* ── 网格试算提示 ── */
.od-grid-sizing {
  margin: 2px 0 8px;
  padding: 10px 12px;
  background: rgba(37, 99, 235, 0.04);
  border: 1px solid rgba(37, 99, 235, 0.15);
  border-radius: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
}
.od-grid-sizing-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}
.od-grid-sizing-state {
  flex: 1 1 auto;
  line-height: 1.45;
  word-break: break-word;
}
.od-grid-sizing-state.muted { color: var(--el-text-color-secondary, #9ca3af); }
.od-grid-sizing-state.ok { color: #15803d; }
.od-grid-sizing-state.warn { color: #b91c1c; }
.od-grid-sizing-reload {
  flex: 0 0 auto;
  appearance: none;
  border: 0;
  background: transparent;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  color: var(--el-text-color-secondary, #6b7280);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background-color 0.15s, color 0.15s;
}
.od-grid-sizing-reload:hover:not(:disabled) {
  background: rgba(37, 99, 235, 0.12);
  color: #2563eb;
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
  font-size: 11px;
  line-height: 1.5;
}
.od-grid-sizing-warns li.lvl-warn { color: #b45309; }
.od-grid-sizing-warns li.lvl-info { color: var(--el-text-color-secondary, #6b7280); }
.od-input-affix {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  background: var(--el-bg-color, #ffffff);
  border: 1px solid var(--el-border-color, #d1d5db);
  border-radius: 8px;
}
.od-input-affix .od-input {
  border: none;
  outline: none;
  background: transparent;
  padding: 0;
  height: 32px;
  flex: 1 1 auto;
  width: 110px;
  text-align: right;
}
.od-input-affix:focus-within {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18);
}
.od-affix {
  font-size: 11px;
  color: var(--el-text-color-secondary, #6b7280);
  letter-spacing: 0.04em;
}

.od-stepper {
  display: inline-flex;
  align-items: center;
  gap: 0;
  border: 1px solid var(--el-border-color, #d1d5db);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-bg-color, #ffffff);
}
.od-step {
  width: 32px;
  height: 36px;
  border: none;
  background: transparent;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-secondary, #6b7280);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.od-step:hover {
  background: var(--el-fill-color-light, #f3f4f6);
  color: var(--el-text-color-primary, #111827);
}
.od-stepper .od-input {
  border: none;
  border-left: 1px solid var(--el-border-color-lighter, #e5e7eb);
  border-right: 1px solid var(--el-border-color-lighter, #e5e7eb);
  border-radius: 0;
  width: 70px;
  height: 36px;
  box-shadow: none;
}

/* ── Section header ── */
.od-section {
  font-size: 12px;
  font-weight: 700;
  color: var(--el-text-color-secondary, #6b7280);
  letter-spacing: 0.04em;
  padding-left: 10px;
  border-left: 3px solid #2563eb;
  margin-top: 4px;
  text-transform: uppercase;
}

/* ── Hint / Error ── */
.od-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
  background: var(--el-fill-color-blank, #f3f4f6);
  padding: 10px 12px;
  border-radius: 8px;
  line-height: 1.5;
}
.od-error {
  font-size: 12px;
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  padding: 10px 12px;
  border-radius: 8px;
  line-height: 1.5;
}

/* Loading dots animation */
.od-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px;
  color: var(--el-text-color-secondary, #6b7280);
  font-size: 13px;
}
.od-loading-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #2563eb;
  animation: od-bounce 1.4s ease-in-out infinite both;
}
.od-loading-dot:nth-child(1) { animation-delay: -0.32s; }
.od-loading-dot:nth-child(2) { animation-delay: -0.16s; }
@keyframes od-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

/* ── 概要行（仅市价开仓展示品种/分组/方向） ── */
.od-summary {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.od-summary-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--el-fill-color-blank, #f3f4f6);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
}
.od-summary-k {
  color: var(--el-text-color-secondary, #6b7280);
}
.od-summary-v {
  color: var(--el-text-color-primary, #111827);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}


/* ── 概要行（仅市价开仓展示品种/分组/方向） ── */
.od-summary {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.od-summary-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--el-fill-color-blank, #f3f4f6);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
}
.od-summary-k {
  color: var(--el-text-color-secondary, #6b7280);
}
.od-summary-v {
  color: var(--el-text-color-primary, #111827);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
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
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
}
.od-carry-label { font-weight: 600; color: var(--el-text-color-primary, #111827); }
.od-carry-sub { font-size: 11px; }

/* 暂停提示：主人已暂停接单时，「带单」开关看着能开但实际只发主节点。
   摆在这里说清楚，免得主人以为开关失灵。 */
.od-carry-note {
  font-size: 11px;
  color: #b45309;
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
  height: 36px;
  padding: 0 18px;
  border: none;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 700;
  color: #ffffff;
  background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.28);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.15s, opacity 0.15s;
  white-space: nowrap;
}
.od-btn-primary:hover { box-shadow: 0 6px 18px rgba(37, 99, 235, 0.36); }
.od-btn-primary:active { transform: translateY(1px); }
.od-btn-primary:disabled { opacity: 0.55; cursor: not-allowed; box-shadow: none; }
.od-btn-primary-main { font-size: 13px; }
.od-btn-primary-sub {
  font-size: 11px;
  font-weight: 500;
  opacity: 0.85;
  letter-spacing: 0.02em;
  padding-left: 8px;
  border-left: 1px solid rgba(255, 255, 255, 0.35);
}
.od-btn-primary-sub.muted { opacity: 0.7; border-left-color: rgba(255, 255, 255, 0.2); }
</style>