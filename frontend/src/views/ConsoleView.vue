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
  <div class="console-page">
    <div class="row between page-header">
      <div class="h1">中控台</div>
      <span v-if="savedFlag" class="tag green">{{ savedFlag }}</span>
    </div>

    <div class="grid layout-config console-body">
      <div class="card card-pad span-full console-card">
        <div class="console-card-head">
          <strong>多区间方向过滤</strong>
        </div>
        <div class="console-card-scroll">
          <FilterRulesEditor v-model="filters" mode="global" />
        </div>
        <div class="console-card-foot">
          <div v-if="filtersError" class="console-card-error">{{ filtersError }}</div>
          <div class="row">
            <button class="btn-primary" @click="saveFilters">保存过滤规则</button>
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
