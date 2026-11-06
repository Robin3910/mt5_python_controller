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
  <div class="pf-page cv-console-page">
    <section class="pf-card">
      <p class="pf-hint cv-console-desc">
        按品种配置操作中控台：信号到达后按判定结果执行分发，命中拦截时不下发到节点。
      </p>

      <div class="cv-console-body">
        <FilterRulesEditor v-model="filters" mode="global" @trigger="onManualTrigger" />
      </div>

      <p v-if="filtersError" class="pf-error cv-console-error">{{ filtersError }}</p>

      <div class="pf-divider"></div>

      <div class="cv-console-foot">
        <div class="cv-console-status">
          <span v-if="isDirty" class="pf-pill amber">未保存</span>
          <span v-else-if="savedFlag" class="pf-pill green">{{ savedFlag }}</span>
          <span v-else class="pf-hint">过滤规则已与后台同步</span>
        </div>
        <div class="cv-console-actions">
          <button type="button" class="pf-btn pf-btn-primary cv-save-btn" @click="saveFilters()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
              <path d="M17 21v-8H7v8M7 3v5h8" />
            </svg>
            保存过滤规则
          </button>
        </div>
      </div>
    </section>

    <div class="pf-note cv-console-note">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
      <p>
        未登记或取消「启用」的品种信号将被直接拒收（含平仓）；后台手动平仓不受影响。
        节点手数策略为「跟随中控台」时使用中控台配置的手数。
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 版式 1:1 复刻 UI/H5.html 的「操作中控台」tab；
   卡片 / 按钮 / 分段 / 开关等共用外观见 styles.css 的 pf- 前缀类。 */
.cv-console-page {
  max-width: 760px;
}

.cv-console-desc {
  margin: 0;
}

.cv-console-body {
  margin-top: 16px;
}

.cv-console-error {
  margin-top: 12px;
}

.cv-console-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.cv-console-status {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}

.cv-console-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.cv-save-btn {
  height: 38px;
  padding: 0 24px;
  font-size: 13px;
}

.cv-console-note {
  margin-top: 0;
}
</style>
