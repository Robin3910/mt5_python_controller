<script setup lang="ts">
// 策略任务面板（strategy 链路）：分组汇总 + 主任务列表（可展开看子任务）。
//
// **盈亏口径必须标注清楚**：
// `group_signal_task.realized_profit` 是「任务收口时节点上报的累计值」，
// 与成交侧的逐笔净盈亏（deal_stats 的 net_profit 汇总）**不是同一口径**——
// 两者可能不等，且绝不可加总。所以这一块单独成区，并显式写出来源说明，
// 不与成交侧盈亏混在一个数字里。
import { computed, ref } from 'vue'
import type { SignalCenterTasks } from '@/api/types'

const props = defineProps<{
  tasks: SignalCenterTasks | null
  loading?: boolean
}>()

const items = computed(() => props.tasks?.items ?? [])
const taskList = computed(() => props.tasks?.tasks ?? [])

/** 展开的主任务 */
const expanded = ref<Record<number, boolean>>({})

function toggle(taskId: number): void {
  expanded.value[taskId] = !expanded.value[taskId]
}

function isExpanded(taskId: number): boolean {
  return !!expanded.value[taskId]
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return `${sign}${Math.abs(v).toFixed(2)}`
}

function moneyClass(v: number): string {
  if (v > 0) return 'pos'
  if (v < 0) return 'neg'
  return 'flat'
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

/** 节点内耗时（毫秒）：小于 1s 显示毫秒，超过转秒更好读 */
function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待下发',
  dispatching: '分发中',
  sent: '已下发',
  opened: '已开仓',
  running: '运行中',
  closing: '平仓中',
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
  skipped: '已跳过',
  offline: '离线',
}

/** 状态药丸色 */
function statusClass(status: string): string {
  if (status === 'done' || status === 'opened') return 'green'
  if (status === 'partial' || status === 'skipped' || status === 'closing') return 'amber'
  if (status === 'failed' || status === 'offline') return 'red'
  if (status === 'running' || status === 'dispatching' || status === 'sent') return 'blue'
  return 'grey'
}

function statusText(status: string): string {
  return STATUS_LABELS[status] ?? status
}
</script>

