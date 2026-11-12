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
import { fetchNodeDeals } from '@/api/deals'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import { useAuthStore } from '@/stores/auth'
import { confirmAction } from '@/utils/confirm'
import { trendLabel, trendTagClass } from '@/utils/trendConfig'
import { quickTrigger, type TradingStrategyType, type QuickTriggerResult } from '@/api/trading'
import type {
  AccountSnapshot,
  CloseRequest,
    GroupOut,
  GroupSignalTaskRecord,
  ManualSignalPayload,
  ManualSignalResult,
  DealRecord,
  NodeOut,
  PendingOrder,
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

// 历史交易角标：近 7 日成交条数，由 loadTradeHistory 写入。
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

const SYMBOL_NAMES: Record<string, string> = {
  XAUUSD: '黄金',
  XAGUSD: '白银',
  EURUSD: '欧元兑美元',
  GBPUSD: '英镑兑美元',
  USDJPY: '美元兑日元',
  AUDUSD: '澳元兑美元',
  USDCAD: '美元兑加元',
  USDCHF: '美元兑瑞郎',
  NZDUSD: '纽元兑美元',
}
function symbolAlias(sym: string): string {
  const key = (sym || '').replace(/[^A-Za-z]/g, '').toUpperCase()
  return SYMBOL_NAMES[key] || ''
}
function isBuySide(type: string | null | undefined): boolean {
  return (type || '').toLowerCase() === 'buy'
}
function fmtPx(n: number | null | undefined, sym: string): string {
  if (n == null || !isFinite(n) || n === 0) return '—'
  const digits = /JPY$/i.test(sym) ? 3 : Math.abs(n) >= 50 ? 2 : 5
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
function fmtSigned(n: number): string {
  const abs = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return n >= 0 ? `+${abs}` : `-${abs}`
}
function fmtHm(ts: number | null | undefined): string {
  if (!ts || !isFinite(ts)) return '—'
  const d = new Date(ts * 1000)
  if (isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

interface SymbolBook {
  symbol: string
  rows: Position[]
  buy: number
  sell: number
  volume: number
  profit: number
}
const positionBooks = computed<SymbolBook[]>(() => {
  const map = new Map<string, Position[]>()
  for (const p of allPositions.value) {
    const sym = p.symbol || '—'
    const bucket = map.get(sym) || []
    bucket.push(p)
    map.set(sym, bucket)
  }
  return [...map.entries()].map(([symbol, rows]) => {
    const sorted = [...rows].sort((a, b) => (a.ticket || 0) - (b.ticket || 0))
    let buy = 0
    let sell = 0
    let volume = 0
    let profit = 0
    for (const row of sorted) {
      if (isBuySide(row.type)) buy += 1
      else sell += 1
      volume += row.volume || 0
      profit += row.profit || 0
    }
    return { symbol, rows: sorted, buy, sell, volume, profit }
  }).sort((a, b) => a.symbol.localeCompare(b.symbol))
})
function pendingOf(symbol: string): PendingOrder[] {
  return (account.value?.orders || []).filter((o) => o.symbol === symbol)
}
const actionBusy = ref('')

async function closeSymbolAll(symbol: string): Promise<void> {
  if (!primaryNode.value || actionBusy.value) return
  const n = allPositions.value.filter((p) => p.symbol === symbol).length
  if (!n) { ElMessage.info('该品种没有持仓'); return }
  if (!(await confirmAction(
    `确认平掉 ${symbol} 的全部 ${n} 笔持仓？\n同品种策略仍在运行时可能再次开仓。`,
    '一键平仓',
  ))) return
  actionBusy.value = `all:${symbol}`
  try {
    await hub.closeNode(primaryNode.value.node_id, { target: 'symbol', symbol })
    ElMessage.success(`已提交平掉 ${symbol}`)
    hub.fetchNodeAccount(primaryNode.value.node_id).catch(() => {})
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err?.response?.data?.detail || '平仓失败')
  } finally {
    actionBusy.value = ''
  }
}

async function closeSymbolProfit(symbol: string): Promise<void> {
  if (!primaryNode.value || actionBusy.value) return
  const winners = allPositions.value.filter((p) => p.symbol === symbol && (p.profit || 0) > 0 && p.ticket)
  if (!winners.length) { ElMessage.info('该品种没有盈利持仓'); return }
  if (!(await confirmAction(
    `确认平掉 ${symbol} 当前浮动盈利的 ${winners.length} 笔持仓？\n不会终止策略，策略仍在运行时可能再次开仓。`,
    '平盈',
  ))) return
  actionBusy.value = `profit:${symbol}`
  let sent = 0
  let failed = 0
  for (const p of winners) {
    try {
      await hub.closeNode(primaryNode.value.node_id, { target: 'ticket', ticket: p.ticket })
      sent += 1
    } catch {
      failed += 1
    }
  }
  actionBusy.value = ''
  if (failed && !sent) ElMessage.error('平盈失败')
  else if (failed) ElMessage.warning(`已提交 ${sent} 笔，${failed} 笔失败`)
  else ElMessage.success(`已提交平掉 ${sent} 笔盈利持仓`)
  hub.fetchNodeAccount(primaryNode.value.node_id).catch(() => {})
}

async function cancelSymbolPending(symbol: string): Promise<void> {
  if (!primaryNode.value || actionBusy.value) return
  const orders = pendingOf(symbol)
  if (!orders.length) { ElMessage.info('该品种没有挂单'); return }
  const magics = [...new Set(orders.map((o) => o.magic).filter((m) => m))]
  const manuals = orders.filter((o) => !o.magic)
  if (!magics.length) {
    ElMessage.warning('该品种只有手工挂单，请到节点主页删除全部挂单')
    return
  }
  const lines = orders.slice(0, 12).map((o) => `• ${o.type}  ${o.volume}手  @${o.price_open}`).join('\n')
  const manualNote = manuals.length ? `\n其中 ${manuals.length} 张手工挂单不会在这里删除。` : ''
  if (!(await confirmAction(
    `确认删除 ${symbol} 的策略挂单？\n只删除这些魔术号上的挂单，持仓不动，其他品种的策略不受影响。${manualNote}\n\n${lines}`,
    '删挂单',
  ))) return
  actionBusy.value = `pend:${symbol}`
  try {
    await hub.cancelPendingAll(primaryNode.value.node_id, { scope: 'strategy', magics })
    ElMessage.success(`已提交删除 ${symbol} 挂单`)
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err?.response?.data?.detail || '删挂单失败')
  } finally {
    actionBusy.value = ''
  }
}

interface GridBook {
  symbol: string
  tasks: GroupSignalTaskRecord[]
  profit: number
  positions: number
  volumeLabel: string
}
const gridBooks = computed<GridBook[]>(() => {
  const map = new Map<string, GroupSignalTaskRecord[]>()
  for (const task of gridTasks.value) {
    const sym = task.symbol || '—'
    const bucket = map.get(sym) || []
    bucket.push(task)
    map.set(sym, bucket)
  }
  return [...map.entries()].map(([symbol, tasks]) => ({
    symbol,
    tasks,
    profit: tasks.reduce((sum, task) => sum + gridTaskProfit(task), 0),
    positions: tasks.reduce((sum, task) => sum + gridTaskPositionCount(task), 0),
    volumeLabel: `${tasks.length} 个任务`,
  })).sort((a, b) => a.symbol.localeCompare(b.symbol))
})

interface HistRow {
  key: string
  symbol: string
  direction: string
  volume: number
  open: number
  close: number
  profit: number
  time: number
}
const historyDeals = ref<DealRecord[]>([])
const historyLoading = ref(false)
const historyWinRate = ref<number | null>(null)
const historyProfit = ref(0)

function buildHistoryRows(deals: DealRecord[]): HistRow[] {
  const buckets = new Map<string, { ins: DealRecord[]; outs: DealRecord[] }>()
  for (const deal of deals) {
    const key = deal.position_id ? `${deal.symbol || ''}:${deal.position_id}` : `deal:${deal.deal_id}`
    const bucket = buckets.get(key) || { ins: [], outs: [] }
    const entry = (deal.entry_type || '').toUpperCase()
    if (entry === 'IN') bucket.ins.push(deal)
    else if (entry === 'OUT' || entry === 'OUT_BY' || entry === 'INOUT') bucket.outs.push(deal)
    buckets.set(key, bucket)
  }
  const rows: HistRow[] = []
  for (const [key, bucket] of buckets) {
    if (!bucket.outs.length) continue
    const outs = [...bucket.outs].sort((a, b) => a.deal_time - b.deal_time)
    const ins = [...bucket.ins].sort((a, b) => a.deal_time - b.deal_time)
    const last = outs[outs.length - 1]
    const direction = ins[0]?.direction || last.position_direction || (last.direction === 'BUY' ? 'SELL' : 'BUY')
    rows.push({
      key,
      symbol: last.symbol || ins[0]?.symbol || '—',
      direction,
      volume: outs.reduce((sum, deal) => sum + (deal.volume || 0), 0),
      open: ins[0]?.price || 0,
      close: last.price || 0,
      profit: outs.reduce((sum, deal) => sum + (deal.net_profit || 0), 0),
      time: last.deal_time,
    })
  }
  rows.sort((a, b) => b.time - a.time)
  return rows
}
const historyRows = computed(() => buildHistoryRows(historyDeals.value))
const historyBooks = computed(() => {
  const map = new Map<string, HistRow[]>()
  for (const row of historyRows.value) {
    const bucket = map.get(row.symbol) || []
    bucket.push(row)
    map.set(row.symbol, bucket)
  }
  return [...map.entries()].map(([symbol, rows]) => {
    const wins = rows.filter((row) => row.profit >= 0).length
    return {
      symbol,
      rows,
      volume: rows.reduce((sum, row) => sum + row.volume, 0),
      profit: rows.reduce((sum, row) => sum + row.profit, 0),
      wins,
      losses: rows.length - wins,
    }
  })
})
const todayPnl = computed(() => {
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  const sec = start.getTime() / 1000
  return historyDeals.value
    .filter((deal) => {
      const entry = (deal.entry_type || '').toUpperCase()
      return (entry === 'OUT' || entry === 'OUT_BY' || entry === 'INOUT') && deal.deal_time >= sec
    })
    .reduce((sum, deal) => sum + (deal.net_profit || 0), 0)
})
const assetDist = computed(() => {
  const volumes = new Map<string, number>()
  for (const sym of availableSymbols.value) volumes.set(sym, 0)
  for (const book of positionBooks.value) volumes.set(book.symbol, book.volume)
  const total = [...volumes.values()].reduce((sum, n) => sum + n, 0)
  return [...volumes.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([symbol, volume]) => ({
      symbol,
      volume,
      pct: total > 0 ? Math.round((volume / total) * 100) : 0,
    }))
})

async function loadTradeHistory(): Promise<void> {
  if (!primaryNode.value) return
  historyLoading.value = true
  try {
    const from = Math.floor(Date.now() / 1000) - 7 * 24 * 3600
    const page = await fetchNodeDeals(primaryNode.value.node_id, 1, 200, { from_time: from })
    historyDeals.value = page.items
    dealsTotal.value = page.total
    historyWinRate.value = page.summary?.data_available ? page.summary.win_rate : null
    historyProfit.value = page.summary?.total_profit || 0
  } catch {
    historyDeals.value = []
  } finally {
    historyLoading.value = false
  }
}
watch(
  () => primaryNode.value?.node_id,
  () => { void loadTradeHistory() },
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
  <div class="tr">
    <div v-if="!primaryNode" class="tr-card tr-empty">
      <p class="tr-empty-title">尚未设置主节点</p>
      <p>请先在首页把一个节点设为主节点，再回来开仓。</p>
      <button type="button" class="tr-go" @click="router.push('/home')">返回首页</button>
    </div>

    <template v-else>
      <section class="tr-card tr-quote">
        <div class="tr-quote-top">
          <div>
            <p class="tr-kicker">交易 · {{ currentSymbol || '—' }}</p>
            <p class="tr-price">{{ fmtPx(currentQuote()?.mid, currentSymbol) }}</p>
            <p class="tr-chg">
              <span :class="(currentQuote()?.change ?? 0) >= 0 ? 'pill up' : 'pill down'">
                {{ fmtDailyChangeAbs(currentQuote(), currentSymbol) ?? '—' }}
              </span>
              <span :class="(currentQuote()?.change ?? 0) >= 0 ? 'up' : 'down'">
                {{ fmtDailyChangePct(currentQuote()) ?? '—' }}
              </span>
              <span v-if="trendReady" class="pill" :class="trendClass(trendVerdict)">{{ trendText(trendVerdict) }}</span>
            </p>
            </div>
          <div class="tr-meta">
            <span class="tr-meta-id">MT5 ID {{ primaryNode.mt5_login ?? '—' }}</span>
            <span><i class="tr-dot" :class="isOnline ? 'on' : ''"></i>已连接 {{ hub.onlineCount }} 节点</span>
            <span>更新时间 {{ fmtHm(account?.updated_at) }}</span>
          </div>
        </div>
        <div v-if="availableSymbols.length" class="tr-chips">
            <button
              v-for="sym in availableSymbols"
              :key="sym"
            type="button"
            class="tr-chip"
            :class="{ on: currentSymbol === sym }"
              @click="pickSymbol(sym)"
            >{{ sym }}</button>
          </div>
        <p v-else class="tr-muted">主节点尚未上报任何品种报价</p>
      </section>

      <section class="tr-card tr-create">
        <div class="tr-create-main">
          <p class="tr-create-title">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>
            创建交易
          </p>
          <div class="tr-modes">
            <button type="button" :disabled="!isOnline" @click="openDialog('trend_market')">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/></svg>
              <span>市价开仓</span>
        </button>
            <button type="button" :disabled="!isOnline" @click="openDialog('trend_limit')">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>
              <span>限价开仓</span>
        </button>
            <button type="button" :disabled="!isOnline" @click="openDialog('grid')">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 17h2v-7H3v7zm4 0h2V7H7v10zm4 0h2v-4h-2v4zm4 0h2V4h-2v13zm4 0h2v-7h-2v7z"/></svg>
              <span>智能吸筹</span>
        </button>
      </div>
          <p class="tr-hint">配置完成后，下方将展示对应的订单配置与当前交易、资产、历史交易。</p>
          <p v-if="!isOnline" class="tr-hint">主节点离线，无法触发</p>
          </div>
        <div class="tr-sum">
          <div><span>当前持仓</span><strong>{{ totalPosCount }} 笔</strong></div>
          <div><span>合计手数</span><strong>{{ totalPosVolume.toFixed(2) }} 手</strong></div>
          <div><span>浮动盈亏</span><strong :class="totalPosPnl >= 0 ? 'up' : 'down'">{{ fmtSigned(totalPosPnl) }}</strong></div>
          </div>
      </section>

      <section class="tr-card tr-panel">
        <div class="tr-tabs" role="tablist">
          <button type="button" :class="{ on: detailTab === 'positions' }" @click="detailTab = 'positions'">
            当前交易<span>{{ totalPosCount }}</span>
          </button>
          <button type="button" :class="{ on: detailTab === 'grid' }" @click="detailTab = 'grid'">
            智能吸筹<span>{{ gridTasks.length }}</span>
          </button>
          <button type="button" :class="{ on: detailTab === 'assets' }" @click="detailTab = 'assets'">资产</button>
          <button type="button" :class="{ on: detailTab === 'history' }" @click="detailTab = 'history'">
            历史交易<span>{{ dealsTotal }}</span>
          </button>
        </div>

        <div v-if="detailTab === 'positions'" class="tr-body">
          <p v-if="!positionBooks.length" class="tr-empty-line">暂无持仓</p>
          <section v-for="book in positionBooks" :key="book.symbol" class="tr-book">
            <header class="tr-book-head">
              <div>
                <b>{{ book.symbol }}</b>
                <em v-if="symbolAlias(book.symbol)">{{ symbolAlias(book.symbol) }}</em>
                <span>{{ book.rows.length }} 笔 · {{ book.volume.toFixed(2) }} 手</span>
                <span class="wide">多 {{ book.buy }} / 空 {{ book.sell }}</span>
            </div>
              <strong :class="book.profit >= 0 ? 'up' : 'down'">{{ fmtSigned(book.profit) }}</strong>
            </header>
            <div class="tr-grid tr-pos-grid tr-head">
              <span>#</span><span>方向</span><span>手数</span><span>开仓价</span><span>止盈/止损</span><span>浮动盈亏</span><span>操作</span>
                        </div>
            <div v-for="(p, i) in book.rows" :key="p.ticket" class="tr-grid tr-pos-grid tr-line">
              <span class="dim">{{ i + 1 }}</span>
              <span><i class="dir" :class="isBuySide(p.type) ? 'buy' : 'sell'">{{ fmtPosType(p.type) }}</i></span>
              <span class="num">{{ (p.volume || 0).toFixed(2) }}</span>
              <span class="num">{{ fmtPx(p.price_open, p.symbol) }}</span>
              <span class="sltp"><em class="up">{{ fmtPx(p.tp, p.symbol) }}</em><em class="down">{{ fmtPx(p.sl, p.symbol) }}</em></span>
              <strong :class="(p.profit || 0) >= 0 ? 'up' : 'down'">{{ fmtSigned(p.profit || 0) }}</strong>
              <span>
                <button type="button" class="tr-x" :disabled="isClosing(p.ticket) || !!actionBusy" @click="closePosition(p)">
                  {{ isClosing(p.ticket) ? '…' : '平仓' }}
                      </button>
              </span>
                      </div>
            <footer class="tr-book-actions">
              <button type="button" class="chip green" :disabled="!!actionBusy || !book.rows.some((p) => (p.profit || 0) > 0)" @click="closeSymbolProfit(book.symbol)">平盈</button>
              <button type="button" class="chip amber" :disabled="!!actionBusy || !pendingOf(book.symbol).length" @click="cancelSymbolPending(book.symbol)">删挂单</button>
              <button type="button" class="chip solid" :disabled="!!actionBusy" @click="closeSymbolAll(book.symbol)">一键平仓</button>
            </footer>
          </section>
          <p v-if="positionBooks.length" class="tr-foot-note">
            当前持仓 {{ totalPosCount }} 笔 · 合计手数 {{ totalPosVolume.toFixed(2) }} · 浮动盈亏 {{ fmtSigned(totalPosPnl) }}
          </p>
          </div>

        <div v-else-if="detailTab === 'grid'" class="tr-body">
          <p v-if="gridLoading && !gridTasks.length" class="tr-empty-line">加载中…</p>
          <p v-else-if="!gridBooks.length" class="tr-empty-line">暂无运行中的智能吸筹</p>
          <section v-for="book in gridBooks" :key="book.symbol" class="tr-book">
            <header class="tr-book-head">
              <div>
                <b>{{ book.symbol }}</b>
                <em v-if="symbolAlias(book.symbol)">{{ symbolAlias(book.symbol) }}</em>
                <span>{{ book.positions }} 笔持仓 · {{ book.volumeLabel }}</span>
                </div>
              <strong :class="book.profit >= 0 ? 'up' : 'down'">{{ fmtSigned(book.profit) }}</strong>
            </header>
            <div class="tr-grid tr-grid-grid tr-head">
              <span>方向</span><span>节点</span><span>持仓</span><span>挂单</span><span>盈亏</span><span>操作</span>
              </div>
            <div v-for="t in book.tasks" :key="t.task_id" class="tr-grid tr-grid-grid tr-line">
              <span><i class="dir" :class="gridDirTagClass(t.action)">{{ fmtPosType(t.action) }}</i></span>
              <span class="num dim" :title="gridTaskNodeNames(t)">{{ gridTaskNodeNames(t) }}</span>
              <span class="num">{{ gridTaskPositionCount(t) }}</span>
              <span class="num dim">{{ gridTaskPendingCount(t) || '—' }}</span>
              <strong :class="gridTaskProfit(t) >= 0 ? 'up' : 'down'">{{ fmtSigned(gridTaskProfit(t)) }}</strong>
              <span>
                        <button
                  type="button"
                  class="tr-x"
                          :disabled="isClosingGridTask(t.task_id) || !isGridTaskStoppable(t)"
                          @click="stopGridTask(t)"
                >{{ isClosingGridTask(t.task_id) ? '…' : '停止' }}</button>
                      </span>
                    </div>
          </section>
                    </div>

        <div v-else-if="detailTab === 'assets'" class="tr-body">
          <div class="tr-assets">
            <div><span>账户余额</span><strong>{{ accountBalance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</strong></div>
            <div><span>净值</span><strong>{{ accountEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</strong></div>
            <div><span>占用保证金</span><strong>{{ accountMargin.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</strong></div>
            <div><span>可用保证金</span><strong>{{ accountFreeMargin.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</strong></div>
            <div><span>浮动盈亏</span><strong :class="accountProfit >= 0 ? 'up' : 'down'">{{ fmtSigned(accountProfit) }}</strong></div>
            <div><span>今日盈亏</span><strong :class="todayPnl >= 0 ? 'up' : 'down'">{{ historyDeals.length || historyWinRate != null ? fmtSigned(todayPnl) : '—' }}</strong></div>
                  </div>
          <p class="tr-dist-title">持仓品种分布</p>
          <div v-for="item in assetDist" :key="item.symbol" class="tr-dist">
            <div class="tr-dist-top">
              <span>{{ item.symbol }}</span>
              <span>{{ item.volume > 0 ? `${item.volume.toFixed(2)} 手 · ${item.pct}%` : `空仓 · ${item.pct}%` }}</span>
                  </div>
            <div class="tr-bar"><i :style="{ width: `${item.pct}%` }"></i></div>
                </div>
          <p v-if="!assetDist.length" class="tr-empty-line">暂无品种</p>
          </div>

        <div v-else class="tr-body">
          <div class="tr-hist-pills">
            <span>近 7 日 {{ dealsTotal }} 笔</span>
            <span>胜率 {{ historyWinRate == null ? '—' : `${historyWinRate.toFixed(0)}%` }}</span>
            <span :class="historyProfit >= 0 ? 'up' : 'down'">累计盈亏 {{ fmtSigned(historyProfit) }}</span>
        </div>
          <p v-if="historyLoading && !historyBooks.length" class="tr-empty-line">加载中…</p>
          <p v-else-if="!historyBooks.length" class="tr-empty-line">近 7 日暂无已平仓记录</p>
          <section v-for="book in historyBooks" :key="book.symbol" class="tr-book">
            <header class="tr-book-head">
              <div>
                <b>{{ book.symbol }}</b>
                <em v-if="symbolAlias(book.symbol)">{{ symbolAlias(book.symbol) }}</em>
                <span>{{ book.rows.length }} 笔 · {{ book.volume.toFixed(2) }} 手</span>
                <span class="wide">平盈 {{ book.wins }} / 平损 {{ book.losses }}</span>
      </div>
              <strong :class="book.profit >= 0 ? 'up' : 'down'">{{ fmtSigned(book.profit) }}</strong>
            </header>
            <div class="tr-grid tr-hist-grid tr-head">
              <span>#</span><span>时间</span><span>方向</span><span>手数</span><span>开仓价</span><span>平仓价</span><span>盈亏</span>
            </div>
            <div v-for="(row, i) in book.rows" :key="row.key" class="tr-grid tr-hist-grid tr-line">
              <span class="dim">{{ i + 1 }}</span>
              <span class="dim">{{ fmtPosTime(row.time) }}</span>
              <span><i class="dir" :class="isBuySide(row.direction) ? 'buy' : 'sell'">{{ fmtPosType(row.direction) }}</i></span>
              <span class="num">{{ row.volume.toFixed(2) }}</span>
              <span class="num">{{ fmtPx(row.open, row.symbol) }}</span>
              <span class="num">{{ fmtPx(row.close, row.symbol) }}</span>
              <strong :class="row.profit >= 0 ? 'up' : 'down'">{{ fmtSigned(row.profit) }}</strong>
            </div>
          </section>
        </div>
      </section>
    </template>

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
.tr { display: flex; flex-direction: column; gap: 12px; min-width: 0; color: #E6EBF4; }
.tr-card { border: 1px solid #1C2740; background: #101828; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.35); }
.tr-empty { padding: 36px 16px; text-align: center; color: #8B96A8; }
.tr-empty-title { margin: 0 0 8px; color: #E6EBF4; font-size: 16px; font-weight: 650; }
.tr-go, .tr-modes button, .tr-chip, .tr-x, .chip { cursor: pointer; }
.tr-go { margin-top: 12px; border: 0; border-radius: 8px; background: #2563EB; color: #fff; padding: 8px 14px; }
.tr-quote, .tr-create, .tr-body { padding: 12px; }
.tr-quote-top, .tr-book-head, .tr-book-actions, .tr-meta, .tr-chg, .tr-chips, .tr-hist-pills { display: flex; align-items: center; gap: 8px; }
.tr-quote-top { justify-content: space-between; align-items: flex-end; flex-wrap: wrap; }
.tr-create {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: center;
}
.tr-kicker, .tr-hint, .tr-muted, .tr-foot-note, .tr-empty-line { color: #8B96A8; }
.tr-kicker { margin: 0; font-size: 12px; }
.tr-price { margin: 4px 0 0; font-size: 30px; font-weight: 650; line-height: 1; letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }
.tr-chg { margin: 8px 0 0; font-size: 12px; font-variant-numeric: tabular-nums; }
.pill { border-radius: 4px; padding: 1px 6px; font-weight: 650; }
.pill.down, .down { color: #F6465D; }
.pill.up, .up { color: #22C55E; }
.pill.down { background: rgba(246,70,93,.15); }
.pill.up, .pill.green { background: rgba(34,197,94,.15); color: #22C55E; }
.pill.red { background: rgba(246,70,93,.15); color: #F6465D; }
.pill.blue { background: rgba(37,99,235,.15); color: #93C5FD; }
.tr-meta { flex-wrap: wrap; color: #5B6B85; font-size: 10px; }
.tr-meta span, .tr-hist-pills span { border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 4px 8px; }
.tr-meta-id { color: #3B82F6; border-color: rgba(37,99,235,.4) !important; background: rgba(37,99,235,.1) !important; }
.tr-dot { width: 6px; height: 6px; border-radius: 99px; background: #5B6B85; display: inline-block; margin-right: 4px; }
.tr-dot.on { background: #22C55E; }
.tr-chips { margin-top: 12px; overflow-x: auto; }
.tr-chip { flex: 0 0 auto; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 6px 12px; font-size: 12px; }
.tr-chip.on { border-color: #2563EB; background: #2563EB; color: #fff; }
.tr-create-main { min-width: 0; }
.tr-create-title { margin: 0; display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 650; }
.tr-create-title svg, .tr-modes svg { width: 16px; height: 16px; color: #2563EB; }
.tr-modes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.tr-modes button {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  border: 1px solid #22304A; background: #0C1220; color: #F1F5FB; border-radius: 12px; padding: 14px 8px; font-size: 13.5px; font-weight: 750;
}
.tr-modes button:disabled { opacity: .45; cursor: not-allowed; }
.tr-hint { margin: 10px 0 0; font-size: 10px; line-height: 1.5; color: #4A5568; }
.tr-sum {
  width: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid #22304A;
  background: #0C1220;
  border-radius: 8px;
  padding: 10px;
}
.tr-sum div { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; line-height: 1.25; color: #5B6B85; }
.tr-sum strong { color: #E6EBF4; font-weight: 650; font-variant-numeric: tabular-nums; }
.tr-tabs { display: flex; gap: 2px; border-bottom: 1px solid #1C2740; padding: 0 8px; overflow-x: auto; }
.tr-tabs button { position: relative; border: 0; background: transparent; color: #5B6B85; font-size: 12px; font-weight: 650; padding: 10px 12px; white-space: nowrap; }
.tr-tabs button span { margin-left: 4px; color: #4A5568; font-size: 10px; font-weight: 550; }
.tr-tabs button.on { color: #E6EBF4; }
.tr-tabs button.on::after { content: ''; position: absolute; left: 8px; right: 8px; bottom: -1px; height: 2px; border-radius: 2px; background: #2563EB; }
.tr-book { margin-top: 12px; overflow: hidden; border: 1px solid #1C2740; border-radius: 12px; background: #101828; }
.tr-book-head { justify-content: space-between; gap: 8px; padding: 8px 12px; background: #0C1424; border-bottom: 1px solid #16213A; }
.tr-book-head div { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.tr-book-head b { color: #3B82F6; background: rgba(37,99,235,.15); border-radius: 6px; padding: 1px 6px; font-size: 13px; }
.tr-book-head em, .tr-book-head span { font-style: normal; color: #5B6B85; font-size: 10px; }
.tr-book-head strong, .tr-line strong, .num { font-variant-numeric: tabular-nums; }
.tr-grid { display: grid; align-items: center; gap: 6px; padding: 4px 12px; font-size: 11px; }
.tr-pos-grid { grid-template-columns: 14px 28px 40px minmax(56px,1fr) minmax(58px,1fr) minmax(64px,1fr) 48px; }
.tr-grid-grid { grid-template-columns: 36px minmax(60px,1.2fr) 40px 40px minmax(64px,1fr) 48px; }
.tr-hist-grid { grid-template-columns: 14px 72px 28px 40px minmax(56px,1fr) minmax(56px,1fr) minmax(64px,1fr); }
.tr-head { color: #5B6B85; background: #0A1120; border-bottom: 1px solid #16213A; font-size: 10px; }
.tr-line { border-bottom: 1px dashed #1B2A48; }
.tr-line:last-of-type { border-bottom: 0; }
.tr-head span:nth-child(n+2), .tr-line span:nth-child(n+3), .tr-line strong { text-align: right; }
.tr-pos-grid span:nth-child(2), .tr-hist-grid span:nth-child(3) { text-align: center; }
.dim { color: #8B96A8; }
.dir { display: inline-flex; min-width: 20px; height: 15px; align-items: center; justify-content: center; border-radius: 3px; font-style: normal; font-size: 10px; font-weight: 700; }
.dir.buy, .dir.green { color: #22C55E; background: rgba(34,197,94,.15); }
.dir.sell, .dir.red { color: #F6465D; background: rgba(246,70,93,.15); }
.sltp { text-align: right; line-height: 1.2; }
.sltp em { display: block; font-style: normal; font-size: 11px; }
.tr-x { border: 1px solid rgba(246,70,93,.4); background: rgba(246,70,93,.1); color: #F6465D; border-radius: 6px; padding: 1px 8px; font-size: 10px; }
.tr-x:disabled, .chip:disabled, .tr-modes button:disabled { opacity: .4; cursor: not-allowed; }
.tr-book-actions { justify-content: flex-end; padding: 8px 12px; background: #0A1120; border-top: 1px solid #16213A; }
.chip { border-radius: 8px; padding: 6px 10px; font-size: 11px; font-weight: 650; }
.chip.green { color: #22C55E; border: 1px solid rgba(34,197,94,.4); background: rgba(34,197,94,.1); }
.chip.amber { color: #F59E0B; border: 1px solid rgba(245,158,11,.4); background: rgba(245,158,11,.1); }
.chip.solid { color: #fff; border: 0; background: #F6465D; box-shadow: 0 2px 8px rgba(246,70,93,.35); }
.tr-foot-note, .tr-empty-line { margin: 8px 0 0; font-size: 10px; text-align: center; }
.tr-assets { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 8px; }
.tr-assets div { border: 1px solid #22304A; background: #0C1220; border-radius: 10px; padding: 10px; }
.tr-assets span, .tr-dist-title { color: #5B6B85; font-size: 10px; }
.tr-assets strong { display: block; margin-top: 4px; font-size: 16px; font-variant-numeric: tabular-nums; }
.tr-dist-title { margin: 14px 0 8px; }
.tr-dist { margin-bottom: 8px; }
.tr-dist-top { display: flex; justify-content: space-between; color: #8B96A8; font-size: 11px; }
.tr-bar { height: 4px; margin-top: 4px; border-radius: 99px; background: #16213A; overflow: hidden; }
.tr-bar i { display: block; height: 100%; background: #2563EB; }
.tr-hist-pills { flex-wrap: wrap; margin-bottom: 4px; }
.tr-hist-pills .up, .tr-hist-pills .down { border-color: transparent; }
.wide { display: none; }
@media (min-width: 720px) {
  .wide { display: inline; }
  .tr-assets { grid-template-columns: repeat(3, minmax(0,1fr)); }
  .tr-quote, .tr-body { padding: 16px; }
}
@media (min-width: 1024px) {
  .tr-create { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 24px; padding: 16px; }
}
</style>
