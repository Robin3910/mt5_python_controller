<script setup lang="ts">
// 统一信号流：normal + strategy 混排，游标分页，点击行展开分发明细。
//
// **游标分页而不是页码分页**：信号是持续写入的，页码分页在翻页过程中
// 会有新信号插到前面，导致「翻到第 2 页发现有条信号在第 1 页也见过」。
// 游标分页以「接收时间 + 信号号」为锚点，翻页期间有新增也不会错位或重复。
import { ref } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { SignalCenterDetail, SignalStreamItem } from '@/api/types'

defineProps<{
  items: SignalStreamItem[]
  loading?: boolean
  hasMore?: boolean
}>()

const emit = defineEmits<{
  (e: 'loadMore'): void
}>()

const hub = useHubStore()

const expanded = ref<Record<string, boolean>>({})
/** 展开时才拉明细（含原始载荷），避免列表页一次带 20 份 JSON */
const details = ref<Record<string, SignalCenterDetail | null>>({})
const detailLoading = ref<Record<string, boolean>>({})

async function toggle(signalId: string): Promise<void> {
  const nowOpen = !expanded.value[signalId]
  expanded.value[signalId] = nowOpen
  if (!nowOpen) return
  if (signalId in details.value) return
  detailLoading.value[signalId] = true
  try {
    details.value[signalId] = await hub.fetchSignalCenterDetail(signalId)
  } finally {
    detailLoading.value[signalId] = false
  }
}

function isExpanded(signalId: string): boolean {
  return !!expanded.value[signalId]
}

function detailOf(signalId: string): SignalCenterDetail | null {
  return details.value[signalId] ?? null
}

function fmtPayload(raw: string | null | undefined): string {
  if (!raw) return '—'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function statusTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '完成' },
    partial: { cls: 'amber', text: '部分成功' },
    dispatching: { cls: 'blue', text: '分发中' },
    pending: { cls: 'blue', text: '等待' },
    failed: { cls: 'red', text: '失败' },
    rejected: { cls: 'red', text: '已拒收' },
  }
  return m[status] ?? { cls: '', text: status }
}

function sourceTag(source: string): { cls: string; text: string } {
  if (source === 'manual') return { cls: 'amber', text: '手动' }
  if (source === 'limit_watch') return { cls: 'blue', text: '限价' }
  return { cls: '', text: 'TradingView' }
}

function modelTag(model: string): { cls: string; text: string } {
  return model === 'strategy'
    ? { cls: 'blue', text: '分组' }
    : { cls: '', text: '币种' }
}

function dispatchTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '成功' },
    opened: { cls: 'green', text: '已开仓' },
    running: { cls: 'green', text: '运行中' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
  }
  return m[status] ?? { cls: 'blue', text: status }
}
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 10px">
      <h3 class="pf-section-title"><span class="pf-bar" />统一信号流</h3>
      <span class="pf-count">{{ items.length }} 条已加载</span>
    </div>

    <template v-if="items.length">
      <div class="pf-rows">
        <div class="pf-rows-inner">
          <div class="pf-row pf-row-head">
            <span></span>
            <span class="c-muted">时间</span>
            <span class="c-muted">品种</span>
            <span class="c-muted">动作</span>
            <span class="c-muted">来源</span>
            <span class="c-muted">链路</span>
            <span class="c-muted">状态</span>
            <span class="c-muted">节点处理</span>
            <span class="c-muted">端到端</span>
          </div>

          <template v-for="row in items" :key="row.signal_id">
            <div class="pf-row clickable" @click="toggle(row.signal_id)">
              <span class="c-dim">{{ isExpanded(row.signal_id) ? '▾' : '▸' }}</span>
              <span class="c-muted">{{ fmtTime(row.received_at) }}</span>
              <span class="c-strong">{{ row.symbol || '—' }}</span>
              <span>
                <span
                  v-if="row.action"
                  class="pf-pill"
                  :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'red' : 'grey'"
                >{{ row.action }}</span>
                <span v-else class="c-dim">—</span>
              </span>
              <span><span class="pf-pill" :class="sourceTag(row.source).cls">{{ sourceTag(row.source).text }}</span></span>
              <span><span class="pf-pill" :class="modelTag(row.model).cls">{{ modelTag(row.model).text }}</span></span>
              <span><span class="pf-pill" :class="statusTag(row.status).cls">{{ statusTag(row.status).text }}</span></span>
              <span class="c-muted">
                {{ row.done }}/{{ row.targets }}
                <template v-if="row.skipped">· 跳 {{ row.skipped }}</template>
                <template v-if="row.failed">· 败 {{ row.failed }}</template>
              </span>
              <span class="c-mono">{{ fmtMs(row.elapsed_ms) }}</span>
            </div>

            <!-- 展开：分发明细 -->
            <div v-if="isExpanded(row.signal_id)" class="sc-detail">
              <div class="sc-detail-head">
                <span class="c-muted">信号号</span>
                <span class="c-mono">{{ row.signal_id }}</span>
                <span v-if="row.reject_code" class="pf-pill red">
                  {{ row.reject_label || row.reject_code }}
                </span>
                <span class="c-muted">手数 {{ row.volume ?? '—' }}</span>
              </div>

              <p v-if="detailLoading[row.signal_id]" class="c-dim" style="font-size: 11px">
                明细加载中…
              </p>

              <template v-else-if="detailOf(row.signal_id)">
                <div class="sc-detail-kv">
                  <span>品种 <b>{{ detailOf(row.signal_id)!.symbol || '—' }}</b></span>
                  <span>SL <b>{{ detailOf(row.signal_id)!.sl ?? '—' }}</b></span>
                  <span>TP <b>{{ detailOf(row.signal_id)!.tp ?? '—' }}</b></span>
                  <span>来源 IP <b class="c-mono">{{ detailOf(row.signal_id)!.source_ip || '—' }}</b></span>
                  <span>备注 <b>{{ detailOf(row.signal_id)!.comment || '—' }}</b></span>
                </div>

                <!-- 节点分发明细（含两段节点内时延） -->
                <table
                  v-if="detailOf(row.signal_id)!.dispatches.length"
                  class="sc-dispatch-table"
                >
                  <thead>
                    <tr>
                      <th>节点</th>
                      <th>状态</th>
                      <th class="right">手数</th>
                      <th>拦截原因</th>
                      <th class="right">节点排队</th>
                      <th class="right">券商往返</th>
                      <th>错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(d, i) in detailOf(row.signal_id)!.dispatches"
                      :key="`${d.node_id}-${i}`"
                    >
                      <td class="c-mono">{{ d.node_id }}</td>
                      <td>
                        <span class="pf-pill" :class="dispatchTag(d.status).cls">
                          {{ dispatchTag(d.status).text }}
                        </span>
                      </td>
                      <td class="right">{{ d.decided_vol ?? '—' }}</td>
                      <td class="c-muted">
                        <template v-if="d.skip_label">
                          <span class="pf-pill amber">{{ d.skip_label }}</span>
                          <span v-if="d.skip_reason" class="sc-skip-reason"> {{ d.skip_reason }}</span>
                        </template>
                        <template v-else>—</template>
                      </td>
                      <td class="right c-muted">{{ fmtMs(d.queued_ms) }}</td>
                      <td class="right c-muted">{{ fmtMs(d.order_to_fill_ms) }}</td>
                      <td class="c-muted sc-break">{{ d.error || '—' }}</td>
                    </tr>
                  </tbody>
                </table>

                <!-- strategy 链路：主任务 -->
                <table
                  v-if="detailOf(row.signal_id)!.group_tasks.length"
                  class="sc-dispatch-table"
                  style="margin-top: 8px"
                >
                  <thead>
                    <tr>
                      <th>分组</th>
                      <th>策略</th>
                      <th>状态</th>
                      <th class="right">节点</th>
                      <th class="right">下单</th>
                      <th class="right">已实现盈亏</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="t in detailOf(row.signal_id)!.group_tasks" :key="t.task_id">
                      <td>{{ t.group_name || t.group_id }}</td>
                      <td class="c-muted">{{ t.strategy_name || '—' }}</td>
                      <td>
                        <span class="pf-pill" :class="dispatchTag(t.status).cls">
                          {{ dispatchTag(t.status).text }}
                        </span>
                        <span v-if="t.skip_label" class="pf-pill amber" style="margin-left: 4px">
                          {{ t.skip_label }}
                        </span>
                      </td>
                      <td class="right">{{ t.node_count }}</td>
                      <td class="right">{{ t.total_orders }} 笔 / {{ t.total_volume }} 手</td>
                      <td class="right">{{ t.realized_profit }}</td>
                    </tr>
                  </tbody>
                </table>

                <!-- 原始 Webhook 载荷 -->
                <div class="c-muted" style="font-size: 11px; margin: 10px 0 4px">
                  原始 Webhook 参数
                </div>
                <pre class="sc-payload">{{ fmtPayload(detailOf(row.signal_id)!.raw_payload) }}</pre>
              </template>

              <table v-else-if="row.dispatches.length" class="sc-dispatch-table">
                <thead>
                  <tr>
                    <th>节点</th>
                    <th>状态</th>
                    <th class="right">手数</th>
                    <th>拦截原因</th>
                    <th>错误</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(d, i) in row.dispatches" :key="`${d.node_id}-${i}`">
                    <td class="c-mono">{{ d.node_id }}</td>
                    <td>
                      <span class="pf-pill" :class="dispatchTag(d.status).cls">
                        {{ dispatchTag(d.status).text }}
                      </span>
                    </td>
                    <td class="right">{{ d.decided_vol ?? '—' }}</td>
                    <td class="c-muted">
                      <template v-if="d.skip_label">
                        <span class="pf-pill amber">{{ d.skip_label }}</span>
                        <span v-if="d.skip_reason" class="sc-skip-reason"> {{ d.skip_reason }}</span>
                      </template>
                      <template v-else>—</template>
                    </td>
                    <td class="c-muted sc-break">{{ d.error || '—' }}</td>
                  </tr>
                </tbody>
              </table>

              <p v-else class="c-dim" style="font-size: 11px">
                {{ row.status === 'rejected' ? '该信号在受理阶段被拒收，未产生节点明细。' : '该信号未产生节点分发明细。' }}
              </p>
            </div>
          </template>
        </div>
      </div>

      <div class="row between" style="margin-top: 12px">
        <span class="muted" style="font-size: 11px">游标分页：向下滚动加载不会重复或漏掉</span>
        <button v-if="hasMore" class="pf-btn pf-btn-mini" :disabled="loading" @click="emit('loadMore')">
          {{ loading ? '加载中…' : '加载更多' }}
        </button>
      </div>
    </template>

    <p v-else-if="!loading" class="pf-empty">窗口内没有信号记录</p>
    <p v-else class="pf-empty">加载中…</p>
  </div>
