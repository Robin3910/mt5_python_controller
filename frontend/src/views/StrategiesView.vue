<script setup lang="ts">
// 策略管理页：基于模版新建策略、绑定品种，选模版后可自定义规则参数
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  BatchCalcType,
  BatchTimeframe,
  StrategyBatchLevel,
  StrategyOut,
  StrategyRule,
  StrategyTemplateOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

/** ATR / 波幅统计的已收盘 K 线根数，与后端 BATCH_BAR_PERIOD 一致，固定不可配 */
const BATCH_BAR_PERIOD = 14

const hub = useHubStore()
const router = useRouter()

const searchQuery = ref('')
const appliedQuery = ref('')
const loading = ref(false)
const templates = ref<StrategyTemplateOut[]>([])

function currentSearchOptions(): { q?: string } {
  return appliedQuery.value ? { q: appliedQuery.value } : {}
}

async function loadStrategies(): Promise<void> {
  loading.value = true
  try {
    await hub.fetchStrategies(currentSearchOptions())
  } finally {
    loading.value = false
  }
}

async function runSearch(): Promise<void> {
  appliedQuery.value = searchQuery.value.trim()
  await loadStrategies()
}

onMounted(async () => {
  await Promise.all([
    loadStrategies(),
    hub.fetchStrategyTemplates().then((list) => {
      templates.value = list
    }),
  ])
})

const RULE_TYPE_LABEL: Record<number, string> = {
  1: '逆势加仓',
  2: '顺势加仓',
}

const RULE_TYPE_HELP: Record<number, string> = {
  1:
    '逆势加仓：以监控方向最近一笔订单为基准，价格朝不利方向偏离达到「点数 × Point()」后触发加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
  2:
    '顺势加仓：以监控方向最近一笔订单为基准，价格朝有利方向偏离达到「点数 × Point()」后触发加仓。' +
    '实际手数 = 倍数 × 基础订单手数 + 额外手数。',
}

const FIELD_HELP = {
  template: '选择策略模版后，会载入模版默认规则；可在下方自定义各参数后再创建。',
  name: '策略名称，全局唯一。便于在列表与审计中辨识。',
  symbol: '绑定品种代码，如 XAUUSD。策略规则仅作用于该品种。',
  rules:
    '每条规则独立配置。关闭「启用」后该规则不会执行。' +
    '实际加仓手数 = 倍数 × 基础订单手数 + 额外手数；触发距离 = 点数 × Point()。',
  status:
    '关闭后本条规则不会参与监控与加仓；已产生的历史订单不受影响。',
  action:
    '监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '系统会按该方向取最近一笔订单作为加仓基准。',
  point:
    '加仓触发点数。实时以监控方向最近订单为基准，价格偏离达到「点数 × Point()」后开始执行加仓。' +
    'Point() 为品种最小价格变动单位。',
  lot_times:
    '加仓倍数。以监控方向最近订单的手数为基准，参与计算：倍数 × 基础手数。',
  extra_lot:
    '额外手数，默认 0。加在倍率结果之后：实际手数 = 倍数 × 基础手数 + 额外手数。',
  max_allow_num:
    '最大允许加仓次数。达到次数上限后，本条规则不再继续加仓。',
  batch_enabled:
    '开启后按当前持仓笔数命中下方档位，使用该档的点数 / 倍数 / 额外手数，' +
    '覆盖上方基础参数。档位由批数与总手数自动生成，不可手动增删。',
  batch_action:
    '分批加仓独立的监控方向：全部=多空都监控，多单=只监控多单，空单=只监控空单。' +
    '可与上方基础监控方向不同。',
  batch_count:
    '分批批数。修改后会按「第 2 笔 ~ 总手数」自动均分生成对应档位区间，不可手动添加档位。',
  total_lot_limit:
    '分批总手数上限，同时作为档位末笔上限。' +
    '系统会把第 2 笔到该上限均分到各档；达到上限后不再继续分批加仓。',
  batch_level:
    '持仓笔数区间由批数与总手数自动计算，只读。' +
    '命中区间时按该档的间距判断是否加仓，手数 = 倍数 × 基础手数 + 额外手数。',
  calc_type:
    '本档加仓间距怎么算：\n' +
    '点数 = 固定间距，偏离达到「点数 × Point()」触发；\n' +
    '指定价 = 到价触发，多单要求价位低于参考价、空单要求高于参考价；\n' +
    `ATR = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线的平均真实波幅作间距；\n` +
    `波幅 = 用 ${BATCH_BAR_PERIOD} 根已收盘 K 线中最大的高低波幅作间距。`,
  batch_price:
    '本档的指定价位（绝对价格）。逆势时多单需低于参考价、空单需高于参考价；' +
    '顺势方向相反。价位方向不符或留空时本档不会触发。',
  batch_timeframe:
    `统计 ATR / 波幅用的 K 线周期，固定取最近 ${BATCH_BAR_PERIOD} 根**已收盘** K 线（不含当前未走完的那根）。` +
    '算出的价格距离会换算成点数，再与实际偏离比较。',
}

