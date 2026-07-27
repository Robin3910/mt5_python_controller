<script setup lang="ts">
// 策略管理页：基于模版新建策略、绑定品种，选模版后可自定义规则参数
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type { StrategyOut, StrategyRule, StrategyTemplateOut } from '@/api/types'
import { confirmAction } from '@/utils/confirm'

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
    '逆势加仓：以监控方向最近一笔订单为基准，价格朝不利方向偏离达到 point × Point() 后触发加仓。' +
    '实际手数 = lot_times × 基础订单手数 + extra_lot。',
  2:
    '顺势加仓：以监控方向最近一笔订单为基准，价格朝有利方向偏离达到 point × Point() 后触发加仓。' +
    '实际手数 = lot_times × 基础订单手数 + extra_lot。',
}

const FIELD_HELP = {
  template: '选择策略模版后，会载入模版默认规则；可在下方自定义各参数后再创建。',
  name: '策略名称，全局唯一。便于在列表与审计中辨识。',
  symbol: '绑定品种代码，如 XAUUSD。策略规则仅作用于该品种。',
  rules:
    '每条规则独立配置。关闭「启用」后该规则不会执行。' +
    '实际加仓手数 = lot_times × 基础订单手数 + extra_lot；触发距离 = point × Point()。',
  status:
    '0=关闭，1=启用。关闭后本条规则不会参与监控与加仓；已产生的历史订单不受影响。',
  action:
    '监控方向：all=多空都监控，buy=只监控多单，sell=只监控空单。' +
    '系统会按该方向取最近一笔订单作为加仓基准。',
  point:
    '点差倍数。实时以监控方向最近订单为基准，价格偏离达到 point × Point() 后开始执行加仓。' +
    'Point() 为品种最小价格变动单位。',
  lot_times:
    '加仓倍率，默认 1。以监控方向最近订单的手数为基准，参与计算：lot_times × 基础手数。',
  extra_lot:
    '额外手数，默认 0。加在倍率结果之后：实际手数 = lot_times × 基础手数 + extra_lot。',
  max_allow_num:
    '最大允许加仓次数。达到次数上限后，本条规则不再继续加仓。',
}

function cloneRules(rules: StrategyRule[]): StrategyRule[] {
  return rules.map((r) => ({
    type: r.type,
    status: r.status,
    action: r.action,
    point: r.point,
    lot_times: r.lot_times,
    extra_lot: r.extra_lot,
    max_allow_num: r.max_allow_num,
  }))
}

// ---- 新建 ----
const showForm = ref(false)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  template_id: '',
  name: '',
  symbol: '',
  rules: [] as StrategyRule[],
})

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.template_id === form.template_id) || null,
)

function loadRulesFromTemplate(templateId: string): void {
  const tpl = templates.value.find((t) => t.template_id === templateId)
  form.rules = tpl ? cloneRules(tpl.rules) : []
}

watch(
  () => form.template_id,
  (id) => {
    if (showForm.value && id) loadRulesFromTemplate(id)
  },
)

function openCreate(): void {
  formError.value = ''
  form.template_id = templates.value[0]?.template_id || ''
  form.name = ''
  form.symbol = ''
  loadRulesFromTemplate(form.template_id)
  showForm.value = true
}

function validateRules(rules: StrategyRule[]): string | null {
  if (!rules.length) return '请至少配置一条规则'
  for (const r of rules) {
    const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
    if (r.point < 0) return `${label}：point 不能为负`
    if (r.lot_times < 0) return `${label}：加仓倍率不能为负`
    if (r.extra_lot < 0) return `${label}：额外手数不能为负`
    if (r.max_allow_num < 0) return `${label}：最大加仓次数不能为负`
    if (!['all', 'buy', 'sell'].includes(String(r.action || '').toLowerCase())) {
      return `${label}：监控方向非法`
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
  const rulesErr = validateRules(form.rules)
  if (rulesErr) {
    formError.value = rulesErr
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const tplName = selectedTemplate.value?.name || form.template_id
    const enabledCount = form.rules.filter((r) => r.status === 1).length
    if (
      !(await confirmAction(
        `确认创建策略「${name}」？\n\n模版：${tplName}\n绑定品种：${symbol}\n启用规则：${enabledCount} / ${form.rules.length}`,
      ))
    ) {
      return
    }
    try {
      await hub.createStrategy(
        {
          template_id: form.template_id,
          name,
          symbol,
          rules: cloneRules(form.rules),
        },
        currentSearchOptions(),
      )
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      formError.value = err?.response?.data?.detail || '创建失败，请稍后重试'
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
  return enabled.map((r) => RULE_TYPE_LABEL[r.type] || `类型${r.type}`).join(' · ')
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
              <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
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

    <!-- 新增策略弹窗 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="card modal modal-lg strategy-form-modal">
        <div class="modal-header card-pad" style="padding-bottom: 0">
          <div class="h1">新增策略</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            选择策略模版 → 填写名称 / 品种 → 自定义规则参数后创建
          </p>
        </div>

        <div class="modal-body card-pad" style="padding-top: 16px">
          <div class="form-grid">
            <div class="field">
              <FormLabel field-id="strategy-template" text="策略模版" :help="FIELD_HELP.template" />
              <select id="strategy-template" v-model="form.template_id">
                <option disabled value="">请选择模版</option>
                <option v-for="t in templates" :key="t.template_id" :value="t.template_id">
                  {{ t.name }}
                </option>
              </select>
              <p v-if="selectedTemplate" class="muted" style="font-size: 12px; margin-top: 6px">
                {{ selectedTemplate.description }}
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
              <span class="muted" style="font-size: 12px">切换模版会重新载入默认值</span>
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
                    <option value="all">all（全部）</option>
                    <option value="buy">buy（只监控多单）</option>
                    <option value="sell">sell（只监控空单）</option>
                  </select>
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-point`" text="point" :help="FIELD_HELP.point" />
                  <input :id="`rule-${idx}-point`" v-model.number="r.point" type="number" min="0" step="1" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-lot-times`" text="lot_times" :help="FIELD_HELP.lot_times" />
                  <input :id="`rule-${idx}-lot-times`" v-model.number="r.lot_times" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-extra-lot`" text="extra_lot" :help="FIELD_HELP.extra_lot" />
                  <input :id="`rule-${idx}-extra-lot`" v-model.number="r.extra_lot" type="number" min="0" step="0.01" />
                </div>
                <div class="field">
                  <FormLabel :field-id="`rule-${idx}-max-allow`" text="max_allow_num" :help="FIELD_HELP.max_allow_num" />
                  <input :id="`rule-${idx}-max-allow`" v-model.number="r.max_allow_num" type="number" min="0" step="1" />
                </div>
              </div>
              <p class="rule-hint">
                手数 = {{ r.lot_times }} × 基础手数 + {{ r.extra_lot }}；触发 = {{ r.point }} × Point()
              </p>
            </div>
          </div>

          <p v-if="formError" class="form-error" style="margin-top: 10px">{{ formError }}</p>
        </div>

        <div class="modal-footer card-pad" style="padding-top: 0">
          <span></span>
          <div class="row" style="gap: 8px">
            <button class="btn-ghost" :disabled="saving" @click="showForm = false">取消</button>
            <button class="btn-primary" :disabled="saving" @click="save">
              {{ saving ? '保存中…' : '创建' }}
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
  font-family: var(--mono);
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

@media (max-width: 900px) {
  .rule-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
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
}
</style>
