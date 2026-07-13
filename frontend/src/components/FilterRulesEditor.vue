<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElSwitch } from 'element-plus'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/theme-chalk/dark/css-vars.css'
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
} from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'

const props = withDefaults(defineProps<{ mode?: 'global' | 'node' }>(), { mode: 'global' })

const model = defineModel<FilterRulesConfig | NodeDispatchFiltersConfig>({ required: true })

const isGlobal = computed(() => props.mode === 'global')
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
    alert(`品种 ${key} 已存在`)
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
  if (Object.keys(model.value).length && !(await confirmAction('载入示例将覆盖当前配置，是否继续？', '确认载入示例'))) return
  model.value = exampleFilterRules()
}

function setAllowBuy(symbol: string, value: string | number | boolean): void {
  updateRule(symbol, { allow_buy: Boolean(value) })
}

function setAllowSell(symbol: string, value: string | number | boolean): void {
  updateRule(symbol, { allow_sell: Boolean(value) })
}

function setFollowSync(symbol: string, value: string | number | boolean): void {
  updateNodeRule(symbol, { follow_sync: Boolean(value) })
}

function setFollowPoll(symbol: string, value: string | number | boolean): void {
  updateNodeRule(symbol, { follow_poll: Boolean(value) })
}

defineExpose({ loadExample })
</script>

