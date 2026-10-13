<script setup lang="ts">
// 手动触发策略信号：按钮 + 表单弹窗 + 二次确认
// 与 Webhook 的 model=strategy 走同一条分组分发链路，只是入口换成后台操作。
// 普通用户只作用于本人分组（后端收窄），也拿不到全局 Webhook token，不提供复制 JSON
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import FormLabel from '@/components/FormLabel.vue'
import StrategySignalFields from '@/components/StrategySignalFields.vue'
import TagSelect from '@/components/TagSelect.vue'
import { signalSymbolMatches } from '@/components/strategySignal'
import type { TagOption } from '@/components/tagOption'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import type {
  BatchTimeframe,
  GroupDispatchMode,
  GroupOut,
  ManualSignalAction,
  ManualSignalPayload,
  ManualSignalResult,
  ManualSmcLevel,
  StrategyOut,
  StrategyRule,
} from '@/api/types'

const emit = defineEmits<{
  accepted: []
}>()

const hub = useHubStore()
const authStore = useAuthStore()

const DISPATCH_MODE_LABEL: Record<GroupDispatchMode, string> = {
  sync: '全员同步',
  poll: '轮询轮转（单节点领取）',
}

const TRIGGER_HELP = {
  kind:
    '交易信号按 model=strategy 下发 BUY / SELL / CLOSE。' +
    'SMC 价位按 type=smc-data 推送，只更新正在跑、且逆势分批档位用了同一周期的任务，不会新开仓。',
  template_ids:
    '策略模版定向（信号的 template_ids 字段）：勾选后，只有绑定了这些模版的分组才会收到本信号，' +
    '在品种匹配之上再加一层筛选。一个都不勾表示不限制模版。SMC 价位同样按模版收窄下面的分组列表。',
  group_ids:
    '分组定向（信号的 group_ids 字段）：勾选后，只有勾中的分组会收到本信号。' +
    '一个都不勾表示下面列出的分组全部收到。SMC 价位勾选后只更新这些分组里进行中的任务。',
  tf:
    '与逆势分批档位里的 K 线周期对应，只作匹配。可选 M1 / M5 / M15 / M30 / H1 / H4 / D1 / W1 / MN。',
  smc_type:
    '与档位的计算方式对应：FVG / OB / PDZ / FIBO。节点不计算这些指标。',
  smc_side:
    '1 为支撑，多单逆势加仓只用支撑；-1 为阻力，空单只用阻力。',
  smc_price: '现价到达或越过这一价位时，节点按该档手数市价加仓。',
}

type SignalKind = 'trade' | 'smc'
type SmcCalcType = ManualSmcLevel['type']

const KIND_TAGS: TagOption<SignalKind>[] = [
  { value: 'trade', label: '交易信号', hint: 'BUY / SELL / CLOSE' },
  { value: 'smc', label: 'SMC 价位', hint: 'type=smc-data' },
]
const TF_TAGS: TagOption<BatchTimeframe>[] = (
  ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN'] as BatchTimeframe[]
).map((tf) => ({ value: tf, label: tf }))
const SMC_TYPE_TAGS: TagOption<SmcCalcType>[] = [
  { value: 'fvg', label: 'FVG' },
  { value: 'ob', label: 'OB' },
  { value: 'pdz', label: 'PDZ' },
  { value: 'fibo', label: 'FIBO' },
]
const SMC_SIDE_TAGS: TagOption<1 | -1>[] = [
  { value: 1, label: '支撑', hint: '多单' },
  { value: -1, label: '阻力', hint: '空单' },
]

interface SmcRow {
  type: SmcCalcType
  side: 1 | -1
  price: number | null
}

function blankSmcRow(): SmcRow {
  return { type: 'fvg', side: 1, price: null }
}

const signalKind = ref<SignalKind>('trade')
const showTrigger = ref(false)
const triggering = ref(false)
const triggerError = ref('')
const triggerGroups = ref<GroupOut[]>([])
const loadingTriggerGroups = ref(false)
/** Webhook AUTH_TOKEN，写入可复制的信号 JSON，不随手动触发接口提交。 */
const webhookAuthToken = ref('')

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
  tf: 'M5' as BatchTimeframe,
  smcLevels: [blankSmcRow()] as SmcRow[],
})

const isSmc = computed(() => signalKind.value === 'smc')
const isCloseAction = computed(() => !isSmc.value && triggerForm.action === 'CLOSE')

function strategyOf(g: GroupOut): StrategyOut | undefined {
  return g.strategy_id ? hub.strategies.find((s) => s.strategy_id === g.strategy_id) : undefined
}

