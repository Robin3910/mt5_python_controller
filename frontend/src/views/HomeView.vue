<script setup lang="ts">
// 总览页：趋势观测 + 汇总统计 + 节点卡片（筛选/排序/分页）+ 远程平仓 + 事件流
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import HubTrendEmbed from '@/components/HubTrendEmbed.vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, NodeOut, Position } from '@/api/types'
import { confirmAction } from '@/utils/confirm'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const hub = useHubStore()
const auth = useAuthStore()
const router = useRouter()

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
  openMenuNodeId.value = openMenuNodeId.value === n.node_id ? null : n.node_id
}
function closeNodeMenu(): void {
  openMenuNodeId.value = null
}
function onDocClick(ev: MouseEvent): void {
  if (!openMenuNodeId.value) return
  const target = ev.target as HTMLElement | null
  // 点在菜单容器 / 触发按钮上都不要关闭
  if (target && target.closest('.dash-node-menu')) return
  closeNodeMenu()
}
function onDocKey(ev: KeyboardEvent): void {
  if (ev.key === 'Escape') closeNodeMenu()
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

/** 旧主节点 id：设置主节点时找同 owner 下当前的主节点；取消主节点就是自己。 */
function findCurrentPrimaryId(excludeId: string): string | null {
  const sameOwner = hub.nodes.filter((x) => x.node_id !== excludeId && x.is_primary)
  return sameOwner[0]?.node_id ?? null
}

/** 把卡片操作菜单内的「主节点」开关切换走完整流程：
 *  预查 in-flight → 命中阻塞直接禁提交 / 改文案 → 二次确认 → 调用 store → 处理 409 / 通用错误。
 *
 *  客户（普通用户）只能「设置为主节点」：菜单里不渲染取消入口，函数入口再兜一层，
 *  避免将来别处复用时把取消能力漏给客户——主节点一旦被清空，交易页会直接退化成
 *  「尚未设置主节点」空态，客户自己也无法在页面上恢复。 */
async function togglePrimary(n: NodeOut): Promise<void> {
  closeNodeMenu()
  const next = !n.is_primary
  if (!next && !auth.isAdmin) return

  // 阶段 0 预查：旧主节点在跑任务时直接弹阻塞弹窗，不进入二次确认
  const victimId = next ? findCurrentPrimaryId(n.node_id) : n.is_primary ? n.node_id : null
  if (victimId) {
    try {
      const active = await fetchActiveTasks(victimId)
      if (active.count > 0) {
        await showPrimaryBlockedDialog(active)
        return
      }
    } catch (e) {
      // 预查失败不阻塞流程（后端会再校验一次）
      console.warn('primary precheck failed', e)
    }
  }

  if (next) {
    if (!(await confirmAction(
      `将「${n.name}」设置为本账号的主节点？\n` +
        '同一账号下已有主节点时，原主节点会被自动取消。',
      '设置为主节点',
    ))) return
  } else {
    if (!(await confirmAction(
      `取消「${n.name}」的主节点标记？`,
      '取消主节点',
    ))) return
  }
  try {
    const updated = await hub.setNodePrimary(n.node_id, next)
    ElMessage.success(
      updated.is_primary
        ? `已将「${updated.name}」设置为主节点`
        : `已取消「${updated.name}」的主节点标记`,
    )
  } catch (err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    // 409 阻塞：detail 是个对象，带 blocking_tasks
    if (detail && typeof detail === 'object' && (detail as { code?: string }).code === 'primary_switch_blocked') {
      const d = detail as {
        node_name?: string
        message?: string
        blocking_tasks?: Array<{ group_id: string; symbol: string | null; status: string; magic: number | null }>
      }
      await showPrimaryBlockedDialog({
        count: d.blocking_tasks?.length ?? 0,
        node_name: d.node_name ?? '',
        items: (d.blocking_tasks ?? []).map((t) => ({
          dispatch_id: 0,
          task_id: 0,
          signal_id: '',
          group_id: t.group_id,
          symbol: t.symbol,
          magic: t.magic,
          status: t.status,
        })),
      })
      return
    }
    // 其它错误：保持原 toast
    const text = typeof detail === 'string' ? detail : ''
    ElMessage.error(text || (next ? '设置主节点失败' : '取消主节点失败'))
  }
}

async function showPrimaryBlockedDialog(active: {
  count: number
  node_name: string
  items: Array<{ group_id: string; symbol: string | null; status: string; magic: number | null }>
}): Promise<void> {
  const lines = active.items
    .slice(0, 20)
    .map((t) => {
      const sym = t.symbol ?? '-'
      const mg = t.magic != null ? `#${t.magic}` : '-'
      return `• ${t.group_id}  ${sym}  ${t.status}  ${mg}`
    })
    .join('\n')
  const more = active.items.length > 20 ? `\n…（共 ${active.items.length} 条）` : ''
  await ElMessageBox.alert(
    `旧主节点「${active.node_name}」仍有 ${active.count} 条在跑子任务，\n` +
      '主节点切换已被系统拒绝。请先到任务页收口这些任务后，再回来重试。\n\n' +
      '阻塞明细：\n' + lines + more,
    '主节点切换被拦截',
    {
      type: 'warning',
      confirmButtonText: '我知道了',
    },
  )
}

type StatusFilter = 'all' | 'online' | 'offline'
type SortBy = 'equity_desc' | 'equity_asc' | 'name'
const SORT_TAGS: TagOption<SortBy>[] = [
  { value: 'equity_desc', label: '按净值降序' },
  { value: 'equity_asc', label: '按净值升序' },
  { value: 'name', label: '按名称' },
]
type ViewMode = 'grid' | 'list'

const q = ref('')
const statusFilter = ref<StatusFilter>('all')
const sortBy = ref<SortBy>('equity_desc')
const viewMode = ref<ViewMode>('grid')
const page = ref(1)
const pageSize = 9

function statusOf(n: NodeOut): 'online' | 'offline' {
  const s = hub.statuses[n.node_id] || n.status
  return s === 'online' ? 'online' : 'offline'
}
function acct(id: string): AccountSnapshot | undefined {
  return hub.accounts[id]
}
function fmt(n: number | undefined): string {
  return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}
function fmtSigned(n: number): string {
  const abs = Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })
  return n >= 0 ? `+${abs}` : `-${abs}`
}
function nodeVolume(positions: Position[] | undefined): number {
  return (positions || []).reduce((a, p) => a + (p.volume || 0), 0)
}
function nodeProfit(positions: Position[] | undefined): number {
  return (positions || []).reduce((a, p) => a + (p.profit || 0), 0)
}
function goNode(id: string): void {
  router.push({ name: 'node-detail', params: { id } })
}

