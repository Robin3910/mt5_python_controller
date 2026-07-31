<script setup lang="ts">
// 分组信号页：展示某分组处理过的 strategy 主任务与各节点下发明细
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import type { GroupOut, GroupSignalTaskRecord, GroupTaskEventRecord } from '@/api/types'

const route = useRoute()
const router = useRouter()
const hub = useHubStore()

const groupId = computed(() => String(route.params.id))
const group = ref<GroupOut | null>(null)
const loadError = ref('')

const signals = ref<GroupSignalTaskRecord[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const expanded = ref<Record<string, boolean>>({})

/** 节点子任务展开：dispatch_id -> 关联订单事件 */
const expandedDispatch = ref<Record<string, boolean>>({})
const dispatchEvents = ref<Record<string, GroupTaskEventRecord[]>>({})
const loadingDispatchEvents = ref<Record<string, boolean>>({})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

async function loadGroup(): Promise<void> {
  loadError.value = ''
  const groups = await hub.listGroups()
  group.value = groups.find((g) => g.group_id === groupId.value) ?? null
  if (!group.value) loadError.value = '分组不存在或已被删除'
}

async function loadSignals(): Promise<void> {
  if (!group.value) {
    signals.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    const res = await hub.fetchGroupSignals(group.value.group_id, page.value, pageSize.value)
    signals.value = res.items
    total.value = res.total
    if (res.page !== page.value) page.value = res.page
  } finally {
    loading.value = false
  }
}

async function reload(): Promise<void> {
  page.value = 1
  expanded.value = {}
  expandedDispatch.value = {}
  dispatchEvents.value = {}
  loadingDispatchEvents.value = {}
  await loadGroup()
  await loadSignals()
}

function goPage(next: number): void {
  const p = Math.min(Math.max(1, next), totalPages.value)
  if (p === page.value) return
  page.value = p
  loadSignals()
}

function onPageSizeChange(): void {
  page.value = 1
  loadSignals()
}

function toggleRow(key: string | number): void {
  const k = String(key)
  expanded.value[k] = !expanded.value[k]
}

function isExpanded(key: string | number): boolean {
  return !!expanded.value[String(key)]
}

function isDispatchExpanded(dispatchId: number): boolean {
  return !!expandedDispatch.value[String(dispatchId)]
}

async function toggleDispatch(dispatchId: number): Promise<void> {
  const k = String(dispatchId)
  const next = !expandedDispatch.value[k]
  expandedDispatch.value[k] = next
  if (!next || !group.value) return
  if (dispatchEvents.value[k]) return
  loadingDispatchEvents.value[k] = true
  try {
    dispatchEvents.value[k] = await hub.fetchGroupDispatchEvents(group.value.group_id, dispatchId)
  } finally {
    loadingDispatchEvents.value[k] = false
  }
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function fmtPayload(raw: unknown): string {
  if (raw == null) return '—'
  if (typeof raw === 'string') {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }
  return JSON.stringify(raw, null, 2)
}

function taskTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    pending: { cls: 'blue', text: '待处理' },
    dispatching: { cls: 'blue', text: '分发中' },
    running: { cls: 'amber', text: '策略运行中' },
    done: { cls: 'green', text: '完成' },
    partial: { cls: 'amber', text: '部分成功' },
    failed: { cls: 'red', text: '失败' },
    skipped: { cls: '', text: '未下发' },
  }
  return m[status] || { cls: '', text: status }
}

function dispatchTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '完成' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
    opened: { cls: 'amber', text: '已开仓' },
    running: { cls: 'amber', text: '加仓监控中' },
    closing: { cls: 'amber', text: '平仓中' },
  }
  return m[status] || { cls: 'blue', text: status }
}

/** 任务是否仍在跑（用于提示分组此时不接收新信号） */
function isTaskActive(status: string): boolean {
  return ['pending', 'dispatching', 'running'].includes(status)
}

