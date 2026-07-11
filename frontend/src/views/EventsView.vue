<script setup lang="ts">
// 事件页：Webhook 信号原始参数 + 各节点后续处理情况
import { computed, onMounted, ref, watch } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { SignalEventRecord } from '@/api/types'

const hub = useHubStore()

const items = ref<SignalEventRecord[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const expanded = ref<Record<string, boolean>>({})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

async function loadEvents(): Promise<void> {
  loading.value = true
  try {
    const res = await hub.fetchSignalEvents(page.value, pageSize.value)
    items.value = res.items
    total.value = res.total
    if (res.page !== page.value) page.value = res.page
  } finally {
    loading.value = false
  }
}

function goPage(next: number): void {
  const p = Math.min(Math.max(1, next), totalPages.value)
  if (p === page.value) return
  page.value = p
  loadEvents()
}

function onPageSizeChange(): void {
  page.value = 1
  loadEvents()
}

function toggleRow(signalId: string): void {
  expanded.value[signalId] = !expanded.value[signalId]
}

function isExpanded(signalId: string): boolean {
  return !!expanded.value[signalId]
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function fmtPayload(raw: string | null | undefined): string {
  if (!raw) return '—'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function signalTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    dispatching: { cls: 'blue', text: '分发中' },
    rejected: { cls: 'red', text: '已拒收' },
    duplicate: { cls: 'amber', text: '重复' },
    done: { cls: 'green', text: '完成' },
    partial: { cls: 'amber', text: '部分成功' },
    failed: { cls: 'red', text: '失败' },
  }
  return m[status] || { cls: '', text: status }
}

function dispatchTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '成功' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
  }
  return m[status] || { cls: 'blue', text: status }
}

function dispatchSummary(row: SignalEventRecord): string {
  const n = row.dispatches.length
  if (!n) return row.status === 'rejected' ? '全局拒收' : '无节点处理'
  const done = row.dispatches.filter((d) => d.status === 'done').length
  const skipped = row.dispatches.filter((d) => d.status === 'skipped').length
  const failed = row.dispatches.filter((d) => d.status === 'failed').length
  const parts: string[] = [`${n} 节点`]
  if (done) parts.push(`${done} 成功`)
  if (skipped) parts.push(`${skipped} 跳过`)
  if (failed) parts.push(`${failed} 失败`)
  return parts.join(' · ')
}

onMounted(loadEvents)

let refreshTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => hub.events.length,
  () => {
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(loadEvents, 1200)
  },
)
</script>

