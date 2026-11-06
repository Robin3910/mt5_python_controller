<script setup lang="ts">
// 审核卡片：待审核 / 已审核两种形态。
//
// 视觉映射自原型（Tailwind 任意值）→ 本页 scoped style 的 CSS 变量，
// 不复用 Tailwind 类名（见设计文档 §6.1）。
import { computed } from 'vue'
import type { UserReviewOut } from '@/api/types'

const props = defineProps<{
  review: UserReviewOut
  expanded: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle'): void
  (e: 'approve'): void
  (e: 'reject'): void
}>()

const isPending = computed(() => props.review.status === 'pending')
const isApproved = computed(() => props.review.status === 'approved')
const isRejected = computed(() => props.review.status === 'rejected')

/** 头像首字：优先显示名，其次用户名 */
const initial = computed(() => {
  const src = props.review.display_name || props.review.username || '?'
  return src.slice(0, 1).toUpperCase()
})

/** 展示名：显示名优先 */
const title = computed(() => props.review.display_name || props.review.username)

function fmtTime(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fmtBalance(v: number): string {
  return `${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} U`
}
</script>

<template>
  <article class="ua-card" :class="`is-${review.status}`">
    <!-- 头部：头像 / 用户名 / 账号 / 时间 / 状态徽章 -->
    <header class="ua-card-head">
      <span class="ua-avatar" :class="`is-${review.status}`" aria-hidden="true">{{ initial }}</span>
      <div class="ua-head-main">
        <div class="ua-head-row">
          <span class="ua-name">{{ title }}</span>
          <span class="ua-account">{{ review.account }}</span>
        </div>
        <div class="ua-sub">
          <template v-if="isPending">
            注册 {{ fmtTime(review.register_time) }} · {{ review.phone || '未绑定手机号' }}
          </template>
          <template v-else>
            注册 {{ fmtTime(review.register_time) }} · 审核 {{ fmtTime(review.audit_time) }}
          </template>
        </div>
      </div>
      <span class="ua-status" :class="`is-${review.status}`">
        <span v-if="isPending" class="ua-pulse" aria-hidden="true"></span>
        <svg v-else-if="isApproved" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
          <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
          <path d="M18 6L6 18M6 6l12 12" stroke-linecap="round" />
        </svg>
        {{ isPending ? '待审核' : isApproved ? '已通过' : '已驳回' }}
      </span>
    </header>

    <!-- KYC 实名材料 -->
    <section class="ua-kyc">
      <div class="ua-kyc-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M12 2l2.4 2.1 3.2-.3 1.2 3 2.8 1.5-1 3.1 1 3.1-2.8 1.5-1.2 3-3.2-.3L12 22l-2.4-2.1-3.2.3-1.2-3-2.8-1.5 1-3.1-1-3.1 2.8-1.5 1.2-3 3.2.3z" />
          <circle cx="12" cy="12" r="2.6" />
        </svg>
        KYC 实名材料 · 提交于 {{ fmtTime(review.submit_time) }}
      </div>
      <dl class="ua-kyc-rows">
        <div class="ua-kyc-row">
          <dt>实名姓名</dt>
          <dd>{{ review.real_name || '—' }}</dd>
        </div>
        <div class="ua-kyc-row">
          <dt>邮箱</dt>
          <dd>{{ review.email || '—' }}</dd>
        </div>
        <div class="ua-kyc-row">
          <dt>手机号</dt>
          <dd>{{ review.phone || '—' }}</dd>
        </div>
      </dl>
      <p v-if="!review.kyc_submitted" class="ua-kyc-note">
        用户未提交姓名（可由管理员在通过前手工补录）
      </p>
    </section>

    <!-- 驳回原因 -->
    <p v-if="isRejected && review.reject_reason" class="ua-reject">
      驳回原因：{{ review.reject_reason }}
    </p>

    <!-- 名下节点（可折叠） -->
    <section class="ua-nodes">
      <button type="button" class="ua-nodes-head" :aria-expanded="expanded" @click="emit('toggle')">
        <span class="ua-nodes-head-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <rect x="9" y="2" width="6" height="5" rx="1" />
            <rect x="2" y="17" width="6" height="5" rx="1" />
            <rect x="16" y="17" width="6" height="5" rx="1" />
            <path d="M12 7v4M5 17v-3h14v3" stroke-linecap="round" />
          </svg>
          名下节点
          <span class="ua-count">{{ review.nodes.length }} 个</span>
        </span>
        <svg class="ua-caret" :class="{ open: expanded }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
          <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>

      <div v-if="expanded" class="ua-nodes-body">
        <p v-if="!review.nodes.length" class="ua-nodes-empty">
          该用户暂无节点，审核通过后可创建
        </p>
        <ul v-else class="ua-node-list">
          <li v-for="n in review.nodes" :key="n.node_id" class="ua-node">
            <span class="ua-node-icon" :class="n.status === 'online' ? 'on' : 'off'" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="2" y="4" width="20" height="8" rx="2" />
                <rect x="2" y="12" width="20" height="8" rx="2" />
                <path d="M6 8h.01M6 16h.01" stroke-linecap="round" />
              </svg>
            </span>
            <span class="ua-node-name">{{ n.name }}</span>
            <span class="ua-node-id ua-mono">{{ n.node_id }}</span>
            <span class="ua-node-status" :class="n.status">{{ n.status === 'online' ? '在线' : '离线' }}</span>
            <span class="ua-node-balance ua-mono">{{ fmtBalance(n.balance) }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 操作条（仅待审核） -->
    <footer v-if="isPending" class="ua-actions">
      <button type="button" class="ua-btn ua-btn-reject" :disabled="busy" @click="emit('reject')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
          <path d="M18 6L6 18M6 6l12 12" stroke-linecap="round" />
        </svg>
        驳回
      </button>
      <button type="button" class="ua-btn ua-btn-approve" :disabled="busy" @click="emit('approve')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
          <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        通过
      </button>
    </footer>
  </article>
</template>

<style scoped>
.ua-card {
  border-radius: 12px;
  border: 1px solid var(--ua-border);
  background: var(--ua-card);
  padding: 12px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
}
@media (min-width: 1024px) {
  .ua-card { padding: 16px; }
}

/* ── 头部 ── */
.ua-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ua-avatar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 700;
}
.ua-avatar.is-pending {
  background: linear-gradient(135deg, rgba(245, 158, 11, 0.3), rgba(245, 158, 11, 0.05));
  color: #fbbf24;
}
.ua-avatar.is-approved { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
.ua-avatar.is-rejected { background: rgba(246, 70, 93, 0.15); color: #f6465d; }

.ua-head-main { flex: 1; min-width: 0; }
.ua-head-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ua-name {
  font-size: 12px;
  font-weight: 700;
  color: var(--ua-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ua-account {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--ua-border-2);
  font-size: 9px;
  color: var(--ua-muted);
}
.ua-sub {
  margin-top: 2px;
  font-size: 9px;
  color: var(--ua-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ua-status {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: 10px;
  font-weight: 600;
}
.ua-status svg { width: 11px; height: 11px; }
.ua-status.is-pending {
  border-color: rgba(245, 158, 11, 0.3);
  background: rgba(245, 158, 11, 0.1);
  color: #fbbf24;
}
.ua-status.is-approved {
  border-color: rgba(34, 197, 94, 0.3);
  background: rgba(34, 197, 94, 0.1);
  color: #4ade80;
}
.ua-status.is-rejected {
  border-color: rgba(246, 70, 93, 0.4);
  background: rgba(246, 70, 93, 0.1);
  color: #f6465d;
}
.ua-pulse {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  animation: ua-pulse 1.6s ease-in-out infinite;
}
@keyframes ua-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

/* ── KYC 块 ── */
.ua-kyc {
  margin-top: 10px;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: var(--ua-soft);
}
.ua-kyc-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9px;
  font-weight: 700;
  color: #38bdf8;
}
.ua-kyc-title svg { width: 12px; height: 12px; flex-shrink: 0; }
.ua-kyc-rows { margin: 8px 0 0; display: flex; flex-direction: column; gap: 5px; }
.ua-kyc-row {
  display: grid;
  grid-template-columns: 62px 1fr;
  gap: 8px;
  font-size: 10px;
  align-items: baseline;
}
.ua-kyc-row dt { color: var(--ua-dim); }
.ua-kyc-row dd {
  margin: 0;
  color: var(--ua-text);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ua-kyc-note {
  margin: 8px 0 0;
  font-size: 9px;
  color: var(--ua-amber-l);
}

/* ── 驳回原因 ── */
.ua-reject {
  margin: 8px 0 0;
  padding: 7px 9px;
  border-radius: 6px;
  border: 1px solid rgba(246, 70, 93, 0.25);
  background: rgba(246, 70, 93, 0.05);
  font-size: 9px;
  line-height: 1.6;
  color: #f87171;
}

/* ── 名下节点 ── */
.ua-nodes { margin-top: 8px; }
.ua-nodes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: var(--ua-soft);
  cursor: pointer;
  color: var(--ua-muted);
  font-size: 10px;
  font-weight: 700;
}
.ua-nodes-head:hover { border-color: #2b3a5c; }
.ua-nodes-head-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ua-nodes-head-left svg { width: 14px; height: 14px; color: #06b6d4; flex-shrink: 0; }
.ua-count {
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid rgba(6, 182, 212, 0.3);
  background: rgba(6, 182, 212, 0.1);
  font-size: 10px;
  color: #22d3ee;
}
.ua-caret {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  transition: transform 0.18s;
}
.ua-caret.open { transform: rotate(180deg); }

.ua-nodes-body { padding-top: 8px; }
.ua-nodes-empty {
  margin: 0;
  padding: 14px;
  border-radius: 8px;
  border: 1px dashed var(--ua-border-2);
  text-align: center;
  font-size: 10px;
  color: var(--ua-dim);
}
.ua-node-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.ua-node {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--ua-border);
  background: var(--ua-deep);
  min-width: 0;
}
.ua-node-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 5px;
}
.ua-node-icon svg { width: 12px; height: 12px; }
.ua-node-icon.on { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
.ua-node-icon.off { background: rgba(61, 76, 102, 0.2); color: #5b6b85; }
.ua-node-name {
  font-size: 10px;
  font-weight: 700;
  color: var(--ua-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ua-node-id { font-size: 9px; color: var(--ua-dim); flex-shrink: 0; }
.ua-node-status {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 9px;
}
.ua-node-status.online { background: rgba(34, 197, 94, 0.1); color: #4ade80; }
.ua-node-status.offline { background: var(--ua-border-2); color: var(--ua-dim); }
.ua-node-balance {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  color: var(--ua-text);
}

/* ── 操作条 ── */
.ua-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--ua-border);
}
.ua-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 28px;
  padding: 0 14px;
  border-radius: 7px;
  border: 1px solid transparent;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}
.ua-btn svg { width: 13px; height: 13px; }
.ua-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.ua-btn-reject {
  border-color: rgba(246, 70, 93, 0.4);
  background: rgba(246, 70, 93, 0.1);
  color: #f6465d;
}
.ua-btn-reject:hover:not(:disabled) { background: rgba(246, 70, 93, 0.18); }
.ua-btn-approve {
  background: #22c55e;
  color: #fff;
  box-shadow: 0 2px 8px rgba(34, 197, 94, 0.25);
}
.ua-btn-approve:hover:not(:disabled) { background: #16a34a; }

.ua-mono { font-family: var(--mono); }
</style>
