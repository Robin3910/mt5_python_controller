<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import {
  fetchAiChannelProfile, fetchAiChannelRequests, approveAiApplications, rejectAiApplication,
  setAiChannelPaused, deleteAiChannelMembers,
  type AiApplication, type AiChannelGroup, type AiChannelProfile, type AiChannelRequests,
} from '@/api/aiChannel'
import { previewChannelDeletion, deletionBlocked, deletionImpact } from '@/api/nodeDeletion'
import RobotIcon from '@/components/RobotIcon.vue'
import { trackAiUi } from '@/utils/aiAudit'

const profile = ref<AiChannelProfile | null>(null)
const data = ref<AiChannelRequests | null>(null)
const status = ref<'pending' | 'reviewed'>('pending')
const keyword = ref('')
const page = ref(1)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const collapsed = ref(new Set<string>())
let revision = 0
let timer: number | undefined
let searchTimer: number | undefined

function errorText(cause: unknown): string {
  const detail = (cause as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  if (typeof detail === 'string') return detail
  return (detail as { message?: string })?.message || '操作失败，请稍后重试'
}

async function load(): Promise<void> {
  const current = ++revision
  loading.value = true
  try {
    const [info, list] = await Promise.all([fetchAiChannelProfile(), fetchAiChannelRequests(status.value, keyword.value, page.value)])
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
    trackAiUi('search', { value: keyword.value })
    void load()
  }, 350)
}

function changeStatus(next: 'pending' | 'reviewed'): void {
  status.value = next
  page.value = 1
  trackAiUi('filter', { value: next })
  void load()
}

function collapse(groupId: string): void {
  const next = new Set(collapsed.value)
  next.has(groupId) ? next.delete(groupId) : next.add(groupId)
  collapsed.value = next
  trackAiUi('collapse_group', { group_id: groupId, value: next.has(groupId) ? 'collapsed' : 'expanded' })
}

async function confirm(message: string, title: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消', closeOnClickModal: false })
    return true
  } catch {
    trackAiUi('cancel_dialog', { value: title })
    return false
  }
}

async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  try { await action(); await load() } catch (cause) { ElMessage.error(errorText(cause)) }
  finally { busy.value = false }
}

async function approve(ids: number[], nodeCount: number): Promise<void> {
  if (!ids.length || busy.value || !await confirm(`将通过 ${ids.length} 笔申请、${nodeCount} 个节点。通过后节点开始接收该机器人新的策略信号。`, '通过机器人申请')) return
  await run(async () => {
    const result = await approveAiApplications(ids)
    if (result.failed.length) ElMessage.warning(`通过 ${result.approved.length} 笔，失败 ${result.failed.length} 笔：${result.failed[0]?.detail}`)
    else ElMessage.success(`已通过 ${result.approved.length} 笔申请`)
  })
}

async function reject(application: AiApplication): Promise<void> {
  if (busy.value || !await confirm(`拒绝「${application.applicant_name}」本次申请的全部 ${application.nodes.length} 个节点？`, '拒绝该申请')) return
  await run(async () => { await rejectAiApplication(application.request_id); ElMessage.success('申请已拒绝') })
}

function ownerPaused(application: AiApplication): boolean {
  return application.nodes.filter((node) => node.status === 'approved').every((node) => node.leader_paused)
}

async function pause(group: AiChannelGroup, application?: AiApplication): Promise<void> {
  const next = !(application ? ownerPaused(application) : group.leader_paused)
  if (busy.value || !await confirm(`${next ? '暂停' : '恢复'}「${group.robot_name}」${application ? `中 ${application.applicant_name} 的` : '全部'}跟单节点？\n只影响新的开仓，在跑任务和持仓继续保留。`, next ? '暂停跟单' : '恢复跟单')) return
  await run(async () => {
    await setAiChannelPaused(group.group_id, next, application?.follower_user_id)
    ElMessage.success(next ? '已暂停频道跟单' : '已恢复频道跟单')
  })
}

