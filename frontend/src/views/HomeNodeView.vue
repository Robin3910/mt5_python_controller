<script setup lang="ts">
// 首页节点主页：账户概览 + AI 交易 / 带单两个频道的持仓与历史。
// 归类由后端按策略魔术号完成；实时持仓仍读账户快照。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import api from '@/api/client'
import type {
  AccountSnapshot,
  CancelPendingResult,
  FollowChannelsOut,
  FollowClosedPosition,
  FollowMagicChannel,
  NodeOut,
  PendingOrder,
  Position,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { confirmAction } from '@/utils/confirm'
import { setNodePrimary } from '@/utils/nodePrimary'

const route = useRoute()
const router = useRouter()
const hub = useHubStore()
const auth = useAuthStore()

const id = computed(() => String(route.params.id || ''))
const node = computed<NodeOut | undefined>(() => hub.nodes.find((n) => n.node_id === id.value))
const account = computed<AccountSnapshot | undefined>(() => hub.accounts[id.value])
const online = computed(() => (hub.statuses[id.value] || node.value?.status) === 'online')
const loadedNodes = ref(false)
const channels = ref<FollowChannelsOut | null>(null)
const channelError = ref('')
const busy = ref('')

type Market = 'ai' | 'follow'
type SubTab = 'holding' | 'history'
const market = ref<Market>('ai')
const sub = ref<SubTab>('holding')

const positions = computed<Position[]>(() => account.value?.positions || [])
const orders = computed<PendingOrder[]>(() => account.value?.orders || [])

const magicMap = computed(() => {
  const map = new Map<number, FollowMagicChannel>()
  for (const row of channels.value?.magics || []) map.set(row.magic, row)
  return map
})

function inMarket(magic: number, which: Market): boolean {
  return magicMap.value.get(magic)?.market === which
}

const aiPositions = computed(() => positions.value.filter((p) => inMarket(p.magic, 'ai')))
const followPositions = computed(() => positions.value.filter((p) => inMarket(p.magic, 'follow')))
const aiOrders = computed(() => orders.value.filter((o) => inMarket(o.magic, 'ai')))
const followOrders = computed(() => orders.value.filter((o) => inMarket(o.magic, 'follow')))

const holding = computed(() => (market.value === 'ai' ? aiPositions.value : followPositions.value))
const pending = computed(() => (market.value === 'ai' ? aiOrders.value : followOrders.value))
const history = computed<FollowClosedPosition[]>(() => {
  if (!channels.value) return []
  return market.value === 'ai' ? channels.value.ai_history : channels.value.follow_history
})

const posCount = computed(() => positions.value.length)
const posVolume = computed(() => positions.value.reduce((a, p) => a + (p.volume || 0), 0))
const posProfit = computed(() => positions.value.reduce((a, p) => a + (p.profit || 0), 0))

function sideOf(list: Position[]) {
  let buy = 0
  let sell = 0
  let volume = 0
  let profit = 0
  for (const p of list) {
    if (isBuy(p.type)) buy += 1
    else sell += 1
    volume += p.volume || 0
    profit += p.profit || 0
  }
  return { buy, sell, volume, profit, count: list.length }
}
const holdingSide = computed(() => sideOf(holding.value))
const historyVolume = computed(() => history.value.reduce((a, r) => a + (r.volume || 0), 0))
const historyProfit = computed(() => history.value.reduce((a, r) => a + (r.profit || 0), 0))

const magicKey = computed(() => {
  const pos = positions.value.map((p) => p.magic).join(',')
  const ord = orders.value.map((o) => o.magic).join(',')
  return `${pos}|${ord}`
})

function isBuy(type: string | null | undefined): boolean {
  return (type || '').toLowerCase() === 'buy'
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
function fmtPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return '—'
  const abs = Math.abs(n)
  const digits = abs >= 100 ? 2 : abs >= 10 ? 3 : 5
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
function fmtClock(ts: number | null | undefined): string {
  if (!ts || !Number.isFinite(ts)) return '—'
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (v: number) => String(v).padStart(2, '0')
  return `${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

async function loadChannels(): Promise<void> {
  if (!id.value) return
  try {
    const { data } = await api.get<FollowChannelsOut>(`/api/nodes/${id.value}/follow-channels`)
    channels.value = data
    channelError.value = ''
  } catch (e) {
    channelError.value = errText(e, '频道持仓加载失败')
  }
}

async function boot(): Promise<void> {
  if (!hub.nodes.length) {
    try { await hub.fetchNodes() } catch { /* 列表失败时下面给出空态 */ }
  }
  loadedNodes.value = true
  await loadChannels()
}

onMounted(boot)
watch(id, () => { void boot() })
watch(magicKey, () => { void loadChannels() })
watch(() => hub.dealVersions[id.value] || 0, () => { void loadChannels() })

function offlineReason(): string {
  return online.value ? '' : '节点离线，无法操作'
}

async function makePrimary(): Promise<void> {
  if (!node.value || node.value.is_primary || busy.value) return
  await setNodePrimary({
    node: node.value,
    nodes: hub.nodes,
    isAdmin: auth.isAdmin,
    apply: (nodeId, next) => hub.setNodePrimary(nodeId, next),
  })
}

async function closeAllAccount(): Promise<void> {
  if (!node.value || busy.value) return
  const why = offlineReason()
  if (why) { ElMessage.warning(why); return }
  if (!positions.value.length) { ElMessage.info('当前没有持仓'); return }
  const ok = await confirmAction(
    `确认平掉节点「${node.value.name}」的全部持仓？\n将同时终止该节点上所有进行中的策略任务。`,
    '确认平仓',
  )
  if (!ok) return
  busy.value = 'close-all'
  try {
    await hub.closeNode(id.value, { target: 'all' })
    ElMessage.success('已提交全部平仓')
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally {
    busy.value = ''
  }
}

async function closeProfitAccount(): Promise<void> {
  if (!node.value || busy.value) return
  const why = offlineReason()
  if (why) { ElMessage.warning(why); return }
  const winners = positions.value.filter((p) => (p.profit || 0) > 0)
  if (!winners.length) { ElMessage.info('当前没有盈利持仓'); return }
  const ok = await confirmAction(
    `确认平掉节点「${node.value.name}」当前浮动盈利的 ${winners.length} 笔持仓？\n` +
      '只平盈利单，亏损单保留。不会终止策略任务，策略仍在运行时可能再次开仓。',
    '确认平盈',
  )
  if (!ok) return
  busy.value = 'close-profit'
  let sent = 0
  let failed = 0
  for (const p of winners) {
    try {
      await hub.closeNode(id.value, { target: 'ticket', ticket: p.ticket })
      sent += 1
    } catch {
      failed += 1
    }
  }
  busy.value = ''
  if (failed && !sent) ElMessage.error('平盈失败')
  else if (failed) ElMessage.warning(`已提交 ${sent} 笔，${failed} 笔失败`)
  else ElMessage.success(`已提交平掉 ${sent} 笔盈利持仓`)
}

function hasLimitWatch(): boolean {
  return hub.groups.some(
    (g) => g.enabled && g.limit_watch_enabled
      && g.nodes.some((m) => m.node_id === id.value && m.limit_watch_report !== false),
  )
}

function reportCancel(res: CancelPendingResult): void {
  if (res.status === 'error') {
    ElMessage.error(res.error || '节点未确认撤单')
    return
  }
  const extra: string[] = []
  if (res.failed?.length) {
    extra.push(`失败明细：${res.failed.map((f) => `${f.symbol || '-'}${f.ticket} ${f.error}`).join('；')}`)
  }
  if (res.status === 'regenerated' || (res.remaining ?? 0) > 0) {
    extra.push('撤单后系统又生成了挂单，可能是限价监听自动触发，请检查分组监听开关')
  }
  const tail = extra.length ? `\n${extra.join('\n')}` : ''
  if (res.failed?.length) ElMessage.warning(`删除 ${res.cancelled} 张挂单，${res.failed.length} 张失败${tail}`)
  else ElMessage.success(`已删除 ${res.cancelled} 张挂单${tail}`)
}

async function cancelOrders(scope: 'all' | 'strategy', list: PendingOrder[]): Promise<void> {
  if (!node.value || busy.value) return
  const why = offlineReason()
  if (why) { ElMessage.warning(why); return }
  if (!list.length) { ElMessage.info('没有挂单'); return }
  const lines = list
    .slice(0, 20)
    .map((o) => `• ${o.symbol}  ${o.type}  ${o.volume}手  @${o.price_open}`)
    .join('\n')
  const more = list.length > 20 ? `\n…（共 ${list.length} 条）` : ''
  const stopHint = scope === 'all'
    ? '· 将同时停止该节点上所有在跑策略任务（否则策略会自动补单）。'
    : '· 只停止本频道这些挂单对应的策略任务，另一个频道不受影响。'
  const hints = ['· 只删除挂单，已有持仓不受影响。', stopHint]
  if (hasLimitWatch()) hints.push('· 该节点属于开启限价监听的分组，删除后这些手工限价单不会再触发策略。')
  try {
    await ElMessageBox.confirm(
      `确认删除「${node.value.name}」的${scope === 'all' ? '全部' : '本频道'}挂单？\n\n` +
        `共 ${list.length} 张：\n${lines}${more}\n\n${hints.join('\n')}`,
      '删除挂单',
      {
        type: 'warning',
        confirmButtonText: `确认删除 ${list.length} 张挂单`,
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  busy.value = 'cancel'
  try {
    const magics = scope === 'strategy' ? [...new Set(list.map((o) => o.magic))] : undefined
    const res = await hub.cancelPendingAll(id.value, { scope, magics })
    reportCancel(res)
  } catch (e) {
    ElMessage.error(errText(e, '删挂单失败，请稍后重试'))
  } finally {
    busy.value = ''
  }
}

async function closeChannel(mode: 'all' | 'profit' | 'ticket', ticket?: number): Promise<void> {
  if (!node.value || busy.value) return
  const why = offlineReason()
  if (why) { ElMessage.warning(why); return }
  const label = market.value === 'ai' ? 'AI 交易' : '跟单'
  if (mode === 'ticket') {
    const ok = await confirmAction(`确认平掉这张${label}持仓？\n不会终止整个策略任务。`, '确认平仓')
    if (!ok) return
  } else if (mode === 'profit') {
    const n = holding.value.filter((p) => (p.profit || 0) > 0).length
    if (!n) { ElMessage.info('本频道没有盈利持仓'); return }
    const ok = await confirmAction(
      `确认平掉${label}频道当前浮动盈利的 ${n} 笔持仓？\n不会终止策略任务，策略仍在运行时可能再次开仓。`,
      '确认平盈',
    )
    if (!ok) return
  } else {
    if (!holding.value.length) { ElMessage.info('本频道没有持仓'); return }
    const ok = await confirmAction(
      `确认平掉${label}频道的全部持仓？\n将终止本频道仍在运行的策略任务。另一个频道不受影响。`,
      '确认平仓',
    )
    if (!ok) return
  }
  busy.value = `ch-${mode}`
  try {
    const { data } = await api.post(`/api/nodes/${id.value}/follow-channels/close`, {
      market: market.value,
      mode,
      ticket: ticket ?? null,
    })
    if (data.status === 'partial') ElMessage.warning(`已提交，有 ${data.failed?.length || 0} 笔未确认`)
    else ElMessage.success('已提交平仓')
    await loadChannels()
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="hn">
    <button v-if="auth.isAdmin" type="button" class="hn-admin-back" @click="router.push({ name: 'home' })">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
      返回首页
    </button>

    <p v-if="loadedNodes && !node" class="hn-missing">找不到这个节点，或它不属于当前账号。</p>

    <template v-else-if="node">
      <section class="hn-hero">
        <div class="hn-glow hn-glow-a" aria-hidden="true"></div>
        <div class="hn-glow hn-glow-b" aria-hidden="true"></div>
        <div class="hn-hero-top">
          <div class="hn-id">
            <span class="hn-wallet" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M21 7.28V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-2.28A2 2 0 0 0 22 15V9a2 2 0 0 0-1-1.72zM20 9v6h-7V9h7zM5 19V5h14v2h-6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h6v2H5z"/><circle cx="16" cy="12" r="1.2" fill="currentColor"/></svg>
              <i :class="online ? 'is-on' : 'is-off'"></i>
            </span>
            <div class="hn-id-text">
              <div class="hn-title">
                <strong>{{ node.node_id }}</strong>
                <em>{{ node.name }}</em>
              </div>
              <p>
                <i class="hn-dot" :class="online ? 'is-on' : 'is-off'"></i>
                {{ online ? '在线' : '离线' }}
                <span>|</span>
                节点主页 · 账户概览与跟单持仓
              </p>
            </div>
          </div>
          <div class="hn-actions">
            <span v-if="node.is_primary" class="hn-badge">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2 9.2 8.6 2 9.2l5.2 4.6L5.8 21 12 17.3 18.2 21l-1.4-7.2L22 9.2l-7.2-.6L12 2z"/></svg>
              主节点
            </span>
            <button v-else type="button" class="hn-btn hn-btn-primary" :disabled="!!busy" @click="makePrimary">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2 9.2 8.6 2 9.2l5.2 4.6L5.8 21 12 17.3 18.2 21l-1.4-7.2L22 9.2l-7.2-.6L12 2z"/></svg>
              设为主节点
            </button>
            <button type="button" class="hn-btn hn-btn-amber" :disabled="!!busy || !online || !orders.length" @click="cancelOrders('all', orders)">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
              删挂单
            </button>
            <button type="button" class="hn-btn hn-btn-green" :disabled="!!busy || !online || !positions.some((p) => (p.profit || 0) > 0)" @click="closeProfitAccount">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6h-6z"/></svg>
              平盈
            </button>
            <button type="button" class="hn-btn hn-btn-blue" :disabled="!!busy || !online || !positions.length" @click="closeAllAccount">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>
              平仓
            </button>
          </div>
        </div>

        <div class="hn-stats">
          <div class="hn-stat">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M21 18v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v1h-9a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h9zm-9-2h10V8H12v8zm4-2.5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>余额</p>
            <strong>{{ fmtMoney(account?.balance) }}</strong>
          </div>
          <div class="hn-stat">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4zm2 2H5V5h14v14zm0-16H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/></svg>净值</p>
            <strong>{{ fmtMoney(account?.equity) }}</strong>
          </div>
          <div class="hn-stat">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M18 8h-1V6A5 5 0 0 0 7 6v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2zM9 6a3 3 0 0 1 6 0v2H9V6zm9 14H6V10h12v10z"/></svg>占用保证金</p>
            <strong>{{ fmtMoney(account?.margin) }}</strong>
          </div>
          <div class="hn-stat">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 3H5a2 2 0 0 0-2 2v14l4-4h12a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zm0 10H6.17L5 14.17V5h14v8z"/></svg>持仓笔数</p>
            <strong>{{ posCount }} 笔</strong>
          </div>
          <div class="hn-stat">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 17h2v-7H3v7zm4 0h2V7H7v10zm4 0h2v-4h-2v4zm4 0h2V4h-2v13zm4 0h2v-7h-2v7z"/></svg>合计手数</p>
            <strong>{{ fmtVol(posVolume) }}</strong>
          </div>
          <div class="hn-stat is-pnl">
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3.5 18.5 9 13l4 4 6.5-6.5L22 13V3h-6l2.5 2.5L13 11l-4-4-6.5 6.5L3.5 18.5z"/></svg>合计盈亏</p>
            <strong :class="posProfit >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(posProfit) }}</strong>
          </div>
        </div>
      </section>

      <section class="hn-channel">
        <header class="hn-channel-head">
          <p>
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>
            频道跟单
          </p>
          <span>AI 交易与带单信号自动同步持仓</span>
        </header>
        <div class="hn-channel-body">
          <div class="hn-tabs" role="tablist">
            <button type="button" role="tab" :class="{ on: market === 'ai' }" :aria-selected="market === 'ai'" @click="market = 'ai'">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm6 4H9v-2h6v2zm0-5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>
              <span>AI交易持仓</span>
              <em>{{ aiPositions.length }}</em>
            </button>
            <button type="button" role="tab" :class="{ on: market === 'follow' }" :aria-selected="market === 'follow'" @click="market = 'follow'">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 11a3 3 0 1 0-3-3 3 3 0 0 0 3 3zM8 11a3 3 0 1 0-3-3 3 3 0 0 0 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
              <span>跟单持仓</span>
              <em>{{ followPositions.length }}</em>
            </button>
          </div>

          <div class="hn-sub">
            <div class="hn-sub-tabs">
              <button type="button" :class="{ on: sub === 'holding' }" @click="sub = 'holding'">
                <svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 3H5a2 2 0 0 0-2 2v14l4-4h12a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/></svg>
                持仓明细
                <em>{{ holding.length }}</em>
              </button>
              <button type="button" :class="{ on: sub === 'history' }" @click="sub = 'history'">
                <svg viewBox="0 0 24 24"><path fill="currentColor" d="M13 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7V3zm-1 5h2v6l4 2-.9 1.8L11 15V8z"/></svg>
                历史持仓
                <em>{{ history.length }}</em>
              </button>
            </div>
            <p class="hn-hint">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M11 17h2v-6h-2v6zm1-8a1.25 1.25 0 1 0 0-2.5A1.25 1.25 0 0 0 12 9zm0-7a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"/></svg>
              <span>跟随机器人信号自动同步的持仓明细与历史结算</span>
            </p>
          </div>

          <p v-if="channelError" class="hn-empty">{{ channelError }}</p>

          <div v-else-if="sub === 'holding'" class="hn-table">
            <div class="hn-row hn-head hn-hold-grid">
              <span>品种</span><span>方向</span><span>手数</span><span>开仓价</span>
              <span>止盈 / 止损</span><span>浮动盈亏</span><span>操作</span>
            </div>
            <p v-if="!holding.length" class="hn-empty">本频道暂无持仓</p>
            <div v-for="p in holding" :key="p.ticket" class="hn-line">
              <div class="hn-mobile">
                <div class="hn-mobile-top">
                  <b>{{ p.symbol }}</b>
                  <i class="hn-dir" :class="isBuy(p.type) ? 'is-buy' : 'is-sell'">{{ isBuy(p.type) ? '多' : '空' }}</i>
                  <strong :class="(p.profit || 0) >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(p.profit || 0) }}</strong>
                </div>
                <p>手数 <span>{{ fmtVol(p.volume || 0) }}</span> 开仓价 <span>{{ fmtPx(p.price_open) }}</span> 开仓时间 <span>{{ fmtClock(p.time) }}</span></p>
                <div class="hn-mobile-bot">
                  <span>止盈 <em class="is-up">{{ fmtPx(p.tp) }}</em> / 止损 <em class="is-down">{{ fmtPx(p.sl) }}</em></span>
                  <button type="button" class="hn-close" :disabled="!!busy || !online" @click="closeChannel('ticket', p.ticket)">平仓</button>
                </div>
              </div>
              <div class="hn-row hn-hold-grid hn-desk">
                <span class="muted">{{ p.symbol }}</span>
                <span><i class="hn-dir" :class="isBuy(p.type) ? 'is-buy' : 'is-sell'">{{ isBuy(p.type) ? '多' : '空' }}</i></span>
                <span class="muted num">{{ fmtVol(p.volume || 0) }}</span>
                <span class="muted num">{{ fmtPx(p.price_open) }}</span>
                <span class="sltp">
                  <em class="is-up">止盈 {{ fmtPx(p.tp) }}</em>
                  <em class="is-down">止损 {{ fmtPx(p.sl) }}</em>
                </span>
                <strong :class="(p.profit || 0) >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(p.profit || 0) }}</strong>
                <span><button type="button" class="hn-close" :disabled="!!busy || !online" @click="closeChannel('ticket', p.ticket)">平仓</button></span>
              </div>
            </div>
            <div v-if="holding.length" class="hn-foot">
              <span>
                多 <b class="is-up">{{ holdingSide.buy }}</b>
                <i>/</i>
                空 <b class="is-down">{{ holdingSide.sell }}</b>
                · {{ holdingSide.count }} 笔 · <em>{{ fmtVol(holdingSide.volume) }} 手</em>
              </span>
              <strong :class="holdingSide.profit >= 0 ? 'is-up' : 'is-down'">合计 {{ fmtSigned(holdingSide.profit) }}</strong>
            </div>
            <div v-if="holding.length" class="hn-foot-actions">
              <button type="button" class="hn-chip amber" :disabled="!!busy || !online || !pending.length" @click="cancelOrders('strategy', pending)">删挂单</button>
              <button type="button" class="hn-chip green" :disabled="!!busy || !online || !holding.some((p) => (p.profit || 0) > 0)" @click="closeChannel('profit')">平盈</button>
              <button type="button" class="hn-chip red" :disabled="!!busy || !online || !holding.length" @click="closeChannel('all')">平仓</button>
            </div>
          </div>

          <div v-else class="hn-table">
            <div class="hn-row hn-head hn-hist-grid">
              <span>品种</span><span>方向</span><span>手数</span><span>开仓 → 平仓价</span>
              <span>平仓时间</span><span>状态</span><span>已结算盈亏</span>
            </div>
            <p v-if="!history.length" class="hn-empty">本频道暂无历史持仓</p>
            <div v-for="r in history" :key="r.position_id" class="hn-line">
              <div class="hn-mobile">
                <div class="hn-mobile-top">
                  <b>{{ r.symbol || '—' }}</b>
                  <i class="hn-dir" :class="isBuy(r.direction) ? 'is-buy' : 'is-sell'">{{ isBuy(r.direction) ? '多' : '空' }}</i>
                  <i class="hn-state" :class="r.status === 'sl' ? 'is-sl' : r.status === 'tp' ? 'is-tp' : 'is-flat'">{{ r.status_label }}</i>
                  <strong :class="r.profit >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(r.profit) }}</strong>
                </div>
                <p>
                  手数 <span>{{ fmtVol(r.volume) }}</span>
                  开仓价 <span>{{ fmtPx(r.price_open) }}</span>
                  平仓价 <span>{{ fmtPx(r.price_close) }}</span>
                  平仓时间 <span>{{ fmtClock(r.closed_at) }}</span>
                </p>
              </div>
              <div class="hn-row hn-hist-grid hn-desk">
                <span class="muted">{{ r.symbol || '—' }}</span>
                <span><i class="hn-dir" :class="isBuy(r.direction) ? 'is-buy' : 'is-sell'">{{ isBuy(r.direction) ? '多' : '空' }}</i></span>
                <span class="muted num">{{ fmtVol(r.volume) }}</span>
                <span class="sltp">
                  <em>{{ fmtPx(r.price_open) }}</em>
                  <em class="dim">→ {{ fmtPx(r.price_close) }}</em>
                </span>
                <span class="dim num">{{ fmtClock(r.closed_at) }}</span>
                <span><i class="hn-state" :class="r.status === 'sl' ? 'is-sl' : r.status === 'tp' ? 'is-tp' : 'is-flat'">{{ r.status_label }}</i></span>
                <strong :class="r.profit >= 0 ? 'is-up' : 'is-down'">{{ fmtSigned(r.profit) }}</strong>
              </div>
            </div>
            <div v-if="history.length" class="hn-foot">
              <span>{{ history.length }} 笔 · <em>{{ fmtVol(historyVolume) }} 手</em></span>
              <strong :class="historyProfit >= 0 ? 'is-up' : 'is-down'">已结算 {{ fmtSigned(historyProfit) }}</strong>
            </div>
          </div>
        </div>
      </section>

      <p class="hn-note">频道列表只含 AI 交易与带单策略单。手工单和普通信号单计入上方账户合计，不在此列出。</p>
    </template>
  </div>
</template>

<style scoped>
.hn { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.hn-admin-back {
  display: inline-flex; align-items: center; gap: 4px; align-self: flex-start;
  border: 0; background: transparent; color: #E6EBF4; font-size: 14px; font-weight: 600; padding: 0;
}
.hn-admin-back svg { width: 18px; height: 18px; }
.hn-missing, .hn-empty, .hn-note { margin: 0; color: #5B6B85; font-size: 12px; }
.hn-note { text-align: center; color: #4A5568; font-size: 10px; }
.hn-empty { padding: 16px 12px; text-align: center; }
.hn-hero, .hn-channel {
  position: relative; overflow: hidden; border: 1px solid #1C2740; border-radius: 16px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}
.hn-hero { background: #0D1526; }
.hn-channel { background: #101828; box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35); }
.hn-glow { position: absolute; border-radius: 999px; pointer-events: none; filter: blur(36px); }
.hn-glow-a { width: 192px; height: 192px; right: -64px; top: -80px; background: rgba(37, 99, 235, 0.1); }
.hn-glow-b { width: 160px; height: 160px; left: 25%; bottom: -80px; background: rgba(245, 158, 11, 0.06); }
.hn-hero-top, .hn-id, .hn-title, .hn-actions, .hn-stat p, .hn-channel-head, .hn-tabs button, .hn-sub, .hn-sub-tabs button, .hn-foot, .hn-foot-actions, .hn-mobile-top, .hn-mobile-bot {
  display: flex; align-items: center; gap: 8px;
}
.hn-hero-top { position: relative; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 16px 16px 0; }
.hn-id { min-width: 0; gap: 12px; }
.hn-wallet {
  position: relative; width: 44px; height: 44px; flex: 0 0 auto; border-radius: 16px; color: #fff;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #2563EB, #1E3A8A);
  box-shadow: 0 6px 18px rgba(37, 99, 235, 0.45);
}
.hn-wallet svg { width: 22px; height: 22px; }
.hn-wallet i, .hn-dot { border-radius: 999px; background: #5B6B85; }
.hn-wallet i { position: absolute; width: 12px; height: 12px; right: -2px; top: -2px; border: 2px solid #0D1526; }
.hn-wallet i.is-on, .hn-dot.is-on { background: #22C55E; }
.hn-dot { width: 6px; height: 6px; }
.hn-dot.is-on { animation: hn-pulse 1.6s ease-out infinite; }
@keyframes hn-pulse { 50% { opacity: 0.45; } }
.hn-id-text { min-width: 0; }
.hn-title { flex-wrap: wrap; gap: 6px; }
.hn-title strong { color: #F1F5FB; font-size: 16px; letter-spacing: 0.02em; }
.hn-title em {
  font-style: normal; color: #93C5FD; background: rgba(37, 99, 235, 0.15);
  border-radius: 6px; padding: 1px 6px; font-size: 11px; font-weight: 650;
}
.hn-id-text p { margin: 4px 0 0; color: #5B6B85; font-size: 11px; display: flex; align-items: center; gap: 6px; }
.hn-id-text p span { color: #2A3954; }
.hn-actions { flex-wrap: wrap; gap: 6px; }
.hn-badge, .hn-btn, .hn-chip, .hn-close {
  display: inline-flex; align-items: center; gap: 4px; border-radius: 8px; font-weight: 600;
}
.hn-badge, .hn-btn { padding: 6px 12px; font-size: 12px; }
.hn-badge { color: #FBBF24; border: 1px solid rgba(180, 83, 9, 0.6); background: rgba(59, 42, 16, 0.5); }
.hn-btn, .hn-chip, .hn-close, .hn-admin-back { cursor: pointer; }
.hn-btn:disabled, .hn-chip:disabled, .hn-close:disabled { opacity: 0.4; cursor: not-allowed; }
.hn-btn svg, .hn-badge svg { width: 14px; height: 14px; }
.hn-btn-primary { color: #93C5FD; border: 1px solid rgba(37, 99, 235, 0.6); background: rgba(22, 38, 74, 0.5); }
.hn-btn-amber { color: #E2A13C; border: 1px solid rgba(124, 74, 18, 0.6); background: rgba(59, 42, 16, 0.4); }
.hn-btn-green { color: #6EE7A8; border: 1px solid rgba(34, 197, 94, 0.6); background: rgba(14, 42, 28, 0.5); }
.hn-btn-blue { color: #93C5FD; border: 1px solid rgba(29, 78, 216, 0.6); background: rgba(22, 38, 74, 0.5); }
.hn-stats {
  position: relative; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px; padding: 16px;
}
.hn-stat { border: 1px solid #22304A; background: #0C1220; border-radius: 12px; padding: 10px; min-width: 0; }
.hn-stat p { margin: 0; color: #5B6B85; font-size: 10px; gap: 4px; }
.hn-stat svg { width: 11px; height: 11px; color: #3D4C66; }
.hn-stat strong, .num, .sltp em, .hn-foot, .hn-mobile strong, .hn-mobile span {
  font-variant-numeric: tabular-nums;
}
.hn-stat strong { display: block; margin-top: 4px; font-size: 14px; color: #E6EBF4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hn-stat.is-pnl {
  border-color: rgba(37, 99, 235, 0.4);
  background: linear-gradient(135deg, rgba(22, 38, 74, 0.8), #0C1424);
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.15);
}
.is-up { color: #22C55E; }
.is-down { color: #F6465D; }
.hn-channel-head { justify-content: space-between; gap: 8px; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid #16213A; background: #0C1424; }
.hn-channel-head p { margin: 0; display: flex; align-items: center; gap: 6px; color: #E6EBF4; font-size: 12px; font-weight: 650; }
.hn-channel-head svg { width: 15px; height: 15px; color: #2563EB; }
.hn-channel-head span, .hn-hint { color: #5B6B85; font-size: 10px; }
.hn-channel-body { padding: 12px; }
.hn-tabs { display: flex; border: 1px solid #22304A; background: #0C1220; border-radius: 12px; padding: 4px; gap: 4px; }
.hn-tabs button {
  flex: 1; min-width: 0; justify-content: center; border: 0; background: transparent; color: #8B96A8;
  border-radius: 8px; padding: 8px; font-size: 12px; font-weight: 650;
}
.hn-tabs button svg { width: 15px; height: 15px; color: #5B6B85; flex: 0 0 auto; }
.hn-tabs button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hn-tabs button em, .hn-sub-tabs em {
  font-style: normal; border-radius: 6px; padding: 1px 6px; font-size: 10px; font-weight: 700; background: #16213A; color: #5B6B85;
}
.hn-tabs button.on {
  color: #fff; background: linear-gradient(90deg, #2563EB, #1D4ED8); box-shadow: 0 2px 10px rgba(37, 99, 235, 0.35);
}
.hn-tabs button.on svg, .hn-tabs button.on em { color: #fff; background: transparent; }
.hn-tabs button.on em { background: rgba(255, 255, 255, 0.2); }
.hn-sub { margin-top: 12px; gap: 8px; align-items: center; }
.hn-sub-tabs { display: flex; gap: 2px; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 2px; flex: 0 0 auto; }
.hn-sub-tabs button {
  border: 0; background: transparent; color: #8B96A8; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-weight: 650;
}
.hn-sub-tabs button svg { width: 13px; height: 13px; }
.hn-sub-tabs button.on { color: #93C5FD; background: rgba(37, 99, 235, 0.2); box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.5); }
.hn-sub-tabs button.on em { color: #93C5FD; background: transparent; }
.hn-hint { display: flex; align-items: center; gap: 4px; margin: 0; min-width: 0; flex: 1; }
.hn-hint svg { width: 12px; height: 12px; color: #3D4C66; flex: 0 0 auto; }
.hn-hint span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hn-table { margin-top: 8px; overflow: hidden; border: 1px solid #1E2B47; border-radius: 8px; background: #0A1120; }
.hn-row { display: none; }
.hn-hold-grid { grid-template-columns: minmax(60px, 0.8fr) 30px 40px minmax(64px, 0.85fr) minmax(92px, 1.1fr) minmax(60px, 0.8fr) 50px; }
.hn-hist-grid { grid-template-columns: minmax(64px, 0.9fr) 30px 40px minmax(112px, 1.2fr) minmax(70px, 0.9fr) 44px minmax(68px, 0.9fr); }
.hn-head { color: #5B6B85; background: #0C1424; border-bottom: 1px solid #16213A; font-size: 10px; font-weight: 550; }
.hn-line { border-bottom: 1px dashed #1B2A48; }
.hn-line:last-of-type { border-bottom: 0; }
.hn-line:hover { background: rgba(15, 27, 48, 0.7); }
.hn-mobile { padding: 8px 12px; }
.hn-mobile-top b { color: #E6EBF4; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hn-mobile-top strong { margin-left: auto; font-size: 11px; }
.hn-mobile p { margin: 4px 0 0; color: #5B6B85; font-size: 10px; display: flex; flex-wrap: wrap; gap: 4px 12px; }
.hn-mobile p span, .muted { color: #8B96A8; }
.hn-mobile-bot { margin-top: 6px; justify-content: space-between; color: #5B6B85; font-size: 10px; }
.hn-dir {
  display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 15px;
  border-radius: 3px; font-style: normal; font-size: 10px; font-weight: 700;
}
.hn-dir.is-buy, .hn-state.is-tp { color: #22C55E; background: rgba(34, 197, 94, 0.15); }
.hn-dir.is-sell, .hn-state.is-sl { color: #F6465D; background: rgba(246, 70, 93, 0.15); }
.hn-state { font-style: normal; border-radius: 4px; padding: 1px 6px; font-size: 9px; font-weight: 650; }
.hn-state.is-flat { color: #8B96A8; background: #16213A; }
.hn-close {
  border: 1px solid rgba(246, 70, 93, 0.4); background: rgba(246, 70, 93, 0.1); color: #F6465D;
  border-radius: 4px; padding: 2px 8px; font-size: 10px;
}
.sltp { text-align: right; line-height: 1.25; }
.sltp em { display: block; font-style: normal; font-size: 10px; }
.dim { color: #5B6B85; }
.hn-foot, .hn-foot-actions { background: #0C1424; border-top: 1px solid #16213A; padding: 4px 10px; font-size: 10px; color: #5B6B85; }
.hn-foot { justify-content: space-between; }
.hn-foot i { color: #2A3954; font-style: normal; margin: 0 4px; }
.hn-foot em { color: #8B96A8; font-style: normal; }
.hn-foot-actions { justify-content: flex-end; background: rgba(12, 20, 36, 0.6); }
.hn-chip { border-radius: 8px; padding: 6px 10px; font-size: 11px; }
.hn-chip.amber { color: #F59E0B; border: 1px solid rgba(245, 158, 11, 0.4); background: rgba(245, 158, 11, 0.1); }
.hn-chip.green { color: #22C55E; border: 1px solid rgba(34, 197, 94, 0.4); background: rgba(34, 197, 94, 0.1); }
.hn-chip.red { color: #F6465D; border: 1px solid rgba(246, 70, 93, 0.4); background: rgba(246, 70, 93, 0.1); }
@media (min-width: 640px) {
  .hn-mobile { display: none; }
  .hn-row { display: grid; align-items: center; gap: 6px; padding: 4px 10px; font-size: 11px; }
  .hn-row > span:nth-child(2), .hn-hist-grid > span:nth-child(6) { text-align: center; }
  .hn-row > span:nth-child(n + 3) { text-align: right; }
  .hn-hold-grid > span:last-child, .hn-row > span:last-child { text-align: right; }
  .hn-desk .hn-dir, .hn-desk .hn-state { margin-left: auto; margin-right: auto; }
  .hn-hold-grid > span:nth-child(2) { display: flex; justify-content: center; }
}
@media (min-width: 1024px) {
  .hn-hero-top, .hn-stats { padding-left: 20px; padding-right: 20px; }
  .hn-stats { grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
  .hn-stat strong { font-size: 16px; }
  .hn-channel-head, .hn-channel-body { padding-left: 16px; padding-right: 16px; }
}
</style>
