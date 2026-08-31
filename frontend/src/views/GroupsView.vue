<script setup lang="ts">
// 分组管理页：strategy 信号（Webhook model=strategy）的分发单元
// 新建/编辑/删除分组、启停、维护成员节点、设置分组级分发模式；信号明细见 GroupSignalsView
import { computed, defineAsyncComponent, onMounted, ref, reactive, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, ElTable, ElTableColumn } from 'element-plus'
import { vLoading } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/table/style/css'
import 'element-plus/es/components/loading/style/css'
import 'element-plus/es/components/tooltip/style/css'
import FormLabel from '@/components/FormLabel.vue'
import LimitWatchLogCell from '@/components/LimitWatchLogCell.vue'
import ManualStrategyTrigger from '@/components/ManualStrategyTrigger.vue'
import { useHubStore } from '@/stores/hub'
import type {
  GroupDispatchMode,
  GroupOut,
  LimitWatchLogOut,
  NodeOut,
  StrategyOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const StrategyFormModal = defineAsyncComponent(() => import('@/components/StrategyFormModal.vue'))

const hub = useHubStore()
const router = useRouter()

const searchQuery = ref('')
const appliedQuery = ref('')
const loading = ref(false)

function currentSearchOptions(): { q?: string } {
  return appliedQuery.value ? { q: appliedQuery.value } : {}
}

async function loadGroups(): Promise<void> {
  loading.value = true
  try {
    await hub.fetchGroups(currentSearchOptions())
  } finally {
    loading.value = false
  }
}

async function runSearch(): Promise<void> {
  appliedQuery.value = searchQuery.value.trim()
  await loadGroups()
}

onMounted(async () => {
  await Promise.all([loadGroups(), hub.fetchNodes(), hub.fetchStrategies()])
})

const DISPATCH_MODE_LABEL: Record<GroupDispatchMode, string> = {
  sync: '全员同步',
  poll: '轮询轮转（单节点领取）',
}

const FIELD_HELP = {
  name: '分组名称，全局唯一。会展示在信号明细与操作审计中，便于追溯是哪个分组下发的订单。',
  enabled:
    '禁用后该分组不再接收任何 strategy 信号；已下发的历史任务不受影响。',
  dispatch_mode:
    '分组级分发模式，作用于整个分组、不区分币种：全员同步 = 组内所有有效节点并发下发；轮询轮转 = 一条信号只交给组内队首的一个有效节点，成功后该节点移到队尾。',
  trend_risk:
    '开启后，开仓信号进入各节点前会按「趋势面板」全局参数计算该节点上信号品种的趋势：' +
    'BUY 仅多头放行、SELL 仅空头放行；中性、数据不足或行情读取失败一律拦截并记入子任务跳过原因。CLOSE 不受影响。默认关闭。',
  limit_watch:
    '仅绑定趋势策略（模版2）的分组可用，默认关闭。开启后监听组内节点 MT5 上手动挂的限价单：' +
    '注释包含关键字即视为触发单（关键字可留空，表示不限注释）；参数须与「手动触发策略信号」的限价开仓规则一致（手数、止损、挂单价齐全），' +
    '合格则撤掉该挂单并按手动触发同构发给本分组；同一节点命中多个已开监听的趋势分组时发给全部命中分组。' +
    '策略托管单（带魔术号）不会被误撤。留空时组内所有手工限价单都会被扫描，请谨慎。',
  strategy:
    '一对一绑定交易策略。每个分组最多绑定一个策略，同一策略也不能挂到多个分组。' +
    '未绑定不影响分组本身的信号分发；可稍后在编辑中补绑或换绑。',
  remark: '备注，仅用于后台展示。',
  nodes:
    '加入本分组的节点。信号进入时只有「已启用且在线」的成员才算有效节点；勾选顺序即轮询轮转的初始顺序。',
}

// ---- 新建 / 编辑 ----
const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const saving = ref(false)
const formError = ref('')

const form = reactive({
  name: '',
  enabled: true,
  dispatch_mode: 'sync' as GroupDispatchMode,
  trend_risk_enabled: false,
  limit_watch_enabled: false,
  limit_watch_keyword: 'limit',
  strategy_id: '' as string,
  remark: '',
  node_ids: [] as string[],
})

/** 已被其它分组占用的策略 ID（当前编辑分组自己的绑定除外） */
const occupiedStrategyIds = computed(() => {
  const taken = new Set<string>()
  for (const g of hub.groups) {
    if (!g.strategy_id) continue
    if (formMode.value === 'edit' && g.group_id === editingId.value) continue
    taken.add(g.strategy_id)
  }
  return taken
})

/** 可选策略：未占用的 + 当前已绑定的 */
const selectableStrategies = computed<StrategyOut[]>(() => {
  const taken = occupiedStrategyIds.value
  return hub.strategies.filter(
    (s) => !taken.has(s.strategy_id) || s.strategy_id === form.strategy_id,
  )
})

function strategyLabel(s: StrategyOut): string {
  const status = s.enabled ? '' : '（已禁用）'
  return `${s.name} · ${s.symbol}${status}`
}

const formBoundStrategy = computed(() =>
  hub.strategies.find((s) => s.strategy_id === form.strategy_id),
)
const formIsTrendStrategy = computed(() => formBoundStrategy.value?.template_id === 'tpl_2')

watch(
  () => form.strategy_id,
  () => {
    if (!formIsTrendStrategy.value) form.limit_watch_enabled = false
  },
)

// 成员选择：已选节点按选择顺序排列（即轮询顺序），其余节点排在后面
const memberNodes = computed<NodeOut[]>(() =>
  form.node_ids
    .map((id) => hub.nodes.find((n) => n.node_id === id))
    .filter((n): n is NodeOut => !!n),
)
const availableNodes = computed<NodeOut[]>(() =>
  hub.nodes.filter((n) => !form.node_ids.includes(n.node_id)),
)

function addMember(nodeId: string): void {
  const id = (nodeId || '').trim()
  if (!id || form.node_ids.includes(id)) return
  // 重新赋值，保证列表与下拉「可选节点」立刻同步刷新
  form.node_ids = [...form.node_ids, id]
}
function removeMember(nodeId: string): void {
  form.node_ids = form.node_ids.filter((id) => id !== nodeId)
}
function moveMember(index: number, delta: number): void {
  const next = index + delta
  if (next < 0 || next >= form.node_ids.length) return
  const list = [...form.node_ids]
  ;[list[index], list[next]] = [list[next], list[index]]
  form.node_ids = list
}

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  formError.value = ''
  Object.assign(form, {
    name: '',
    enabled: true,
    dispatch_mode: 'sync' as GroupDispatchMode,
    trend_risk_enabled: false,
    limit_watch_enabled: false,
    limit_watch_keyword: 'limit',
    strategy_id: '',
    remark: '',
    node_ids: [],
  })
  void hub.fetchStrategies()
  showForm.value = true
}

