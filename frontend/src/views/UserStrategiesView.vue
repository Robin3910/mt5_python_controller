<script setup lang="ts">
// 交易页：单品种报价 + 顶部持仓汇总
// 底部三个策略按钮（市价/限价/智能吸筹）任选其一 → 弹开仓参数弹窗
//   - 市价 / 限价：SL 必填，方向由后端根据 (入场价|现价) vs SL 自动判断
//   - 限价：再加一个 entry_price 必填
//   - 网格：方向必选（all / buy / sell）
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import OrderDialog, { type OrderForm } from '../components/OrderDialog.vue'
import StrategyFormModal from '@/components/StrategyFormModal.vue'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import { useAuthStore } from '@/stores/auth'
import { quickTrigger, type TradingStrategyType, type QuickTriggerResult } from '@/api/trading'
import type {
  AccountSnapshot,
  GroupOut,
  GroupSignalTaskRecord,
  NodeDispatchRecord,
  NodeOut,
  PaginatedNodeDispatches,
  QuoteInfo,
  StrategyOut,
} from '@/api/types'

/** AI 智能加仓策略模版 ID（与后端 strategy_templates.TEMPLATE_1_ID 对齐） */
const TEMPLATE_1_ID = 'tpl_1'

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

// fmtPips / fmtPosType / fmtPosTime 暂未在模板里使用，等接上 UI 再恢复。

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
const GRID_TAB_REFRESH_MS = 5000
let gridTimer: number | null = null

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
// gridTaskPositionCount / gridTaskPendingCount / gridTaskProfit / gridTaskNodeNames
// / gridDirTagClass / stopGridTask 当前未在模板里调用，等接上 UI 再恢复。

