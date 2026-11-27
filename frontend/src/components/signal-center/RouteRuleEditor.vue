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
//
// **类型条件有两种形态**（后端 `type_by_timeframe` 非空时走映射形态）：
// 1. 映射形态（本弹窗默认）：**每个周期单独指定收哪些类型** ——
//    「M1 只收 OB，M5 只收 FVG」。白名单语义：没列出的周期一律不收。
// 2. 扁平形态（存量规则）：类型与周期各自独立，是笛卡尔积 ——
//    「类型=ob 且 周期∈{M1,M5}」，表达不了按周期区分类型。
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
  /** 周期 → 类型 白名单映射（映射形态） */
  type_by_timeframe: {} as Record<string, string[]>,
  /** 扁平形态的类型条件（仅扁平形态下生效） */
  signal_types: [] as string[],
  timeframes: [] as string[],
  directions: [] as string[],
  group_ids: [] as string[],
  priority: 100,
})

const saving = ref(false)
const error = ref('')

/**
 * 类型条件的形态：'map' = 按周期各自指定类型；'flat' = 存量规则的扁平形态。
 *
 * **为什么用显式的 mode 而不是「看 map 是否为空」**：映射形态下把周期全删光，
 * map 会变成空对象 —— 后端会把它当成扁平形态（= 不限制 = 收全部信号），
 * 与用户「我要按周期收」的意图完全相反。所以形态必须显式记。
 */
const typeMode = ref<'map' | 'flat'>('map')

const isEdit = computed(() => !!props.rule)

/** 可选目标分组：只列出「自动（按规则）」模式的分组 */
const groupOptions = computed(() =>
  hub.groups
    .filter((g) => (g.signal_source ?? 'direct') === 'auto')
    .map((g) => ({ group_id: g.group_id, name: g.name || g.group_id })),
)

/** 没有任何自动模式的分组时给出提示（配了规则也没分组能收） */
const noAutoGroups = computed(() => groupOptions.value.length === 0)

/** 已配置周期（按加入顺序倒序展示，最新加的在前） */
const mappedTimeframes = computed(() => Object.keys(form.type_by_timeframe).reverse())

/** 还没加进矩阵的周期（供「添加周期」面板挑选） */
const availableTimeframes = computed(() =>
  TIMEFRAMES.filter((tf) => !(tf in form.type_by_timeframe)),
)

/** 条件全空 = 收全部信号 —— 要明确提示，不能让人以为配了个窄规则 */
const catchAll = computed(() => {
  if (typeMode.value === 'map') {
    // 映射形态下白名单为空 = 一个周期都没配 → 收不到任何信号（不是 catch-all）
    return false
  }
  return (
    form.signal_types.length === 0 &&
    form.timeframes.length === 0 &&
    form.directions.length === 0
  )
})