/** 表单内规则：分批字段均已填充，便于直接 v-model 绑定 */
type EditableRule = StrategyRule & {
  batch_enabled: boolean
  batch_action: string
  batch_count: number
  total_lot_limit: number
  batch_levels: StrategyBatchLevel[]
}

function cloneRules(rules: StrategyRule[]): EditableRule[] {
  return rules.map((r) => ({
    type: r.type,
    status: r.status,
    action: r.action,
    point: r.point,
    lot_times: r.lot_times,
    extra_lot: r.extra_lot,
    max_allow_num: r.max_allow_num,
    batch_enabled: r.batch_enabled ?? false,
    batch_action: r.batch_action ?? 'all',
    batch_count: r.batch_count ?? 0,
    total_lot_limit: r.total_lot_limit ?? 0,
    batch_levels: (r.batch_levels || []).map((lv) => ({ ...lv })),
  }))
}

/** 分批档位从第 2 笔起算（第 1 笔为首单） */
const BATCH_POS_START = 2

/** 加仓间距的计算方式；与后端 strategy_templates.BATCH_CALC_TYPES 对齐 */
const CALC_TYPE_OPTIONS: Array<{ value: BatchCalcType; label: string }> = [
  { value: 'point', label: '点数' },
  { value: 'price', label: '指定价' },
  { value: 'atr', label: 'ATR' },
  { value: 'range', label: '波幅' },
]

/** ATR / 波幅可选周期。不提供「当前图表周期」：节点是独立进程，没有图表上下文 */
const TIMEFRAME_OPTIONS: BatchTimeframe[] = [
  'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN',
]

/** 需要读 K 线才能算出间距的方式 */
const BAR_CALC_TYPES: BatchCalcType[] = ['atr', 'range']

function isBarCalc(calcType: BatchCalcType): boolean {
  return BAR_CALC_TYPES.includes(calcType)
}

/** 将 [2, limit] 均分为 count 段，对齐 MTcommander「批数 × 总手数」切档 */
function splitBatchRanges(
  limit: number,
  count: number,
): Array<{ pos_from: number; pos_to: number }> {
  const end = Math.max(BATCH_POS_START, Math.floor(limit) || BATCH_POS_START)
  const total = end - BATCH_POS_START + 1
  const n = Math.max(1, Math.min(Math.floor(count) || 1, total))
  const base = Math.floor(total / n)
  const rem = total % n
  const ranges: Array<{ pos_from: number; pos_to: number }> = []
  let cur = BATCH_POS_START
  for (let i = 0; i < n; i++) {
    const size = base + (i < rem ? 1 : 0)
    const to = cur + size - 1
    ranges.push({ pos_from: cur, pos_to: to })
    cur = to + 1
  }
  return ranges
}

type LevelParams = Pick<
  StrategyBatchLevel,
  'calc_type' | 'point' | 'price' | 'timeframe' | 'lot_times' | 'extra_lot'
>