// ── 历史订单 ──
// 走 GET /api/nodes/{id}/dispatches?page=&page_size=，分页返回 signal_history + signal_dispatch 联表明细。
// 每条 = 一次「信号 × 本节点执行」结果，包含 order_ticket / price / retcode / status / error 等成交流水。
const HISTORY_PAGE_SIZE = 50
const historyItems = ref<NodeDispatchRecord[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyLoading = ref(false)
const historyNodeId = ref<string | null>(null) // 上次拉取用的节点 id，避免重复拉

async function fetchHistory(): Promise<void> {
  const nid = primaryNode.value?.node_id
  if (!nid) {
    historyItems.value = []
    historyTotal.value = 0
    return
  }
  historyLoading.value = true
  historyNodeId.value = nid
  try {
    const res: PaginatedNodeDispatches = await hub.fetchNodeDispatches(
      nid,
      historyPage.value,
      HISTORY_PAGE_SIZE,
    )
    historyItems.value = res.items || []
    historyTotal.value = res.total || 0
  } catch (e) {
    historyItems.value = []
    historyTotal.value = 0
    // 不弹错：tab 里自有空态
    console.warn('fetchHistory failed', e)
  } finally {
    historyLoading.value = false
  }
}

// historyStatusClass / fmtHistoryTime / goHistoryPage / historyTotalPages 暂未在模板里使用，
// 等接上历史 tab UI 再恢复。

/** 切到 history tab 时拉一次（首次） */
watch(
  [detailTab, () => primaryNode.value?.node_id],
  ([tab, nid]) => {
    if (tab === 'history' && nid) {
      // 节点变了 → 强制回第一页 + 重拉
      if (historyNodeId.value !== nid) {
        historyPage.value = 1
        fetchHistory()
      } else if (historyItems.value.length === 0 && !historyLoading.value) {
        fetchHistory()
      }
    }
  },
  { immediate: true },
)

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

/** 底部 Tab 切换：开仓策略 / AI 交易 */
const activeTab = ref<'strategy' | 'ai'>('strategy')
/** 是否有 AI 智能加仓策略权限 */
const canUseAi = computed(() => auth.canUseAiStrategy)

const dialogVisible = ref(false)
const submitting = ref(false)
const lastError = ref('')
const defaultVolume = ref(0.01)

// 市价开仓走 strategy 模型需要真实 group_id；
// 按 dialogGroupName（与后端命名规则一致）在 hub.groups 里查找。
// 找不到（用户从未创建过该分组）→ OrderDialog 会拒绝触发 strategy 信号。
const dialogGroupId = ref<string | null>(null)

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

// ───────────────────────── AI 智能加仓策略（tpl_1）─────────────────────────
/** AI 策略配置弹窗可见性 */
const aiFormVisible = ref(false)
/** 编辑中的策略 ID；空 = 新建 */
const aiFormId = ref('')
/** 已配置的 AI 智能加仓策略（tpl_1） */
const aiStrategies = ref<StrategyOut[]>([])
/** 保存后自动建分组中（避免重复点击） */
const aiGroupCreating = ref(false)

/** AI 策略分组名：AI-Augmentation-{用户名}-{MT5 登录号}-{品种}，与开仓分组命名保持同一套规则 */
const aiGroupName = computed(() => aiGroupNameFor(currentSymbol.value))

/** 按品种拼出 AI 分组名 */
function aiGroupNameFor(symbol: string): string {
  if (!primaryNode.value) return ''
  const login = primaryNode.value.mt5_login ?? 'unknown'
  const username = auth.me?.username || 'unknown'
  return `AI-Augmentation-${username}-${login}-${symbol}`
}

/** 打开 AI 策略配置弹窗（新建） */
function openAiForm(): void {
  aiFormId.value = ''
  aiFormVisible.value = true
}

/** 打开 AI 策略配置弹窗（编辑已有策略） */
function editAiForm(strategyId: string): void {
  aiFormId.value = strategyId
  aiFormVisible.value = true
}

/** 拉取当前用户可见的 AI 智能加仓策略（tpl_1），按品种归组展示 */
async function fetchAiStrategies(): Promise<void> {
  try {
    await hub.fetchStrategies()
    aiStrategies.value = (hub.strategies || []).filter((s) => s.template_id === TEMPLATE_1_ID)
  } catch {
    aiStrategies.value = []
  }
}

/** 某个品种下已配置的 AI 策略（按创建时间倒序，最新的在前） */
function aiStrategiesOfSymbol(symbol: string): StrategyOut[] {
  return aiStrategies.value
    .filter((s) => s.symbol === symbol)
    .slice()
    .sort((a, b) => b.created_at - a.created_at)
}

/** 当前品种下已配置的 AI 策略 */
const symbolAiStrategies = computed(() => aiStrategiesOfSymbol(currentSymbol.value))

/** 策略摘要：启用规则数 / 总规则数 */
function aiStrategyRuleCount(s: StrategyOut): string {
  const rules = s.rules || []
  const enabled = rules.filter((r) => r.status === 1).length
  return `${enabled} / ${rules.length}`
}

/** 切到 AI Tab 时拉取已配置的策略列表（编辑/新建共用同一份数据源） */
watch(activeTab, (tab) => {
  if (tab === 'ai') void fetchAiStrategies()
})

/**
 * AI 策略保存成功后：按 (AI-Augmentation-用户名-MT5登录号-品种) 幂等创建分组，并把主节点加入、绑定该策略。
 * 已有同名分组则复用（补绑策略 + 补齐主节点）。
 */
async function onAiStrategySaved(
  info: { strategyId: string; name: string; symbol: string; templateId: string } | null | undefined,
): Promise<void> {
  if (!info) return
  if (aiGroupCreating.value) return
  aiGroupCreating.value = true
  await fetchAiStrategies()
  try {
    const nodeId = primaryNode.value?.node_id ?? ''
    // 用策略自身的 symbol 拼分组名：编辑态下 currentSymbol 可能与策略品种不一致
    const groupName = aiGroupNameFor(info.symbol)
    if (!nodeId || !groupName) {
      ElMessage.warning('主节点未就绪，策略已保存但未能自动创建分组')
      return
    }
    const groups = await hub.listGroups()
    let group = groups.find((g) => g.name === groupName) ?? null
    if (!group) {
      group = await hub.createGroup({
        name: groupName,
        enabled: true,
        node_ids: [nodeId],
      })
    }
    // 分组已存在但可能没绑策略 / 没含主节点，补齐（失败不阻断保存）
    if (group.strategy_id !== info.strategyId) {
      await hub.updateGroup(group.group_id, { strategy_id: info.strategyId })
    }
    const memberIds = (group.nodes ?? []).map((n) => n.node_id)
    if (!memberIds.includes(nodeId)) {
      await hub.updateGroup(group.group_id, { node_ids: [...memberIds, nodeId] })
    }
    ElMessage.success(`AI 策略「${info.name}」已保存，并已创建分组「${groupName}」`)
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    ElMessage.warning(
      `策略已保存，但自动创建分组失败：${err?.response?.data?.detail || err?.message || '未知错误'}`,
    )
  } finally {
    aiGroupCreating.value = false
  }
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

// 单笔持仓平仓：当前模板未引用，等接上 UI 再恢复 closingTickets / isClosing / closePosition。

// 简单轮询拉取快照（保持行情新鲜）
let pollTimer: number | null = null
onMounted(async () => {
  await hub.fetchNodes()
  if (primaryNode.value && !hub.accounts[primaryNode.value.node_id]) {
    try {
      await hub.fetchNodeAccount(primaryNode.value.node_id)
    } catch {
      /* 节点尚未上报快照，忽略 */
    }
  }
  pollTimer = window.setInterval(() => {
    if (primaryNode.value) {
      hub.fetchNodeAccount(primaryNode.value.node_id).catch(() => {})
    }
  }, 5000)
})
onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
  if (gridTimer !== null) {
    window.clearInterval(gridTimer)
    gridTimer = null
  }
})
</script>