function openEdit(g: GroupOut): void {
  formMode.value = 'edit'
  editingId.value = g.group_id
  formError.value = ''
  Object.assign(form, {
    name: g.name,
    enabled: g.enabled,
    dispatch_mode: g.dispatch_mode,
    trend_risk_enabled: Boolean(g.trend_risk_enabled),
    limit_watch_enabled: Boolean(g.limit_watch_enabled),
    limit_watch_keyword: g.limit_watch_keyword ?? '',
    strategy_id: g.strategy_id || '',
    remark: g.remark || '',
    node_ids: g.nodes.map((n) => n.node_id),
  })
  void hub.fetchStrategies()
  showForm.value = true
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) {
    formError.value = '请填写分组名称'
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const strategyId = form.strategy_id.trim() || null
    const payload = {
      name,
      enabled: form.enabled,
      dispatch_mode: form.dispatch_mode,
      trend_risk_enabled: form.trend_risk_enabled,
      limit_watch_enabled: formIsTrendStrategy.value ? form.limit_watch_enabled : false,
      limit_watch_keyword: (form.limit_watch_keyword || '').trim(),
      strategy_id: strategyId,
      remark: form.remark.trim() || null,
      node_ids: form.node_ids,
    }
    const styName =
      selectableStrategies.value.find((s) => s.strategy_id === strategyId)?.name
      || strategyId
      || '未绑定'
    const summary =
      `分发模式：${DISPATCH_MODE_LABEL[form.dispatch_mode]}\n` +
      `趋势风控：${form.trend_risk_enabled ? '开启' : '关闭'}\n` +
      (formIsTrendStrategy.value
        ? `限价监听：${form.limit_watch_enabled ? `开启（${watchKeywordLabel(form.limit_watch_keyword)}）` : '关闭'}\n`
        : '') +
      `绑定策略：${styName}\n` +
      `成员节点：${form.node_ids.length} 个`
    const verb = formMode.value === 'create' ? '创建' : '更新'
    if (!(await confirmAction(`确认${verb}分组「${name}」？\n\n${summary}`))) return
    try {
      if (formMode.value === 'create') {
        await hub.createGroup(payload, currentSearchOptions())
      } else {
        await hub.updateGroup(editingId.value, payload, currentSearchOptions())
      }
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } }
      formError.value = err?.response?.data?.detail || `${verb}失败，请稍后重试`
      await ElMessageBox.alert(formError.value, '无法保存', {
        type: 'warning',
        confirmButtonText: '知道了',
      })
      return
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(g: GroupOut): Promise<void> {
  const next = g.enabled ? '禁用' : '启用'
  const effect = g.enabled ? '不再接收任何 strategy 信号' : '恢复接收 strategy 信号'
  if (!(await confirmAction(`确认${next}分组「${g.name}」？\n\n${next}后该分组将${effect}。`))) return
  await hub.updateGroup(g.group_id, { enabled: !g.enabled }, currentSearchOptions())
}

async function toggleTrendRisk(g: GroupOut): Promise<void> {
  const next = g.trend_risk_enabled ? '关闭' : '开启'
  const effect = g.trend_risk_enabled
    ? '开仓信号不再按趋势面板全局参数拦截'
    : '开仓信号将按趋势面板全局参数对各节点算趋势：BUY 仅多头放行、SELL 仅空头放行；中性/数据不足/行情失败一律拦截。CLOSE 不受影响'
  if (!(await confirmAction(`确认${next}分组「${g.name}」的趋势风控？\n\n${next}后：${effect}。`))) return
  await hub.updateGroup(
    g.group_id,
    { trend_risk_enabled: !g.trend_risk_enabled },
    currentSearchOptions(),
  )
}

function isTrendGroup(g: GroupOut): boolean {
  const sty = g.strategy_id ? hub.strategies.find((s) => s.strategy_id === g.strategy_id) : undefined
  return sty?.template_id === 'tpl_2'
}

function watchKeywordLabel(kw: string | null | undefined): string {
  const text = String(kw ?? '').trim()
  return text || '不限注释'
}

function limitWatchButtonTitle(g: GroupOut): string {
  if (!g.limit_watch_enabled) return FIELD_HELP.limit_watch
  return `已开启 · ${watchKeywordLabel(g.limit_watch_keyword)}`
}

const showWatchLogColumn = computed(() => hub.groups.some((g) => g.limit_watch_enabled))
const emptyTableText = computed(() =>
  appliedQuery.value ? '无匹配分组' : '暂无分组，点击右上角「新建分组」开始配置',
)

function asGroup(row: unknown): GroupOut {
  return row as GroupOut
}

function watchLogsOf(g: GroupOut): LimitWatchLogOut[] {
  return hub.limitWatchLogs[g.group_id] || g.limit_watch_logs || []
}

const watchLogGroup = ref<GroupOut | null>(null)
const watchLogItems = ref<LimitWatchLogOut[]>([])
const watchLogTotal = ref(0)
const watchLogPage = ref(1)
const watchLogPageSize = 50
const watchLogLoading = ref(false)

const expandedWatchLogId = ref<number | null>(null)

function watchLogEventTag(event: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    ok: { cls: 'green', text: '已触发' },
    rejected: { cls: 'red', text: '未通过' },
    cancel_failed: { cls: 'red', text: '撤单失败' },
    dispatch_rejected: { cls: 'amber', text: '未下发' },
    duplicate: { cls: 'amber', text: '重复' },
  }
  return m[event] || { cls: '', text: event }
}

function fmtWatchTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function fmtWatchNum(n: number | null | undefined): string {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return String(n)
}

function watchLogNodeName(nodeId: string): string {
  return hub.nodes.find((n) => n.node_id === nodeId)?.name || nodeId || '—'
}

function watchLogReason(row: LimitWatchLogOut): string {
  const msg = (row.message || '').trim()
  const idx = msg.indexOf('：')
  if (idx >= 0) {
    const rest = msg.slice(idx + 1).trim()
    if (rest) return rest
  }
  return ''
}

function watchLogSummary(row: LimitWatchLogOut): string {
  const reason = watchLogReason(row)
  if (row.event === 'ok') return '挂单已转成策略信号'
  if (row.event === 'rejected') return reason || '未通过检查，挂单仍保留'
  if (row.event === 'cancel_failed') return reason || '没能撤掉这张挂单'
  if (row.event === 'dispatch_rejected') return reason || '挂单已撤，信号没有发出'
  if (row.event === 'duplicate') return '挂单已撤，相同信号刚发过，没有再发'
  return reason || '—'
}

function watchLogSignalId(row: LimitWatchLogOut): string {
  const sid = row.detail?.signal_id
  if (typeof sid === 'string' && sid.trim()) return sid.trim()
  return ''
}

function canJumpWatchLogSignal(row: LimitWatchLogOut): boolean {
  return row.event === 'ok' && !!watchLogSignalId(row)
}

function openWatchLogSignal(row: LimitWatchLogOut, e?: Event): void {
  e?.preventDefault()
  e?.stopPropagation()
  const sid = watchLogSignalId(row)
  const gid = watchLogGroup.value?.group_id
  if (!sid || !gid) return
  closeWatchLogs()
  void router.push({
    name: 'group-signals',
    params: { id: gid },
    query: { signal_id: sid },
  })
}

function toggleWatchLogRow(id: number): void {
  expandedWatchLogId.value = expandedWatchLogId.value === id ? null : id
}

async function loadWatchLogs(): Promise<void> {
  const g = watchLogGroup.value
  if (!g) return
  watchLogLoading.value = true
  try {
    const res = await hub.fetchLimitWatchLogs(g.group_id, watchLogPage.value, watchLogPageSize)
    const live = hub.limitWatchLogs[g.group_id] || []
    watchLogItems.value = watchLogPage.value === 1
      ? hub.mergeLimitWatchLogItems(live, res.items)
      : res.items
    watchLogTotal.value = Math.max(res.total, watchLogItems.value.length)
    if (res.page !== watchLogPage.value) watchLogPage.value = res.page
  } finally {
    watchLogLoading.value = false
  }
}

function openWatchLogs(g: GroupOut): void {
  watchLogGroup.value = g
  watchLogPage.value = 1
  expandedWatchLogId.value = null
  void loadWatchLogs()
}

function closeWatchLogs(): void {
  watchLogGroup.value = null
  watchLogItems.value = []
  watchLogTotal.value = 0
  expandedWatchLogId.value = null
}

const watchLogTotalPages = computed(() =>
  Math.max(1, Math.ceil(watchLogTotal.value / watchLogPageSize)),
)

function goWatchLogPage(next: number): void {
  const p = Math.min(Math.max(1, next), watchLogTotalPages.value)
  if (p === watchLogPage.value) return
  watchLogPage.value = p
  expandedWatchLogId.value = null
  void loadWatchLogs()
}

watch(
  () => {
    const gid = watchLogGroup.value?.group_id
    return gid ? hub.limitWatchLogs[gid] : undefined
  },
  (live) => {
    if (!watchLogGroup.value || watchLogPage.value !== 1 || !live?.length) return
    watchLogItems.value = hub.mergeLimitWatchLogItems(live, watchLogItems.value)
    if (watchLogItems.value.length > watchLogTotal.value) {
      watchLogTotal.value = watchLogItems.value.length
    }
  },
)

async function toggleLimitWatch(g: GroupOut): Promise<void> {
  if (!isTrendGroup(g)) {
    await ElMessageBox.alert('限价单监听仅适用于绑定趋势策略的分组', '无法切换', {
      type: 'warning',
      confirmButtonText: '知道了',
    })
    return
  }
  const next = g.limit_watch_enabled ? '关闭' : '开启'
  const keywordHint = watchKeywordLabel(g.limit_watch_keyword)
  const effect = g.limit_watch_enabled
    ? '不再把组内节点 MT5 上手动挂的限价单转成策略信号'
    : `将监听组内节点 MT5 上手动挂的限价单（${keywordHint === '不限注释' ? '不限注释' : `注释含「${keywordHint}」`}），合格则撤单并按手动触发同构发给本分组`
  if (!(await confirmAction(`确认${next}分组「${g.name}」的限价监听？\n\n${next}后：${effect}。`))) return
  await hub.updateGroup(
    g.group_id,
    { limit_watch_enabled: !g.limit_watch_enabled },
    currentSearchOptions(),
  )
}

async function remove(g: GroupOut): Promise<void> {
  if (!(await confirmAction(`确认删除分组「${g.name}」？\n\n该操作不可恢复（历史信号任务记录会保留）。`, '确认删除'))) return
  await hub.deleteGroup(g.group_id, currentSearchOptions())
}

const closingGroupIds = ref<Record<string, boolean>>({})