function dispatchSummary(row: GroupSignalTaskRecord): string {
  const n = row.dispatches.length
  if (!n) return row.skip_reason || '无节点处理'
  const done = row.dispatches.filter((d) => d.status === 'done').length
  const failed = row.dispatches.filter((d) => d.status === 'failed' || d.status === 'offline').length
  const running = row.dispatches.filter((d) =>
    ['opened', 'running', 'closing'].includes(d.status),
  ).length
  const parts: string[] = [`${n} 节点`]
  if (running) parts.push(`${running} 运行中`)
  if (done) parts.push(`${done} 完成`)
  if (failed) parts.push(`${failed} 失败`)
  return parts.join(' · ')
}

function eventTag(eventType: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    open: { cls: 'green', text: '开仓' },
    add_counter: { cls: 'amber', text: '逆势加仓' },
    add_trend: { cls: 'amber', text: '顺势加仓' },
    close_partial: { cls: 'blue', text: '部分平仓' },
    close_all: { cls: 'blue', text: '全部平仓' },
    error: { cls: 'red', text: '异常' },
    resume: { cls: '', text: '恢复' },
  }
  return m[eventType] || { cls: '', text: eventType }
}

onMounted(reload)
watch(groupId, reload)
</script>

<template>
  <div class="group-signals-page">
    <a class="node-link" style="font-size: 13px" @click="router.push('/groups')">← 返回分组列表</a>

    <div class="row between page-header" style="margin-top: 12px">
      <div>
        <div class="h1">分组信号{{ group ? ` · ${group.name}` : '' }}</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          <template v-if="group">
            共 {{ total }} 条主任务 · 点击行展开信号明细与各节点处理过程
          </template>
          <template v-else-if="loadError">{{ loadError }}</template>
          <template v-else>加载中…</template>
        </p>
      </div>
      <button class="btn-sm btn-ghost" :disabled="loading || !group" @click="loadSignals">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <div v-if="group" class="card card-pad">
      <div v-if="signals.length" class="table-scroll">
        <table class="group-signal-table">
          <thead>
            <tr>
              <th style="width: 22px"></th>
              <th>时间</th><th>任务号</th><th>动作</th><th>品种</th>
              <th class="right">手数</th><th>分发模式</th><th>任务状态</th><th>节点处理</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="t in signals" :key="t.task_id">
              <tr class="clickable" @click="toggleRow(t.task_id)">
                <td class="muted">{{ isExpanded(t.task_id) ? '▾' : '▸' }}</td>
                <td class="muted" style="font-size: 12px">{{ fmtTime(t.created_at) }}</td>
                <td>#{{ t.task_id }}</td>
                <td>
                  <span
                    v-if="t.action"
                    class="tag"
                    :class="t.action === 'BUY' ? 'green' : t.action === 'SELL' ? 'blue' : ''"
                  >{{ t.action }}</span>
                  <span v-else class="muted">—</span>
                </td>
                <td>{{ t.symbol || '—' }}</td>
                <td class="right">{{ t.volume ?? '—' }}</td>
                <td class="muted" style="font-size: 12px">
                  {{ t.dispatch_mode === 'poll' ? '轮询轮转' : '全员同步' }}
                </td>
                <td><span class="tag" :class="taskTag(t.status).cls">{{ taskTag(t.status).text }}</span></td>
                <td class="muted" style="font-size: 12px">{{ dispatchSummary(t) }}</td>
              </tr>
              <tr v-if="isExpanded(t.task_id)" class="detail-row">
                <td></td>
                <td colspan="8">
                  <div class="kv-grid" style="margin: 6px 0 10px">
                    <div class="kv"><span class="k">信号 ID</span><span class="v" style="font-size: 12px">{{ t.signal_id }}</span></div>
                    <div class="kv"><span class="k">绑定策略</span><span class="v" style="font-size: 12px">{{ t.strategy_name || '—' }}</span></div>
                    <div class="kv"><span class="k">来源 IP</span><span class="v" style="font-size: 12px">{{ t.source_ip || '—' }}</span></div>
                    <div class="kv"><span class="k">SL</span><span class="v">{{ t.sl ?? '—' }}</span></div>
                    <div class="kv"><span class="k">TP</span><span class="v">{{ t.tp ?? '—' }}</span></div>
                    <div class="kv"><span class="k">备注</span><span class="v" style="font-size: 12px">{{ t.comment || '—' }}</span></div>
                    <div class="kv"><span class="k">下发节点数</span><span class="v">{{ t.node_count }}</span></div>
                    <div class="kv"><span class="k">累计下单</span><span class="v">{{ t.total_orders }} 笔 / {{ t.total_volume }} 手</span></div>
                    <div class="kv"><span class="k">已实现盈亏</span><span class="v">{{ t.realized_profit }}</span></div>
                    <div class="kv"><span class="k">开仓时间</span><span class="v" style="font-size: 12px">{{ fmtTime(t.opened_at) }}</span></div>
                    <div class="kv"><span class="k">完成时间</span><span class="v" style="font-size: 12px">{{ fmtTime(t.finished_at) }}</span></div>
                    <div v-if="t.skip_reason" class="kv span-full">
                      <span class="k">未下发原因</span><span class="v" style="font-size: 12px">{{ t.skip_reason }}</span>
                    </div>
                    <div v-if="isTaskActive(t.status)" class="kv span-full">
                      <span class="k">提示</span>
                      <span class="v" style="font-size: 12px">
                        任务进行中，下方运行中的节点该品种暂不接收新的策略信号；发送 CLOSE 信号可终止
                      </span>
                    </div>
                  </div>

                  <details class="payload-fold">
                    <summary>原始信号</summary>
                    <pre class="token-box group-payload">{{ fmtPayload(t.raw_payload) }}</pre>
                  </details>

                  <details class="payload-fold">
                    <summary>下发数据（发送给节点的命令）</summary>
                    <pre class="token-box group-payload">{{ fmtPayload(t.payload) }}</pre>
                  </details>

                  <div class="muted" style="font-size: 12px; margin-bottom: 8px">
                    各节点处理情况 · 点击节点行展开本次策略任务关联订单
                  </div>
                  <div v-if="t.dispatches.length" class="table-scroll">
                    <table class="group-detail-table">
                      <thead>
                        <tr>
                          <th style="width: 22px"></th>
                          <th>节点</th><th>魔术号</th><th>状态</th><th class="right">首单手数</th>
                          <th class="right">持仓</th><th class="right">加仓</th><th class="right">累计手数</th>
                          <th class="right">盈亏</th><th>结束原因</th>
                          <th>订单</th><th class="right">成交价</th>
                          <th>错误</th><th>下发时间</th><th>完成时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        <template v-for="d in t.dispatches" :key="d.id">
                          <tr class="clickable" @click="toggleDispatch(d.id)">
                            <td class="muted">{{ isDispatchExpanded(d.id) ? '▾' : '▸' }}</td>
                            <td>{{ d.node_name || d.node_id }}</td>
                            <td class="muted" style="font-size: 12px">{{ d.magic ?? '—' }}</td>
                            <td><span class="tag" :class="dispatchTag(d.status).cls">{{ dispatchTag(d.status).text }}</span></td>
                            <td class="right">{{ d.decided_vol ?? '—' }}</td>
                            <td class="right">{{ d.position_count }}</td>
                            <td class="right">{{ d.add_count }}</td>
                            <td class="right">{{ d.total_volume }}</td>
                            <td class="right">{{ d.realized_profit }}</td>
                            <td class="muted group-break">{{ d.finish_reason || d.skip_reason || '—' }}</td>
                            <td>{{ d.order ?? '—' }}</td>
                            <td class="right">{{ d.price ?? '—' }}</td>
                            <td class="muted group-break">{{ d.error || '—' }}</td>
                            <td class="muted" style="white-space: nowrap">{{ fmtTime(d.dispatched_at) }}</td>
                            <td class="muted" style="white-space: nowrap">{{ fmtTime(d.finished_at) }}</td>
                          </tr>
                          <tr v-if="isDispatchExpanded(d.id)" class="detail-row">
                            <td></td>
                            <td colspan="14">
                              <div class="muted" style="font-size: 12px; margin-bottom: 6px">
                                关联订单 · {{ d.node_name || d.node_id }}
                                <template v-if="d.magic != null"> · 魔术号 {{ d.magic }}</template>
                              </div>
                              <div v-if="loadingDispatchEvents[String(d.id)]" class="muted" style="font-size: 13px">
                                加载中…
                              </div>
                              <div
                                v-else-if="(dispatchEvents[String(d.id)] || []).length"
                                class="table-scroll"
                              >
                                <table class="group-event-table">
                                  <thead>
                                    <tr>
                                      <th>时间</th>
                                      <th>类型</th>
                                      <th>动作</th>
                                      <th class="right">手数</th>
                                      <th class="right">成交价</th>
                                      <th>订单号</th>
                                      <th class="right">持仓</th>
                                      <th class="right">累计手数</th>
                                      <th class="right">盈亏</th>
                                      <th>说明</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    <tr v-for="ev in dispatchEvents[String(d.id)]" :key="`${d.id}-${ev.id}-${ev.created_at}`">
                                      <td class="muted" style="white-space: nowrap">{{ fmtTime(ev.created_at) }}</td>
                                      <td>
                                        <span class="tag" :class="eventTag(ev.event_type).cls">
                                          {{ eventTag(ev.event_type).text }}
                                        </span>
                                      </td>
                                      <td>{{ ev.action || '—' }}</td>
                                      <td class="right">{{ ev.volume ?? '—' }}</td>
                                      <td class="right">{{ ev.price ?? '—' }}</td>
                                      <td>{{ ev.order_ticket ?? '—' }}</td>
                                      <td class="right">{{ ev.position_count ?? '—' }}</td>
                                      <td class="right">{{ ev.total_volume ?? '—' }}</td>
                                      <td class="right">{{ ev.profit ?? '—' }}</td>
                                      <td class="muted group-break">{{ ev.message || '—' }}</td>
                                    </tr>
                                  </tbody>
                                </table>
                              </div>
                              <div v-else class="muted" style="font-size: 13px">
                                该节点本次策略任务暂无关联订单记录。
                              </div>
                            </td>
                          </tr>
                        </template>
                      </tbody>
                    </table>
                  </div>
                  <div v-else class="muted" style="font-size: 13px">该主任务未产生节点下发明细。</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <div v-else-if="loading" class="muted" style="font-size: 13px; padding: 8px 0">加载中…</div>
      <div v-else class="muted" style="font-size: 13px; padding: 8px 0">该分组暂无信号记录。</div>

      <div v-if="total > 0" class="pagination" style="margin-top: 12px">
        <span class="muted pagination-info">
          共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页
        </span>
        <div class="row pagination-actions">
          <select v-model.number="pageSize" class="pagination-size" @change="onPageSizeChange">
            <option :value="10">10 条/页</option>
            <option :value="20">20 条/页</option>
            <option :value="50">50 条/页</option>
          </select>
          <button class="btn-sm btn-ghost" :disabled="loading || page <= 1" @click="goPage(page - 1)">
            上一页
          </button>
          <button
            class="btn-sm btn-ghost"
            :disabled="loading || page >= totalPages"
            @click="goPage(page + 1)"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.group-signals-page {
  width: 100%;
  min-width: 0;
}

.group-signal-table,
.group-detail-table,
.group-event-table {
  width: 100%;
  min-width: 0;
}

.group-signal-table th,
.group-signal-table td,
.group-detail-table th,
.group-detail-table td,
.group-event-table th,
.group-event-table td {
  font-size: 12px;
  vertical-align: top;
}

.group-event-table {
  margin-top: 2px;
  background: rgba(255, 255, 255, 0.02);
}

.group-payload {
  width: 100%;
  max-width: 100%;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
}

.payload-fold {
  margin-bottom: 10px;
}

.payload-fold > summary {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
  list-style: none;
}

.payload-fold > summary::-webkit-details-marker {
  display: none;
}

.payload-fold > summary::before {
  content: '▸';
  font-size: 11px;
}

.payload-fold[open] > summary::before {
  content: '▾';
}

.payload-fold > summary:hover {
  color: var(--text);
}

.payload-fold[open] > summary {
  margin-bottom: 6px;
}

.group-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}
</style>
