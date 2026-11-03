<script setup lang="ts">
// 交易页：单品种报价 + 顶部持仓汇总
// 底部三个策略按钮（市价/限价/智能吸筹）任选其一 → 弹开仓参数弹窗
//   - 市价 / 限价：SL 必填，方向由后端根据 (入场价|现价) vs SL 自动判断
//   - 限价：再加一个 entry_price 必填
//   - 网格：方向必选（all / buy / sell）
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import OrderDialog, { type OrderForm } from '../components/OrderDialog.vue'
import DealsTable from '@/components/DealsTable.vue'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import { useAuthStore } from '@/stores/auth'
import { trendLabel, trendTagClass } from '@/utils/trendConfig'
import { quickTrigger, type TradingStrategyType, type QuickTriggerResult } from '@/api/trading'
import type {
  AccountSnapshot,
  CloseRequest,
    GroupOut,
  GroupSignalTaskRecord,
  ManualSignalPayload,
  ManualSignalResult,
  NodeOut,
  Position,
  QuoteInfo,
  TrendPanelData,
  TrendVerdict,
} from '@/api/types'

const hub = useHubStore()
const auth = useAuthStore()
const router = useRouter()

// 头部状态信息：MT5 登录号、服务器、在线/离线
/**
 * 主节点选取规则（仅管理员一侧改变）：
 *
 * - 管理员：只允许 `owner_user_id === 当前管理员 user_id` 区间内的 `is_primary && enabled`
 *   节点；这是后端 owner_user_id 维度的真实语义（参见 backend/node_service.py 中
 *   `_clear_primary_for_owner` / `set_primary`）。如果该范围内没有主节点，回退到
 *   模板的「尚未设置主节点」空态，不静默拿别人的主节点顶替——这条规则把
 *   「管理员的【交易】页不能拿普通用户的主节点来展示」这一点固化下来。
 * - 普通用户：保持原有 `is_primary && enabled` 行为不变。后端 `/api/nodes` 接口已
 *   经按 owner 过滤，普通用户能看到的列表里 `is_primary=true` 的节点本来就是
 *   他自己的主节点，无需在客户端再收紧。
 */
const primaryNode = computed<NodeOut | undefined>(() => {
  if (auth.isAdmin && auth.me?.user_id != null) {
    const myId = auth.me.user_id
    const owned = hub.nodes.find(
      (n) => n.is_primary && n.enabled && (n.owner_user_id ?? null) === myId,
    ) as NodeOut | undefined
    if (owned) return owned
    // 管理员名下没有主节点 → 让模板显示「尚未设置主节点」空态，
    // 不再去找"第一个 is_primary" 顶替。
    return undefined
  }
  return hub.nodes.find((n) => n.is_primary && n.enabled) as NodeOut | undefined
})

const account = computed<AccountSnapshot | null>(() => {
  if (!primaryNode.value) return null
  return hub.accounts[primaryNode.value.node_id] || null
})

function nodeOnline(n: NodeOut): boolean {
  return hub.statuses[n.node_id] === 'online' || n.status === 'online'
}
const isOnline = computed(() => !!primaryNode.value && nodeOnline(primaryNode.value))

// ── 多品种报价 ──
// 不再限制白名单，动态从账户数据中获取所有有报价或持仓的品种
// 保持与首页 HubTrendEmbed 一致的逻辑

/**
 * 当前账户所有可用品种：合并 quotes 和 positions，
 * 同时追加白名单（即使暂无数据也要展示，方便用户感知可交易品种）。
 * 按字母序排列。
 */
// 注意：白名单故意留空（`[]`），所有可用品种都从账户 quotes / positions 动态拼出，
// 所以类型直接取 `string`，避免 `(typeof [])[number]` 退化成 `never`。
const ALL_QUOTE_SYMBOLS: readonly string[] = []
type AllQuoteSymbol = string

const availableSymbols = computed<AllQuoteSymbol[]>(() => {
  const found = new Set<AllQuoteSymbol>()
  // 白名单：确保用户能感知所有常见品种
  for (const sym of ALL_QUOTE_SYMBOLS) found.add(sym)
  // quotes 中有数据的品种
  const quotes = account.value?.quotes || {}
  for (const sym of Object.keys(quotes)) {
    const q = quotes[sym]
    if ((q.bid || 0) > 0 || (q.ask || 0) > 0 || (q.mid || 0) > 0) {
      found.add(sym.toUpperCase() as AllQuoteSymbol)
    }
  }
  // 持仓中的品种
  for (const pos of account.value?.positions || []) {
    if (pos.symbol) found.add(pos.symbol.toUpperCase() as AllQuoteSymbol)
  }
  return [...found].sort()
})

// 当前选中：默认 = 第一个有数据的 symbol；快照未到时回退到白名单首项
const currentSymbol = ref<AllQuoteSymbol>(
  (availableSymbols.value[0] ?? ALL_QUOTE_SYMBOLS[0]) as AllQuoteSymbol,
)

// 当前选中的 symbol 在可用列表里突然消失（节点切换/快照清空）时，自动回退到第一个可用 symbol
watch(availableSymbols, (list) => {
  if (list.length === 0) return
  if (!list.includes(currentSymbol.value)) {
    currentSymbol.value = list[0]
  }
})

function pickSymbol(sym: string): void {
  currentSymbol.value = sym as AllQuoteSymbol
}

function currentQuote(): QuoteInfo | null {
  if (!account.value || !account.value.quotes) return null
  return account.value.quotes[currentSymbol.value] ?? null
}

/** 当前品种 mid 报价（用于 OrderDialog 计算市价开仓方向）。 */
const currentMid = computed<number | null>(
  () => currentQuote()?.mid ?? null,
)

function fmtPrice(n: number | undefined, sym: string): string {
  if (!n || !isFinite(n)) return '—'
  // JPY 类 3 位，其它 5 位
  const digits = /JPY$/.test(sym) ? 3 : 5
  return n.toFixed(digits)
}

/**
 * 当日涨跌额：MT5 的 SYMBOL_PRICE_CHANGE（相对昨收的绝对价差）。
 * 快照只上报了涨跌幅（%），这里按 mid / (1 + pct) 反推昨收再求差值，
 * 节点没上报 change 时返回 null，由模板显示「—」而不是伪造 0。
 */