async function closeGroup(g: GroupOut): Promise<void> {
  if (closingGroupIds.value[g.group_id]) return
  const n = g.active_task_count
  const taskHint = n > 0
    ? `当前进行中主任务 ${n} 条。\n`
    : '当前列表显示没有进行中主任务；若仍有未收口子任务也会一并终止。\n'
  if (!(await confirmAction(
    `确认对分组「${g.name}」一键平仓？\n\n`
      + '将终止该分组全部未收口的策略任务，并平掉对应魔术号的持仓。\n'
      + '不影响其它分组，也不会做账户级全平。\n'
      + taskHint
      + '此操作不可撤销。',
    '确认平仓',
  ))) {
    return
  }
  closingGroupIds.value = { ...closingGroupIds.value, [g.group_id]: true }
  try {
    const res = await hub.closeGroup(g.group_id)
    if (res.status === 'skipped') {
      ElMessage.warning(res.reason || '该分组没有进行中的策略任务')
      hub.pushEvent(`分组「${g.name}」一键平仓：无进行中任务`, 'warn')
    } else if (res.status === 'closing') {
      const forced = res.forced || 0
      const msg = `已向 ${res.targets} 个节点任务下发平仓终止`
      if (forced) {
        ElMessage.warning(`${msg}；${forced} 个节点离线，已强制结束并排队待重连补发`)
        hub.pushEvent(`分组「${g.name}」一键平仓：${msg}（${forced} 离线）`, 'warn')
      } else {
        ElMessage.success(msg)
        hub.pushEvent(`分组「${g.name}」一键平仓：${msg}`, 'ok')
      }
    } else {
      ElMessage.warning(res.reason || '目标节点均离线，已强制结束子任务，待重连后补发平仓')
      hub.pushEvent(`分组「${g.name}」一键平仓：节点离线`, 'warn')
    }
    await loadGroups()
  } catch (e: unknown) {
    const detail =
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      || '平仓下发失败'
    ElMessage.error(typeof detail === 'string' ? detail : '平仓下发失败')
  } finally {
    const next = { ...closingGroupIds.value }
    delete next[g.group_id]
    closingGroupIds.value = next
  }
}

const PURGE_CONFIRM_TEXT = '清空交易记录'
const purging = ref(false)

async function purgeTradeLogs(): Promise<void> {
  if (!(await confirmAction(
    '确认清空全部交易记录？\n\n'
      + '将删除：信号历史、按币种分发明细、分组策略主任务 / 节点子任务 / 事件流、限价监听日志，并清理相关运行态缓存。\n'
      + '分组、策略、节点配置与操作审计不受影响。\n\n'
      + '此操作不可恢复。',
    '清空交易记录',
  ))) {
    return
  }
  try {
    const { value } = await ElMessageBox.prompt(
      `请输入「${PURGE_CONFIRM_TEXT}」以确认`,
      '二次确认',
      {
        confirmButtonText: '确认清空',
        cancelButtonText: '取消',
        inputPattern: new RegExp(`^${PURGE_CONFIRM_TEXT}$`),
        inputErrorMessage: `请输入：${PURGE_CONFIRM_TEXT}`,
        type: 'warning',
        closeOnClickModal: false,
      },
    )
    if (value !== PURGE_CONFIRM_TEXT) return
  } catch {
    return
  }

  purging.value = true
  try {
    const res = await hub.purgeTradeLogs(PURGE_CONFIRM_TEXT)
    const parts = [`已清空交易记录（共 ${res.total_deleted} 条）`]
    if (res.strategies_stopped) parts.push(`已终止 ${res.strategies_stopped} 个进行中的策略任务`)
    if (res.strategies_unreachable) {
      parts.push(`${res.strategies_unreachable} 个任务所在节点离线，请人工确认其 MT5 持仓`)
    }
    if (res.strategies_unreachable) ElMessage.warning(parts.join('；'))
    else ElMessage.success(parts.join('；'))
    await loadGroups()
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    ElMessage.error(err?.response?.data?.detail || err?.message || '清空失败，请稍后重试')
  } finally {
    purging.value = false
  }
}

function openSignals(g: GroupOut): void {
  router.push({ name: 'group-signals', params: { id: g.group_id } })
}

function openActiveSignals(g: GroupOut): void {
  router.push({
    name: 'group-signals',
    params: { id: g.group_id },
    query: { status: 'active' },
  })
}

const showStrategyForm = ref(false)
const editingStrategyId = ref('')

function openGroupStrategyEdit(g: GroupOut): void {
  if (!g.strategy_id) {
    ElMessage.warning('该分组未绑定策略')
    return
  }
  editingStrategyId.value = g.strategy_id
  showStrategyForm.value = true
}

async function onStrategyFormSaved(): Promise<void> {
  await Promise.all([hub.fetchStrategies(), loadGroups()])
}
</script>

