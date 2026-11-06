<script setup lang="ts">
// 中控台 / 节点详情共用：按品种配置分发策略、价格区间、允许方向与手数。
// 样式 1:1 复刻 UI/H5.html 原型（品种卡片 + 区间卡片 + 分段选择 + 开关 + 步进器）。
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import type {
  FilterDirection,
  FilterRulesConfig,
  NodeDispatchFiltersConfig,
  NodeSymbolDispatchRule,
  SymbolFilterRule,
} from '@/api/types'
import {
  createEmptyInterval,
  createEmptyNodeSymbolRule,
  createEmptySymbolRule,
  exampleFilterRules,
  nodesFollowingGlobalLot,
  parseFilterRules,
} from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'
import { useHubStore } from '@/stores/hub'

const props = withDefaults(defineProps<{ mode?: 'global' | 'node' }>(), { mode: 'global' })

const emit = defineEmits<{
  trigger: [payload: { symbol: string; action: 'BUY' | 'SELL'; volume: number }]
}>()

const model = defineModel<FilterRulesConfig | NodeDispatchFiltersConfig>({ required: true })
const hub = useHubStore()

const isGlobal = computed(() => props.mode === 'global')

/** 分发模式：sync = 全员同步，poll = 轮询轮转（单节点领取） */
const DISPATCH_MODE_OPTIONS: { key: 'sync' | 'poll'; label: string }[] = [
  { key: 'sync', label: '全员同步' },
  { key: 'poll', label: '轮询轮转' },
]
const POSITION_SCOPE_OPTIONS: { key: 'symbol' | 'account'; label: string }[] = [
  { key: 'symbol', label: '按品种' },
  { key: 'account', label: '按账户' },
]
const DEFAULT_ACTION_OPTIONS: { key: 'block' | 'pass'; label: string }[] = [
  { key: 'block', label: '拦截' },
  { key: 'pass', label: '放行' },
]
const LOT_MODE_OPTIONS: { key: 'global' | 'fixed' | 'signal'; label: string }[] = [
  { key: 'global', label: '跟随中控台' },
  { key: 'fixed', label: '固定手数' },
  { key: 'signal', label: '跟随信号' },
]

const addingSymbol = ref(false)
const newSymbol = ref('')
const filterKeyword = ref('')

const globalSymbolListAll = computed(() => {
  const cfg = model.value as FilterRulesConfig
  return Object.entries(cfg)
    .map(([symbol, rule]) => ({ symbol, rule }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol))
})

const nodeSymbolListAll = computed(() => {
  const cfg = model.value as NodeDispatchFiltersConfig
  return Object.entries(cfg)
    .map(([symbol, rule]) => ({ symbol, rule }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol))
})

const totalSymbolCount = computed(() =>
  isGlobal.value ? globalSymbolListAll.value.length : nodeSymbolListAll.value.length,
)

const globalSymbolList = computed(() => {
  const kw = filterKeyword.value.trim().toUpperCase()
  if (!kw) return globalSymbolListAll.value
  return globalSymbolListAll.value.filter((it) => it.symbol.toUpperCase().includes(kw))
})

const nodeSymbolList = computed(() => {
  const kw = filterKeyword.value.trim().toUpperCase()
  if (!kw) return nodeSymbolListAll.value
  return nodeSymbolListAll.value.filter((it) => it.symbol.toUpperCase().includes(kw))
})

const visibleSymbolCount = computed(() =>
  isGlobal.value ? globalSymbolList.value.length : nodeSymbolList.value.length,
)

const hasKeyword = computed(() => filterKeyword.value.trim().length > 0)

function clearFilterKeyword(): void {
  filterKeyword.value = ''
}

function updateRule(symbol: string, patch: Partial<SymbolFilterRule>): void {
  const cfg = model.value as FilterRulesConfig
  const cur = cfg[symbol]
  if (!cur) return
  model.value = { ...cfg, [symbol]: { ...cur, ...patch } }
}

function updateNodeRule(symbol: string, patch: Partial<NodeSymbolDispatchRule>): void {
  const cfg = model.value as NodeDispatchFiltersConfig
  const cur = cfg[symbol]
  if (!cur) return
  model.value = { ...cfg, [symbol]: { ...cur, ...patch } }
}