function strategyLabel(s: StrategyOut): string {
  const status = s.enabled ? '' : '（已禁用）'
  return `${s.name} · ${s.symbol}${status}`
}

function groupStrategyLabel(g: GroupOut): string {
  const sty = strategyOf(g)
  return sty ? strategyLabel(sty) : '未绑定'
}

const triggerSymbolOptions = computed<string[]>(() => {
  const out: string[] = []
  for (const g of triggerGroups.value) {
    const symbol = strategyOf(g)?.symbol
    if (symbol && !out.includes(symbol)) out.push(symbol)
  }
  return out
})

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

function ruleWatchesSmc(rule: StrategyRule, tf: string, types: SmcCalcType[]): boolean {
  if (rule.type !== 1 || !rule.status || !rule.batch_enabled) return false
  return (rule.batch_levels || []).some((lv) => {
    if ((lv.timeframe || 'M5') !== tf) return false
    const calc = lv.calc_type
    if (calc !== 'fvg' && calc !== 'ob' && calc !== 'pdz' && calc !== 'fibo') return false
    return !types.length || types.includes(calc)
  })
}

const selectedSmcTypes = computed<SmcCalcType[]>(() => {
  const out: SmcCalcType[] = []
  for (const row of triggerForm.smcLevels) {
    if (!out.includes(row.type)) out.push(row.type)
  }
  return out
})

const candidateGroups = computed<GroupOut[]>(() => {
  const symbol = triggerForm.symbol.trim()
  if (!symbol) return []
  const templates = triggerForm.template_ids
  return triggerGroups.value.filter((g) => {
    if (!g.enabled) return false
    const sty = strategyOf(g)
    if (!sty || !sty.enabled || !signalSymbolMatches(sty.symbol, symbol)) return false
    if (templates.length && !templates.includes(sty.template_id)) return false
    if (!isSmc.value) return true
    return (sty.rules || []).some((rule) => ruleWatchesSmc(rule, triggerForm.tf, selectedSmcTypes.value))
  })
})

const matchedGroups = computed<GroupOut[]>(() => {
  const picked = triggerForm.group_ids
  if (!picked.length) return candidateGroups.value
  return candidateGroups.value.filter((g) => picked.includes(g.group_id))
})

function toggleTriggerGroup(groupId: string, checked: boolean): void {
  const kept = triggerForm.group_ids.filter((id) => id !== groupId)
  triggerForm.group_ids = checked ? [...kept, groupId] : kept
}

watch(candidateGroups, (list) => {
  if (!triggerForm.group_ids.length) return
  const ids = new Set(list.map((g) => g.group_id))
  const kept = triggerForm.group_ids.filter((id) => ids.has(id))
  if (kept.length !== triggerForm.group_ids.length) triggerForm.group_ids = kept
})

const readyGroupCount = computed(
  () => matchedGroups.value.filter((g) => g.online_node_count > 0).length,
)

function addSmcRow(): void {
  triggerForm.smcLevels.push(blankSmcRow())
}

function removeSmcRow(index: number): void {
  if (triggerForm.smcLevels.length <= 1) {
    triggerForm.smcLevels[0] = blankSmcRow()
    return
  }
  triggerForm.smcLevels.splice(index, 1)
}

async function openTrigger(): Promise<void> {
  triggerError.value = ''
  signalKind.value = 'trade'
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
    tf: 'M5' as BatchTimeframe,
    smcLevels: [blankSmcRow()],
  })
  triggerGroups.value = []
  showTrigger.value = true
  loadingTriggerGroups.value = true
  try {
    const [groups, , auth] = await Promise.all([
      hub.listGroups(),
      hub.fetchStrategies(),
      authStore.isAdmin
        ? hub.fetchWebhookAuth().catch(() => ({ enabled: false, token: '' }))
        : Promise.resolve({ enabled: false, token: '' }),
    ])
    triggerGroups.value = groups
    webhookAuthToken.value = auth.token || ''
  } catch {
    triggerError.value = '读取分组失败，命中范围暂时无法预演；请关闭弹窗后重试'
    return
  } finally {
    loadingTriggerGroups.value = false
  }
  const options = triggerSymbolOptions.value
  if (options.length === 1) triggerForm.symbol = options[0]
}

function buildSmcLevels(): ManualSmcLevel[] | null {
  const levels: ManualSmcLevel[] = []
  for (const row of triggerForm.smcLevels) {
    const price = Number(row.price)
    if (!(price > 0)) return null
    levels.push({ type: row.type, side: row.side, price })
  }
  return levels.length ? levels : null
}

