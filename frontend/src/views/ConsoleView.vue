<script setup lang="ts">
// 中控台：系统配置（区间过滤与按币种分发策略）
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { useHubStore } from '@/stores/hub'
import FilterRulesEditor from '@/components/FilterRulesEditor.vue'
import type { FilterRulesConfig } from '@/api/types'
import {
  parseFilterRules,
  serializeFilterRules,
  validateDisableGlobalLot,
  validateFilterRules,
} from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()

const filters = ref<FilterRulesConfig>({})
const filtersError = ref('')
const savedFlag = ref('')
/** 最近一次成功加载/保存后的序列化快照，用于判定未保存修改 */
const savedSnapshot = ref('')

onMounted(async () => {
  await Promise.all([hub.fetchConfig(), hub.fetchNodes()])
  filters.value = parseFilterRules(hub.filters)
  markClean()
})

function snapshotOf(cfg: FilterRulesConfig): string {
  return JSON.stringify(serializeFilterRules(cfg))
}

function markClean(): void {
  savedSnapshot.value = snapshotOf(filters.value)
}

const isDirty = computed(() => snapshotOf(filters.value) !== savedSnapshot.value)

function flash(msg: string): void {
  savedFlag.value = msg
  setTimeout(() => (savedFlag.value = ''), 1800)
}

/** 保存过滤规则。返回是否保存成功（校验失败 / 取消 / 请求失败均返回 false）。 */
async function saveFilters(options?: { skipConfirm?: boolean }): Promise<boolean> {
  filtersError.value = ''
  const payload = serializeFilterRules(filters.value)
  // 保存前刷新节点列表，确保「关闭全局手数」校验用的是最新节点配置
  await hub.fetchNodes()
  const errors = [
    ...validateFilterRules(payload),
    ...validateDisableGlobalLot(payload, hub.nodes),
  ]
  if (errors.length) {
    filtersError.value = errors[0]
    await ElMessageBox.alert(errors[0], '无法保存', { type: 'warning', confirmButtonText: '知道了' })
    return false
  }
  const count = Object.keys(payload).length
  if (!options?.skipConfirm) {
    if (!(await confirmAction(`确认保存过滤规则？\n\n共 ${count} 个品种，保存后立即影响信号准入与分发。`))) {
      return false
    }
  }
  try {
    await hub.saveFilters(payload)
    filters.value = parseFilterRules(hub.filters)
    markClean()
    flash('过滤规则已保存')
    return true
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    const detail = err?.response?.data?.detail || err?.message || '保存失败'
    filtersError.value = detail
    await ElMessageBox.alert(detail, '无法保存', { type: 'warning', confirmButtonText: '知道了' })
    return false
  }
}

/**
 * 有未保存修改时中断当前动作，弹窗引导进入保存流程。
 * 返回 true 表示已干净（本就无改动，或保存成功），可继续原动作。
 */
async function ensureSavedOrInterrupt(actionHint: string): Promise<boolean> {
  if (!isDirty.value) return true
  try {
    await ElMessageBox.confirm(
      `中控台配置尚未保存。\n\n${actionHint}将按后台已保存的规则执行，请先保存当前修改。`,
      '配置未保存',
      {
        confirmButtonText: '保存并继续',
        cancelButtonText: '取消',
        type: 'warning',
        closeOnClickModal: false,
      },
    )
  } catch {
    return false
  }
  return saveFilters({ skipConfirm: true })
}

// 手动触发信号：由 FilterRulesEditor 的 BUY/SELL 按钮确认后 emit，调用后台复用 /webhook 流程。
// 触发依据后台“已保存”的中控台配置；有未保存修改时先打断并引导保存。
async function onManualTrigger(payload: {
  symbol: string
  action: 'BUY' | 'SELL'
  volume: number
}): Promise<void> {
  const ok = await ensureSavedOrInterrupt(
    `手动触发 ${payload.action} ${payload.symbol}`,
  )
  if (!ok) return

  try {
    const res = await hub.triggerManualSignal(payload)
    if (res.status === 'accepted') {
      ElMessage.success(
        `已触发 ${payload.action} ${payload.symbol}（手数 ${payload.volume}，${res.targets ?? 0} 个目标节点）`,
      )
    } else if (res.status === 'duplicate') {
      ElMessage.warning(`重复信号被抑制：5 秒内已有相同的 ${payload.action} ${payload.symbol}`)
    } else if (res.status === 'rejected') {
      ElMessage.warning(`已拒收：${res.reason || '该品种未启用或不符合中控台规则'}`)
    } else {
      ElMessage.info(`已提交：${res.status}`)
    }
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    ElMessage.error(`触发失败：${err?.response?.data?.detail || err?.message || '请求失败'}`)
  }
}
</script>

<template>
  <div class="console-page">
    <div class="row between page-header">
      <div class="h1">中控台</div>
      <div class="row" style="gap: 8px">
        <span v-if="isDirty" class="tag amber">未保存</span>
        <span v-if="savedFlag" class="tag green">{{ savedFlag }}</span>
      </div>
    </div>

    <div class="grid layout-config console-body">
      <div class="card card-pad span-full console-card">
        <div class="console-card-head">
          <strong>多区间方向过滤</strong>
        </div>
        <div class="console-card-scroll">
          <FilterRulesEditor v-model="filters" mode="global" @trigger="onManualTrigger" />
        </div>
        <div class="console-card-foot">
          <div v-if="filtersError" class="console-card-error">{{ filtersError }}</div>
          <div class="row">
            <button class="btn-primary" @click="saveFilters()">保存过滤规则</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 中控台整体：撑满可视区高度，内部 flex 布局，仅规则列表滚动 */
.console-page {
  display: flex;
  flex-direction: column;
  /* 60px topbar + 24px*2 content padding = 108px */
  height: calc(100vh - 108px);
  min-height: 420px;
}

.console-body {
  flex: 1;
  min-height: 0;
}

/* 卡片撑满 console-body，内部三段式 flex：头 / 滚动区 / 底 */
.console-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.console-card-head {
  flex-shrink: 0;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--glass-border);
}

.console-card-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 让 sticky 工具栏与滚动条对齐，同时保留内部 padding */
  margin: 12px -8px 0;
  padding: 0 8px;
}

.console-card-foot {
  flex-shrink: 0;
  padding-top: 12px;
  margin-top: 12px;
  border-top: 1px solid var(--glass-border);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.console-card-error {
  color: var(--red);
  font-size: 12px;
}

/* 移动端：视口偏矮时，回退为自然高度，避免可视区被压得太小 */
@media (max-width: 768px) {
  .console-page {
    height: auto;
    min-height: 0;
  }
  .console-body {
    flex: initial;
    min-height: 0;
  }
  .console-card {
    height: auto;
  }
  .console-card-scroll {
    overflow-y: visible;
    margin: 12px 0 0;
    padding: 0;
  }
}
</style>
