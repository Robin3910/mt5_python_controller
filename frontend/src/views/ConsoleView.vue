<script setup lang="ts">
// 中控台：系统配置（区间过滤与按币种分发策略）
import { onMounted, ref } from 'vue'
import { useHubStore } from '@/stores/hub'
import FilterRulesEditor from '@/components/FilterRulesEditor.vue'
import type { FilterRulesConfig } from '@/api/types'
import { parseFilterRules, serializeFilterRules, validateFilterRules } from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()

const filters = ref<FilterRulesConfig>({})
const filtersError = ref('')
const savedFlag = ref('')

onMounted(async () => {
  await hub.fetchConfig()
  filters.value = parseFilterRules(hub.filters)
})

function flash(msg: string): void {
  savedFlag.value = msg
  setTimeout(() => (savedFlag.value = ''), 1800)
}

async function saveFilters(): Promise<void> {
  filtersError.value = ''
  const payload = serializeFilterRules(filters.value)
  const errors = validateFilterRules(payload)
  if (errors.length) {
    filtersError.value = errors[0]
    return
  }
  const count = Object.keys(payload).length
  if (!(await confirmAction(`确认保存过滤规则？\n\n共 ${count} 个品种，保存后立即影响信号准入与分发。`))) return
  await hub.saveFilters(payload)
  filters.value = parseFilterRules(hub.filters)
  flash('过滤规则已保存')
}
</script>

<template>
  <div class="row between page-header">
    <div class="h1">中控台</div>
    <span v-if="savedFlag" class="tag green">{{ savedFlag }}</span>
  </div>

  <div class="grid layout-config">
    <div class="card card-pad span-full">
      <strong>多区间方向过滤</strong>
      <div style="margin-top: 12px">
        <FilterRulesEditor v-model="filters" mode="global" />
      </div>
      <div v-if="filtersError" style="color: var(--red); font-size: 12px; margin-top: 10px">{{ filtersError }}</div>
      <div class="row" style="margin-top: 12px"><button class="btn-primary" @click="saveFilters">保存过滤规则</button></div>
    </div>
  </div>
</template>
