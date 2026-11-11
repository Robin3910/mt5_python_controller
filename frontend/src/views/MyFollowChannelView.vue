<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import type { FollowApplication } from '@/api/types'
import {
  fetchFollowChannelProfile, fetchFollowChannelRequests, approveFollowRequests,
  rejectFollowRequest, setFollowMembersPaused, removeFollowMembers,
  type FollowApplicant, type FollowChannelProfile, type FollowChannelRequests,
} from '@/api/followChannel'
import { trackFollowUi } from '@/utils/followAudit'

const profile = ref<FollowChannelProfile | null>(null)
const data = ref<FollowChannelRequests | null>(null)
const status = ref<'pending' | 'reviewed'>('pending')
const keyword = ref('')
const page = ref(1)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const collapsed = ref(new Set<number>())
let revision = 0
let timer: number | undefined
let searchTimer: number | undefined

function errorText(cause: unknown): string {
  const detail = (cause as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : '操作失败，请稍后重试'
}
async function load(): Promise<void> {
  const current = ++revision
  loading.value = true
  try {
    const [info, list] = await Promise.all([fetchFollowChannelProfile(), fetchFollowChannelRequests(status.value, keyword.value, page.value)])
    if (current !== revision) return
    profile.value = info
    data.value = list
    error.value = ''
  } catch (cause) {
    if (current === revision) error.value = errorText(cause)
  } finally { if (current === revision) loading.value = false }
}
function search(): void {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    page.value = 1
    trackFollowUi('search', { value: keyword.value })
    void load()
  }, 350)
}
function changeStatus(next: 'pending' | 'reviewed'): void {
  status.value = next
  page.value = 1
  trackFollowUi('filter', { value: next })
  void load()
}
function changePage(next: number): void {
  page.value = next
  trackFollowUi('page', { value: String(next) })
  void load()
}
function collapse(id: number): void {
  const next = new Set(collapsed.value)
  next.has(id) ? next.delete(id) : next.add(id)
  collapsed.value = next
  trackFollowUi('collapse_group', { value: String(id) })
}
async function confirm(message: string, title: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消', closeOnClickModal: false })
    return true
  } catch {
    trackFollowUi('cancel_dialog', { value: title })
    return false
  }
}
async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  try { await action(); await load() } catch (cause) { ElMessage.error(errorText(cause)) }
  finally { busy.value = false }
}
async function approve(ids: number[]): Promise<void> {
  if (!ids.length || busy.value || !await confirm(`通过 ${ids.length} 条节点跟单申请？通过后开始接收后续新信号。`, '通过跟单申请')) return
  await run(async () => {
    const result = await approveFollowRequests(ids)
    if (result.failed.length) ElMessage.warning(`已通过 ${result.approved.length} 条，${result.failed.length} 条未完成：${result.failed[0]?.detail}`)
    else ElMessage.success(`已通过 ${result.approved.length} 条申请`)
  })
}
async function reject(node: FollowApplication): Promise<void> {
  if (busy.value || !await confirm(`拒绝「${node.name || node.node_id}」的本次跟单申请？申请人可重新提交。`, '拒绝跟单申请')) return
  await run(async () => { await rejectFollowRequest(node.request_id); ElMessage.success('申请已拒绝') })
}
function approved(applicant: FollowApplicant): FollowApplication[] {
  return applicant.nodes.filter(node => node.status === 'approved' && node.follow_state != null)
}
async function pause(nodes: FollowApplication[]): Promise<void> {
  const next = !nodes.every(node => node.leader_paused)
  if (!nodes.length || busy.value || !await confirm(`${next ? '暂停' : '恢复'}本频道的 ${nodes.length} 条节点跟单？仅影响新的开仓，在跑任务与持仓继续保留。`, next ? '暂停跟单' : '恢复跟单')) return
  await run(async () => {
    await setFollowMembersPaused(nodes.map(node => node.request_id), next)
    ElMessage.success(next ? '已暂停跟单' : '已恢复跟单')
  })
}
async function remove(nodes: FollowApplication[]): Promise<void> {
  if (!nodes.length || busy.value || !await confirm(`移出本频道的 ${nodes.length} 条节点跟单关系？在跑任务与持仓保留，节点本身及其他频道关系保留。`, '移出跟单节点')) return
  await run(async () => { await removeFollowMembers(nodes.map(node => node.request_id)); ElMessage.success('已移出本频道') })
}
function stateText(node: FollowApplication): string {
  if (node.status === 'pending') return '待审核'
  if (node.status === 'rejected') return '已拒绝'
  if (node.status === 'cancelled' || !node.available || node.follow_state == null) return '已移出 / 失效'
  if (!node.marketable) return '频道或节点已停用'
  return node.leader_paused || node.follow_state !== 'running' || profile.value?.follow_paused ? '已暂停' : '跟单中'
}
function refresh(): void { trackFollowUi('refresh'); void load() }
onMounted(() => {
  trackFollowUi('page_view', { value: 'my-follow-channel' })
  void load()
  timer = window.setInterval(() => { if (document.visibilityState === 'visible' && !busy.value) void load() }, 45_000)
})
onUnmounted(() => { ++revision; window.clearInterval(timer); window.clearTimeout(searchTimer) })
</script>

