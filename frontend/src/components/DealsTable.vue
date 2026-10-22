<script setup lang="ts">
// 成交明细（订单历史 Tab 的唯一视图）。
//
// 三个页面（交易 / 节点详情 / 策略）复用本组件，只传 nodeId ——
// 避免三处各写一份实现、各自漂移。
//
// 关键约定：
// - **没有视图切换**：Tab 里就是成交明细本身。用户点【历史订单】想看的是
//   「我到底赚了多少」，成交明细才是答案。
// - **没有「状态」列**：成交即成功，失败/跳过的单压根不在成交里。
//   要排查派发结果请去信号事件页。
// - **统计条用后端返回的 summary**，不要在前端自己算 —— 后端与 items
//   用同一 WHERE 条件，前端再算一遍必然对不上。
import { computed, onMounted, ref, watch } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { DealRecord, DealSummary } from '@/api/types'

const props = defineProps<{
  nodeId: string | null | undefined
}>()

/** 上报成交总条数（父组件用它显示 Tab 角标） */
const emit = defineEmits<{ total: [number] }>()

const hub = useHubStore()

const PAGE_SIZE = 50

// —— 筛选条件 ——
const filterSymbol = ref('')
const filterDirection = ref('')
const filterEntry = ref('')
const filterSource = ref('')
const filterDays = ref(0) // 0 = 全部

