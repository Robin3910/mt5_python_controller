<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import type { AccountSnapshot, NodeOut } from '@/api/types'
import { parseNodeDispatchFilters } from '@/utils/filterRules'

const NodePerformancePanel = defineAsyncComponent(() => import('@/components/NodePerformancePanel.vue'))

const props = defineProps<{
  node: NodeOut
  acct?: AccountSnapshot
  showPerformance?: boolean
  showConfig?: boolean
}>()

const nodeDispatchFilters = computed(() => parseNodeDispatchFilters(props.node.filters))
const nodeDispatchSymbols = computed(() => Object.keys(nodeDispatchFilters.value).sort())

function fmt(n: number | undefined | null): string {
  return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function fmtTime(ms: number | undefined | null): string {
  return ms ? new Date(ms).toLocaleString() : '—'
}
</script>

<template>
  <div>
    <div class="card card-pad" style="margin-bottom: 16px">
      <strong>账户</strong>
      <div v-if="acct" class="kv-grid kv-grid-metrics" style="margin-top: 12px">
        <div class="kv"><span class="k">余额</span><span class="v">{{ fmt(acct.balance) }}</span></div>
        <div class="kv"><span class="k">净值</span><span class="v">{{ fmt(acct.equity) }}</span></div>
        <div class="kv"><span class="k">占用保证金</span><span class="v">{{ fmt(acct.margin) }}</span></div>
        <div class="kv"><span class="k">可用保证金</span><span class="v">{{ fmt(acct.free_margin) }}</span></div>
        <div class="kv"><span class="k">杠杆</span><span class="v">{{ acct.leverage || '—' }}</span></div>
        <div class="kv"><span class="k">最近上报</span><span class="v" style="font-size: 12px">{{ fmtTime((acct.updated_at || 0) * 1000) }}</span></div>
      </div>
      <div v-else class="muted" style="font-size: 12px; margin-top: 8px">暂无账户快照（节点未上线或尚未上报）。</div>
    </div>

    <div v-if="showPerformance" class="card card-pad" style="margin-bottom: 16px">
      <NodePerformancePanel :node-id="node.node_id" />
    </div>

    <div v-if="showConfig" class="card card-pad">
      <strong>节点配置</strong>
      <div class="kv-grid" style="margin-top: 12px">
        <div class="kv"><span class="k">启用</span><span class="v">{{ node.enabled ? '是' : '否' }}</span></div>
        <div class="kv"><span class="k">创建时间</span><span class="v" style="font-size: 12px">{{ fmtTime((node.created_at || 0) * 1000) }}</span></div>
      </div>
      <div style="margin-top: 14px">
        <strong style="font-size: 13px">按币种配置</strong>
        <div v-if="!nodeDispatchSymbols.length" class="muted" style="font-size: 12px; margin-top: 8px">
          未单独配置（回退默认：固定手数 0.01、轮询序 0、全部参与同步与轮询）
        </div>
        <div v-else class="kv-grid" style="margin-top: 8px">
          <div v-for="sym in nodeDispatchSymbols" :key="sym" class="kv span-full">
            <span class="k">{{ sym }}</span>
            <span class="v" style="display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end">
              <span class="tag" :class="nodeDispatchFilters[sym]?.follow_sync ? 'green' : ''">同步</span>
              <span class="tag" :class="nodeDispatchFilters[sym]?.follow_poll ? 'green' : ''">轮询</span>
              <span class="tag blue">{{ nodeDispatchFilters[sym]?.lot_mode }}</span>
              <span v-if="nodeDispatchFilters[sym]?.lot_mode === 'fixed'" class="muted" style="font-size: 12px">
                {{ nodeDispatchFilters[sym]?.lot }}
              </span>
              <span class="muted" style="font-size: 12px">序 {{ nodeDispatchFilters[sym]?.poll_order ?? 0 }}</span>
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