<template>
  <div class="events-page">
  <div class="page-header">
    <div class="h1">Webhook 事件</div>
    <p class="muted" style="font-size: 13px; margin-top: 4px">
      记录所有经 /webhook 接收的信号原始参数及各节点后续处理情况
    </p>
  </div>

  <div class="card card-pad events-panel">
    <div class="row between" style="margin-bottom: 12px">
      <span class="muted" style="font-size: 12px">共 {{ total }} 条 · 点击行展开详情</span>
      <div class="row" style="gap: 8px">
        <label class="row muted" style="font-size: 12px; gap: 6px">
          每页
          <select v-model.number="pageSize" class="input-sm" @change="onPageSizeChange">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
        </label>
        <button class="btn-sm btn-ghost" :disabled="loading" @click="loadEvents">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <template v-if="items.length">
    <div class="list-cards mobile-only">
      <div
        v-for="row in items"
        :key="row.signal_id"
        class="list-card card clickable"
        @click="toggleRow(row.signal_id)"
      >
        <div class="list-card-head row between">
          <strong>{{ row.symbol || '—' }}</strong>
          <span class="muted">{{ isExpanded(row.signal_id) ? '▾' : '▸' }}</span>
        </div>
        <div class="list-field">
          <span class="k">时间</span>
          <span class="v muted" style="font-size: 12px">{{ fmtTime(row.received_at) }}</span>
        </div>
        <div class="list-field">
          <span class="k">动作</span>
          <span class="v">
            <span
              v-if="row.action"
              class="tag"
              :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'blue' : ''"
            >{{ row.action }}</span>
            <span v-else class="muted">—</span>
          </span>
        </div>
        <div class="list-field">
          <span class="k">整体状态</span>
          <span class="v">
            <span class="tag" :class="signalTag(row.status).cls">{{ signalTag(row.status).text }}</span>
          </span>
        </div>
        <div class="list-field">
          <span class="k">节点处理</span>
          <span class="v muted" style="font-size: 12px">{{ dispatchSummary(row) }}</span>
        </div>
        <div v-if="isExpanded(row.signal_id)" class="list-card-detail">
          <div class="muted" style="font-size: 12px; margin-bottom: 6px">原始 Webhook 参数</div>
          <pre class="token-box" style="font-size: 12px; white-space: pre-wrap; margin-bottom: 12px">{{ fmtPayload(row.raw_payload) }}</pre>
          <div v-if="row.dispatches.length">
            <div v-for="d in row.dispatches" :key="d.id" class="list-card-nested" style="margin-bottom: 8px">
              <div class="list-field"><span class="k">节点</span><span class="v">{{ d.node_name || d.node_id }}</span></div>
              <div class="list-field">
                <span class="k">状态</span>
                <span class="v"><span class="tag" :class="dispatchTag(d.status).cls">{{ dispatchTag(d.status).text }}</span></span>
              </div>
              <div class="list-field"><span class="k">跳过原因</span><span class="v muted" style="font-size: 12px">{{ d.skip_reason || '—' }}</span></div>
            </div>
          </div>
          <div v-else class="muted" style="font-size: 13px">该信号未产生节点分发明细。</div>
        </div>
      </div>
    </div>

    <div class="events-table-wrap desktop-only">
    <table class="events-table">
      <thead>
        <tr>
          <th class="col-expand"></th>
          <th class="col-time">时间</th>
          <th class="col-action">动作</th>
          <th class="col-symbol">品种</th>
          <th class="col-volume right">手数</th>
          <th class="col-parse">解析</th>
          <th class="col-status">整体状态</th>
          <th class="col-summary">节点处理</th>
          <th class="col-ip">来源 IP</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in items" :key="row.signal_id">
          <tr class="clickable" @click="toggleRow(row.signal_id)">
            <td class="muted col-expand">{{ isExpanded(row.signal_id) ? '▾' : '▸' }}</td>
            <td class="muted col-time">{{ fmtTime(row.received_at) }}</td>
            <td class="col-action">
              <span
                v-if="row.action"
                class="tag"
                :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'blue' : ''"
              >{{ row.action }}</span>
              <span v-else class="muted">—</span>
            </td>
            <td class="col-symbol">{{ row.symbol || '—' }}</td>
            <td class="col-volume right">{{ row.volume ?? '—' }}</td>
            <td class="col-parse">
              <span class="tag" :class="row.parsed_ok ? 'green' : 'red'">{{ row.parsed_ok ? '成功' : '失败' }}</span>
            </td>
            <td class="col-status">
              <span class="tag" :class="signalTag(row.status).cls">{{ signalTag(row.status).text }}</span>
            </td>
            <td class="muted col-summary">{{ dispatchSummary(row) }}</td>
            <td class="muted col-ip">{{ row.source_ip || '—' }}</td>
          </tr>
          <tr v-if="isExpanded(row.signal_id)" class="detail-row">
            <td colspan="9">
              <div class="events-detail">
                <div class="grid cols-2 events-detail-kv" style="gap: 12px; margin-bottom: 12px">
                  <div class="kv"><span class="k">信号 ID</span><span class="v events-break">{{ row.signal_id }}</span></div>
                  <div class="kv"><span class="k">分发模式</span><span class="v">{{ row.dispatch_mode || '—' }}</span></div>
                  <div class="kv"><span class="k">SL</span><span class="v">{{ row.sl ?? '—' }}</span></div>
                  <div class="kv"><span class="k">TP</span><span class="v">{{ row.tp ?? '—' }}</span></div>
                  <div class="kv"><span class="k">备注</span><span class="v events-break">{{ row.comment || '—' }}</span></div>
                </div>
                <div class="muted" style="font-size: 12px; margin-bottom: 6px">原始 Webhook 参数</div>
                <pre class="token-box events-payload">{{ fmtPayload(row.raw_payload) }}</pre>
                <div class="muted" style="font-size: 12px; margin-bottom: 8px">各节点处理情况</div>
                <div v-if="row.dispatches.length" class="events-detail-wrap">
                <table class="events-detail-table">
                  <thead>
                    <tr>
                      <th>节点</th><th>状态</th><th>闸门</th><th class="right">决策手数</th>
                      <th>跳过原因</th><th>订单</th><th>错误</th><th>下发时间</th><th>完成时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="d in row.dispatches" :key="d.id">
                      <td>{{ d.node_name || d.node_id }}</td>
                      <td><span class="tag" :class="dispatchTag(d.status).cls">{{ dispatchTag(d.status).text }}</span></td>
                      <td>{{ d.gate_result === 'skipped' ? '被过滤' : '通过' }}</td>
                      <td class="right">{{ d.decided_vol ?? '—' }}</td>
                      <td class="muted events-break">{{ d.skip_reason || '—' }}</td>
                      <td>{{ d.order ?? '—' }}</td>
                      <td class="muted events-break">{{ d.error || '—' }}</td>
                      <td class="muted col-time">{{ fmtTime(d.dispatched_at) }}</td>
                      <td class="muted col-time">{{ fmtTime(d.finished_at) }}</td>
                    </tr>
                  </tbody>
                </table>
                </div>
                <div v-else class="muted" style="font-size: 13px">该信号未产生节点分发明细。</div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    </div>
    </template>

    <div v-else-if="!loading" class="muted" style="font-size: 13px; padding: 8px 0">暂无 Webhook 信号记录。</div>
    <div v-else class="muted" style="font-size: 13px; padding: 8px 0">加载中…</div>

    <div v-if="totalPages > 1" class="row between" style="margin-top: 16px">
      <span class="muted" style="font-size: 12px">第 {{ page }} / {{ totalPages }} 页</span>
      <div class="row" style="gap: 8px">
        <button class="btn-sm btn-ghost" :disabled="page <= 1 || loading" @click="goPage(page - 1)">上一页</button>
        <button class="btn-sm btn-ghost" :disabled="page >= totalPages || loading" @click="goPage(page + 1)">下一页</button>
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
.events-page {
  width: 100%;
  min-width: 0;
}