// —— 数据 ——
const items = ref<DealRecord[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const summary = ref<DealSummary | null>(null)

// —— 统计条 ——
const stats = computed(() => summary.value)

// 是否有任何筛选条件（用于提示「统计条随筛选联动」）
const hasFilter = computed(
  () =>
    !!filterSymbol.value ||
    !!filterDirection.value ||
    !!filterEntry.value ||
    !!filterSource.value ||
    filterDays.value > 0,
)

const totalPages = computed(() =>
  total.value <= 0 ? 1 : Math.max(1, Math.ceil(total.value / PAGE_SIZE)),
)

/** 构造后端查询参数（空条件不传，避免后端收到无意义的空串过滤）。 */
function queryParams(): Record<string, string | number> {
  const p: Record<string, string | number> = {}
  if (filterSymbol.value.trim()) p.symbol = filterSymbol.value.trim().toUpperCase()
  if (filterDirection.value) p.direction = filterDirection.value
  if (filterEntry.value) p.entry_type = filterEntry.value
  if (filterSource.value) p.source = filterSource.value
  if (filterDays.value > 0) {
    p.from_time = Math.floor(Date.now() / 1000) - filterDays.value * 86400
  }
  return p
}

async function fetchDeals(): Promise<void> {
  const nid = props.nodeId
  if (!nid) {
    items.value = []
    total.value = 0
    summary.value = null
    return
  }
  loading.value = true
  try {
    const res = await hub.fetchNodeDeals(nid, page.value, PAGE_SIZE, queryParams())
    items.value = res.items || []
    total.value = res.total || 0
    summary.value = res.summary || null
    emit('total', total.value)
  } catch (e) {
    items.value = []
    total.value = 0
    summary.value = null
    emit('total', 0)
    console.warn('fetchDeals failed', e)
  } finally {
    loading.value = false
  }
}

/** 筛选条件变化 -> 回到第一页重拉（统计条也随筛选联动） */
watch(
  [filterSymbol, filterDirection, filterEntry, filterSource, filterDays],
  () => {
    page.value = 1
    fetchDeals()
  },
)

/** 节点变化 -> 重拉 */
watch(
  () => props.nodeId,
  () => {
    page.value = 1
    fetchDeals()
  },
)

onMounted(fetchDeals)

async function goPage(p: number): Promise<void> {
  if (p < 1 || p > totalPages.value || p === page.value) return
  page.value = p
  await fetchDeals()
}

// ── 展示辅助 ──
function fmtTime(ts: number | null | undefined): string {
  if (!ts || !isFinite(ts)) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtPrice(n: number, sym: string): string {
  if (!n && n !== 0) return '—'
  if (!isFinite(n)) return '—'
  const digits = /JPY$/.test(sym) ? 3 : 5
  return n.toFixed(digits)
}

function fmtMoney(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  return `${n >= 0 ? '+' : ''}$${n.toFixed(2)}`
}

function fmtVolume(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  return n.toFixed(2)
}

function pnlClass(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return ''
  return n > 0 ? 'pos' : n < 0 ? 'neg' : ''
}

/**
 * 方向色的依据：**持仓方向**，不是 MT5 的成交方向。
 *
 * 一笔盈利的多单平仓，在 MT5 里 type=SELL（卖出平仓）。直接显示 direction
 * 会让人误以为又开了一单空仓，所以出场行按被平掉的那个持仓的方向上色。
 */
function dirClass(d: DealRecord): string {
  const dir = (d.entry_type === 'IN' ? d.direction : d.position_direction) || ''
  const v = dir.toLowerCase()
  if (v === 'buy') return 'buy'
  if (v === 'sell') return 'sell'
  return ''
}

function dirLabel(d: DealRecord): string {
  const dir = (d.entry_type === 'IN' ? d.direction : d.position_direction) || ''
  const v = dir.toLowerCase()
  if (v === 'buy') return '多'
  if (v === 'sell') return '空'
  return '—'
}

/** MT5 DEAL_REASON 的人读标签 */
const REASON_LABELS: Record<number, string> = {
  0: '客户端',
  1: '移动端',
  2: '网页',
  3: '专家',
  4: '止损',
  5: '止盈',
  6: '对手方',
}
function reasonLabel(r: number): string {
  return REASON_LABELS[r] ?? `原因${r}`
}

/** 数据新鲜度提示：成交是准实时的，绩效必然滞后，UI 要标注 */
function updatedText(): string {
  const ts = summary.value?.updated_at
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `数据更新于 ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
</script>

<template>
  <div class="deals">
    <!-- 统计条：随筛选条件联动（数据来自后端 summary） -->
    <div v-if="stats" class="deals-stats">
      <div class="ds-item">
        <span class="ds-k">净盈亏</span>
        <span class="ds-v mono" :class="pnlClass(stats.total_profit)">
          {{ fmtMoney(stats.total_profit) }}
        </span>
      </div>
      <div class="ds-item">
        <span class="ds-k">胜率</span>
        <span class="ds-v mono">{{ stats.win_rate.toFixed(2) }}%</span>
      </div>
      <div class="ds-item">
        <span class="ds-k">交易笔数</span>
        <span class="ds-v mono">{{ stats.total_trades }}</span>
      </div>
      <div class="ds-item">
        <span class="ds-k">盈利 / 亏损</span>
        <span class="ds-v mono">{{ stats.win_trades }} / {{ stats.lose_trades }}</span>
      </div>
      <div class="ds-item">
        <span class="ds-k">手续费</span>
        <span class="ds-v mono">{{ stats.commission_total.toFixed(2) }}</span>
      </div>
      <div class="ds-item">
        <span class="ds-k">隔夜利息</span>
        <span class="ds-v mono">{{ stats.swap_total.toFixed(2) }}</span>
      </div>
      <div class="ds-item">
        <span class="ds-k">收益率</span>
        <span class="ds-v mono" :class="pnlClass(stats.profit_rate)">
          <template v-if="stats.profit_rate_valid">{{ stats.profit_rate.toFixed(2) }}%</template>
          <template v-else>—</template>
        </span>
      </div>
      <div v-if="updatedText()" class="ds-updated muted">{{ updatedText() }}</div>
    </div>

    <!-- 筛选器 -->
    <div class="deals-filters">
      <select v-model="filterDays" class="df-select">
        <option :value="0">全部时间</option>
        <option :value="1">今天</option>
        <option :value="7">近 7 天</option>
        <option :value="30">近 30 天</option>
        <option :value="90">近 90 天</option>
      </select>
      <input
        v-model="filterSymbol"
        class="df-input mono"
        type="text"
        placeholder="品种"
        aria-label="按品种筛选"
      />
      <select v-model="filterDirection" class="df-select" aria-label="按方向筛选">
        <option value="">全部方向</option>
        <option value="BUY">买单</option>
        <option value="SELL">卖单</option>
      </select>
      <select v-model="filterEntry" class="df-select" aria-label="按开平筛选">
        <option value="">全部开平</option>
        <option value="IN">开仓</option>
        <option value="OUT">平仓</option>
        <option value="INOUT">双向</option>
        <option value="OUT_BY">对冲平仓</option>
      </select>
      <select v-model="filterSource" class="df-select" aria-label="按来源筛选">
        <option value="">全部来源</option>
        <option value="signal">信号</option>
        <option value="strategy">策略</option>
        <option value="manual">手工</option>
        <option value="terminal">终端</option>
      </select>
      <button
        v-if="hasFilter"
        class="btn-sm btn-ghost"
        type="button"
        @click="
          filterSymbol = ''; filterDirection = ''; filterEntry = ''; filterSource = ''; filterDays = 0
        "
      >
        清除筛选
      </button>
    </div>

    <!-- 列表 -->
    <div v-if="loading && items.length === 0" class="detail-empty">
      <div class="dot-spin-lg" aria-hidden="true"></div>
      <div>加载中…</div>
    </div>
    <div v-else-if="items.length === 0" class="detail-empty">
      <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </svg>
      <div>暂无成交记录</div>
      <div v-if="!stats?.data_available" class="muted">节点尚未上报成交数据</div>
    </div>
    <template v-else>
      <!-- 桌面端：表格 -->
      <div class="pos-table-wrap">
        <table class="pos-table deals-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>品种</th>
              <th>方向</th>
              <th>开/平</th>
              <th class="num">手数</th>
              <th class="num">成交价</th>
              <th class="num pnl">盈亏</th>
              <th class="num">手续费</th>
              <th class="num">隔夜利息</th>
              <th>来源</th>
              <th class="num">成交号</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in items" :key="d.id">
              <td class="mono muted">{{ fmtTime(d.deal_time) }}</td>
              <td class="mono strong">{{ d.symbol || '—' }}</td>
              <td>
                <span class="dir-tag" :class="dirClass(d)">{{ dirLabel(d) }}</span>
              </td>
              <td>
                <span class="deal-entry" :class="d.entry_type.toLowerCase()">{{ d.entry_label }}</span>
                <span v-if="d.reason" class="muted deal-reason" :title="`DEAL_REASON=${d.reason}`">
                  {{ reasonLabel(d.reason) }}
                </span>
              </td>
              <td class="num mono">{{ fmtVolume(d.volume) }}</td>
              <td class="num mono">{{ fmtPrice(d.price, d.symbol || '') }}</td>
              <td class="num pnl mono" :class="pnlClass(d.net_profit)">{{ fmtMoney(d.net_profit) }}</td>
              <td class="num mono muted">{{ d.commission.toFixed(2) }}</td>
              <td class="num mono muted">{{ d.swap.toFixed(2) }}</td>
              <td><span class="deal-source">{{ d.source_label }}</span></td>
              <td class="num mono muted">{{ d.deal_id }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 手机端：卡片列表 -->
      <div class="pos-cards">
        <div v-for="d in items" :key="d.id" class="pos-card">
          <div class="pos-card-row1">
            <div class="pos-card-left">
              <span class="mono strong">{{ d.symbol || '—' }}</span>
              <span class="dir-tag" :class="dirClass(d)">{{ dirLabel(d) }}</span>
              <span class="deal-entry" :class="d.entry_type.toLowerCase()">{{ d.entry_label }}</span>
            </div>
            <div class="pos-card-pnl mono" :class="pnlClass(d.net_profit)">
              {{ fmtMoney(d.net_profit) }}
            </div>
          </div>
          <div class="pos-card-row2 muted mono">
            {{ fmtTime(d.deal_time) }} · {{ fmtVolume(d.volume) }} 手 ·
            @{{ fmtPrice(d.price, d.symbol || '') }} · {{ d.source_label }}
          </div>
        </div>
      </div>

      <!-- 翻页器 -->
      <div class="hist-pager" v-if="total > PAGE_SIZE">
        <button class="pager-btn" :disabled="page <= 1" @click="goPage(1)">«</button>
        <button class="pager-btn" :disabled="page <= 1" @click="goPage(page - 1)">‹</button>
        <span class="pager-info">{{ page }} / {{ totalPages }}</span>
        <button class="pager-btn" :disabled="page >= totalPages" @click="goPage(page + 1)">›</button>
        <button class="pager-btn" :disabled="page >= totalPages" @click="goPage(totalPages)">»</button>
        <span class="pager-total muted">共 {{ total }} 条</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.deals { display: flex; flex-direction: column; gap: 10px; }

/* ── 统计条 ── */
.deals-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 18px;
  padding: 10px 12px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
}
.ds-item { display: flex; align-items: baseline; gap: 6px; }
.ds-k { font-size: 11px; color: var(--muted); }
.ds-v { font-size: 13px; font-weight: 700; }
.ds-v.pos { color: var(--green); }
.ds-v.neg { color: var(--red); }
.ds-updated { margin-left: auto; font-size: 11px; }

/* ── 筛选器 ── */
.deals-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.df-select, .df-input {
  height: 28px;
  padding: 0 8px;
  font-size: 12px;
  color: var(--text);
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  outline: none;
}
.df-input { width: 100px; }
.df-select:focus, .df-input:focus { border-color: var(--primary); }

/* ── 表格 ── */
.deals-table { min-width: 1000px; }
.deal-entry {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 600;
  background: var(--glass);
  color: var(--muted);
  border: 1px solid var(--glass-border);
}
.deal-entry.out { color: #fbbf24; border-color: rgba(251, 191, 36, 0.28); }
.deal-entry.inout { color: #a78bfa; border-color: rgba(167, 139, 250, 0.28); }
.deal-reason { margin-left: 6px; font-size: 11px; }
.deal-source { font-size: 11px; color: var(--muted); }

@media (max-width: 768px) {
  .ds-updated { margin-left: 0; width: 100%; }
}
</style>
