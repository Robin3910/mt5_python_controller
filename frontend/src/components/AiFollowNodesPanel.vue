<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AiFollowNode, AiFollowNodeState, AiFollowNodesResponse } from '@/api/types'

const props = defineProps<{ data: AiFollowNodesResponse | null; loading: boolean; error: string; busy: boolean }>()
const emit = defineEmits<{
  state: [nodeIds: string[], state: AiFollowNodeState]
  close: [nodeIds: string[]]
  retry: []
}>()
type Filter = 'all' | 'online' | 'pos' | 'win' | 'loss' | 'off'
const filter = ref<Filter>('all')
const query = ref('')
const stateNames: Record<AiFollowNodeState, string> = { running: '跟单中', paused: '已暂停', stopped: '已停止' }
const filters: { value: Filter; label: string }[] = [
  { value: 'all', label: '全部' }, { value: 'online', label: '在线' },
  { value: 'pos', label: '持仓' }, { value: 'win', label: '浮盈' },
  { value: 'loss', label: '浮亏' }, { value: 'off', label: '离线' },
]
const nodes = computed(() => props.data?.nodes || [])
function matches(node: AiFollowNode, value: Filter): boolean {
  if (value === 'online') return node.follow_state === 'running'
  if (value === 'off') return node.follow_state !== 'running'
  if (value === 'pos') return node.follow_state !== 'stopped' && (node.position_count || 0) > 0
  if (value === 'win') return node.follow_state !== 'stopped' && (node.floating_profit || 0) > 0
  if (value === 'loss') return node.follow_state !== 'stopped' && (node.floating_profit || 0) < 0
  return true
}
const counts = computed(() => Object.fromEntries(filters.map((item) => [item.value, nodes.value.filter((node) => matches(node, item.value)).length])))
const shownNodes = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return nodes.value.filter((node) => matches(node, filter.value) && (!keyword || [
    node.name, node.mt5_login, node.mt5_server, ...node.symbols,
    stateNames[node.follow_state], ...node.groups.map((group) => group.strategy_name),
  ].join(' ').toLowerCase().includes(keyword)))
})
const runningIds = computed(() => nodes.value.filter((node) => node.follow_state === 'running').map((node) => node.node_id))
const activeIds = computed(() => nodes.value.filter((node) => node.follow_state !== 'stopped').map((node) => node.node_id))
const closableIds = computed(() => nodes.value.filter((node) => (node.position_count || 0) > 0).map((node) => node.node_id))

function money(value: number | null | undefined, signed = false): string {
  if (value == null) return '—'
  const amount = Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return value < 0 ? `-${amount}` : `${signed ? '+' : ''}${amount}`
}
</script>