<template>
  <div>
    <!-- 分组汇总表 -->
    <div class="pf-card" style="margin-bottom: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />分组任务汇总</h3>
        <span class="pf-count">
          {{ tasks?.totals.tasks ?? 0 }} 个任务 · {{ tasks?.totals.orders ?? 0 }} 笔下单
        </span>
      </div>

      <template v-if="items.length">
        <table class="sc-task-table">
          <thead>
            <tr>
              <th>分组</th>
              <th class="right">任务数</th>
              <th class="right">下单笔数</th>
              <th class="right">手数</th>
              <th class="right">已实现盈亏</th>
              <th>状态分布</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in items" :key="t.group_id">
              <td class="c-strong">{{ t.group_name }}</td>
              <td class="right">{{ t.tasks }}</td>
              <td class="right">{{ t.orders }}</td>
              <td class="right">{{ t.volume }}</td>
              <td class="right" :class="moneyClass(t.realized_profit)">
                {{ fmtMoney(t.realized_profit) }}
              </td>
              <td>
                <div class="sc-task-tags">
                  <span
                    v-for="(count, status) in t.statuses"
                    :key="status"
                    class="pf-pill"
                    :class="statusClass(status)"
                  >
                    {{ statusText(status) }} {{ count }}
                  </span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </template>
      <p v-else-if="!loading" class="pf-empty">窗口内没有策略任务</p>
      <p v-else class="pf-empty">加载中…</p>
    </div>

    <!-- 主任务列表（可展开看子任务） -->
    <div class="pf-card" style="margin-bottom: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />主任务明细</h3>
        <span class="pf-count">点击行展开节点子任务</span>
      </div>

      <template v-if="taskList.length">
        <div class="pf-rows">
          <div class="pf-rows-inner">
            <div class="pf-row pf-row-head">
              <span></span>
              <span class="c-muted">创建时间</span>
              <span class="c-muted">分组</span>
              <span class="c-muted">品种</span>
              <span class="c-muted">策略</span>
              <span class="c-muted">状态</span>
              <span class="c-muted">节点</span>
              <span class="c-muted">下单</span>
              <span class="c-muted">已实现盈亏</span>
            </div>

            <template v-for="t in taskList" :key="t.task_id">
              <div class="pf-row clickable" @click="toggle(t.task_id)">
                <span class="c-dim">{{ isExpanded(t.task_id) ? '▾' : '▸' }}</span>
                <span class="c-muted">{{ fmtTime(t.created_at) }}</span>
                <span class="c-strong">{{ t.group_name }}</span>
                <span>{{ t.symbol || '—' }}</span>
                <span class="c-muted">{{ t.strategy_name || '—' }}</span>
                <span>
                  <span class="pf-pill" :class="statusClass(t.status)">{{ statusText(t.status) }}</span>
                  <span v-if="t.skip_label" class="pf-pill amber" style="margin-left: 4px">
                    {{ t.skip_label }}
                  </span>
                </span>
                <span class="c-muted">{{ t.node_count }}</span>
                <span class="c-muted">{{ t.total_orders }} 笔 / {{ t.total_volume }} 手</span>
                <span class="c-mono" :class="moneyClass(t.realized_profit)">
                  {{ fmtMoney(t.realized_profit) }}
                </span>
              </div>

              <!-- 展开：节点子任务 -->
              <div v-if="isExpanded(t.task_id)" class="sc-detail">
                <div class="sc-detail-head">
                  <span class="c-muted">信号号</span>
                  <span class="c-mono">{{ t.signal_id }}</span>
                  <span v-if="t.skip_reason" class="c-muted">{{ t.skip_reason }}</span>
                </div>

                <table v-if="t.dispatches.length" class="sc-dispatch-table">
                  <thead>
                    <tr>
                      <th>节点</th>
                      <th>状态</th>
                      <th class="right">手数</th>
                      <th class="right">下单</th>
                      <th class="right">已实现盈亏</th>
                      <th>拦截 / 结束原因</th>
                      <th class="right">节点排队</th>
                      <th class="right">券商往返</th>
                      <th class="right">完成时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(d, i) in t.dispatches" :key="`${d.node_id}-${i}`">
                      <td class="c-mono">{{ d.node_id }}</td>
                      <td>
                        <span class="pf-pill" :class="statusClass(d.status)">
                          {{ statusText(d.status) }}
                        </span>
                      </td>
                      <td class="right">{{ d.decided_vol ?? '—' }}</td>
                      <td class="right">{{ d.total_orders }} 笔 / {{ d.total_volume }} 手</td>
                      <td class="right" :class="moneyClass(d.realized_profit)">
                        {{ fmtMoney(d.realized_profit) }}
                      </td>
                      <td class="c-muted sc-break">
                        <template v-if="d.skip_label">
                          <span class="pf-pill amber">{{ d.skip_label }}</span>
                        </template>
                        <span>{{ d.finish_reason || d.skip_reason || d.error || '—' }}</span>
                      </td>
                      <td class="right c-muted">{{ fmtMs(d.queued_ms) }}</td>
                      <td class="right c-muted">{{ fmtMs(d.order_to_fill_ms) }}</td>
                      <td class="right c-muted">{{ fmtTime(d.finished_at) }}</td>
                    </tr>
                  </tbody>
                </table>
                <p v-else class="c-dim" style="font-size: 11px">该任务没有节点子任务。</p>
              </div>
            </template>
          </div>
        </div>
      </template>
      <p v-else-if="!loading" class="pf-empty">窗口内没有策略主任务</p>
      <p v-else class="pf-empty">加载中…</p>
    </div>

    <!-- 合计（独立口径，单独成区） -->
    <div class="pf-card" style="margin-bottom: 12px">
      <div class="pf-section-head" style="margin-bottom: 10px">
        <h3 class="pf-section-title"><span class="pf-bar" />策略任务已实现盈亏（合计）</h3>
      </div>
      <div class="pf-metrics">
        <div class="pf-metric">
          <p class="pf-metric-k">任务数</p>
          <p class="pf-metric-v">{{ tasks?.totals.tasks ?? 0 }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">下单笔数</p>
          <p class="pf-metric-v">{{ tasks?.totals.orders ?? 0 }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">手数</p>
          <p class="pf-metric-v">{{ tasks?.totals.volume ?? 0 }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">已实现盈亏</p>
          <p class="pf-metric-v" :class="moneyClass(tasks?.totals.realized_profit ?? 0)">
            {{ fmtMoney(tasks?.totals.realized_profit) }}
          </p>
        </div>
      </div>
    </div>

    <!-- 口径提示：防止与成交侧净盈亏混读 -->
    <div class="pf-scope-note">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 9v4M12 17h.01" />
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>
        {{ tasks?.profit_scope }}
        —— 上面的盈亏<strong>只来自策略任务链路</strong>，
        成交侧的逐笔净盈亏请看「成交明细」页，两者分开看、不可相加。
      </span>
    </div>
  </div>
</template>

<style scoped>
.sc-task-table {
  width: 100%;
  border-collapse: collapse;
}
.sc-task-table th,
.sc-task-table td {
  padding: 6px;
  font-size: 12px;
  text-align: left;
  border-bottom: 1px solid var(--pf-border-2);
  vertical-align: top;
}
.sc-task-table th {
  color: var(--pf-dim);
  font-weight: 500;
  font-size: 10px;
}
.sc-task-table .right { text-align: right; }
.sc-task-table tr:last-child td { border-bottom: none; }

.sc-task-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

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

.sc-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}

.pos { color: var(--pf-green); }
.neg { color: #f87171; }
.flat { color: var(--pf-text); }
</style>