const totalEquity = computed(() =>
  homeVisibleNodes.value.reduce(
    (acc, n) => acc + ((hub.accounts[n.node_id]?.equity) || 0),
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
    if (statusFilter.value !== 'all' && statusOf(n) !== statusFilter.value) return false
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
  // 主节点始终置顶；主键之后再按用户选定的次级规则排序。
  const primaryFirst = (a: NodeOut, b: NodeOut) =>
    (a.is_primary ? 0 : 1) - (b.is_primary ? 0 : 1)
  list = [...list].sort((a, b) => {
    const primaryCmp = primaryFirst(a, b)
    if (primaryCmp !== 0) return primaryCmp
    if (sortBy.value === 'equity_desc') return equityOf(b) - equityOf(a)
    if (sortBy.value === 'equity_asc') return equityOf(a) - equityOf(b)
    return a.name.localeCompare(b.name, 'zh')
  })
  return list
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredNodes.value.length / pageSize)))

const pagedNodes = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredNodes.value.slice(start, start + pageSize)
})

const pageNumbers = computed(() => {
  const total = totalPages.value
  const cur = page.value
  const pages: number[] = []
  const from = Math.max(1, cur - 2)
  const to = Math.min(total, from + 4)
  for (let i = Math.max(1, to - 4); i <= to; i++) pages.push(i)
  return pages
})

