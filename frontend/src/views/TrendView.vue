<script setup lang="ts">
// 管理后台顶栏「趋势面板」：跨节点查看趋势，复用节点详情里的 TrendPanel。
// 默认选中第一个在线节点、默认品种 XAUUSD；参数仍按所选节点落库。
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, NodeOut } from '@/api/types'
import { parseNodeDispatchFilters } from '@/utils/filterRules'

const TrendPanel = defineAsyncComponent(() => import('@/components/TrendPanel.vue'))

const DEFAULT_SYMBOL = 'XAUUSD'

const hub = useHubStore()
const selectedNodeId = ref('')
const loading = ref(true)
const nodeMenuOpen = ref(false)

function statusOf(n: NodeOut): 'online' | 'offline' {
  const s = hub.statuses[n.node_id] || n.status
  return s === 'online' ? 'online' : 'offline'
}

/** 默认第一个在线节点；全离线时退回列表首个，便于仍能看配置/离线提示 */
function pickDefaultNodeId(): string {
  const online = hub.nodes.find((n) => statusOf(n) === 'online')
  if (online) return online.node_id
  return hub.nodes[0]?.node_id || ''
}

const selectedNode = computed(() => hub.nodes.find((n) => n.node_id === selectedNodeId.value))
const online = computed(() => {
  const n = selectedNode.value
  return n ? statusOf(n) === 'online' : false
})

const acct = computed<AccountSnapshot | undefined>(() =>
  selectedNodeId.value ? hub.accounts[selectedNodeId.value] : undefined,
)

const symbolOptions = computed<string[]>(() => {
  const found = new Set<string>([DEFAULT_SYMBOL])
  for (const sym of Object.keys(acct.value?.quotes || {})) found.add(sym.toUpperCase())
  for (const pos of acct.value?.positions || []) {
    if (pos.symbol) found.add(pos.symbol.toUpperCase())
  }
  const filters = parseNodeDispatchFilters(selectedNode.value?.filters)
  for (const sym of Object.keys(filters)) found.add(sym.toUpperCase())
  return [...found].sort()
})

const nodeOptions = computed(() =>
  [...hub.nodes].sort((a, b) => {
    const ao = statusOf(a) === 'online' ? 0 : 1
    const bo = statusOf(b) === 'online' ? 0 : 1
    if (ao !== bo) return ao - bo
    return (a.name || a.node_id).localeCompare(b.name || b.node_id, 'zh')
  }),
)

function nodeLabel(n: NodeOut): string {
  const name = n.name || n.node_id
  return n.mt5_login ? `${name} · ${n.mt5_login}` : name
}

function onSelectNode(id: string): void {
  selectedNodeId.value = id
  nodeMenuOpen.value = false
}

function onDocClick(e: MouseEvent): void {
  const root = (e.target as HTMLElement | null)?.closest?.('.trend-hub-field')
  if (!root) nodeMenuOpen.value = false
}

watch(selectedNodeId, (id) => {
  if (id && !hub.accounts[id]) {
    void hub.fetchNodeAccount(id)
  }
})

// 当前选中节点被删掉时，回落到默认挑选逻辑
watch(
  () => hub.nodes.map((n) => n.node_id).join(','),
  () => {
    if (!selectedNodeId.value || !hub.nodes.some((n) => n.node_id === selectedNodeId.value)) {
      selectedNodeId.value = pickDefaultNodeId()
    }
  },
)

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  loading.value = true
  try {
    await hub.fetchNodes()
    selectedNodeId.value = pickDefaultNodeId()
    if (selectedNodeId.value && !hub.accounts[selectedNodeId.value]) {
      await hub.fetchNodeAccount(selectedNodeId.value)
    }
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <div>
    <div class="row between" style="margin-bottom: 16px; align-items: flex-start">
      <div>
        <h2 style="margin: 0">趋势面板</h2>
        <p class="muted" style="margin: 6px 0 0; font-size: 13px">
          跨节点只读观测 EMA / RSI 趋势；默认第一个在线节点、品种 {{ DEFAULT_SYMBOL }}
        </p>
      </div>
      <div class="trend-hub-field">
        <span class="muted" style="font-size: 12px">观察节点</span>
        <button
          type="button"
          class="trend-hub-select"
          :disabled="!nodeOptions.length"
          :aria-expanded="nodeMenuOpen"
          @click.stop="nodeMenuOpen = !nodeMenuOpen"
        >
          <span
            class="dot"
            :class="selectedNode && statusOf(selectedNode) === 'online' ? 'online' : 'offline'"
          ></span>
          <span class="trend-hub-select-text">
            {{ selectedNode ? nodeLabel(selectedNode) : '暂无节点' }}
          </span>
          <span class="trend-hub-caret" aria-hidden="true">▾</span>
        </button>
        <div v-if="nodeMenuOpen && nodeOptions.length" class="trend-hub-menu" role="listbox">
          <button
            v-for="n in nodeOptions"
            :key="n.node_id"
            type="button"
            class="trend-hub-option"
            :class="{ active: n.node_id === selectedNodeId }"
            role="option"
            :aria-selected="n.node_id === selectedNodeId"
            @click="onSelectNode(n.node_id)"
          >
            <span class="dot" :class="statusOf(n) === 'online' ? 'online' : 'offline'"></span>
            <span>{{ nodeLabel(n) }}</span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="card card-pad muted">正在加载节点列表…</div>
    <div v-else-if="!selectedNodeId" class="card card-pad muted">
      暂无可用节点。请先在「节点」接入至少一个节点后再查看趋势。
    </div>
    <TrendPanel
      v-else
      :key="selectedNodeId"
      :node-id="selectedNodeId"
      :trend-config="selectedNode?.trend"
      :symbol-options="symbolOptions"
      :default-symbol="DEFAULT_SYMBOL"
      symbol-storage-prefix="trend:hub:symbol"
      :online="online"
    />
  </div>
</template>

<style scoped>
.trend-hub-field {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 240px;
}
.trend-hub-select {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(12, 18, 32, 0.6);
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  color: var(--text);
  font-size: 13px;
  padding: 8px 12px;
  text-align: left;
  cursor: pointer;
  width: 100%;
}
.trend-hub-select:focus {
  border-color: var(--primary);
  outline: none;
}
.trend-hub-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.trend-hub-select-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trend-hub-caret {
  color: var(--muted);
  font-size: 11px;
}
.trend-hub-menu {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 20;
  background: rgba(12, 18, 32, 0.98);
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 4px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
  max-height: 280px;
  overflow: auto;
}
.trend-hub-option {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
}
.trend-hub-option:hover,
.trend-hub-option.active {
  background: rgba(59, 130, 246, 0.18);
}
</style>
