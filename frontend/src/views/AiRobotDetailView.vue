<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { fetchMyAiRobotDetail, setAiFollowPaused } from '@/api/aiFollow'
import type { AiMyRobotDetailResponse } from '@/api/types'
import StrategyFormModal from '@/components/StrategyFormModal.vue'

const route = useRoute()
const router = useRouter()
const leaderId = computed(() => Number(route.params.leaderUserId))
const groupId = computed(() => String(route.params.groupId || ''))
const detail = ref<AiMyRobotDetailResponse | null>(null)
const loading = ref(false)
const error = ref('')
const busy = ref(false)
const editorOpen = ref(false)
let pollTimer: number | undefined
let loadVersion = 0

const title = computed(() => detail.value?.robot.strategy_name || detail.value?.robot.display_name || detail.value?.robot.username || '机器人详情')
const symbols = computed(() => [...new Set((detail.value?.groups || []).map((group) => group.symbol).filter(Boolean))].join(' · '))
const elapsedDays = computed(() => detail.value?.started_at == null ? null : Math.max(0, Math.floor((Date.now() / 1000 - detail.value.started_at) / 86400)))
const chartMax = computed(() => Math.max(1, ...(detail.value?.daily_profit || []).map((point) => Math.abs(point.profit))))
const hasDailyDeals = computed(() => detail.value?.daily_profit.some((point) => point.deals > 0))

function money(value: number | null | undefined, signed = false): string {
  if (value == null) return '—'
  const formatted = Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return value < 0 ? `-${formatted}` : `${signed ? '+' : ''}${formatted}`
}

