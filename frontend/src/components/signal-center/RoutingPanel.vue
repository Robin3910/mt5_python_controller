<script setup lang="ts">
// 路由面板：把「待路由」的信号下发给管理员指定的分组。
//
// **为什么路由要单独一个面板，而不是在信号流里就地操作**：
// 路由是「选一组信号 → 选一组分组 → 批量下发」，本质是批量操作。
// 塞进列表的行内按钮会让人一条一条点，而实际用法是「这几条 ob 信号 → 那两个分组」。
//
// 三条互相独立的路径（后端 `group.signal_source` 决定行为）：
// 1. **直连**：TV 告警直接进分组（默认，存量分组零变化）；
// 2. **信号中心下发**：本面板手动路由（`route_mode=center`）；
// 3. **手动触发**：中控台直接触发（`route_mode=direct`，但走的是人工入口）。
//
// **幂等**：同一信号在同一分组已有未收口任务 → 后端返回 409，
// 本面板把「已有任务」提示出来而不是静默失败 —— 重复下单是必须让人看见的事。
import { computed, ref } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { SignalStreamItem } from '@/api/types'
import QualityBadge from '@/components/signal-center/QualityBadge.vue'

const props = defineProps<{
  items: SignalStreamItem[]
  loading?: boolean
}>()

const hub = useHubStore()

/** 选中的信号号 */
const selected = ref<Set<string>>(new Set())
/** 目标分组 */
const targetGroup = ref<string>('')
/** 参数覆盖（留空 = 沿用信号原值） */
const overrideVolume = ref<number | null>(null)
const overrideSl = ref<number | null>(null)
const overrideTp = ref<number | null>(null)

const submitting = ref(false)
const message = ref<{ kind: 'ok' | 'err'; text: string } | null>(null)

/** 只允许路由 strategy 链路的开仓信号 */
const routable = computed(() =>
  props.items.filter(
    (i) => i.model === 'strategy' && i.action && i.action !== 'CLOSE',
  ),
)

const selectedList = computed(() => Array.from(selected.value))

/** 中心模式的分组排在前面 —— 那是「等路由」的那批 */
const groupOptions = computed(() => {
  const list = hub.groups.map((g) => ({
    group_id: g.group_id,
    name: g.name,
    signal_source: g.signal_source ?? 'direct',
  }))
  return list.sort((a, b) => {
    if (a.signal_source !== b.signal_source) {
      return a.signal_source === 'center' ? -1 : 1
    }
    return (a.name || '').localeCompare(b.name || '')
  })
})

function isSelected(id: string): boolean {
  return selected.value.has(id)
}

