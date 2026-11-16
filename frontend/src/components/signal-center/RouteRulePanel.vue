<script setup lang="ts">
// 自动路由规则列表：一条规则 = 一组过滤条件 → 一组目标分组。
//
// **这是唯一的路由方式**（人工路由已下线）：配好规则 → 之后每条信号
// 自动按规则分发，不需要任何人点「下发」。
//
// **每条规则都显示命中统计**（命中 N 条 / 最近命中时间）——
// 「配了但一条都没命中」是最容易发生的问题（条件写窄了 / 目标分组不是自动模式），
// 必须一眼可见，否则用户会以为功能坏了。
import { computed, onMounted, ref } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { SignalRouteRule } from '@/api/types'
import RouteRuleEditor from '@/components/signal-center/RouteRuleEditor.vue'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()

const loading = ref(false)
const error = ref('')
const editorOpen = ref(false)
const editing = ref<SignalRouteRule | null>(null)

onMounted(async () => {
  loading.value = true
  try {
    await hub.fetchRouteRules()
  } catch (e) {
    error.value = '规则列表加载失败'
  } finally {
    loading.value = false
  }
})

const rules = computed(() => hub.routeRules)

/** 分组 ID -> 分组名（用于回显目标分组） */
const groupNames = computed(() => {
  const m: Record<string, string> = {}
  for (const g of hub.groups) m[g.group_id] = g.name || g.group_id
  return m
})

/** 条件摘要：类型 / 周期 / 方向，空的显示「全部」 */
function typeSummary(rule: SignalRouteRule): string {
  if (!rule.signal_types.length) return '全部类型'
  return rule.signal_types
    .map((t) => {
      // 分类原样显示；类型码显示成「ob（订单块 OB）」更好认
      const known: Record<string, string> = {
        ob: '订单块 OB',
        fvg: 'FVG',
        pdz: 'PDZ',
        fibo: '斐波那契',
        bos: 'BOS',
        swing: 'Swing',
        choch: 'CHoCH',
        mm: 'MM',
      }
      return known[t] ?? t
    })
    .join('、')
}

function tfSummary(rule: SignalRouteRule): string {
  if (!rule.timeframes.length) return '全部周期'
  return rule.timeframes.join('、')
}

function dirSummary(rule: SignalRouteRule): string {
  if (!rule.directions.length) return '多空均可'
  return rule.directions.map((d) => (d === 'BUY' ? '做多' : '做空')).join('、')
}

function groupSummary(rule: SignalRouteRule): string {
  if (!rule.group_ids.length) return '—'
  return rule.group_ids.map((gid) => groupNames.value[gid] ?? gid).join('、')
}

/** 命中时间的相对描述 */
function hitLabel(rule: SignalRouteRule): string {
  if (!rule.hit?.count) return '尚未命中'
  const ts = rule.hit.last_hit_ts
  if (!ts) return `命中 ${rule.hit.count} 条`
  const d = new Date(ts * 1000)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `命中 ${rule.hit.count} 条 · 最近 ${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function openCreate(): void {
  editing.value = null
  editorOpen.value = true
}

function openEdit(rule: SignalRouteRule): void {
  editing.value = rule
  editorOpen.value = true
}

async function toggleEnabled(rule: SignalRouteRule): Promise<void> {
  const res = await hub.updateRouteRule(rule.rule_id, {
    name: rule.name,
    enabled: !rule.enabled,
    signal_types: rule.signal_types,
    timeframes: rule.timeframes,
    directions: rule.directions,
    group_ids: rule.group_ids,
    priority: rule.priority,
  })
  if (!res.ok) error.value = res.message
}

async function remove(rule: SignalRouteRule): Promise<void> {
  const ok = await confirmAction(`确认删除自动路由规则「${rule.name}」？`)
  if (!ok) return
  const res = await hub.deleteRouteRule(rule.rule_id)
  if (!res.ok) error.value = res.message
}
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 10px">
      <h3 class="pf-section-title"><span class="pf-bar" />自动路由规则</h3>
      <button type="button" class="pf-btn pf-btn-mini" @click="openCreate">+ 新建规则</button>
    </div>

    <p class="rr-intro">
      配好规则后，信号中心收到信号会自动下发给对应分组，不需要再手动点「下发」。
      只有「信号来源 = 自动（按规则）」的分组会被规则命中。
    </p>

    <p v-if="error" class="pf-error">{{ error }}</p>

    <div v-if="loading" class="pf-empty">加载中…</div>

    <div v-else-if="!rules.length" class="pf-empty">
      还没有配置自动路由规则。点右上角「新建规则」开始。
    </div>

    <div v-else class="rr-list">
      <div
        v-for="rule in rules"
        :key="rule.rule_id"
        class="rr-item"
        :class="{ off: !rule.enabled }"
      >
        <div class="rr-item-head">
          <span class="rr-dot" :class="rule.enabled ? 'on' : 'off'" />
          <strong class="rr-name">{{ rule.name }}</strong>
          <span class="pf-pill" :class="rule.enabled ? 'green' : 'grey'">
            {{ rule.enabled ? '已启用' : '已停用' }}
          </span>
          <span class="rr-actions">
            <button type="button" class="pf-btn pf-btn-mini" @click="openEdit(rule)">编辑</button>
            <button type="button" class="pf-btn pf-btn-mini" @click="toggleEnabled(rule)">
              {{ rule.enabled ? '停用' : '启用' }}
            </button>
            <button type="button" class="pf-btn pf-btn-mini" @click="remove(rule)">删除</button>
          </span>
        </div>

        <div class="rr-conds">
          <span class="rr-cond"><i>类型</i>{{ typeSummary(rule) }}</span>
          <span class="rr-cond"><i>周期</i>{{ tfSummary(rule) }}</span>
          <span class="rr-cond"><i>方向</i>{{ dirSummary(rule) }}</span>
        </div>

        <div class="rr-conds">
          <span class="rr-cond"><i>目标</i>{{ groupSummary(rule) }}</span>
        </div>

        <div class="rr-hit" :class="{ none: !rule.hit?.count }">{{ hitLabel(rule) }}</div>
      </div>
    </div>

    <RouteRuleEditor
      :open="editorOpen"
      :rule="editing"
      @close="editorOpen = false"
    />
  </div>
</template>

<style scoped>
.rr-intro {
  margin: 0 0 10px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--pf-muted, #8b96a8);
}

.rr-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rr-item {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--glass-border);
  background: var(--glass);
}
.rr-item.off { opacity: 0.6; }

.rr-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rr-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--pf-text, #e6ebf4);
}

.rr-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.rr-dot.on { background: #22c55e; }
.rr-dot.off { background: #4a5568; }

.rr-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.rr-conds {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 6px;
}
.rr-cond {
  font-size: 11px;
  color: var(--pf-text, #c7d0e0);
}
.rr-cond i {
  font-style: normal;
  margin-right: 5px;
  color: var(--pf-faint, #5b6b85);
}

.rr-hit {
  margin-top: 6px;
  font-size: 11px;
  color: #60a5fa;
}
.rr-hit.none { color: var(--pf-faint, #5b6b85); }
</style>
