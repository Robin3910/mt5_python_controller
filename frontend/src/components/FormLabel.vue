<script setup lang="ts">
// 表单字段标签 + 帮助图标（点击展示说明）
import { onMounted, onUnmounted, ref } from 'vue'

defineProps<{
  text: string
  help: string
  fieldId?: string
}>()

const open = ref(false)
const wrapRef = ref<HTMLElement | null>(null)

function toggle(): void {
  open.value = !open.value
}

function onDocClick(e: MouseEvent): void {
  if (!open.value || !wrapRef.value) return
  if (!wrapRef.value.contains(e.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="wrapRef" class="form-label-wrap">
    <label v-if="fieldId" class="form-label-row" :for="fieldId">
      <span>{{ text }}</span>
      <button
        type="button"
        class="field-help-btn"
        aria-label="查看字段说明"
        :aria-expanded="open"
        @click.stop="toggle"
      >
        ?
      </button>
    </label>
    <div v-else class="form-label-row">
      <span>{{ text }}</span>
      <button
        type="button"
        class="field-help-btn"
        aria-label="查看字段说明"
        :aria-expanded="open"
        @click.stop="toggle"
      >
        ?
      </button>
    </div>
    <div v-if="open" class="field-help-popover" role="tooltip">{{ help }}</div>
  </div>
</template>