function fmtDailyChangeAbs(q: QuoteInfo | null, sym: string): string | null {
  const pct = q?.change
  const mid = q?.mid
  if (pct == null || !isFinite(pct) || mid == null || !isFinite(mid) || mid <= 0) return null
  const base = mid / (1 + pct / 100)
  if (!isFinite(base) || base <= 0) return null
  const diff = mid - base
  const digits = /JPY$/.test(sym) ? 3 : 5
  const s = diff >= 0 ? '+' : ''
  return `${s}${diff.toFixed(digits)}`
}

/** 当日涨跌幅（%）：与 MT5 Market Watch「Daily Change」同口径 */
function fmtDailyChangePct(q: QuoteInfo | null): string | null {
  const pct = q?.change
  if (pct == null || !isFinite(pct)) return null
  const s = pct >= 0 ? '+' : ''
  return `${s}${pct.toFixed(2)}%`
}

/** 趋势标签配色（复用趋势面板的 tag 语义） */
function trendClass(v: TrendVerdict): string {
  return trendTagClass(v)
}
function trendText(v: TrendVerdict): string {
  return trendLabel(v)
}

/** 持仓方向（buy/sell）转中文标签 */
function fmtPosType(t: string | null | undefined): string {
  const v = (t || '').toLowerCase()
  if (v === 'buy') return '多'
  if (v === 'sell') return '空'
  return t || '—'
}