<template>
  <div class="filter-editor">
    <div class="row between filter-toolbar">
      <p v-if="isGlobal" class="muted filter-hint">
        按品种设置分发策略、价格区间、允许方向与全局手数。未登记或取消「启用」的品种信号将被直接拒收（含平仓）；后台手动平仓不受影响。节点手数策略为「跟随中控台」时使用此处配置的手数。
      </p>
      <p v-else class="muted filter-hint">
        按品种配置该节点的分发参与、手数策略与轮询顺序。未配置品种将回退节点默认策略（固定 0.01、轮询序 0）。
      </p>
      <div class="row filter-toolbar-actions">
        <div v-if="totalSymbolCount > 0" class="filter-symbol-search">
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
          >×</button>
          <span v-if="hasKeyword" class="filter-symbol-search-count">
            {{ visibleSymbolCount }} / {{ totalSymbolCount }}
          </span>
        </div>
        <button v-if="isGlobal" type="button" class="btn-sm btn-ghost" @click="loadExample">载入示例</button>
        <button type="button" class="btn-sm btn-primary" @click="addingSymbol = true">+ 添加品种</button>
      </div>
    </div>

    <div v-if="addingSymbol" class="filter-add-symbol card card-pad">
      <div class="form-grid two">
        <div>
          <label>品种代码</label>
          <input v-model="newSymbol" placeholder="如 XAUUSD" @keyup.enter="confirmAddSymbol" />
        </div>
        <div class="row" style="align-items: flex-end; gap: 8px">
          <button type="button" class="btn-primary btn-sm" @click="confirmAddSymbol">确认添加</button>
          <button type="button" class="btn-sm btn-ghost" @click="cancelAddSymbol">取消</button>
        </div>
      </div>
    </div>

    <template v-if="isGlobal">
      <div v-if="!totalSymbolCount" class="filter-empty muted">
        尚未配置任何品种。点击「添加品种」开始，或「载入示例」查看 XAUUSD 示范。
      </div>
      <div v-else-if="!globalSymbolList.length" class="filter-empty muted">
        无匹配「{{ filterKeyword }}」的品种。
        <button type="button" class="btn-sm btn-ghost" style="margin-left: 8px" @click="clearFilterKeyword">清除筛选</button>
      </div>

      <div v-for="{ symbol, rule } in globalSymbolList" :key="symbol" class="filter-symbol-card card card-pad">
        <div class="row between filter-symbol-head">
          <div class="row filter-symbol-switches" style="gap: 10px; flex-wrap: wrap">
            <strong class="filter-symbol-name">{{ symbol }}</strong>
            <label class="filter-enabled">
              <input
                type="checkbox"
                :checked="rule.enabled"
                @change="updateRule(symbol, { enabled: ($event.target as HTMLInputElement).checked })"
              />
              启用
            </label>
            <div class="filter-dir-switch">
              <span class="filter-switch-label">允许做多 (BUY)</span>
              <el-switch :model-value="rule.allow_buy" @change="setAllowBuy(symbol, $event)" />
            </div>
            <div class="filter-dir-switch">
              <span class="filter-switch-label">允许做空 (SELL)</span>
              <el-switch :model-value="rule.allow_sell" @change="setAllowSell(symbol, $event)" />
            </div>
          </div>
          <button type="button" class="btn-sm btn-ghost" @click="removeSymbol(symbol)">删除品种</button>
        </div>

        <div class="form-grid two" style="margin-top: 12px">
          <div>
            <label>分发模式</label>
            <select
              :value="rule.dispatch_mode"
              @change="updateRule(symbol, { dispatch_mode: ($event.target as HTMLSelectElement).value as 'sync' | 'poll' })"
            >
              <option value="sync">全员同步</option>
              <option value="poll">轮询轮转（单节点领取）</option>
            </select>
          </div>
          <div>
            <label>持仓判定范围</label>
            <select
              :value="rule.position_scope"
              @change="updateRule(symbol, { position_scope: ($event.target as HTMLSelectElement).value as 'symbol' | 'account' })"
            >
              <option value="symbol">按品种（同品种无持仓才开）</option>
              <option value="account">按账户（账户无任何持仓才开）</option>
            </select>
          </div>
          <div>
            <label>默认动作（价格不在任何区间内）</label>
            <select
              :value="rule.default_action"
              @change="updateRule(symbol, { default_action: ($event.target as HTMLSelectElement).value as 'block' | 'pass' })"
            >
              <option value="block">拦截 (block)</option>
              <option value="pass">放行 (pass)</option>
            </select>
          </div>
          <div>
            <label>启用全局手数</label>
            <select
              :value="rule.lot_enabled ? 'true' : 'false'"
              @change="updateRule(symbol, { lot_enabled: ($event.target as HTMLSelectElement).value === 'true' })"
            >
              <option value="false">关闭</option>
              <option value="true">启用</option>
            </select>
          </div>
          <div>
            <label>全局手数</label>
            <input
              :value="rule.lot"
              type="number"
              step="0.01"
              min="0.01"
              :disabled="!rule.lot_enabled"
              @input="updateRule(symbol, { lot: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
        </div>

        <div class="filter-intervals">
          <div class="row between" style="margin: 14px 0 8px">
            <span class="muted" style="font-size: 12px">价格区间列表</span>
            <button type="button" class="btn-sm btn-ghost" @click="addInterval(symbol)">+ 添加区间</button>
          </div>

          <div v-if="!rule.intervals.length" class="muted" style="font-size: 12px; padding: 8px 0">
            暂无区间。添加后，仅当价格落在区间内才会按允许方向过滤。
          </div>

          <div v-else class="table-scroll desktop-only">
            <table class="filter-interval-table">
              <thead>
                <tr>
                  <th>价格下限</th>
                  <th>价格上限</th>
                  <th>允许方向</th>
                  <th class="right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(iv, idx) in rule.intervals" :key="idx">
                  <td>
                    <input
                      :value="iv.low"
                      type="number"
                      step="any"
                      class="filter-num-input"
                      @input="updateInterval(symbol, idx, { low: Number(($event.target as HTMLInputElement).value) })"
                    />
                  </td>
                  <td>
                    <input
                      :value="iv.high"
                      type="number"
                      step="any"
                      class="filter-num-input"
                      @input="updateInterval(symbol, idx, { high: Number(($event.target as HTMLInputElement).value) })"
                    />
                  </td>
                  <td>
                    <div class="row filter-dir-row">
                      <label class="filter-dir">
                        <input
                          type="checkbox"
                          :checked="iv.allow.includes('BUY')"
                          @change="toggleDirection(symbol, idx, 'BUY', ($event.target as HTMLInputElement).checked)"
                        />
                        做多 (BUY)
                      </label>
                      <label class="filter-dir">
                        <input
                          type="checkbox"
                          :checked="iv.allow.includes('SELL')"
                          @change="toggleDirection(symbol, idx, 'SELL', ($event.target as HTMLInputElement).checked)"
                        />
                        做空 (SELL)
                      </label>
                    </div>
                  </td>
                  <td class="right">
                    <button type="button" class="btn-sm btn-ghost" @click="removeInterval(symbol, idx)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="rule.intervals.length" class="mobile-only list-cards">
            <div v-for="(iv, idx) in rule.intervals" :key="idx" class="list-card-nested">
              <div class="list-field">
                <span class="k">价格下限</span>
                <input
                  :value="iv.low"
                  type="number"
                  step="any"
                  @input="updateInterval(symbol, idx, { low: Number(($event.target as HTMLInputElement).value) })"
                />
              </div>
              <div class="list-field">
                <span class="k">价格上限</span>
                <input
                  :value="iv.high"
                  type="number"
                  step="any"
                  @input="updateInterval(symbol, idx, { high: Number(($event.target as HTMLInputElement).value) })"
                />
              </div>
              <div class="list-field">
                <span class="k">允许方向</span>
                <div class="row filter-dir-row">
                  <label class="filter-dir">
                    <input
                      type="checkbox"
                      :checked="iv.allow.includes('BUY')"
                      @change="toggleDirection(symbol, idx, 'BUY', ($event.target as HTMLInputElement).checked)"
                    />
                    做多
                  </label>
                  <label class="filter-dir">
                    <input
                      type="checkbox"
                      :checked="iv.allow.includes('SELL')"
                      @change="toggleDirection(symbol, idx, 'SELL', ($event.target as HTMLInputElement).checked)"
                    />
                    做空
                  </label>
                </div>
              </div>
              <div class="list-card-actions">
                <button type="button" class="btn-sm btn-ghost" @click="removeInterval(symbol, idx)">删除区间</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <template v-else>
      <div v-if="!totalSymbolCount" class="filter-empty muted">
        尚未配置任何品种。点击「添加品种」为该节点设置各币种的分发、手数与轮询顺序。
      </div>
      <div v-else-if="!nodeSymbolList.length" class="filter-empty muted">
        无匹配「{{ filterKeyword }}」的品种。
        <button type="button" class="btn-sm btn-ghost" style="margin-left: 8px" @click="clearFilterKeyword">清除筛选</button>
      </div>

      <div v-for="{ symbol, rule } in nodeSymbolList" :key="symbol" class="filter-symbol-card card card-pad">
        <div class="row between filter-symbol-head">
          <div class="row filter-symbol-switches" style="gap: 10px; flex-wrap: wrap">
            <strong class="filter-symbol-name">{{ symbol }}</strong>
            <div class="filter-dir-switch">
              <span class="filter-switch-label">参与同步</span>
              <el-switch :model-value="rule.follow_sync" @change="setFollowSync(symbol, $event)" />
            </div>
            <div class="filter-dir-switch">
              <span class="filter-switch-label">参与轮询</span>
              <el-switch :model-value="rule.follow_poll" @change="setFollowPoll(symbol, $event)" />
            </div>
          </div>
          <button type="button" class="btn-sm btn-ghost" @click="removeSymbol(symbol)">删除品种</button>
        </div>
        <div class="form-grid three" style="margin-top: 12px">
          <div>
            <label>手数策略</label>
            <select
              :value="rule.lot_mode"
              @change="updateNodeRule(symbol, { lot_mode: ($event.target as HTMLSelectElement).value as 'global' | 'fixed' | 'signal' })"
            >
              <option value="global">跟随中控台</option>
              <option value="fixed">固定手数</option>
              <option value="signal">跟随信号</option>
            </select>
          </div>
          <div>
            <label>固定手数</label>
            <input
              :value="rule.lot ?? ''"
              type="number"
              step="0.01"
              :disabled="rule.lot_mode !== 'fixed'"
              @input="updateNodeRule(symbol, { lot: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
          <div>
            <label>轮询初始顺序（越小越先轮到）</label>
            <input
              :value="rule.poll_order"
              type="number"
              step="1"
              @input="updateNodeRule(symbol, { poll_order: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.filter-editor { display: flex; flex-direction: column; gap: 12px; }
