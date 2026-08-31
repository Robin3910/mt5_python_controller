<script setup lang="ts">
// 分组信号页：展示某分组处理过的 strategy 主任务与各节点下发明细
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useHubStore } from '@/stores/hub'
import type {
  GroupOut,
  GroupSignalTaskRecord,
  GroupTaskDispatchRecord,
  GroupTaskEventDetail,
  GroupTaskEventRecord,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const route = useRoute()
const router = useRouter()
const hub = useHubStore()

const groupId = computed(() => String(route.params.id))
const group = ref<GroupOut | null>(null)
const loadError = ref('')

/** 仅看进行中主任务（URL ?status=active） */
const activeOnly = computed(() => route.query.status === 'active')

/** 监听日志跳转：URL ?signal_id= 精确定位某条主任务 */
const focusSignalId = computed(() => {
  const q = route.query.signal_id
  if (typeof q === 'string') return q.trim()
  if (Array.isArray(q) && typeof q[0] === 'string') return q[0].trim()
  return ''
})

const signals = ref<GroupSignalTaskRecord[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const expanded = ref<Record<string, boolean>>({})

/** 节点子任务展开：dispatch_id -> 关联订单事件 */
const expandedDispatch = ref<Record<string, boolean>>({})
const dispatchEvents = ref<Record<string, GroupTaskEventRecord[]>>({})
const loadingDispatchEvents = ref<Record<string, boolean>>({})
/** 订单展开：展示该笔订单的开单原因与逐项计算参数 */
const expandedEvent = ref<Record<string, boolean>>({})

/** 自动刷新订单数据：默认开启，每秒静默刷新 */
const autoRefresh = ref(true)
let autoRefreshTimer: ReturnType<typeof setInterval> | undefined
let autoRefreshInFlight = false

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

async function loadGroup(): Promise<void> {
  loadError.value = ''
  const groups = await hub.listGroups()
  group.value = groups.find((g) => g.group_id === groupId.value) ?? null
  if (!group.value) loadError.value = '分组不存在或已被删除'
}

async function fetchSignalsPage(opts?: { locate?: boolean }): Promise<void> {
  if (!group.value) {
    signals.value = []
    total.value = 0
    return
  }
  const sid = focusSignalId.value
  const res = await hub.fetchGroupSignals(
    group.value.group_id,
    page.value,
    pageSize.value,
    !sid && activeOnly.value ? 'active' : undefined,
    sid || undefined,
  )
  signals.value = res.items
  total.value = res.total
  if (res.page !== page.value) page.value = res.page
  const nodeIds = [...new Set(res.items.flatMap((t) => t.dispatches.map((d) => d.node_id)))]
  await Promise.all(
    nodeIds.filter((id) => !hub.accounts[id]).map((id) => hub.fetchNodeAccount(id)),
  )
  if (sid) {
    if (opts?.locate) {
      for (const t of res.items) {
        if (t.signal_id === sid) expanded.value[String(t.task_id)] = true
      }
      if (res.total === 0) {
        ElMessage.warning('未找到该信号，可能已被清空')
      } else {
        await nextTick()
        const el = document.querySelector(`[data-signal-id="${CSS.escape(sid)}"]`)
        el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }
    }
    return
  }
  // 「仅进行中」无数据时自动切回「全部」
  if (activeOnly.value && res.total === 0) {
    setActiveFilter(false)
  }
}

async function loadSignals(): Promise<void> {
  if (!group.value) {
    signals.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    await fetchSignalsPage({ locate: true })
  } finally {
    loading.value = false
  }
}

async function refreshExpandedDispatchEvents(): Promise<void> {
  if (!group.value) return
  const ids = Object.keys(expandedDispatch.value)
    .filter((k) => expandedDispatch.value[k])
    .map((k) => Number(k))
    .filter((id) => Number.isFinite(id))
  if (!ids.length) return
  const gid = group.value.group_id
  await Promise.all(
    ids.map(async (dispatchId) => {
      const k = String(dispatchId)
      dispatchEvents.value[k] = await hub.fetchGroupDispatchEvents(gid, dispatchId)
    }),
  )
}

/** 静默刷新主任务列表与已展开节点的关联订单（不打断展开态、不闪加载文案） */
async function refreshLiveData(): Promise<void> {
  if (!group.value || autoRefreshInFlight) return
  autoRefreshInFlight = true
  try {
    await fetchSignalsPage()
    await refreshExpandedDispatchEvents()
  } finally {
    autoRefreshInFlight = false
  }
}

function stopAutoRefresh(): void {
  if (autoRefreshTimer !== undefined) {
    clearInterval(autoRefreshTimer)
    autoRefreshTimer = undefined
  }
}

function startAutoRefresh(): void {
  stopAutoRefresh()
  autoRefreshTimer = setInterval(() => {
    void refreshLiveData()
  }, 1000)
}

async function reload(): Promise<void> {
  page.value = 1
  expanded.value = {}
  expandedDispatch.value = {}
  dispatchEvents.value = {}
  loadingDispatchEvents.value = {}
  expandedEvent.value = {}
  await loadGroup()
  await loadSignals()
}

function setActiveFilter(onlyActive: boolean): void {
  const query = { ...route.query }
  if (onlyActive) {
    query.status = 'active'
  } else {
    delete query.status
  }
  delete query.signal_id
  router.replace({ name: 'group-signals', params: { id: groupId.value }, query })
}

function clearSignalFocus(): void {
  const query = { ...route.query }
  delete query.signal_id
  router.replace({ name: 'group-signals', params: { id: groupId.value }, query })
}

function goPage(next: number): void {
  const p = Math.min(Math.max(1, next), totalPages.value)
  if (p === page.value) return
  page.value = p
  loadSignals()
}

function onPageSizeChange(): void {
  page.value = 1
  loadSignals()
}

function toggleRow(key: string | number): void {
  const k = String(key)
  expanded.value[k] = !expanded.value[k]
}

function isExpanded(key: string | number): boolean {
  return !!expanded.value[String(key)]
}

function isDispatchExpanded(dispatchId: number): boolean {
  return !!expandedDispatch.value[String(dispatchId)]
}

async function toggleDispatch(dispatchId: number): Promise<void> {
  const k = String(dispatchId)
  const next = !expandedDispatch.value[k]
  expandedDispatch.value[k] = next
  if (!next || !group.value) return
  if (dispatchEvents.value[k]) return
  loadingDispatchEvents.value[k] = true
  try {
    dispatchEvents.value[k] = await hub.fetchGroupDispatchEvents(group.value.group_id, dispatchId)
  } finally {
    loadingDispatchEvents.value[k] = false
  }
}

function eventKey(dispatchId: number, ev: GroupTaskEventRecord): string {
  return `${dispatchId}-${ev.id}-${ev.created_at}`
}

function isEventExpanded(dispatchId: number, ev: GroupTaskEventRecord): boolean {
  return !!expandedEvent.value[eventKey(dispatchId, ev)]
}

function toggleEvent(dispatchId: number, ev: GroupTaskEventRecord): void {
  if (!ev.detail) return
  const k = eventKey(dispatchId, ev)
  expandedEvent.value[k] = !expandedEvent.value[k]
}

const LIMIT_KIND_LABEL: Record<string, string> = {
  total_lot_limit: '分批笔数上限',
  max_allow_num: '最大加仓次数',
}

function limitText(detail: GroupTaskEventDetail): string {
  if (!detail.limit_value) return '不限'
  const label = LIMIT_KIND_LABEL[detail.limit_kind || ''] || detail.limit_kind || '上限'
  return `${label} ${detail.limit_value}`
}

/** 把开单依据拆成可逐项展示的键值对；各 kind 的参数集不同 */
function detailRows(detail: GroupTaskEventDetail): Array<{ k: string; v: string }> {
  const rows: Array<{ k: string; v: string }> = []
  const push = (k: string, v: unknown): void => {
    if (v === null || v === undefined || v === '') return
    rows.push({ k, v: String(v) })
  }
  if (detail.kind === 'add') {
    push('规则类型', detail.rule_type_label)
    push('命中规则', detail.rule_index === undefined ? '' : `第 ${detail.rule_index + 1} 条`)
    push('分批档位', detail.batch ? `第 ${(detail.level_index ?? 0) + 1} 档` : '未启用分批')
    push('持仓方向', detail.direction)
    push(
      detail.rule_type === 1 ? '基准价（逆势锚点）' : '基准价（最近一笔开仓价）',
      detail.base_price,
    )
    push('触发时市价', detail.price)
    push('最小变动单位', detail.point)
    push('实际偏离', detail.deviation === undefined ? '' : `${detail.deviation} 点`)
    push('触发阈值', detail.threshold === undefined ? '' : `${detail.threshold} 点`)
    push('手数公式', detail.volume_formula)
    push('首单手数（倍率基准）', detail.base_volume)
    push('倍数', detail.lot_times)
    push('追加手数', detail.extra_lot)
    push('本次手数', detail.volume)
    push('触发前持仓', detail.position_count === undefined ? '' : `${detail.position_count} 笔`)
    push('触发前加仓', detail.add_count === undefined ? '' : `${detail.add_count} 次`)
    push('本次为第', detail.next_position_no === undefined ? '' : `${detail.next_position_no} 笔`)
    push('生效上限', limitText(detail))
    push('错误', detail.error)
    return rows
  }
  if (detail.kind === 'risk_sized_plan' || (detail.kind === 'open' && detail.risk_amount != null)) {
    push('来源信号', detail.signal_id)
    push('品种', detail.symbol)
    push('方向', detail.direction || detail.action)
    push('开仓方式', detail.entry_mode_label || detail.entry_mode)
    push('风险金额', detail.risk_amount)
    push('实际风险', detail.risk_used)
    push('手数公式', detail.lot_formula)
    push(
      '总手数',
      detail.dropped_lot
        ? `${detail.total_lot}（反推 ${detail.planned_lot}，余 ${detail.dropped_lot} 不开）`
        : detail.total_lot,
    )
    push('底仓', detail.base_volume === undefined ? '' : `${detail.base_volume}（${detail.base_ratio ?? 0}% · TP=0）`)
    push(
      '分散仓',
      detail.add_batches
        ? `${detail.distribute_volume ?? ''} 手 · 等分 ${detail.add_batches} 单` +
          (detail.order_count ? ` · 共 ${detail.order_count} 单` : '') +
          (detail.ladder_step ? ` · 阶梯限价步长 ${detail.ladder_step}` : '')
        : '无（底仓即全仓）',
    )
    push('底仓挂单价', detail.entry_mode === 'limit' ? detail.entry_price : '')
    push('止损', detail.stop_loss || '不设')
    push(
      '阶梯止盈',
      detail.take_profit
        ? `末档 ${detail.take_profit}` + (detail.tp_step ? ` · 步长 ${detail.tp_step}` : '')
        : '不设',
    )
    push('止损距离', detail.sl_points === undefined ? '' : `${detail.sl_points} 点`)
    push('触发侧报价', detail.spread ? `${detail.risk_price}（点差 ${detail.spread}）` : '')
    push('托管策略', detail.strategy_name)
    push('策略模版', detail.template_id)
    push('魔术号', detail.magic)
    push('错误', detail.error || detail.reason)
    return rows
  }
  if (detail.kind === 'risk_sized_limit_filled') {
    push('说明', detail.message)
    push('成交价', detail.price)
    push('成交笔数', detail.position_count)
    push('挂单等待', detail.waited_seconds === undefined ? '' : `${detail.waited_seconds} 秒`)
    return rows
  }
  if (detail.kind === 'risk_sized_distribute' || detail.kind === 'risk_sized_add') {
    push('分散仓', detail.batch_index === undefined ? '' : `第 ${detail.batch_index}/${detail.batch_total ?? '?'} 档`)
    push('首单开仓价', detail.entry_price)
    push('本档挂单价', detail.limit_price)
    push('现价', detail.price)
    push('本单手数', detail.volume)
    push('总手数', detail.total_lot)
    push('止损', detail.stop_loss || '不设')
    push(
      '本档止盈',
      detail.take_profit
        ? `${detail.take_profit}` + (detail.tp_full ? ` · 末档 ${detail.tp_full}` : '')
        : '不设',
    )
    push('风险金额', detail.risk_amount)
    push('实际风险', detail.risk_used)
    push('错误', detail.error)
    return rows
  }
  if (detail.kind === 'breakeven') {
    push('监控方式', detail.breakeven_mode_label || detail.breakeven_mode)
    push('均价', detail.avg_price)
    push('现价', detail.price)
    push('有利偏离', detail.favorable)
    push('触发阈值', detail.threshold)
    push('止损距倍数', detail.breakeven_times)
    push('止损距离', detail.sl_distance)
    push('新止损', detail.stop_loss)
    push('持仓笔数', detail.position_count)
    push('错误', detail.error)
    return rows
  }
  if (detail.kind === 'close_reason') {
    push('说明', detail.message)
    push('止损笔数', detail.sl)
    push('止盈笔数', detail.tp)
    push('Stop Out', detail.so)
    push('人工', detail.manual)
    push('程序', detail.expert)
    push('其他', detail.other)
    push('出场合计', detail.total)
    return rows
  }
  if (detail.kind === 'grid_plan' || detail.kind === 'grid_fill' || detail.kind === 'grid_close') {
    if (detail.kind === 'grid_plan') {
      push('来源信号', detail.signal_id)
      push('品种', detail.symbol)
      push('方向', detail.side || detail.action)
      push('网格方向', detail.grid_side_label || detail.grid_side)
      push('网格模式', detail.grid_mode_label || detail.grid_mode)
      push('价格区间', detail.price_lower == null ? '' : `${detail.price_lower} ~ ${detail.price_upper}`)
      push('网格数量', detail.grid_count)
      push('每格手数', detail.lot_per_grid)
      push('总手数上限', detail.total_lot_limit || '不限')
      push('触发价', detail.trigger_price || '立即启动')
      {
        const side = String(detail.grid_side || '').toLowerCase()
        const short = side === 'short'
        const sl = short ? detail.stop_upper : detail.stop_lower
        const tp = short ? detail.stop_lower : detail.stop_upper
        push('止损 / 止盈', `${sl || '不设'} / ${tp || '不设'}`)
      }
      push('初始建仓', detail.prefill_enabled === false ? '关闭' : '开启')
      push('等待触发', detail.waiting_trigger ? '是' : '')
      push('预填格位', detail.prefill_levels?.length ? detail.prefill_levels.join(', ') : '')
      push('托管策略', detail.strategy_name)
      push('策略模版', detail.template_id)
      push('魔术号', detail.magic)
      push('错误', detail.error || detail.reason)
      return rows
    }
    push('格位', detail.level_index)
    push('买线', detail.level_price)
    push('卖线', detail.exit_price)
    push('方向', detail.side || detail.action)
    push('现价', detail.price)
    push('手数', detail.volume || detail.lot_per_grid)
    push('持格', detail.holding_count === undefined ? '' : `${detail.holding_count}/${detail.grid_count ?? '?'}`)
    push('订单号', detail.ticket)
    push('错误', detail.error)
    return rows
  }
  if (detail.kind === 'account_risk') {
    push('风控规则', detail.rule_label || detail.rule)
    push('触发说明', detail.message_core)
    push('平仓动作', detail.close_action_label || detail.close_action)
    push('品种', detail.symbol)
    push('监控模式', detail.monitor_mode)
    push('比例阈值', detail.ratio_threshold === undefined ? '' : `${detail.ratio_threshold}%`)
    push('当前比例', detail.current_ratio === undefined ? '' : `${Number(detail.current_ratio).toFixed(2)}%`)
    push('浮动盈亏', detail.floating_pl)
    push('余额', detail.balance)
    push('净值', detail.equity)
    push('净值阈值', detail.amount_threshold)
    push('盈亏阈值', detail.pl_amount)
    push('当前盈亏', detail.current_pl)
    push('订单数', detail.order_count)
    push('拟平笔数', detail.close_count)
    push('保护触发额', detail.trigger_amount)
    push('收窄目标', detail.narrow_amount)
    push('分档', detail.tier_index === undefined ? '' : `#${detail.tier_index + 1}`)
    push('手数门槛', detail.min_lot)
    push('当前手数', detail.current_lot)
    push('规则条目', detail.item_id)
    return rows
  }
  push('来源信号', detail.signal_id)
  push('品种', detail.symbol)
  push('方向', detail.action)
  push('首单手数', detail.volume)
  push('止损', detail.stop_loss || '不设')
  push('止盈', detail.take_profit || '不设')
  push('信号备注', detail.signal_comment)
  push('托管策略', detail.strategy_name)
  push('策略 ID', detail.strategy_id)
  push('策略模版', detail.template_id)
  push(
    '策略规则',
    detail.rule_count === undefined
      ? ''
      : `${detail.enabled_rule_count ?? 0} / ${detail.rule_count} 条生效`,
  )
  push('魔术号', detail.magic)
  return rows
}

/** 一律按东八区（北京时间）显示，不跟券商 MT5 钟面对齐。 */
function fmtTime(sec: number | null | undefined): string {
  if (!sec) return '—'
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(sec * 1000)).replace(', ', ' ').replace(',', ' ')
}

function fmtPayload(raw: unknown): string {
  if (raw == null) return '—'
  if (typeof raw === 'string') {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }
  return JSON.stringify(raw, null, 2)
}

function taskTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    pending: { cls: 'blue', text: '待处理' },
    dispatching: { cls: 'blue', text: '分发中' },
    running: { cls: 'amber', text: '策略运行中' },
    done: { cls: 'green', text: '完成' },
    partial: { cls: 'amber', text: '部分成功' },
    failed: { cls: 'red', text: '失败' },
    skipped: { cls: '', text: '未下发' },
  }
  return m[status] || { cls: '', text: status }
}

function dispatchTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '完成' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
    opened: { cls: 'amber', text: '已开仓' },
    running: { cls: 'amber', text: '加仓监控中' },
    closing: { cls: 'amber', text: '平仓中' },
  }
  return m[status] || { cls: 'blue', text: status }
}

/** 任务是否仍在跑（用于提示分组此时不接收新信号） */
function isTaskActive(status: string): boolean {
  return ['pending', 'dispatching', 'running'].includes(status)
}

/** 子任务是否可手动下发平仓终止（未终态即可；平仓中允许重试） */
function isDispatchCloseable(status: string): boolean {
  return !['done', 'failed', 'skipped', 'offline'].includes(status)
}

const closingDispatchIds = ref<Record<string, boolean>>({})

async function closeDispatch(d: GroupTaskDispatchRecord): Promise<void> {
  if (!group.value || !isDispatchCloseable(d.status)) return
  const name = d.node_name || d.node_id
  const magic = d.magic != null ? ` · 魔术号 ${d.magic}` : ''
  if (
    !(await confirmAction(
      `确认对节点「${name}」下发平仓终止？\n将平掉该子任务对应魔术号的持仓并结束策略监控${magic}。`,
      '确认平仓',
    ))
  ) {
    return
  }
  const key = String(d.id)
  closingDispatchIds.value = { ...closingDispatchIds.value, [key]: true }
  try {
    const res = await hub.closeGroupDispatch(group.value.group_id, d.id)
    if (res.status === 'closing') {
      ElMessage.success(`已向 ${name} 下发平仓终止`)
    } else {
      ElMessage.warning(res.reason || `节点 ${name} 离线，已强制结束子任务，待重连后补发平仓`)
    }
    await refreshLiveData()
  } catch (e: unknown) {
    const detail =
      (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
      '平仓下发失败'
    ElMessage.error(typeof detail === 'string' ? detail : '平仓下发失败')
  } finally {
    const next = { ...closingDispatchIds.value }
    delete next[key]
    closingDispatchIds.value = next
  }
}

function dispatchSummary(row: GroupSignalTaskRecord): string {
  const n = row.dispatches.length
  if (!n) return row.skip_reason || '无节点处理'
  const done = row.dispatches.filter((d) => d.status === 'done').length
  const failed = row.dispatches.filter((d) => d.status === 'failed' || d.status === 'offline').length
  const running = row.dispatches.filter((d) =>
    ['opened', 'running', 'closing'].includes(d.status),
  ).length
  const parts: string[] = [`${n} 节点`]
  if (running) parts.push(`${running} 运行中`)
  if (done) parts.push(`${done} 完成`)
  if (failed) parts.push(`${failed} 失败`)
  return parts.join(' · ')
}

function eventTag(
  eventType: string,
  detail?: GroupTaskEventDetail | null,
): { cls: string; text: string } {
  // tpl_2 分散仓复用 add_trend（库字段长度限制），用 detail.kind 区分展示
  const kind = detail && typeof detail === 'object' ? String(detail.kind || '') : ''
  if (
    eventType === 'add_trend'
    && (kind === 'risk_sized_distribute' || kind === 'risk_sized_add')
  ) {
    return { cls: 'amber', text: '分散仓' }
  }
  const m: Record<string, { cls: string; text: string }> = {
    open: { cls: 'green', text: '开仓' },
    add_counter: { cls: 'amber', text: '逆势加仓' },
    add_trend: { cls: 'amber', text: '顺势加仓' },
    grid_add: { cls: 'amber', text: '网格买入' },
    grid_shift: { cls: 'blue', text: '网格平移' },
    breakeven: { cls: 'blue', text: '保本' },
    close_partial: { cls: 'blue', text: '部分平仓' },
    close_all: { cls: 'blue', text: '全部平仓' },
    error: { cls: 'red', text: '异常' },
    resume: { cls: '', text: '恢复' },
  }
  return m[eventType] || { cls: '', text: eventType }
}

onMounted(reload)
watch(groupId, reload)
watch(activeOnly, () => {
  page.value = 1
  expanded.value = {}
  expandedDispatch.value = {}
  dispatchEvents.value = {}
  loadingDispatchEvents.value = {}
  expandedEvent.value = {}
  void loadSignals()
})
watch(focusSignalId, () => {
  page.value = 1
  expanded.value = {}
  expandedDispatch.value = {}
  dispatchEvents.value = {}
  loadingDispatchEvents.value = {}
  expandedEvent.value = {}
  void loadSignals()
})

watch(
  autoRefresh,
  (on) => {
    if (on) {
      void refreshLiveData()
      startAutoRefresh()
    } else {
      stopAutoRefresh()
    }
  },
  { immediate: true },
)

onUnmounted(stopAutoRefresh)
</script>

<template>
  <div class="group-signals-page">
    <a class="node-link signals-back" @click="router.push('/groups')">← 返回分组列表</a>

    <div class="row between page-header signals-header">
      <div>
        <div class="h1">分组信号{{ group ? ` · ${group.name}` : '' }}</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          <template v-if="group && focusSignalId">
            正在查看信号 {{ focusSignalId }}
            <a class="node-link" style="margin-left: 8px" @click="clearSignalFocus">查看全部</a>
          </template>
          <template v-else-if="group">
            {{ activeOnly ? '进行中' : '全部' }}共 {{ total }} 条主任务 · 点击行展开信号明细与各节点处理过程
          </template>
          <template v-else-if="loadError">{{ loadError }}</template>
          <template v-else>加载中…</template>
        </p>
      </div>
      <div class="row signals-actions">
        <div class="row" style="gap: 4px">
          <button
            class="btn-sm"
            :class="activeOnly ? 'btn-ghost' : 'btn-primary'"
            :disabled="!group"
            @click="setActiveFilter(false)"
          >全部</button>
          <button
            class="btn-sm"
            :class="activeOnly ? 'btn-primary' : 'btn-ghost'"
            :disabled="!group"
            @click="setActiveFilter(true)"
          >仅进行中</button>
        </div>
        <label class="row muted auto-refresh-toggle" title="开启后每秒刷新主任务与已展开节点的关联订单">
          <input v-model="autoRefresh" type="checkbox" :disabled="!group" />
          <span>自动刷新</span>
          <span v-if="autoRefresh" class="auto-refresh-hint">1s</span>
        </label>
        <button class="btn-sm btn-ghost" :disabled="loading || !group" @click="loadSignals">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="group" class="card card-pad signals-panel">
      <div class="signals-scroll">
      <div v-if="signals.length" class="table-scroll">
        <table class="group-signal-table">
          <thead>
            <tr>
              <th style="width: 22px"></th>
              <th>时间</th><th>任务号</th><th>动作</th><th>品种</th>
              <th class="right">手数</th><th>分发模式</th><th>任务状态</th><th>节点处理</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="t in signals" :key="t.task_id">
              <tr
                class="clickable"
                :class="{ 'signal-focus': focusSignalId === t.signal_id }"
                :data-signal-id="t.signal_id"
                @click="toggleRow(t.task_id)"
              >
                <td class="muted">{{ isExpanded(t.task_id) ? '▾' : '▸' }}</td>
                <td class="muted" style="font-size: 12px">{{ fmtTime(t.created_at) }}</td>
                <td>#{{ t.task_id }}</td>
                <td>
                  <span
                    v-if="t.action"
                    class="tag"
                    :class="t.action === 'BUY' ? 'green' : t.action === 'SELL' ? 'blue' : ''"
                  >{{ t.action }}</span>
                  <span v-else class="muted">—</span>
                </td>
                <td>{{ t.symbol || '—' }}</td>
                <td class="right">{{ t.volume ?? '—' }}</td>
                <td class="muted" style="font-size: 12px">
                  {{ t.dispatch_mode === 'poll' ? '轮询轮转' : '全员同步' }}
                </td>
                <td><span class="tag" :class="taskTag(t.status).cls">{{ taskTag(t.status).text }}</span></td>
                <td class="muted" style="font-size: 12px">{{ dispatchSummary(t) }}</td>
              </tr>
              <tr v-if="isExpanded(t.task_id)" class="detail-row">
                <td></td>
                <td colspan="8">
                  <div class="kv-grid" style="margin: 6px 0 10px">
                    <div class="kv"><span class="k">信号 ID</span><span class="v" style="font-size: 12px">{{ t.signal_id }}</span></div>
                    <div class="kv"><span class="k">绑定策略</span><span class="v" style="font-size: 12px">{{ t.strategy_name || '—' }}</span></div>
                    <div class="kv"><span class="k">来源 IP</span><span class="v" style="font-size: 12px">{{ t.source_ip || '—' }}</span></div>
                    <div class="kv"><span class="k">SL</span><span class="v">{{ t.sl ?? '—' }}</span></div>
                    <div class="kv"><span class="k">TP</span><span class="v">{{ t.tp ?? '—' }}</span></div>
                    <div class="kv"><span class="k">备注</span><span class="v" style="font-size: 12px">{{ t.comment || '—' }}</span></div>
                    <div class="kv"><span class="k">下发节点数</span><span class="v">{{ t.node_count }}</span></div>
                    <div class="kv"><span class="k">累计下单</span><span class="v">{{ t.total_orders }} 笔 / {{ t.total_volume }} 手</span></div>
                    <div class="kv"><span class="k">已实现盈亏</span><span class="v">{{ t.realized_profit }}</span></div>
                    <div class="kv"><span class="k">开仓时间</span><span class="v" style="font-size: 12px">{{ fmtTime(t.opened_at) }}</span></div>
                    <div class="kv"><span class="k">完成时间</span><span class="v" style="font-size: 12px">{{ fmtTime(t.finished_at) }}</span></div>
                    <div v-if="t.skip_reason" class="kv span-full">
                      <span class="k">未下发原因</span><span class="v" style="font-size: 12px">{{ t.skip_reason }}</span>
                    </div>
                    <div v-if="isTaskActive(t.status)" class="kv span-full">
                      <span class="k">提示</span>
                      <span class="v" style="font-size: 12px">
                        任务进行中，下方运行中的节点该品种暂不接收新的策略信号；可在节点行点击「平仓」单独终止，或返回分组列表点「平仓」一键结束全部
                      </span>
                    </div>
                  </div>

                  <details class="payload-fold">
                    <summary>原始信号</summary>
                    <pre class="token-box group-payload">{{ fmtPayload(t.raw_payload) }}</pre>
                  </details>

                  <details class="payload-fold">
                    <summary>下发数据（发送给节点的命令）</summary>
                    <pre class="token-box group-payload">{{ fmtPayload(t.payload) }}</pre>
                  </details>

                  <div class="muted" style="font-size: 12px; margin-bottom: 8px">
                    各节点处理情况 · 点击节点行展开本次策略任务关联订单
                  </div>
                  <div v-if="t.dispatches.length" class="table-scroll">
                    <table class="group-detail-table">
                      <thead>
                        <tr>
                          <th style="width: 22px"></th>
                          <th>操作</th>
                          <th>节点</th><th>魔术号</th><th>状态</th><th class="right">首单手数</th>
                          <th class="right">持仓</th><th class="right">挂单</th>
                          <th class="right">加仓</th><th class="right">累计手数</th>
                          <th class="right">盈亏</th><th>结束原因</th>
                          <th>订单</th><th class="right">成交价</th>
                          <th>错误</th><th>下发时间</th><th>完成时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        <template v-for="d in t.dispatches" :key="d.id">
                          <tr class="clickable" @click="toggleDispatch(d.id)">
                            <td class="muted">{{ isDispatchExpanded(d.id) ? '▾' : '▸' }}</td>
                            <td @click.stop>
                              <button
                                v-if="isDispatchCloseable(d.status)"
                                type="button"
                                class="btn-sm btn-ghost"
                                :disabled="!!closingDispatchIds[String(d.id)]"
                                @click="closeDispatch(d)"
                              >
                                {{ closingDispatchIds[String(d.id)] ? '下发中…' : '平仓' }}
                              </button>
                              <span v-else class="muted" style="font-size: 12px">—</span>
                            </td>
                            <td>{{ d.node_name || d.node_id }}</td>
                            <td class="muted" style="font-size: 12px">{{ d.magic ?? '—' }}</td>
                            <td><span class="tag" :class="dispatchTag(d.status).cls">{{ dispatchTag(d.status).text }}</span></td>
                            <td class="right">{{ d.decided_vol ?? '—' }}</td>
                            <td class="right">{{ d.position_count }}</td>
                            <td class="right">{{ d.pending_orders || '—' }}</td>
                            <td class="right">{{ d.add_count }}</td>
                            <td class="right">{{ d.total_volume }}</td>
                            <td class="right">{{ d.realized_profit }}</td>
                            <td class="muted group-break">{{ d.finish_reason || d.skip_reason || '—' }}</td>
                            <td>{{ d.order ?? '—' }}</td>
                            <td class="right">{{ d.price ?? '—' }}</td>
                            <td class="muted group-break">{{ d.error || '—' }}</td>
                            <td class="muted" style="white-space: nowrap">{{ fmtTime(d.dispatched_at) }}</td>
                            <td class="muted" style="white-space: nowrap">{{ fmtTime(d.finished_at) }}</td>
                          </tr>
                          <tr v-if="isDispatchExpanded(d.id)" class="detail-row">
                            <td></td>
                            <td colspan="16">
                              <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                                关联订单 · {{ d.node_name || d.node_id }}
                                <template v-if="d.magic != null"> · 魔术号 {{ d.magic }}</template>
                              </div>
                              <div v-if="loadingDispatchEvents[String(d.id)]" class="muted" style="font-size: 13px">
                                加载中…
                              </div>
                              <div
                                v-else-if="(dispatchEvents[String(d.id)] || []).length"
                                class="table-scroll"
                              >
                                <table class="group-event-table">
                                  <thead>
                                    <tr>
                                      <th style="width: 22px"></th>
                                      <th title="北京时间（东八区）">时间</th>
                                      <th>类型</th>
                                      <th>动作</th>
                                      <th class="right">手数</th>
                                      <th class="right">成交价</th>
                                      <th>订单号</th>
                                      <th class="right">持仓</th>
                                      <th class="right">累计手数</th>
                                      <th class="right">盈亏</th>
                                      <th>开单原因</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    <template v-for="ev in dispatchEvents[String(d.id)]" :key="eventKey(d.id, ev)">
                                      <tr :class="ev.detail ? 'clickable' : ''" @click="toggleEvent(d.id, ev)">
                                        <td class="muted">
                                          <template v-if="ev.detail">{{ isEventExpanded(d.id, ev) ? '▾' : '▸' }}</template>
                                        </td>
                                        <td class="muted" style="white-space: nowrap">{{ fmtTime(ev.created_at) }}</td>
                                        <td>
                                          <span class="tag" :class="eventTag(ev.event_type, ev.detail).cls">
                                            {{ eventTag(ev.event_type, ev.detail).text }}
                                          </span>
                                        </td>
                                        <td>{{ ev.action || '—' }}</td>
                                        <td class="right">{{ ev.volume ?? '—' }}</td>
                                        <td class="right">{{ ev.price ?? '—' }}</td>
                                        <td>{{ ev.order_ticket ?? '—' }}</td>
                                        <td class="right">{{ ev.position_count ?? '—' }}</td>
                                        <td class="right">{{ ev.total_volume ?? '—' }}</td>
                                        <td class="right">{{ ev.profit ?? '—' }}</td>
                                        <td class="muted group-break">{{ ev.message || '—' }}</td>
                                      </tr>
                                      <tr v-if="ev.detail && isEventExpanded(d.id, ev)" class="detail-row">
                                        <td></td>
                                        <td colspan="10">
                                          <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                                            计算依据 · 订单 {{ ev.order_ticket ?? '—' }}
                                          </div>
                                          <div class="kv-grid event-detail-grid">
                                            <div v-for="row in detailRows(ev.detail)" :key="row.k" class="kv">
                                              <span class="k">{{ row.k }}</span>
                                              <span class="v">{{ row.v }}</span>
                                            </div>
                                          </div>
                                        </td>
                                      </tr>
                                    </template>
                                  </tbody>
                                </table>
                              </div>
                              <div v-else class="muted" style="font-size: 13px">
                                该节点本次策略任务暂无关联订单记录。
                              </div>
                            </td>
                          </tr>
                        </template>
                      </tbody>
                    </table>
                  </div>
                  <div v-else class="muted" style="font-size: 13px">该主任务未产生节点下发明细。</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <div v-else-if="loading" class="muted" style="font-size: 13px; padding: 8px 0">加载中…</div>
      <div v-else class="muted" style="font-size: 13px; padding: 8px 0">
        {{ focusSignalId ? '未找到该信号，可能已被清空。' : '该分组暂无信号记录。' }}
      </div>
      </div>

      <div v-if="total > 0" class="pagination signals-foot">
        <span class="muted pagination-info">
          共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页
        </span>
        <div class="row pagination-actions">
          <select v-model.number="pageSize" class="pagination-size" @change="onPageSizeChange">
            <option :value="10">10 条/页</option>
            <option :value="20">20 条/页</option>
            <option :value="50">50 条/页</option>
          </select>
          <button class="btn-sm btn-ghost" :disabled="loading || page <= 1" @click="goPage(page - 1)">
            上一页
          </button>
          <button
            class="btn-sm btn-ghost"
            :disabled="loading || page >= totalPages"
            @click="goPage(page + 1)"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 固定为浏览器可视高度：顶栏 60px + content 上下 padding 48px = 108px */
.group-signals-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
  min-width: 0;
  height: calc(100vh - 108px);
  height: calc(100dvh - 108px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
  min-height: 0;
  overflow: hidden;
}

.signals-back {
  flex-shrink: 0;
  font-size: 13px;
}

.signals-header {
  flex-shrink: 0;
  margin: 0;
}

.signals-actions {
  gap: 12px;
  flex-shrink: 0;
}

.auto-refresh-toggle {
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}

.auto-refresh-toggle input {
  width: auto;
  margin: 0;
  cursor: pointer;
}

.auto-refresh-toggle:has(input:disabled) {
  cursor: not-allowed;
  opacity: 0.6;
}

.auto-refresh-hint {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--primary);
}