<template>
  <div class="trading-page">
    <!-- 空态：无主节点 -->
    <div v-if="!primaryNode" class="empty-state card card-pad">
      <div class="empty-title">尚未设置主节点</div>
      <div class="empty-desc">请先在【节点】页将一个节点标记为主节点，再回到此页面触发开仓。</div>
      <button class="btn-primary" @click="router.push('/nodes')">前往节点页</button>
    </div>

    <template v-else>
      
      <!-- 单品种报价 -->
      <div class="quote-board card card-pad">
        <div class="quote-col">
          <div class="quote-head">
            <span class="quote-sym">{{ currentSymbol }}</span>
            <!-- <span class="quote-pct" v-if="currentQuote()">100.00%</span> -->
          </div>
          <div class="quote-mid">{{ fmtPrice(currentQuote()?.mid, currentSymbol) }}</div>
          <!-- <div class="quote-bidask">
            <div class="ba-row bid">
              <span class="ba-k">买价</span>
              <span class="ba-v">{{ fmtPrice(currentQuote()?.bid, currentSymbol) }}</span>
            </div>
            <div class="ba-row ask">
              <span class="ba-k">卖价</span>
              <span class="ba-v">{{ fmtPrice(currentQuote()?.ask, currentSymbol) }}</span>
            </div>
            <div class="ba-row chg">
              <span class="ba-k">变动</span>
              <span
                class="ba-v"
                :class="(currentQuote()?.change ?? 0) >= 0 ? 'pos' : 'neg'"
              >{{ fmtPips(currentQuote()?.change) }}</span>
            </div>
          </div> -->
          <!-- <div class="quote-position" v-if="symbolPosCount > 0">
            <div class="qp-row">
              <span class="qp-k">{{ currentSymbol }} 持仓</span>
              <span class="qp-v">{{ symbolPosCount }} 笔 · {{ symbolPosVolume.toFixed(2) }} 手</span>
            </div>
            <div class="qp-row">
              <span class="qp-k">浮盈</span>
              <span class="qp-v" :class="symbolPosPnl >= 0 ? 'pos' : 'neg'">
                {{ symbolPosPnl >= 0 ? '+' : '' }}{{ symbolPosPnl.toFixed(2) }}
              </span>
            </div>
          </div> -->
          <div class="quote-sym-picker" v-if="availableSymbols.length > 0">
            <button
              v-for="sym in availableSymbols"
              :key="sym"
              class="sym-chip"
              :class="{ active: currentSymbol === sym }"
              @click="pickSymbol(sym)"
            >{{ sym }}</button>
          </div>
          <div v-else class="sym-empty muted">主节点尚未上报任何品种报价</div>
        </div>
      </div>

      <!-- 底部 Tab 切换：开仓策略 / AI 交易 -->
      <div class="strategy-tabs">
        <button
          class="strategy-tab"
          :class="{ active: activeTab === 'strategy' }"
          @click="activeTab = 'strategy'"
        >开仓策略</button>
        <button
          v-if="canUseAi"
          class="strategy-tab"
          :class="{ active: activeTab === 'ai' }"
          @click="activeTab = 'ai'"
        >AI 交易</button>
      </div>

      <!-- Tab 内容 -->
      <template v-if="activeTab === 'strategy'">
        <!-- 三个策略按钮 -->
        <div class="action-bar">
          <button
            class="action-btn"
            :class="{ 'action-btn-disabled': !isOnline }"
            :disabled="!isOnline"
            @click="openDialog('trend_market')"
          >
            <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
            </svg>
            <span class="action-label">市价开仓</span>
          </button>
          <div class="action-sep"></div>
          <button
            class="action-btn"
            :class="{ 'action-btn-disabled': !isOnline }"
            :disabled="!isOnline"
            @click="openDialog('trend_limit')"
          >
            <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <circle cx="12" cy="12" r="5" />
              <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
              <path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3" />
            </svg>
            <span class="action-label">限价开仓</span>
          </button>
          <div class="action-sep"></div>
          <button
            class="action-btn"
            :class="{ 'action-btn-disabled': !isOnline }"
            :disabled="!isOnline"
            @click="openDialog('grid')"
          >
            <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M3 6h18M3 12h18M3 18h18" />
              <path d="M6 4v16M12 4v16M18 4v16" />
              <path d="M4 8l4 4M8 8l-4 4M16 8l4 4M20 8l-4 4M4 16l4-2M8 16l-4-2M16 14l4 4M20 14l-4 4" />
            </svg>
            <span class="action-label">智能吸筹</span>
          </button>
        </div>
        <div v-if="!isOnline" class="offline-tip">主节点离线，无法触发</div>
      </template>

      <!-- AI 交易 Tab（仅有权限者可见） -->
      <template v-else-if="activeTab === 'ai'">
        <div class="ai-panel card card-pad">
          <div class="ai-panel-head">
            <div class="ai-panel-title">AI 智能加仓策略</div>
            <!-- <div class="ai-panel-sub">
              选择品种并配置加仓参数，保存后自动创建分组「{{ aiGroupName }}」并把你的主节点加入
            </div> -->
          </div>

          <!-- <div class="ai-panel-sym">
            <span class="ai-panel-sym-label">目标品种</span>
            <div class="ai-panel-sym-chips">
              <button
                v-for="sym in availableSymbols"
                :key="sym"
                class="sym-chip"
                :class="{ active: currentSymbol === sym }"
                @click="pickSymbol(sym)"
              >{{ sym }}</button>
            </div>
            <span v-if="!availableSymbols.length" class="muted" style="font-size: 12px">
              主节点尚未上报任何品种报价
            </span>
          </div> -->

          <!-- 该品种下已配置的 AI 策略：可点「编辑」改参数 -->
          <div v-if="symbolAiStrategies.length" class="ai-strategy-list">
            <div class="ai-list-head">
              <span>{{ currentSymbol }} 已配置策略</span>
              <span class="muted">{{ symbolAiStrategies.length }} 个</span>
            </div>
            <div v-for="s in symbolAiStrategies" :key="s.strategy_id" class="ai-strategy-item">
              <div class="ai-item-main">
                <div class="ai-item-name">{{ s.name }}</div>
                <div class="ai-item-meta">
                  <span class="ai-item-tag">{{ s.template_name }}</span>
                  <span class="ai-item-tag">规则 {{ aiStrategyRuleCount(s) }}</span>
                  <span class="ai-item-tag" :class="{ off: !s.enabled }">
                    {{ s.enabled ? '已启用' : '已停用' }}
                  </span>
                </div>
              </div>
              <button class="ai-item-edit" :disabled="aiGroupCreating" @click="editAiForm(s.strategy_id)">
                编辑
              </button>
            </div>
          </div>
          <div v-else class="ai-list-empty">
            {{ currentSymbol }} 尚未配置 AI 策略，点击下方按钮开始配置
          </div>

          <button
            class="btn-primary ai-panel-btn"
            :disabled="aiGroupCreating || !availableSymbols.length"
            @click="openAiForm"
          >
            {{ aiGroupCreating ? '处理中…' : symbolAiStrategies.length ? '新增 AI 策略' : '配置 AI 策略' }}
          </button>
        </div>
      </template>

    </template>

    <!-- 触发弹窗：开仓参数（新 UI） -->
    <OrderDialog
      v-model="dialogVisible"
      v-model:strategy="strategyType"
      :form="form"
      :group-name="dialogGroupName"
      :group-id="dialogGroupId"
      :submitting="submitting"
      :last-error="lastError"
      :can-submit="canSubmit"
      :current-mid="currentMid"
      mode="save"
      @update:form="(v) => Object.assign(form, v)"
      @submit="submit"
      @cancel="closeDialog"
    />

    <!-- AI 智能加仓策略配置弹窗：保存后自动建分组 + 绑定主节点 -->
    <StrategyFormModal
      v-model="aiFormVisible"
      :mode="aiFormId ? 'edit' : 'create'"
      :strategy-id="aiFormId"
      preset-template-id="tpl_1"
      :preset-symbol="currentSymbol"
      :preset-symbol-locked="true"
      @saved="onAiStrategySaved"
    />
  </div>
