<script setup lang="ts">
// 手动触发策略信号：按钮 + 表单弹窗 + 二次确认
// 与 Webhook 的 model=strategy 走同一条分组分发链路，只是入口换成后台管理员操作
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  GroupDispatchMode,
  GroupOut,
  ManualSignalAction,
  ManualSignalPayload,
  ManualSignalResult,
  StrategyOut,
} from '@/api/types'

const emit = defineEmits<{
  accepted: []
}>()

const hub = useHubStore()

const DISPATCH_MODE_LABEL: Record<GroupDispatchMode, string> = {
  sync: '全员同步',
  poll: '轮询轮转（单节点领取）',
}

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

const showTrigger = ref(false)
const triggering = ref(false)
const triggerError = ref('')
const triggerGroups = ref<GroupOut[]>([])
const loadingTriggerGroups = ref(false)

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

function normalizeSymbolKey(symbol: string): string {
  return (symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
}

function symbolMatch(strategySymbol: string, signalSymbol: string): boolean {
  const a = normalizeSymbolKey(strategySymbol)
  const b = normalizeSymbolKey(signalSymbol)
  if (!a || !b) return false
  return a.startsWith(b) || b.startsWith(a)
}

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
    triggerError.value = '读取分组失败，命中范围暂时无法预演；请关闭弹窗后重试'
    return
  } finally {
    loadingTriggerGroups.value = false
  }
  const options = triggerSymbolOptions.value
  if (options.length === 1) triggerForm.symbol = options[0]
}

function buildTriggerPayload(symbol: string): ManualSignalPayload {
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

function submitTrigger(): void {
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
