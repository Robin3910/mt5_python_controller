<script setup lang="ts">
// 通过弹窗：确认 KYC 实名审核 + 可选同时分配节点。
//
// 节点分配复用后端 users_api.assign_nodes_core 的既有校验（活动子任务 409 /
// 分组成员 409 confirm_required），这里只负责收集勾选结果并处理确认弹窗。
import { computed, ref, watch } from 'vue'
import type { NodeOut, UserReviewOut } from '@/api/types'

const props = defineProps<{
  review: UserReviewOut | null
  /** 管理员可见的全部节点（用于勾选分配） */
  nodes: NodeOut[]
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'confirm', nodeIds: string[], confirm: boolean): void
}>()

/** 勾选的节点 id 集合 */
const picked = ref<Set<string>>(new Set())
const showNodes = ref(false)

watch(
  () => props.review,
  () => {
    picked.value = new Set()
    showNodes.value = false
  },
)

const title = computed(() => props.review?.display_name || props.review?.username || '')

/** 该用户当前名下的节点（已在 review.nodes 里） */
const ownedIds = computed(() => new Set((props.review?.nodes || []).map((n) => n.node_id)))

/** 可勾选的节点：不含该用户已有的（已有的无需再分配） */
const candidates = computed(() => props.nodes.filter((n) => !ownedIds.value.has(n.node_id)))

function toggle(id: string, on: boolean): void {
  const next = new Set(picked.value)
  if (on) next.add(id)
  else next.delete(id)
  picked.value = next
}

function isPicked(id: string): boolean {
  return picked.value.has(id)
}
</script>

<template>
  <div v-if="review" class="modal-mask" @click.self="emit('cancel')">
    <div class="card card-pad ua-modal">
      <div class="ua-modal-head">
        <span class="ua-modal-icon is-approve" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" stroke-linejoin="round" />
            <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <div class="ua-modal-titles">
          <div class="ua-modal-title">通过 KYC 实名审核</div>
          <div class="ua-modal-sub">
            {{ title }} · {{ review.real_name || '未提交实名材料' }}（{{ review.account }}）
          </div>
        </div>
      </div>

      <div class="ua-modal-body">
        <div class="ua-info-grid">
          <div class="ua-info-cell">
            <div class="ua-info-label">邮箱</div>
            <div class="ua-info-value">{{ review.email || '未填写' }}</div>
          </div>
          <div class="ua-info-cell">
            <div class="ua-info-label">名下节点</div>
            <div class="ua-info-value">{{ review.nodes.length }} 个</div>
          </div>
        </div>

        <p class="ua-note">
          通过后该用户将完成实名认证，可正常登录使用交易、带单与节点管理等完整功能。
        </p>

        <!-- 节点分配（可选） -->
        <div class="ua-pick">
          <button type="button" class="ua-pick-head" :aria-expanded="showNodes" @click="showNodes = !showNodes">
            <span class="ua-pick-head-left">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <rect x="9" y="2" width="6" height="5" rx="1" />
                <rect x="2" y="17" width="6" height="5" rx="1" />
                <rect x="16" y="17" width="6" height="5" rx="1" />
                <path d="M12 7v4M5 17v-3h14v3" stroke-linecap="round" />
              </svg>
              同时分配节点
              <span v-if="picked.size" class="ua-pick-count">{{ picked.size }} 个</span>
            </span>
            <svg class="ua-pick-caret" :class="{ open: showNodes }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
              <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>

          <div v-if="showNodes" class="ua-pick-body">
            <p class="ua-pick-hint">
              不勾选则保持现有节点归属不变。勾选后节点将从原所有者转入该用户名下。
            </p>
            <p v-if="!candidates.length" class="ua-pick-empty">没有可分配的节点</p>
            <ul v-else class="ua-pick-list">
              <li v-for="n in candidates" :key="n.node_id" class="ua-pick-item">
                <label class="ua-pick-label">
                  <input
                    type="checkbox"
                    :checked="isPicked(n.node_id)"
                    @change="toggle(n.node_id, ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="ua-pick-name">{{ n.name }}</span>
                  <span class="ua-pick-id">{{ n.node_id }}</span>
                  <span class="ua-pick-owner">
                    {{ n.owner_username || '管理员名下' }}
                  </span>
                </label>
              </li>
            </ul>
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-ghost" :disabled="busy" @click="emit('cancel')">取消</button>
        <button
          class="btn-approve-solid"
          :disabled="busy"
          @click="emit('confirm', Array.from(picked), false)"
        >
          {{ busy ? '提交中…' : '确认通过' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ua-modal {
  width: 100%;
  max-width: 400px;
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

.ua-info-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.ua-info-cell {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #22304a;
  background: #0c1220;
  min-width: 0;
}
.ua-info-label { font-size: 9px; color: #5b6b85; }
.ua-info-value {
  margin-top: 3px;
  font-size: 11px;
  font-weight: 700;
  color: #e6ebf4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ua-note {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(34, 197, 94, 0.2);
  background: rgba(34, 197, 94, 0.06);
  font-size: 10px;
  line-height: 1.6;
  color: #b8c4d9;
}

.ua-pick { margin-top: 10px; }
.ua-pick-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #22304a;
  background: #0c1220;
  color: #8b96a8;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}
.ua-pick-head:hover { border-color: #2b3a5c; }
.ua-pick-head-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ua-pick-head-left svg { width: 14px; height: 14px; color: #06b6d4; flex-shrink: 0; }
.ua-pick-count {
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid rgba(6, 182, 212, 0.3);
  background: rgba(6, 182, 212, 0.1);
  font-size: 10px;
  color: #22d3ee;
}
.ua-pick-caret { width: 15px; height: 15px; flex-shrink: 0; transition: transform 0.18s; }
.ua-pick-caret.open { transform: rotate(180deg); }

.ua-pick-body { padding-top: 8px; }
.ua-pick-hint { margin: 0 0 8px; font-size: 9px; line-height: 1.6; color: #5b6b85; }
.ua-pick-empty {
  margin: 0;
  padding: 12px;
  border-radius: 8px;
  border: 1px dashed #22304a;
  text-align: center;
  font-size: 10px;
  color: #5b6b85;
}
.ua-pick-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; }
.ua-pick-item {
  border-radius: 7px;
  border: 1px solid #1c2740;
  background: #0a101e;
}
.ua-pick-label {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 9px;
  font-size: 10px;
  cursor: pointer;
  min-width: 0;
}
.ua-pick-label input { flex-shrink: 0; accent-color: #22c55e; }
.ua-pick-name {
  color: #e6ebf4;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ua-pick-id {
  flex-shrink: 0;
  font-family: var(--mono);
  font-size: 9px;
  color: #5b6b85;
}
.ua-pick-owner {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 9px;
  color: #8b96a8;
}

.btn-approve-solid {
  height: 34px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: #22c55e;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(34, 197, 94, 0.25);
}
.btn-approve-solid:hover:not(:disabled) { background: #16a34a; }
.btn-approve-solid:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