function defaultLevelParams(index: number, prev?: StrategyBatchLevel): LevelParams {
  if (prev) {
    return {
      calc_type: prev.calc_type || 'point',
      point: prev.point,
      price: prev.price ?? 0,
      timeframe: prev.timeframe || 'M5',
      lot_times: prev.lot_times,
      extra_lot: prev.extra_lot,
    }
  }
  return {
    calc_type: 'point',
    point: 100 + index * 100,
    price: 0,
    timeframe: 'M5',
    lot_times: Number((1.1 + index * 0.1).toFixed(2)),
    extra_lot: 0,
  }
}

/** 按 batch_count + total_lot_limit 重建档位；保留已有档的点/倍/手参数 */
function rebuildBatchLevels(r: EditableRule): void {
  const limit = Math.max(BATCH_POS_START, Math.floor(Number(r.total_lot_limit) || BATCH_POS_START))
  r.total_lot_limit = limit
  const maxCount = limit - BATCH_POS_START + 1
  let count = Math.max(1, Math.floor(Number(r.batch_count) || 1))
  if (count > maxCount) count = maxCount
  r.batch_count = count

  const old = r.batch_levels
  r.batch_levels = splitBatchRanges(limit, count).map((range, i) => ({
    ...range,
    ...defaultLevelParams(i, old[i]),
  }))
}

function setBatchEnabled(r: EditableRule, enabled: boolean): void {
  r.batch_enabled = enabled
  if (!enabled) return
  if (!r.batch_count) r.batch_count = 3
  if (!r.total_lot_limit || r.total_lot_limit < BATCH_POS_START) r.total_lot_limit = 10
  rebuildBatchLevels(r)
}

function onBatchMetaChange(r: EditableRule): void {
  if (!r.batch_enabled) return
  rebuildBatchLevels(r)
}

// ---- 新建 / 编辑 ----
const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const editingTemplateName = ref('')
const saving = ref(false)
const formError = ref('')
const form = reactive({
  template_id: '',
  name: '',
  symbol: '',
  rules: [] as EditableRule[],
})

const isEditMode = computed(() => formMode.value === 'edit')

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.template_id === form.template_id) || null,
)

const formTemplateLabel = computed(() => {
  if (selectedTemplate.value) return selectedTemplate.value.name
  return editingTemplateName.value || form.template_id || '—'
})

function loadRulesFromTemplate(templateId: string): void {
  const tpl = templates.value.find((t) => t.template_id === templateId)
  form.rules = tpl ? cloneRules(tpl.rules) : []
}

watch(
  () => form.template_id,
  (id) => {
    // 仅新建时切换模版会重载默认规则；编辑不允许改模版
    if (showForm.value && !isEditMode.value && id) loadRulesFromTemplate(id)
  },
)

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  editingTemplateName.value = ''
  formError.value = ''
  form.template_id = templates.value[0]?.template_id || ''
  form.name = ''
  form.symbol = ''
  loadRulesFromTemplate(form.template_id)
  showForm.value = true
}

function openEdit(s: StrategyOut): void {
  formMode.value = 'edit'
  editingId.value = s.strategy_id
  editingTemplateName.value = s.template_name || ''
  formError.value = ''
  form.template_id = s.template_id
  form.name = s.name
  form.symbol = s.symbol
  form.rules = cloneRules(s.rules || [])
  showForm.value = true
}

