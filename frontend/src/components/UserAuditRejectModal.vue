<script setup lang="ts">
// 驳回弹窗：5 个预设原因 chip（点选自动填入 textarea）+ 补充说明。
//
// 「确认驳回」按钮在理由为空时禁用 —— 预设 chip 会自动填入 textarea。
import { ref, watch } from 'vue'
import { REJECT_REASONS, type UserReviewOut } from '@/api/types'

const props = defineProps<{
  review: UserReviewOut | null
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'confirm', reason: string): void
}>()

const reason = ref('')

watch(
  () => props.review,
  () => {
    reason.value = ''
  },
)

function pick(text: string): void {
  reason.value = text
}
</script>

<template>
  <div v-if="review" class="modal-mask" @click.self="emit('cancel')">
    <div class="card card-pad ua-modal">
      <div class="ua-modal-head">
        <span class="ua-modal-icon is-reject" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9" />
            <path d="M8.5 15.5l7-7M15.5 8.5l-7 7" stroke-linecap="round" />
          </svg>
        </span>
        <div class="ua-modal-titles">
          <div class="ua-modal-title">驳回 KYC 实名审核</div>
          <div class="ua-modal-sub">
            {{ review.display_name || review.username }} · {{ review.real_name || '未提交实名材料' }}
            （{{ review.account }}）
          </div>
        </div>
      </div>

      <div class="ua-modal-body">
        <div class="ua-label">选择驳回原因</div>
        <div class="ua-chips">
          <button
            v-for="r in REJECT_REASONS"
            :key="r"
            type="button"
            class="ua-chip"
            :class="{ on: reason === r }"
            @click="pick(r)"
          >
            {{ r }}
          </button>
        </div>

        <div class="ua-label" style="margin-top: 12px">补充说明（可留空）</div>
        <textarea
          v-model="reason"
          class="ua-textarea"
          rows="3"
          maxlength="256"
          placeholder="点击上方预设原因或直接输入驳回理由"
        ></textarea>
        <p class="ua-counter">{{ reason.length }} / 256</p>
      </div>

      <div class="modal-footer">
        <button class="btn-ghost" :disabled="busy" @click="emit('cancel')">取消</button>
        <button
          class="btn-danger-solid"
          :disabled="busy || !reason.trim()"
          @click="emit('confirm', reason.trim())"
        >
          {{ busy ? '提交中…' : '确认驳回' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ua-modal {
  width: 100%;
  max-width: 380px;
  border-color: #2a3b5c;
  background: #0f172a;
}
.ua-modal-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ua-modal-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
}
.ua-modal-icon svg { width: 18px; height: 18px; }
.ua-modal-icon.is-reject { background: rgba(246, 70, 93, 0.15); color: #f6465d; }
.ua-modal-icon.is-approve { background: rgba(34, 197, 94, 0.15); color: #22c55e; }

.ua-modal-titles { min-width: 0; }
.ua-modal-title { font-size: 14px; font-weight: 700; color: #e6ebf4; }
.ua-modal-sub {
  margin-top: 2px;
  font-size: 10px;
  color: #8b96a8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ua-modal-body { margin-top: 14px; }
.ua-label {
  font-size: 11px;
  font-weight: 500;
  color: #8b96a8;
  margin-bottom: 6px;
}
.ua-chips { display: flex; flex-direction: column; gap: 6px; }
.ua-chip {
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: #0c1220;
  color: #8b96a8;
  font-size: 11px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}
.ua-chip:hover { border-color: #2b3a5c; color: #b8c4d9; }
.ua-chip.on {
  border-color: rgba(246, 70, 93, 0.55);
  background: rgba(246, 70, 93, 0.15);
  color: #f87171;
}

.ua-textarea {
  width: 100%;
  padding: 9px 11px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: #0c1220;
  color: #e6ebf4;
  font-size: 11px;
  line-height: 1.6;
  resize: vertical;
  box-sizing: border-box;
}
.ua-textarea:focus { outline: none; border-color: #f6465d; }
.ua-textarea::placeholder { color: #4a5568; }
.ua-counter { margin: 4px 0 0; font-size: 9px; color: #5b6b85; text-align: right; }

.btn-danger-solid {
  height: 34px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: #f6465d;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.btn-danger-solid:hover:not(:disabled) { background: #ef3a52; }
.btn-danger-solid:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
