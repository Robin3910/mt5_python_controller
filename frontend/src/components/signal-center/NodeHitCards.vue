<script setup lang="ts">
// 节点命中卡：按节点展示信号数 / 成功率 / 时延分位 / 主要拦截原因。
//
// **时延必须分节点展示**：端到端（服务端钟）跨节点可比；
// 节点排队与券商往返是节点本地时钟的差值，只在同一节点内部有意义，
// 跨节点比较或求和都是错的 —— 所以这里每个节点各显示各的。
import { computed } from 'vue'
import type { SignalNodeHit } from '@/api/types'

const props = defineProps<{
  items: SignalNodeHit[]
  loading?: boolean
}>()

/** 按 owner 分组：同一个 owner 的节点聚在一张卡里 */
const groups = computed(() => {
  const map = new Map<string, SignalNodeHit[]>()
  for (const item of props.items) {
    // 用 node_id 前缀做粗略归属（节点命名形如 7-904093，前段是租户/分组号）
    const key = item.node_id.split('-')[0] || '其它'
    const list = map.get(key)
    if (list) list.push(item)
    else map.set(key, [item])
  }
  return Array.from(map.entries())
    .map(([key, list]) => ({
      key,
      title: key,
      items: [...list].sort((a, b) => b.signal_count - a.signal_count),
      total: list.reduce((s, i) => s + i.signal_count, 0),
    }))
    .sort((a, b) => b.total - a.total)
})

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(1)}%`
}

function rateClass(v: number): string {
  if (v >= 95) return 'ok'
  if (v >= 80) return 'warn'
  return 'fail'
}
</script>

<template>
  <div>
    <template v-if="groups.length">
      <div
        v-for="g in groups"
        :key="g.key"
        class="pf-owner-group"
        style="margin-bottom: 12px"
      >
        <div class="pf-owner-head">
          <div class="pf-owner-name">
            <span class="pf-owner-avatar">{{ g.title.slice(0, 1) }}</span>
            <h4 class="pf-owner-title">{{ g.title }}</h4>
          </div>
          <div class="pf-owner-meta">
            <span>{{ g.items.length }} 个节点</span>
            <span>{{ g.total }} 次命中</span>
          </div>
        </div>

        <div class="pf-owner-body">
          <div
            v-for="n in g.items"
            :key="n.node_id"
            class="sc-node-row"
          >
            <div class="sc-node-main">
              <div class="sc-node-id">
                <span class="c-strong">{{ n.node_name || n.node_id }}</span>
                <span class="c-dim">{{ n.node_id }}</span>
              </div>
              <div class="sc-node-tags">
                <span class="pf-pill green">成功 {{ n.done }}</span>
                <span v-if="n.skipped" class="pf-pill amber">跳过 {{ n.skipped }}</span>
                <span v-if="n.failed" class="pf-pill red">失败 {{ n.failed }}</span>
                <span v-if="n.pending" class="pf-pill blue">在途 {{ n.pending }}</span>
                <span v-if="n.top_skip_label" class="pf-pill grey">
                  主因 {{ n.top_skip_label }}
                </span>
              </div>
            </div>
            <div class="sc-node-metrics">
              <div class="sc-node-metric">
                <span class="k">命中率</span>
                <span class="v" :class="rateClass(n.success_rate)">{{ fmtPct(n.success_rate) }}</span>
              </div>
              <div class="sc-node-metric">
                <span class="k">端到端 P50</span>
                <span class="v">{{ fmtMs(n.latency_p50_ms) }}</span>
              </div>
              <div class="sc-node-metric">
                <span class="k">端到端 P90</span>
                <span class="v">{{ fmtMs(n.latency_p90_ms) }}</span>
              </div>
              <div class="sc-node-metric">
                <span class="k">节点内排队 P50</span>
                <span class="v">{{ fmtMs(n.queued_p50_ms) }}</span>
              </div>
              <div class="sc-node-metric">
                <span class="k">券商往返 P50</span>
                <span class="v">{{ fmtMs(n.fill_p50_ms) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <div v-else-if="!loading" class="pf-card">
      <p class="pf-empty">窗口内没有节点命中记录</p>
    </div>
    <div v-else class="pf-card">
      <p class="pf-empty">加载中…</p>
    </div>

    <div class="pf-scope-note info" style="margin-top: 4px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
      <span>
        「端到端」用服务端时钟，跨节点可比；「节点内排队」「券商往返」是节点本地时钟的差值，
        只在同一个节点内部有意义，不同节点之间不可比较、不可求和。
      </span>
    </div>
  </div>
</template>

<style scoped>
.sc-node-row {
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  padding: 8px 10px;
}

.sc-node-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.sc-node-id {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  font-size: 12px;
}
.sc-node-id .c-dim { font-size: 10px; }

.sc-node-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.sc-node-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 10px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--pf-border-2);
}
@media (min-width: 900px) {
  .sc-node-metrics { grid-template-columns: repeat(5, minmax(0, 1fr)); }
}

.sc-node-metric {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
  font-size: 10px;
}
.sc-node-metric .k {
  color: var(--pf-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sc-node-metric .v {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--pf-text);
  white-space: nowrap;
}
.sc-node-metric .v.ok { color: var(--pf-green); }
.sc-node-metric .v.warn { color: #fbbf24; }
.sc-node-metric .v.fail { color: #f87171; }
</style>