async function removeSymbol(symbol: string): Promise<void> {
  const label = isGlobal.value ? '过滤规则' : '分发配置'
  if (!(await confirmAction(`确认删除品种 ${symbol} 的${label}？`, '确认删除'))) return
  const next = { ...model.value }
  delete next[symbol]
  model.value = next
}

async function confirmAddSymbol(): Promise<void> {
  const key = newSymbol.value.trim().toUpperCase()
  if (!key) return
  if (model.value[key]) {
    ElMessageBox.alert(`品种 ${key} 已存在`, '无法添加', { type: 'warning', confirmButtonText: '知道了' })
    return
  }
  const label = isGlobal.value ? '过滤规则' : '分发配置'
  if (!(await confirmAction(`确认添加品种 ${key} 的${label}？`, '确认添加'))) return
  if (isGlobal.value) {
    model.value = { ...(model.value as FilterRulesConfig), [key]: createEmptySymbolRule() }
  } else {
    model.value = { ...(model.value as NodeDispatchFiltersConfig), [key]: createEmptyNodeSymbolRule() }
  }
  newSymbol.value = ''
  addingSymbol.value = false
}

function cancelAddSymbol(): void {
  newSymbol.value = ''
  addingSymbol.value = false
}

function addInterval(symbol: string): void {
  const rule = (model.value as FilterRulesConfig)[symbol]
  if (!rule) return
  updateRule(symbol, { intervals: [...rule.intervals, createEmptyInterval()] })
}

function updateInterval(
  symbol: string,
  index: number,
  patch: Partial<{ low: number; high: number; allow: FilterDirection[] }>,
): void {
  const rule = (model.value as FilterRulesConfig)[symbol]
  if (!rule) return
  const intervals = rule.intervals.map((iv, i) => (i === index ? { ...iv, ...patch } : iv))
  updateRule(symbol, { intervals })
}

function toggleDirection(symbol: string, index: number, dir: FilterDirection, checked: boolean): void {
  const rule = (model.value as FilterRulesConfig)[symbol]
  if (!rule) return
  const iv = rule.intervals[index]
  if (!iv) return
  const allow = new Set(iv.allow)
  if (checked) allow.add(dir)
  else allow.delete(dir)
  updateInterval(symbol, index, { allow: [...allow] })
}

async function removeInterval(symbol: string, index: number): Promise<void> {
  const rule = (model.value as FilterRulesConfig)[symbol]
  if (!rule) return
  if (!(await confirmAction(`确认删除品种 ${symbol} 的第 ${index + 1} 条价格区间？`, '确认删除'))) return
  updateRule(symbol, { intervals: rule.intervals.filter((_, i) => i !== index) })
}

async function loadExample(): Promise<void> {
  if (!isGlobal.value) return
  if (
    Object.keys(model.value).length &&
    !(await confirmAction('载入示例将覆盖当前配置，是否继续？', '确认载入示例'))
  )
    return
  model.value = exampleFilterRules()
}

function setAllowBuy(symbol: string, value: boolean): void {
  updateRule(symbol, { allow_buy: value })
}

function setAllowSell(symbol: string, value: boolean): void {
  updateRule(symbol, { allow_sell: value })
}

/** 关闭「启用全局手数」前检查是否有节点仍跟随中控台。 */
async function setLotEnabled(symbol: string, enabled: boolean): Promise<void> {
  if (!enabled) {
    if (!hub.nodes.length) await hub.fetchNodes()
    const dependents = nodesFollowingGlobalLot(hub.nodes, symbol)
    if (dependents.length) {
      const shown = dependents.slice(0, 5).join('、')
      const suffix =
        dependents.length > 5 ? `（${shown} 等共 ${dependents.length} 个节点）` : `（${shown}）`
      await ElMessageBox.alert(
        `${symbol}：以下节点手数策略为「跟随中控台」，无法关闭全局手数${suffix}\n\n请先将这些节点改为「固定手数」或「跟随信号」后再关闭。`,
        '无法关闭全局手数',
        { type: 'warning', confirmButtonText: '知道了' },
      )
      return
    }
  }
  updateRule(symbol, { lot_enabled: enabled })
}