.signals-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.signals-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  -webkit-overflow-scrolling: touch;
}

.signals-foot {
  flex-shrink: 0;
  margin-top: 12px;
}

@media (max-width: 640px) {
  /* 小屏顶栏约 56px+、content padding 14*2 */
  .group-signals-page {
    height: calc(100vh - 84px);
    height: calc(100dvh - 84px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
  }
}

.group-signal-table,
.group-detail-table,
.group-event-table {
  width: 100%;
}

/* 多列表格保持足够最小宽度，避免状态标签被挤成竖排 */
.group-signal-table {
  min-width: 860px;
}

.group-signal-table tr.signal-focus td {
  background: rgba(0, 212, 170, 0.12);
}

.group-detail-table {
  min-width: 1280px;
}

.group-event-table {
  min-width: 960px;
}

.group-signal-table th,
.group-signal-table td,
.group-detail-table th,
.group-detail-table td,
.group-event-table th,
.group-event-table td {
  font-size: 12px;
  vertical-align: middle;
}

.group-signal-table .tag,
.group-detail-table .tag,
.group-event-table .tag {
  white-space: nowrap;
}

.group-event-table {
  margin-top: 2px;
  background: rgba(255, 255, 255, 0.02);
}

.event-detail-grid {
  margin: 2px 0 8px;
  gap: 10px 14px;
}

.event-detail-grid .v {
  font-size: 12px;
  word-break: break-word;
}

.group-payload {
  width: 100%;
  max-width: 100%;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
}

.payload-fold {
  margin-bottom: 10px;
}

.payload-fold > summary {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
  list-style: none;
}

.payload-fold > summary::-webkit-details-marker {
  display: none;
}

.payload-fold > summary::before {
  content: '▸';
  font-size: 11px;
}

.payload-fold[open] > summary::before {
  content: '▾';
}

.payload-fold > summary:hover {
  color: var(--text);
}

.payload-fold[open] > summary {
  margin-bottom: 6px;
}

.group-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}
</style>
