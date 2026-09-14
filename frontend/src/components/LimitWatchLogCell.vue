<script setup lang="ts">
// 分组列表「监听日志」：只显示条数，点击打开完整日志
import { computed } from 'vue'
import type { LimitWatchLogOut } from '@/api/types'

const props = defineProps<{
  logs: LimitWatchLogOut[]
}>()

const emit = defineEmits<{ open: [] }>()

const count = computed(() => props.logs.length)
</script>

<template>
  <button type="button" class="btn-sm btn-success" title="查看全部监听日志" @click="emit('open')">
    {{ count }} 条
  </button>
</template>