function setFollowSync(symbol: string, value: boolean): void {
  updateNodeRule(symbol, { follow_sync: value })
}

function setFollowPoll(symbol: string, value: boolean): void {
  updateNodeRule(symbol, { follow_poll: value })
}

/** 节点手数策略改为「跟随中控台」前检查中控台是否已启用全局手数。 */
async function setLotMode(symbol: string, mode: 'global' | 'fixed' | 'signal'): Promise<void> {
  if (mode === 'global') {
    if (!Object.keys(hub.filters).length) await hub.fetchConfig()
    const globalRules = parseFilterRules(hub.filters)
    const gf =
      globalRules[symbol] ||
      globalRules[symbol.replace(/[^A-Z0-9]/g, '')] ||
      Object.entries(globalRules).find(([k]) => {
        const kb = k.replace(/[^A-Z0-9]/g, '')
        const base = symbol.replace(/[^A-Z0-9]/g, '')
        return kb && base && (kb === base || kb.startsWith(base) || base.startsWith(kb))
      })?.[1]
    if (!gf?.lot_enabled) {
      await ElMessageBox.alert(
        `${symbol}：中控台该品种未启用全局手数，无法将手数策略设为「跟随中控台」。\n\n请先到中控台开启该品种的「启用全局手数」。`,
        '无法设置跟随中控台',
        { type: 'warning', confirmButtonText: '知道了' },
      )
      return
    }
  }
  updateNodeRule(symbol, { lot_mode: mode })
}

/** 全局手数步进：每次 0.01，范围 [0.01, 1] */
function stepLot(symbol: string, delta: number): void {
  const rule = (model.value as FilterRulesConfig)[symbol]
  if (!rule) return
  const next = Math.min(1, Math.max(0.01, Math.round((Number(rule.lot) + delta) * 100) / 100))
  updateRule(symbol, { lot: next })
}

// 手动触发信号：弹窗确认并可自定义手数。
// 已启用全局手数时默认填入该币种全局手数；未启用时默认填入配置中的 lot 字段。
// 确认后 emit('trigger')，由父组件（中控台）调用后台手动触发接口。
async function manualTrigger(symbol: string, action: 'BUY' | 'SELL'): Promise<void> {
  const cfg = model.value as FilterRulesConfig
  const rule = cfg[symbol]
  if (!rule) return
  const defaultLot = Number(rule.lot) > 0 ? Number(rule.lot) : 0.01
  const hint = rule.lot_enabled
    ? `确认手动触发 ${action} ${symbol}？\n\n默认手数为当前币种全局手数，可修改后触发。`
    : `品种 ${symbol} 未启用全局手数，请输入本次手动触发 ${action} 的手数：`
  let volume: number
  try {
    const { value } = await ElMessageBox.prompt(hint, '确认手动触发', {
      confirmButtonText: '确认触发',
      cancelButtonText: '取消',
      inputValue: String(defaultLot),
      inputPattern: /^\d*\.?\d+$/,
      inputErrorMessage: '请输入大于 0 的手数',
      closeOnClickModal: false,
    })
    volume = Number(value)
  } catch {
    return // 用户取消
  }
  if (!(volume > 0)) {
    ElMessageBox.alert('手数必须大于 0', '无法触发', { type: 'warning', confirmButtonText: '知道了' })
    return
  }
  emit('trigger', { symbol, action, volume })
}

defineExpose({ loadExample })
</script>

