<script setup lang="ts">
// 策略管理页：基于模版新建策略、绑定品种，选模版后可自定义规则参数
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import StrategyFormModal from '@/components/StrategyFormModal.vue'
import { useHubStore } from '@/stores/hub'
import type {
  BatchCalcType,
  GridMode,
  GridSide,
  StrategyBatchLevel,
  StrategyOut,
  StrategyRule,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

/** ATR / 波幅统计的已收盘 K 线根数，与后端 BATCH_BAR_PERIOD 一致 */
const BATCH_BAR_PERIOD = 14

/** 规则 type，与后端 strategy_templates 对齐 */
const RULE_TYPE_COUNTER = 1
const RULE_TYPE_TREND = 2
const RULE_TYPE_RISK_SIZED = 3
const RULE_TYPE_GRID = 4

const hub = useHubStore()
const router = useRouter()

const searchQuery = ref('')
const appliedQuery = ref('')
const loading = ref(false)

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
  await loadStrategies()
})

const RULE_TYPE_LABEL: Record<number, string> = {
  [RULE_TYPE_COUNTER]: '逆势加仓',
  [RULE_TYPE_TREND]: '顺势加仓',
  [RULE_TYPE_RISK_SIZED]: '以损定量趋势单',
  [RULE_TYPE_GRID]: '网格交易',
}

/** 仅影响列表展示：顺势加仓在前，逆势加仓在后 */
const RULE_DISPLAY_ORDER: Record<number, number> = {
  [RULE_TYPE_TREND]: 0,
  [RULE_TYPE_COUNTER]: 1,
  [RULE_TYPE_RISK_SIZED]: 2,
  [RULE_TYPE_GRID]: 3,
}

function displayRules(rules: StrategyRule[]): StrategyRule[] {
  return [...rules].sort(
    (a, b) => (RULE_DISPLAY_ORDER[a.type] ?? a.type) - (RULE_DISPLAY_ORDER[b.type] ?? b.type),
  )
}

function isRiskSized(rule: { type: number }): boolean {
  return rule.type === RULE_TYPE_RISK_SIZED
}

function isGrid(rule: { type: number }): boolean {
  return rule.type === RULE_TYPE_GRID
}

const GRID_MODE_OPTIONS: Array<{ value: GridMode; label: string }> = [
  { value: 'arithmetic', label: '等差' },
  { value: 'geometric', label: '等比' },
]

const GRID_SIDE_OPTIONS: Array<{ value: GridSide; label: string }> = [
  { value: 'long', label: '只做多' },
  { value: 'short', label: '只做空' },
]

/** 网格止损/止盈展示：字段按价格上下沿存储，文案随方向切换 */
function gridStopFields(side: GridSide | undefined | null): {
  slKey: 'stop_lower' | 'stop_upper'
  tpKey: 'stop_lower' | 'stop_upper'
} {
  if (side === 'short') {
    return { slKey: 'stop_upper', tpKey: 'stop_lower' }
  }
  return { slKey: 'stop_lower', tpKey: 'stop_upper' }
}

const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  showForm.value = true
}

function openEdit(s: StrategyOut): void {
  formMode.value = 'edit'
  editingId.value = s.strategy_id
  showForm.value = true
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
  const enabled = displayRules(rules).filter((r) => r.status === 1)
  if (!enabled.length) return '全部关闭'
  return enabled
    .map((r) => {
      const label = RULE_TYPE_LABEL[r.type] || `类型${r.type}`
      if (isRiskSized(r)) {
        const batches = r.add_batches ?? 0
        return (
          `${label}（${entryModeLabel(r)} · 风险 ${r.risk_amount ?? 0}` +
          ` · 盈亏比 ${r.rr_ratio ?? 0}${batches ? ` · 分散 ${batches} 单` : ''}）`
        )
      }
      if (isGrid(r)) {
        const side = GRID_SIDE_OPTIONS.find((o) => o.value === r.grid_side)?.label || r.grid_side
        const mode = GRID_MODE_OPTIONS.find((o) => o.value === r.grid_mode)?.label || r.grid_mode
        const trailing = r.trailing_up ? ' · 追踪' : ''
        return `${label}（${r.grid_count ?? 0} 格${mode} · ${side} · 每格 ${r.lot_per_grid ?? 0}${trailing}）`
      }
      const levels = r.batch_levels?.length ?? 0
      return r.batch_enabled && levels ? `${label}（分批 ${levels} 档）` : label
    })
    .join(' · ')
}