watch([q, statusFilter, sortBy], () => {
  page.value = 1
})
watch(totalPages, (tp) => {
  if (page.value > tp) page.value = tp
})

async function closeNodeAll(n: NodeOut): Promise<void> {
  if (!(await confirmAction(
    `确认平掉节点「${n.name}」的全部持仓？\n将同时终止该节点上所有进行中的策略任务。`,
    '确认平仓',
  ))) return
  await hub.closeNode(n.node_id, { target: 'all' })
}
</script>

<template>
  <div class="dash-page">
    <!-- <div class="row between page-header">
      <div>
        <div class="h1">实时总览11122</div>
        <p class="muted page-desc">多节点账户净值、持仓与事件流实时监控</p>
      </div>
      <div class="row" style="gap: 8px">
        <ManualStrategyTrigger />
      </div>
    </div> -->

    <HubTrendEmbed compact show-full-panel class="dash-trend" />

    <div class="dash-summary">
      <div class="card card-pad dash-stat">
        <div class="stat"><span class="k">在线节点</span><span class="v">{{ statusCounts.online }} / {{ statusCounts.all }}</span></div>
      </div>
      <div class="card card-pad dash-stat">
        <div class="stat"><span class="k">合计净值</span><span class="v">{{ fmt(totalEquity) }}</span></div>
      </div>
      <div class="card card-pad dash-stat">
        <div class="stat"><span class="k">合计持仓</span><span class="v">{{ totalPositions }}</span></div>
      </div>
      <div class="card card-pad dash-stat">
        <div class="stat"><span class="k">合计手数</span><span class="v">{{ fmt(totalVolume) }}</span></div>
      </div>
      <div class="card card-pad dash-stat" :class="totalProfit >= 0 ? 'dash-stat-profit-pos' : 'dash-stat-profit-neg'">
        <div class="stat"><span class="k">合计盈亏</span><span class="v dash-stat-profit-value">{{ fmtSigned(totalProfit) }}</span></div>
      </div>
    </div>

    <!-- 管理员专属提示：本页只展示「管理员自己独占拥有」的节点 -->
    <div v-if="auth.isAdmin" class="home-scope-banner muted">
      首页仅显示所有者（owner）为你本人的节点，共 {{ statusCounts.all }} 个；
      尚未分配的节点、以及分配给其他用户的节点，请在「节点」或「总览」中查看。
    </div>

    <div class="dash-layout">
      <div class="dash-main">
        <div class="card card-pad dash-toolbar">
          <div class="dash-search">
            <svg class="dash-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
            <input v-model="q" class="dash-search-input" type="search" placeholder="搜索节点 / 账号" />
          </div>
          <div class="dash-status-chips" role="group" aria-label="状态筛选">
            <button type="button" class="dash-chip" :class="{ active: statusFilter === 'all' }" @click="statusFilter = 'all'">全部 {{ statusCounts.all }}</button>
            <button type="button" class="dash-chip online" :class="{ active: statusFilter === 'online' }" @click="statusFilter = 'online'">在线 {{ statusCounts.online }}</button>
            <button type="button" class="dash-chip offline" :class="{ active: statusFilter === 'offline' }" @click="statusFilter = 'offline'">离线 {{ statusCounts.offline }}</button>
          </div>
          <div class="dash-toolbar-right">
            <TagSelect v-model="sortBy" :options="SORT_TAGS" aria-label="排序" />
            <!-- <div class="dash-view-toggle" role="group" aria-label="视图切换">
              <button type="button" class="dash-view-btn" :class="{ active: viewMode === 'grid' }" title="网格" @click="viewMode = 'grid'">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
              </button>
              <button type="button" class="dash-view-btn" :class="{ active: viewMode === 'list' }" title="列表" @click="viewMode = 'list'">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
              </button>
            </div> -->
          </div>
        </div>

        <div class="dash-list-head row between">
          <div>
            <strong>节点列表</strong>
            <span class="muted" style="margin-left: 8px; font-size: 12px">{{ filteredNodes.length }} 个结果</span>
          </div>
        </div>

        <div class="dash-nodes" :class="viewMode === 'list' ? 'is-list' : 'is-grid'">
          <div v-for="n in pagedNodes" :key="n.node_id" class="card card-pad dash-node-card">
            <div class="row between dash-node-head">
              <div class="row" style="gap: 8px; min-width: 0">
                <span class="dot" :class="statusOf(n)"></span>
                <strong class="dash-node-name" @click="goNode(n.node_id)">{{ n.name }}</strong>
                <span class="tag" :class="statusOf(n) === 'online' ? 'green' : 'red'">{{ statusOf(n) === 'online' ? '在线' : '离线' }}</span>
                <span v-if="n.is_primary" class="tag dash-node-primary-tag" title="本账号主节点">★ 主节点</span>
              </div>
              <div class="dash-node-menu">
                <button
                  type="button"
                  class="dash-more btn-ghost btn-sm"
                  title="更多操作"
                  aria-haspopup="menu"
                  :aria-expanded="openMenuNodeId === n.node_id"
                  @click.stop="toggleNodeMenu(n)"
                >⋯</button>
                <div
                  v-if="openMenuNodeId === n.node_id"
                  class="dash-node-menu-pop"
                  role="menu"
                  @click.stop
                >
                  <button
                    type="button"
                    class="dash-node-menu-item"
                    role="menuitem"
                    @click="goNode(n.node_id)"
                  >
                    <span class="dash-node-menu-icon">🔍</span>
                    <span>查看详情</span>
                  </button>
                  <!-- 只有「还没设为主节点」时才给客户显示；已是主节点的取消入口仅管理员可见 -->
                  <button
                    v-if="!n.is_primary || auth.isAdmin"
                    type="button"
                    class="dash-node-menu-item"
                    :class="{ 'is-on': !!n.is_primary }"
                    role="menuitem"
                    @click="togglePrimary(n)"
                  >
                    <span class="dash-node-menu-icon">{{ n.is_primary ? '★' : '☆' }}</span>
                    <span>{{ n.is_primary ? '取消主节点' : '设置为主节点' }}</span>
                  </button>
                </div>
              </div>
            </div>
            <div class="muted dash-node-sub">
              {{ acct(n.node_id)?.login || n.mt5_login || '—' }} @ {{ acct(n.node_id)?.server || n.mt5_server || '—' }}
            </div>

            <div class="dash-metrics">
              <div class="stat">
                <span class="k">净值</span>
                <span class="v dash-metric-v">{{ fmt(acct(n.node_id)?.equity) }}</span>
              </div>
              <div class="stat">
                <span class="k">持仓</span>
                <span class="v dash-metric-v">
                  {{ acct(n.node_id)?.positions?.length || 0 }} - {{ fmt(nodeVolume(acct(n.node_id)?.positions)) }} 手
                </span>
              </div>
              <div class="stat">
                <span class="k">盈亏</span>
                <span
                  class="v dash-metric-v dash-metric-profit"
                  :class="nodeProfit(acct(n.node_id)?.positions) >= 0 ? 'profit-pos' : 'profit-neg'"
                >{{ fmtSigned(nodeProfit(acct(n.node_id)?.positions)) }}</span>
              </div>
            </div>

            <!-- <template v-if="acct(n.node_id)?.positions?.length">
              <table class="dash-pos-table">
                <thead>
                  <tr><th>品种</th><th>方向</th><th class="right">手数</th><th class="right">盈亏</th><th></th></tr>
                </thead>
                <tbody>
                  <tr v-for="p in previewPositions(acct(n.node_id)!.positions)" :key="p.ticket">
                    <td>{{ p.symbol }}</td>
                    <td><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></td>
                    <td class="right">{{ p.volume }}</td>
                    <td class="right" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</td>
                    <td class="right"><button type="button" class="btn-sm btn-ghost" @click="closeTicket(n, p.ticket)">平</button></td>
                  </tr>
                </tbody>
              </table>
            </template> -->
            <!-- <div v-else class="muted dash-empty-pos">无持仓</div> -->

            <div class="dash-node-foot row between">
              <button
                v-if="acct(n.node_id)?.positions?.length"
                type="button"
                class="dash-link"
                @click="goNode(n.node_id)"
              >查看全部 {{ acct(n.node_id)!.positions.length }} 个持仓 →</button>
              <span v-else></span>
              <button
                type="button"
                class="btn-sm btn-ghost"
                :disabled="!acct(n.node_id)?.positions?.length"
                @click="closeNodeAll(n)"
              >平掉该节点全部</button>
            </div>
          </div>

          <div v-if="homeVisibleNodes.length && !filteredNodes.length" class="card card-pad muted span-full">没有匹配的节点</div>
          <div v-if="!homeVisibleNodes.length" class="card card-pad muted span-full">
            <template v-if="auth.isAdmin">
              你当前没有 owner 为本人的节点。
              请到「节点」页把未分配的节点分配给自己，或新建后再回到首页查看。
            </template>
            <template v-else>
              还没有节点，请到「节点」页面创建。
            </template>
          </div>
        </div>

        <div v-if="filteredNodes.length > pageSize" class="dash-pagination">
          <button type="button" class="dash-page-btn" :disabled="page <= 1" @click="page -= 1">‹</button>
          <button
            v-for="p in pageNumbers"
            :key="p"
            type="button"
            class="dash-page-btn"
            :class="{ active: p === page }"
            @click="page = p"
          >{{ p }}</button>
          <button type="button" class="dash-page-btn" :disabled="page >= totalPages" @click="page += 1">›</button>
        </div>
      </div>