<template>
  <div class="filter-editor">
    <!-- 工具栏：搜索 / 载入示例 / 添加品种 -->
    <div class="filter-toolbar">
      <div class="filter-symbol-search">
        <svg class="filter-symbol-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" stroke-linecap="round" />
        </svg>
        <input
          v-model="filterKeyword"
          class="filter-symbol-search-input"
          type="search"
          :placeholder="`筛选品种（共 ${totalSymbolCount}）`"
          @keyup.esc="clearFilterKeyword"
        />
        <button
          v-if="hasKeyword"
          type="button"
          class="filter-symbol-search-clear"
          aria-label="清除筛选"
          @click="clearFilterKeyword"
        >
          ×
        </button>
        <span v-if="hasKeyword" class="filter-symbol-search-count">
          {{ visibleSymbolCount }} / {{ totalSymbolCount }}
        </span>
      </div>
      <div class="filter-toolbar-actions">
        <button v-if="isGlobal" type="button" class="pf-btn" @click="loadExample">载入示例</button>
        <button type="button" class="pf-btn pf-btn-primary" @click="addingSymbol = true">添加品种</button>
      </div>
    </div>

    <!-- 添加品种 -->
    <div v-if="addingSymbol" class="filter-add-row">
      <div class="filter-add-input">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M3 3v18h18" />
          <path d="m19 9-5 5-4-4-3 3" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <input
          v-model="newSymbol"
          type="text"
          placeholder="输入品种代码，如 XAUUSD"
          @keyup.enter="confirmAddSymbol"
        />
      </div>
      <button type="button" class="pf-btn pf-btn-primary" @click="confirmAddSymbol">添加品种</button>
      <button type="button" class="pf-btn" @click="cancelAddSymbol">取消</button>
    </div>

    <!-- ── 全局模式：中控台 ── -->
    <template v-if="isGlobal">
      <div v-if="!totalSymbolCount" class="pf-empty">
        尚未配置任何品种。点击「添加品种」开始，或「载入示例」查看 XAUUSD 示范。
      </div>
      <div v-else-if="!globalSymbolList.length" class="pf-empty">
        无匹配「{{ filterKeyword }}」的品种。
        <button type="button" class="pf-btn pf-btn-mini" @click="clearFilterKeyword">清除筛选</button>
      </div>

      <article v-for="{ symbol, rule } in globalSymbolList" :key="symbol" class="filter-symbol">
        <!-- 品种头 -->
        <header class="filter-symbol-head">
          <div class="filter-symbol-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="m12 3 2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 17.8 6.2 20.9l1.1-6.5L2.6 9.8l6.5-.9Z" stroke-linejoin="round" />
            </svg>
            <span class="filter-symbol-name">{{ symbol }}</span>
            <span v-if="!rule.enabled" class="pf-pill grey">已停用</span>
          </div>
          <div class="filter-symbol-actions">
            <button type="button" class="pf-btn pf-btn-mini" @click="removeSymbol(symbol)">删除</button>
          </div>
        </header>

        <!-- 方向规则 -->
        <div class="filter-section">
          <p class="pf-section-title"><span class="pf-bar"></span>方向规则</p>
          <div class="filter-switch-grid">
            <div class="pf-switch-row">
              <span class="pf-switch-label">启用该品种</span>
              <button
                type="button"
                role="switch"
                :aria-checked="rule.enabled"
                class="pf-switch"
                :class="{ on: rule.enabled }"
                @click="updateRule(symbol, { enabled: !rule.enabled })"
              ></button>
            </div>
            <div class="pf-switch-row">
              <span class="pf-switch-label">允许做多 (BUY)</span>
              <button
                type="button"
                role="switch"
                :aria-checked="rule.allow_buy"
                class="pf-switch"
                :class="{ on: rule.allow_buy }"
                @click="setAllowBuy(symbol, !rule.allow_buy)"
              ></button>
            </div>
            <div class="pf-switch-row">
              <span class="pf-switch-label">允许做空 (SELL)</span>
              <button
                type="button"
                role="switch"
                :aria-checked="rule.allow_sell"
                class="pf-switch"
                :class="{ on: rule.allow_sell }"
                @click="setAllowSell(symbol, !rule.allow_sell)"
              ></button>
            </div>
          </div>
        </div>

        <!-- 分发设置 -->
        <div class="filter-section">
          <p class="pf-section-title"><span class="pf-bar"></span>分发设置</p>
          <div class="filter-field">
            <span class="pf-label">分发模式</span>
            <div class="pf-seg">
              <button
                v-for="opt in DISPATCH_MODE_OPTIONS"
                :key="opt.key"
                type="button"
                class="pf-seg-item"
                :class="{ active: rule.dispatch_mode === opt.key }"
                @click="updateRule(symbol, { dispatch_mode: opt.key })"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
          <div class="filter-field">
            <span class="pf-label">持仓判定范围</span>
            <div class="pf-seg">
              <button
                v-for="opt in POSITION_SCOPE_OPTIONS"
                :key="opt.key"
                type="button"
                class="pf-seg-item"
                :class="{ active: rule.position_scope === opt.key }"
                @click="updateRule(symbol, { position_scope: opt.key })"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
          <div class="filter-field">
            <span class="pf-label">默认动作（价格不在任何区间内）</span>
            <div class="pf-seg">
              <button
                v-for="opt in DEFAULT_ACTION_OPTIONS"
                :key="opt.key"
                type="button"
                class="pf-seg-item"
                :class="{ active: rule.default_action === opt.key, danger: opt.key === 'block', success: opt.key === 'pass' }"
                @click="updateRule(symbol, { default_action: opt.key })"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
        </div>

        <!-- 价格区间 -->
        <div class="filter-section">
          <div class="pf-section-head">
            <p class="pf-section-title"><span class="pf-bar"></span>价格区间</p>
            <button type="button" class="pf-btn pf-btn-mini" @click="addInterval(symbol)">添加区间</button>
          </div>

          <p v-if="!rule.intervals.length" class="pf-hint filter-empty-line">
            暂无区间。添加后，仅当价格落在区间内才会按允许方向过滤。
          </p>

          <div v-for="(iv, idx) in rule.intervals" :key="idx" class="filter-range">
            <div class="filter-range-head">
              <p class="filter-range-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="m7 8 4 4-4 4M13 16h4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                区间 {{ idx + 1 }}
                <span class="filter-range-desc">· 价格落入区间时按方向规则执行</span>
              </p>
              <button type="button" class="filter-range-del" @click="removeInterval(symbol, idx)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M18 6 6 18M6 6l12 12" stroke-linecap="round" />
                </svg>
                移除区间
              </button>
            </div>

            <div class="filter-range-grid">
              <label class="filter-range-field">
                <span class="pf-label">价格下限</span>
                <input
                  :value="iv.low"
                  type="number"
                  step="any"
                  class="pf-input filter-num-input"
                  @input="updateInterval(symbol, idx, { low: Number(($event.target as HTMLInputElement).value) })"
                />
              </label>
              <label class="filter-range-field">
                <span class="pf-label">价格上限</span>
                <input
                  :value="iv.high"
                  type="number"
                  step="any"
                  class="pf-input filter-num-input"
                  @input="updateInterval(symbol, idx, { high: Number(($event.target as HTMLInputElement).value) })"
                />
              </label>
            </div>

            <div class="filter-range-switches">
              <div class="pf-switch-row">
                <span class="pf-switch-label">做多 (BUY)</span>
                <button
                  type="button"
                  role="switch"
                  :aria-checked="iv.allow.includes('BUY')"
                  class="pf-switch"
                  :class="{ on: iv.allow.includes('BUY') }"
                  @click="toggleDirection(symbol, idx, 'BUY', !iv.allow.includes('BUY'))"
                ></button>
              </div>
              <div class="pf-switch-row">
                <span class="pf-switch-label">做空 (SELL)</span>
                <button
                  type="button"
                  role="switch"
                  :aria-checked="iv.allow.includes('SELL')"
                  class="pf-switch"
                  :class="{ on: iv.allow.includes('SELL') }"
                  @click="toggleDirection(symbol, idx, 'SELL', !iv.allow.includes('SELL'))"
                ></button>
              </div>
            </div>
          </div>
        </div>

        <!-- 全局手数 -->
        <div class="filter-section">
          <p class="pf-section-title"><span class="pf-bar"></span>全局手数</p>
          <div class="pf-switch-row filter-lot-switch">
            <span class="pf-switch-label">启用全局手数</span>
            <button
              type="button"
              role="switch"
              :aria-checked="rule.lot_enabled"
              class="pf-switch"
              :class="{ on: rule.lot_enabled }"
              @click="setLotEnabled(symbol, !rule.lot_enabled)"
            ></button>
          </div>
          <div class="filter-field">
            <span class="pf-label">默认开仓手数</span>
            <div class="pf-stepper">
              <button type="button" class="pf-stepper-btn" :disabled="!rule.lot_enabled" @click="stepLot(symbol, -0.01)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M5 12h14" stroke-linecap="round" />
                </svg>
              </button>
              <input
                :value="rule.lot"
                type="number"
                step="0.01"
                min="0.01"
                max="1"
                class="pf-stepper-input"
                :disabled="!rule.lot_enabled"
                @input="updateRule(symbol, { lot: Number(($event.target as HTMLInputElement).value) })"
              />
              <button type="button" class="pf-stepper-btn" :disabled="!rule.lot_enabled" @click="stepLot(symbol, 0.01)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M12 5v14M5 12h14" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </div>
          <p class="pf-hint">节点手数策略为「跟随中控台」时使用此处配置的手数。</p>
        </div>

        <!-- 手动触发 -->
        <div class="filter-section filter-section-last">
          <p class="pf-section-title"><span class="pf-bar"></span>手动触发</p>
          <div class="filter-manual">
            <button type="button" class="pf-btn pf-btn-buy" @click="manualTrigger(symbol, 'BUY')">BUY</button>
            <button type="button" class="pf-btn pf-btn-sell" @click="manualTrigger(symbol, 'SELL')">SELL</button>
          </div>
        </div>
      </article>
    </template>

    <!-- ── 节点模式：节点详情「按币种配置」── -->
    <template v-else>
      <div v-if="!totalSymbolCount" class="pf-empty">
        尚未配置任何品种。点击「添加品种」为该节点设置各币种的分发、手数与轮询顺序。
      </div>
      <div v-else-if="!nodeSymbolList.length" class="pf-empty">
        无匹配「{{ filterKeyword }}」的品种。
        <button type="button" class="pf-btn pf-btn-mini" @click="clearFilterKeyword">清除筛选</button>
      </div>

      <article v-for="{ symbol, rule } in nodeSymbolList" :key="symbol" class="filter-symbol">
        <header class="filter-symbol-head">
          <div class="filter-symbol-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="m12 3 2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 17.8 6.2 20.9l1.1-6.5L2.6 9.8l6.5-.9Z" stroke-linejoin="round" />
            </svg>
            <span class="filter-symbol-name">{{ symbol }}</span>
          </div>
          <div class="filter-symbol-actions">
            <button type="button" class="pf-btn pf-btn-mini" @click="removeSymbol(symbol)">删除</button>
          </div>
        </header>

        <div class="filter-section">
          <p class="pf-section-title"><span class="pf-bar"></span>分发参与</p>
          <div class="filter-switch-grid">
            <div class="pf-switch-row">
              <span class="pf-switch-label">参与同步</span>
              <button
                type="button"
                role="switch"
                :aria-checked="rule.follow_sync"
                class="pf-switch"
                :class="{ on: rule.follow_sync }"
                @click="setFollowSync(symbol, !rule.follow_sync)"
              ></button>
            </div>
            <div class="pf-switch-row">
              <span class="pf-switch-label">参与轮询</span>
              <button
                type="button"
                role="switch"
                :aria-checked="rule.follow_poll"
                class="pf-switch"
                :class="{ on: rule.follow_poll }"
                @click="setFollowPoll(symbol, !rule.follow_poll)"
              ></button>
            </div>
          </div>
        </div>

        <div class="filter-section filter-section-last">
          <p class="pf-section-title"><span class="pf-bar"></span>手数与顺序</p>
          <div class="filter-field">
            <span class="pf-label">手数策略</span>
            <div class="pf-seg">
              <button
                v-for="opt in LOT_MODE_OPTIONS"
                :key="opt.key"
                type="button"
                class="pf-seg-item"
                :class="{ active: rule.lot_mode === opt.key }"
                @click="setLotMode(symbol, opt.key)"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
          <div class="filter-field">
            <span class="pf-label">固定手数</span>
            <input
              :value="rule.lot ?? ''"
              type="number"
              step="0.01"
              class="pf-input filter-num-input"
              :disabled="rule.lot_mode !== 'fixed'"
              @input="updateNodeRule(symbol, { lot: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
          <div class="filter-field">
            <span class="pf-label">轮询初始顺序（越小越先轮到）</span>
            <input
              :value="rule.poll_order"
              type="number"
              step="1"
              class="pf-input filter-num-input"
              @input="updateNodeRule(symbol, { poll_order: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
        </div>
      </article>
    </template>
  </div>