</template>

<style scoped>
.clickable { cursor: pointer; }

.sc-detail {
  border-bottom: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  padding: 8px 10px;
  min-width: 0;
}

.sc-detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
  font-size: 11px;
}

.sc-dispatch-table {
  width: 100%;
  border-collapse: collapse;
}
.sc-dispatch-table th,
.sc-dispatch-table td {
  padding: 5px 6px;
  font-size: 11px;
  text-align: left;
  border-bottom: 1px solid var(--pf-border-2);
  vertical-align: top;
}
.sc-dispatch-table th {
  color: var(--pf-dim);
  font-weight: 500;
  font-size: 10px;
}
.sc-dispatch-table .right { text-align: right; }
.sc-dispatch-table tr:last-child td { border-bottom: none; }

.sc-skip-reason {
  font-size: 10px;
  color: var(--pf-faint);
}

.sc-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}

.sc-detail-kv {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-bottom: 8px;
  font-size: 11px;
  color: var(--pf-dim);
}
.sc-detail-kv b {
  color: var(--pf-text);
  font-weight: 500;
}

.sc-payload {
  margin: 0;
  padding: 8px;
  border-radius: 8px;
  background: #0a0f1a;
  border: 1px solid var(--pf-border-2);
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.6;
  color: #b8c4d9;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 260px;
  overflow: auto;
}
</style>