.filter-hint { font-size: 12px; margin: 0; flex: 1; min-width: 200px; }
/* 工具栏 sticky 到滚动容器顶部；无滚动容器时也无副作用 */
.filter-toolbar {
  align-items: flex-start;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 5;
  padding: 10px 14px 12px;
  /* 实心背景，避免 sticky 时下方卡片透视重叠；border + shadow 与内容分层 */
  background: var(--bg-soft);
  border-bottom: 1px solid var(--glass-border);
  box-shadow: 0 6px 10px -8px rgba(0, 0, 0, 0.45);
}
.filter-toolbar-actions { flex-shrink: 0; }

/* 品种筛选输入框 */
.filter-symbol-search {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-width: 200px;
}
.filter-symbol-search-icon {
  position: absolute;
  left: 10px;
  width: 14px;
  height: 14px;
  color: var(--muted);
  pointer-events: none;
}
.filter-symbol-search-input {
  width: 100%;
  padding: 6px 60px 6px 30px;
  font-size: 12px;
  border-radius: var(--radius-sm);
  background: rgba(6, 10, 18, 0.5);
  border: 1px solid var(--border);
  color: var(--text);
  transition: border-color var(--transition), box-shadow var(--transition);
}
.filter-symbol-search-input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(0, 212, 170, 0.12);
}
.filter-symbol-search-input::-webkit-search-cancel-button { display: none; }
.filter-symbol-search-clear {
  position: absolute;
  right: 6px;
  width: 20px;
  height: 20px;
  padding: 0;
  border-radius: 50%;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  color: var(--muted);
  font-size: 14px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.filter-symbol-search-clear:hover {
  color: var(--text);
  border-color: var(--glass-highlight);
}
.filter-symbol-search-count {
  position: absolute;
  right: 32px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--muted);
  pointer-events: none;
  white-space: nowrap;
}

.filter-add-symbol { background: var(--bg-soft); }
.filter-empty {
  padding: 24px 16px;
  text-align: center;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  font-size: 13px;
}
.filter-symbol-card { background: var(--bg-soft); }
.filter-symbol-name { font-size: 15px; letter-spacing: 0.3px; }
.filter-symbol-switches { align-items: center; }
.filter-dir-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.filter-switch-label {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
:deep(.el-switch.is-checked .el-switch__core) {
  background-color: var(--primary);
  border-color: var(--primary);
}
.filter-enabled {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  cursor: pointer;
}
.filter-interval-table { min-width: 520px; }
.filter-num-input { min-width: 100px; }
.filter-dir-row { gap: 16px; flex-wrap: wrap; }
.filter-dir {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
}
@media (max-width: 768px) {
  .filter-toolbar { flex-direction: column; }
  .filter-toolbar-actions { width: 100%; justify-content: flex-end; flex-wrap: wrap; }
  .filter-symbol-search { flex: 1 1 100%; min-width: 0; }
}
</style>