</template>

<style scoped>
/* ── Page frame ── */
.trading-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  /* 移动端风格：底部留出触达区 */
  padding-bottom: 8px;
}

/* ── Empty state ── */
.empty-state { text-align: center; padding: 40px 20px; }
.empty-title { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
.empty-desc { color: var(--muted); margin-bottom: 16px; }

/* ── Status bar ── */
.status-bar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
}
.status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.status-left, .status-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.status-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  flex-wrap: wrap;
}
.ss-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1 1 auto;
}
.ss-k {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.02em;
}
.ss-v {
  font-size: 13px;
  font-weight: 700;
  font-family: var(--mono);
  color: var(--text);
}
.ss-v.pos { color: var(--green); }
.ss-v.neg { color: var(--red); }
.ss-sep {
  width: 1px;
  height: 18px;
  background: var(--glass-border);
  flex-shrink: 0;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  color: var(--muted);
}
.status-pill.on { color: var(--green); border-color: rgba(16, 185, 129, 0.28); background: rgba(16, 185, 129, 0.08); }
.status-pill.off { color: #9ca3af; }
.status-node {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.status-muted { font-size: 12px; color: var(--muted); font-family: var(--mono); }
.status-sep { color: rgba(255, 255, 255, 0.18); }

/* ── Quote board (双柱) ── */
.quote-board {
  display: flex;
  align-items: stretch;
  padding: 0;
  overflow: hidden;
}
.quote-col {
  flex: 1 1 0;
  min-width: 0;
  padding: 18px 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.quote-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.quote-sym {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.quote-pct {
  font-size: 11px;
  color: var(--muted);
  font-family: var(--mono);
}
.quote-mid {
  font-size: 32px;
  font-weight: 700;
  line-height: 1.1;
  font-family: var(--mono);
  letter-spacing: -0.01em;
  color: var(--text);
  margin: 2px 0 6px;
}
.quote-bidask {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
}
.ba-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  font-family: var(--mono);
}
.ba-k { color: var(--muted); }
.ba-v { color: var(--text); font-weight: 600; }
.ba-v.pos { color: var(--green); }
.ba-v.neg { color: var(--red); }

.quote-position {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  background: rgba(37, 99, 235, 0.06);
  border: 1px solid rgba(37, 99, 235, 0.18);
  border-radius: var(--radius-sm);
}
.qp-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  font-family: var(--mono);
}
.qp-k { color: var(--muted); }
.qp-v { color: var(--text); font-weight: 600; }
.qp-v.pos { color: var(--green); }
.qp-v.neg { color: var(--red); }

.quote-sym-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.sym-chip {
  padding: 4px 8px;
  font-size: 11px;
  font-weight: 600;
  border-radius: 6px;
  background: var(--glass);
  color: var(--muted);
  border: 1px solid var(--glass-border);
  font-family: var(--mono);
}
.sym-chip:hover { color: var(--text); border-color: var(--glass-highlight); }
.sym-chip.active {
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
  color: #ffffff;
  border-color: transparent;
  box-shadow: 0 0 12px rgba(37, 99, 235, 0.32);
}
.sym-empty {
  margin-top: 4px;
  font-size: 12px;
  padding: 6px 0 2px;
}

/* ── Action bar (3 个策略按钮) ── */
/* ── 底部 Tab 切换 ── */
.strategy-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  margin-top: 8px;
  background: var(--panel);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  backdrop-filter: blur(var(--blur)) saturate(1.4);
  -webkit-backdrop-filter: blur(var(--blur)) saturate(1.4);
}
.strategy-tab {
  flex: 1 1 0;
  appearance: none;
  border: none;
  background: transparent;
  border-radius: var(--radius-sm);
  padding: 10px 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  white-space: nowrap;
}
.strategy-tab:hover { color: var(--text); }
.strategy-tab.active {
  background: rgba(37, 99, 235, 0.18);
  color: var(--primary);
}

/* ── AI 交易面板 ── */
.ai-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.ai-panel-head { display: flex; flex-direction: column; gap: 4px; }
.ai-panel-title { font-size: 15px; font-weight: 700; color: var(--text); }
.ai-panel-sub { font-size: 12px; color: var(--muted); line-height: 1.6; }
.ai-panel-sym { display: flex; flex-direction: column; gap: 8px; }
.ai-panel-sym-label { font-size: 12px; color: var(--muted); }
.ai-panel-sym-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ai-panel-btn { width: 100%; padding: 12px; font-size: 14px; font-weight: 600; }

/* ── 已配置 AI 策略列表 ── */
.ai-strategy-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ai-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--muted);
}
.ai-strategy-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: var(--panel-2, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border);
  border-radius: 10px;
}
.ai-item-main { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.ai-item-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ai-item-meta { display: flex; flex-wrap: wrap; gap: 6px; }
.ai-item-tag {
  padding: 2px 7px;
  font-size: 11px;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  border-radius: 5px;
  line-height: 1.4;
}
.ai-item-tag.off { color: var(--muted); opacity: 0.75; }
.ai-item-edit {
  flex-shrink: 0;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent, #60a5fa);
  background: rgba(96, 165, 250, 0.1);
  border: 1px solid rgba(96, 165, 250, 0.28);
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, transform 0.1s;
}
.ai-item-edit:hover { background: rgba(96, 165, 250, 0.2); border-color: rgba(96, 165, 250, 0.5); }
.ai-item-edit:active { transform: translateY(1px); }
.ai-item-edit:disabled { cursor: not-allowed; opacity: 0.6; }
.ai-list-empty {
  padding: 14px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--border);
  border-radius: 10px;
}