.events-panel {
  width: 100%;
  min-width: 0;
}

.events-table-wrap {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.events-table,
.events-detail-table {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.events-table th,
.events-table td,
.events-detail-table th,
.events-detail-table td {
  white-space: normal;
  word-break: break-word;
  overflow-wrap: anywhere;
  vertical-align: top;
  font-size: 12px;
}

.events-table .col-expand { width: 28px; white-space: nowrap; }
.events-table .col-time { width: 11%; white-space: nowrap; }
.events-table .col-action { width: 7%; white-space: nowrap; }
.events-table .col-symbol { width: 9%; }
.events-table .col-volume { width: 6%; white-space: nowrap; }
.events-table .col-parse { width: 7%; white-space: nowrap; }
.events-table .col-status { width: 8%; white-space: nowrap; }
.events-table .col-summary { width: auto; }
.events-table .col-ip { width: 10%; }

.events-detail {
  padding: 8px 0 4px;
  min-width: 0;
}

.events-detail-kv {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
}

.events-detail-wrap {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.events-detail-table .col-time {
  white-space: nowrap;
}

.events-payload {
  width: 100%;
  max-width: 100%;
  margin-bottom: 16px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
}

.events-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}

@media (max-width: 1100px) {
  .events-table .col-time { width: 14%; }
  .events-table .col-ip { width: 12%; }
}

@media (max-width: 900px) {
  .events-table,
  .events-detail-table {
    table-layout: auto;
  }
}
</style>