</template>

<style scoped>
/* 版式 1:1 复刻 UI/H5.html 原型；
   卡片 / 分段 / 开关 / 步进器等共用外观见 styles.css 的 pf- 前缀类。 */
.filter-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 工具栏 ── */
.filter-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.filter-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.filter-symbol-search {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-width: 200px;
  flex: 1 1 220px;
  max-width: 320px;
}
.filter-symbol-search-icon {
  position: absolute;
  left: 10px;
  width: 14px;
  height: 14px;
  color: var(--pf-dim);
  pointer-events: none;
}
.filter-symbol-search-input {
  width: 100%;
  height: 32px;
  padding: 0 60px 0 30px;
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-text);
  font-size: 12px;
  outline: none;
  transition: border-color 0.15s;
}
.filter-symbol-search-input:focus { border-color: var(--pf-blue); }
.filter-symbol-search-input::-webkit-search-cancel-button { display: none; }
.filter-symbol-search-clear {
  position: absolute;
  right: 6px;
  width: 20px;
  height: 20px;
  padding: 0;
  border-radius: 50%;
  background: var(--pf-card);
  border: 1px solid var(--pf-border-2);
  color: var(--pf-muted);
  font-size: 14px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.filter-symbol-search-clear:hover { color: var(--pf-text); }
.filter-symbol-search-count {
  position: absolute;
  right: 32px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--pf-dim);
  pointer-events: none;
  white-space: nowrap;
}

