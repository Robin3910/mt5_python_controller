<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElSwitch } from 'element-plus'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import type { FilterDirection, FilterRulesConfig, SymbolFilterRule } from '@/api/types'
import {
  createEmptyInterval,
  createEmptySymbolRule,
  exampleFilterRules,
} from '@/utils/filterRules'

const model = defineModel<FilterRulesConfig>({ required: true })

const addingSymbol = ref(false)
const newSymbol = ref('')

const symbolList = computed(() =>
  Object.entries(model.value)
    .map(([symbol, rule]) => ({ symbol, rule }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol)),
)

function updateRule(symbol: string, patch: Partial<SymbolFilterRule>): void {
  const cur = model.value[symbol]
  if (!cur) return
  model.value = {
    ...model.value,
    [symbol]: { ...cur, ...patch },
  }
}

function removeSymbol(symbol: string): void {
  if (!confirm(`删除品种 ${symbol} 的过滤规则？`)) return
  const next = { ...model.value }
  delete next[symbol]
  model.value = next
}

function confirmAddSymbol(): void {
  const key = newSymbol.value.trim().toUpperCase()
  if (!key) return
  if (model.value[key]) {
    alert(`品种 ${key} 已存在`)
    return
  }
  model.value = { ...model.value, [key]: createEmptySymbolRule() }
  newSymbol.value = ''
  addingSymbol.value = false
}

function cancelAddSymbol(): void {
  newSymbol.value = ''
  addingSymbol.value = false
}

function addInterval(symbol: string): void {
  const rule = model.value[symbol]
  if (!rule) return
  updateRule(symbol, { intervals: [...rule.intervals, createEmptyInterval()] })
}

function updateInterval(
  symbol: string,
  index: number,
  patch: Partial<{ low: number; high: number; allow: FilterDirection[] }>,
): void {
  const rule = model.value[symbol]
  if (!rule) return
  const intervals = rule.intervals.map((iv, i) => (i === index ? { ...iv, ...patch } : iv))
  updateRule(symbol, { intervals })
}

function toggleDirection(symbol: string, index: number, dir: FilterDirection, checked: boolean): void {
  const rule = model.value[symbol]
  if (!rule) return
  const iv = rule.intervals[index]
  if (!iv) return
  const allow = new Set(iv.allow)
  if (checked) allow.add(dir)
  else allow.delete(dir)
  updateInterval(symbol, index, { allow: [...allow] })
}

function removeInterval(symbol: string, index: number): void {
  const rule = model.value[symbol]
  if (!rule) return
  updateRule(symbol, {
    intervals: rule.intervals.filter((_, i) => i !== index),
  })
}

function loadExample(): void {
  if (Object.keys(model.value).length && !confirm('载入示例将覆盖当前配置，是否继续？')) return
  model.value = exampleFilterRules()
}

function setAllowBuy(symbol: string, value: string | number | boolean): void {
  updateRule(symbol, { allow_buy: Boolean(value) })
}

function setAllowSell(symbol: string, value: string | number | boolean): void {
  updateRule(symbol, { allow_sell: Boolean(value) })
}

defineExpose({ loadExample })
</script>

<template>
  <div class="filter-editor">
    <div class="row between filter-toolbar">
      <p class="muted filter-hint">
        按品种设置价格区间与允许方向。可单独关闭某品种的做多/做空总开关；价格落在区间内时只允许勾选的方向开仓；不在任何区间时按「默认动作」处理。
      </p>
      <div class="row filter-toolbar-actions">
        <button type="button" class="btn-sm btn-ghost" @click="loadExample">载入示例</button>
        <button type="button" class="btn-sm btn-primary" @click="addingSymbol = true">+ 添加品种</button>
      </div>
    </div>

    <div v-if="addingSymbol" class="filter-add-symbol card card-pad">
      <div class="form-grid two">
        <div>
          <label>品种代码</label>
          <input
            v-model="newSymbol"
            placeholder="如 XAUUSD"
            @keyup.enter="confirmAddSymbol"
          />
        </div>
        <div class="row" style="align-items: flex-end; gap: 8px">
          <button type="button" class="btn-primary btn-sm" @click="confirmAddSymbol">确认添加</button>
          <button type="button" class="btn-sm btn-ghost" @click="cancelAddSymbol">取消</button>
        </div>
      </div>
    </div>

    <div v-if="!symbolList.length" class="filter-empty muted">
      尚未配置任何品种。点击「添加品种」开始，或「载入示例」查看 XAUUSD 示范。
    </div>

    <div v-for="{ symbol, rule } in symbolList" :key="symbol" class="filter-symbol-card card card-pad">
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
            <el-switch
              :model-value="rule.allow_buy"
              @change="setAllowBuy(symbol, $event)"
            />
          </div>
          <div class="filter-dir-switch">
            <span class="filter-switch-label">允许做空 (SELL)</span>
            <el-switch
              :model-value="rule.allow_sell"
              @change="setAllowSell(symbol, $event)"
            />
          </div>
        </div>
        <button type="button" class="btn-sm btn-ghost" @click="removeSymbol(symbol)">删除品种</button>
      </div>

      <div class="form-grid two" style="margin-top: 12px">
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
      </div>

      <div class="filter-intervals">
        <div class="row between" style="margin: 14px 0 8px">
          <span class="muted" style="font-size: 12px">价格区间列表</span>
          <button type="button" class="btn-sm btn-ghost" @click="addInterval(symbol)">+ 添加区间</button>
        </div>

        <div v-if="!rule.intervals.length" class="muted" style="font-size: 12px; padding: 8px 0">
          暂无区间。添加后，仅当价格落在区间内才会按允许方向过滤。
        </div>

        <!-- 桌面：表格 -->
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

        <!-- 手机：卡片 -->
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
  </div>
</template>

<style scoped>
.filter-editor { display: flex; flex-direction: column; gap: 12px; }
.filter-hint { font-size: 12px; margin: 0; flex: 1; min-width: 200px; }
.filter-toolbar { align-items: flex-start; gap: 12px; }
.filter-toolbar-actions { flex-shrink: 0; }
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
  .filter-toolbar-actions { width: 100%; justify-content: flex-end; }
}
</style>
