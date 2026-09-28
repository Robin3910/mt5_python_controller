<script setup lang="ts" generic="T extends string | number | boolean">
// 单选标签：替代原生下拉，选项平铺为可点选标签
import type { TagOption } from '@/components/tagOption'

defineProps<{
  modelValue: T | null | undefined
  options: readonly TagOption<T>[]
  disabled?: boolean
  ariaLabel?: string
  id?: string
  emptyText?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: T]
  change: [value: T]
}>()

function pick(opt: TagOption<T>, current: T | null | undefined, disabled?: boolean): void {
  if (disabled || opt.disabled || opt.value === current) return
  emit('update:modelValue', opt.value)
  emit('change', opt.value)
}
</script>

<template>
  <div
    :id="id"
    class="tag-select"
    role="radiogroup"
    :aria-label="ariaLabel"
    :aria-disabled="disabled || undefined"
  >
    <button
      v-for="(opt, i) in options"
      :key="`${String(opt.value)}-${i}`"
      type="button"
      role="radio"
      class="tag-select-item"
      :class="[opt.tone ? `is-${opt.tone}` : '', { active: opt.value === modelValue }]"
      :aria-checked="opt.value === modelValue"
      :disabled="disabled || opt.disabled"
      @click="pick(opt, modelValue, disabled)"
    >
      <span v-if="opt.hint" class="tag-select-code">{{ opt.label }}</span>
      <span :class="opt.hint ? 'tag-select-hint' : 'tag-select-label'">{{ opt.hint || opt.label }}</span>
    </button>
    <span v-if="!options.length && emptyText" class="tag-select-empty">{{ emptyText }}</span>
  </div>
</template>

<style scoped>
.tag-select {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.tag-select-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 6px 12px;
  border: 1px solid var(--glass-border);
  border-radius: 999px;
  background: var(--glass);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  box-shadow: none;
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  flex-shrink: 0;
}

.tag-select-item:has(.tag-select-code) {
  padding-left: 6px;
}

.tag-select-item:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--glass-highlight);
  background: rgba(255, 255, 255, 0.06);
  transform: none;
}

.tag-select-item:active:not(:disabled) {
  transform: none;
}

.tag-select-item:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px rgba(0, 212, 170, 0.16);
}

.tag-select-item.active {
  background: rgba(0, 212, 170, 0.12);
  border-color: rgba(0, 212, 170, 0.45);
  color: var(--primary);
}

.tag-select-code {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--glass-border);
  background: rgba(6, 10, 18, 0.45);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  line-height: 1.4;
}

.tag-select-hint,
.tag-select-label {
  white-space: nowrap;
}

.tag-select-item.is-buy.active {
  background: rgba(16, 185, 129, 0.14);
  border-color: rgba(16, 185, 129, 0.45);
  color: #6ee7b7;
}

.tag-select-item.is-buy.active .tag-select-code {
  background: rgba(16, 185, 129, 0.22);
  border-color: rgba(16, 185, 129, 0.4);
  color: #a7f3d0;
}

.tag-select-item.is-sell.active {
  background: rgba(239, 68, 68, 0.12);
  border-color: rgba(239, 68, 68, 0.42);
  color: #fca5a5;
}

.tag-select-item.is-sell.active .tag-select-code {
  background: rgba(239, 68, 68, 0.2);
  border-color: rgba(239, 68, 68, 0.4);
  color: #fecaca;
}

.tag-select-item.is-close.active {
  background: rgba(245, 158, 11, 0.12);
  border-color: rgba(245, 158, 11, 0.42);
  color: #fbbf24;
}

.tag-select-item.is-close.active .tag-select-code {
  background: rgba(245, 158, 11, 0.2);
  border-color: rgba(245, 158, 11, 0.4);
  color: #fde68a;
}

.tag-select-item.is-on.active {
  background: rgba(16, 185, 129, 0.14);
  border-color: rgba(16, 185, 129, 0.45);
  color: #6ee7b7;
}

.tag-select-item.is-off.active {
  background: rgba(239, 68, 68, 0.1);
  border-color: rgba(239, 68, 68, 0.35);
  color: #fca5a5;
}

.tag-select-empty {
  font-size: 12px;
  color: var(--muted);
}
</style>