/* ── 添加品种行 ── */
.filter-add-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.filter-add-input {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1 1 220px;
  min-width: 0;
}
.filter-add-input svg {
  position: absolute;
  left: 10px;
  width: 14px;
  height: 14px;
  color: var(--pf-dim);
  pointer-events: none;
}
.filter-add-input input {
  width: 100%;
  height: 34px;
  padding: 0 12px 0 32px;
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-text);
  font-size: 12px;
  outline: none;
}
.filter-add-input input:focus { border-color: var(--pf-blue); }

/* ── 品种卡片 ── */
.filter-symbol {
  border-radius: 12px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  padding: 10px;
}

.filter-symbol-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid #1a2436;
}

.filter-symbol-title {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.filter-symbol-title svg {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  color: var(--pf-dim);
}

.filter-symbol-name {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
  color: var(--pf-text);
}

.filter-symbol-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

/* ── 区块 ── */
.filter-section {
  padding: 12px 0;
  border-bottom: 1px solid #1a2436;
}
.filter-section-last { border-bottom: none; padding-bottom: 0; }

.filter-switch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  gap: 10px;
  margin-top: 10px;
}

.filter-field { margin-top: 10px; }
.filter-field:first-of-type { margin-top: 8px; }

.filter-lot-switch {
  margin-top: 8px;
  max-width: 260px;
}

/* ── 区间卡片 ── */
.filter-range {
  margin-top: 10px;
  border-radius: 8px;
  border: 1px solid var(--pf-border);
  background: rgba(6, 10, 18, 0.6);
  padding: 8px;
}

.filter-range-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.filter-range-title {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  font-size: 10px;
  font-weight: 500;
  color: var(--pf-dim);
}
.filter-range-title svg { width: 12px; height: 12px; }
.filter-range-desc { color: var(--pf-faint); font-weight: 400; }

.filter-range-del {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
  padding: 2px 6px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: rgba(248, 113, 113, 0.8);
  font-size: 10px;
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
}
.filter-range-del:hover { color: #f87171; background: rgba(239, 68, 68, 0.1); }
.filter-range-del svg { width: 12px; height: 12px; }

.filter-range-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 6px;
}
.filter-range-field { display: block; min-width: 0; }

.filter-num-input {
  height: 30px;
  font-family: var(--mono);
  font-size: 12px;
}

.filter-range-switches {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  margin-top: 8px;
}
.filter-range-switches .pf-switch-row { min-width: 140px; }

.filter-empty-line { margin-top: 8px; }

/* ── 手动触发 ── */
.filter-manual {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
</style>
