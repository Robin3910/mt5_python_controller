<script setup lang="ts">
// 总览页：汇总统计 + 节点卡片（筛选/排序/分页）+ 远程平仓 + 事件流
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, HubEvent, NodeOut, Position } from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()
const router = useRouter()

onMounted(async () => {
  await hub.fetchNodes()
})

type StatusFilter = 'all' | 'online' | 'offline'
type SortBy = 'equity_desc' | 'equity_asc' | 'name'
type ViewMode = 'grid' | 'list'
type EventTab = 'all' | 'risk' | 'trade'

const q = ref('')
const statusFilter = ref<StatusFilter>('all')
const sortBy = ref<SortBy>('equity_desc')
const viewMode = ref<ViewMode>('grid')
const page = ref(1)
const pageSize = 9
const eventTab = ref<EventTab>('all')
const readKeys = ref<Set<string>>(new Set())

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
function eventKey(e: HubEvent): string {
  return `${e.ts}|${e.text}`
}
function isRead(e: HubEvent): boolean {
  return readKeys.value.has(eventKey(e))
}
function markAllRead(): void {
  const next = new Set(readKeys.value)
  for (const e of hub.events) next.add(eventKey(e))
  readKeys.value = next
}
/** 从事件文案解析首个 ±金额 */
function parseDelta(text: string): number | null {
  const m = text.match(/([+-])\s*(\d+(?:\.\d+)?)/)
  if (!m) return null
  const n = Number(m[2])
  if (Number.isNaN(n)) return null
  return m[1] === '-' ? -n : n
}
function previewPositions(positions: Position[] | undefined): Position[] {
  return (positions || []).slice(0, 3)
}
function goNode(id: string): void {
  router.push({ name: 'node-detail', params: { id } })
}

const totalEquity = computed(() => hub.totalEquity)
const totalPositions = computed(() =>
  Object.values(hub.accounts).reduce((a, s) => a + (s?.positions?.length || 0), 0),
)

const statusCounts = computed(() => {
  let online = 0
  let offline = 0
  for (const n of hub.nodes) {
    if (statusOf(n) === 'online') online += 1
    else offline += 1
  }
  return { all: hub.nodes.length, online, offline }
})

const filteredNodes = computed(() => {
  const query = q.value.trim().toLowerCase()
  let list = hub.nodes.filter((n) => {
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
  list = [...list]
  if (sortBy.value === 'equity_desc') list.sort((a, b) => equityOf(b) - equityOf(a))
  else if (sortBy.value === 'equity_asc') list.sort((a, b) => equityOf(a) - equityOf(b))
  else list.sort((a, b) => a.name.localeCompare(b.name, 'zh'))
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

const filteredEvents = computed(() => {
  if (eventTab.value === 'risk') return hub.events.filter((e) => e.kind === 'warn')
  if (eventTab.value === 'trade') return hub.events.filter((e) => e.kind === 'ok')
  return hub.events
})

async function closeNodeAll(n: NodeOut): Promise<void> {
  if (!(await confirmAction(`确认平掉节点「${n.name}」的全部持仓？`, '确认平仓'))) return
  await hub.closeNode(n.node_id, { target: 'all' })
}
async function closeTicket(n: NodeOut, ticket: number): Promise<void> {
  if (!(await confirmAction(`确认平掉订单 #${ticket}？`, '确认平仓'))) return
  await hub.closeNode(n.node_id, { target: 'ticket', ticket })
}
</script>

<template>
  <div class="dash-page">
    <div class="page-header">
      <div>
        <div class="h1">实时总览</div>
        <p class="muted page-desc">多节点账户净值、持仓与事件流实时监控</p>
      </div>
    </div>

    <div class="dash-summary">
      <div class="card card-pad dash-stat">
        <div class="dash-stat-icon online" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/></svg>
        </div>
        <div class="stat"><span class="k">在线节点</span><span class="v">{{ hub.onlineCount }} / {{ hub.nodes.length }}</span></div>
      </div>
      <div class="card card-pad dash-stat">
        <div class="dash-stat-icon equity" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/><path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/></svg>
        </div>
        <div class="stat"><span class="k">合计净值</span><span class="v">{{ fmt(totalEquity) }}</span></div>
      </div>
      <div class="card card-pad dash-stat">
        <div class="dash-stat-icon positions" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05"/><path d="M12 22.08V12"/></svg>
        </div>
        <div class="stat"><span class="k">合计持仓</span><span class="v">{{ totalPositions }}</span></div>
      </div>
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
            <select v-model="sortBy" class="dash-select" aria-label="排序">
              <option value="equity_desc">按净值降序</option>
              <option value="equity_asc">按净值升序</option>
              <option value="name">按名称</option>
            </select>
            <div class="dash-view-toggle" role="group" aria-label="视图切换">
              <button type="button" class="dash-view-btn" :class="{ active: viewMode === 'grid' }" title="网格" @click="viewMode = 'grid'">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
              </button>
              <button type="button" class="dash-view-btn" :class="{ active: viewMode === 'list' }" title="列表" @click="viewMode = 'list'">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
              </button>
            </div>
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
              </div>
              <button type="button" class="dash-more btn-ghost btn-sm" title="查看详情" @click="goNode(n.node_id)">⋯</button>
            </div>
            <div class="muted dash-node-sub">
              {{ acct(n.node_id)?.login || n.mt5_login || '—' }} @ {{ acct(n.node_id)?.server || n.mt5_server || '—' }}
            </div>

            <div class="dash-metrics">
              <div class="stat"><span class="k">余额</span><span class="v dash-metric-v">{{ fmt(acct(n.node_id)?.balance) }}</span></div>
              <div class="stat"><span class="k">净值</span><span class="v dash-metric-v">{{ fmt(acct(n.node_id)?.equity) }}</span></div>
              <div class="stat"><span class="k">占用保证金</span><span class="v dash-metric-v">{{ fmt(acct(n.node_id)?.margin) }}</span></div>
            </div>

            <template v-if="acct(n.node_id)?.positions?.length">
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
            </template>
            <div v-else class="muted dash-empty-pos">无持仓</div>

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
                class="btn-sm btn-danger"
                :disabled="!acct(n.node_id)?.positions?.length"
                @click="closeNodeAll(n)"
              >平掉该节点全部</button>
            </div>
          </div>

          <div v-if="hub.nodes.length && !filteredNodes.length" class="card card-pad muted span-full">没有匹配的节点</div>
          <div v-if="!hub.nodes.length" class="card card-pad muted span-full">还没有节点，请到「节点」页面创建。</div>
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
      </aside>
    </div>
  </div>
</template>