function clock(timestamp: number | null): string {
  return timestamp == null ? '—' : new Date(timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function errorText(cause: unknown): string {
  const message = (cause as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof message === 'string' ? message : '操作失败，请重试'
}

async function loadDetail(): Promise<void> {
  const version = ++loadVersion
  const requestedId = leaderId.value
  loading.value = true
  error.value = ''
  try {
    const response = await fetchMyAiRobotDetail(requestedId, groupId.value)
    if (version === loadVersion) detail.value = response
  } catch (cause) {
    if (version === loadVersion) {
      detail.value = null
      error.value = errorText(cause)
    }
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

async function changePaused(paused: boolean, stopping = false): Promise<void> {
  if (!detail.value?.can_control || busy.value) return
  const robot = detail.value.robot
  const action = stopping ? '停止机器人' : paused ? '暂停机器人' : '启动机器人'
  try {
    await ElMessageBox.confirm(
      paused
        ? `确认${action}「${title.value}」？\n\n账号名下所有 AI 分组的主节点、自有成员和跟单节点将停止接收新开仓，同时停止接收新增跟单。\n已有任务和持仓保留并继续按原规则运行，平仓 / CLOSE 仍可执行。`
        : `确认启动「${title.value}」？账号名下所有 AI 分组将恢复接收新的开仓信号与新增跟单，暂停期间的信号不会补发。`,
      action,
      { type: paused ? 'warning' : 'info', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch { return }
  busy.value = true
  try {
    await setAiFollowPaused({ leader_user_id: robot.leader_user_id, paused })
    ElMessage.success(paused ? '已停止接收新开仓信号' : '已恢复接收新开仓信号')
    await loadDetail()
  } catch (cause) {
    ElMessage.error(errorText(cause))
  } finally { busy.value = false }
}

onMounted(() => {
  void loadDetail()
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !busy.value && !editorOpen.value) void loadDetail()
  }, 45_000)
})
watch([leaderId, groupId], () => {
  detail.value = null
  editorOpen.value = false
  void loadDetail()
})
onUnmounted(() => {
  loadVersion++
  if (pollTimer != null) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="robot-detail">
    <p v-if="loading && !detail" class="rd-empty">加载机器人详情…</p>
    <section v-else-if="error" class="rd-panel rd-error" role="alert">
      <p>{{ error }}</p>
      <button type="button" @click="loadDetail">重试</button>
      <button type="button" @click="router.push({ name: 'ai-trade' })">返回 AI 交易</button>
    </section>
    <template v-else-if="detail">
      <section class="rd-panel rd-overview">
        <div class="rd-heading">
          <span class="rd-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm7 4H8v-2h8v2zm-1-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg></span>
          <div class="rd-name"><h1>{{ title }}</h1><p>{{ symbols || '—' }} · 已运行 {{ elapsedDays ?? '—' }} 天 · 更新 {{ clock(detail.updated_at) }}</p></div>
          <span class="rd-status" :class="{ paused: detail.robot.ai_follow_paused }"><i></i>{{ detail.robot.ai_follow_paused ? '已暂停' : '运行中' }}</span>
        </div>
        <div class="rd-metrics">
          <div><span>跟单节点</span><strong>{{ detail.metrics.follow_node_count }} 个</strong></div>
          <div><span>总投资额</span><strong>{{ money(detail.metrics.investment_amount) }}<small v-if="detail.metrics.investment_amount != null"> U</small></strong></div>
          <div><span>总浮动盈亏</span><strong :class="{ up: detail.metrics.floating_profit != null && detail.metrics.floating_profit >= 0, down: detail.metrics.floating_profit != null && detail.metrics.floating_profit < 0 }">{{ money(detail.metrics.floating_profit, true) }}</strong></div>
          <div><span>持仓手数</span><strong>{{ money(detail.metrics.volume) }} 手</strong></div>
        </div>
        <div class="rd-summary">
          <span>盈利节点 <b class="up">{{ detail.metrics.profitable_node_count ?? '—' }}</b> / {{ detail.metrics.node_count }}</span>
          <span>共持仓 <b>{{ detail.metrics.position_count ?? '—' }}</b> 笔</span>
          <span>{{ detail.metrics.snapshot_node_count < detail.metrics.node_count ? '等待节点上报完整账户快照' : '仅统计本人关联节点，包含本人主节点' }}</span>
        </div>
      </section>

      <section class="rd-panel rd-profit">
        <h2>近 7 日收益（U）</h2>
        <div v-if="detail.daily_profit.length" class="rd-chart" role="img" :aria-label="detail.daily_profit.map((point) => `${point.date}：${money(point.profit, true)}`).join('；')">
          <div v-for="point in detail.daily_profit" :key="point.ts" class="rd-column">
            <div class="rd-bar-space"><div class="rd-bar" :class="{ negative: point.profit < 0, zero: point.profit === 0 }" :style="{ height: `${Math.max(1, Math.abs(point.profit) / chartMax * 100)}%` }"><span>{{ money(point.profit, true) }}</span></div></div>
            <span class="rd-date">{{ point.date }}</span>
          </div>
        </div>
        <p v-else class="rd-empty">收益数据暂不可用</p>
        <p class="rd-note">{{ hasDailyDeals ? '按北京时间自然日统计已平仓成交净收益' : '近 7 日暂无已平仓成交' }}</p>
      </section>

      <section class="rd-panel rd-controls">
        <h2>运行控制</h2>
        <div v-if="detail.can_control" class="rd-buttons">
          <button type="button" class="rd-pause" :disabled="busy" @click="changePaused(!detail.robot.ai_follow_paused)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="detail.robot.ai_follow_paused ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-2 15V7l7 5-7 5z' : 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 14H8V8h3v8zm5 0h-3V8h3v8z'"/></svg>{{ detail.robot.ai_follow_paused ? '启动机器人' : '暂停机器人' }}</button>
          <button v-if="detail.robot.editable_strategy_id" type="button" class="rd-adjust" :disabled="busy" @click="editorOpen = true"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 5h8v2H3V5zm12 0h6v2h-6V5zM3 17h4v2H3v-2zm8 0h10v2H11v-2zM3 11h12v2H3v-2zm16 0h2v2h-2v-2zM11 3h4v6h-4V3zm4 6h4v6h-4V9zM7 15h4v6H7v-6z"/></svg>调整参数</button>
          <button type="button" class="rd-stop" :disabled="busy || detail.robot.ai_follow_paused" @click="changePaused(true, true)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm4 14H8V8h8v8z"/></svg>停止机器人</button>
        </div>
        <button v-else-if="detail.robot.editable_strategy_id" type="button" class="rd-adjust" @click="editorOpen = true">调整参数</button>
        <p v-else class="rd-readonly">仅机器人主人可调整参数与控制运行，当前为只读详情。</p>
        <p v-if="detail.can_control" class="rd-note">暂停与停止均只停止新开仓，已有任务和持仓继续按原规则运行。</p>
        <div class="rd-results">
          <div><span>累计收益</span><strong :class="{ up: detail.performance.data_available && detail.performance.total_profit >= 0, down: detail.performance.data_available && detail.performance.total_profit < 0 }">{{ detail.performance.data_available ? `${money(detail.performance.total_profit, true)} U` : '—' }}</strong></div>
          <div><span>收益率</span><strong>{{ detail.metrics.profit_rate == null ? '—' : `${money(detail.metrics.profit_rate, true)}%` }}</strong></div>
        </div>
      </section>
    </template>
    <StrategyFormModal v-model="editorOpen" mode="edit" :strategy-id="detail?.robot.editable_strategy_id || ''" preset-template-id="tpl_1" :preset-symbol-locked="true" @saved="loadDetail" />
  </div>
</template>

<style scoped>
.robot-detail { display: flex; flex-direction: column; gap: 12px; color: #e6ebf4; font-size: 13px; }
.rd-panel { padding: 12px; border: 1px solid #1e293b; border-radius: 16px; background: #101827; }
.rd-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.rd-icon { display: grid; width: 38px; height: 38px; flex-shrink: 0; place-items: center; border-radius: 12px; color: #3b82f6; background: #112140; }
.rd-icon svg { width: 22px; height: 22px; }
.rd-name { min-width: 0; flex: 1; }
.rd-name h1 { margin: 0 0 3px; font-size: 14px; font-weight: 700; overflow-wrap: anywhere; }
.rd-name p { margin: 0; color: #64748b; font-size: 11px; }
.rd-status { display: flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 99px; color: #16c784; background: #12362d; font-size: 11px; white-space: nowrap; }
.rd-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.rd-status.paused { color: #f0b90b; background: #352e19; }
.rd-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.rd-metrics > div { padding: 10px; border: 1px solid #23314b; border-radius: 9px; background: #0b1220; }
.rd-metrics span { display: block; margin-bottom: 6px; color: #64748b; font-size: 11px; }
.rd-metrics strong { font-size: 14px; overflow-wrap: anywhere; }
.rd-metrics small { font-size: 11px; }
.rd-summary { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 9px; color: #64748b; font-size: 10px; }
.rd-summary b { color: #cbd5e1; }
.rd-summary > span:last-child { color: #536078; }
.robot-detail .up { color: #16c784; }
.robot-detail .down { color: #f6465d; }
.rd-panel h2 { margin: 0 0 12px; padding-left: 7px; border-left: 2px solid #2563eb; font-size: 12px; font-weight: 600; }
.rd-chart { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 7px; padding-top: 23px; }
.rd-column { display: flex; min-width: 0; flex-direction: column; gap: 6px; text-align: center; }
.rd-bar-space { display: flex; height: 66px; align-items: flex-end; border-bottom: 1px solid #25314a; }
.rd-bar { position: relative; width: 100%; border-radius: 4px 4px 0 0; color: #16c784; background: #16a34a; }
.rd-bar.negative { color: #f6465d; background: #c83d53; }
.rd-bar.zero { color: #64748b; background: #334155; }
.rd-bar span { position: absolute; bottom: calc(100% + 5px); left: 50%; transform: translateX(-50%); font-size: 10px; white-space: nowrap; }
.rd-date { color: #64748b; font-size: 10px; }
.rd-note { margin: 8px 0 0; color: #64748b; font-size: 10px; line-height: 1.6; }
.rd-buttons { display: flex; flex-direction: column; gap: 8px; }
.rd-controls button { display: flex; width: 100%; align-items: center; justify-content: center; gap: 5px; padding: 12px; border: 1px solid #26324a; border-radius: 12px; font-size: 12px; font-weight: 600; cursor: pointer; }
.rd-controls button:disabled { opacity: .45; cursor: not-allowed; }
.rd-controls button svg { width: 15px; height: 15px; }
.rd-pause { color: #f0b90b; border-color: #65501e !important; background: #2c281e; }
.rd-adjust { color: #94a3b8; background: #0b1220; }
.rd-adjust svg { width: 15px; height: 15px; }
.rd-stop { color: #f6465d; border-color: #632a3c !important; background: #291d2e; }
.rd-results { margin-top: 8px; padding: 9px; border: 1px solid #1e293b; border-radius: 8px; background: #0b1220; }
.rd-results > div { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; font-size: 11px; }
.rd-results span { color: #64748b; }
.rd-readonly, .rd-empty { padding: 10px; color: #64748b; text-align: center; }
.rd-error p { color: #f6465d; }
.rd-error button { margin-right: 8px; padding: 7px 12px; border: 1px solid #26324a; border-radius: 8px; color: #cbd5e1; background: #0b1220; cursor: pointer; }
:global(.app-shell.is-customer:has(.robot-detail) .brand-back-ai) { width: 28px; height: 28px; padding: 0; margin-right: 4px; flex-shrink: 0; }
:global(.app-shell.is-customer:has(.robot-detail) .brand-back-ai svg) { width: 19px; height: 19px; }
@media (max-width: 480px) {
  .rd-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rd-heading { align-items: flex-start; }
  .rd-status { padding: 4px 7px; }
  .rd-chart { gap: 4px; }
  .rd-bar span { font-size: 9px; }
}
</style>
