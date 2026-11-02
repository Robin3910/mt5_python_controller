<script setup lang="ts">
// 首页：左侧趋势/K 线 + 右侧按归属分组的节点总览。平仓与删挂单仍走原确认流程。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import HubTrendEmbed from '@/components/HubTrendEmbed.vue'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, CancelPendingResult, NodeOut, PendingOrder, Position } from '@/api/types'
import { confirmAction } from '@/utils/confirm'
import { setNodePrimary } from '@/utils/nodePrimary'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const hub = useHubStore()
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const trendConfigOpen = computed(() => route.query.config === 'trend')

onMounted(async () => {
  await hub.fetchNodes()
})

/**
 * 首页可见节点列表。
 *
 * 口径（与首页以外的页面**无关**：本派生仅在 HomeView 内使用）：
 * - 管理员登录：只展示「owner_user_id 等于自己 user_id」的节点；
 *   「owner_user_id 为空」（在管理员名下 / 仍未分配给任何具体用户）的节点
 *   在首页**不**展示——这是新口径与上一版的差异；这一类节点仍然在
 *   「节点」页 / 「总览」页可见，留给日常运维窗口。
 * - 非管理员登录：保持原全集行为，不影响他们的体验。
 *
 * 选择这条口径的原因：项目里"录入者"和"当前所有者"是两个概念；
 * 管理员把节点创建出来后常会再 transfer 给具体用户，此时该节点的 owner
 * 已经是受让人。用严格 owner==self 把"已分配出去但仍归管理员独占"的边界
 * 场景过滤掉，让首页真正聚焦"我现在自己独占拥有"的节点集合。
 */
const homeVisibleNodes = computed<NodeOut[]>(() => {
  if (!auth.isAdmin) return hub.nodes
  const myId = auth.me?.user_id ?? null
  if (myId == null) return []
  return hub.nodes.filter((n) => (n.owner_user_id ?? null) === myId)
})

// 「卡片操作菜单」目前展开的节点 ID。null = 全部折叠；一次只能打开一个菜单，
// 再次点击同一节点则关闭；点击其它节点切换展开目标；点击页面空白处收起。
const openMenuNodeId = ref<string | null>(null)
function toggleNodeMenu(n: NodeOut): void {
  sortOpen.value = false
  openMenuNodeId.value = openMenuNodeId.value === n.node_id ? null : n.node_id
}
function closeNodeMenu(): void {
  openMenuNodeId.value = null
}
const sortOpen = ref(false)
function onDocClick(ev: MouseEvent): void {
  const target = ev.target as HTMLElement | null
  if (openMenuNodeId.value && !target?.closest('.home-node-menu')) closeNodeMenu()
  if (sortOpen.value && !target?.closest('.home-sort')) sortOpen.value = false
}
function onDocKey(ev: KeyboardEvent): void {
  if (ev.key === 'Escape') {
    closeNodeMenu()
    sortOpen.value = false
  }
}
function toggleSort(): void {
  closeNodeMenu()
  sortOpen.value = !sortOpen.value
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onDocKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onDocKey)
})

/** 拉取某节点当前 in-flight 子任务精简列表；空 = 放行。 */
async function fetchActiveTasks(nodeId: string): Promise<{
  count: number
  items: Array<{
    dispatch_id: number
    task_id: number
    signal_id: string
    group_id: string
    symbol: string | null
    magic: number | null
    status: string
  }>
  node_name: string
}> {
  const { data } = await api.get(`/api/nodes/${nodeId}/active-tasks`)
  return data
}

/** 卡片菜单与节点主页共用同一套主节点切换。客户不能取消主节点。 */
async function togglePrimary(n: NodeOut): Promise<void> {
  closeNodeMenu()
  await setNodePrimary({
    node: n,
    nodes: hub.nodes,
    isAdmin: auth.isAdmin,
    apply: (id, next) => hub.setNodePrimary(id, next),
  })
}

