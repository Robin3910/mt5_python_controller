<script setup lang="ts">
// AI 信号管理中心的页头：标题 + 当前档位 + 更新时间 + 刷新 + 口径说明条。
//
// 说明条**必须显眼** —— 这个页面的数字口径多（质量分 / 等级 / 止盈档位），
// 不讲清楚用户会把它们混着读。
defineProps<{
  /** 当前档位（窗口） */
  scopeLabel?: string
  /** 更新时间（Unix 秒） */
  updatedAt?: number | null
  loading?: boolean
  /** 口径说明（后端原样给出） */
  scope?: string
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()

function fmtUpdated(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<template>
  <section class="pf-card">
    <div class="pf-card-head">
      <div class="pf-card-head-main">
        <span class="pf-card-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a10 10 0 0 1 10 10" />
            <path d="M12 6a6 6 0 0 1 6 6" />
            <circle cx="12" cy="12" r="2" />
          </svg>
        </span>
        <div>
          <p class="pf-card-title">AI信号管理中心</p>
          <p class="pf-card-sub">
            TV报警接入 · 当前档位：{{ scopeLabel || '全部' }} · 更新于 {{ fmtUpdated(updatedAt) }}
          </p>
        </div>
      </div>
      <button type="button" class="pf-btn" :disabled="loading" @click="emit('refresh')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12a9 9 0 1 1-3-6.7M21 3v6h-6" />
        </svg>
        刷新
      </button>
    </div>

    <!-- 口径说明条：口径多，必须显眼 -->
    <div v-if="scope" class="pf-scope-note info" style="margin-top: 10px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
      <span>{{ scope }}</span>
    </div>
  </section>
</template>