function toggle(id: string): void {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function selectAll(): void {
  selected.value = new Set(routable.value.map((i) => i.signal_id))
}

function clearSelection(): void {
  selected.value = new Set()
}

async function doRoute(): Promise<void> {
  if (!selectedList.value.length) {
    message.value = { kind: 'err', text: '请先选择要路由的信号' }
    return
  }
  if (!targetGroup.value) {
    message.value = { kind: 'err', text: '请选择目标分组' }
    return
  }
  submitting.value = true
  message.value = null
  try {
    const res = await hub.batchRouteSignals({
      signal_ids: selectedList.value,
      group_ids: [targetGroup.value],
      volume: overrideVolume.value,
      sl: overrideSl.value,
      tp: overrideTp.value,
    })
    if (!res) {
      message.value = { kind: 'err', text: '路由请求失败' }
      return
    }
    const { ok, conflicts, failed } = res.summary
    const parts = [`成功 ${ok}`]
    if (conflicts) parts.push(`冲突 ${conflicts}（该分组已有进行中任务）`)
    if (failed) parts.push(`失败 ${failed}`)
    message.value = {
      kind: conflicts || failed ? 'err' : 'ok',
      text: parts.join(' · '),
    }
    if (ok) clearSelection()
  } finally {
    submitting.value = false
  }
}

async function doDrop(): Promise<void> {
  if (!selectedList.value.length) {
    message.value = { kind: 'err', text: '请先选择要丢弃的信号' }
    return
  }
  submitting.value = true
  try {
    let done = 0
    for (const id of selectedList.value) {
      const r = await hub.dropSignal(id)
      if (r.ok) done += 1
    }
    message.value = { kind: 'ok', text: `已丢弃 ${done} 条` }
    if (done) clearSelection()
  } finally {
    submitting.value = false
  }
}

function routeStateTag(state: string | null): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    pending: { cls: 'amber', text: '待路由' },
    auto: { cls: 'blue', text: '已直连' },
    routed: { cls: 'green', text: '已下发' },
    dropped: { cls: 'grey', text: '已丢弃' },
  }
  return m[state ?? ''] ?? { cls: '', text: '—' }
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 10px">
      <h3 class="pf-section-title"><span class="pf-bar" />路由到分组</h3>
      <span class="pf-count">已选 {{ selectedList.length }} 条</span>
    </div>

    <!-- 目标与参数 -->
    <div class="pf-filter-bar" style="margin-bottom: 10px">
      <select v-model="targetGroup" class="pf-filter-input select">
        <option value="">选择目标分组…</option>
        <option v-for="g in groupOptions" :key="g.group_id" :value="g.group_id">
          {{ g.name || g.group_id }}{{ g.signal_source === 'center' ? '（信号中心模式）' : '' }}
        </option>
      </select>
      <input
        v-model.number="overrideVolume"
        class="pf-filter-input"
        type="number"
        step="0.01"
        min="0"
        placeholder="手数（留空沿用）"
      />
      <input
        v-model.number="overrideSl"
        class="pf-filter-input"
        type="number"
        step="0.00001"
        placeholder="SL（留空沿用）"
      />
      <input
        v-model.number="overrideTp"
        class="pf-filter-input"
        type="number"
        step="0.00001"
        placeholder="TP（留空沿用）"
      />
      <button
        type="button"
        class="pf-btn pf-btn-mini"
        :disabled="submitting"
        @click="doRoute"
      >
        {{ submitting ? '下发中…' : '下发' }}
      </button>
      <button
        type="button"
        class="pf-btn pf-btn-mini"
        :disabled="submitting"
        @click="doDrop"
      >
        丢弃
      </button>
    </div>

    <p v-if="message" class="sc-msg" :class="message.kind">{{ message.text }}</p>

    <!-- 待路由列表 -->
    <template v-if="routable.length">
      <div class="row between" style="margin-bottom: 6px">
        <span class="muted" style="font-size: 11px">可路由 {{ routable.length }} 条（仅分组链路的开仓信号）</span>
        <span>
          <button type="button" class="pf-btn pf-btn-mini" @click="selectAll">全选</button>
          <button type="button" class="pf-btn pf-btn-mini" @click="clearSelection">清空</button>
        </span>
      </div>

      <div class="pf-rows">
        <div class="pf-rows-inner">
          <div class="pf-row pf-row-head">
            <span></span>
            <span class="c-muted">时间</span>
            <span class="c-muted">品种</span>
            <span class="c-muted">动作</span>
            <span class="c-muted">周期</span>
            <span class="c-muted">类型</span>
            <span class="c-muted">质量</span>
            <span class="c-muted">路由状态</span>
            <span class="c-muted">信号号</span>
          </div>
          <div
            v-for="row in routable"
            :key="row.signal_id"
            class="pf-row clickable"
            @click="toggle(row.signal_id)"
          >
            <span>{{ isSelected(row.signal_id) ? '☑' : '☐' }}</span>
            <span class="c-muted">{{ fmtTime(row.received_at) }}</span>
            <span class="c-strong">{{ row.symbol || '—' }}</span>
            <span>
              <span
                class="pf-pill"
                :class="row.action === 'BUY' ? 'green' : row.action === 'SELL' ? 'red' : 'grey'"
              >{{ row.action }}</span>
            </span>
            <span class="c-muted">{{ row.timeframe || '—' }}</span>
            <span class="c-muted">{{ row.signal_type || '—' }}</span>
            <span><QualityBadge :quality="row.quality" /></span>
            <span><span class="pf-pill" :class="routeStateTag(row.route_state).cls">{{ routeStateTag(row.route_state).text }}</span></span>
            <span class="c-mono">{{ row.signal_id }}</span>
          </div>
        </div>
      </div>
    </template>

    <p v-else-if="!loading" class="pf-empty">当前筛选下没有可路由的信号</p>
    <p v-else class="pf-empty">加载中…</p>
  </div>
</template>

<style scoped>
.clickable { cursor: pointer; }
.sc-msg {
  margin: 0 0 8px;
  font-size: 11px;
}
.sc-msg.ok { color: var(--pf-green, #22c55e); }
.sc-msg.err { color: var(--pf-amber, #f59e0b); }
</style>
