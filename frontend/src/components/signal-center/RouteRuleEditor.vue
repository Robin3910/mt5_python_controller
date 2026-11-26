<script setup lang="ts">
// 自动路由规则编辑弹窗：新建 / 编辑一条「过滤条件 → 目标分组」的规则。
//
// **为什么规则是「配置」而不是「批处理」**：
// 人工路由（选信号 → 选分组 → 点下发）已下线，现在是唯一的路由方式 ——
// 配好规则 → 之后进来的每条信号自动按规则分发。
//
// **空 = 不限制**（与后端 `signal_route_rules` 的语义一致）：
// - 类型空 → 所有类型都收
// - 周期空 → 所有周期都收
// - 方向空 → 所有方向都收
//
// 唯一的必填是**目标分组**：没有目标分组的规则没有意义，服务端也会直接拒收。
import { computed, reactive, ref, watch } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { SignalRouteRule, SignalRouteRuleInput } from '@/api/types'

const props = defineProps<{
  open: boolean
  /** 传了就是编辑模式 */
  rule?: SignalRouteRule | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const hub = useHubStore()

/**
 * 具体类型码的中文名（与后端 `signal_meta.SIGNAL_TYPE_LABELS` 一致）。
 *
 * 只保留常用的基础类型码；分类选项已从前端移除，直接选类型码即可。
 */
const TYPE_CODES: { code: string; label: string }[] = [
  { code: 'ob', label: '订单块 OB' },
  { code: 'fvg', label: '公允价值缺口 FVG' },
  { code: 'pdz', label: '溢价折让区 PDZ' },
  { code: 'fibo', label: '斐波那契' },
]

/** 周期选项（与后端 `signal_meta.TIMEFRAMES` 同源，只列常用的） */
const TIMEFRAMES = ['M1', 'M3', 'M5', 'M15', 'M30', 'M45', 'H1', 'H4', 'D1', 'W1']

const DIRECTIONS = [
  { value: 'BUY', label: '做多' },
  { value: 'SELL', label: '做空' },
]

const form = reactive({
  name: '',
  enabled: true,
  signal_types: [] as string[],
  timeframes: [] as string[],
  directions: [] as string[],
  group_ids: [] as string[],
  priority: 100,
})

const saving = ref(false)
const error = ref('')

const isEdit = computed(() => !!props.rule)

/** 可选目标分组：只列出「自动（按规则）」模式的分组 */
const groupOptions = computed(() =>
  hub.groups
    .filter((g) => (g.signal_source ?? 'direct') === 'auto')
    .map((g) => ({ group_id: g.group_id, name: g.name || g.group_id })),
)

/** 没有任何自动模式的分组时给出提示（配了规则也没分组能收） */
const noAutoGroups = computed(() => groupOptions.value.length === 0)

/** 条件全空 = 收全部信号 —— 要明确提示，不能让人以为配了个窄规则 */
const catchAll = computed(
  () =>
    form.signal_types.length === 0 &&
    form.timeframes.length === 0 &&
    form.directions.length === 0,
)

function reset(): void {
  const r = props.rule
  form.name = r?.name ?? ''
  form.enabled = r?.enabled ?? true
  form.signal_types = [...(r?.signal_types ?? [])]
  form.timeframes = [...(r?.timeframes ?? [])]
  form.directions = [...(r?.directions ?? [])]
  form.group_ids = [...(r?.group_ids ?? [])]
  form.priority = r?.priority ?? 100
  error.value = ''
}

watch(
  () => props.open,
  (v) => {
    if (v) reset()
  },
)

/** 多选切换：选中就加进去，取消就移除（保序） */
function toggle(list: string[], value: string): void {
  const i = list.indexOf(value)
  if (i >= 0) list.splice(i, 1)
  else list.push(value)
}

function isOn(list: string[], value: string): boolean {
  return list.includes(value)
}

async function save(): Promise<void> {
  error.value = ''
  if (!form.name.trim()) {
    error.value = '请填写规则名称'
    return
  }
  if (!form.group_ids.length) {
    error.value = '请至少选择一个目标分组'
    return
  }
  saving.value = true
  const payload: SignalRouteRuleInput = {
    name: form.name.trim(),
    enabled: form.enabled,
    signal_types: [...form.signal_types],
    timeframes: [...form.timeframes],
    directions: [...form.directions],
    group_ids: [...form.group_ids],
    priority: form.priority,
  }
  const res = props.rule
    ? await hub.updateRouteRule(props.rule.rule_id, payload)
    : await hub.createRouteRule(payload)
  saving.value = false
  if (!res.ok) {
    error.value = res.message
    return
  }
  emit('close')
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="card card-pad rr-modal" role="dialog" aria-modal="true">
      <div class="rr-head">
        <div class="h1">{{ isEdit ? '编辑自动路由规则' : '新建自动路由规则' }}</div>
        <button type="button" class="rr-close" aria-label="关闭" @click="emit('close')">×</button>
      </div>

      <div class="rr-body">
        <!-- 规则名 + 启用 -->
        <div class="rr-field">
          <label class="pf-label" for="rr-name">规则名称</label>
          <input
            id="rr-name"
            v-model="form.name"
            class="pf-input"
            maxlength="64"
            placeholder="例如：结构分析 → 黄金组"
          />
        </div>

        <!-- 目标分组（必填） -->
        <div class="rr-field">
          <span class="pf-label">目标分组 <em class="rr-req">必填</em></span>
          <p class="pf-hint" style="margin: 0 0 6px">
            只有「信号来源 = 自动（按规则）」的分组才会被自动路由命中。直连分组不受规则影响。
          </p>
          <div v-if="noAutoGroups" class="pf-error">
            当前没有任何「自动（按规则）」模式的分组。请先到【分组管理】把目标分组改成该模式。
          </div>
          <div v-else class="rr-chips">
            <button
              v-for="g in groupOptions"
              :key="g.group_id"
              type="button"
              class="rr-chip"
              :class="{ on: isOn(form.group_ids, g.group_id) }"
              @click="toggle(form.group_ids, g.group_id)"
            >
              {{ g.name }}
            </button>
          </div>
        </div>

        <!-- 信号类型 -->
        <div class="rr-field">
          <span class="pf-label">信号类型</span>
          <p class="pf-hint" style="margin: 0 0 6px">不选 = 所有类型都收。</p>
          <div class="rr-chips">
            <button
              v-for="t in TYPE_CODES"
              :key="t.code"
              type="button"
              class="rr-chip rr-chip-code"
              :class="{ on: isOn(form.signal_types, t.code) }"
              @click="toggle(form.signal_types, t.code)"
            >
              {{ t.code }}
              <span class="rr-chip-hint">{{ t.label }}</span>
            </button>
          </div>
        </div>

        <!-- 周期 -->
        <div class="rr-field">
          <span class="pf-label">周期</span>
          <p class="pf-hint" style="margin: 0 0 6px">不选 = 所有周期都收。</p>
          <div class="rr-chips">
            <button
              v-for="tf in TIMEFRAMES"
              :key="tf"
              type="button"
              class="rr-chip rr-chip-code"
              :class="{ on: isOn(form.timeframes, tf) }"
              @click="toggle(form.timeframes, tf)"
            >
              {{ tf }}
            </button>
          </div>
        </div>

        <!-- 方向 -->
        <div class="rr-field">
          <span class="pf-label">方向</span>
          <p class="pf-hint" style="margin: 0 0 6px">不选 = 多空都收。</p>
          <div class="rr-chips">
            <button
              v-for="d in DIRECTIONS"
              :key="d.value"
              type="button"
              class="rr-chip"
              :class="{ on: isOn(form.directions, d.value) }"
              @click="toggle(form.directions, d.value)"
            >
              {{ d.label }}
            </button>
          </div>
        </div>

        <!-- 优先级 -->
        <div class="rr-field">
          <label class="pf-label" for="rr-priority">优先级</label>
          <p class="pf-hint" style="margin: 0 0 6px">
            数值小的先挑走分组：同一分组被多条规则命中时，只按第一条命中的规则下发一次
            （不会重复开仓）。
          </p>
          <input
            id="rr-priority"
            v-model.number="form.priority"
            class="pf-input rr-priority"
            type="number"
            step="1"
            min="0"
            max="9999"
          />
        </div>

        <!-- 启用 -->
        <div class="rr-field rr-row">
          <label class="pf-label" for="rr-enabled">启用规则</label>
          <input id="rr-enabled" v-model="form.enabled" type="checkbox" />
        </div>

        <p v-if="catchAll" class="rr-warn">
          ⚠️ 未选任何过滤条件 —— 这条规则会接收所有信号（不限类型 / 周期 / 方向）。
        </p>

        <p v-if="error" class="pf-error">{{ error }}</p>
      </div>

      <div class="modal-footer rr-actions">
        <button type="button" class="btn-ghost" :disabled="saving" @click="emit('close')">
          取消
        </button>
        <button
          type="button"
          class="btn-primary"
          :disabled="saving || !form.group_ids.length"
          @click="save"
        >
          {{ saving ? '保存中…' : isEdit ? '保存' : '创建规则' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rr-modal {
  width: 100%;
  max-width: 560px;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
}

.rr-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
}

.rr-close {
  border: none;
  background: transparent;
  color: var(--pf-muted, #8b96a8);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.rr-close:hover { color: var(--pf-text, #e6ebf4); }

.rr-body {
  margin-top: 14px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  flex: 1;
  min-height: 0;
}

.rr-field { display: flex; flex-direction: column; }
.rr-row {
  flex-direction: row;
  align-items: center;
  gap: 10px;
}

.rr-req {
  font-style: normal;
  font-size: 10px;
  color: var(--pf-amber, #f59e0b);
  margin-left: 4px;
}

.rr-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rr-chip {
  padding: 5px 10px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.2;
  cursor: pointer;
  transition: none;
}
.rr-chip:hover:not(.on) {
  color: var(--text);
  border-color: var(--glass-highlight);
}
.rr-chip.on {
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
  border-color: transparent;
  color: #fff;
}
.rr-chip-code {
  font-family: var(--mono);
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.rr-chip-hint {
  font-family: var(--font, sans-serif);
  font-weight: 500;
  opacity: 0.75;
  font-size: 10px;
}

.rr-priority { max-width: 120px; }

.rr-warn {
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  background: rgba(245, 158, 11, 0.1);
  color: #fbbf24;
  font-size: 12px;
  line-height: 1.6;
}

.rr-actions { justify-content: flex-end; flex-shrink: 0; }
</style>
