<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import RobotIcon from '@/components/RobotIcon.vue'
import { fetchAiRobotDetail, joinAiFollow } from '@/api/aiFollow'
import type { AiRobotDetailResponse } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useAiFollowDraftStore } from '@/stores/aiFollowDraft'
import { trackAiUi } from '@/utils/aiAudit'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const draft = useAiFollowDraftStore()
const detail = ref<AiRobotDetailResponse | null>(null)
const error = ref('')
const loading = ref(true)
const submitting = ref(false)
const presets = [20, 50, 80, 100]
const customRatio = ref(draft.settings.loss_ratio != null && !presets.includes(draft.settings.loss_ratio))
const leaderId = Number(route.params.leaderUserId)
const groupId = String(route.params.groupId)
const title = computed(() => detail.value?.robot.strategy_name || detail.value?.robot.display_name || 'AI 交易机器人')
const selectedNodes = computed(() => detail.value?.my_nodes.filter((node) => draft.nodeIds.includes(node.node_id)) || [])
const selectedNames = computed(() => selectedNodes.value.map((node) => node.name || node.node_id).join('、'))
const hasDraft = computed(() => draft.userId === auth.me?.user_id && draft.leaderId === leaderId && draft.groupId === groupId && draft.nodeIds.length > 0)

function errorText(cause: unknown): string {
  const data = (cause as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  if (typeof data === 'string') return data
  if (Array.isArray(data)) return data.map((item) => item.msg).join('；')
  return '加载或提交失败，请重试'
}

function changeNodes(): void {
  trackAiUi('open_picker', { group_id: groupId, value: 'change_nodes' })
  void router.push({ name: 'ai-trade', query: { pick_group: groupId, leader: leaderId } })
}

function chooseRisk(mode: 'leader' | 'custom'): void {
  draft.settings.risk_mode = mode
  draft.settings.loss_ratio = mode === 'custom' ? draft.settings.loss_ratio || 20 : null
  trackSettings()
}

function trackSettings(): void {
  trackAiUi('follow_settings', { group_id: groupId, value: JSON.stringify(draft.settings) })
}

function next(): void {
  trackAiUi('follow_step', { group_id: groupId, value: 'confirm' })
  if (!hasDraft.value || selectedNodes.value.length !== draft.nodeIds.length) {
    ElMessage.warning('所选节点已变化，请重新选择节点')
    return
  }
  if (draft.settings.risk_mode === 'custom' && !(Number(draft.settings.loss_ratio) > 0 && Number(draft.settings.loss_ratio) <= 100)) {
    ElMessage.warning('请输入大于 0 且不超过 100 的信号盈亏比')
    return
  }
  draft.step = 2
}

async function submit(): Promise<void> {
  if (submitting.value || !detail.value || !hasDraft.value) return
  submitting.value = true
  try {
    const result = await joinAiFollow({ leader_user_id: leaderId, group_ids: [groupId], node_ids: [...draft.nodeIds], settings: { ...draft.settings } })
    if (!result.pending.length) {
      ElMessage.warning(result.skipped[0]?.reason || '没有可提交的节点，请重新选择')
      return
    }
    ElMessage.success('申请已提交，等待审核')
    if (result.skipped.length) ElMessage.warning(`有 ${result.skipped.length} 项未提交：${result.skipped[0]?.reason}`)
    draft.$reset()
    await router.replace({ name: 'ai-trade', query: { tab: 'pending' } })
  } catch (cause) { ElMessage.error(errorText(cause)) }
  finally { submitting.value = false }
}

onMounted(async () => {
  trackAiUi('page_view', { group_id: groupId, value: 'ai-follow-setup' })
  try { detail.value = await fetchAiRobotDetail(leaderId, '30d', groupId) }
  catch (cause) { error.value = errorText(cause) }
  finally { loading.value = false }
})
</script>

<template>
  <main class="follow-setup-page">
    <section v-if="loading" class="follow-setup-card empty">正在加载跟单设置…</section>
    <section v-else-if="error || !detail" class="follow-setup-card empty"><p>{{ error }}</p><button type="button" @click="changeNodes">返回选择节点</button></section>
    <section v-else class="follow-setup-card">
      <div class="follow-steps" aria-label="跟单步骤"><span :class="{ done: draft.step === 2 }"><i>{{ draft.step === 2 ? '✓' : '1' }}</i>参数风控</span><b :class="{ done: draft.step === 2 }"></b><span :class="{ inactive: draft.step === 1 }"><i>2</i>确认提交</span></div>
      <div class="follow-robot"><RobotIcon :size="24" boxed variant="iconfont" /><div><h2>{{ title }} <small>{{ detail.robot.strategy_name }}</small></h2><p>{{ detail.robot.description || '策略自动运行，同步机器人开仓与平仓信号' }}</p></div><span class="follow-count">{{ detail.robot.follower_node_count }} 个节点跟随</span></div>
      <div class="follow-selection"><span class="selection-icon">▣</span><div><p>跟单节点</p><strong :title="selectedNames">{{ hasDraft ? `已选 ${draft.nodeIds.length} 个：${selectedNames}` : '尚未选择跟单节点' }}</strong></div><button type="button" :disabled="submitting" @click="changeNodes">⇄ 更换节点</button></div>
      <template v-if="draft.step === 1">
        <h3>跟单参数与风控</h3><p class="follow-muted">设置信号盈亏比，跟单任务按此风控执行</p>
        <fieldset><legend>信号盈亏比设置</legend><div class="risk-modes" role="radiogroup" aria-label="信号盈亏比设置"><button type="button" role="radio" :aria-checked="draft.settings.risk_mode === 'leader'" :class="{ selected: draft.settings.risk_mode === 'leader' }" @click="chooseRisk('leader')"><strong>跟随带单</strong><small>信号盈亏比跟随带单策略</small></button><button type="button" role="radio" :aria-checked="draft.settings.risk_mode === 'custom'" :class="{ selected: draft.settings.risk_mode === 'custom' }" @click="chooseRisk('custom')"><strong>自定义设置</strong><small>自定义信号盈亏比</small></button></div></fieldset>
        <p v-if="draft.settings.risk_mode === 'leader'" class="follow-hint"><span>●</span> 跟随带单风控：信号盈亏比使用带单策略自带规则，无需手动设置。</p>
        <fieldset v-else><legend>信号盈亏比 <small>按账户余额的该比例计算本信号共同止损</small></legend><div class="ratio-options"><button v-for="value in presets" :key="value" type="button" :class="{ selected: !customRatio && draft.settings.loss_ratio === value }" @click="customRatio = false; draft.settings.loss_ratio = value; trackSettings()">{{ value }}%</button><button type="button" :class="{ selected: customRatio }" @click="customRatio = true; trackAiUi('follow_settings', { group_id: groupId, value: 'custom_ratio' })">自定义</button><label v-if="customRatio"><input v-model.number="draft.settings.loss_ratio" type="number" min="0.01" max="100" step="0.01" aria-label="自定义信号盈亏比" @change="trackSettings">%</label></div></fieldset>
        <fieldset><legend>清仓方式 <small>{{ draft.settings.run_mode === 'loop' ? '循环：完成一轮后继续跟随下一轮信号' : '单次：完成一轮后自动停止后续跟单，启动失败可重试' }}</small></legend><div class="run-modes"><button type="button" :aria-pressed="draft.settings.run_mode === 'loop'" :class="{ selected: draft.settings.run_mode === 'loop' }" @click="draft.settings.run_mode = 'loop'; trackSettings()">循环</button><button type="button" :aria-pressed="draft.settings.run_mode === 'single'" :class="{ selected: draft.settings.run_mode === 'single' }" @click="draft.settings.run_mode = 'single'; trackSettings()">单次</button></div></fieldset>
        <footer><button type="button" class="next" :disabled="!hasDraft" @click="next">下一步 ›</button></footer>
      </template>
      <template v-else>
        <h3>确认跟单信息</h3><p class="follow-muted">请核对以下跟单设置，审核通过后开始同步策略信号</p>
        <dl class="follow-summary"><div><dt>跟单机器人</dt><dd>{{ title }}（{{ detail.robot.strategy_name }}）</dd></div><div><dt>跟单节点</dt><dd>已选 {{ draft.nodeIds.length }} 个：{{ selectedNames }}</dd></div><div><dt>信号盈亏比</dt><dd>{{ draft.settings.risk_mode === 'leader' ? '跟随带单策略' : `${draft.settings.loss_ratio}%` }}</dd></div><div><dt>清仓方式</dt><dd>{{ draft.settings.run_mode === 'loop' ? '循环' : '单次' }}</dd></div></dl>
        <p class="follow-warning">⚠ 跟单交易存在市场风险，请确认已了解策略规则与风险。手数完整跟随机器人，发单人修改策略参数会实时作用于在跑任务；自定义信号盈亏比保持独立。</p>
        <footer class="confirm-actions"><button type="button" :disabled="submitting" @click="draft.step = 1; trackAiUi('follow_step', { group_id: groupId, value: 'risk' })">上一步</button><button type="button" class="confirm" :disabled="submitting || !hasDraft" @click="submit">{{ submitting ? '正在提交…' : '⇄ 确认跟单' }}</button></footer>
      </template>
      <p class="follow-footnote">提交后等待发单人审核，通过后同步「{{ title }}」的新信号。可在 AI 交易页暂停或停止跟单。</p>
    </section>
  </main>
</template>

<style scoped>
.follow-setup-page{min-height:calc(100dvh - 100px);display:flex;align-items:center;padding:16px 4px;color:#e6ebf4;font-size:12px}
.follow-setup-card{width:min(100%,744px);padding:16px;border:1px solid #22304a;border-radius:12px;background:#101a2e;box-sizing:border-box}
.follow-setup-card button{cursor:pointer;font:inherit;color:#8b96a8;border:1px solid #22304a;background:#0c1424;border-radius:9px;padding:9px 12px}
.follow-setup-card button:disabled{opacity:.4;cursor:not-allowed}
.follow-steps{display:flex;align-items:center;gap:8px;font-size:10px;margin-bottom:16px}
.follow-steps span{display:flex;gap:6px;align-items:center;white-space:nowrap}.follow-steps i{font-style:normal;display:grid;place-items:center;width:21px;height:21px;border-radius:50%;background:#2563eb;color:white;box-shadow:0 0 0 3px #2563eb25}.follow-steps b{height:1px;flex:1;background:#223451}.follow-steps .inactive{color:#5b6b85}.follow-steps .inactive i{background:#223451;box-shadow:none;color:#6a7b96}.follow-steps .done{color:#22c55e}.follow-steps .done i,.follow-steps b.done{background:#22c55e;box-shadow:none}
.follow-robot{display:flex;align-items:center;gap:10px;padding:12px 10px;background:#0c1424;border:1px solid #22304a;border-radius:12px}.follow-robot>div{min-width:0;flex:1}.follow-robot h2{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:12px;margin:0}.follow-robot h2 small{font-size:9px;color:#8b96a8;background:#1b293f;border-radius:4px;padding:2px 5px;font-weight:400}.follow-robot p{color:#5b6b85;font-size:10px;line-height:1.5;margin:4px 0 0}.follow-count{font-size:9px;border:1px solid #22304a;padding:4px 7px;border-radius:7px;color:#8b96a8;white-space:nowrap}
.follow-selection{display:flex;align-items:center;gap:10px;margin:12px 0 18px;padding:10px;border:1px solid #7c3aed50;border-radius:12px;background:#7c3aed16}.selection-icon{width:38px;height:38px;display:grid;place-items:center;flex-shrink:0;background:#7c3aed20;border-radius:9px;color:#a78bfa;font-size:24px}.follow-selection>div{flex:1;min-width:0}.follow-selection p{margin:0 0 4px;color:#8b96a8;font-size:10px}.follow-selection strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}.follow-selection button{font-size:10px;white-space:nowrap;padding:7px 10px;background:#7c3aed18;border-color:#7c3aed60;color:#c4b5fd}
h3{font-size:12px;margin:0 0 4px}.follow-muted{font-size:10px;color:#5b6b85;margin:0 0 14px}fieldset{border:0;padding:0;margin:12px 0 0}legend{color:#8b96a8;font-size:11px;margin-bottom:7px;padding:0}legend small{font-size:10px;color:#5b6b85;margin-left:5px}.risk-modes,.run-modes,.ratio-options{display:flex;gap:8px}.risk-modes button{flex:1;text-align:left;padding:12px}.risk-modes strong,.risk-modes small{display:block}.risk-modes small{font-size:10px;color:#5b6b85;margin-top:4px}.follow-setup-card button.selected{border-color:#2563eb;background:#2563eb18;box-shadow:0 0 0 1px #2563eb30;color:#bfdbfe}.run-modes button{flex:1;padding:8px}.ratio-options{flex-wrap:wrap}.ratio-options button{flex:1;white-space:nowrap}.ratio-options label{display:flex;align-items:center;gap:5px;color:#8b96a8}.ratio-options input{width:90px;border:1px solid #22304a;border-radius:7px;background:#0c1424;color:#e6ebf4;padding:8px}
.follow-hint{font-size:10px;line-height:1.5;color:#7a8ba6;border:1px solid #22304a;background:#0c1424;border-radius:9px;padding:9px 12px}.follow-hint span{color:#22c55e;margin-right:7px}.follow-summary{padding:12px;border:1px solid #22304a;border-radius:12px;background:#0c1424}.follow-summary>div{display:flex;justify-content:space-between;gap:20px;padding:6px 0;font-size:11px}.follow-summary dt{color:#5b6b85;flex-shrink:0}.follow-summary dd{text-align:right;margin:0;line-height:1.5;font-weight:600;overflow-wrap:anywhere}.follow-warning{color:#fbbf24;background:#0c1927;border:1px solid #fbbf2430;border-radius:9px;padding:10px 12px;font-size:10px;line-height:1.7}
footer{border-top:1px solid #22304a;margin-top:17px;padding-top:12px;display:flex;gap:8px}.follow-setup-card .next,.follow-setup-card .confirm{background:linear-gradient(90deg,#2563eb,#1d4ed8);border:0;color:white;font-size:14px;font-weight:700;padding:12px;flex:1;box-shadow:0 3px 12px #2563eb40}.follow-setup-card .confirm{background:linear-gradient(90deg,#22c55e,#16a34a);box-shadow:0 3px 12px #22c55e30}.follow-footnote{font-size:10px;color:#47566e;line-height:1.6;margin:12px 0 0}.empty{text-align:center;color:#8b96a8}.follow-setup-card button:focus-visible,.follow-setup-card input:focus-visible{outline:2px solid #3b82f6;outline-offset:2px}
@media(max-width:600px){.follow-setup-page{align-items:flex-start;padding:10px 0}.follow-setup-card{padding:13px}.follow-count{display:none}.follow-selection{gap:7px}.selection-icon{width:30px;height:32px;font-size:20px}.follow-selection strong{font-size:11px}.follow-summary>div{gap:12px}.follow-summary dd{max-width:72%}.ratio-options button{padding:9px 7px}.follow-setup-card .next,.follow-setup-card .confirm{font-size:13px}}
</style>