function validateRules(rules: EditableRule[]): string | null {
  if (!rules.length) return '请至少配置一条规则'
  for (const r of rules) {
    const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
    if (r.point < 0) return `${label}：点数不能为负`
    if (r.lot_times < 0) return `${label}：倍数不能为负`
    if (r.extra_lot < 0) return `${label}：手数不能为负`
    if (r.max_allow_num < 0) return `${label}：次数不能为负`
    if (!['all', 'buy', 'sell'].includes(String(r.action || '').toLowerCase())) {
      return `${label}：监控方向非法`
    }
    if (!r.batch_enabled) continue
    if (!['all', 'buy', 'sell'].includes(String(r.batch_action || '').toLowerCase())) {
      return `${label}：分批监控方向非法`
    }
    if (r.batch_count < 1) return `${label}：分批批数至少为 1`
    if (r.total_lot_limit < BATCH_POS_START) {
      return `${label}：总手数上限需 ≥ ${BATCH_POS_START}（档位从第 ${BATCH_POS_START} 笔起）`
    }
    if (!r.batch_levels.length) return `${label}：启用分批加仓后至少需要一个档位`
    const last = r.batch_levels[r.batch_levels.length - 1]
    if (r.batch_levels.length !== r.batch_count) {
      return `${label}：档位数与批数不一致，请调整总手数或批数后重试`
    }
    if (last.pos_to !== Math.floor(r.total_lot_limit)) {
      return `${label}：档位末笔需等于总手数上限`
    }
    for (const [i, lv] of r.batch_levels.entries()) {
      const at = `${label} 档位 ${i + 1}`
      if (!CALC_TYPE_OPTIONS.some((o) => o.value === lv.calc_type)) {
        return `${at}：计算方式非法`
      }
      if (lv.calc_type === 'point' && !(lv.point > 0)) return `${at}：点数需大于 0`
      if (lv.calc_type === 'price' && !(lv.price > 0)) return `${at}：请填写指定价位`
      if (isBarCalc(lv.calc_type) && !TIMEFRAME_OPTIONS.includes(lv.timeframe)) {
        return `${at}：请选择 K 线周期`
      }
      if (lv.point < 0) return `${at}：点数不能为负`
      if (lv.lot_times < 0) return `${at}：倍数不能为负`
      if (lv.extra_lot < 0) return `${at}：手数不能为负`
    }
  }
  return null
}