/** 主节点时间戳（秒）→ 简短本地时间；0 / NaN 视为空 */
function fmtPosTime(ts: number): string {
  if (!ts || !isFinite(ts)) return '—'
  const d = new Date(ts * 1000)
  if (isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 节点全量持仓汇总（用于顶部状态条）
const allPositions = computed<Position[]>(() => {
  return account.value?.positions || []
})
const totalPosCount = computed(() => allPositions.value.length)
const totalPosVolume = computed(() =>
  allPositions.value.reduce((a, p) => a + (p.volume || 0), 0),
)
const totalPosPnl = computed(() =>
  allPositions.value.reduce((a, p) => a + (p.profit || 0), 0),
)

// 资产数据（从 AccountSnapshot）
const accountProfit = computed(() => totalPosPnl.value) // 浮动盈亏 = 持仓 profit 之和
const accountBalance = computed(() => account.value?.balance ?? 0)
const accountEquity = computed(() => account.value?.equity ?? 0)
const accountMargin = computed(() => account.value?.margin ?? 0)
const accountFreeMargin = computed(() => account.value?.free_margin ?? 0)
const accountLeverage = computed(() => account.value?.leverage ?? 0)

// 当前品种的持仓摘要（叠加在报价卡上）
const symbolPositions = computed<Position[]>(() =>
  allPositions.value.filter((p) => p.symbol === currentSymbol.value),
)
const symbolPosCount = computed(() => symbolPositions.value.length)
const symbolPosVolume = computed(() =>
  symbolPositions.value.reduce((a, p) => a + (p.volume || 0), 0),
)
const symbolPosPnl = computed(() =>
  symbolPositions.value.reduce((a, p) => a + (p.profit || 0), 0),
)

// 全部持仓：按 ticket 倒序（最新开仓在前）
const sortedPositions = computed<Position[]>(() =>
  [...allPositions.value].sort((a, b) => (b.ticket || 0) - (a.ticket || 0)),
)

// 持仓 / 资产 / 历史订单 / 吸筹策略 tab
type DetailTab = 'positions' | 'assets' | 'history' | 'grid'
const detailTab = ref<DetailTab>('positions')

// ── 吸筹策略（运行中的网格任务） ──
// 数据来源：遍历当前用户名下所有"网格分组"（按命名规则 grid-<user>-<login>-<sym>），
// 调 GET /api/groups/{id}/signals?status=active 拉运行中的主任务，合并展示。
// 停止 = 与 GroupSignalsView 的"平仓"按钮同一接口：POST /api/groups/{id}/dispatches/{id}/close。
const gridTasks = ref<GroupSignalTaskRecord[]>([])
const gridLoading = ref(false)
const gridRefreshing = ref(false) // 轮询时为 true，避免 loading 闪烁
const closingGridTaskIds = ref<Set<number>>(new Set())
const GRID_TAB_REFRESH_MS = 5000
let gridTimer: number | null = null

function isClosingGridTask(taskId: number): boolean {
  return closingGridTaskIds.value.has(taskId)
}

/** 主任务是否仍有可停止的子任务（终态都跳过）。 */
function isGridTaskStoppable(task: GroupSignalTaskRecord): boolean {
  if (['done', 'failed', 'skipped'].includes(task.status)) return false
  return task.dispatches.some(
    (d) => !['done', 'failed', 'skipped'].includes(d.status),
  )
}

async function refreshGridTasks(initial = false): Promise<void> {
  if (!auth.me) {
    gridTasks.value = []
    return
  }
  // 进入 tab 第一次拉 → 全屏 loading；轮询 → 静默刷新
  if (initial) gridLoading.value = true
  else gridRefreshing.value = true
  try {
    // 确保 groups 是新的：用户刚在分组页新建了网格分组时也认得出来
    if (!hub.groups || hub.groups.length === 0) {
      try {
        await hub.listGroups()
      } catch {
        /* 忽略，沿用旧缓存 */
      }
    }
    const gridGroups = (hub.groups || []).filter(
      (g: GroupOut) => typeof g.name === 'string' && g.name.startsWith('grid-'),
    )
    if (gridGroups.length === 0) {
      gridTasks.value = []
      return
    }
    const all: GroupSignalTaskRecord[] = []
    for (const g of gridGroups) {
      try {
        const res = await hub.fetchGroupSignals(g.group_id, 1, 50, 'active')
        for (const t of res.items || []) {
          // 同一 signal_id 可能下发给多节点 → 多个 dispatches，但本质是 1 个网格任务。
          // fetchGroupSignals 已经按 task 去重，这里直接收。
          all.push(t)
        }
      } catch {
        /* 单个分组拉失败不影响其它分组 */
      }
    }
    all.sort((a, b) =>
      (b.opened_at || b.created_at || 0) - (a.opened_at || a.created_at || 0),
    )
    gridTasks.value = all
  } finally {
    gridLoading.value = false
    gridRefreshing.value = false
  }
}

/** 合并展示时把同一 task 下所有 dispatch 的 position_count / profit 等求和。 */
function gridTaskPositionCount(task: GroupSignalTaskRecord): number {
  return task.dispatches.reduce((a, d) => a + (d.position_count || 0), 0)
}
function gridTaskPendingCount(task: GroupSignalTaskRecord): number {
  return task.dispatches.reduce((a, d) => a + (d.pending_orders || 0), 0)
}
function gridTaskProfit(task: GroupSignalTaskRecord): number {
  return task.dispatches.reduce((a, d) => a + (d.realized_profit || 0), 0)
}

/** 网格任务的运行节点摘要（多节点用「 / 」连接）。 */
function gridTaskNodeNames(task: GroupSignalTaskRecord): string {
  const names = task.dispatches
    .map((d) => d.node_name || d.node_id)
    .filter(Boolean)
  if (names.length === 0) return '—'
  if (names.length <= 2) return names.join(' / ')
  return `${names[0]} / ${names[1]} 等 ${names.length} 节点`
}

/** 网格 action → dir-tag 配色（all 也按"多"色展示）。 */
function gridDirTagClass(action: string | null): string {
  const v = (action || '').toLowerCase()
  if (v === 'buy' || v === 'long' || v === 'all') return 'buy'
  if (v === 'sell' || v === 'short') return 'sell'
  return ''
}

async function stopGridTask(task: GroupSignalTaskRecord): Promise<void> {
  if (!isGridTaskStoppable(task) || isClosingGridTask(task.task_id)) return
  const sym = task.symbol || '?'
  const nodeSummary = gridTaskNodeNames(task)
  // 只对该 task 下的活跃 dispatch 下发 strategy_stop，不影响同组其他 task。
  // 终态的 dispatch 跳过（已经收口，没必要再发一次）。
  const activeDispatches = task.dispatches.filter(
    (d) => !['done', 'failed', 'skipped'].includes(d.status),
  )
  const dispatchCount = activeDispatches.length
  if (dispatchCount === 0) return
  try {
    await ElMessageBox.confirm(
      `确认停止 ${sym} 网格任务（${nodeSummary}）？\n将向 ${dispatchCount} 个节点按魔术号平掉该网格的全部持仓并结束策略监控。\n（同分组内的其它任务不受影响）`,
      '停止吸筹策略',
      {
        type: 'warning',
        confirmButtonText: '确认停止',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  // 按 task 粒度停止：只对该 task 下未收口的 dispatch 逐个下发 strategy_stop，
  // 不调 closeGroup（整组一键平仓会同时干掉同分组内的其它 task，与"只停一个"的需求冲突）。
  const groupId = task.group_id
  closingGridTaskIds.value = new Set([...closingGridTaskIds.value, task.task_id])
  let targetsCount = 0
  let forcedCount = 0
  let failCount = 0
  try {
    // 并发下发所有 dispatch 的停止命令，避免顺序等待拖慢交互
    const results = await Promise.allSettled(
      activeDispatches.map((d) => hub.closeGroupDispatch(groupId, d.id)),
    )
    for (let i = 0; i < results.length; i++) {
      const r = results[i]
      if (r.status === 'fulfilled') {
        const v = r.value as { status?: string }
        if (v?.status === 'closing') targetsCount += 1
        else forcedCount += 1
      } else {
        failCount += 1
      }
    }
    const parts: string[] = []
    if (targetsCount > 0) parts.push(`已下发停止 ${sym} · 成功 ${targetsCount}`)
    if (forcedCount > 0) parts.push(`离线 ${forcedCount}`)
    if (parts.length > 0) {
      // 成功 / 离线 都视为本次操作完成
      ElMessage.success(parts.join(' · '))
    } else if (failCount > 0) {
      ElMessage.error(`下发停止失败（${failCount} 个节点）`)
    } else {
      ElMessage.warning('已下发停止，结果待刷新确认')
    }
    await refreshGridTasks()
  } finally {
    const next = new Set(closingGridTaskIds.value)
    next.delete(task.task_id)
    closingGridTaskIds.value = next
  }
}

// ── 历史订单 ──
// Tab 内已换成共享组件 <DealsTable>（成交明细唯一视图），本页不再持有派发明细逻辑。
// 这里只保留网格 tab 还要用的时间格式化。

/** 把 Unix 时间戳（秒）→ 简短本地时间（含日期） */
function fmtHistoryTime(ts: number | null | undefined): string {
  if (!ts || !isFinite(ts)) return '—'
  const d = new Date(ts * 1000)
  if (isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 历史订单 Tab 的成交总条数（Tab 角标用；由 DealsTable 上报）
const dealsTotal = ref(0)

/** 吸筹策略 tab：切到时拉一次 + 启轮询；切走关轮询；分组数据变化时强制重拉 */
watch(
  [detailTab, () => hub.groups.length],
  ([tab, groupLen]) => {
    if (tab === 'grid') {
      // 首次进入 或 分组数量变化（新建了网格分组） → 重新拉
      void refreshGridTasks(gridTasks.value.length === 0 || groupLen === 0)
      if (gridTimer === null) {
        gridTimer = window.setInterval(() => {
          void refreshGridTasks(false)
        }, GRID_TAB_REFRESH_MS)
      }
    } else if (gridTimer !== null) {
      window.clearInterval(gridTimer)
      gridTimer = null
    }
  },
  { immediate: true },
)

// ── 弹窗（开仓参数）──
const STRATEGY_LABEL: Record<TradingStrategyType, string> = {
  trend_market: '市价开仓',
  trend_limit: '限价开仓',
  grid: '智能吸筹',
}

const dialogVisible = ref(false)
const submitting = ref(false)
const lastError = ref('')
const defaultVolume = ref(0.01)

// 市价开仓走 strategy 模型需要真实 group_id；
// 按 dialogGroupName（与后端命名规则一致）在 hub.groups 里查找。
// 找不到（用户从未创建过该分组）→ OrderDialog 会拒绝触发 strategy 信号。
const dialogGroupId = ref<string | null>(null)

/** OrderDialog 组件引用，用于在「策略复用」触发后回写结果 / 错误 */
const orderDialogRef = ref<InstanceType<typeof OrderDialog> | null>(null)

// 策略类型 + 表单（与后端 _group_name 字段对齐：strategy_type-username-login-symbol）
const strategyType = ref<TradingStrategyType>('trend_market')

const form = reactive<OrderForm & { grid_step?: number | null; grid_layers?: number | null }>({
  symbol: 'EURUSD' as string,
  volume: 0.01,
  sl: 0 as number | null,
  tp: 0 as number | null,
  entry_price: 0 as number | null,
  /** 网格模式：all（多头）/ sell（空头） */
  direction: 'all' as 'all' | 'buy' | 'sell',
  remark: '',
})

/** 点底部策略按钮 → 打开弹窗，按钮类型决定 strategyType */
async function openDialog(type: TradingStrategyType): Promise<void> {
  if (!primaryNode.value || !isOnline.value) {
    ElMessage.warning('主节点不在线，无法触发')
    return
  }
  strategyType.value = type
  form.symbol = currentSymbol.value
  form.volume = defaultVolume.value
  form.sl = 0
  form.tp = 0
  form.entry_price = 0
  form.direction = 'all'
  form.remark = ''
  lastError.value = ''
  dialogGroupId.value = null
  // 预拉一次分组列表：
  // 必须拿到真实 group_id 才能调 POST /api/console/manual-signal 的 strategy 模型（市价开仓）。
  // 拉不到或没匹配上时，市价开仓弹窗会拒绝触发；限价 / 网格不受影响（仍走原 submit 路径）。
  try {
    const groups = await hub.listGroups()
    dialogGroupId.value = resolveDialogGroupId(groups)
  } catch {
    dialogGroupId.value = null
  }
  dialogVisible.value = true
}

const canSubmit = computed(() => {
  if (!primaryNode.value || !isOnline.value) return false
  if (!form.symbol) return false
  // 限价：必须填入场价
  if (strategyType.value === 'trend_limit' && (!form.entry_price || form.entry_price <= 0)) {
    return false
  }
  // 市价 / 限价：止损价必填（后端会基于 SL 自动判断多空）
  if (
    strategyType.value !== 'grid' &&
    (!form.sl || form.sl <= 0)
  ) {
    return false
  }
  return true
})

const dialogGroupName = computed(() => {
  if (!primaryNode.value) return ''
  const login = primaryNode.value.mt5_login ?? 'unknown'
  const username = auth.me?.username || 'unknown'
  return `${strategyType.value}-${username}-${login}-${form.symbol}`
})

/** 按 dialogGroupName 在 hub.groups 里查找对应的 group_id。
 *  找不到时返回 null（OrderDialog 会回落到原 quickTrigger 路径）。*/
function resolveDialogGroupId(groups: GroupOut[]): string | null {
  const target = dialogGroupName.value
  if (!target) return null
  const hit = groups.find((g) => g.name === target)
  return hit?.group_id ?? null
}

function closeDialog(): void {
  dialogVisible.value = false
}

async function submit(): Promise<void> {
  if (!primaryNode.value) return
  submitting.value = true
  lastError.value = ''
  try {
    // 手数兜底：未填 / 0 / NaN / 负数 → 0.01，
    // 避免后端「开仓信号必须填写手数」拦截
    const rawVolume = form.volume
    const safeVolume =
      rawVolume == null || isNaN(rawVolume) || rawVolume <= 0 ? 0.01 : rawVolume
    if (safeVolume !== rawVolume) form.volume = safeVolume
    const payload = {
      node_id: primaryNode.value.node_id,
      strategy_type: strategyType.value,
      symbol: form.symbol,
      volume: safeVolume,
      sl: form.sl && form.sl > 0 ? form.sl : null,
      tp: form.tp && form.tp > 0 ? form.tp : null,
      entry_price: strategyType.value === 'trend_limit' ? form.entry_price : null,
      // 网格才传方向；其他模式由后端基于 SL 自动判断
      direction:
        strategyType.value === 'grid' ? form.direction : null,
      remark: form.remark || null,
    }
    console.log(payload)
    const r: QuickTriggerResult = await quickTrigger(payload)
    if (r.result.status === 'accepted') {
      ElMessage.success(
        `${STRATEGY_LABEL[strategyType.value]} ${form.symbol}（${r.result.targets ?? 0} 个目标节点）`,
      )
      dialogVisible.value = false
    } else if (r.result.status === 'duplicate') {
      ElMessage.warning('重复信号被抑制（5 秒内已有相同信号）')
    } else if (r.result.status === 'rejected') {
      lastError.value = r.result.reason || '触发被拒收'
    } else {
      ElMessage.info(`已提交：${r.result.status}`)
    }
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastError.value = err?.response?.data?.detail || err?.message || '触发失败'
  } finally {
    submitting.value = false
  }
}

/** OrderDialog 「策略复用」开关开启后的回调：调 POST /api/console/manual-signal，
 *  与 StrategyFormModal.triggerBoundGroup 同一条路径。
 *  payload 已是 ManualSignalPayload（含 model=strategy + group_ids=[group_id]）。
 *  这里只负责调接口、回传结果 / 错误给 OrderDialog。*/
async function triggerStrategyFromDialog(
  payload: ManualSignalPayload,
): Promise<void> {
  try {
    const res: ManualSignalResult = await hub.triggerManualSignal(payload)
    if (res.status === 'accepted' || res.status === 'duplicate') {
      // 成功 / 重复都关掉弹窗（重复是幂等保护，无副作用）
      dialogVisible.value = false
    }
    orderDialogRef.value?.onStrategyResult(res)
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    const msg = err?.response?.data?.detail || err?.message || '策略复用触发失败'
    orderDialogRef.value?.onStrategyError(msg)
  }
}

// ── 单笔持仓平仓 ──
// 走 POST /api/nodes/{id}/close，body={target:'ticket', ticket}。
// 防止双击：loading 状态以 ticket 维度记录到 closingTickets Set。
const closingTickets = ref<Set<number>>(new Set())
function isClosing(ticket: number): boolean {
  return closingTickets.value.has(ticket)
}
async function closePosition(p: Position): Promise<void> {
  if (!primaryNode.value) {
    ElMessage.warning('主节点不存在')
    return
  }
  if (!p.ticket) {
    ElMessage.warning('该持仓缺少 ticket，无法平仓')
    return
  }
  if (closingTickets.value.has(p.ticket)) return
  const type = (p.type || '').toLowerCase()
  const dirLabel = type === 'buy' ? '多' : type === 'sell' ? '空' : ''
  const sym = p.symbol
  const vol = (p.volume || 0).toFixed(2)
  try {
    await ElMessageBox.confirm(
      `确认平仓 #${p.ticket} ${sym} ${dirLabel} ${vol} 手？`,
      '平仓确认',
      {
        type: 'warning',
        confirmButtonText: '确认平仓',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  const body: CloseRequest = { target: 'ticket', ticket: p.ticket }
  closingTickets.value = new Set([...closingTickets.value, p.ticket])
  try {
    await hub.closeNode(primaryNode.value.node_id, body)
    ElMessage.success(`已请求平仓 #${p.ticket} ${sym}`)
    // 立即拉一次快照，避免等 5s 轮询
    hub.fetchNodeAccount(primaryNode.value.node_id).catch(() => {})
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    const msg = err?.response?.data?.detail || err?.message || `平仓失败 #${p.ticket}`
    ElMessage.error(msg)
  } finally {
    const next = new Set(closingTickets.value)
    next.delete(p.ticket)
    closingTickets.value = next
  }
}

// ── 当前品种的多空结论（趋势面板同源） ──
// 与【趋势面板】读同一套后端判定（GET /api/nodes/{id}/trend），这里只取结论标签，
// 不复制指标算法：周期与阈值都沿用全局已保存配置，切换品种时重取。
// 节点离线 / 品种无 K 线时后端会报错，这里静默降级成「暂无结论」，不打断报价展示。
const TREND_POLL_MS = 15000
const trendData = ref<TrendPanelData | null>(null)
const trendVerdict = computed<TrendVerdict>(
  () => trendData.value?.trend ?? 'unknown',
)
const trendReady = computed(() => !!trendData.value?.ready)

async function loadTrend(): Promise<void> {
  if (!primaryNode.value || !currentSymbol.value) {
    trendData.value = null
    return
  }
  const nodeId = primaryNode.value.node_id
  const sym = currentSymbol.value
  try {
    const res = await hub.fetchNodeTrend(nodeId, sym)
    // 响应回来时用户可能已切走品种 / 节点，丢弃过期结论
    if (
      primaryNode.value?.node_id !== nodeId ||
      currentSymbol.value !== sym
    ) {
      return
    }
    trendData.value = res
  } catch {
    // 离线、无 K 线等一律降级：不显示结论，不打断页面其它数据
    trendData.value = null
  }
}

// 切换主节点 / 品种后立刻对齐结论，避免停留在上一个品种的多空方向
watch([primaryNode, currentSymbol], () => {
  void loadTrend()
})

// 简单轮询拉取快照（保持行情新鲜）
let pollTimer: number | null = null
let trendTimer: number | null = null
onMounted(async () => {
  await hub.fetchNodes()
  if (primaryNode.value && !hub.accounts[primaryNode.value.node_id]) {
    try {
      await hub.fetchNodeAccount(primaryNode.value.node_id)
    } catch {
      /* 节点尚未上报快照，忽略 */
    }
  }
  void loadTrend()
  pollTimer = window.setInterval(() => {
    if (primaryNode.value) {
      hub.fetchNodeAccount(primaryNode.value.node_id).catch(() => {})
    }
  }, 5000)
  trendTimer = window.setInterval(() => {
    void loadTrend()
  }, TREND_POLL_MS)
})
onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
  if (trendTimer !== null) {
    window.clearInterval(trendTimer)
    trendTimer = null
  }
  if (gridTimer !== null) {
    window.clearInterval(gridTimer)
    gridTimer = null
  }
})
</script>

<template>
  <div class="pf-page tr-page">
    <!-- 空态：无主节点 -->
    <div v-if="!primaryNode" class="pf-card tr-empty">
      <span class="tr-empty-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="6" rx="2" />
          <rect x="3" y="15" width="18" height="6" rx="2" />
          <path d="M7 6h.01M7 18h.01" />
        </svg>
      </span>
      <p class="tr-empty-title">尚未设置主节点</p>
      <p class="pf-hint">请先在【节点】页将一个节点标记为主节点，再回到此页面触发开仓。</p>
      <button class="pf-btn pf-btn-primary" @click="router.push('/nodes')">前往节点页</button>
    </div>

    <template v-else>
      <!-- 状态条：主节点身份 + 在线状态 + 账户汇总 -->
      <section class="pf-card">
        <div class="pf-card-head">
          <div class="pf-card-head-main">
            <span class="pf-card-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="3" width="18" height="6" rx="2" />
                <rect x="3" y="15" width="18" height="6" rx="2" />
                <path d="M7 6h.01M7 18h.01" />
              </svg>
            </span>
            <div>
              <p class="pf-card-title">{{ primaryNode.name }}</p>
              <p class="pf-card-sub">MT5 <b>{{ primaryNode.mt5_login ?? '—' }}</b></p>
            </div>
          </div>
          <span class="pf-pill" :class="isOnline ? 'green' : 'grey'">
            <span v-if="isOnline" class="pf-pill-dot" aria-hidden="true"></span>
            {{ isOnline ? '在线' : '离线' }}
          </span>
        </div>

        <div class="pf-kv-grid" style="margin-top: 10px">
          <div class="pf-kv">
            <span class="k">当前持仓</span>
            <span class="v">{{ totalPosCount }} 笔</span>
          </div>
          <div class="pf-kv">
            <span class="k">合计手数</span>
            <span class="v">{{ totalPosVolume.toFixed(2) }}</span>
          </div>
          <div class="pf-kv">
            <span class="k">浮动盈亏</span>
            <span class="v" :class="totalPosPnl >= 0 ? 'pos' : 'neg'">
              {{ totalPosPnl >= 0 ? '+' : '' }}{{ totalPosPnl.toFixed(2) }}
            </span>
          </div>
          <div class="pf-kv">
            <span class="k">净值</span>
            <span class="v">{{ accountEquity.toFixed(2) }}</span>
          </div>
        </div>
      </section>

      <!-- 单品种报价 -->
      <section class="pf-card">
        <div class="pf-card-head">
          <div class="pf-card-head-main">
            <span class="pf-card-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 17l5-6 4 4 5-7 4 4" />
                <path d="M3 21h18" />
              </svg>
            </span>
            <div>
              <p class="pf-card-title">报价</p>
              <p class="pf-card-sub">{{ currentSymbol }} · 主节点最新快照</p>
            </div>
          </div>
          <span class="pf-pill" :class="trendReady ? (trendClass(trendVerdict) || 'grey') : 'grey'">
            {{ trendReady ? trendText(trendVerdict) : '暂无结论' }}
          </span>
        </div>

        <!-- 品种切换 -->
        <div v-if="availableSymbols.length" class="tr-syms" role="tablist" aria-label="品种">
          <button
            v-for="sym in availableSymbols"
            :key="sym"
            type="button"
            class="tr-sym"
            :class="{ active: currentSymbol === sym }"
            :aria-pressed="currentSymbol === sym"
            @click="pickSymbol(sym)"
          >{{ sym }}</button>
        </div>
        <p v-else class="pf-hint">主节点尚未上报任何品种报价</p>

        <!-- 价格 + 涨跌 -->
        <div class="tr-quote">
          <p class="tr-price">{{ fmtPrice(currentQuote()?.mid, currentSymbol) }}</p>
          <div class="tr-chg">
            <span
              class="tr-chg-pill"
              :class="(currentQuote()?.change ?? 0) >= 0 ? 'pos' : 'neg'"
            >{{ fmtDailyChangeAbs(currentQuote(), currentSymbol) ?? '—' }}</span>
            <span
              class="tr-chg-pct"
              :class="(currentQuote()?.change ?? 0) >= 0 ? 'pos' : 'neg'"
            >{{ fmtDailyChangePct(currentQuote()) ?? '—' }}</span>
          </div>
        </div>

        <!-- 当前品种持仓摘要 -->
        <div v-if="symbolPosCount > 0" class="pf-kv-grid" style="margin-top: 10px">
          <div class="pf-kv">
            <span class="k">{{ currentSymbol }} 持仓</span>
            <span class="v">{{ symbolPosCount }} 笔 · {{ symbolPosVolume.toFixed(2) }} 手</span>
          </div>
          <div class="pf-kv">
            <span class="k">浮动盈亏</span>
            <span class="v" :class="symbolPosPnl >= 0 ? 'pos' : 'neg'">
              {{ symbolPosPnl >= 0 ? '+' : '' }}{{ symbolPosPnl.toFixed(2) }}
            </span>
          </div>
        </div>
      </section>

      <!-- 三个策略按钮 -->
      <div class="tr-actions">
        <button
          type="button"
          class="tr-action"
          :disabled="!isOnline"
          @click="openDialog('trend_market')"
        >
          <span class="tr-action-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
            </svg>
          </span>
          <span class="tr-action-label">市价开仓</span>
        </button>
        <button
          type="button"
          class="tr-action"
          :disabled="!isOnline"
          @click="openDialog('trend_limit')"
        >
          <span class="tr-action-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9" />
              <circle cx="12" cy="12" r="5" />
              <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
              <path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3" />
            </svg>
          </span>
          <span class="tr-action-label">限价开仓</span>
        </button>
        <button
          type="button"
          class="tr-action"
          :disabled="!isOnline"
          @click="openDialog('grid')"
        >
          <span class="tr-action-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 6h18M3 12h18M3 18h18" />
              <path d="M6 4v16M12 4v16M18 4v16" />
            </svg>
          </span>
          <span class="tr-action-label">智能吸筹</span>
        </button>
      </div>
      <p v-if="!isOnline" class="pf-hint tr-offline">主节点离线，无法触发</p>

      <!-- 持仓 / 资产 / 历史订单 / 吸筹策略 -->
      <section class="pf-card">
        <div class="pf-tabs tr-tabs" role="tablist">
          <button
            class="pf-tab"
            :class="{ active: detailTab === 'positions' }"
            role="tab"
            :aria-selected="detailTab === 'positions'"
            @click="detailTab = 'positions'"
          >
            当前持仓
            <span v-if="totalPosCount > 0" class="tr-tab-count">{{ totalPosCount }}</span>
          </button>
          <button
            class="pf-tab"
            :class="{ active: detailTab === 'assets' }"
            role="tab"
            :aria-selected="detailTab === 'assets'"
            @click="detailTab = 'assets'"
          >
            资产
          </button>
          <button
            class="pf-tab"
            :class="{ active: detailTab === 'history' }"
            role="tab"
            :aria-selected="detailTab === 'history'"
            @click="detailTab = 'history'"
          >
            历史订单
            <span v-if="dealsTotal > 0" class="tr-tab-count muted">{{ dealsTotal }}</span>
          </button>
          <button
            class="pf-tab"
            :class="{ active: detailTab === 'grid' }"
            role="tab"
            :aria-selected="detailTab === 'grid'"
            @click="detailTab = 'grid'"
          >
            吸筹策略
            <span v-if="gridTasks.length > 0" class="tr-tab-count muted">{{ gridTasks.length }}</span>
          </button>
        </div>

        <div class="tr-body">
          <!-- 当前持仓 -->
          <div v-if="detailTab === 'positions'" role="tabpanel">
            <template v-if="allPositions.length">
              <!-- 桌面端：分隔行表格 -->
              <div class="pf-rows tr-rows tr-rows-pos">
                <div class="pf-rows-inner">
                  <div class="pf-row pf-row-head">
                    <span>品种</span>
                    <span>方向</span>
                    <span>手数</span>
                    <span>开仓价</span>
                    <span>现价</span>
                    <span>止损</span>
                    <span>止盈</span>
                    <span>开仓时间</span>
                    <span>浮盈</span>
                    <span></span>
                  </div>
                  <div v-for="p in sortedPositions" :key="p.ticket" class="pf-row">
                    <span class="c-strong">{{ p.symbol }}</span>
                    <span>
                      <span class="pf-pill" :class="(p.type || '').toLowerCase() === 'buy' ? 'green' : 'red'">
                        {{ fmtPosType(p.type) }}
                      </span>
                    </span>
                    <span class="c-mono">{{ (p.volume || 0).toFixed(2) }}</span>
                    <span class="c-mono">{{ fmtPrice(p.price_open, p.symbol) }}</span>
                    <span class="c-mono">{{ fmtPrice(p.price_current, p.symbol) }}</span>
                    <span class="c-muted">{{ p.sl ? fmtPrice(p.sl, p.symbol) : '—' }}</span>
                    <span class="c-muted">{{ p.tp ? fmtPrice(p.tp, p.symbol) : '—' }}</span>
                    <span class="c-muted">{{ fmtPosTime(p.time) }}</span>
                    <span class="c-mono" :class="(p.profit || 0) >= 0 ? 'c-green' : 'c-red'">
                      {{ (p.profit || 0) >= 0 ? '+' : '' }}{{ (p.profit || 0).toFixed(2) }}
                    </span>
                    <span>
                      <button
                        class="pf-btn pf-btn-mini pf-btn-danger"
                        :disabled="isClosing(p.ticket)"
                        :title="`平仓 #${p.ticket}`"
                        @click="closePosition(p)"
                      >{{ isClosing(p.ticket) ? '中…' : '平' }}</button>
                    </span>
                  </div>
                </div>
              </div>
              <!-- 手机端：卡片列表 -->
              <div class="tr-cards mobile-only">
                <div v-for="p in sortedPositions" :key="p.ticket" class="tr-card">
                  <div class="tr-card-head">
                    <div class="tr-card-left">
                      <span class="c-strong">{{ p.symbol }}</span>
                      <span class="pf-pill" :class="(p.type || '').toLowerCase() === 'buy' ? 'green' : 'red'">
                        {{ fmtPosType(p.type) }}
                      </span>
                    </div>
                    <div class="tr-card-right">
                      <span class="tr-card-pnl" :class="(p.profit || 0) >= 0 ? 'pos' : 'neg'">
                        {{ (p.profit || 0) >= 0 ? '+' : '' }}{{ (p.profit || 0).toFixed(2) }}
                      </span>
                      <button
                        class="pf-btn pf-btn-mini pf-btn-danger"
                        :disabled="isClosing(p.ticket)"
                        @click="closePosition(p)"
                      >{{ isClosing(p.ticket) ? '中…' : '平' }}</button>
                    </div>
                  </div>
                  <div class="tr-card-grid">
                    <div class="pf-kv"><span class="k">手数</span><span class="v">{{ (p.volume || 0).toFixed(2) }}</span></div>
                    <div class="pf-kv"><span class="k">开仓</span><span class="v">{{ fmtPrice(p.price_open, p.symbol) }}</span></div>
                    <div class="pf-kv"><span class="k">现价</span><span class="v">{{ fmtPrice(p.price_current, p.symbol) }}</span></div>
                    <div class="pf-kv"><span class="k">止损</span><span class="v dim">{{ p.sl ? fmtPrice(p.sl, p.symbol) : '—' }}</span></div>
                    <div class="pf-kv"><span class="k">止盈</span><span class="v dim">{{ p.tp ? fmtPrice(p.tp, p.symbol) : '—' }}</span></div>
                    <div class="pf-kv"><span class="k">开仓时间</span><span class="v dim">{{ fmtPosTime(p.time) }}</span></div>
                  </div>
                </div>
              </div>
            </template>
            <p v-else class="pf-empty">暂无持仓</p>
          </div>

          <!-- 资产 -->
          <div v-if="detailTab === 'assets'" role="tabpanel">
            <div class="pf-kv-grid">
              <div class="pf-kv">
                <span class="k">净值（Equity）</span>
                <span class="v" :class="accountEquity >= 0 ? 'pos' : 'neg'">
                  {{ accountEquity >= 0 ? '+' : '' }}{{ accountEquity.toFixed(2) }}
                </span>
              </div>
              <div class="pf-kv">
                <span class="k">浮动盈亏</span>
                <span class="v" :class="accountProfit >= 0 ? 'pos' : 'neg'">
                  {{ accountProfit >= 0 ? '+' : '' }}{{ accountProfit.toFixed(2) }}
                </span>
              </div>
              <div class="pf-kv">
                <span class="k">余额（Balance）</span>
                <span class="v">{{ accountBalance.toFixed(2) }}</span>
              </div>
              <div class="pf-kv">
                <span class="k">已用保证金（Margin）</span>
                <span class="v dim">{{ accountMargin.toFixed(2) }}</span>
              </div>
              <div class="pf-kv">
                <span class="k">可用保证金（Free）</span>
                <span class="v">{{ accountFreeMargin.toFixed(2) }}</span>
              </div>
              <div class="pf-kv">
                <span class="k">杠杆</span>
                <span class="v">1:{{ accountLeverage }}</span>
              </div>
            </div>
          </div>

          <!-- 历史订单：成交明细 -->
          <div v-if="detailTab === 'history'" role="tabpanel">
            <DealsTable :node-id="primaryNode?.node_id ?? null" @total="dealsTotal = $event" />
          </div>

          <!-- 吸筹策略（运行中的网格任务） -->
          <div v-if="detailTab === 'grid'" role="tabpanel">
            <div v-if="gridLoading && gridTasks.length === 0" class="pf-empty">加载中…</div>
            <div v-else-if="!gridLoading && gridTasks.length === 0" class="pf-empty">暂无运行中的网格策略</div>
            <template v-else>
              <p v-if="gridRefreshing" class="pf-hint tr-refresh">刷新中…</p>
              <div class="pf-rows tr-rows tr-rows-grid">
                <div class="pf-rows-inner">
                  <div class="pf-row pf-row-head">
                    <span>品种</span>
                    <span>方向</span>
                    <span>节点</span>
                    <span>持仓</span>
                    <span>挂单</span>
                    <span>盈亏</span>
                    <span>启动</span>
                    <span>信号 ID</span>
                    <span></span>
                  </div>
                  <div v-for="t in gridTasks" :key="t.task_id" class="pf-row">
                    <span class="c-strong">{{ t.symbol || '—' }}</span>
                    <span>
                      <span class="pf-pill" :class="gridDirTagClass(t.action) === 'buy' ? 'green' : 'red'">
                        {{ fmtPosType(t.action) }}
                      </span>
                    </span>
                    <span class="c-muted" :title="gridTaskNodeNames(t)">{{ gridTaskNodeNames(t) }}</span>
                    <span class="c-mono">{{ gridTaskPositionCount(t) }}</span>
                    <span class="c-mono c-muted">{{ gridTaskPendingCount(t) || '—' }}</span>
                    <span class="c-mono" :class="gridTaskProfit(t) >= 0 ? 'c-green' : 'c-red'">
                      {{ gridTaskProfit(t) >= 0 ? '+' : '' }}{{ gridTaskProfit(t).toFixed(2) }}
                    </span>
                    <span class="c-muted">{{ fmtHistoryTime(t.opened_at || t.created_at) }}</span>
                    <span class="c-mono c-muted" :title="t.signal_id">{{ t.signal_id }}</span>
                    <span>
                      <button
                        class="pf-btn pf-btn-mini pf-btn-danger"
                        :disabled="isClosingGridTask(t.task_id) || !isGridTaskStoppable(t)"
                        :title="isGridTaskStoppable(t) ? `停止 ${t.symbol} 网格任务` : '该任务已结束'"
                        @click="stopGridTask(t)"
                      >{{ isClosingGridTask(t.task_id) ? '中…' : '停' }}</button>
                    </span>
                  </div>
                </div>
              </div>
              <div class="tr-cards mobile-only">
                <div v-for="t in gridTasks" :key="`m-${t.task_id}`" class="tr-card">
                  <div class="tr-card-head">
                    <div class="tr-card-left">
                      <span class="c-strong">{{ t.symbol || '—' }}</span>
                      <span class="pf-pill" :class="gridDirTagClass(t.action) === 'buy' ? 'green' : 'red'">
                        {{ fmtPosType(t.action) }}
                      </span>
                      <span class="pf-pill grey">网格</span>
                    </div>
                    <div class="tr-card-right">
                      <button
                        class="pf-btn pf-btn-mini pf-btn-danger"
                        :disabled="isClosingGridTask(t.task_id) || !isGridTaskStoppable(t)"
                        @click="stopGridTask(t)"
                      >{{ isClosingGridTask(t.task_id) ? '中…' : '停' }}</button>
                    </div>
                  </div>
                  <div class="tr-card-grid">
                    <div class="pf-kv"><span class="k">节点</span><span class="v">{{ gridTaskNodeNames(t) }}</span></div>
                    <div class="pf-kv"><span class="k">持仓</span><span class="v">{{ gridTaskPositionCount(t) }}</span></div>
                    <div class="pf-kv"><span class="k">挂单</span><span class="v dim">{{ gridTaskPendingCount(t) || '—' }}</span></div>
                    <div class="pf-kv"><span class="k">盈亏</span><span class="v" :class="gridTaskProfit(t) >= 0 ? 'pos' : 'neg'">
                      {{ gridTaskProfit(t) >= 0 ? '+' : '' }}{{ gridTaskProfit(t).toFixed(2) }}
                    </span></div>
                    <div class="pf-kv span-full"><span class="k">信号 ID</span><span class="v dim">{{ t.signal_id }}</span></div>
                    <div class="pf-kv span-full"><span class="k">启动</span><span class="v dim">{{ fmtHistoryTime(t.opened_at || t.created_at) }}</span></div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </section>
    </template>

    <!-- 触发弹窗：开仓参数 -->
    <OrderDialog
      ref="orderDialogRef"
      v-model="dialogVisible"
      v-model:strategy="strategyType"
      :form="form"
      :group-name="dialogGroupName"
      :group-id="dialogGroupId"
      :submitting="submitting"
      :last-error="lastError"
      :can-submit="canSubmit"
      :current-mid="currentMid"
      @update:form="(v) => Object.assign(form, v)"
      @submit="submit"
      @cancel="closeDialog"
      @trigger-strategy="triggerStrategyFromDialog"
    />
  </div>
</template>

<style scoped>
/* ── 页面骨架（对齐 UI/H5.html 原型的深色卡片语言）── */
.tr-page {
  gap: 12px;
}

/* ── 空态 ── */
.tr-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 20px;
  text-align: center;
}
.tr-empty-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: rgba(37, 99, 235, 0.15);
  color: #60a5fa;
}
.tr-empty-icon svg { width: 22px; height: 22px; }
.tr-empty-title {
  margin: 4px 0 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--pf-text);
}
.tr-empty .pf-btn { margin-top: 8px; }

/* ── 品种切换条（横向滚动，不换行挤压）── */
.tr-syms {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  margin-top: 10px;
}
.tr-syms::-webkit-scrollbar { display: none; }
.tr-sym {
  flex: 0 0 auto;
  padding: 6px 12px;
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-muted);
  font-size: 12px;
  font-weight: 500;
  font-family: var(--mono);
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.tr-sym:hover { color: var(--pf-text); }
.tr-sym.active {
  border-color: transparent;
  background: var(--pf-blue);
  color: #fff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.35);
}

/* ── 价格区 ── */
.tr-quote {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.tr-price {
  margin: 0;
  font-size: 30px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.02em;
  font-family: var(--mono);
  color: var(--pf-text);
}
@media (min-width: 640px) {
  .tr-price { font-size: 36px; }
}
.tr-chg {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tr-chg-pill {
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  font-family: var(--mono);
}
.tr-chg-pill.pos { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
.tr-chg-pill.neg { background: rgba(246, 70, 93, 0.15); color: #f87171; }
.tr-chg-pct {
  font-size: 12px;
  font-family: var(--mono);
}
.tr-chg-pct.pos { color: #22c55e; }
.tr-chg-pct.neg { color: #f6465d; }

/* ── 三个策略按钮 ── */
.tr-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.tr-action {
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 64px;
  padding: 10px 8px;
  border-radius: 12px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-text);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, opacity 0.15s;
}
.tr-action:hover:not(:disabled) {
  border-color: rgba(37, 99, 235, 0.6);
  background: #111a2c;
}
.tr-action:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.tr-action-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  color: #60a5fa;
}
.tr-action-icon svg { width: 20px; height: 20px; }
.tr-action-label { line-height: 1.1; }
.tr-offline {
  margin: -4px 0 0;
  color: #fbbf24;
  text-align: center;
}

/* ── Tab 条 ── */
.tr-tabs {
  margin-bottom: 10px;
}
.tr-tab-count {
  padding: 0 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.18);
  font-size: 9px;
  font-family: var(--mono);
  line-height: 15px;
}
.tr-tab-count.muted {
  background: rgba(255, 255, 255, 0.1);
}
.tr-body {
  min-height: 0;
}

/* ── 数据表 ── */
.tr-rows .pf-rows-inner { min-width: 900px; }
.tr-rows-pos .pf-row-head,
.tr-rows-pos .pf-row {
  grid-template-columns:
    minmax(0, 1fr) 64px 64px 92px 92px 84px 84px 120px 92px 48px;
}
.tr-rows-grid .pf-row-head,
.tr-rows-grid .pf-row {
  grid-template-columns:
    minmax(0, 0.9fr) 64px minmax(0, 1.2fr) 64px 64px 92px 130px minmax(0, 1fr) 48px;
}
.tr-rows .pf-row { cursor: default; }
.tr-refresh {
  margin: 0 0 6px;
  text-align: right;
}

/* ── 手机端卡片 ── */
.tr-cards { display: block; }
.tr-card {
  padding: 10px;
  border-radius: 8px;
  border: 1px solid var(--pf-border);
  background: var(--pf-soft);
}
.tr-card + .tr-card { margin-top: 8px; }
.tr-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.tr-card-left,
.tr-card-right {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.tr-card-pnl {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--mono);
}
.tr-card-pnl.pos { color: #22c55e; }
.tr-card-pnl.neg { color: #f6465d; }
.tr-card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.tr-card-grid .pf-kv .v { font-size: 12px; }

@media (max-width: 640px) {
  .tr-price { font-size: 26px; }
  .tr-action { min-height: 56px; font-size: 12px; }
  .tr-action-icon svg { width: 18px; height: 18px; }
  .tr-rows { display: none; }
}
@media (max-width: 420px) {
  .tr-price { font-size: 24px; }
  .tr-actions { gap: 6px; }
  .tr-action { min-height: 52px; padding: 8px 4px; }
}
</style>