function reset(): void {
  const r = props.rule
  form.name = r?.name ?? ''
  form.enabled = r?.enabled ?? true
  const map = { ...(r?.type_by_timeframe ?? {}) }
  // 存量规则（没有映射）→ 走扁平形态原样回显；
  // 带映射的规则 → 走矩阵形态。新建规则默认矩阵形态。
  typeMode.value = Object.keys(map).length > 0 || !r ? 'map' : 'flat'
  form.type_by_timeframe = map
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

/** 切换形态：两个语义的类型 / 周期条件不能并存，来回切时做一次搬运 */
function setTypeMode(mode: 'map' | 'flat'): void {
  if (mode === typeMode.value) return
  if (mode === 'map') {
    // 扁平 → 映射：把已选周期搬进矩阵，类型条件原样落到每个周期
    const tfs = form.timeframes.length ? form.timeframes : ['M15']
    form.type_by_timeframe = Object.fromEntries(
      tfs.map((tf) => [tf, [...form.signal_types]]),
    )
    form.signal_types = []
    form.timeframes = []
  } else {
    // 映射 → 扁平：周期取矩阵里的 key，类型取各周期类型的并集
    const types: string[] = []
    for (const list of Object.values(form.type_by_timeframe)) {
      for (const code of list) if (!types.includes(code)) types.push(code)
    }
    form.timeframes = Object.keys(form.type_by_timeframe)
    form.signal_types = types
    form.type_by_timeframe = {}
  }
  typeMode.value = mode
}

/** 新增一个周期行（默认该周期不限类型 = 空数组） */
function addTimeframe(tf: string): void {
  if (tf in form.type_by_timeframe) return
  form.type_by_timeframe[tf] = []
}

function removeTimeframe(tf: string): void {
  delete form.type_by_timeframe[tf]
}

function toggleTfType(tf: string, code: string): void {
  const list = form.type_by_timeframe[tf] ?? (form.type_by_timeframe[tf] = [])
  toggle(list, code)
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
  if (typeMode.value === 'map' && !mappedTimeframes.value.length) {
    // 映射形态下一个周期都没配 = 白名单为空 = 这条规则一条信号都收不到，
    // 与用户「我要按周期收」的意图完全相反 —— 必须拦下来。
    error.value = '请至少添加一个周期并指定它要收的类型'
    return
  }
  saving.value = true
  const payload: SignalRouteRuleInput = {
    name: form.name.trim(),
    enabled: form.enabled,
    // 映射形态只写 type_by_timeframe；扁平形态只写 signal_types / timeframes。
    // 后端在 type_by_timeframe 非空时会忽略并清空扁平的两个条件。
    signal_types: typeMode.value === 'flat' ? [...form.signal_types] : [],
    timeframes: typeMode.value === 'flat' ? [...form.timeframes] : [],
    type_by_timeframe:
      typeMode.value === 'map' ? { ...form.type_by_timeframe } : {},
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

        <!-- 信号类型（按周期各自指定） -->
        <div class="rr-field">
          <span class="pf-label">信号类型</span>
          <p class="pf-hint" style="margin: 0 0 8px">
            每个周期可以单独指定它要收哪些类型。没列出的周期一律不收。
          </p>

          <!-- 形态切换 -->
          <div class="rr-modes">
            <button
              type="button"
              class="rr-mode"
              :class="{ on: typeMode === 'map' }"
              @click="setTypeMode('map')"
            >
              按周期配置
            </button>
            <button
              type="button"
              class="rr-mode"
              :class="{ on: typeMode === 'flat' }"
              @click="setTypeMode('flat')"
            >
              统一配置
            </button>
          </div>

          <!-- 映射形态：周期 → 类型矩阵 -->
          <div v-if="typeMode === 'map'" class="rr-map">
            <div v-if="!mappedTimeframes.length" class="rr-map-empty">
              还没有添加任何周期 —— 添加周期后才能指定它要收哪些类型。
            </div>

            <div v-for="tf in mappedTimeframes" :key="tf" class="rr-map-row">
              <div class="rr-map-tf">
                <span class="rr-map-tf-code">{{ tf }}</span>
                <button
                  type="button"
                  class="rr-map-del"
                  :aria-label="`移除 ${tf}`"
                  @click="removeTimeframe(tf)"
                >
                  ×
                </button>
              </div>
              <div class="rr-map-types">
                <button
                  v-for="t in TYPE_CODES"
                  :key="t.code"
                  type="button"
                  class="rr-chip rr-chip-code"
                  :class="{ on: isOn(form.type_by_timeframe[tf] ?? [], t.code) }"
                  @click="toggleTfType(tf, t.code)"
                >
                  {{ t.code }}
                  <span class="rr-chip-hint">{{ t.label }}</span>
                </button>
                <span
                  v-if="!(form.type_by_timeframe[tf] ?? []).length"
                  class="rr-map-any"
                >
                  该周期不限类型
                </span>
              </div>
            </div>

            <div v-if="availableTimeframes.length" class="rr-add-tf">
              <span class="rr-add-tf-label">添加周期</span>
              <div class="rr-chips">
                <button
                  v-for="tf in availableTimeframes"
                  :key="tf"
                  type="button"
                  class="rr-chip rr-chip-code"
                  @click="addTimeframe(tf)"
                >
                  {{ tf }}
                </button>
              </div>
            </div>
          </div>

          <!-- 扁平形态：类型 + 周期各自独立（存量规则的形态） -->
          <div v-else class="rr-flat">
            <div class="rr-flat-block">
              <span class="rr-flat-label">类型</span>
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
            <div class="rr-flat-block">
              <span class="rr-flat-label">周期</span>
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
          <p class="pf-hint" style="margin: 6px 0 0">
            不选 = 不限制。这种形态下类型与周期是各自独立的，
            表达不了「M1 只收 OB、M5 只收 FVG」—— 需要按周期区分请切到「按周期配置」。
          </p>
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

/* —— 类型条件的形态切换 —— */
.rr-modes {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.rr-mode {
  padding: 4px 12px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.rr-mode:hover:not(.on) {
  color: var(--text);
  border-color: var(--glass-highlight);
}
.rr-mode.on {
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
  border-color: transparent;
  color: #fff;
}

/* —— 周期 → 类型矩阵 —— */
.rr-map {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rr-map-empty {
  padding: 10px;
  border-radius: 8px;
  border: 1px dashed var(--glass-border);
  color: var(--pf-faint, #5b6b85);
  font-size: 12px;
}
.rr-map-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass);
}
.rr-map-tf {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  width: 72px;
}
.rr-map-tf-code {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--text);
}
.rr-map-del {
  border: none;
  background: transparent;
  color: var(--pf-muted, #8b96a8);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  padding: 0 3px;
}
.rr-map-del:hover { color: #f87171; }
.rr-map-types {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  flex: 1;
  min-width: 0;
}
.rr-map-any {
  font-size: 11px;
  color: var(--pf-faint, #5b6b85);
  font-style: italic;
}
.rr-add-tf {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding-top: 2px;
}
.rr-add-tf-label {
  flex-shrink: 0;
  width: 72px;
  padding-top: 6px;
  font-size: 11px;
  color: var(--pf-faint, #5b6b85);
}

/* —— 扁平形态（存量规则） —— */
.rr-flat {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rr-flat-block {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.rr-flat-label {
  flex-shrink: 0;
  width: 32px;
  padding-top: 6px;
  font-size: 11px;
  color: var(--pf-faint, #5b6b85);
}

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