async function save(): Promise<void> {
  if (!form.template_id) {
    formError.value = '请选择策略模版'
    return
  }
  const name = form.name.trim()
  if (!name) {
    formError.value = '请填写策略名称'
    return
  }
  const symbol = form.symbol.trim().toUpperCase()
  if (!symbol) {
    formError.value = '请填写绑定品种'
    return
  }
  const rulesErr = validateRules(form.rules.map((r) => {
    if (r.batch_enabled) rebuildBatchLevels(r)
    return r
  }))
  if (rulesErr) {
    formError.value = rulesErr
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const tplName = formTemplateLabel.value
    const enabledCount = form.rules.filter((r) => r.status === 1).length
    const actionLabel = isEditMode.value ? '保存' : '创建'
    if (
      !(await confirmAction(
        `确认${actionLabel}策略「${name}」？\n\n模版：${tplName}\n绑定品种：${symbol}\n启用规则：${enabledCount} / ${form.rules.length}`,
      ))
    ) {
      return
    }
    try {
      const rules = cloneRules(form.rules)
      if (isEditMode.value) {
        await hub.updateStrategy(
          editingId.value,
          { name, symbol, rules },
          currentSearchOptions(),
        )
      } else {
        await hub.createStrategy(
          {
            template_id: form.template_id,
            name,
            symbol,
            rules,
          },
          currentSearchOptions(),
        )
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      formError.value = err?.response?.data?.detail || `${actionLabel}失败，请稍后重试`
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

async function toggleEnabled(s: StrategyOut): Promise<void> {
  const next = s.enabled ? '禁用' : '启用'
  if (!(await confirmAction(`确认${next}策略「${s.name}」？`))) return
  await hub.updateStrategy(s.strategy_id, { enabled: !s.enabled }, currentSearchOptions())
}

async function remove(s: StrategyOut): Promise<void> {
  if (!(await confirmAction(`确认删除策略「${s.name}」？\n\n该操作不可恢复。`, '确认删除'))) return
  await hub.deleteStrategy(s.strategy_id, currentSearchOptions())
}

function ruleSummary(rules: StrategyRule[]): string {
  if (!rules.length) return '—'
  const enabled = rules.filter((r) => r.status === 1)
  if (!enabled.length) return '全部关闭'
  return enabled
    .map((r) => {
      const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
      const levels = r.batch_levels?.length ?? 0
      return r.batch_enabled && levels ? `${label}（分批 ${levels} 档）` : label
    })
    .join(' · ')
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function resetRuleToTemplate(idx: number): void {
  const tplRule = selectedTemplate.value?.rules?.[idx]
  if (!tplRule || !form.rules[idx]) return
  Object.assign(form.rules[idx], cloneRules([tplRule])[0])
}
</script>

<template>
  <div class="strategies-page">
    <div class="row between page-header">
      <div>
        <div class="h1">策略管理</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          基于策略模版创建实例并绑定品种；选模版后可自定义逆势 / 顺势加仓参数
        </p>
      </div>
      <div class="row" style="gap: 8px">
        <button class="btn-ghost" @click="router.push('/groups')">← 返回分组</button>
        <button class="btn-primary" @click="openCreate">+ 新增策略</button>
      </div>
    </div>

    <div class="card card-pad" style="margin-bottom: 12px">
      <div class="row" style="gap: 8px">
        <input
          v-model="searchQuery"
          type="search"
          placeholder="搜索策略名称 / 品种…"
          aria-label="搜索策略"
          :disabled="loading"
          style="width: 25%"
          @keydown.enter="runSearch"
        />
        <button class="btn-primary btn-sm" :disabled="loading" @click="runSearch">
          {{ loading ? '搜索中…' : '搜索' }}
        </button>
      </div>
      <p v-if="appliedQuery" class="muted" style="font-size: 12px; margin: 8px 0 0">
        {{ hub.strategies.length ? `找到 ${hub.strategies.length} 个策略` : '无匹配策略' }}
      </p>
    </div>

    <!-- 移动端卡片 -->
    <div class="list-cards mobile-only">
      <div v-for="s in hub.strategies" :key="s.strategy_id" class="list-card card">
        <div class="list-card-head row between">
          <strong>{{ s.name }}</strong>
          <span class="tag" :class="s.enabled ? 'green' : ''">{{ s.enabled ? '已启用' : '已禁用' }}</span>
        </div>
        <div class="list-field"><span class="k">策略 ID</span><span class="v muted" style="font-size: 12px">{{ s.strategy_id }}</span></div>
        <div class="list-field"><span class="k">模版</span><span class="v">{{ s.template_name }}</span></div>
        <div class="list-field"><span class="k">绑定品种</span><span class="v"><code>{{ s.symbol }}</code></span></div>
        <div class="list-field"><span class="k">规则</span><span class="v">{{ ruleSummary(s.rules) }}</span></div>
        <div class="list-field"><span class="k">创建时间</span><span class="v muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</span></div>
        <div class="list-card-actions">
          <button class="btn-sm btn-ghost" @click="openEdit(s)">编辑</button>
          <button class="btn-sm" :class="s.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(s)">
            {{ s.enabled ? '禁用' : '启用' }}
          </button>
          <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
        </div>
      </div>
      <div v-if="!hub.strategies.length && !loading" class="card card-pad muted">
        {{ appliedQuery ? '无匹配策略' : '暂无策略' }}
      </div>
    </div>

    <!-- 桌面端表格 -->
    <div class="card table-scroll desktop-only">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>模版</th>
            <th>绑定品种</th>
            <th>规则</th>
            <th>创建时间</th>
            <th>启用</th>
            <th class="right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in hub.strategies" :key="s.strategy_id">
            <td>
              {{ s.name }}
              <div class="muted" style="font-size: 11px">{{ s.strategy_id }}</div>
            </td>
            <td><span class="tag blue">{{ s.template_name }}</span></td>
            <td><code>{{ s.symbol }}</code></td>
            <td class="muted" style="font-size: 12px">{{ ruleSummary(s.rules) }}</td>
            <td class="muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</td>
            <td>
              <button class="btn-sm" :class="s.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(s)">
                {{ s.enabled ? '已启用' : '已禁用' }}
              </button>
            </td>
            <td class="right">
              <div class="row" style="gap: 6px; justify-content: flex-end">
                <button class="btn-sm btn-ghost" @click="openEdit(s)">编辑</button>
                <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="!hub.strategies.length && !loading">
            <td colspan="7" class="muted" style="padding: 18px">
              {{ appliedQuery ? '无匹配策略' : '暂无策略，点击右上角「新增策略」开始配置' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 / 编辑策略弹窗 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="card modal modal-lg strategy-form-modal">
        <div class="modal-header card-pad" style="padding-bottom: 0">
          <div class="h1">{{ isEditMode ? '编辑策略' : '新增策略' }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            {{
              isEditMode
                ? '可修改名称、绑定品种与规则参数；模版创建后不可更换'
                : '选择策略模版 → 填写名称 / 品种 → 自定义规则参数后创建'
            }}
          </p>
        </div>

        <div class="modal-body card-pad" style="padding-top: 16px">
          <div class="form-grid">
            <div class="field">
              <FormLabel field-id="strategy-template" text="策略模版" :help="FIELD_HELP.template" />
              <select
                id="strategy-template"
                v-model="form.template_id"
                :disabled="isEditMode"
              >
                <option disabled value="">请选择模版</option>
                <option v-for="t in templates" :key="t.template_id" :value="t.template_id">
                  {{ t.name }}
                </option>
                <option
                  v-if="isEditMode && form.template_id && !selectedTemplate"
                  :value="form.template_id"
                >
                  {{ formTemplateLabel }}
                </option>
              </select>
              <p v-if="selectedTemplate" class="muted" style="font-size: 12px; margin-top: 6px">
                {{ selectedTemplate.description }}
              </p>
              <p v-else-if="isEditMode" class="muted" style="font-size: 12px; margin-top: 6px">
                当前模版：{{ formTemplateLabel }}
              </p>
            </div>

            <div class="field">
              <FormLabel field-id="strategy-name" text="策略名称" :help="FIELD_HELP.name" />
              <input id="strategy-name" v-model="form.name" type="text" maxlength="64" placeholder="例如：黄金逆势策略" />
            </div>

            <div class="field">
              <FormLabel field-id="strategy-symbol" text="绑定品种" :help="FIELD_HELP.symbol" />
              <input
                id="strategy-symbol"
                v-model="form.symbol"
                type="text"
                maxlength="32"
                placeholder="例如：XAUUSD"
                style="text-transform: uppercase"
              />
            </div>
          </div>

          <div v-if="form.rules.length" class="rules-editor">
            <div class="rules-editor-head">
              <FormLabel text="规则参数" :help="FIELD_HELP.rules" />
              <span class="muted" style="font-size: 12px">
                {{ isEditMode ? '可按需调整规则；「恢复默认」将回退到模版默认值' : '切换模版会重新载入默认值' }}
              </span>
            </div>

            <div v-for="(r, idx) in form.rules" :key="`${r.type}-${idx}`" class="rule-panel">
              <div class="rule-panel-head">
                <FormLabel
                  class="rule-title-label"
                  :text="RULE_TYPE_LABEL[r.type] || `规则 ${idx + 1}`"
                  :help="RULE_TYPE_HELP[r.type] || FIELD_HELP.rules"
                />
                <div class="rule-panel-actions">
                  <div class="rule-enable-wrap">
                    <FormLabel text="启用" :help="FIELD_HELP.status" />
                    <input
                      type="checkbox"
                      :checked="r.status === 1"
                      aria-label="启用本条规则"
                      @change="r.status = ($event.target as HTMLInputElement).checked ? 1 : 0"
                    />
                  </div>
                  <button type="button" class="btn-sm btn-ghost" @click="resetRuleToTemplate(idx)">恢复默认</button>
                </div>
              </div>

              <div class="rule-grid">
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-action`" text="监控方向" :help="FIELD_HELP.action" />
                  <select :id="`rule-${idx}-action`" v-model="r.action">
                    <option value="all">全部</option>
                    <option value="buy">多单</option>
                    <option value="sell">空单</option>
                  </select>
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-point`" text="点数" :help="FIELD_HELP.point" />
                  <input :id="`rule-${idx}-point`" v-model.number="r.point" type="number" min="0" step="1" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-lot-times`" text="倍数" :help="FIELD_HELP.lot_times" />
                  <input :id="`rule-${idx}-lot-times`" v-model.number="r.lot_times" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-extra-lot`" text="手数" :help="FIELD_HELP.extra_lot" />
                  <input :id="`rule-${idx}-extra-lot`" v-model.number="r.extra_lot" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-max-allow`" text="次数" :help="FIELD_HELP.max_allow_num" />
                  <input :id="`rule-${idx}-max-allow`" v-model.number="r.max_allow_num" type="number" min="0" step="1" />
                </div>
              </div>
              <p class="rule-hint">
                手数 = {{ r.lot_times }} × 基础手数 + {{ r.extra_lot }}；触发 = {{ r.point }} × Point()
              </p>

              <div class="batch-block">
                <div class="batch-head">
                  <div class="rule-enable-wrap">
                    <FormLabel text="分批加仓" :help="FIELD_HELP.batch_enabled" />
                    <input
                      type="checkbox"
                      :checked="r.batch_enabled"
                      :aria-label="`启用${RULE_TYPE_LABEL[r.type] || '本条规则'}分批加仓`"
                      @change="setBatchEnabled(r, ($event.target as HTMLInputElement).checked)"
                    />
                  </div>
                  <span v-if="r.batch_enabled" class="muted batch-count-hint">
                    共 {{ r.batch_count }} 批
                  </span>
                </div>

                <template v-if="r.batch_enabled">
                  <div class="batch-top-grid">
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-action`"
                        text="分批方向"
                        :help="FIELD_HELP.batch_action"
                      />
                      <select :id="`rule-${idx}-batch-action`" v-model="r.batch_action">
                        <option value="all">全部</option>
                        <option value="buy">多单</option>
                        <option value="sell">空单</option>
                      </select>
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-batch-count`"
                        text="批数"
                        :help="FIELD_HELP.batch_count"
                      />
                      <input
                        :id="`rule-${idx}-batch-count`"
                        v-model.number="r.batch_count"
                        type="number"
                        min="1"
                        step="1"
                        @change="onBatchMetaChange(r)"
                      />
                    </div>
                    <div class="field">
                      <FormLabel
                        :field-id="`rule-${idx}-total-lot`"
                        text="总手数"
                        :help="FIELD_HELP.total_lot_limit"
                      />
                      <input
                        :id="`rule-${idx}-total-lot`"
                        v-model.number="r.total_lot_limit"
                        type="number"
                        :min="BATCH_POS_START"
                        step="1"
                        @change="onBatchMetaChange(r)"
                      />
                    </div>
                  </div>

                  <div class="batch-levels">
                    <div v-for="(lv, li) in r.batch_levels" :key="li" class="batch-level">
                      <span class="batch-level-no">└{{ li + 1 }}</span>
                      <div class="field batch-range">
                        <FormLabel text="持仓笔数" :help="FIELD_HELP.batch_level" />
                        <div class="batch-range-value" title="由批数与总手数自动生成">
                          {{ lv.pos_from }} ~ {{ lv.pos_to }}
                        </div>
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-calc`"
                          text="计算方式"
                          :help="FIELD_HELP.calc_type"
                        />
                        <select :id="`rule-${idx}-lv-${li}-calc`" v-model="lv.calc_type">
                          <option v-for="o in CALC_TYPE_OPTIONS" :key="o.value" :value="o.value">
                            {{ o.label }}
                          </option>
                        </select>
                      </div>
                      <div v-if="lv.calc_type === 'price'" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-price`"
                          text="指定价"
                          :help="FIELD_HELP.batch_price"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-price`"
                          v-model.number="lv.price"
                          type="number"
                          min="0"
                          step="0.00001"
                        />
                      </div>
                      <div v-else-if="isBarCalc(lv.calc_type)" class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-tf`"
                          text="K 线周期"
                          :help="FIELD_HELP.batch_timeframe"
                        />
                        <select :id="`rule-${idx}-lv-${li}-tf`" v-model="lv.timeframe">
                          <option v-for="tf in TIMEFRAME_OPTIONS" :key="tf" :value="tf">
                            {{ tf }}
                          </option>
                        </select>
                      </div>
                      <div v-else class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-point`"
                          text="点数"
                          :help="FIELD_HELP.point"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-point`"
                          v-model.number="lv.point"
                          type="number"
                          min="0"
                          step="1"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-times`"
                          text="倍数"
                          :help="FIELD_HELP.lot_times"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-times`"
                          v-model.number="lv.lot_times"
                          type="number"
                          min="0"
                          step="0.01"
                        />
                      </div>
                      <div class="field">
                        <FormLabel
                          :field-id="`rule-${idx}-lv-${li}-extra`"
                          text="手数"
                          :help="FIELD_HELP.extra_lot"
                        />
                        <input
                          :id="`rule-${idx}-lv-${li}-extra`"
                          v-model.number="lv.extra_lot"
                          type="number"
                          min="0"
                          step="0.01"
                        />
                      </div>
                    </div>
                  </div>
                  <p class="rule-hint">
                    档位区间由批数 × 总手数自动切分（第 {{ BATCH_POS_START }} 笔 ~ 第 {{ Math.floor(r.total_lot_limit) }} 笔），不可手动添加；
                    每档的加仓间距可独立选择点数 / 指定价 / ATR / 波幅，ATR 与波幅取该周期最近 {{ BATCH_BAR_PERIOD }} 根已收盘 K 线
                  </p>
                </template>
              </div>
            </div>
          </div>

          <p v-if="formError" class="form-error" style="margin-top: 10px">{{ formError }}</p>
        </div>

        <div class="modal-footer card-pad" style="padding-top: 0">
          <span></span>
          <div class="row" style="gap: 8px">
            <button class="btn-ghost" :disabled="saving" @click="showForm = false">取消</button>
            <button class="btn-primary" :disabled="saving" @click="save">
              {{ saving ? '保存中…' : isEditMode ? '保存' : '创建' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  margin-bottom: 14px;
  align-items: flex-start;
}

.rules-editor {
  margin-top: 18px;
}

.rules-editor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.rule-panel {
  margin-bottom: 12px;
  padding: 14px 16px;
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  border: 1px solid var(--glass-border);
}

.rule-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.rule-title {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.rule-title-label :deep(.form-label-row) {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.rule-panel-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.rule-enable-wrap {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.rule-enable-wrap :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.rule-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
}

.rule-grid .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.rule-grid :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.rule-grid :deep(.form-label-row) {
  white-space: nowrap;
}

.rule-grid :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.rule-grid input,
.rule-grid select {
  width: 100%;
  min-width: 0;
}

.rule-hint {
  margin: 10px 0 0;
  font-size: 11px;
  color: var(--muted);
}

.batch-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--glass-border);
}

.batch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.batch-count-hint {
  font-size: 12px;
}

.batch-top-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 12px;
  align-items: start;
  margin-top: 12px;
  max-width: 640px;
}

.batch-levels {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.batch-level {
  display: grid;
  grid-template-columns: 28px minmax(88px, 0.9fr) repeat(4, minmax(0, 1fr));
  gap: 10px;
  align-items: end;
}

.batch-level-no {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  padding-bottom: 8px;
}

.batch-range-value {
  display: flex;
  align-items: center;
  min-height: 34px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--glass-border);
  background: color-mix(in srgb, var(--bg-soft) 70%, transparent);
  font-family: var(--mono);
  font-size: 13px;
  color: var(--muted);
  user-select: none;
}

.batch-top-grid .field,
.batch-level .field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.batch-top-grid :deep(.form-label-wrap),
.batch-level :deep(.form-label-wrap) {
  margin-bottom: 0;
}

.batch-top-grid :deep(.form-label-row),
.batch-level :deep(.form-label-row) {
  white-space: nowrap;
}

.batch-top-grid :deep(.field-help-popover),
.batch-level :deep(.field-help-popover) {
  position: absolute;
  z-index: 5;
  left: 0;
  right: auto;
  min-width: 220px;
  max-width: min(320px, 70vw);
}

.batch-top-grid input,
.batch-top-grid select,
.batch-level input,
.batch-level select {
  width: 100%;
  min-width: 0;
}

@media (max-width: 900px) {
  .rule-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .batch-top-grid {
    grid-template-columns: 1fr 1fr;
    max-width: none;
  }
  .batch-level {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .batch-level-no {
    grid-column: 1 / -1;
    padding-bottom: 0;
  }
}

@media (max-width: 768px) {
  .strategy-form-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .rule-grid {
    grid-template-columns: 1fr 1fr;
  }
  .rules-editor-head {
    flex-wrap: wrap;
  }
  .batch-top-grid {
    grid-template-columns: 1fr;
    max-width: none;
  }
  .batch-level {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