type StatusFilter = 'all' | 'holding' | 'profit' | 'loss' | 'online' | 'offline'
type SortBy = 'equity_desc' | 'equity_asc' | 'name'
const SORT_OPTIONS: { value: SortBy; label: string }[] = [
  { value: 'equity_desc', label: '按净值降序' },
  { value: 'equity_asc', label: '按净值升序' },
  { value: 'name', label: '按名称' },
]
const FILTERS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'holding', label: '持仓' },
  { value: 'profit', label: '浮盈' },
  { value: 'loss', label: '浮亏' },
  { value: 'online', label: '在线' },
  { value: 'offline', label: '离线' },
]

const q = ref('')
const statusFilter = ref<StatusFilter>('all')
const sortBy = ref<SortBy>('name')
function pickSort(value: SortBy): void {
  sortBy.value = value
  sortOpen.value = false
}

function statusOf(n: NodeOut): 'online' | 'offline' {
  const s = hub.statuses[n.node_id] || n.status
  return s === 'online' ? 'online' : 'offline'
}
function acct(id: string): AccountSnapshot | undefined {
  return hub.accounts[id]
}
function fmtMoney(n: number | undefined): string {
  return (n ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function fmtSigned(n: number): string {
  const abs = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return n >= 0 ? `+${abs}` : `-${abs}`
}
function fmtVol(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function nodeVolume(positions: Position[] | undefined): number {
  return (positions || []).reduce((a, p) => a + (p.volume || 0), 0)
}
function nodeProfit(positions: Position[] | undefined): number {
  return (positions || []).reduce((a, p) => a + (p.profit || 0), 0)
}
function goNode(id: string): void {
  router.push({ name: 'home-node', params: { id } })
}

const totalBalance = computed(() =>
  homeVisibleNodes.value.reduce(
    (acc, n) => acc + ((hub.accounts[n.node_id]?.balance) || 0),
    0,
  ),
)
const totalPositions = computed(() =>
  homeVisibleNodes.value.reduce(
    (a, n) => a + ((hub.accounts[n.node_id]?.positions?.length) || 0),
    0,
  ),
)
const totalVolume = computed(() =>
  homeVisibleNodes.value.reduce(
    (a, n) =>
      a + ((hub.accounts[n.node_id]?.positions || []).reduce(
        (x, p) => x + (p.volume || 0),
        0,
      )),
    0,
  ),
)
const totalProfit = computed(() =>
  homeVisibleNodes.value.reduce(
    (a, n) =>
      a + ((hub.accounts[n.node_id]?.positions || []).reduce(
        (x, p) => x + (p.profit || 0),
        0,
      )),
    0,
  ),
)

/** 首页统计：全/在线/离线都基于首页可见的派生列表。 */
const statusCounts = computed(() => {
  let online = 0
  let offline = 0
  for (const n of homeVisibleNodes.value) {
    if (statusOf(n) === 'online') online += 1
    else offline += 1
  }
  return { all: homeVisibleNodes.value.length, online, offline }
})

const filteredNodes = computed(() => {
  const query = q.value.trim().toLowerCase()
  let list = homeVisibleNodes.value.filter((n) => {
    const positions = acct(n.node_id)?.positions
    const profit = nodeProfit(positions)
    if (statusFilter.value === 'online' || statusFilter.value === 'offline') {
      if (statusOf(n) !== statusFilter.value) return false
    } else if (statusFilter.value === 'holding') {
      if (!positions?.length) return false
    } else if (statusFilter.value === 'profit') {
      if (profit <= 0) return false
    } else if (statusFilter.value === 'loss') {
      if (profit >= 0) return false
    }
    if (!query) return true
    const a = acct(n.node_id)
    const hay = [
      n.name,
      n.node_id,
      String(a?.login ?? n.mt5_login ?? ''),
      String(a?.server ?? n.mt5_server ?? ''),
    ]
      .join(' ')
      .toLowerCase()
    return hay.includes(query)
  })

  const equityOf = (n: NodeOut) => acct(n.node_id)?.equity ?? 0
  // 是否有持仓（无快照的节点按无仓位处理）
  const hasPositions = (n: NodeOut) => (acct(n.node_id)?.positions?.length || 0) > 0
  // 主节点始终置顶；主键之后再按用户选定的次级规则排序。
  const primaryFirst = (a: NodeOut, b: NodeOut) =>
    (a.is_primary ? 0 : 1) - (b.is_primary ? 0 : 1)
  list = [...list].sort((a, b) => {
    // 有仓位优先：避免个别节点异常未同步时落到列表末尾（始终生效）
    const posCmp = (hasPositions(b) ? 0 : 1) - (hasPositions(a) ? 0 : 1)
    if (posCmp !== 0) return posCmp
    const primaryCmp = primaryFirst(a, b)
    if (primaryCmp !== 0) return primaryCmp
    if (sortBy.value === 'equity_desc') return equityOf(b) - equityOf(a)
    if (sortBy.value === 'equity_asc') return equityOf(a) - equityOf(b)
    return a.name.localeCompare(b.name, 'zh')
  })
  return list
})

const groupedNodes = computed(() => {
  const map = new Map<string, NodeOut[]>()
  for (const n of filteredNodes.value) {
    const name = (n.owner_username || '').trim() || '未分配'
    const bucket = map.get(name)
    if (bucket) bucket.push(n)
    else map.set(name, [n])
  }
  return [...map.entries()].map(([name, nodes]) => ({
    key: name,
    name,
    initial: name.slice(0, 1),
    nodes,
    online: nodes.filter((n) => statusOf(n) === 'online').length,
    balance: nodes.reduce((sum, n) => sum + (acct(n.node_id)?.balance || 0), 0),
    profit: nodes.reduce((sum, n) => sum + nodeProfit(acct(n.node_id)?.positions), 0),
  }))
})

async function closeNodeAll(n: NodeOut): Promise<void> {
  if (!(await confirmAction(
    `确认平掉节点「${n.name}」的全部持仓？\n将同时终止该节点上所有进行中的策略任务。`,
    '确认平仓',
  ))) return
  await hub.closeNode(n.node_id, { target: 'all' })
}

// ---- 远程撤挂单（只撤挂单，持仓不动） ----
// 主节点卡片才显示：主节点是唯一接收策略信号、也是挂单唯一堆积的节点
const pendingBusy = ref<Record<string, boolean>>({})

function pendingOf(n: NodeOut): PendingOrder[] | undefined {
  return acct(n.node_id)?.orders
}

/** 挂单按钮的四态禁用原因；返回空串表示可点 */
function pendingDisabledReason(n: NodeOut): string {
  if (n.status !== 'online') return '节点离线，无法操作'
  const orders = pendingOf(n)
  if (orders === undefined) return '等待节点上报挂单数据'
  if (!orders.length) return '无挂单'
  if (pendingBusy.value[n.node_id]) return '正在执行删挂单'
  return ''
}

/** 该节点是否属于开启限价监听的分组（决定撤单提示要不要出现第三行）。 */
function hasLimitWatch(n: NodeOut): boolean {
  return hub.groups.some(
    (g) => g.enabled && g.limit_watch_enabled
      && g.nodes.some((m) => m.node_id === n.node_id && m.limit_watch_report !== false),
  )
}

/** 撤单确认弹窗：列挂单明细 + 三条关键提示（含限价监听提醒） */
async function confirmCancelPending(n: NodeOut): Promise<void> {
  const orders = pendingOf(n) ?? []
  if (!orders.length) return
  const manual = orders.filter((o) => !o.magic).length
  const lines = orders
    .slice(0, 20)
    .map((o) => `• ${o.symbol}  ${o.type}  ${o.volume}手  @${o.price_open}  magic=${o.magic ?? 0}`)
    .join('\n')
  const more = orders.length > 20 ? `\n…（共 ${orders.length} 条）` : ''
  // 策略任务数走既有预查接口；查不到就只说明「会停止在跑策略」，不猜数字
  let strategies = -1
  try {
    strategies = (await fetchActiveTasks(n.node_id)).count
  } catch {
    strategies = -1   // 预查失败不阻塞撤单，也不显示可能错误的数字
  }
  const hints = [
    '· 只删除挂单，已有持仓不受影响。',
    strategies >= 0
      ? `· 将同时停止该节点上 ${strategies} 个在跑策略任务（否则策略会自动补单）。`
      : '· 将同时停止该节点上所有在跑策略任务（否则策略会自动补单）。',
  ]
  if (hasLimitWatch(n)) {
    hints.push('· 该节点属于开启限价监听的分组，删除后这些手工限价单不会再触发策略。')
  }
  try {
    await ElMessageBox.confirm(
      `确认删除「${n.name}」的全部挂单？\n\n` +
        `该节点当前有 ${orders.length} 张挂单（手工单 ${manual} 张 / 策略单 ${orders.length - manual} 张）：\n` +
        `${lines}${more}\n\n${hints.join('\n')}`,
      '删除全部挂单',
      {
        type: 'warning',
        confirmButtonText: `确认删除 ${orders.length} 张挂单`,
        cancelButtonText: '取消',
        customClass: 'cancel-pending-box',
      },
    )
  } catch {
    return   // 用户点了取消
  }

  pendingBusy.value = { ...pendingBusy.value, [n.node_id]: true }
  try {
    const res: CancelPendingResult = await hub.cancelPendingAll(n.node_id, { scope: 'all' })
    reportCancelPending(res)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail || '删挂单失败，请稍后重试')
  } finally {
    const next = { ...pendingBusy.value }
    delete next[n.node_id]
    pendingBusy.value = next
  }
}

/** 撤单结果分五种情形反馈，失败明细绝不吞掉 */
function reportCancelPending(res: CancelPendingResult): void {
  if (res.status === 'error') {
    ElMessage.error(res.error || '节点未确认撤单')
    return
  }
  const extra: string[] = []
  if (res.failed?.length) {
    extra.push(
      `失败明细：${res.failed.map((f) => `${f.symbol || '-'}${f.ticket} ${f.error}`).join('；')}`,
    )
  }
  if (res.status === 'regenerated' || (res.remaining ?? 0) > 0) {
    extra.push('撤单后系统又生成了挂单，可能是限价监听自动触发，请检查分组监听开关')
  }
  if (res.strategies?.forced) {
    extra.push(`其中 ${res.strategies.forced} 个任务为强制停止`)
  }
  const tail = extra.length ? `\n${extra.join('\n')}` : ''
  if (res.failed?.length) ElMessage.warning(`删除 ${res.cancelled} 张挂单，${res.failed.length} 张失败${tail}`)
  else ElMessage.success(`已删除 ${res.cancelled} 张挂单${tail}`)
}
</script>

<template>
  <div class="home-board" :class="{ 'is-config': trendConfigOpen }">
    <div class="home-board-main">
      <HubTrendEmbed home />
    </div>

    <aside v-if="!trendConfigOpen" class="home-board-side">
      <p v-if="auth.isAdmin" class="home-scope">
        首页仅显示所有者为你本人的节点，共 {{ statusCounts.all }} 个。尚未分配或已分给其他用户的节点，请到「节点」或「总览」查看。
      </p>

      <section class="home-panel">
        <div class="home-panel-head">
          <p class="home-kicker">节点总览</p>
          <span class="home-online-pill">
            <i></i>{{ statusCounts.online }} / {{ statusCounts.all }} 在线
          </span>
        </div>

        <div class="home-stats">
          <div class="home-stat">
            <span class="k">在线节点</span>
            <span class="v">{{ statusCounts.online }}/{{ statusCounts.all }}</span>
          </div>
          <div class="home-stat">
            <span class="k">合计余额</span>
            <span class="v">{{ fmtMoney(totalBalance) }}</span>
          </div>
          <div class="home-stat">
            <span class="k">合计持仓</span>
            <span class="v">{{ totalPositions }}</span>
          </div>
          <div class="home-stat">
            <span class="k">合计手数</span>
            <span class="v">{{ fmtVol(totalVolume) }}</span>
          </div>
          <div class="home-stat home-stat-pnl" :class="totalProfit >= 0 ? 'is-up' : 'is-down'">
            <span class="k">合计盈亏</span>
            <span class="v">{{ fmtSigned(totalProfit) }}</span>
          </div>
        </div>

        <div class="home-tools">
          <label class="home-search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
            <input v-model="q" type="search" placeholder="搜索节点编号 / 名称" />
          </label>
          <div class="home-sort">
            <button type="button" class="home-sort-btn" :aria-expanded="sortOpen" @click.stop="toggleSort">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M8 7h11M8 12h8M8 17h5"/><path d="M4 7v10M4 17l-2-2M4 17l2-2"/></svg>
              排序
            </button>
            <div v-if="sortOpen" class="home-menu" role="menu">
              <button
                v-for="opt in SORT_OPTIONS"
                :key="opt.value"
                type="button"
                class="home-menu-item"
                :class="{ active: sortBy === opt.value }"
                role="menuitem"
                @click="pickSort(opt.value)"
              >{{ opt.label }}</button>
            </div>
          </div>
        </div>

        <div class="home-filters" role="group" aria-label="节点筛选">
          <button
            v-for="f in FILTERS"
            :key="f.value"
            type="button"
            class="home-filter"
            :class="{ active: statusFilter === f.value }"
            @click="statusFilter = f.value"
          >{{ f.label }}</button>
        </div>

        <div v-if="groupedNodes.length" class="home-groups">
          <section v-for="g in groupedNodes" :key="g.key" class="home-group">
            <header class="home-group-head">
              <span class="home-avatar" aria-hidden="true">{{ g.initial }}</span>
              <div class="home-group-text">
                <strong>{{ g.name }}</strong>
                <p>{{ g.nodes.length }} 个节点 · {{ g.online }}/{{ g.nodes.length }} 在线 · 余额 {{ fmtMoney(g.balance) }}</p>
              </div>
              <span class="home-group-pnl" :class="g.profit >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(g.profit) }}</span>
            </header>

            <article
              v-for="n in g.nodes"
              :key="n.node_id"
              class="home-node"
              @click="goNode(n.node_id)"
            >
              <div class="home-node-top">
                <span class="home-node-ico" :class="{ primary: n.is_primary }" aria-hidden="true">
                  <svg v-if="n.is_primary" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 9.2 8.4 2.4 9.2l5 4.6L6.2 21 12 17.6 17.8 21l-1.2-7.2 5-4.6-6.8-.8L12 2z"/></svg>
                  <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 15V9M12 15v-3M16 15V8"/></svg>
                </span>
                <div class="home-node-id">
                  <div class="home-node-title">
                    <span>{{ n.node_id }}</span>
                    <em v-if="n.is_primary">主节点</em>
                  </div>
                  <p>{{ n.name }}</p>
                </div>
                <span class="home-status" :class="statusOf(n)">{{ statusOf(n) === 'online' ? '在线' : '离线' }}</span>
                <div class="home-node-menu" @click.stop>
                  <button
                    type="button"
                    class="home-more"
                    title="更多操作"
                    aria-haspopup="menu"
                    :aria-expanded="openMenuNodeId === n.node_id"
                    @click="toggleNodeMenu(n)"
                  >⋯</button>
                  <div v-if="openMenuNodeId === n.node_id" class="home-menu" role="menu">
                    <button type="button" class="home-menu-item" role="menuitem" @click="closeNodeMenu(); goNode(n.node_id)">查看详情</button>
                    <button
                      v-if="!n.is_primary || auth.isAdmin"
                      type="button"
                      class="home-menu-item"
                      :class="{ active: n.is_primary }"
                      role="menuitem"
                      @click="togglePrimary(n)"
                    >{{ n.is_primary ? '取消主节点' : '设置为主节点' }}</button>
                    <button
                      type="button"
                      class="home-menu-item"
                      role="menuitem"
                      :disabled="!(acct(n.node_id)?.positions?.length)"
                      @click="closeNodeMenu(); closeNodeAll(n)"
                    >平掉该节点全部</button>
                    <button
                      v-if="n.is_primary"
                      type="button"
                      class="home-menu-item"
                      role="menuitem"
                      :disabled="!!pendingDisabledReason(n)"
                      :title="pendingDisabledReason(n) || '删除该节点全部挂单（不动持仓）'"
                      @click="closeNodeMenu(); confirmCancelPending(n)"
                    >{{ pendingOf(n)?.length ? `删挂单 (${pendingOf(n)!.length})` : '删挂单' }}</button>
                  </div>
                </div>
              </div>
              <div class="home-node-metrics">
                <span><em>余额</em> {{ fmtMoney(acct(n.node_id)?.balance) }}</span>
                <span><em>持仓</em> {{ acct(n.node_id)?.positions?.length || 0 }} · {{ fmtVol(nodeVolume(acct(n.node_id)?.positions)) }}手</span>
                <strong :class="nodeProfit(acct(n.node_id)?.positions) >= 0 ? 'is-up' : 'is-down'">
                  {{ fmtSigned(nodeProfit(acct(n.node_id)?.positions)) }}
                </strong>
              </div>
            </article>
          </section>
        </div>

        <p v-else-if="homeVisibleNodes.length" class="home-empty">没有匹配的节点</p>
        <p v-else class="home-empty">
          <template v-if="auth.isAdmin">你当前没有所有者为本人的节点。请到「节点」页分配或新建后再回来查看。</template>
          <template v-else>还没有节点，请到「节点」页面创建。</template>
        </p>
        <p
          v-if="filteredNodes.length && !filteredNodes.some((n) => n.is_primary)"
          class="home-empty"
        >尚未设置主节点，「删挂单」入口在设置主节点后可用。</p>
      </section>
    </aside>
  </div>
</template>

<style scoped>
.home-board {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 16px;
  align-items: start;
  min-width: 0;
}
.home-board.is-config { display: block; }
.home-board.is-config .home-board-main {
  max-width: 560px;
  margin: 8px auto 0;
}
.home-board-main,
.home-board-side { min-width: 0; }
.home-board-side {
  position: sticky;
  top: 76px;
}
.home-scope {
  margin: 0 0 8px;
  color: #5B6B85;
  font-size: 11px;
  line-height: 1.5;
}
.home-panel {
  border: 1px solid #1C2740;
  background: #101828;
  border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  padding: 16px;
}
.home-panel-head,
.home-group-head,
.home-node-top,
.home-node-metrics,
.home-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.home-panel-head { justify-content: space-between; }
.home-kicker {
  margin: 0;
  color: #8B96A8;
  font-size: 12px;
  font-weight: 500;
}
.home-online-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 10px;
}
.home-online-pill i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #34d399;
}
.home-stats {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.home-stat {
  grid-column: span 2;
  display: block;
  gap: 0;
  min-height: 0;
  overflow: hidden;
  position: static;
  background: #0C1220;
  border: 1px solid #22304A;
  border-radius: 8px;
  padding: 8px 10px;
  min-width: 0;
}
.home-stat::before { content: none; display: none; }
.home-stat:nth-child(4),
.home-stat:nth-child(5) { grid-column: span 3; }
.home-stat .k {
  display: block;
  color: #5B6B85;
  font-size: 11px;
  margin-bottom: 4px;
}
.home-stat .v {
  display: block;
  color: #E6EBF4;
  font-size: 16px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.home-stat-pnl.is-up {
  background: rgba(16, 185, 129, 0.12);
  border-color: rgba(16, 185, 129, 0.35);
}
.home-stat-pnl.is-down {
  background: rgba(239, 68, 68, 0.1);
  border-color: rgba(239, 68, 68, 0.32);
}
.home-stat-pnl.is-up .v { color: #34d399; }
.home-stat-pnl.is-down .v { color: #f87171; }
.home-tools { margin-top: 12px; }
.home-search {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #5B6B85;
}
.home-search svg { width: 14px; height: 14px; flex-shrink: 0; }
.home-search input {
  flex: 1;
  width: auto;
  min-width: 0;
  border: none;
  border-radius: 0;
  background: transparent;
  color: #E6EBF4;
  font: inherit;
  font-size: 12px;
  padding: 0;
  outline: none;
  box-shadow: none;
  backdrop-filter: none;
}
.home-search input:focus {
  border: none;
  box-shadow: none;
}
.home-search input::placeholder { color: #5B6B85; }
.home-sort { position: relative; flex-shrink: 0; }
.home-sort-btn,
.home-filter,
.home-more,
.home-menu-item {
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
}
.home-sort-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 34px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  font-size: 12px;
}
.home-sort-btn svg { width: 14px; height: 14px; }
.home-sort-btn:hover,
.home-filter:hover,
.home-more:hover { background: #16213A; color: #E6EBF4; }
.home-filters {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 4px;
  margin-top: 8px;
}
.home-filter {
  padding: 6px 0;
  border-radius: 6px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
}
.home-filter.active,
.home-menu-item.active {
  background: #2563EB;
  border-color: transparent;
  color: #fff;
}
.home-groups { margin-top: 14px; display: flex; flex-direction: column; gap: 14px; }
.home-group-head { align-items: flex-start; }
.home-avatar {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #162033;
  color: #8B96A8;
  font-size: 12px;
  font-weight: 650;
  flex-shrink: 0;
}
.home-group-text { flex: 1; min-width: 0; }
.home-group-text strong {
  display: block;
  color: #E6EBF4;
  font-size: 13px;
  font-weight: 600;
}
.home-group-text p {
  margin: 2px 0 0;
  color: #5B6B85;
  font-size: 11px;
  line-height: 1.4;
}
.home-group-pnl {
  font-size: 12px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.home-node {
  margin-top: 8px;
  padding: 10px;
  border-radius: 10px;
  border: 1px solid #22304A;
  background: #0C1220;
  cursor: pointer;
}
.home-node:hover { border-color: #31425f; }
.home-node-top { align-items: flex-start; }
.home-node-ico {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #8B96A8;
  background: #101828;
}
.home-node-ico svg { width: 16px; height: 16px; }
.home-node-ico.primary { color: #fbbf24; background: rgba(251, 191, 36, 0.12); }
.home-node-id { flex: 1; min-width: 0; }
.home-node-title {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: #E6EBF4;
  font-size: 13px;
  font-weight: 600;
}
.home-node-title span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.home-node-title em {
  flex-shrink: 0;
  font-style: normal;
  font-size: 10px;
  font-weight: 600;
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.12);
  border-radius: 999px;
  padding: 1px 6px;
}
.home-node-id p {
  margin: 2px 0 0;
  color: #5B6B85;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.home-status {
  flex-shrink: 0;
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 600;
}
.home-status.online { color: #34d399; background: rgba(16, 185, 129, 0.12); }
.home-status.offline { color: #8B96A8; background: rgba(139, 150, 168, 0.12); }
.home-node-menu { position: relative; flex-shrink: 0; }
.home-more {
  width: 24px;
  height: 24px;
  padding: 0;
  border-radius: 6px;
  background: transparent;
  color: #8B96A8;
  font-size: 16px;
  line-height: 1;
}
.home-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 8;
  min-width: 148px;
  padding: 4px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
.home-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border-radius: 6px;
  background: transparent;
  color: #E6EBF4;
  font-size: 12px;
}
.home-menu-item:hover:not(:disabled) { background: #16213A; }
.home-node-metrics {
  margin-top: 8px;
  color: #8B96A8;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.home-node-metrics em {
  font-style: normal;
  color: #5B6B85;
  margin-right: 4px;
}
.home-node-metrics span { white-space: nowrap; }
.home-node-metrics strong {
  margin-left: auto;
  font-size: 13px;
  font-weight: 650;
}
.is-up { color: #34d399; }
.is-down { color: #f87171; }
.home-empty {
  margin: 12px 0 0;
  color: #5B6B85;
  font-size: 12px;
  line-height: 1.5;
}
@media (max-width: 1023px) {
  .home-board { grid-template-columns: 1fr; gap: 12px; }
  .home-board-side { position: static; }
  .home-panel { padding: 12px; }
}
@media (max-width: 420px) {
  .home-node-metrics { flex-wrap: wrap; }
  .home-node-metrics strong { margin-left: 0; }
}
</style>