.action-bar {
  position: sticky;
  bottom: 12px;
  display: flex;
  align-items: stretch;
  gap: 8px;
  padding: 10px 12px;
  margin-top: 8px;
  background: var(--panel);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow), 0 0 24px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(var(--blur)) saturate(1.4);
  -webkit-backdrop-filter: blur(var(--blur)) saturate(1.4);
}
.action-btn {
  flex: 1 1 0;
  height: 64px;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #ffffff;
  border: none;
  cursor: pointer;
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
  box-shadow: 0 6px 20px rgba(37, 99, 235, 0.28);
  transition: transform 0.1s, box-shadow var(--transition), filter var(--transition);
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.action-icon {
  width: 22px;
  height: 22px;
  opacity: 0.85;
  transition: transform var(--transition), opacity var(--transition);
}
.action-label {
  line-height: 1.1;
  font-size: 13px;
}
.action-btn:hover { filter: brightness(1.08); }
.action-btn:hover .action-icon { opacity: 1; transform: translateY(-1px); }
.action-btn:active { transform: translateY(1px); }
.action-btn:disabled, .action-btn.action-btn-disabled { opacity: 0.45; cursor: not-allowed; box-shadow: none; }
.action-btn:disabled .action-icon, .action-btn.action-btn-disabled .action-icon { opacity: 0.5; }
.action-sep { width: 1px; background: var(--glass-border); flex-shrink: 0; }

.offline-tip {
  font-size: 12px;
  color: #fbbf24;
  text-align: center;
  padding: 4px 0 0;
}

/* ── Dialog form 样式已迁出到 OrderDialog.vue ── */

/* ── 持仓 / 资产详情（Tab 切换） ── */
.detail-panel {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: 0;
  overflow: hidden;
}

/* Tab 切换条 */
.detail-tabs {
  display: flex;
  gap: 4px;
  padding: 10px 14px 0;
  border-bottom: 1px solid var(--glass-border);
  flex-shrink: 0;
}
.detail-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 8px 8px 0 0;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  letter-spacing: 0.01em;
  position: relative;
  top: 1px;
}
.detail-tab:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.detail-tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  background: rgba(37, 99, 235, 0.06);
}
.tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  font-size: 11px;
  font-weight: 700;
  background: var(--primary);
  color: #ffffff;
  border-radius: 999px;
  line-height: 1;
}
.tab-badge-muted {
  background: var(--glass);
  color: var(--muted);
  border: 1px solid var(--glass-border);
}

