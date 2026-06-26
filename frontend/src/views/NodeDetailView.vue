<script setup lang="ts">
// 节点详情页：Tab + 列表展示单个节点上报的数据
// （概览 / 持仓 / 报价 / 成交回报）。账户与持仓走 WS 实时刷新，
// 成交回报 = 持久化历史 + 本会话实时回报合并。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, NodeDispatchRecord, NodeFeedItem, NodeOut } from '@/api/types'

const route = useRoute()
const router = useRouter()
const hub = useHubStore()

const id = computed(() => String(route.params.id))
const node = computed<NodeOut | undefined>(() => hub.nodes.find((n) => n.node_id === id.value))
const acct = computed<AccountSnapshot | undefined>(() => hub.accounts[id.value])
const statusOf = computed(() => hub.statuses[id.value] || node.value?.status || 'offline')

type TabKey = 'overview' | 'positions' | 'prices' | 'signals' | 'feed'
const tab = ref<TabKey>('overview')
const tabs: { key: TabKey; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'positions', label: '持仓' },
  { key: 'prices', label: '报价' },
  { key: 'signals', label: '信号' },
  { key: 'feed', label: '成交回报' },
]

const history = ref<NodeDispatchRecord[]>([])
const historyPage = ref(1)
const historyPageSize = ref(20)
const historyTotal = ref(0)
const loadingHistory = ref(false)
const tokenModal = reactive({ show: false, token: '', title: '' })

const historyTotalPages = computed(() =>
  Math.max(1, Math.ceil(historyTotal.value / historyPageSize.value)),
)

async function reload(): Promise<void> {
  if (!hub.nodes.length) await hub.fetchNodes()
  await hub.fetchNodeAccount(id.value)
  historyPage.value = 1
  await loadHistory()
}
async function loadHistory(): Promise<void> {
  loadingHistory.value = true
  try {
    const res = await hub.fetchNodeDispatches(id.value, historyPage.value, historyPageSize.value)
    history.value = res.items
    historyTotal.value = res.total
    if (res.page !== historyPage.value) historyPage.value = res.page
  } finally {
    loadingHistory.value = false
  }
}
function goHistoryPage(page: number): void {
  const next = Math.min(Math.max(1, page), historyTotalPages.value)
  if (next === historyPage.value) return
  historyPage.value = next
  loadHistory()
}
function onHistoryPageSizeChange(): void {
  historyPage.value = 1
  loadHistory()
}

onMounted(reload)
watch(id, reload)

// 有新的实时回报到达时，稍后刷新一次历史，让“信号 / 成交回报”保持最新
let feedTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => (hub.nodeFeed[id.value] ?? []).length,
  () => {
    if (feedTimer) clearTimeout(feedTimer)
    feedTimer = setTimeout(loadHistory, 1500)
  },
)

// ---- 列表数据 ----
const positions = computed(() => acct.value?.positions ?? [])
const prices = computed(() => {
  const quotes = acct.value?.quotes ?? {}
  const legacy = acct.value?.prices ?? {}
  const symbols = new Set([...Object.keys(quotes), ...Object.keys(legacy)])
  return [...symbols]
    .map((symbol) => {
      const q = quotes[symbol]
      return {
        symbol,
        bid: q?.bid,
        ask: q?.ask,
        price: q?.mid ?? legacy[symbol],
        change: q?.change,
      }
    })
    .sort((a, b) => a.symbol.localeCompare(b.symbol))
})

function recordToFeedItem(r: NodeDispatchRecord): NodeFeedItem {
  return {
    signal_id: r.signal_id,
    ts: (r.finished_at || r.dispatched_at || r.received_at || 0) * 1000,
    symbol: r.symbol ?? undefined,
    action: r.action ?? undefined,
    status: r.status,
    volume: r.decided_vol ?? r.volume ?? undefined,
    sl: r.sl ?? undefined,
    tp: r.tp ?? undefined,
    price: r.price ?? undefined,
    raw_payload: r.raw_payload ?? undefined,
    order: r.order,
    error: r.error ?? undefined,
    reason: r.skip_reason ?? undefined,
    detail: r.comment ?? undefined,
  }
}

