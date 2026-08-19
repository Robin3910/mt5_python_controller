<script setup lang="ts">
// 分组管理页：strategy 信号（Webhook model=strategy）的分发单元
// 新建/编辑/删除分组、启停、维护成员节点、设置分组级分发模式；信号明细见 GroupSignalsView
import { computed, defineAsyncComponent, onMounted, ref, reactive, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  GroupDispatchMode,
  GroupOut,
  ManualSignalAction,
  ManualSignalPayload,
  ManualSignalResult,
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

async function remove(g: GroupOut): Promise<void> {
  if (!(await confirmAction(`确认删除分组「${g.name}」？\n\n该操作不可恢复（历史信号任务记录会保留）。`, '确认删除'))) return
  await hub.deleteGroup(g.group_id, currentSearchOptions())
}

const PURGE_CONFIRM_TEXT = '清空交易记录'
const purging = ref(false)

async function purgeTradeLogs(): Promise<void> {
  if (!(await confirmAction(
    '确认清空全部交易记录？\n\n'
      + '将删除：信号历史、按币种分发明细、分组策略主任务 / 节点子任务 / 事件流，并清理相关运行态缓存。\n'
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

// ---- 手动触发策略信号 ----
// 与 Webhook 的 model=strategy 走同一条分组分发链路，只是入口换成后台管理员操作。
const showTrigger = ref(false)
const triggering = ref(false)
const triggerError = ref('')
// 命中范围要按全部分组试算，不能用被搜索条件过滤过的列表
const triggerGroups = ref<GroupOut[]>([])
const loadingTriggerGroups = ref(false)

const TRIGGER_HELP = {
  symbol:
    '信号品种。分组链路按「绑定策略的品种」匹配：只有已启用、且绑定了同品种启用策略的分组才会收到本信号。' +
    '不同券商的后缀差异（XAUUSD / XAUUSDm / XAUUSD.pro）会自动归一化后匹配。',
  action:
    'BUY / SELL 触发策略托管开仓：命中分组的有效节点会收到首单参数与策略规则快照，之后由节点自主按规则加仓。' +
    'CLOSE 是终止指令，平掉命中分组内进行中任务对应魔术号的持仓并结束节点侧监控。',
  volume:
    '首单手数。分组链路直接采用此手数（仅受单笔上限保护），不走节点的按币种手数策略。',
  stop_loss:
    '首单止损价（绝对价格），留空表示不设。\n' +
    '策略模版2（以损定量趋势单）必填：它的手数就是由风险金额与止损距离反推的，缺止损会被拒收。',
  take_profit: '首单止盈价（绝对价格），留空表示不设。',
  entry_price:
    '限价开仓的挂单价（对应 Webhook 的 limit_price 字段），留空表示不设。\n' +
    '只有配成「限价」开仓的策略模版2 会用它：底仓与分散仓全部挂在这个价。\n' +
    '这类策略缺了入场价会被拒收；配成「市价」的策略与其它模版忽略该字段。\n' +
    '入场价必须落在止损价的盈利侧（多单高于止损、空单低于止损），否则挂单一成交就已越过止损。',
  comment: '订单备注，会写入 MT5 订单的 comment 字段，便于对账。',
  template_ids:
    '策略模版定向（信号的 template_ids 字段）：勾选后，只有绑定了这些模版的分组才会收到本信号，' +
    '在品种匹配之上再加一层筛选。一个都不勾表示不限制模版。',
  group_ids:
    '分组定向（信号的 group_ids 字段）：勾选后，只有勾中的分组会收到本信号。' +
    '一个都不勾表示下面列出的分组全部收到。',
}

const triggerForm = reactive({
  symbol: '',
  action: 'BUY' as ManualSignalAction,
  volume: 0.1 as number | null,
  stop_loss: null as number | null,
  take_profit: null as number | null,
  entry_price: null as number | null,
  comment: '',
  template_ids: [] as string[],
  group_ids: [] as string[],
})

const isCloseAction = computed(() => triggerForm.action === 'CLOSE')

/** 品种归一化，与后端 group_rules.normalize_symbol_key 同口径 */
function normalizeSymbolKey(symbol: string): string {
  return (symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
}

/** 策略品种与信号品种是否同一标的（归一化后互为前缀即匹配） */
function symbolMatch(strategySymbol: string, signalSymbol: string): boolean {
  const a = normalizeSymbolKey(strategySymbol)
  const b = normalizeSymbolKey(signalSymbol)
  if (!a || !b) return false
  return a.startsWith(b) || b.startsWith(a)
}

function strategyOf(g: GroupOut): StrategyOut | undefined {
  return g.strategy_id ? hub.strategies.find((s) => s.strategy_id === g.strategy_id) : undefined
}

function groupStrategyLabel(g: GroupOut): string {
  const sty = strategyOf(g)
  return sty ? strategyLabel(sty) : '未绑定'
}

/** 已绑定到分组的策略品种（去重），供品种快捷选择 */
const triggerSymbolOptions = computed<string[]>(() => {
  const out: string[] = []
  for (const g of triggerGroups.value) {
    const symbol = strategyOf(g)?.symbol
    if (symbol && !out.includes(symbol)) out.push(symbol)
  }
  return out
})

/** 分组已绑定策略用到的模版（去重），供模版定向勾选 */
const triggerTemplateOptions = computed<Array<{ id: string; name: string }>>(() => {
  const out: Array<{ id: string; name: string }> = []
  for (const g of triggerGroups.value) {
    const sty = strategyOf(g)
    if (sty && !out.some((t) => t.id === sty.template_id)) {
      out.push({ id: sty.template_id, name: sty.template_name })
    }
  }
  return out
})

function toggleTriggerTemplate(templateId: string, checked: boolean): void {
  const kept = triggerForm.template_ids.filter((id) => id !== templateId)
  triggerForm.template_ids = checked ? [...kept, templateId] : kept
}

/** 候选分组：与后端 GroupDispatcher._candidate_groups 的入选条件同口径（尚未按分组定向收窄） */
const candidateGroups = computed<GroupOut[]>(() => {
  const symbol = triggerForm.symbol.trim()
  if (!symbol) return []
  const templates = triggerForm.template_ids
  return triggerGroups.value.filter((g) => {
    if (!g.enabled) return false
    const sty = strategyOf(g)
    if (!sty || !sty.enabled || !symbolMatch(sty.symbol, symbol)) return false
    return !templates.length || templates.includes(sty.template_id)
  })
})

/** 实际命中分组：候选分组再按分组定向收窄；一个都没勾表示候选分组全收 */
const matchedGroups = computed<GroupOut[]>(() => {
  const picked = triggerForm.group_ids
  if (!picked.length) return candidateGroups.value
  return candidateGroups.value.filter((g) => picked.includes(g.group_id))
})

function toggleTriggerGroup(groupId: string, checked: boolean): void {
  const kept = triggerForm.group_ids.filter((id) => id !== groupId)
  triggerForm.group_ids = checked ? [...kept, groupId] : kept
}

// 改品种 / 改模版定向会换掉候选分组，勾选里的失效 ID 必须同步剔除，
// 否则提交时带着一批已不在候选内的 ID，后端会把整条信号判成无匹配分组。
watch(candidateGroups, (list) => {
  if (!triggerForm.group_ids.length) return
  const ids = new Set(list.map((g) => g.group_id))
  const kept = triggerForm.group_ids.filter((id) => ids.has(id))
  if (kept.length !== triggerForm.group_ids.length) triggerForm.group_ids = kept
})

/** 命中分组里当前具备有效节点的数量（有效节点为 0 时信号会被记为未下发） */
const readyGroupCount = computed(
  () => matchedGroups.value.filter((g) => g.online_node_count > 0).length,
)

async function openTrigger(): Promise<void> {
  triggerError.value = ''
  Object.assign(triggerForm, {
    symbol: '',
    action: 'BUY' as ManualSignalAction,
    volume: 0.1,
    stop_loss: null,
    take_profit: null,
    entry_price: null,
    comment: '',
    template_ids: [] as string[],
    group_ids: [] as string[],
  })
  triggerGroups.value = []
  showTrigger.value = true
  loadingTriggerGroups.value = true
  try {
    const [groups] = await Promise.all([hub.listGroups(), hub.fetchStrategies()])
    triggerGroups.value = groups
  } catch {
    // 读不到分组时预览会显示成「无匹配」，这里说明清楚，避免被误当成真实结果
    triggerError.value = '读取分组失败，命中范围暂时无法预演；请关闭弹窗后重试'
    return
  } finally {
    loadingTriggerGroups.value = false
  }
  // 只有一个可选品种时直接填上，省一次输入
  const options = triggerSymbolOptions.value
  if (options.length === 1) triggerForm.symbol = options[0]
}

function buildTriggerPayload(symbol: string): ManualSignalPayload {
  const payload: ManualSignalPayload = { symbol, action: triggerForm.action, model: 'strategy' }
  // 两个定向字段对 CLOSE 同样生效（只终止被点名分组内的任务），所以放在 CLOSE 早返回之前
  if (triggerForm.template_ids.length) payload.template_ids = [...triggerForm.template_ids]
  if (triggerForm.group_ids.length) payload.group_ids = [...triggerForm.group_ids]
  if (isCloseAction.value) return payload
  payload.volume = Number(triggerForm.volume)
  if (triggerForm.stop_loss) payload.stop_loss = triggerForm.stop_loss
  if (triggerForm.take_profit) payload.take_profit = triggerForm.take_profit
  if (triggerForm.entry_price) payload.entry_price = triggerForm.entry_price
  const comment = triggerForm.comment.trim()
  if (comment) payload.comment = comment
  return payload
}

/** 与后端 console_api._build_signal_payload / Webhook 解析同构的信号体 */
function buildWebhookSignalPayload(payload: ManualSignalPayload): Record<string, unknown> {
  const data: Record<string, unknown> = {
    action: payload.action,
    symbol: payload.symbol,
    model: payload.model ?? 'strategy',
  }
  if (payload.template_ids?.length) data.template_ids = [...payload.template_ids]
  if (payload.group_ids?.length) data.group_ids = [...payload.group_ids]
  if (payload.volume != null) data.volume = payload.volume
  if (payload.stop_loss) data.sl = payload.stop_loss
  if (payload.take_profit) data.tp = payload.take_profit
  if (payload.entry_price) data.limit_price = payload.entry_price
  if (payload.comment) data.comment = payload.comment
  return data
}

function triggerSummary(payload: ManualSignalPayload): string {
  const hit = matchedGroups.value
  const groupText = hit.length
    ? hit.map((g) => `· ${g.name}（有效节点 ${g.online_node_count}）`).join('\n')
    : '· 无（当前没有匹配的分组，信号将被拒收）'
  const lines = [`品种：${payload.symbol}`, `动作：${payload.action}`]
  if (payload.template_ids?.length) {
    const names = payload.template_ids.map(
      (id) => triggerTemplateOptions.value.find((t) => t.id === id)?.name || id,
    )
    lines.push(`模版定向：${names.join('、')}`)
  }
  if (payload.group_ids?.length) {
    lines.push(`分组定向：只发给勾选的 ${payload.group_ids.length} 个分组`)
  }
  if (isCloseAction.value) {
    lines.push('说明：平掉命中分组内进行中任务的持仓并结束策略监控')
  } else {
    lines.push(`手数：${payload.volume}`)
    lines.push(`止损：${payload.stop_loss ?? '不设'}　止盈：${payload.take_profit ?? '不设'}`)
    if (payload.entry_price) lines.push(`入场价：${payload.entry_price}（限价开仓用）`)
    if (payload.comment) lines.push(`备注：${payload.comment}`)
  }
  return `${lines.join('\n')}\n\n预计命中分组：\n${groupText}`
}

const showTriggerConfirm = ref(false)
const pendingTriggerPayload = ref<ManualSignalPayload | null>(null)
const triggerCopyTip = ref('')
const triggerPayloadExpanded = ref(false)

const pendingTriggerPayloadJson = computed(() => {
  if (!pendingTriggerPayload.value) return ''
  return JSON.stringify(buildWebhookSignalPayload(pendingTriggerPayload.value), null, 2)
})

async function copyPendingTriggerPayload(): Promise<void> {
  const text = pendingTriggerPayloadJson.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    triggerCopyTip.value = '已复制'
    window.setTimeout(() => {
      triggerCopyTip.value = ''
    }, 2000)
  } catch {
    triggerCopyTip.value = '复制失败'
  }
}

function cancelTriggerConfirm(): void {
  showTriggerConfirm.value = false
  pendingTriggerPayload.value = null
  triggerCopyTip.value = ''
  triggerPayloadExpanded.value = false
}

/** 展示触发结果；返回 true 表示这次触发已经收口，可以关闭弹窗 */
function reportTriggerResult(payload: ManualSignalPayload, res: ManualSignalResult): boolean {
  const head = `${payload.action} ${payload.symbol}`
  if (res.status === 'accepted') {
    const groups = res.groups ?? 0
    const targets = res.targets ?? 0
    if (payload.action === 'CLOSE') {
      if (targets > 0) {
        ElMessage.success(`已下发终止指令：命中 ${groups} 个分组，${targets} 个节点任务开始平仓`)
      } else {
        ElMessage.warning(`命中 ${groups} 个分组，但没有进行中的策略任务需要终止`)
      }
      return true
    }
    const detail = `命中 ${groups} 个分组，${targets} 个节点收到下发`
    if (targets > 0) {
      ElMessage.success(`已触发 ${head}：${detail}`)
    } else {
      ElMessage.warning(`${head} 已受理但未下发：${detail}（${acceptedButNotDispatchedHint(res)}）`)
    }
    return true
  }
  if (res.status === 'duplicate') {
    ElMessage.warning(`重复信号被抑制：5 秒内已有相同参数的 ${head} 策略信号`)
    return true
  }
  if (res.status === 'rejected') {
    triggerError.value = res.reason || '信号被拒收'
    ElMessage.warning(`已拒收：${triggerError.value}`)
    return false
  }
  ElMessage.info(`已提交：${res.status}`)
  return true
}

/** accepted 但 targets=0 时，用各分组真实未下发原因，避免把趋势拦截说成节点忙 */
function acceptedButNotDispatchedHint(res: ManualSignalResult): string {
  const reasons = [
    ...new Set(
      (res.tasks || [])
        .map((t) => (t.reason || '').trim())
        .filter(Boolean),
    ),
  ]
  if (reasons.length) return reasons.join('；')
  return '分组无有效节点，或未能下发到任何节点'
}

async function submitTrigger(): Promise<void> {
  const symbol = triggerForm.symbol.trim().toUpperCase()
  if (!symbol) {
    triggerError.value = '请填写信号品种'
    return
  }
  if (!isCloseAction.value && !(Number(triggerForm.volume) > 0)) {
    triggerError.value = '开仓信号必须填写大于 0 的手数'
    return
  }
  pendingTriggerPayload.value = buildTriggerPayload(symbol)
  triggerCopyTip.value = ''
  triggerPayloadExpanded.value = false
  showTriggerConfirm.value = true
}

async function confirmSubmitTrigger(): Promise<void> {
  const payload = pendingTriggerPayload.value
  if (!payload) return
  showTriggerConfirm.value = false

  triggering.value = true
  triggerError.value = ''
  try {
    const res = await hub.triggerManualSignal(payload)
    const done = reportTriggerResult(payload, res)
    // 只有真正受理才会新增主任务，列表的信号计数需要重取
    if (res.status === 'accepted') await loadGroups()
    if (done) showTrigger.value = false
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    triggerError.value = err?.response?.data?.detail || err?.message || '触发失败，请稍后重试'
    ElMessage.error(`触发失败：${triggerError.value}`)
  } finally {
    triggering.value = false
    pendingTriggerPayload.value = null
    triggerPayloadExpanded.value = false
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
        <button class="btn-ghost" @click="openTrigger">手动触发信号</button>
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
        <div v-if="g.strategy_id" class="list-field">
          <span class="k">策略</span>
          <span class="v">
            <button class="btn-sm btn-success" @click="openGroupStrategyEdit(g)">编辑策略</button>
          </span>
        </div>
        <div class="list-field"><span class="k">成员节点</span><span class="v">{{ g.node_count }}</span></div>
        <div class="list-field"><span class="k">有效节点</span><span class="v">{{ g.online_node_count }}</span></div>
        <div class="list-field">
          <span class="k">信号</span>
          <span class="v">
            <button class="btn-sm btn-success" @click="openSignals(g)">{{ g.signal_count }} 条</button>
          </span>
        </div>
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
          <button class="btn-sm btn-ghost" @click="openEdit(g)">编辑</button>
          <button class="btn-sm btn-danger" @click="remove(g)">删除</button>
        </div>
      </div>
      <div v-if="!hub.groups.length && !loading" class="card card-pad muted">
        {{ appliedQuery ? '无匹配分组' : '暂无分组' }}
      </div>
    </div>

    <!-- 桌面端表格 -->
    <div class="card table-scroll desktop-only">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>绑定策略</th>
            <th>分发模式</th>
            <th>趋势风控</th>
            <th>策略</th>
            <th class="right">成员节点</th>
            <th class="right">有效节点</th>
            <th class="right">信号</th>
            <th class="right">进行中</th>
            <th>备注</th>
            <th>启用</th>
            <th class="right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in hub.groups" :key="g.group_id">
            <td>
              {{ g.name }}
              <div class="muted" style="font-size: 11px">{{ g.group_id }}</div>
            </td>
            <td>
              <template v-if="g.strategy_name">
                {{ g.strategy_name }}
                <div v-if="g.strategy_id" class="muted" style="font-size: 11px">{{ g.strategy_id }}</div>
              </template>
              <span v-else class="muted">未绑定</span>
            </td>
            <td><span class="tag blue">{{ DISPATCH_MODE_LABEL[g.dispatch_mode] }}</span></td>
            <td>
              <button
                class="btn-sm"
                :class="g.trend_risk_enabled ? 'btn-success' : 'btn-danger'"
                :title="FIELD_HELP.trend_risk"
                @click="toggleTrendRisk(g)"
              >
                {{ g.trend_risk_enabled ? '开启' : '关闭' }}
              </button>
            </td>
            <td>
              <button
                v-if="g.strategy_id"
                class="btn-sm btn-success"
                @click="openGroupStrategyEdit(g)"
              >编辑策略</button>
              <span v-else class="muted">—</span>
            </td>
            <td class="right">{{ g.node_count }}</td>
            <td class="right" :class="g.online_node_count ? '' : 'muted'">{{ g.online_node_count }}</td>
            <td class="right">
              <button class="btn-sm btn-success" @click="openSignals(g)">{{ g.signal_count }} 条</button>
            </td>
            <td class="right">
              <button
                v-if="g.active_task_count > 0"
                class="btn-sm btn-success"
                @click="openActiveSignals(g)"
              >{{ g.active_task_count }} 条</button>
              <span v-else class="muted">—</span>
            </td>
            <td class="muted" style="font-size: 12px">{{ g.remark || '—' }}</td>
            <td>
              <button class="btn-sm" :class="g.enabled ? 'btn-success' : 'btn-danger'" @click="toggleEnabled(g)">
                {{ g.enabled ? '已启用' : '已禁用' }}
              </button>
            </td>
            <td class="right">
              <button class="btn-sm btn-ghost" @click="openEdit(g)">编辑</button>
              <button class="btn-sm btn-danger" @click="remove(g)">删除</button>
            </td>
          </tr>
          <tr v-if="!hub.groups.length && !loading">
            <td colspan="11" class="muted" style="padding: 18px">
              {{ appliedQuery ? '无匹配分组' : '暂无分组，点击右上角「新建分组」开始配置' }}
            </td>
          </tr>
        </tbody>
      </table>
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

    <!-- 手动触发策略信号弹窗 -->
    <div v-if="showTrigger" class="modal-mask" @click.self="showTrigger = false">
      <div class="card card-pad modal modal-lg group-trigger-modal">
        <div class="modal-header">
          <div class="h1">手动触发策略信号</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            等同于收到一条 <code>model=strategy</code> 的 Webhook：按品种匹配「已启用且绑定同品种启用策略」的分组，
            各分组再按自己的分发模式下发给有效节点。不影响按币种分发（<code>model=normal</code>）的链路。
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid two">
            <div>
              <FormLabel field-id="trigger-symbol" text="信号品种" :help="TRIGGER_HELP.symbol" />
              <input id="trigger-symbol" v-model="triggerForm.symbol" placeholder="例如：XAUUSD" />
              <div v-if="triggerSymbolOptions.length" class="symbol-picks">
                <span class="muted">已配置：</span>
                <button
                  v-for="s in triggerSymbolOptions"
                  :key="s"
                  type="button"
                  class="btn-sm btn-ghost"
                  @click="triggerForm.symbol = s"
                >
                  {{ s }}
                </button>
              </div>
            </div>
            <div>
              <FormLabel field-id="trigger-action" text="信号方向" :help="TRIGGER_HELP.action" />
              <select id="trigger-action" v-model="triggerForm.action">
                <option value="BUY">BUY（策略托管开多）</option>
                <option value="SELL">SELL（策略托管开空）</option>
                <option value="CLOSE">CLOSE（终止任务并平仓）</option>
              </select>
            </div>

            <template v-if="!isCloseAction">
              <div>
                <FormLabel field-id="trigger-volume" text="首单手数" :help="TRIGGER_HELP.volume" />
                <input
                  id="trigger-volume"
                  v-model.number="triggerForm.volume"
                  type="number"
                  min="0.01"
                  step="0.01"
                />
              </div>
              <div>
                <FormLabel field-id="trigger-comment" text="订单备注" :help="TRIGGER_HELP.comment" />
                <input id="trigger-comment" v-model="triggerForm.comment" placeholder="选填" />
              </div>
              <div>
                <FormLabel field-id="trigger-sl" text="止损价" :help="TRIGGER_HELP.stop_loss" />
                <input
                  id="trigger-sl"
                  v-model.number="triggerForm.stop_loss"
                  type="number"
                  step="0.01"
                  placeholder="留空表示不设"
                />
              </div>
              <div>
                <FormLabel field-id="trigger-tp" text="止盈价" :help="TRIGGER_HELP.take_profit" />
                <input
                  id="trigger-tp"
                  v-model.number="triggerForm.take_profit"
                  type="number"
                  step="0.01"
                  placeholder="留空表示不设"
                />
              </div>
              <div>
                <FormLabel
                  field-id="trigger-entry"
                  text="入场价（限价开仓）"
                  :help="TRIGGER_HELP.entry_price"
                />
                <input
                  id="trigger-entry"
                  v-model.number="triggerForm.entry_price"
                  type="number"
                  step="any"
                  placeholder="仅限价开仓需要，留空表示不设"
                />
              </div>
            </template>
            <p v-else class="span-full trigger-warning">
              CLOSE 会平掉命中分组内进行中任务对应魔术号的持仓并结束节点侧策略监控，不影响按币种分发链路的持仓。
            </p>

            <div v-if="triggerTemplateOptions.length" class="span-full">
              <FormLabel text="策略模版定向" :help="TRIGGER_HELP.template_ids" />
              <div class="template-picks">
                <label v-for="t in triggerTemplateOptions" :key="t.id" class="template-pick">
                  <input
                    type="checkbox"
                    :checked="triggerForm.template_ids.includes(t.id)"
                    @change="toggleTriggerTemplate(t.id, ($event.target as HTMLInputElement).checked)"
                  />
                  <span>{{ t.name }}</span>
                  <code>{{ t.id }}</code>
                </label>
                <span v-if="!triggerForm.template_ids.length" class="muted">不限制模版</span>
              </div>
            </div>

            <div class="span-full">
              <FormLabel
                text="预计命中分组"
                :help="TRIGGER_HELP.group_ids"
              />
              <div v-if="loadingTriggerGroups" class="muted" style="font-size: 12px">
                正在读取全部分组…
              </div>
              <div v-else-if="!triggerForm.symbol.trim()" class="muted" style="font-size: 12px">
                填写品种后，这里会列出将收到本信号的分组。
              </div>
              <div v-else-if="!candidateGroups.length" class="muted" style="font-size: 12px">
                没有匹配的分组：该品种下没有「已启用且绑定同品种启用策略」<template
                  v-if="triggerForm.template_ids.length"
                >且模版在定向范围内</template>的分组，信号会被拒收。
              </div>
              <div v-else class="member-list">
                <label
                  v-for="g in candidateGroups"
                  :key="g.group_id"
                  class="member-row group-pick"
                  :class="{ 'group-pick-off': triggerForm.group_ids.length && !triggerForm.group_ids.includes(g.group_id) }"
                >
                  <input
                    type="checkbox"
                    :checked="triggerForm.group_ids.includes(g.group_id)"
                    :aria-label="`只发给分组 ${g.name}`"
                    @change="toggleTriggerGroup(g.group_id, ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="member-name">{{ g.name }}</span>
                  <span class="muted member-meta">{{ groupStrategyLabel(g) }}</span>
                  <span class="tag blue">{{ DISPATCH_MODE_LABEL[g.dispatch_mode] }}</span>
                  <span class="tag" :class="g.online_node_count ? 'green' : 'red'">
                    有效节点 {{ g.online_node_count }}
                  </span>
                </label>
                <p class="muted" style="font-size: 12px; margin: 0">
                  <template v-if="triggerForm.group_ids.length">
                    已定向到勾选的 {{ matchedGroups.length }} 个分组（信号带 group_ids），其余分组不会收到。
                  </template>
                  <template v-else>上面 {{ candidateGroups.length }} 个分组都会收到；勾选后只发给勾中的分组。</template>
                </p>
              </div>
              <p
                v-if="matchedGroups.length > readyGroupCount"
                class="muted"
                style="font-size: 12px; margin: 8px 0 0"
              >
                其中 {{ matchedGroups.length - readyGroupCount }} 个分组当前没有有效节点（成员未启用或不在线），
                会生成主任务但记为未下发。
              </p>
            </div>

            <div v-if="triggerError" class="span-full" style="color: var(--red); font-size: 13px">
              {{ triggerError }}
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showTrigger = false">取消</button>
          <button
            class="btn-primary"
            :disabled="triggering || loadingTriggerGroups || !triggerForm.symbol.trim()"
            @click="submitTrigger"
          >
            {{ triggering ? '触发中…' : '触发信号' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 触发前二次确认：摘要 + Webhook 同构 JSON（可复制） -->
    <div v-if="showTriggerConfirm && pendingTriggerPayload" class="modal-mask trigger-confirm-mask" @click.self="cancelTriggerConfirm">
      <div class="card card-pad modal trigger-confirm-modal">
        <div class="modal-header">
          <div class="h1">确认触发信号</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            确认手动触发 strategy 信号？可展开查看 Webhook 同构 JSON。
          </p>
        </div>
        <div class="modal-body">
          <pre class="trigger-summary">{{ triggerSummary(pendingTriggerPayload) }}</pre>
          <div class="trigger-payload-section">
            <button
              type="button"
              class="trigger-payload-toggle"
              :aria-expanded="triggerPayloadExpanded"
              @click="triggerPayloadExpanded = !triggerPayloadExpanded"
            >
              <span class="muted" aria-hidden="true">{{ triggerPayloadExpanded ? '▾' : '▸' }}</span>
              <strong>信号原始请求参数</strong>
            </button>
            <template v-if="triggerPayloadExpanded">
              <div class="trigger-payload-head row between">
                <span class="muted" style="font-size: 12px">Webhook 同构 JSON，与手动触发经后端转换后的信号体一致</span>
                <button type="button" class="btn-sm btn-ghost" @click.stop="copyPendingTriggerPayload">
                  {{ triggerCopyTip || '复制 JSON' }}
                </button>
              </div>
              <pre class="trigger-payload-json">{{ pendingTriggerPayloadJson }}</pre>
            </template>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" :disabled="triggering" @click="cancelTriggerConfirm">取消</button>
          <button class="btn-primary" :disabled="triggering" @click="confirmSubmitTrigger">
            {{ triggering ? '触发中…' : '确认' }}
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

.symbol-picks {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
}

.template-picks {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 6px;
  font-size: 12px;
}

.template-pick {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.template-pick code {
  color: var(--muted);
}

.group-pick {
  cursor: pointer;
}

/* 已做分组定向时，没被勾中的分组淡化，一眼看出本次不会收到信号 */
.group-pick-off {
  opacity: 0.45;
}

.trigger-warning {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.08);
  color: #fbbf24;
  font-size: 12px;
  line-height: 1.6;
}

.trigger-confirm-mask {
  z-index: 60;
}

.trigger-confirm-modal {
  width: min(560px, calc(100vw - 32px));
  max-height: calc(100dvh - 48px);
  display: flex;
  flex-direction: column;
}

.trigger-confirm-modal .modal-body {
  overflow: auto;
}

.trigger-summary {
  margin: 0 0 14px;
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: rgba(6, 10, 18, 0.45);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  color: var(--text);
}

.trigger-payload-section {
  margin-top: 4px;
}

.trigger-payload-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 0;
  background: none;
  border: none;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.trigger-payload-toggle:hover strong {
  color: var(--primary);
}

.trigger-payload-head {
  align-items: center;
  margin-bottom: 8px;
}

.trigger-payload-json {
  margin: 0;
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: rgba(6, 10, 18, 0.65);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  color: #a5f3fc;
  user-select: all;
  max-height: 220px;
  overflow: auto;
}

@media (max-width: 768px) {
  .group-form-modal,
  .group-trigger-modal {
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