<template>
  <div class="groups-page">
    <div class="row between page-header">
      <div>
        <div class="h1">分组管理</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          分组是 strategy 信号的分发单元：Webhook 携带 <code>model=strategy</code> 时按分组分发，
          与按币种分发（<code>model=normal</code>，默认）的规则完全隔离
        </p>
      </div>
      <div class="row" style="gap: 8px">
        <button class="btn-ghost" @click="router.push('/strategies')">策略管理</button>
        <ManualStrategyTrigger @accepted="loadGroups" />
        <button class="btn-danger" :disabled="purging" @click="purgeTradeLogs">
          {{ purging ? '清空中…' : '清空交易记录' }}
        </button>
        <button class="btn-primary" @click="openCreate">+ 新建分组</button>
      </div>
    </div>

    <div class="card card-pad" style="margin-bottom: 12px">
      <div class="row" style="gap: 8px">
        <input
          v-model="searchQuery"
          type="search"
          placeholder="搜索分组名称…"
          aria-label="搜索分组名称"
          :disabled="loading"
          style="width: 25%"
          @keydown.enter="runSearch"
        />
        <button class="btn-primary btn-sm" :disabled="loading" @click="runSearch">
          {{ loading ? '搜索中…' : '搜索' }}
        </button>
      </div>
      <p v-if="appliedQuery" class="muted" style="font-size: 12px; margin: 8px 0 0">
        {{ hub.groups.length ? `找到 ${hub.groups.length} 个分组` : '无匹配分组' }}
      </p>
    </div>

    <!-- 移动端卡片 -->
    <div class="list-cards mobile-only">
      <div v-for="g in hub.groups" :key="g.group_id" class="list-card card">
        <div class="list-card-head row between">
          <strong>{{ g.name }}</strong>
          <span class="tag" :class="g.enabled ? 'green' : ''">{{ g.enabled ? '已启用' : '已禁用' }}</span>
        </div>
        <div class="list-field"><span class="k">分组 ID</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ g.group_id }}</span></div>
        <div class="list-field">
          <span class="k">绑定策略</span>
          <span class="v">{{ g.strategy_name || '未绑定' }}</span>
        </div>
        <div class="list-field">
          <span class="k">分发模式</span>
          <span class="v"><span class="tag blue">{{ DISPATCH_MODE_LABEL[g.dispatch_mode] }}</span></span>
        </div>
        <div class="list-field">
          <span class="k">趋势风控</span>
          <span class="v">
            <button
              class="btn-sm"
              :class="g.trend_risk_enabled ? 'btn-success' : 'btn-danger'"
              :title="FIELD_HELP.trend_risk"
              @click="toggleTrendRisk(g)"
            >
              {{ g.trend_risk_enabled ? '已开启' : '已关闭' }}
            </button>
          </span>
        </div>
        <div class="list-field">
          <span class="k">限价监听</span>
          <span class="v">
            <button
              v-if="isTrendGroup(g)"
              class="btn-sm"
              :class="g.limit_watch_enabled ? 'btn-success' : 'btn-danger'"
              :title="limitWatchButtonTitle(g)"
              @click="toggleLimitWatch(g)"
            >
              {{ g.limit_watch_enabled ? '已开启' : '已关闭' }}
            </button>
            <span v-else class="muted">—</span>
          </span>
        </div>
        <div v-if="g.limit_watch_enabled" class="list-field list-field-block">
          <span class="k">监听日志</span>
          <span class="v">
            <LimitWatchLogCell :logs="watchLogsOf(g)" @open="openWatchLogs(g)" />
          </span>
        </div>
        <div v-if="g.strategy_id" class="list-field">
          <span class="k">策略</span>
          <span class="v">
            <button class="btn-sm btn-success" @click="openGroupStrategyEdit(g)">编辑策略</button>
          </span>
        </div>
        <div class="list-field">
          <span class="k">信号</span>
          <span class="v">
            <button class="btn-sm btn-success" @click="openSignals(g)">{{ g.signal_count }} 条</button>
          </span>
        </div>
        <div class="list-field"><span class="k">成员节点</span><span class="v">{{ g.node_count }}</span></div>
        <div class="list-field"><span class="k">有效节点</span><span class="v">{{ g.online_node_count }}</span></div>
        <div class="list-field">
          <span class="k">进行中</span>
          <span class="v">
            <button
              v-if="g.active_task_count > 0"
              class="btn-sm btn-success"
              @click="openActiveSignals(g)"
            >{{ g.active_task_count }} 条</button>
            <span v-else class="muted">—</span>
          </span>
        </div>
        <div class="list-field"><span class="k">备注</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ g.remark || '—' }}</span></div>
        <div class="list-card-actions">
          <button class="btn-sm" :class="g.enabled ? 'btn-success' : 'btn-danger'" @click="toggleEnabled(g)">
            {{ g.enabled ? '禁用' : '启用' }}
          </button>
          <button
            class="btn-sm btn-danger"
            :disabled="!!closingGroupIds[g.group_id]"
            @click="closeGroup(g)"
          >
            {{ closingGroupIds[g.group_id] ? '下发中…' : '平仓' }}
          </button>
          <button class="btn-sm btn-ghost" @click="openEdit(g)">编辑</button>
          <button class="btn-sm btn-danger" @click="remove(g)">删除</button>
        </div>
      </div>
      <div v-if="!hub.groups.length && !loading" class="card card-pad muted">
        {{ appliedQuery ? '无匹配分组' : '暂无分组' }}
      </div>
    </div>

    <!-- 桌面端表格 -->
    <div class="card groups-table-card desktop-only">
      <el-table
        :key="showWatchLogColumn ? 'watch' : 'plain'"
        :data="hub.groups"
        row-key="group_id"
        size="small"
        class="groups-table"
        v-loading="loading"
        :empty-text="emptyTableText"
        style="width: 100%"
      >
        <el-table-column label="名称" min-width="150">
          <template #default="{ row }">
            {{ asGroup(row).name }}
            <div class="muted" style="font-size: 11px">{{ asGroup(row).group_id }}</div>
          </template>
        </el-table-column>
        <el-table-column label="绑定策略" min-width="140">
          <template #default="{ row }">
            <template v-if="asGroup(row).strategy_name">
              {{ asGroup(row).strategy_name }}
              <div v-if="asGroup(row).strategy_id" class="muted" style="font-size: 11px">{{ asGroup(row).strategy_id }}</div>
            </template>
            <span v-else class="muted">未绑定</span>
          </template>
        </el-table-column>
        <el-table-column label="分发模式" min-width="160">
          <template #default="{ row }">
            <span class="tag blue">{{ DISPATCH_MODE_LABEL[asGroup(row).dispatch_mode] }}</span>
          </template>
        </el-table-column>
        <el-table-column label="趋势风控" width="88">
          <template #default="{ row }">
            <button
              class="btn-sm"
              :class="asGroup(row).trend_risk_enabled ? 'btn-success' : 'btn-danger'"
              :title="FIELD_HELP.trend_risk"
              @click="toggleTrendRisk(asGroup(row))"
            >
              {{ asGroup(row).trend_risk_enabled ? '开启' : '关闭' }}
            </button>
          </template>
        </el-table-column>
        <el-table-column label="限价监听" width="88">
          <template #default="{ row }">
            <button
              v-if="isTrendGroup(asGroup(row))"
              class="btn-sm"
              :class="asGroup(row).limit_watch_enabled ? 'btn-success' : 'btn-danger'"
              :title="limitWatchButtonTitle(asGroup(row))"
              @click="toggleLimitWatch(asGroup(row))"
            >
              {{ asGroup(row).limit_watch_enabled ? '开启' : '关闭' }}
            </button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column
          v-if="showWatchLogColumn"
          label="监听日志"
          width="100"
          align="right"
        >
          <template #default="{ row }">
            <LimitWatchLogCell
              v-if="asGroup(row).limit_watch_enabled"
              :logs="watchLogsOf(asGroup(row))"
              @open="openWatchLogs(asGroup(row))"
            />
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="策略" min-width="110">
          <template #default="{ row }">
            <button
              v-if="asGroup(row).strategy_id"
              class="btn-sm btn-success"
              @click="openGroupStrategyEdit(asGroup(row))"
            >编辑策略</button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="信号" width="92" align="right">
          <template #default="{ row }">
            <button class="btn-sm btn-success" @click="openSignals(asGroup(row))">{{ asGroup(row).signal_count }} 条</button>
          </template>
        </el-table-column>
        <el-table-column label="成员节点" width="92" align="right">
          <template #default="{ row }">{{ asGroup(row).node_count }}</template>
        </el-table-column>
        <el-table-column label="有效节点" width="92" align="right">
          <template #default="{ row }">
            <span :class="asGroup(row).online_node_count ? '' : 'muted'">{{ asGroup(row).online_node_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="进行中" width="92" align="right">
          <template #default="{ row }">
            <button
              v-if="asGroup(row).active_task_count > 0"
              class="btn-sm btn-success"
              @click="openActiveSignals(asGroup(row))"
            >{{ asGroup(row).active_task_count }} 条</button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="muted" style="font-size: 12px">{{ asGroup(row).remark || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="108">
          <template #default="{ row }">
            <button
              class="btn-sm"
              :class="asGroup(row).enabled ? 'btn-success' : 'btn-danger'"
              @click="toggleEnabled(asGroup(row))"
            >
              {{ asGroup(row).enabled ? '已启用' : '已禁用' }}
            </button>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right" align="right">
          <template #default="{ row }">
            <div class="nowrap">
              <button
                class="btn-sm btn-danger"
                :disabled="!!closingGroupIds[asGroup(row).group_id]"
                @click="closeGroup(asGroup(row))"
              >
                {{ closingGroupIds[asGroup(row).group_id] ? '下发中…' : '平仓' }}
              </button>
              <button class="btn-sm btn-ghost" @click="openEdit(asGroup(row))">编辑</button>
              <button class="btn-sm btn-danger" @click="remove(asGroup(row))">删除</button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 限价监听日志 -->
    <div v-if="watchLogGroup" class="modal-mask" @click.self="closeWatchLogs">
      <div class="card card-pad modal modal-lg watch-log-modal">
        <div class="modal-header">
          <div class="h1">监听日志 · {{ watchLogGroup.name }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            节点挂上符合监听条件的限价单后，这里记下处理结果。点一行可看细节；「挂单已转成策略信号」可点进对应主任务。
          </p>
        </div>
        <div class="modal-body">
          <p v-if="watchLogLoading && !watchLogItems.length" class="muted">加载中…</p>
          <p v-else-if="!watchLogItems.length" class="muted">暂无监听记录</p>
          <template v-else>
            <p class="muted" style="font-size: 12px; margin: 0 0 8px">点击行查看细节；「挂单已转成策略信号」可跳到对应信号</p>
            <div class="list-cards mobile-only">
              <div
                v-for="row in watchLogItems"
                :key="row.id"
                class="list-card card clickable"
                @click="toggleWatchLogRow(row.id)"
              >
                <div class="list-card-head row between">
                  <span class="tag" :class="watchLogEventTag(row.event).cls">
                    {{ watchLogEventTag(row.event).text }}
                  </span>
                  <span class="muted" style="font-size: 12px">
                    {{ expandedWatchLogId === row.id ? '▾' : '▸' }}
                    {{ fmtWatchTime(row.ts) }}
                  </span>
                </div>
                <div class="list-field">
                  <span class="k">品种</span>
                  <span class="v">{{ row.symbol || '—' }}</span>
                </div>
                <div class="list-field">
                  <span class="k">方向</span>
                  <span class="v">
                    <span
                      v-if="row.action"
                      class="tag"
                      :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'blue' : ''"
                    >{{ row.action }}</span>
                    <span v-else class="muted">—</span>
                  </span>
                </div>
                <div class="list-field">
                  <span class="k">手数</span>
                  <span class="v">{{ fmtWatchNum(row.volume) }}</span>
                </div>
                <div class="list-field">
                  <span class="k">挂单价</span>
                  <span class="v">{{ fmtWatchNum(row.price) }}</span>
                </div>
                <div class="list-field">
                  <span class="k">说明</span>
                  <button
                    v-if="canJumpWatchLogSignal(row)"
                    type="button"
                    class="node-link watch-log-signal-link"
                    title="查看对应信号"
                    @click="openWatchLogSignal(row, $event)"
                  >{{ watchLogSummary(row) }}</button>
                  <span v-else class="v muted" style="font-size: 12px; font-weight: 500">{{ watchLogSummary(row) }}</span>
                </div>
                <div v-if="expandedWatchLogId === row.id" class="list-card-detail">
                  <div class="list-field">
                    <span class="k">节点</span>
                    <span class="v">{{ watchLogNodeName(row.node_id) }}</span>
                  </div>
                  <div class="list-field">
                    <span class="k">挂单号</span>
                    <span class="v">{{ row.ticket || '—' }}</span>
                  </div>
                  <div class="list-field">
                    <span class="k">止损价</span>
                    <span class="v">{{ fmtWatchNum(row.sl) }}</span>
                  </div>
                  <div class="list-field">
                    <span class="k">止盈价</span>
                    <span class="v">{{ fmtWatchNum(row.tp) }}</span>
                  </div>
                  <div class="list-field">
                    <span class="k">订单备注</span>
                    <span class="v">{{ row.comment || '—' }}</span>
                  </div>
                  <div v-if="watchLogReason(row)" class="list-field">
                    <span class="k">原因</span>
                    <span class="v">{{ watchLogReason(row) }}</span>
                  </div>
                  <div v-if="watchLogSignalId(row)" class="list-field">
                    <span class="k">信号编号</span>
                    <button
                      type="button"
                      class="node-link watch-log-signal-link"
                      title="查看对应信号"
                      @click="openWatchLogSignal(row, $event)"
                    >{{ watchLogSignalId(row) }}</button>
                  </div>
                </div>
              </div>
            </div>
            <div class="watch-log-table-wrap desktop-only">
              <table class="watch-log-table">
                <thead>
                  <tr>
                    <th class="col-expand"></th>
                    <th>时间</th>
                    <th>结果</th>
                    <th>品种</th>
                    <th>方向</th>
                    <th class="right">手数</th>
                    <th class="right">挂单价</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="row in watchLogItems" :key="row.id">
                    <tr class="clickable" @click="toggleWatchLogRow(row.id)">
                      <td class="muted col-expand">{{ expandedWatchLogId === row.id ? '▾' : '▸' }}</td>
                      <td class="muted col-time">{{ fmtWatchTime(row.ts) }}</td>
                      <td>
                        <span class="tag" :class="watchLogEventTag(row.event).cls">
                          {{ watchLogEventTag(row.event).text }}
                        </span>
                      </td>
                      <td>{{ row.symbol || '—' }}</td>
                      <td>
                        <span
                          v-if="row.action"
                          class="tag"
                          :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'blue' : ''"
                        >{{ row.action }}</span>
                        <span v-else class="muted">—</span>
                      </td>
                      <td class="right">{{ fmtWatchNum(row.volume) }}</td>
                      <td class="right">{{ fmtWatchNum(row.price) }}</td>
                      <td
                        :class="{ 'watch-log-jump': canJumpWatchLogSignal(row) }"
                        @click="canJumpWatchLogSignal(row) ? openWatchLogSignal(row, $event) : undefined"
                      >
                        <button
                          v-if="canJumpWatchLogSignal(row)"
                          type="button"
                          class="node-link watch-log-signal-link"
                          title="查看对应信号"
                          @click="openWatchLogSignal(row, $event)"
                        >{{ watchLogSummary(row) }}</button>
                        <template v-else>{{ watchLogSummary(row) }}</template>
                      </td>
                    </tr>
                    <tr v-if="expandedWatchLogId === row.id" class="watch-log-detail-row">
                      <td colspan="8">
                        <div class="watch-log-detail grid">
                          <div class="kv">
                            <span class="k">节点</span>
                            <span class="v">{{ watchLogNodeName(row.node_id) }}</span>
                          </div>
                          <div class="kv">
                            <span class="k">挂单号</span>
                            <span class="v">{{ row.ticket || '—' }}</span>
                          </div>
                          <div class="kv">
                            <span class="k">止损价</span>
                            <span class="v">{{ fmtWatchNum(row.sl) }}</span>
                          </div>
                          <div class="kv">
                            <span class="k">止盈价</span>
                            <span class="v">{{ fmtWatchNum(row.tp) }}</span>
                          </div>
                          <div class="kv">
                            <span class="k">订单备注</span>
                            <span class="v">{{ row.comment || '—' }}</span>
                          </div>
                          <div v-if="watchLogReason(row)" class="kv">
                            <span class="k">原因</span>
                            <span class="v">{{ watchLogReason(row) }}</span>
                          </div>
                          <div v-if="watchLogSignalId(row)" class="kv">
                            <span class="k">信号编号</span>
                            <button
                              type="button"
                              class="node-link watch-log-signal-link"
                              title="查看对应信号"
                              @click="openWatchLogSignal(row, $event)"
                            >{{ watchLogSignalId(row) }}</button>
                          </div>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </template>
        </div>
        <div class="modal-footer row between">
          <span class="muted" style="font-size: 12px">
            共 {{ watchLogTotal }} 条
            <template v-if="watchLogTotalPages > 1">
              · 第 {{ watchLogPage }} / {{ watchLogTotalPages }} 页
            </template>
          </span>
          <div class="row" style="gap: 8px">
            <button
              v-if="watchLogTotalPages > 1"
              class="btn-sm btn-ghost"
              :disabled="watchLogPage <= 1 || watchLogLoading"
              @click="goWatchLogPage(watchLogPage - 1)"
            >上一页</button>
            <button
              v-if="watchLogTotalPages > 1"
              class="btn-sm btn-ghost"
              :disabled="watchLogPage >= watchLogTotalPages || watchLogLoading"
              @click="goWatchLogPage(watchLogPage + 1)"
            >下一页</button>
            <button class="btn-ghost" @click="closeWatchLogs">关闭</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建 / 编辑弹窗 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="card card-pad modal modal-lg group-form-modal">
        <div class="modal-header">
          <div class="h1">{{ formMode === 'create' ? '新建分组' : '编辑分组' }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            可一对一绑定交易策略；分发模式作用于整个分组、不区分币种；信号进入时只有「已启用且在线」的成员节点才会被下发。
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid two">
            <div>
              <FormLabel field-id="group-name" text="分组名称" :help="FIELD_HELP.name" />
              <input id="group-name" v-model="form.name" placeholder="例如：黄金策略组" />
            </div>
            <div>
              <FormLabel field-id="group-strategy" text="绑定策略" :help="FIELD_HELP.strategy" />
              <select id="group-strategy" v-model="form.strategy_id">
                <option value="">不绑定</option>
                <option
                  v-for="s in selectableStrategies"
                  :key="s.strategy_id"
                  :value="s.strategy_id"
                >
                  {{ strategyLabel(s) }}
                </option>
              </select>
              <p v-if="!hub.strategies.length" class="muted" style="font-size: 12px; margin-top: 6px">
                暂无策略，可先到
                <button type="button" class="btn-sm btn-ghost" @click="router.push('/strategies')">策略管理</button>
                创建
              </p>
            </div>
            <div>
              <FormLabel field-id="group-mode" text="分发模式" :help="FIELD_HELP.dispatch_mode" />
              <select id="group-mode" v-model="form.dispatch_mode">
                <option value="sync">全员同步</option>
                <option value="poll">轮询轮转（单节点领取）</option>
              </select>
            </div>
            <div>
              <FormLabel field-id="group-enabled" text="启用状态" :help="FIELD_HELP.enabled" />
              <select id="group-enabled" v-model="form.enabled">
                <option :value="true">启用</option>
                <option :value="false">禁用</option>
              </select>
            </div>
            <div>
              <FormLabel field-id="group-trend-risk" text="趋势风控" :help="FIELD_HELP.trend_risk" />
              <select id="group-trend-risk" v-model="form.trend_risk_enabled">
                <option :value="false">关闭</option>
                <option :value="true">开启</option>
              </select>
            </div>
            <div v-if="formIsTrendStrategy">
              <FormLabel field-id="group-limit-watch" text="限价监听" :help="FIELD_HELP.limit_watch" />
              <select id="group-limit-watch" v-model="form.limit_watch_enabled">
                <option :value="false">关闭</option>
                <option :value="true">开启</option>
              </select>
            </div>
            <div v-if="formIsTrendStrategy">
              <FormLabel
                field-id="group-limit-watch-keyword"
                text="监听关键字"
                help="订单注释包含该关键字即视为触发单，大小写不敏感。留空表示不限注释（组内所有手工限价单都会被扫描）。"
              />
              <input
                id="group-limit-watch-keyword"
                v-model="form.limit_watch_keyword"
                maxlength="32"
                placeholder="留空则不限注释"
              />
            </div>
            <div class="span-full">
              <FormLabel field-id="group-remark" text="备注" :help="FIELD_HELP.remark" />
              <input id="group-remark" v-model="form.remark" placeholder="选填" />
            </div>

            <div class="span-full">
              <FormLabel text="成员节点" :help="FIELD_HELP.nodes" />
              <div class="row" style="gap: 8px; margin-bottom: 10px">
                <!-- 占位项不可 disabled，否则浏览器会预选第一个节点且不再触发 change -->
                <select
                  :key="`member-pick-${form.node_ids.join(',')}`"
                  :value="''"
                  :disabled="!availableNodes.length"
                  @change="addMember(($event.target as HTMLSelectElement).value)"
                >
                  <option value="">
                    {{ availableNodes.length ? '选择要加入的节点…' : '所有节点均已加入' }}
                  </option>
                  <option v-for="n in availableNodes" :key="n.node_id" :value="n.node_id">
                    {{ n.name }}（{{ n.mt5_login || '—' }}）
                  </option>
                </select>
              </div>
              <div v-if="!memberNodes.length" class="muted" style="font-size: 12px">
                尚未加入任何节点。分组无有效节点时不会处理 strategy 信号。
              </div>
              <div v-else class="member-list">
                <div v-for="(n, i) in memberNodes" :key="n.node_id" class="member-row">
                  <span class="member-order">{{ i + 1 }}</span>
                  <span class="dot" :class="hub.statuses[n.node_id] || n.status"></span>
                  <span class="member-name">{{ n.name }}</span>
                  <span class="muted member-meta">{{ n.mt5_login || '—' }}</span>
                  <span class="tag" :class="n.enabled ? 'green' : ''">{{ n.enabled ? '已启用' : '已禁用' }}</span>
                  <span class="member-actions">
                    <button class="btn-sm btn-ghost" :disabled="i === 0" @click="moveMember(i, -1)">↑</button>
                    <button class="btn-sm btn-ghost" :disabled="i === memberNodes.length - 1" @click="moveMember(i, 1)">↓</button>
                    <button class="btn-sm btn-danger" @click="removeMember(n.node_id)">移出</button>
                  </span>
                </div>
              </div>
            </div>

            <div v-if="formError" class="span-full" style="color: var(--red); font-size: 13px">{{ formError }}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showForm = false">取消</button>
          <button class="btn-primary" :disabled="saving || !form.name.trim()" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <StrategyFormModal
      v-model="showStrategyForm"
      mode="edit"
      :strategy-id="editingStrategyId"
      @saved="onStrategyFormSaved"
    />
  </div>
</template>

<style scoped>
.groups-page {
  width: 100%;
  min-width: 0;
}

.nowrap {
  white-space: nowrap;
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.groups-table-card {
  padding: 0;
  overflow: hidden;
}
.groups-table {
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
  --el-table-header-bg-color: rgba(6, 10, 18, 0.55);
  --el-table-header-text-color: var(--muted);
  --el-table-text-color: var(--text);
  --el-table-border-color: var(--glass-border);
  --el-table-row-hover-bg-color: rgba(0, 212, 170, 0.05);
  --el-table-current-row-bg-color: rgba(0, 212, 170, 0.05);
  --el-fill-color-blank: #0e1628;
  --el-table-fixed-box-shadow: -8px 0 12px rgba(0, 0, 0, 0.45);
  width: 100%;
  background: transparent;
}
.groups-table :deep(.el-table__inner-wrapper::before) {
  display: none;
}
.groups-table :deep(.el-table__header th.el-table__cell) {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.groups-table :deep(.el-table__body td.el-table__cell) {
  font-family: var(--mono);
  font-size: 12px;
  vertical-align: middle;
}
.groups-table :deep(.el-table__body td.el-table__cell:first-child) {
  font-family: var(--font);
}
.groups-table :deep(.el-table__empty-text) {
  color: var(--muted);
}
.groups-table :deep(.el-table__empty-block) {
  background: transparent;
}
.groups-table :deep(.el-table-fixed-column--right) {
  background: #0e1628 !important;
}
.groups-table :deep(th.el-table-fixed-column--right) {
  background: #101a2e !important;
}
.groups-table :deep(.el-table__fixed-right-patch) {
  background: #101a2e;
}

.member-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.member-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
}

.member-order {
  min-width: 20px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}

.member-name {
  font-weight: 500;
}

.member-meta {
  font-size: 12px;
}

.member-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.watch-log-modal .modal-body {
  overflow: auto;
  max-height: min(60vh, 480px);
}
.watch-log-table-wrap {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
.watch-log-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.watch-log-table th,
.watch-log-table td {
  padding: 8px 10px;
  text-align: left;
  border-bottom: 1px solid var(--glass-border);
  vertical-align: middle;
  word-break: break-word;
}
.watch-log-table th {
  color: var(--muted);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.watch-log-table .col-expand {
  width: 28px;
  white-space: nowrap;
}
.watch-log-table .col-time {
  white-space: nowrap;
}
.watch-log-detail-row td {
  background: rgba(6, 10, 18, 0.4);
}
.watch-log-detail {
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px 16px;
  padding: 4px 8px 8px;
}
.watch-log-detail .kv .v {
  font-size: 13px;
}
.watch-log-signal-link {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  text-align: left;
  font-size: 12px;
}
.watch-log-table td.watch-log-jump {
  cursor: pointer;
}

@media (max-width: 768px) {
  .group-form-modal,
  .watch-log-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .member-row {
    flex-wrap: wrap;
  }
  .member-actions {
    width: 100%;
    margin-left: 0;
    justify-content: flex-end;
  }
}
</style>