/* Tab 内容区 */
.detail-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding-bottom: 2px;
}

/* 空态 */
.detail-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px 20px;
  color: var(--muted);
  font-size: 13px;
}
.detail-empty svg { opacity: 0.35; }

.dot-spin-lg {
  width: 22px;
  height: 22px;
  border: 2px solid var(--glass-border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: pos-spin 0.8s linear infinite;
}

/* ── 主节点持仓列表（桌面表格） ── */
.pos-table-wrap { overflow-x: auto; }
.pos-cards { display: none; }

.pos-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12px;
  min-width: 720px;
}
.pos-table thead th {
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.04em;
  padding: 8px 10px;
  background: var(--glass);
  border-bottom: 1px solid var(--glass-border);
  position: sticky;
  top: 0;
  z-index: 1;
}
.pos-table thead th.num { text-align: right; }
.pos-table thead th.pnl { text-align: right; }
.pos-table tbody td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--glass-border);
  color: var(--text);
  font-size: 12px;
  vertical-align: middle;
}
.pos-table tbody tr:last-child td { border-bottom: none; }
.pos-table tbody tr:hover { background: rgba(255, 255, 255, 0.02); }
.pos-table td.num { text-align: right; }
.pos-table td.pnl { text-align: right; font-weight: 700; }
.pos-table td.mono { font-family: var(--mono); }
.pos-table td.strong { font-weight: 700; }
.pos-table td.muted { color: var(--muted); }
.pos-table td.pnl.pos { color: var(--green); }
.pos-table td.pnl.neg { color: var(--red); }