<!-- 
      <aside class="card card-pad dash-events">
        <div class="row between dash-events-head">
          <strong>实时事件</strong>
          <button type="button" class="dash-link" @click="markAllRead">全部标记已读</button>
        </div>
        <div class="dash-event-tabs">
          <button type="button" class="dash-event-tab" :class="{ active: eventTab === 'all' }" @click="eventTab = 'all'">全部</button>
          <button type="button" class="dash-event-tab" :class="{ active: eventTab === 'risk' }" @click="eventTab = 'risk'">风险</button>
          <button type="button" class="dash-event-tab" :class="{ active: eventTab === 'trade' }" @click="eventTab = 'trade'">交易</button>
        </div>
        <div class="dash-event-list">
          <div
            v-for="e in filteredEvents"
            :key="eventKey(e)"
            class="dash-event"
            :class="[e.kind, { unread: !isRead(e) }]"
          >
            <span class="ts">{{ new Date(e.ts).toLocaleTimeString() }}</span>
            <span class="dash-event-icon" :class="e.kind" aria-hidden="true">
              <svg v-if="e.kind === 'warn'" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 1 21h22L12 2zm0 6 1.5 8h-3L12 8zm0 10.5a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5z"/></svg>
              <svg v-else-if="e.kind === 'ok'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6 9 17l-5-5"/></svg>
              <span v-else class="dash-event-dot"></span>
            </span>
            <span class="text">{{ e.text }}</span>
            <span
              v-if="parseDelta(e.text) != null"
              class="dash-event-delta"
              :class="(parseDelta(e.text) ?? 0) >= 0 ? 'profit-pos' : 'profit-neg'"
            >{{ fmtSigned(parseDelta(e.text)!) }}</span>
          </div>
          <div v-if="!filteredEvents.length" class="muted" style="font-size: 12px; padding: 12px 0">暂无事件</div>
        </div>
        <RouterLink class="dash-link dash-events-foot" to="/events">查看全部事件 →</RouterLink>
      </aside> -->
    </div>
  </div>
</template>