// 成交回报：第 1 页合并实时条目，其余页仅展示历史分页
const mergedFeed = computed<NodeFeedItem[]>(() => {
  const map = new Map<string, NodeFeedItem>()

  for (const r of history.value) {
    map.set(r.signal_id, recordToFeedItem(r))
  }

  if (historyPage.value === 1) {
    for (const it of hub.nodeFeed[id.value] ?? []) {
      const prev = map.get(it.signal_id)
      if (prev) {
        map.set(it.signal_id, {
          ...prev,
          ...it,
          ts: Math.max(prev.ts, it.ts),
          volume: prev.volume ?? it.volume,
          sl: prev.sl ?? it.sl,
          tp: prev.tp ?? it.tp,
          price: it.price ?? prev.price,
          raw_payload: prev.raw_payload ?? it.raw_payload,
        })
      } else {
        map.set(it.signal_id, it)
      }
    }
  }

  return [...map.values()].sort((a, b) => b.ts - a.ts)
})

// 信号 + 处理：当前页历史明细，按接收时间倒序
const signalRows = computed(() =>
  [...history.value].sort((a, b) => (b.received_at || 0) - (a.received_at || 0)),
)
// 行级展开状态：键必须是“每一行”的唯一标识，不能用 signal_id
// （同一 signal_id 可能对应多条分发记录，否则点开一条会连带展开所有同信号行）。
const expanded = ref<Record<string, boolean>>({})
function toggleRow(key: string | number): void {
  const k = String(key)
  expanded.value[k] = !expanded.value[k]
}
function isExpanded(key: string | number): boolean {
  return !!expanded.value[String(key)]
}

// ---- 格式化 ----
function fmt(n: number | undefined | null): string {
  return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}
function fmtPrice(n: number | undefined | null): string {
  if (n == null) return '—'
  return String(n)
}
function fmtChange(n: number | undefined | null): string {
  if (n == null) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}
function fmtTime(ms: number | undefined | null): string {
  return ms ? new Date(ms).toLocaleString() : '—'
}
function feedTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '成功' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
  }
  return m[status] || { cls: 'blue', text: status }
}
function feedDetail(row: NodeFeedItem): string {
  if (row.detail) return row.detail
  if (row.order) return `订单 #${row.order}`
  if (row.error) return row.error
  if (row.reason) return row.reason
  return ''
}

// ---- 操作 ----
async function toggleEnabled(): Promise<void> {
  if (!node.value) return
  await hub.updateNode(id.value, { enabled: !node.value.enabled })
}
async function doRotate(): Promise<void> {
  if (!confirm('重置该节点令牌？旧令牌将立即失效。')) return
  const res = await hub.rotateToken(id.value)
  tokenModal.token = res.token
  tokenModal.title = '新令牌（只显示一次）'
  tokenModal.show = true
}
async function closeNodeAll(): Promise<void> {
  if (!confirm('确认平掉该节点的全部持仓？')) return
  await hub.closeNode(id.value, { target: 'all' })
}
async function closeTicket(ticket: number): Promise<void> {
  if (!confirm(`平掉订单 #${ticket}？`)) return
  await hub.closeNode(id.value, { target: 'ticket', ticket })
}
function copyToken(): void {
  navigator.clipboard?.writeText(tokenModal.token)
}
</script>