async function remove(group: AiChannelGroup, application?: AiApplication): Promise<void> {
  if (busy.value) return
  await run(async () => {
    trackAiUi('open_delete', { group_id: group.group_id })
    const preview = await previewChannelDeletion(group.group_id, application?.follower_user_id)
    if (preview.blocked.length) {
      await ElMessageBox.alert(deletionBlocked(preview), '暂不能删除系统节点', { type: 'warning', confirmButtonText: '知道了' })
      return
    }
    if (!await confirm(deletionImpact(preview), application ? '删除跟单节点' : '删除整个机器人分组的成员节点')) return
    await deleteAiChannelMembers(group.group_id, preview.node_ids, preview.version, application?.follower_user_id)
    ElMessage.success(`已删除 ${preview.node_count} 个系统节点`)
  })
}

function turnPage(delta: number): void {
  page.value += delta
  trackAiUi('filter', { value: `page:${page.value}` })
  void load()
}

onMounted(() => {
  trackAiUi('page_view', { value: 'my-ai-channel' })
  void load()
  timer = window.setInterval(() => { if (document.visibilityState === 'visible' && !busy.value) void load() }, 45_000)
})
onUnmounted(() => { window.clearInterval(timer); window.clearTimeout(searchTimer); revision++ })
</script>

<template>
  <div class="channel-page">
    <section v-if="profile" class="channel-hero">
      <header><span class="channel-icon"><RobotIcon :size="24" variant="iconfont" /></span><div><h2>AI交易机器人频道 <small>✦</small></h2><p>机器人专属 · 按机器人分组管理申请审核</p></div></header>
      <div class="channel-identity"><span class="identity-icon">✦</span><div><h3>{{ profile.display_name }} <span class="level">Lv.{{ profile.level }} {{ profile.level_name }}</span><span class="online" :class="profile.channel_status">● {{ profile.channel_status === 'online' ? '策略在线' : profile.channel_status === 'paused' ? '已暂停' : '暂无启用策略' }}</span></h3><p>{{ profile.symbols.join(' · ') || '暂无品种' }} · AI 智能 · 已运行 {{ profile.running_days }} 天 · 累计收益 {{ profile.total_profit == null ? '暂无数据' : `${profile.total_profit >= 0 ? '+' : ''}${profile.total_profit.toFixed(2)} U` }}</p></div></div>
      <p class="channel-intro"><strong>频道介绍</strong> {{ profile.intro }}</p>
      <div class="channel-stats"><div><span>✓ 已审核申请</span><b>{{ profile.reviewed_requests }}</b></div><div><span>⌛ 待审核申请</span><b>{{ profile.pending_requests }}</b></div><div><span>▣ 运行中机器人</span><b>{{ profile.running_robots }}</b></div></div>
    </section>

    <p v-if="error" class="channel-error">{{ error }} <button type="button" @click="trackAiUi('refresh'); load()">重试</button></p>
    <div class="channel-toolbar">
      <div class="channel-tabs"><button type="button" :class="{ active: status === 'pending' }" @click="changeStatus('pending')">⌛ 待审核 <span>{{ data?.stats.pending_requests ?? 0 }}</span></button><button type="button" :class="{ 'reviewed-active': status === 'reviewed' }" @click="changeStatus('reviewed')">▣ 已审核 <span>{{ data?.stats.reviewed_requests ?? 0 }}</span></button></div>
      <button v-if="status === 'pending'" type="button" class="approve" :disabled="busy || loading || !data?.all_pending_request_ids.length" @click="approve([...(data?.all_pending_request_ids || [])], data?.all_pending_node_count || 0)">✓ 全部通过</button>
    </div>
    <label class="channel-search"><span>⌕</span><input v-model="keyword" aria-label="搜索机器人或申请人" placeholder="搜索机器人名称 / 申请人 / ID / 品种" maxlength="200" @input="search"></label>
    <p v-if="loading && !data" class="channel-empty">加载中…</p>
    <p v-else-if="data && !data.groups.length" class="channel-empty">{{ keyword ? '没有匹配的申请' : status === 'pending' ? '暂无待审核申请' : '暂无已审核申请' }}</p>
    <section v-for="group in data?.groups || []" :key="group.group_id" class="robot-group">
      <header class="robot-header">
        <span class="robot-avatar">{{ group.robot_name.slice(0, 1) }}</span>
        <div class="robot-heading"><h3>{{ group.robot_name }} <span>成员 {{ group.member_count }} 人</span><em v-if="status === 'pending'">{{ group.pending_count }} 个待审</em></h3><p>ID：{{ group.group_id }}<template v-if="status === 'reviewed'"> · 通过 {{ group.approved_count }} · 拒绝 {{ group.rejected_count }}</template></p></div>
        <div class="group-actions"><button v-if="status === 'pending'" type="button" class="approve" :disabled="busy || !group.pending_request_ids.length" @click="approve([...group.pending_request_ids], group.pending_node_count)">✓ 本组通过</button><template v-else><button type="button" class="icon-button" :aria-label="group.leader_paused ? '恢复全部跟单' : '暂停全部跟单'" :disabled="busy" @click="pause(group)">{{ group.leader_paused ? '▷' : 'Ⅱ' }}</button><button type="button" class="icon-button danger" aria-label="删除整组" :disabled="busy" @click="remove(group)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button></template><button type="button" class="icon-button" :aria-label="collapsed.has(group.group_id) ? '展开该机器人分组' : '折叠该机器人分组'" @click="collapse(group.group_id)">{{ collapsed.has(group.group_id) ? '⌄' : '⌃' }}</button></div>
      </header>
      <div v-if="!collapsed.has(group.group_id)">
        <article v-for="application in group.requests" :key="application.request_id" class="applicant-row">
          <span class="applicant-avatar">{{ application.applicant_name.slice(0, 1) }}</span>
          <div class="applicant-main"><h4>{{ application.applicant_name }} <span>{{ group.robot_name }}</span></h4><p>ID：{{ application.follower_user_id }} · {{ application.symbol || '—' }} · {{ application.nodes.length }} 个节点<template v-if="application.source === 'legacy'"> · 存量复审</template></p><details @toggle="trackAiUi('collapse_group', { group_id: group.group_id, value: `request:${application.request_id}` })"><summary>节点明细 · 加仓上限 {{ application.risk.max_allow_num ?? '—' }} · 总手数上限 {{ application.risk.total_lot_limit ?? '—' }}</summary><p v-for="node in application.nodes" :key="node.node_id">{{ node.name || node.node_id }} · {{ node.mt5_login || '—' }} <span v-if="node.is_primary">主节点</span><span v-if="!node.available"> · 已失效</span><span v-else-if="node.leader_paused"> · 频道已暂停</span><span v-else-if="node.follow_state === 'paused'"> · 跟单人已暂停</span></p></details><p v-if="application.reason" class="reason">{{ application.reason }}</p></div>
          <div class="applicant-actions"><template v-if="application.status === 'pending'"><span class="status pending">● 待审核</span><button type="button" class="reject" aria-label="拒绝该申请" :disabled="busy" @click="reject(application)">×</button><button type="button" class="approve" :disabled="busy" @click="approve([application.request_id], application.nodes.length)">✓ 通过</button></template><template v-else><span class="status" :class="application.status">{{ application.status === 'rejected' ? '⊗ 已拒绝' : ownerPaused(application) ? '频道已暂停' : application.nodes.filter(node => node.status === 'approved').every(node => node.follow_state !== 'running') ? '跟单人已暂停' : '跟单中' }}</span><button v-if="application.status === 'approved'" type="button" role="switch" :aria-checked="!ownerPaused(application)" :aria-label="ownerPaused(application) ? '恢复跟单' : '暂停跟单'" class="pause-switch" :class="{ on: !ownerPaused(application) }" :disabled="busy" @click="pause(group, application)"><i></i></button><button type="button" class="icon-button danger" aria-label="删除该节点" :disabled="busy" @click="remove(group, application)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button></template></div>
        </article>
      </div>
    </section>
    <div v-if="data && data.total > data.page_size" class="channel-pagination"><button type="button" :disabled="page <= 1 || loading" @click="turnPage(-1)">上一页</button><span>{{ page }} / {{ Math.ceil(data.total / data.page_size) }}</span><button type="button" :disabled="page * data.page_size >= data.total || loading" @click="turnPage(1)">下一页</button></div>
  </div>