<template>
  <div class="fc-page">
    <section class="fc-hero">
      <header><span class="fc-icon"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM6 13c-3.3 0-6 1.8-6 4v3h12v-3c0-2.2-2.7-4-6-4Zm0-2a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm10 2c-.6 0-1.2.1-1.8.2 1.1 1 1.8 2.3 1.8 3.8v3h8v-3c0-2.2-3.6-4-8-4Z"/></svg></span><div><h2>我的带单频道 <small>✦</small></h2><p>带单员专属 · 跟单申请节点审核与频道管理</p></div></header>
      <div v-if="profile" class="fc-profile"><span class="fc-avatar">{{ profile.display_name.slice(0, 1) }}</span><div><h3>{{ profile.display_name }} <em>Lv.{{ profile.level }} {{ profile.level_name }}</em><span class="fc-certified">已认证</span></h3><p>{{ profile.symbols.join(' / ') || '暂无交易品种' }} · 已运行 {{ profile.running_days }} 天 · 累计收益 {{ profile.total_profit == null ? '暂无数据' : `${profile.total_profit >= 0 ? '+' : ''}${profile.total_profit.toFixed(2)} U` }}</p></div></div>
      <p v-if="profile" class="fc-intro"><b>带单员介绍</b>{{ profile.intro }}</p>
      <div class="fc-stats"><div><span>✓ 已审核节点</span><b>{{ profile?.reviewed_nodes ?? '—' }}</b></div><div><span>⌛ 待审核节点</span><b>{{ profile?.pending_nodes ?? '—' }}</b></div><div><span>♧ 跟单中节点</span><b>{{ profile?.following_nodes ?? '—' }}</b></div></div>
    </section>
    <p v-if="error" class="fc-error">{{ error }} <button type="button" @click="refresh">重试</button></p>
    <div class="fc-tabs"><button type="button" :class="{ active: status === 'pending' }" @click="changeStatus('pending')">⌛ 待审核 <span>{{ profile?.pending_nodes ?? 0 }}</span></button><button type="button" :class="{ active: status === 'reviewed' }" @click="changeStatus('reviewed')">☑ 已审核 <span>{{ profile?.reviewed_nodes ?? 0 }}</span></button><button v-if="status === 'pending'" type="button" class="fc-approve fc-all" :disabled="busy || loading || !data?.all_pending_request_ids.length" @click="approve(data?.all_pending_request_ids || [])">✓ 全部通过</button></div>
    <label class="fc-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input v-model="keyword" aria-label="搜索申请人或节点" placeholder="搜索申请人 / 节点名称 / 账号 / 品种" maxlength="200" @input="search"></label>
    <p v-if="loading && !data" class="fc-empty">加载中…</p>
    <p v-else-if="data && !data.applicants.length" class="fc-empty">{{ keyword ? '没有匹配的申请' : status === 'pending' ? '暂无待审核申请' : '暂无已审核记录' }}</p>
    <article v-for="applicant in data?.applicants || []" :key="applicant.follower_user_id" class="fc-applicant">
      <header><span class="fc-avatar sm">{{ applicant.applicant_name.slice(0, 1) }}</span><div><h3>{{ applicant.applicant_name }} <small>{{ applicant.nodes.length }} 条{{ status === 'pending' ? '待审' : '已审核' }}</small></h3><p>名下 {{ new Set(applicant.nodes.map(node => node.node_id)).size }} 个节点</p></div><div class="fc-actions"><button v-if="status === 'pending'" type="button" class="fc-approve" :disabled="busy" @click="approve(applicant.nodes.map(node => node.request_id))">✓ 本组通过</button><template v-else-if="approved(applicant).length"><button type="button" class="fc-action" :aria-label="approved(applicant).every(node => node.leader_paused) ? '恢复该用户全部跟单' : '暂停该用户全部跟单'" :disabled="busy" @click="pause(approved(applicant))">{{ approved(applicant).every(node => node.leader_paused) ? '▷' : 'Ⅱ' }}</button><button type="button" class="fc-action danger" aria-label="移出该用户全部跟单节点" :disabled="busy" @click="remove(approved(applicant))">移出</button></template><button type="button" class="fc-action" :aria-label="collapsed.has(applicant.follower_user_id) ? '展开该用户的节点' : '折叠该用户的节点'" @click="collapse(applicant.follower_user_id)">{{ collapsed.has(applicant.follower_user_id) ? '⌄' : '⌃' }}</button></div></header>
      <div v-if="!collapsed.has(applicant.follower_user_id)" class="fc-nodes">
        <div v-for="node in applicant.nodes" :key="node.request_id" class="fc-node"><span class="fc-node-icon">⌘</span><div class="fc-node-info"><h4>{{ node.name || node.node_id }} <em v-if="node.is_primary">主节点</em><small>{{ node.strategy_name || node.group_name }}</small></h4><p>{{ node.symbol || '—' }} · 账号 {{ node.mt5_login || '—' }} · ID: {{ node.node_id }}</p><p v-if="node.reason" class="fc-reason">{{ node.reason }}</p></div><div class="fc-node-actions"><span class="fc-state" :class="node.status">{{ node.status === 'approved' ? '✓ 已通过 · ' : '' }}{{ stateText(node) }}</span><template v-if="node.status === 'pending'"><button type="button" class="fc-action danger" aria-label="拒绝该节点申请" :disabled="busy" @click="reject(node)">×</button><button type="button" class="fc-approve" :disabled="busy || !node.available" @click="approve([node.request_id])">✓ 通过</button></template><template v-else-if="node.status === 'approved' && node.follow_state != null"><button type="button" class="fc-switch" :class="{ on: !node.leader_paused }" role="switch" :aria-checked="!node.leader_paused" :aria-label="node.leader_paused ? '恢复节点跟单' : '暂停节点跟单'" :disabled="busy" @click="pause([node])"><i/></button><button type="button" class="fc-action danger" aria-label="移出该节点跟单" :disabled="busy" @click="remove([node])">移出</button></template></div></div>
      </div>
    </article>
    <footer v-if="data && data.total > data.page_size" class="fc-pagination"><button type="button" :disabled="page === 1 || loading || busy" @click="changePage(page - 1)">上一页</button><span>{{ page }} / {{ Math.ceil(data.total / data.page_size) }}</span><button type="button" :disabled="page * data.page_size >= data.total || loading || busy" @click="changePage(page + 1)">下一页</button></footer>
  </div>
