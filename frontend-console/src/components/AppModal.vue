<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()
</script>

<template>
  <div class="modal-overlay" @click.self="emit('close')">
    <div class="modal-panel">
      <div class="modal-head">
        <div>
          <h3 class="modal-title">{{ title }}</h3>
          <p v-if="subtitle" class="modal-subtitle">{{ subtitle }}</p>
        </div>
        <button class="modal-close" title="关闭" @click="emit('close')">✕</button>
      </div>

      <div class="modal-body">
        <slot />
      </div>

      <div v-if="$slots.footer" class="modal-foot">
        <slot name="footer" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 18px 18px;
}
</style>