/* 品种单元格：品种名 + 平仓按钮并排 */
.sym-cell {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.pos-close-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 600;
  color: #fca5a5;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.28);
  border-radius: 6px;
  cursor: pointer;
  line-height: 1;
  transition: background 0.15s, color 0.15s, border-color 0.15s, transform 0.1s;
}
.pos-close-btn:hover {
  background: rgba(239, 68, 68, 0.18);
  color: #fecaca;
  border-color: rgba(239, 68, 68, 0.5);
}
.pos-close-btn:active { transform: translateY(1px); }
.pos-close-btn:disabled,
.pos-close-btn.busy {
  cursor: not-allowed;
  opacity: 0.6;
  background: rgba(239, 68, 68, 0.05);
}
.pos-close-btn.busy .dot-spin {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: pos-spin 0.8s linear infinite;
}
@keyframes pos-spin {
  to { transform: rotate(360deg); }
}

/* 方向 tag */
.dir-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  border: 1px solid transparent;
}
.dir-tag.buy { color: #fca5a5; background: rgba(239, 68, 68, 0.10); border-color: rgba(239, 68, 68, 0.28); }
.dir-tag.sell { color: #6ee7b7; background: rgba(16, 185, 129, 0.10); border-color: rgba(16, 185, 129, 0.28); }

/* ── 历史订单 ── */
/* 状态色块（不区分成功颜色，保持语义化） */
.hist-status {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  background: var(--glass);
  color: var(--muted);
  border: 1px solid var(--glass-border);
  font-family: var(--mono);
}
.hist-status.ok {
  color: #6ee7b7;
  background: rgba(16, 185, 129, 0.10);
  border-color: rgba(16, 185, 129, 0.28);
}
.hist-status.pending {
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.08);
  border-color: rgba(251, 191, 36, 0.28);
}
.hist-status.err {
  color: #fca5a5;
  background: rgba(239, 68, 68, 0.10);
  border-color: rgba(239, 68, 68, 0.28);
}

/* 历史表比当前持仓表多一列错误，加宽一点 */
.hist-table { min-width: 860px; }
.hist-err {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
}
.hist-err-kv { grid-column: 1 / -1; }
.err-text { color: var(--red); }

/* 翻页器 */
.hist-pager {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  font-size: 12px;
  border-top: 1px solid var(--glass-border);
  background: var(--glass);
}
.pager-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  height: 26px;
  padding: 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  background: transparent;
  border: 1px solid var(--glass-border);
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.pager-btn:hover:not(:disabled) {
  background: rgba(37, 99, 235, 0.10);
  border-color: rgba(37, 99, 235, 0.4);
  color: var(--primary);
}
.pager-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.pager-info {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--text);
  padding: 0 4px;
  min-width: 50px;
  text-align: center;
}
.pager-total {
  margin-left: auto;
  font-size: 11px;
  font-family: var(--mono);
}