</template>

<style scoped>
.fc-page{max-width:1160px;margin:0 auto;color:#e2e8f0;font-size:12px;padding-bottom:28px}.fc-page button{cursor:pointer;font:inherit}.fc-page button:disabled{opacity:.45;cursor:not-allowed}.fc-hero{padding:20px;border:1px solid #1d353e;border-radius:16px;background:linear-gradient(110deg,#0b2527,#0f172a 75%)}.fc-hero>header,.fc-profile,.fc-applicant>header,.fc-actions,.fc-node,.fc-node-actions,.fc-tabs{display:flex;align-items:center;gap:10px}.fc-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:14px;color:#34d399;background:#064e3b}.fc-icon svg{width:26px;height:26px}.fc-hero h2{font-size:18px;margin:0 0 5px}.fc-hero h2 small{color:#34d399}.fc-page p{margin:4px 0 0;color:#8290a6;font-size:11px;line-height:1.6}.fc-profile{margin-top:22px}.fc-avatar{display:grid;place-items:center;flex-shrink:0;width:44px;height:44px;border-radius:12px;background:#241e47;color:#b69aff;font-size:20px}.fc-avatar.sm{width:34px;height:34px;font-size:15px}.fc-page h3{margin:0;font-size:14px;line-height:1.7}.fc-profile em,.fc-node em{font-style:normal;font-size:10px;color:#fbbf24;background:#493718;border:1px solid #74521b;border-radius:5px;padding:2px 6px;margin-left:5px}.fc-certified{font-size:10px;color:#34d399;border:1px solid #146b55;border-radius:20px;padding:3px 7px;margin-left:7px}.fc-intro{padding:12px;margin-top:16px!important;border:1px solid #263545;border-radius:8px;background:#0b1523}.fc-intro b{color:#cbd5e1;display:block;margin-bottom:3px}.fc-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:16px}.fc-stats>div{border:1px solid #263545;background:#0b1523;border-radius:8px;padding:12px}.fc-stats span{display:block;color:#94a3b8;font-size:11px}.fc-stats b{display:block;font-size:23px;margin-top:5px;color:#34d399}.fc-stats>div:nth-child(2) b{color:#fbbf24}.fc-tabs{margin:18px 0 12px}.fc-tabs>button:not(.fc-approve){border:1px solid #22304a;background:#0c1220;color:#94a3b8;border-radius:8px;padding:9px 13px}.fc-tabs .active{color:#c4b5fd!important;border-color:#7c3aed!important;background:#23143c!important}.fc-tabs span{margin-left:5px;font-size:10px}.fc-all{margin-left:auto}.fc-approve{border:1px solid #157457;background:#0b3b2f;color:#4ade80;border-radius:7px;padding:6px 10px;white-space:nowrap}.fc-search{display:flex;align-items:center;gap:8px;padding:10px 12px;margin-bottom:14px;border:1px solid #26324a;border-radius:8px;background:#0b1120}.fc-search svg{width:15px;height:15px;color:#64748b}.fc-search input{width:100%;border:0;background:transparent;color:#e2e8f0;outline:none;font:inherit}.fc-applicant{margin-top:12px;background:#101827;border:1px solid #22304a;border-radius:12px;overflow:hidden}.fc-applicant>header{padding:13px}.fc-applicant h3 small{font-size:10px;font-weight:400;color:#94a3b8;margin-left:6px}.fc-actions{margin-left:auto;gap:7px}.fc-action{border:1px solid #29364d;border-radius:6px;background:#0b1220;color:#94a3b8;padding:5px 8px}.fc-action.danger{color:#f87171;border-color:#542d38}.fc-nodes{border-top:1px solid #22304a;padding:0 12px 5px}.fc-node{padding:13px 0;border-bottom:1px solid #1c293c}.fc-node:last-child{border:0}.fc-node-icon{display:grid;place-items:center;width:30px;height:30px;background:#163043;color:#38bdf8;border-radius:8px;font-size:19px;flex-shrink:0}.fc-node-info{min-width:0;flex:1}.fc-node h4{margin:0;font-size:12px}.fc-node h4 small{display:inline-block;margin-left:6px;padding:2px 6px;border-radius:4px;background:#292144;color:#b7a0ef;font-size:10px;font-weight:400}.fc-node-info p{overflow-wrap:anywhere}.fc-reason{color:#d3a36b!important}.fc-node-actions{flex-shrink:0;gap:8px}.fc-state{font-size:10px;white-space:nowrap;color:#94a3b8}.fc-state.pending{color:#fbbf24}.fc-state.approved{color:#34d399}.fc-state.rejected{color:#f87171}.fc-switch{width:32px;height:18px;border:0;border-radius:20px;background:#334155;padding:2px}.fc-switch i{display:block;width:14px;height:14px;border-radius:50%;background:#cbd5e1;transition:transform .15s}.fc-switch.on{background:#059669}.fc-switch.on i{transform:translateX(14px);background:#ecfdf5}.fc-empty,.fc-error{padding:22px;text-align:center}.fc-error{color:#f87171!important}.fc-error button,.fc-pagination button{background:#101827;border:1px solid #2c3b55;border-radius:6px;color:#cbd5e1;padding:6px 10px}.fc-pagination{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:18px;color:#94a3b8}@media(max-width:640px){.fc-hero{padding:14px}.fc-hero h2{font-size:16px}.fc-profile h3 em{display:inline-block}.fc-profile{align-items:flex-start}.fc-stats>div{padding:10px 7px}.fc-stats span{font-size:10px}.fc-stats b{font-size:20px}.fc-node{flex-wrap:wrap;gap:8px}.fc-node-actions{margin-left:38px;width:calc(100% - 38px);justify-content:flex-end}.fc-state{margin-right:auto}.fc-actions{gap:4px}.fc-action{padding:5px 6px}.fc-applicant h3 small{display:block;margin:0}.fc-tabs{gap:6px}.fc-tabs>button:not(.fc-approve){padding:8px}.fc-all{padding:7px}}
.fc-search input { height: 16px; line-height: 16px; padding: 0; box-shadow: none; }
</style>