</template>

<style scoped>
.channel-page{max-width:1200px;margin:0 auto;color:#e6ebf4;font-size:12px}
.channel-page button{cursor:pointer;font:inherit}
.channel-page button:disabled{opacity:.4;cursor:not-allowed}
.channel-hero{padding:14px;border:1px solid #1c3036;border-radius:12px;background:linear-gradient(135deg,#101d2b,#10251f);box-shadow:0 2px 12px #0004}
.channel-hero header,.channel-identity{display:flex;align-items:center;gap:10px}
.channel-icon,.identity-icon{display:flex;align-items:center;justify-content:center;flex-shrink:0;border-radius:12px;width:40px;height:40px;background:linear-gradient(135deg,#0ea5a4,#22c55e);font-size:24px;color:white;box-shadow:0 2px 14px #22c55e44}
h2,h3,h4,p{margin:0}
h2{font-size:14px}
h2 small{color:#22c55e}
.channel-hero header p,.channel-identity p{margin-top:4px;color:#75839b;font-size:10px}
.channel-identity{margin-top:14px;padding:10px;border:1px solid #22304a;border-radius:8px;background:#0c1220}
.identity-icon{width:34px;height:34px;background:#123526;color:#22c55e;box-shadow:none}
.channel-identity h3{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:12px}
.level,.online{font-size:9px;border-radius:99px;padding:2px 7px}
.level{background:#22c55e20;color:#4ade80;border:1px solid #22c55e40}
.online{background:#0ea5e920;color:#38bdf8}
.online.paused{color:#fbbf24;background:#f59e0b20}
.channel-intro{padding:10px;border:1px solid #22304a;background:#0c1220;border-radius:8px;margin-top:8px;color:#8b96a8;font-size:10px;line-height:1.7}
.channel-intro strong{color:#4ade80}
.channel-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
.channel-stats div{padding:8px;border:1px solid #22304a;border-radius:8px;background:#0c1220}
.channel-stats span{font-size:10px;color:#8b96a8}
.channel-stats b{display:block;font-size:16px;margin-top:4px;color:#22c55e}
.channel-stats div:nth-child(2) b{color:#f59e0b}
.channel-stats div:nth-child(3) b{color:#38bdf8}
.channel-toolbar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:12px 0 8px}
.channel-tabs{display:flex;gap:4px;padding:4px;background:#101828;border:1px solid #1c2740;border-radius:10px}
.channel-tabs button{background:none;border:0;color:#8b96a8;padding:6px 10px;border-radius:7px;font-weight:600}
.channel-tabs button.active{color:#fbbf24;background:#f59e0b24}
.channel-tabs span{font-size:10px;padding:0 4px;border-radius:99px;background:#0003}
.approve{color:white;background:linear-gradient(90deg,#22c55e,#16a34a);border:0;border-radius:7px;padding:7px 10px;box-shadow:0 2px 8px #22c55e30;font-weight:600}
.channel-search{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#0c1220;border:1px solid #22304a;border-radius:8px;color:#5b6b85}
.channel-search input{width:100%;border:0;background:none;outline:none;color:#e6ebf4;font:inherit}
.channel-search input::placeholder{color:#5b6b85}
.robot-group{overflow:hidden;border:1px solid #1c2740;background:#101828;border-radius:12px;margin-top:10px}
.robot-header{display:flex;align-items:center;gap:8px;padding:10px;background:#141e34;border-bottom:1px solid #1c2740}
.robot-avatar,.applicant-avatar{display:flex;align-items:center;justify-content:center;width:34px;height:34px;flex-shrink:0;background:#f59e0b15;color:#fbbf24;border-radius:8px;font-size:15px;font-weight:700}
.robot-heading,.applicant-main{min-width:0;flex:1}
.robot-heading h3{font-size:12px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.robot-heading h3 span{font-size:9px;border:1px solid #38bdf850;color:#38bdf8;border-radius:99px;padding:1px 6px}
.robot-heading em{font-size:9px;font-style:normal;color:#fbbf24;background:#f59e0b15;border-radius:99px;padding:2px 5px}
.robot-heading p,.applicant-main p{margin-top:4px;font-size:9px;color:#5b6b85;overflow-wrap:anywhere}
.group-actions,.applicant-actions{display:flex;align-items:center;gap:6px;flex-shrink:0}
.icon-button{border:1px solid #22304a;background:#0c1220;color:#8b96a8;width:28px;height:28px;border-radius:7px;font-size:16px!important}
.danger{color:#f87171}
.applicant-row{display:flex;align-items:center;gap:9px;padding:10px;border-bottom:1px solid #1c2740}
.applicant-row:last-child{border:0}
.applicant-avatar{width:28px;height:28px;font-size:12px}
.applicant-main h4{font-size:11px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.applicant-main h4 span{font-size:9px;color:#4ade80;background:#22c55e18;padding:2px 5px;border-radius:99px}
.applicant-main details{font-size:9px;color:#8b96a8;margin-top:5px}
.applicant-main summary{cursor:pointer}
.reason{color:#f87171!important}
.status{font-size:9px;border-radius:99px;padding:3px 6px;white-space:nowrap}
.status.pending{background:#f59e0b15;color:#fbbf24;border:1px solid #f59e0b40}
.status.approved{background:#22c55e15;color:#22c55e}
.status.rejected{background:#f6465d15;color:#f87171}
.reject{border:1px solid #f6465d30;background:#f6465d18;color:#f87171;border-radius:5px;font-size:18px!important;line-height:23px;width:23px;padding:0}
.pause-switch{width:30px;border:0;border-radius:99px;padding:3px;background:#374151;display:flex}
.pause-switch i{width:12px;height:12px;background:white;border-radius:50%}
.pause-switch.on{background:#22c55e;justify-content:flex-end}
.channel-empty{text-align:center;padding:28px;color:#5b6b85}
.channel-error{margin:12px 0;color:#f87171}
.channel-error button,.channel-pagination button{background:#101828;border:1px solid #22304a;color:#8b96a8;border-radius:6px;padding:5px 10px}
.channel-pagination{display:flex;align-items:center;justify-content:center;gap:12px;margin:16px 0}
.channel-page input:focus-visible,.channel-page button:focus-visible{outline:2px solid #3b82f6;outline-offset:2px}
@media(max-width:520px){.channel-hero{padding:10px}
.applicant-row{flex-wrap:wrap}
.applicant-actions{margin-left:37px}
.group-actions{gap:4px}
.robot-header{gap:6px}
.robot-heading h3{font-size:11px}
.robot-heading p{font-size:8px}
.approve{padding:6px 8px;font-size:10px!important}
.channel-tabs button{padding:6px 7px;font-size:11px}
.channel-stats{gap:5px}
.channel-stats span{font-size:9px}
.channel-identity p{line-height:1.6}
}

.channel-icon :deep(.robot-icon) { color: #fff; }
.icon-button svg { width: 14px; height: 14px; display: block; margin: auto; }
.channel-tabs button.reviewed-active { background: #22304a; color: #e6ebf4; }
</style>