function buildTriggerPayload(symbol: string): ManualSignalPayload {
  if (isSmc.value) {
    const payload: ManualSignalPayload = {
      type: 'smc-data',
      symbol,
      tf: triggerForm.tf,
      data: buildSmcLevels() || [],
    }
    if (triggerForm.template_ids.length) payload.template_ids = [...triggerForm.template_ids]
    if (triggerForm.group_ids.length) {
      payload.group_ids = [...triggerForm.group_ids]
    } else if (triggerForm.template_ids.length) {
      payload.group_ids = candidateGroups.value.map((g) => g.group_id)
    }
    return payload
  }
  const payload: ManualSignalPayload = { symbol, action: triggerForm.action, model: 'strategy' }
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

function buildWebhookSignalPayload(payload: ManualSignalPayload): Record<string, unknown> {
  if (payload.type === 'smc-data') {
    const data: Record<string, unknown> = {
      type: 'smc-data',
      symbol: payload.symbol,
      tf: payload.tf,
      data: payload.data || [],
    }
    if (authStore.isAdmin) data.token = webhookAuthToken.value
    return data
  }
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
  if (authStore.isAdmin) data.token = webhookAuthToken.value
  return data
}

function triggerSummary(payload: ManualSignalPayload): string {
  if (payload.type === 'smc-data') {
    const hit = matchedGroups.value
    const groupText = hit.length
      ? hit.map((g) => `· ${g.name}（进行中任务 ${g.active_task_count}）`).join('\n')
      : '· 无（没有启用中的逆势档位使用该周期，推送不会写入任何任务）'
    const rows = (payload.data || [])
      .map((row) => `${row.type} ${row.side === 1 ? '支撑' : '阻力'} ${row.price}`)
      .join('、')
    const lines = [
      `品种：${payload.symbol}`,
      `类型：smc-data`,
      `周期：${payload.tf}`,
      `价位：${rows || '无'}`,
    ]
    if (payload.group_ids?.length) {
      lines.push(`分组定向：只更新勾选的 ${payload.group_ids.length} 个分组里进行中的任务`)
    } else {
      lines.push('分组定向：更新下面全部分组里进行中、且档位匹配的任务')
    }
    lines.push('说明：整份覆盖该任务当前价位，不新开仓。同一条记录在每个节点只用一次。')
    return `${lines.join('\n')}\n\n预计涉及分组：\n${groupText}`
  }
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

function reportTriggerResult(payload: ManualSignalPayload, res: ManualSignalResult): boolean {
  if (payload.type === 'smc-data') {
    const tasks = typeof res.tasks === 'number' ? res.tasks : 0
    const nodes = res.nodes ?? 0
    const levels = res.levels ?? payload.data?.length ?? 0
    if (tasks > 0) {
      ElMessage.success(`已推送 SMC 价位：${levels} 条，写入 ${tasks} 个任务，${nodes} 个节点`)
    } else {
      ElMessage.warning(`SMC 价位已受理，但没有进行中的任务匹配 ${payload.symbol} ${payload.tf}`)
    }
    return true
  }
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

function acceptedButNotDispatchedHint(res: ManualSignalResult): string {
  const rows = Array.isArray(res.tasks) ? res.tasks : []
  const reasons = [
    ...new Set(
      rows
        .map((t) => (t.reason || '').trim())
        .filter(Boolean),
    ),
  ]
  if (reasons.length) return reasons.join('；')
  return '分组无有效节点，或未能下发到任何节点'
}

function submitTrigger(): void {
  const symbol = triggerForm.symbol.trim().toUpperCase()
  if (!symbol) {
    triggerError.value = '请填写信号品种'
    return
  }
  if (isSmc.value) {
    if (!TF_TAGS.some((tag) => tag.value === triggerForm.tf)) {
      triggerError.value = '请选择 K 线周期'
      return
    }
    if (!buildSmcLevels()) {
      triggerError.value = '每条价位都需要大于 0 的价格'
      return
    }
    if (triggerForm.template_ids.length && !candidateGroups.value.length) {
      triggerError.value = '没有匹配该模版和周期的分组'
      return
    }
  } else if (!isCloseAction.value && !(Number(triggerForm.volume) > 0)) {
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
    if (res.status === 'accepted') emit('accepted')
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
</script>

<template>
  <button type="button" class="btn-ghost" @click="openTrigger">手动触发信号</button>

  <Teleport to="body">
    <div v-if="showTrigger" class="modal-mask" @click.self="showTrigger = false">
      <div class="card card-pad modal modal-lg group-trigger-modal">
        <div class="modal-header">
          <div class="h1">手动触发信号</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            <template v-if="isSmc">
              等同于收到一条 <code>type=smc-data</code> 的 Webhook：按品种和 K 线周期，
              覆盖进行中任务的 SMC 价位。不新开仓，也不走按币种分发。
            </template>
            <template v-else>
              等同于收到一条 <code>model=strategy</code> 的 Webhook：按品种匹配「已启用且绑定同品种启用策略」的分组，
              各分组再按自己的分发模式下发给有效节点。不影响按币种分发（<code>model=normal</code>）的链路。
            </template>
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid" style="margin-bottom: 14px">
            <div class="span-full">
              <FormLabel text="信号类型" :help="TRIGGER_HELP.kind" />
              <TagSelect v-model="signalKind" :options="KIND_TAGS" aria-label="信号类型" />
            </div>
          </div>
          <StrategySignalFields
            v-if="!isSmc"
            v-model="triggerForm"
            id-prefix="trigger"
            :symbol-options="triggerSymbolOptions"
          />
          <div v-else class="form-grid two">
            <div>
              <FormLabel
                field-id="trigger-smc-symbol"
                text="信号品种"
                help="与策略绑定品种按前缀匹配，例如 XAUUSDT 可以命中 XAUUSD。只更新该品种上正在跑的任务。"
              />
              <input id="trigger-smc-symbol" v-model="triggerForm.symbol" placeholder="例如：XAUUSD" />
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
              <FormLabel text="K 线周期" :help="TRIGGER_HELP.tf" />
              <TagSelect v-model="triggerForm.tf" :options="TF_TAGS" aria-label="K 线周期" />
            </div>
            <div class="span-full">
              <FormLabel text="价位" :help="TRIGGER_HELP.smc_price" />
              <div class="smc-levels">
                <div v-for="(row, index) in triggerForm.smcLevels" :key="index" class="smc-level">
                  <TagSelect v-model="row.type" :options="SMC_TYPE_TAGS" aria-label="订单块类型" />
                  <TagSelect v-model="row.side" :options="SMC_SIDE_TAGS" aria-label="支撑或阻力" />
                  <input
                    v-model.number="row.price"
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder="价格"
                    :aria-label="`第 ${index + 1} 条价位`"
                  />
                  <button type="button" class="btn-sm btn-ghost" @click="removeSmcRow(index)">删除</button>
                </div>
                <button type="button" class="btn-sm btn-ghost" @click="addSmcRow">添加价位</button>
              </div>
            </div>
          </div>

          <div class="form-grid" style="margin-top: 14px">
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
                <template v-if="isSmc">
                  没有匹配的分组：该品种下没有启用中的逆势分批档位使用 {{ triggerForm.tf }}
                  <template v-if="selectedSmcTypes.length">（{{ selectedSmcTypes.join(' / ') }}）</template>。
                  推送不会写入任何任务。
                </template>
                <template v-else>
                  没有匹配的分组：该品种下没有「已启用且绑定同品种启用策略」<template
                    v-if="triggerForm.template_ids.length"
                  >且模版在定向范围内</template>的分组，信号会被拒收。
                </template>
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
                    已定向到勾选的 {{ matchedGroups.length }} 个分组，其余分组不会收到。
                  </template>
                  <template v-else-if="isSmc">
                    上面 {{ candidateGroups.length }} 个分组里、正在跑且档位匹配的任务都会被覆盖；勾选后只更新勾中的分组。
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

    <div v-if="showTriggerConfirm && pendingTriggerPayload" class="modal-mask trigger-confirm-mask" @click.self="cancelTriggerConfirm">
      <div class="card card-pad modal trigger-confirm-modal">
        <div class="modal-header">
          <div class="h1">确认触发信号</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            {{ isSmc ? '确认推送 SMC 价位？可展开查看 Webhook 同构 JSON。' : '确认手动触发 strategy 信号？可展开查看 Webhook 同构 JSON。' }}
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
                <button
                  v-if="authStore.isAdmin"
                  type="button"
                  class="btn-sm btn-ghost"
                  @click.stop="copyPendingTriggerPayload"
                >
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
  </Teleport>
</template>

<style scoped>
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

.member-name {
  font-weight: 500;
}

.member-meta {
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

.group-pick-off {
  opacity: 0.45;
}

.smc-levels {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 6px;
}

.smc-level {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.smc-level input {
  width: 140px;
}

.symbol-picks {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
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
  .group-trigger-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .member-row {
    flex-wrap: wrap;
  }
}
</style>