<template>
  <div>
    <a class="node-link" style="font-size: 13px" @click="router.push('/nodes')">← 返回节点列表</a>

    <div v-if="node" class="row between detail-header" style="margin: 10px 0 16px">
      <div class="row" style="gap: 10px">
        <span class="dot" :class="statusOf"></span>
        <div>
          <div class="h1" style="margin: 0">{{ node.name }}</div>
          <div class="muted" style="font-size: 12px">
            {{ node.node_id }} · {{ acct?.login || node.mt5_login || '—' }} @
            {{ acct?.server || node.mt5_server || '—' }}
          </div>
        </div>
        <span class="tag" :class="statusOf === 'online' ? 'green' : ''">
          {{ statusOf === 'online' ? '在线' : '离线' }}
        </span>
      </div>
      <div class="row">
        <button class="btn-sm" :class="node.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled">
          {{ node.enabled ? '已启用' : '已禁用' }}
        </button>
        <button class="btn-sm btn-ghost" @click="doRotate">令牌</button>
        <button class="btn-sm btn-danger" :disabled="!positions.length" @click="closeNodeAll">平掉全部</button>
      </div>
    </div>

    <div v-if="!node" class="card card-pad muted">节点不存在或正在加载…</div>

    <template v-else>
      <!-- Tab 栏 -->
      <div class="tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="tab"
          :class="{ active: tab === t.key }"
          @click="tab = t.key"
        >
          {{ t.label }}
          <span v-if="t.key === 'positions' && positions.length" class="pill">{{ positions.length }}</span>
        </button>
      </div>

      <!-- 概览 -->
      <div v-if="tab === 'overview'">
        <div class="card card-pad" style="margin-bottom: 16px">
          <strong>账户</strong>
          <div v-if="acct" class="kv-grid" style="margin-top: 12px">
            <div class="kv"><span class="k">余额</span><span class="v">{{ fmt(acct.balance) }}</span></div>
            <div class="kv"><span class="k">净值</span><span class="v">{{ fmt(acct.equity) }}</span></div>
            <div class="kv"><span class="k">占用保证金</span><span class="v">{{ fmt(acct.margin) }}</span></div>
            <div class="kv"><span class="k">可用保证金</span><span class="v">{{ fmt(acct.free_margin) }}</span></div>
            <div class="kv"><span class="k">杠杆</span><span class="v">{{ acct.leverage || '—' }}</span></div>
            <div class="kv"><span class="k">最近上报</span><span class="v" style="font-size: 12px">{{ fmtTime((acct.updated_at || 0) * 1000) }}</span></div>
          </div>
          <div v-else class="muted" style="font-size: 12px; margin-top: 8px">暂无账户快照（节点未上线或尚未上报）。</div>
        </div>

        <div class="card card-pad">
          <strong>节点配置</strong>
          <div class="kv-grid" style="margin-top: 12px">
            <div class="kv">
              <span class="k">手数策略</span>
              <span class="v">
                <span class="tag blue">{{ node.lot_mode }}</span>
                <span v-if="node.lot_mode === 'fixed'"> {{ node.lot }}</span>
              </span>
            </div>
            <div class="kv"><span class="k">跟随同步模式</span><span class="v">{{ node.follow_sync ? '是' : '否' }}</span></div>
            <div class="kv"><span class="k">跟随轮询模式</span><span class="v">{{ node.follow_poll ? '是' : '否' }}</span></div>
            <div class="kv"><span class="k">轮询顺序</span><span class="v">{{ node.poll_order }}</span></div>
            <div class="kv"><span class="k">启用</span><span class="v">{{ node.enabled ? '是' : '否' }}</span></div>
            <div class="kv"><span class="k">创建时间</span><span class="v" style="font-size: 12px">{{ fmtTime((node.created_at || 0) * 1000) }}</span></div>
          </div>
        </div>
      </div>

      <!-- 持仓 -->
      <div v-else-if="tab === 'positions'">
        <div v-if="positions.length" class="list-cards mobile-only">
          <div v-for="p in positions" :key="p.ticket" class="list-card card">
            <div class="list-field"><span class="k">品种</span><span class="v">{{ p.symbol }}</span></div>
            <div class="list-field">
              <span class="k">方向</span>
              <span class="v"><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></span>
            </div>
            <div class="list-field"><span class="k">手数</span><span class="v">{{ p.volume }}</span></div>
            <div class="list-field"><span class="k">开仓价</span><span class="v">{{ p.price_open }}</span></div>
            <div class="list-field"><span class="k">现价</span><span class="v">{{ p.price_current }}</span></div>
            <div class="list-field"><span class="k">止损</span><span class="v">{{ p.sl || '—' }}</span></div>
            <div class="list-field"><span class="k">止盈</span><span class="v">{{ p.tp || '—' }}</span></div>
            <div class="list-field">
              <span class="k">盈亏</span>
              <span class="v" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</span>
            </div>
            <div class="list-field"><span class="k">魔术号</span><span class="v muted">{{ p.magic }}</span></div>
            <div class="list-field"><span class="k">时间</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ fmtTime(p.time * 1000) }}</span></div>
            <div class="list-card-actions">
              <button class="btn-sm btn-ghost" @click="closeTicket(p.ticket)">平仓</button>
            </div>
          </div>
        </div>
        <div v-else class="card card-pad muted mobile-only" style="font-size: 13px">无持仓</div>

        <div class="card card-pad table-scroll desktop-only">
        <table v-if="positions.length">
          <thead>
            <tr>
              <th>品种</th><th>方向</th><th class="right">手数</th><th class="right">开仓价</th>
              <th class="right">现价</th><th class="right">止损</th><th class="right">止盈</th><th class="right">盈亏</th><th class="right">魔术号</th><th>时间</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in positions" :key="p.ticket">
              <td>{{ p.symbol }}</td>
              <td><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></td>
              <td class="right">{{ p.volume }}</td>
              <td class="right">{{ p.price_open }}</td>
              <td class="right">{{ p.price_current }}</td>
              <td class="right">{{ p.sl || '—' }}</td>
              <td class="right">{{ p.tp || '—' }}</td>
              <td class="right" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</td>
              <td class="right muted">{{ p.magic }}</td>
              <td class="muted" style="font-size: 12px">{{ fmtTime(p.time * 1000) }}</td>
              <td class="right"><button class="btn-sm btn-ghost" @click="closeTicket(p.ticket)">平</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="muted" style="font-size: 13px; padding: 8px 0">无持仓</div>
        </div>
      </div>

      <!-- 报价 -->
      <div v-else-if="tab === 'prices'">
        <div v-if="prices.length" class="list-cards mobile-only">
          <div v-for="row in prices" :key="row.symbol" class="list-card card">
            <div class="list-field"><span class="k">品种</span><span class="v">{{ row.symbol }}</span></div>
            <div class="list-field"><span class="k">买价</span><span class="v">{{ fmtPrice(row.bid) }}</span></div>
            <div class="list-field"><span class="k">卖价</span><span class="v">{{ fmtPrice(row.ask) }}</span></div>
            <div class="list-field"><span class="k">最新价</span><span class="v">{{ fmtPrice(row.price) }}</span></div>
            <div class="list-field">
              <span class="k">日变化</span>
              <span class="v" :class="row.change == null ? '' : (row.change >= 0 ? 'profit-pos' : 'profit-neg')">{{ fmtChange(row.change) }}</span>
            </div>
          </div>
        </div>
        <div v-else class="card card-pad muted mobile-only" style="font-size: 13px">暂无报价（节点未上报观察列表）。</div>

        <div class="card card-pad table-scroll desktop-only">
          <table v-if="prices.length">
            <thead>
              <tr>
                <th>品种</th><th class="right">买价</th><th class="right">卖价</th><th class="right">最新价</th><th class="right">日变化</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in prices" :key="row.symbol">
                <td>{{ row.symbol }}</td>
                <td class="right">{{ fmtPrice(row.bid) }}</td>
                <td class="right">{{ fmtPrice(row.ask) }}</td>
                <td class="right">{{ fmtPrice(row.price) }}</td>
                <td class="right" :class="row.change == null ? '' : (row.change >= 0 ? 'profit-pos' : 'profit-neg')">{{ fmtChange(row.change) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="muted" style="font-size: 13px; padding: 8px 0">暂无报价（节点未上报观察列表）。</div>
        </div>
      </div>

      <!-- 信号 + 处理 -->
      <div v-else-if="tab === 'signals'">
        <div class="row between mobile-only" style="margin-bottom: 8px">
          <span class="muted" style="font-size: 12px">点击卡片展开详情</span>
          <button class="btn-sm btn-ghost" :disabled="loadingHistory" @click="loadHistory">
            {{ loadingHistory ? '刷新中…' : '刷新' }}
          </button>
        </div>
        <div v-if="signalRows.length" class="list-cards mobile-only">
          <div
            v-for="s in signalRows"
            :key="s.id"
            class="list-card card clickable"
            @click="toggleRow(s.id)"
          >
            <div class="list-card-head row between">
              <strong>{{ s.symbol || '—' }}</strong>
              <span class="muted">{{ isExpanded(s.id) ? '▾' : '▸' }}</span>
            </div>
            <div class="list-field"><span class="k">时间</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ fmtTime((s.received_at || 0) * 1000) }}</span></div>
            <div class="list-field">
              <span class="k">动作</span>
              <span class="v">
                <span v-if="s.action" class="tag" :class="s.action === 'BUY' ? 'green' : (s.action === 'SELL' ? 'blue' : '')">{{ s.action }}</span>
                <span v-else class="muted">—</span>
              </span>
            </div>
            <div class="list-field"><span class="k">手数</span><span class="v">{{ s.volume ?? '—' }}</span></div>
            <div class="list-field"><span class="k">SL</span><span class="v">{{ s.sl ?? '—' }}</span></div>
            <div class="list-field"><span class="k">TP</span><span class="v">{{ s.tp ?? '—' }}</span></div>
            <div class="list-field">
              <span class="k">解析</span>
              <span class="v"><span class="tag" :class="s.parsed_ok ? 'green' : 'red'">{{ s.parsed_ok ? '成功' : '失败' }}</span></span>
            </div>
            <div class="list-field">
              <span class="k">本节点处理</span>
              <span class="v">
                <span class="tag" :class="feedTag(s.status).cls">{{ feedTag(s.status).text }}</span>
                <span v-if="s.gate_result === 'skipped'" class="tag amber" style="margin-left: 4px">被过滤</span>
              </span>
            </div>
            <div v-if="isExpanded(s.id)" class="list-card-detail">
              <div class="list-field"><span class="k">信号 ID</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ s.signal_id }}</span></div>
              <div class="list-field"><span class="k">来源 IP</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ s.source_ip || '—' }}</span></div>
              <div class="list-field"><span class="k">分发模式</span><span class="v">{{ s.dispatch_mode || '—' }}</span></div>
              <div class="list-field"><span class="k">信号整体状态</span><span class="v">{{ s.signal_status || '—' }}</span></div>
              <div class="list-field"><span class="k">备注</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ s.comment || '—' }}</span></div>
              <div class="list-field"><span class="k">决策手数</span><span class="v">{{ s.decided_vol ?? '—' }}</span></div>
              <div class="list-field"><span class="k">闸门</span><span class="v">{{ s.gate_result === 'skipped' ? '被过滤' : '通过' }}</span></div>
              <div class="list-field"><span class="k">跳过原因</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ s.skip_reason || '—' }}</span></div>
              <div class="list-field"><span class="k">返回码</span><span class="v">{{ s.retcode ?? '—' }}</span></div>
              <div class="list-field"><span class="k">订单号</span><span class="v">{{ s.order ?? '—' }}</span></div>
              <div class="list-field"><span class="k">成交号</span><span class="v">{{ s.deal ?? '—' }}</span></div>
              <div class="list-field"><span class="k">错误</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ s.error || '—' }}</span></div>
              <div class="list-field"><span class="k">下发时间</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ fmtTime((s.dispatched_at || 0) * 1000) }}</span></div>
              <div class="list-field"><span class="k">完成时间</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ fmtTime((s.finished_at || 0) * 1000) }}</span></div>
              <div v-if="s.raw_payload" class="list-field">
                <span class="k">原始信号</span>
                <span class="v"><div class="token-box" style="font-size: 12px; font-weight: 400">{{ s.raw_payload }}</div></span>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="card card-pad muted mobile-only" style="font-size: 13px">暂无信号记录。</div>

        <div class="card card-pad table-scroll desktop-only">
        <div class="row between" style="margin-bottom: 8px">
          <span class="muted" style="font-size: 12px">接收到的信号及本节点的处理情况（点击行展开详情）</span>
          <button class="btn-sm btn-ghost" :disabled="loadingHistory" @click="loadHistory">
            {{ loadingHistory ? '刷新中…' : '刷新' }}
          </button>
        </div>
        <table v-if="signalRows.length">
          <thead>
            <tr>
              <th style="width: 22px"></th><th>时间</th><th>动作</th><th>品种</th>
              <th class="right">手数</th><th class="right">SL</th><th class="right">TP</th>
              <th>解析</th><th>本节点处理</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="s in signalRows" :key="s.id">
              <tr class="clickable" @click="toggleRow(s.id)">
                <td class="muted">{{ isExpanded(s.id) ? '▾' : '▸' }}</td>
                <td class="muted" style="font-size: 12px">{{ fmtTime((s.received_at || 0) * 1000) }}</td>
                <td>
                  <span v-if="s.action" class="tag" :class="s.action === 'BUY' ? 'green' : (s.action === 'SELL' ? 'blue' : '')">{{ s.action }}</span>
                  <span v-else class="muted">—</span>
                </td>
                <td>{{ s.symbol || '—' }}</td>
                <td class="right">{{ s.volume ?? '—' }}</td>
                <td class="right">{{ s.sl ?? '—' }}</td>
                <td class="right">{{ s.tp ?? '—' }}</td>
                <td><span class="tag" :class="s.parsed_ok ? 'green' : 'red'">{{ s.parsed_ok ? '成功' : '失败' }}</span></td>
                <td>
                  <span class="tag" :class="feedTag(s.status).cls">{{ feedTag(s.status).text }}</span>
                  <span v-if="s.gate_result === 'skipped'" class="tag amber" style="margin-left: 4px">被过滤</span>
                </td>
              </tr>
              <tr v-if="isExpanded(s.id)" class="detail-row">
                <td></td>
                <td colspan="8">
                  <div class="kv-grid" style="margin: 6px 0 10px">
                    <div class="kv"><span class="k">信号 ID</span><span class="v" style="font-size: 12px">{{ s.signal_id }}</span></div>
                    <div class="kv"><span class="k">来源 IP</span><span class="v" style="font-size: 12px">{{ s.source_ip || '—' }}</span></div>
                    <div class="kv"><span class="k">分发模式</span><span class="v">{{ s.dispatch_mode || '—' }}</span></div>
                    <div class="kv"><span class="k">信号整体状态</span><span class="v">{{ s.signal_status || '—' }}</span></div>
                    <div class="kv"><span class="k">备注</span><span class="v" style="font-size: 12px">{{ s.comment || '—' }}</span></div>
                  </div>
                  <div style="border-top: 1px solid var(--border); padding-top: 10px">
                    <div class="muted" style="font-size: 12px; margin-bottom: 8px">本节点处理</div>
                    <div class="kv-grid">
                      <div class="kv"><span class="k">决策手数</span><span class="v">{{ s.decided_vol ?? '—' }}</span></div>
                      <div class="kv"><span class="k">闸门</span><span class="v">{{ s.gate_result === 'skipped' ? '被过滤' : '通过' }}</span></div>
                      <div class="kv"><span class="k">跳过原因</span><span class="v" style="font-size: 12px">{{ s.skip_reason || '—' }}</span></div>
                      <div class="kv"><span class="k">执行状态</span><span class="v"><span class="tag" :class="feedTag(s.status).cls">{{ feedTag(s.status).text }}</span></span></div>
                      <div class="kv"><span class="k">返回码</span><span class="v">{{ s.retcode ?? '—' }}</span></div>
                      <div class="kv"><span class="k">订单号</span><span class="v">{{ s.order ?? '—' }}</span></div>
                      <div class="kv"><span class="k">成交号</span><span class="v">{{ s.deal ?? '—' }}</span></div>
                      <div class="kv"><span class="k">错误</span><span class="v" style="font-size: 12px">{{ s.error || '—' }}</span></div>
                      <div class="kv"><span class="k">下发时间</span><span class="v" style="font-size: 12px">{{ fmtTime((s.dispatched_at || 0) * 1000) }}</span></div>
                      <div class="kv"><span class="k">完成时间</span><span class="v" style="font-size: 12px">{{ fmtTime((s.finished_at || 0) * 1000) }}</span></div>
                    </div>
                  </div>
                  <div v-if="s.raw_payload" style="margin-top: 10px">
                    <div class="muted" style="font-size: 12px; margin-bottom: 6px">原始信号</div>
                    <div class="token-box" style="font-size: 12px">{{ s.raw_payload }}</div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-else class="muted" style="font-size: 13px; padding: 8px 0">暂无信号记录。</div>
        </div>

        <div v-if="historyTotal > 0" class="pagination">
          <span class="muted pagination-info">
            共 {{ historyTotal }} 条 · 第 {{ historyPage }} / {{ historyTotalPages }} 页
          </span>
          <div class="row pagination-actions">
            <select v-model.number="historyPageSize" class="pagination-size" @change="onHistoryPageSizeChange">
              <option :value="10">10 条/页</option>
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
            </select>
            <button class="btn-sm btn-ghost" :disabled="loadingHistory || historyPage <= 1" @click="goHistoryPage(historyPage - 1)">
              上一页
            </button>
            <button
              class="btn-sm btn-ghost"
              :disabled="loadingHistory || historyPage >= historyTotalPages"
              @click="goHistoryPage(historyPage + 1)"
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      <!-- 成交回报 -->
      <div v-else-if="tab === 'feed'">
        <div class="row between mobile-only" style="margin-bottom: 8px">
          <span class="muted" style="font-size: 12px">实时回报 + 历史明细</span>
          <button class="btn-sm btn-ghost" :disabled="loadingHistory" @click="loadHistory">
            {{ loadingHistory ? '刷新中…' : '刷新历史' }}
          </button>
        </div>
        <div v-if="mergedFeed.length" class="list-cards mobile-only">
          <div
            v-for="row in mergedFeed"
            :key="row.signal_id"
            class="list-card card clickable"
            @click="toggleRow(row.signal_id)"
          >
            <div class="list-card-head row between">
              <strong>{{ row.symbol || '—' }}</strong>
              <span class="muted">{{ isExpanded(row.signal_id) ? '▾' : '▸' }}</span>
            </div>
            <div class="list-field"><span class="k">时间</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ fmtTime(row.ts) }}</span></div>
            <div class="list-field">
              <span class="k">方向</span>
              <span class="v">
                <span v-if="row.action" class="tag" :class="row.action === 'BUY' ? 'green' : 'blue'">{{ row.action }}</span>
                <span v-else class="muted">—</span>
              </span>
            </div>
            <div class="list-field"><span class="k">手数</span><span class="v">{{ row.volume ?? '—' }}</span></div>
            <div class="list-field"><span class="k">价位</span><span class="v">{{ row.price ?? '—' }}</span></div>
            <div class="list-field"><span class="k">SL</span><span class="v">{{ row.sl ?? '—' }}</span></div>
            <div class="list-field"><span class="k">TP</span><span class="v">{{ row.tp ?? '—' }}</span></div>
            <div class="list-field">
              <span class="k">状态</span>
              <span class="v"><span class="tag" :class="feedTag(row.status).cls">{{ feedTag(row.status).text }}</span></span>
            </div>
            <div class="list-field"><span class="k">详情</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ feedDetail(row) || '—' }}</span></div>
            <div v-if="isExpanded(row.signal_id)" class="list-card-detail">
              <div v-if="row.raw_payload" class="list-field">
                <span class="k">信号原始数据</span>
                <span class="v"><div class="token-box" style="font-size: 12px; font-weight: 400">{{ row.raw_payload }}</div></span>
              </div>
              <div v-else class="list-field"><span class="k">信号原始数据</span><span class="v muted">—</span></div>
            </div>
          </div>
        </div>
        <div v-else class="card card-pad muted mobile-only" style="font-size: 13px">暂无分发/成交记录。</div>

        <div class="card card-pad table-scroll desktop-only">
        <div class="row between" style="margin-bottom: 8px">
          <span class="muted" style="font-size: 12px">实时回报 + 历史明细（点击行展开信号原始数据）</span>
          <button class="btn-sm btn-ghost" :disabled="loadingHistory" @click="loadHistory">
            {{ loadingHistory ? '刷新中…' : '刷新历史' }}
          </button>
        </div>
        <table v-if="mergedFeed.length">
          <thead>
            <tr>
              <th style="width: 22px"></th>
              <th>时间</th><th>品种</th><th>方向</th>
              <th class="right">手数</th><th class="right">价位</th><th class="right">SL</th><th class="right">TP</th>
              <th>状态</th><th>详情</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in mergedFeed" :key="row.signal_id">
              <tr class="clickable" @click="toggleRow(row.signal_id)">
                <td class="muted">{{ isExpanded(row.signal_id) ? '▾' : '▸' }}</td>
                <td class="muted" style="font-size: 12px">{{ fmtTime(row.ts) }}</td>
                <td>{{ row.symbol || '—' }}</td>
                <td>
                  <span v-if="row.action" class="tag" :class="row.action === 'BUY' ? 'green' : 'blue'">{{ row.action }}</span>
                  <span v-else class="muted">—</span>
                </td>
                <td class="right">{{ row.volume ?? '—' }}</td>
                <td class="right">{{ row.price ?? '—' }}</td>
                <td class="right">{{ row.sl ?? '—' }}</td>
                <td class="right">{{ row.tp ?? '—' }}</td>
                <td><span class="tag" :class="feedTag(row.status).cls">{{ feedTag(row.status).text }}</span></td>
                <td class="muted" style="font-size: 12px">{{ feedDetail(row) }}</td>
              </tr>
              <tr v-if="isExpanded(row.signal_id)" class="detail-row">
                <td></td>
                <td colspan="9">
                  <div class="muted" style="font-size: 12px; margin-bottom: 6px">信号原始数据</div>
                  <div v-if="row.raw_payload" class="token-box" style="font-size: 12px">{{ row.raw_payload }}</div>
                  <div v-else class="muted" style="font-size: 13px">—</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-else class="muted" style="font-size: 13px; padding: 8px 0">暂无分发/成交记录。</div>
        </div>

        <div v-if="historyTotal > 0" class="pagination">
          <span class="muted pagination-info">
            共 {{ historyTotal }} 条 · 第 {{ historyPage }} / {{ historyTotalPages }} 页
            <span v-if="historyPage === 1"> · 含实时回报</span>
          </span>
          <div class="row pagination-actions">
            <select v-model.number="historyPageSize" class="pagination-size" @change="onHistoryPageSizeChange">
              <option :value="10">10 条/页</option>
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
            </select>
            <button class="btn-sm btn-ghost" :disabled="loadingHistory || historyPage <= 1" @click="goHistoryPage(historyPage - 1)">
              上一页
            </button>
            <button
              class="btn-sm btn-ghost"
              :disabled="loadingHistory || historyPage >= historyTotalPages"
              @click="goHistoryPage(historyPage + 1)"
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </template>

    <!-- token modal -->
    <div v-if="tokenModal.show" class="modal-mask" @click.self="tokenModal.show = false">
      <div class="card card-pad modal">
        <div class="h1">{{ tokenModal.title }}</div>
        <p class="muted" style="font-size: 12px">将此令牌填入对应节点的 <code>.env</code> 的 <code>NODE_TOKEN</code>。</p>
        <div class="token-box">{{ tokenModal.token }}</div>
        <div class="row between" style="margin-top: 14px">
          <button class="btn-ghost" @click="copyToken">复制</button>
          <button class="btn-primary" @click="tokenModal.show = false">我已保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