const ACTION_LABEL: Record<string, string> = {
  all: '全部',
  buy: '多单',
  sell: '空单',
}

const CALC_TYPE_LABEL: Record<BatchCalcType, string> = {
  point: '点数',
  price: '指定价',
  atr: 'ATR',
  range: '波幅',
}

const expandedIds = ref<Set<string>>(new Set())

function toggleExpand(id: string): void {
  const next = new Set(expandedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedIds.value = next
}

function isExpanded(id: string): boolean {
  return expandedIds.value.has(id)
}

function actionLabel(action: string | undefined | null): string {
  const key = String(action || '').toLowerCase()
  return ACTION_LABEL[key] || action || '—'
}

function calcTypeLabel(calcType: BatchCalcType | string | undefined): string {
  const key = (calcType || 'point') as BatchCalcType
  return CALC_TYPE_LABEL[key] || String(calcType || '点数')
}

/** 开仓方式；缺字段的历史配置一律按市价，与后端归一化口径一致 */
function isLimitEntry(r: StrategyRule): boolean {
  return r.entry_mode === 'limit'
}

function entryModeLabel(r: StrategyRule): string {
  return isLimitEntry(r) ? '限价' : '市价'
}

/** 以损定量的脚注：两种开仓方式对信号的要求与风险口径都不一样 */
function riskSizedFootnote(r: StrategyRule): string {
  if (isLimitEntry(r)) {
    return (
      '手数由风险金额与入场价到止损价的距离反推；底仓 TP=0，' +
      '挂单一直等到成交（GTC）；信号必须携带 sl 与入场价'
    )
  }
  return '手数由风险金额与信号止损价反推；底仓 TP=0，分散仓市价开齐；信号必须携带 sl'
}

/** 规则详情的参数行；各规则类型的字段集合不同，展示由此按 type 分派 */
function ruleDetailRows(r: StrategyRule): Array<{ k: string; v: string }> {
  if (isRiskSized(r)) {
    const batches = r.add_batches ?? 0
    const limit = isLimitEntry(r)
    return [
      { k: '监控方向', v: actionLabel(r.action) },
      {
        k: '开仓方式',
        v: limit ? '限价（挂在信号入场价，GTC 等成交）' : '市价（信号一到打齐）',
      },
      {
        k: '风险金额',
        v: `${r.risk_amount ?? 0}`,
      },
      { k: '盈亏比', v: String(r.rr_ratio ?? 0) },
      {
        k: '底仓',
        v: `${batches ? (r.base_ratio ?? 0) : 100}%（${limit ? '挂信号入场价' : '市价'} · TP=0）`,
      },
      {
        k: '分散仓',
        v: batches
          ? limit
            ? `剩余等分 ${batches} 单，全部挂在信号入场价（阶梯止盈，末档吃满盈亏比）`
            : `剩余等分 ${batches} 单市价（阶梯止盈，末档吃满盈亏比）`
          : '无（底仓即全仓）',
      },
      { k: '总手数上限', v: r.max_total_lot ? String(r.max_total_lot) : '不限' },
      {
        k: '保本触发',
        v: r.breakeven_enabled
          ? `止损距 × ${r.breakeven_times ?? 0} 倍 · ${r.breakeven_mode === 'loop' ? '循环' : '按次'}`
          : '未启用',
      },
    ]
  }
  if (isGrid(r)) {
    const side = GRID_SIDE_OPTIONS.find((o) => o.value === r.grid_side)?.label || r.grid_side || '—'
    const mode = GRID_MODE_OPTIONS.find((o) => o.value === r.grid_mode)?.label || r.grid_mode || '—'
    const stops = gridStopFields(r.grid_side)
    const sl = r[stops.slKey] || 0
    const tp = r[stops.tpKey] || 0
    // 试算只在开启过时展示，且写明「配置期」：格数与手数已经落进上面两行，
    // 这一行只是留痕，避免被读成运行期还在按 ATR 算
    const assist = r.assist_enabled
      ? [
          {
            k: '试算来源',
            v:
              `配置期 · ATR(${r.assist_timeframe || 'H1'}) × ${r.assist_atr_mult ?? 0}` +
              `${r.assist_spacing ? ` · 格距 ${r.assist_spacing}` : ''}` +
              `${r.assist_max_loss ? ` · 预算 ${r.assist_max_loss}` : ''}`,
          },
        ]
      : []
    return [
      { k: '价格区间', v: `${r.price_lower ?? 0} ~ ${r.price_upper ?? 0}` },
      { k: '网格', v: `${r.grid_count ?? 0} 格 · ${mode}` },
      { k: '方向', v: side },
      { k: '每格手数', v: String(r.lot_per_grid ?? 0) },
      { k: '总手数上限', v: r.total_lot_limit ? String(r.total_lot_limit) : '不限' },
      { k: '触发价', v: r.trigger_price ? String(r.trigger_price) : '立即启动' },
      {
        k: '止损 / 止盈',
        v: `${sl || '不设'} / ${tp || '不设'}`,
      },
      { k: '终止清仓', v: r.close_on_stop === false ? '否' : '是' },
      { k: '初始建仓', v: r.prefill_enabled === false ? '关闭' : '开启' },
      {
        k: '向上追踪',
        v: r.trailing_up ? (r.trailing_max ? `开启 · 最多 ${r.trailing_max} 格` : '开启 · 不限') : '关闭',
      },
      ...assist,
    ]
  }
  return [
    { k: '监控方向', v: actionLabel(r.action) },
    { k: '触发点数', v: String(r.point ?? 0) },
    { k: '倍数', v: String(r.lot_times ?? 0) },
    { k: '额外手数', v: String(r.extra_lot ?? 0) },
    { k: '最大加仓次数', v: String(r.max_allow_num ?? 0) },
  ]
}

/** 档位间距摘要：按计算方式展示点数 / 价位 / ATR·波幅周期 */
function levelSpacingText(lv: StrategyBatchLevel): string {
  const kind = (lv.calc_type || 'point') as BatchCalcType
  if (kind === 'price') return `价位 ${lv.price ?? 0}`
  if (kind === 'atr') return `ATR(${lv.timeframe || 'M5'}×${BATCH_BAR_PERIOD})`
  if (kind === 'range') return `波幅(${lv.timeframe || 'M5'}×${BATCH_BAR_PERIOD})`
  return `${lv.point ?? 0} 点`
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}
</script>

<template>
  <div class="strategies-page">
    <div class="row between page-header">
      <div>
        <div class="h1">策略管理</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          基于策略模版创建实例并绑定品种；AI智能加仓策略配顺势 / 逆势加仓，模版2 配以损定量趋势单，模版3 配网格交易
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
      <div
        v-for="s in hub.strategies"
        :key="s.strategy_id"
        class="list-card card clickable"
        @click="toggleExpand(s.strategy_id)"
      >
        <div class="list-card-head row between">
          <strong>
            <span class="muted" style="margin-right: 6px">{{ isExpanded(s.strategy_id) ? '▾' : '▸' }}</span>
            {{ s.name }}
          </strong>
          <span class="tag" :class="s.enabled ? 'green' : ''">{{ s.enabled ? '已启用' : '已禁用' }}</span>
        </div>
        <div class="list-field"><span class="k">策略 ID</span><span class="v muted" style="font-size: 12px">{{ s.strategy_id }}</span></div>
        <div class="list-field"><span class="k">模版</span><span class="v">{{ s.template_name }}</span></div>
        <div class="list-field"><span class="k">绑定品种</span><span class="v"><code>{{ s.symbol }}</code></span></div>
        <div class="list-field"><span class="k">规则</span><span class="v">{{ ruleSummary(s.rules) }}</span></div>
        <div class="list-field"><span class="k">创建时间</span><span class="v muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</span></div>
        <div v-if="isExpanded(s.strategy_id)" class="strategy-detail" @click.stop>
          <div v-if="s.remark" class="muted" style="font-size: 12px; margin-bottom: 8px">备注：{{ s.remark }}</div>
          <div v-if="!s.rules.length" class="muted" style="font-size: 12px">暂无规则</div>
          <div v-for="(r, ri) in displayRules(s.rules)" :key="ri" class="strategy-rule-block">
            <div class="row between" style="margin-bottom: 8px">
              <strong style="font-size: 13px">{{ RULE_TYPE_LABEL[r.type] || `规则 ${ri + 1}` }}</strong>
              <span class="tag" :class="r.status === 1 ? 'green' : ''">{{ r.status === 1 ? '启用' : '关闭' }}</span>
            </div>
            <div class="kv-grid" style="margin-bottom: 8px">
              <div v-for="kv in ruleDetailRows(r)" :key="kv.k" class="kv">
                <span class="k">{{ kv.k }}</span><span class="v">{{ kv.v }}</span>
              </div>
            </div>
            <template v-if="isRiskSized(r)">
              <div class="muted" style="font-size: 12px">
                {{ riskSizedFootnote(r) }}
              </div>
            </template>
            <template v-else-if="isGrid(r)">
              <div class="muted" style="font-size: 12px">
                网格空仓是正常运行态；只由止损 / 止盈 / 终止信号收口
              </div>
            </template>
            <template v-else-if="r.batch_enabled">
              <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                分批加仓 · 方向 {{ actionLabel(r.batch_action) }} ·
                {{ r.batch_count ?? 0 }} 批 · 总手数上限 {{ r.total_lot_limit ?? 0 }}
              </div>
              <div v-if="r.batch_levels?.length" class="table-scroll">
                <table class="strategy-levels-table">
                  <thead>
                    <tr>
                      <th>笔数</th>
                      <th>计算</th>
                      <th>间距</th>
                      <th class="right">倍数</th>
                      <th class="right">额外手数</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(lv, li) in r.batch_levels" :key="li">
                      <td>{{ lv.pos_from }}–{{ lv.pos_to }}</td>
                      <td>{{ calcTypeLabel(lv.calc_type) }}</td>
                      <td class="muted" style="font-size: 12px">{{ levelSpacingText(lv) }}</td>
                      <td class="right">{{ lv.lot_times }}</td>
                      <td class="right">{{ lv.extra_lot }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="muted" style="font-size: 12px">暂无分批档位</div>
            </template>
            <div v-else class="muted" style="font-size: 12px">分批加仓：未启用</div>
          </div>
        </div>
        <div class="list-card-actions" @click.stop>
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
            <th style="width: 28px"></th>
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
          <template v-for="s in hub.strategies" :key="s.strategy_id">
            <tr class="clickable" @click="toggleExpand(s.strategy_id)">
              <td class="muted">{{ isExpanded(s.strategy_id) ? '▾' : '▸' }}</td>
              <td>
                {{ s.name }}
                <div class="muted" style="font-size: 11px">{{ s.strategy_id }}</div>
              </td>
              <td><span class="tag blue">{{ s.template_name }}</span></td>
              <td><code>{{ s.symbol }}</code></td>
              <td class="muted" style="font-size: 12px">{{ ruleSummary(s.rules) }}</td>
              <td class="muted" style="font-size: 12px">{{ fmtTime(s.created_at) }}</td>
              <td @click.stop>
                <button class="btn-sm" :class="s.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(s)">
                  {{ s.enabled ? '已启用' : '已禁用' }}
                </button>
              </td>
              <td class="right" @click.stop>
                <div class="row" style="gap: 6px; justify-content: flex-end">
                  <button class="btn-sm btn-ghost" @click="openEdit(s)">编辑</button>
                  <button class="btn-sm btn-danger" @click="remove(s)">删除</button>
                </div>
              </td>
            </tr>
            <tr v-if="isExpanded(s.strategy_id)" class="detail-row">
              <td></td>
              <td colspan="7">
                <div class="strategy-detail">
                  <div v-if="s.remark" class="muted" style="font-size: 12px; margin-bottom: 10px">
                    备注：{{ s.remark }}
                  </div>
                  <div v-if="!s.rules.length" class="muted" style="font-size: 12px">暂无规则</div>
                  <div v-for="(r, ri) in displayRules(s.rules)" :key="ri" class="strategy-rule-block">
                    <div class="row between" style="margin-bottom: 8px">
                      <strong style="font-size: 13px">{{ RULE_TYPE_LABEL[r.type] || `规则 ${ri + 1}` }}</strong>
                      <span class="tag" :class="r.status === 1 ? 'green' : ''">
                        {{ r.status === 1 ? '启用' : '关闭' }}
                      </span>
                    </div>
                    <div class="kv-grid" style="margin-bottom: 8px">
                      <div v-for="kv in ruleDetailRows(r)" :key="kv.k" class="kv">
                        <span class="k">{{ kv.k }}</span><span class="v">{{ kv.v }}</span>
                      </div>
                    </div>
                    <template v-if="isRiskSized(r)">
                      <div class="muted" style="font-size: 12px">
                        {{ riskSizedFootnote(r) }}
                      </div>
                    </template>
                    <template v-else-if="isGrid(r)">
                      <div class="muted" style="font-size: 12px">
                        网格空仓是正常运行态；只由止损 / 止盈 / 终止信号收口
                      </div>
                    </template>
                    <template v-else-if="r.batch_enabled">
                      <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                        分批加仓 · 方向 {{ actionLabel(r.batch_action) }} ·
                        {{ r.batch_count ?? 0 }} 批 · 总手数上限 {{ r.total_lot_limit ?? 0 }}
                      </div>
                      <div v-if="r.batch_levels?.length" class="table-scroll">
                        <table class="strategy-levels-table">
                          <thead>
                            <tr>
                              <th>笔数区间</th>
                              <th>计算方式</th>
                              <th>间距</th>
                              <th class="right">倍数</th>
                              <th class="right">额外手数</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr v-for="(lv, li) in r.batch_levels" :key="li">
                              <td>{{ lv.pos_from }}–{{ lv.pos_to }}</td>
                              <td>{{ calcTypeLabel(lv.calc_type) }}</td>
                              <td class="muted" style="font-size: 12px">{{ levelSpacingText(lv) }}</td>
                              <td class="right">{{ lv.lot_times }}</td>
                              <td class="right">{{ lv.extra_lot }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div v-else class="muted" style="font-size: 12px">暂无分批档位</div>
                    </template>
                    <div v-else class="muted" style="font-size: 12px">分批加仓：未启用</div>
                  </div>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!hub.strategies.length && !loading">
            <td colspan="8" class="muted" style="padding: 18px">
              {{ appliedQuery ? '无匹配策略' : '暂无策略，点击右上角「新增策略」开始配置' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <StrategyFormModal
      v-model="showForm"
      :mode="formMode"
      :strategy-id="editingId"
      :list-search-options="currentSearchOptions()"
    />
  </div>
</template>

<style scoped>
.page-header {
  margin-bottom: 14px;
  align-items: flex-start;
}

.strategy-detail {
  margin: 6px 0 4px;
}

.strategy-rule-block {
  margin-bottom: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  border: 1px solid var(--glass-border);
}

.strategy-rule-block:last-child {
  margin-bottom: 0;
}

.strategy-levels-table {
  width: 100%;
  margin-top: 4px;
}

.strategy-levels-table th,
.strategy-levels-table td {
  padding: 6px 8px;
  font-size: 12px;
}

.list-card .strategy-detail {
  margin: 10px 0;
  padding-top: 8px;
  border-top: 1px dashed var(--glass-border);
}
</style>