/* 手机持仓卡片 */
.pos-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 14px;
  background: var(--glass);
  border-bottom: 1px solid var(--glass-border);
}
.pos-card + .pos-card { margin-top: 0; }
.pos-card-row1 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.pos-card-left { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.pos-card-right { display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0; }
.pos-card-pnl { font-size: 14px; font-weight: 700; font-family: var(--mono); }
.pos-card-pnl.pos { color: var(--green); }
.pos-card-pnl.neg { color: var(--red); }
.pos-card-row2 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px 8px;
}
.pos-card-row2 .kv { display: flex; justify-content: space-between; gap: 6px; min-width: 0; font-size: 12px; }
.pos-card-row2 .k { color: var(--muted); }
.pos-card-row2 .v { color: var(--text); font-weight: 600; }
.pos-card-row2 .v.muted { color: var(--muted); }
.pos-card-row3 { font-size: 11px; font-family: var(--mono); }

/* ── 资产 ── */
.asset-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1px;
  background: var(--glass-border);
}
.asset-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
  background: var(--glass);
}
.asset-item.asset-highlight {
  background: rgba(37, 99, 235, 0.04);
}
.asset-label {
  font-size: 12px;
  color: var(--muted);
  font-weight: 500;
}
.asset-value {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.01em;
}
.asset-value.pos { color: var(--green); }
.asset-value.neg { color: var(--red); }
.asset-value.muted { color: var(--muted); font-size: 15px; font-weight: 600; }

.mono { font-family: var(--mono); }
.strong { font-weight: 700; }

/* ── Responsive ── */
@media (max-width: 640px) {
  .quote-mid { font-size: 26px; }
  .quote-col { padding: 14px 10px 12px; }
  .action-btn { height: 56px; }
  .action-icon { width: 18px; height: 18px; }
  .action-label { font-size: 12px; }
  /* 手机端：隐藏表格，展示卡片 */
  .pos-table-wrap { display: none; }
  .pos-cards { display: block; }
  /* 资产 3 列 → 2 列 */
  .asset-grid { grid-template-columns: repeat(2, 1fr); }
  /* 历史订单翻页器更紧凑 */
  .hist-pager { flex-wrap: wrap; }
  .pager-total { width: 100%; margin-left: 0; text-align: right; }
}
@media (max-width: 420px) {
  .quote-mid { font-size: 24px; }
  .action-bar { padding: 8px; gap: 6px; }
  .action-btn { height: 52px; }
  .pos-card-row2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  /* 资产单列 */
  .asset-grid { grid-template-columns: 1fr; }
}
@media (max-width: 360px) {
  /* 资产在更窄屏时高亮项放大字号 */
  .asset-value { font-size: 20px; }
}

/* ── 吸筹策略 tab 表格 ── */
.grid-task-table { min-width: 860px; }
.grid-signal-cell {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
}
.grid-refresh-hint {
  font-size: 11px;
  padding: 4px 14px 0;
  text-align: right;
}
</style>