<template>
  <section class="an-panel">
    <header class="an-header">
      <h2>子节点跟单统计<span>{{ shownNodes.length === nodes.length ? nodes.length : `${shownNodes.length}/${nodes.length}` }} 个节点</span></h2>
      <div class="an-batch">
        <button type="button" class="an-pause" title="一键暂停全部运行中节点" :disabled="busy || !runningIds.length" @click="emit('state', runningIds, 'paused')"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 14H8V8h3v8zm5 0h-3V8h3v8z"/></svg>一键暂停</button>
        <button type="button" class="an-close" :disabled="busy || !closableIds.length" @click="emit('close', closableIds)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="m5 3 4 4-2 2-4-4v4H1V1h8v2H5zm14 18-4-4 2-2 4 4v-4h2v8h-8v-2h4zM1 15h2v4l4-4 2 2-4 4h4v2H1v-8zM15 1h8v8h-2V5l-4 4-2-2 4-4h-4V1z"/></svg>一键平仓</button>
        <button type="button" class="an-stop" :disabled="busy || !activeIds.length" @click="emit('state', activeIds, 'stopped')"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm4 14H8V8h8v8z"/></svg>一键停止</button>
      </div>
    </header>
    <p class="an-note">每个子节点独立统计，跟随机器人的开仓 / 平仓信号自动同步执行</p>
    <p class="an-note">暂停与停止只停止新开仓，已有任务和持仓保留；停止后可重新跟单。</p>
    <div class="an-filters">
      <button v-for="item in filters" :key="item.value" type="button" :class="{ on: filter === item.value }" :title="item.value === 'online' ? '跟单中节点' : item.value === 'off' ? '已暂停或已停止节点' : item.label" @click="filter = item.value">{{ item.label }}<span>{{ counts[item.value] || 0 }}</span></button>
    </div>
    <label class="an-search"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14z"/></svg><input v-model="query" placeholder="搜索子节点：名称 / 账户 / 服务器 / 品种 / 状态" /></label>
    <p v-if="error" class="an-error" role="alert">{{ error }}<button type="button" @click="emit('retry')">重试</button></p>
    <p v-if="loading && !data" class="an-empty">加载子节点统计…</p>
    <template v-if="data">
      <div class="an-summary">
        <div><span>跟单节点</span><strong>{{ data.summary.node_count }} 个</strong></div>
        <div><span>持仓手数</span><strong>{{ money(data.summary.volume) }} 手</strong></div>
        <div><span>总投资额</span><strong>{{ money(data.summary.investment_amount) }}<template v-if="data.summary.investment_amount != null"> U</template></strong></div>
        <div><span>总浮动盈亏</span><strong :class="{ up: data.summary.floating_profit != null && data.summary.floating_profit >= 0, down: data.summary.floating_profit != null && data.summary.floating_profit < 0 }">{{ money(data.summary.floating_profit, true) }}</strong></div>
      </div>
      <p class="an-note">盈利节点 <b class="up">{{ data.summary.profitable_node_count ?? '—' }}</b> / {{ data.summary.active_node_count }} · 共持仓 <b>{{ data.summary.position_count ?? '—' }}</b> 笔 · 汇总数据仅统计运行中与已暂停节点</p>
      <p v-if="!shownNodes.length" class="an-empty">{{ nodes.length ? '没有符合条件的节点' : '还没有跟单节点' }}</p>
      <div class="an-grid">
        <article v-for="node in shownNodes" :key="node.node_id" class="an-card" :class="{ stopped: node.follow_state === 'stopped' }">
          <header class="an-card-heading">
            <span class="an-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg></span>
            <div class="an-name"><h3>{{ node.name || node.node_id }}</h3><p :title="node.groups.map((group) => group.strategy_name).filter(Boolean).join(' / ')">MT5 {{ node.mt5_login ?? '—' }} · {{ node.mt5_server || '—' }} · {{ node.symbols.join(' / ') || '—' }}</p></div>
            <span class="an-state" :class="node.follow_state"><i></i>{{ stateNames[node.follow_state] }}</span>
          </header>
          <div class="an-profit" :class="{ up: node.floating_profit != null && node.floating_profit >= 0, down: node.floating_profit != null && node.floating_profit < 0 }"><span><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="m3 17 6-6 4 4 7-7v4h2V4h-8v2h4l-5 6-4-4-8 8 2 1z"/></svg>浮动盈亏</span><strong>{{ money(node.floating_profit, true) }}</strong></div>
          <div class="an-stats"><div><span>持仓笔数</span><b>{{ node.position_count ?? '—' }} 笔</b></div><div><span>手数</span><b>{{ money(node.volume) }}</b></div><div><span>账户余额</span><b>{{ money(node.balance) }}</b></div></div>
          <div class="an-symbols"><span v-for="symbol in node.symbols" :key="symbol">{{ symbol }}</span></div>
          <footer :class="{ stopped: node.follow_state === 'stopped' }">
            <template v-if="node.follow_state !== 'stopped'">
              <button type="button" class="an-pause" :disabled="busy" @click="emit('state', [node.node_id], node.follow_state === 'running' ? 'paused' : 'running')">{{ node.follow_state === 'running' ? '暂停跟单' : '恢复跟单' }}</button>
              <button type="button" class="an-close" :disabled="busy || !(node.position_count && node.position_count > 0)" @click="emit('close', [node.node_id])">{{ node.position_count === 0 ? '已平仓' : '一键平仓' }}</button>
              <button type="button" class="an-stop" :disabled="busy" @click="emit('state', [node.node_id], 'stopped')">停止跟单</button>
            </template>
            <template v-else><span class="an-stopped">已停止跟单</span><button v-if="node.position_count && node.position_count > 0" type="button" class="an-close" :disabled="busy" @click="emit('close', [node.node_id])">一键平仓</button></template>
          </footer>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.an-panel { color: #e6ebf4; }
.an-panel svg { width: 14px; height: 14px; flex-shrink: 0; }
.an-header, .an-card-heading { display: flex; align-items: center; gap: 10px; }
.an-header { justify-content: space-between; flex-wrap: wrap; margin: 14px 0 9px; }
.an-header h2 { margin: 0; padding-left: 7px; border-left: 2px solid #3b82f6; font-size: 13px; font-weight: 600; }
.an-header h2 span { margin-left: 8px; padding: 3px 8px; border: 1px solid #24314b; border-radius: 99px; color: #64748b; font-size: 11px; font-weight: 400; }
.an-batch { display: flex; gap: 6px; }
.an-panel button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 7px 11px; border: 1px solid #24314b; border-radius: 9px; background: #0b1220; font-size: 11px; font-weight: 600; cursor: pointer; }
.an-panel button:disabled { opacity: .4; cursor: not-allowed; }
.an-panel .an-pause { color: #f0b90b; border-color: #634b1d; background: #2c2518; }
.an-panel .an-close { color: #f6465d; border-color: #632739; background: #291620; }
.an-panel .an-stop { color: #ef6b7b; border-color: #632739; background: #291620; }
.an-note { margin: 6px 0; color: #64748b; font-size: 11px; line-height: 1.6; }
.an-filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.an-filters button { padding: 4px 10px; border-radius: 99px; color: #94a3b8; font-weight: 400; }
.an-filters button span { padding: 0 4px; border-radius: 4px; background: #162137; font-size: 10px; }
.an-filters button.on { color: #60a5fa; border-color: #254d8d; background: #11264b; box-shadow: 0 0 8px #2563eb33; }
.an-search { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; padding: 8px 11px; border: 1px solid #24314b; border-radius: 9px; color: #64748b; background: #0b1220; }
.an-search input { width: 100%; min-width: 0; padding: 0; border: 0; outline: 0; color: #cbd5e1; background: transparent; font-size: 12px; }
.an-search input::placeholder { color: #536078; }
.an-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.an-summary > div { padding: 10px 12px; border: 1px solid #24314b; border-radius: 9px; background: #0c1220; }
.an-summary span { display: block; margin-bottom: 6px; color: #64748b; font-size: 11px; }
.an-summary strong { font-size: 14px; }
.an-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.an-card { min-width: 0; padding: 12px; border: 1px solid #24314b; border-radius: 13px; background: #0c1321; box-shadow: 0 2px 10px #0004; }
.an-icon { display: grid; width: 36px; height: 36px; flex-shrink: 0; place-items: center; border-radius: 11px; color: #3b82f6; background: #112140; }
.an-icon svg { width: 20px; height: 20px; }
.an-name { min-width: 0; flex: 1; }
.an-name h3 { margin: 0 0 4px; font-size: 13px; overflow-wrap: anywhere; }
.an-name p { margin: 0; overflow-wrap: anywhere; color: #64748b; font-size: 10px; line-height: 1.5; }
.an-state { display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 99px; font-size: 10px; white-space: nowrap; }
.an-state i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.an-state.running { color: #16c784; background: #10372a; }
.an-state.paused { color: #f0b90b; background: #352b18; }
.an-state.stopped { color: #94a3b8; background: #253044; }
.an-profit { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 12px 0 9px; padding: 10px 12px; border: 1px solid #174237; border-radius: 10px; background: #0e241e; color: #94a3b8; }
.an-profit span { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.an-profit strong { font-size: 16px; }
.an-profit.down { border-color: #512836; background: #29171f; }
.an-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid #24314b; border-radius: 9px; background: #0b1220; }
.an-stats > div { padding: 8px 4px; text-align: center; }
.an-stats > div + div { border-left: 1px solid #24314b; }
.an-stats span { display: block; margin-bottom: 5px; color: #64748b; font-size: 10px; }
.an-stats b { font-size: 12px; overflow-wrap: anywhere; }
.an-symbols { display: flex; flex-wrap: wrap; gap: 5px; min-height: 17px; margin: 8px 0; }
.an-symbols span { padding: 2px 6px; border-radius: 5px; color: #94a3b8; background: #162137; font-size: 10px; }
.an-card footer { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
.an-card footer button { padding: 7px 4px; }
.an-card footer.stopped { grid-template-columns: 1fr auto; align-items: center; }
.an-stopped { color: #64748b; font-size: 11px; text-align: center; }
.an-stopped:only-child { grid-column: 1 / -1; padding: 8px; }
.an-panel .up { color: #16c784; }
.an-panel .down { color: #f6465d; }
.an-error { display: flex; align-items: center; gap: 8px; color: #f6465d; font-size: 12px; }
.an-empty { padding: 18px 8px; color: #64748b; text-align: center; font-size: 12px; }
@media (max-width: 760px) { .an-grid { grid-template-columns: 1fr; } .an-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 420px) { .an-batch { width: 100%; } .an-batch button { flex: 1; padding: 7px 5px; } .an-state { padding: 4px 6px; } }
</style>
