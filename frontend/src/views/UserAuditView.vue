<script setup lang="ts">
// 用户注册审核页（仅管理员）。
//
// 视觉映射自原型（Tailwind 任意值）→ 本页 scoped style 的 CSS 变量，
// 不复用 Tailwind 类名（见设计文档 §6.1）。
//
// 实时性：30s 轮询（设计文档 §5.6 —— WS 推送留作增量）。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import api from '@/api/client'
import { useUserReviewStore } from '@/stores/userReview'
import { confirmAction } from '@/utils/confirm'
import type { NodeOut, UserReviewOut } from '@/api/types'
import UserAuditCard from '@/components/UserAuditCard.vue'
import UserAuditRejectModal from '@/components/UserAuditRejectModal.vue'
import UserAuditApproveModal from '@/components/UserAuditApproveModal.vue'

const store = useUserReviewStore()

/** 折叠状态：review_id → 是否展开 */
const expanded = ref<Record<number, boolean>>({})
/** 正在处理的审核单（禁用其按钮） */
const busyId = ref<number | null>(null)
/** 驳回弹窗目标 */
const rejectTarget = ref<UserReviewOut | null>(null)
/** 通过弹窗目标 */
const approveTarget = ref<UserReviewOut | null>(null)
/** 全部节点（用于分配勾选） */
const allNodes = ref<NodeOut[]>([])
/** 搜索输入（防抖后写回 store） */
const keywordInput = ref('')
/**
 * 当前 Tab。
 *
 * 不复用 store.status —— 「已审核」Tab 需要**同时**显示 approved 与 rejected，
 * 而后端 status 参数一次只给一个值。做法：请求 status=all，然后在本地分档。
 * 用独立的 tab ref（而不是改写 store.list.items）是为了让轮询刷新后
 * 不会把 Tab 内容冲掉。
 */
const tab = ref<'pending' | 'audited'>('pending')

const POLL_MS = 30_000
let pollTimer: number | undefined
let debounceTimer: number | undefined

const stats = computed(() => store.list.stats)
const items = computed(() => store.list.items)

/** 「已审核」Tab = 已通过 + 已驳回 */
const auditedCount = computed(() => store.auditedCount)

/** 「全部通过」按钮只在待审核 tab 且有待审时显示 */
const showBulk = computed(() => tab.value === 'pending' && stats.value.pending > 0)

/** 当前 Tab 要展示的条目 */
const visibleItems = computed<UserReviewOut[]>(() => {
  if (tab.value === 'pending') return items.value.filter((i) => i.status === 'pending')
  return items.value.filter((i) => i.status === 'approved' || i.status === 'rejected')
})

onMounted(async () => {
  await refresh()
  try {
    const { data } = await api.get<NodeOut[]>('/api/nodes')
    allNodes.value = data
  } catch {
    allNodes.value = []
  }
  pollTimer = window.setInterval(() => {
    refresh().catch(() => {})
  }, POLL_MS)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  if (debounceTimer) window.clearTimeout(debounceTimer)
})

/**
 * 拉数据。
 *
 * 「已审核」Tab 一次性取回三种状态（status=all），本地分档 ——
 * 否则 approved / rejected 各发一次请求，两次请求之间状态可能变化，
 * 徽章计数会跟列表对不上。
 */
async function refresh(): Promise<void> {
  if (tab.value === 'audited') {
    await store.fetchAll()
  } else {
    await store.setStatus('pending')
  }
}

/** Tab 切换 */
async function switchTab(next: 'pending' | 'audited'): Promise<void> {
  tab.value = next
  await refresh()
}

/** 搜索（防抖 300ms） */
function onKeyword(): void {
  if (debounceTimer) window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => {
    refresh().catch(() => {})
  }, 300)
}

function clearKeyword(): void {
  keywordInput.value = ''
  store.keyword = ''
  refresh().catch(() => {})
}

function toggleExpand(id: number): void {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] }
}

/** 409 的 detail 结构（与后端 users_api 一致） */
interface ConflictDetail {
  reason?: string
  message?: string
  memberships?: { node_name?: string; group_name?: string }[]
  followings?: { node_name?: string; group_name?: string }[]
  nodes?: { node_id: string; name?: string }[]
}

function detailOf(e: unknown): ConflictDetail | string | undefined {
  const err = e as { response?: { data?: { detail?: ConflictDetail | string } } }
  return err?.response?.data?.detail
}

/**
 * 执行通过（可带节点分配）。
 *
 * 后端分配节点时可能返回 409：
 * - reason='active_tasks' → 节点上有进行中的策略任务，**不可强转**，直接报错；
 * - reason='confirm_required' → 节点仍在分组里，需用户确认后再带 confirm=true 重试。
 */
async function runApprove(
  review: UserReviewOut,
  nodeIds: string[],
  confirm: boolean,
): Promise<void> {
  busyId.value = review.id
  try {
    await store.approve(review.id, nodeIds, confirm)
    approveTarget.value = null
    ElMessage.success('审核已通过，用户现在可以登录')
  } catch (e: unknown) {
    const detail = detailOf(e)
    if (detail && typeof detail === 'object' && detail.reason === 'confirm_required') {
      const names = (detail.memberships || [])
        .map((m) => `${m.node_name || m.group_name}`)
        .join('、')
      const ok = await confirmAction(
        `${detail.message || '节点仍在分组中'}（${names || '相关节点'}）。确认后将把它们移出这些分组。`,
        '确认分配节点',
      )
      if (ok) {
        await runApprove(review, nodeIds, true)
        return
      }
      return
    }
    if (detail && typeof detail === 'object' && detail.reason === 'active_tasks') {
      ElMessage.error(detail.message || '节点仍有进行中的策略任务，无法分配')
      return
    }
    ElMessage.error(typeof detail === 'string' ? detail : '操作失败，请稍后重试')
  } finally {
    busyId.value = null
  }
}

function confirmApprove(nodeIds: string[]): void {
  const review = approveTarget.value
  if (!review) return
  runApprove(review, nodeIds, false)
}

async function confirmReject(reason: string): Promise<void> {
  const review = rejectTarget.value
  if (!review) return
  busyId.value = review.id
  try {
    await store.reject(review.id, reason)
    rejectTarget.value = null
    ElMessage.success('已驳回，用户可重新提交实名材料')
  } catch (e: unknown) {
    const detail = detailOf(e)
    ElMessage.error(typeof detail === 'string' ? detail : '操作失败，请稍后重试')
  } finally {
    busyId.value = null
  }
}

/** 「全部通过」：批量通过所有待审 */
async function bulkApprove(): Promise<void> {
  const ok = await confirmAction(
    `将一次性通过全部 ${stats.value.pending} 个待审核用户。是否继续？`,
    '全部通过',
  )
  if (!ok) return
  try {
    const result = await store.bulkApprove([])
    if (result.failed.length) {
      ElMessage.warning(`已通过 ${result.approved} 个，${result.failed.length} 个失败`)
    } else {
      ElMessage.success(`已通过 ${result.approved} 个用户`)
    }
  } catch {
    ElMessage.error('批量通过失败，请稍后重试')
  }
}

</script>

<template>
  <div class="ua-page">
    <!-- 页头卡 -->
    <section class="ua-hero">
      <div class="ua-hero-top">
        <span class="ua-hero-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" stroke-linejoin="round" />
            <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <div class="ua-hero-titles">
          <div class="ua-hero-title">
            用户注册审核
            <svg class="ua-hero-help" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.4-2.8 2.4" stroke-linecap="round" />
              <circle cx="12" cy="17" r="0.6" fill="currentColor" />
            </svg>
          </div>
          <p class="ua-hero-sub">注册用户 KYC 实名审核 · 节点分布一目了然</p>
        </div>
      </div>

      <div class="ua-intro">
        <span class="ua-intro-label">频道介绍</span>
        所有注册进入平台的用户均需完成 KYC 实名审核，通过后方可使用完整功能；
        每个用户名下挂载的节点数量与明细在此统一查看。
      </div>

      <!-- 统计块 -->
      <div class="ua-stats">
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" stroke-linecap="round" />
              <circle cx="9.5" cy="7" r="3.5" />
              <path d="M22 21v-2a4 4 0 0 0-3-3.87" stroke-linecap="round" />
            </svg>
            注册用户
          </div>
          <div class="ua-stat-value" style="color: #38bdf8">{{ stats.total }}</div>
        </div>
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M7 2v4M17 2v4M3 8h18" stroke-linecap="round" />
              <path d="M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z" />
              <path d="M12 11v5M9.5 13.5h5" stroke-linecap="round" />
            </svg>
            待审核
          </div>
          <div class="ua-stat-value" style="color: #f59e0b">{{ stats.pending }}</div>
        </div>
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" stroke-linejoin="round" />
              <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            已通过
          </div>
          <div class="ua-stat-value" style="color: #22c55e">{{ stats.approved }}</div>
        </div>
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M15 9l-6 6M9 9l6 6" stroke-linecap="round" />
            </svg>
            已驳回
          </div>
          <div class="ua-stat-value" style="color: #f6465d">{{ stats.rejected }}</div>
        </div>
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <rect x="9" y="2" width="6" height="5" rx="1" />
              <rect x="2" y="17" width="6" height="5" rx="1" />
              <rect x="16" y="17" width="6" height="5" rx="1" />
              <path d="M12 7v4M5 17v-3h14v3" stroke-linecap="round" />
            </svg>
            节点总数
          </div>
          <div class="ua-stat-value" style="color: #06b6d4">{{ stats.node_total }}</div>
        </div>
        <div class="ua-stat">
          <div class="ua-stat-label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <rect x="2" y="4" width="20" height="7" rx="2" />
              <rect x="2" y="13" width="20" height="7" rx="2" />
              <path d="M6 7.5h.01M6 16.5h.01" stroke-linecap="round" />
            </svg>
            在线节点
          </div>
          <div class="ua-stat-value" style="color: #4ade80">{{ stats.node_online }}</div>
        </div>
      </div>
    </section>

    <!-- Tab 栏 + 批量操作 -->
    <div class="ua-toolbar">
      <div class="ua-tabs">
        <button
          type="button"
          class="ua-tab is-pending"
          :class="{ on: tab === 'pending' }"
          @click="switchTab('pending')"
        >
          待审核
          <span class="ua-tab-count">{{ stats.pending }}</span>
        </button>
        <button
          type="button"
          class="ua-tab is-audited"
          :class="{ on: tab === 'audited' }"
          @click="switchTab('audited')"
        >
          已审核
          <span class="ua-tab-count">{{ auditedCount }}</span>
        </button>
      </div>
      <button v-if="showBulk" type="button" class="ua-bulk" :disabled="store.loading" @click="bulkApprove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
          <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
          <path d="M3 12h4M17 12h4" stroke-linecap="round" />
        </svg>
        全部通过
      </button>
    </div>

    <!-- 搜索 -->
    <div class="ua-search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <circle cx="11" cy="11" r="7" />
        <path d="M20 20l-3.5-3.5" stroke-linecap="round" />
      </svg>
      <input
        v-model="keywordInput"
        type="search"
        placeholder="搜索用户名 / 账号 / 手机号 / 节点 ID"
        @input="onKeyword"
      />
      <button v-if="keywordInput" type="button" class="ua-search-clear" @click="clearKeyword">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
          <path d="M18 6L6 18M6 6l12 12" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    <!-- 卡片列表 -->
    <div v-if="store.loading && !items.length" class="ua-empty">加载中…</div>
    <div v-else-if="!visibleItems.length" class="ua-empty">
      {{ tab === 'pending' ? '暂无待审核的注册用户' : '暂无已审核的注册用户' }}
    </div>
    <div v-else class="ua-list">
      <UserAuditCard
        v-for="r in visibleItems"
        :key="r.id"
        :review="r"
        :expanded="!!expanded[r.id]"
        :busy="busyId === r.id"
        @toggle="toggleExpand(r.id)"
        @approve="approveTarget = r"
        @reject="rejectTarget = r"
      />
    </div>

    <!-- 弹窗 -->
    <UserAuditRejectModal
      :review="rejectTarget"
      :busy="busyId !== null"
      @cancel="rejectTarget = null"
      @confirm="confirmReject"
    />
    <UserAuditApproveModal
      :review="approveTarget"
      :nodes="allNodes"
      :busy="busyId !== null"
      @cancel="approveTarget = null"
      @confirm="confirmApprove"
    />
  </div>
</template>

<style scoped>
/* 原型色板 → 本页局部 CSS 变量（不复用 Tailwind 类名） */
.ua-page {
  --ua-card: #101828;
  --ua-soft: #0c1220;
  --ua-deep: #0a101e;
  --ua-border: #1c2740;
  --ua-border-2: #22304a;
  --ua-text: #e6ebf4;
  --ua-muted: #8b96a8;
  --ua-dim: #5b6b85;
  --ua-blue: #2563eb;
  --ua-cyan: #38bdf8;
  --ua-green: #22c55e;
  --ua-amber: #f59e0b;
  --ua-amber-l: #fbbf24;
  --ua-red: #f6465d;

  max-width: 760px;
  margin: 0 auto;
  padding: 12px 12px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
@media (min-width: 1024px) {
  .ua-page { padding: 16px 16px 32px; }
}

/* ── 页头 ── */
.ua-hero {
  border-radius: 12px;
  border: 1px solid var(--ua-border);
  background: linear-gradient(135deg, #101828, #0e1a2e);
  padding: 14px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
}
@media (min-width: 1024px) {
  .ua-hero { padding: 16px; }
}
.ua-hero-top {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ua-hero-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 12px;
  background: linear-gradient(135deg, #38bdf8, #2563eb);
  color: #fff;
}
.ua-hero-icon svg { width: 19px; height: 19px; }
.ua-hero-titles { min-width: 0; }
.ua-hero-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--ua-text);
}
.ua-hero-help { width: 14px; height: 14px; color: var(--ua-cyan); }
.ua-hero-sub {
  margin: 2px 0 0;
  font-size: 10px;
  color: var(--ua-muted);
}

.ua-intro {
  margin-top: 12px;
  padding: 9px 11px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: var(--ua-deep);
  font-size: 10px;
  line-height: 1.7;
  color: var(--ua-muted);
}
.ua-intro-label {
  margin-right: 6px;
  font-weight: 700;
  color: var(--ua-cyan);
}

/* ── 统计块 ── */
.ua-stats {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.ua-stat {
  padding: 9px 11px;
  border-radius: 8px;
  border: 1px solid var(--ua-border-2);
  background: var(--ua-soft);
  min-width: 0;
}
.ua-stat-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 9px;
  color: var(--ua-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ua-stat-label svg { width: 12px; height: 12px; flex-shrink: 0; }
.ua-stat-value {
  margin-top: 3px;
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 700;
}

/* ── Tab ── */
.ua-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.ua-tabs {
  display: flex;
  gap: 6px;
}
.ua-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--ua-muted);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}
.ua-tab-count {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--ua-border-2);
  font-size: 9px;
  font-weight: 700;
  color: var(--ua-muted);
}
.ua-tab.is-pending.on {
  background: rgba(245, 158, 11, 0.15);
  color: var(--ua-amber-l);
}
.ua-tab.is-pending.on .ua-tab-count { background: var(--ua-amber); color: #0a101e; }
.ua-tab.is-audited.on {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
}
.ua-tab.is-audited.on .ua-tab-count { background: var(--ua-green); color: #0a101e; }

.ua-bulk {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: var(--ua-green);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}
.ua-bulk svg { width: 13px; height: 13px; }
.ua-bulk:hover:not(:disabled) { background: #16a34a; }
.ua-bulk:disabled { opacity: 0.45; cursor: not-allowed; }

/* ── 搜索 ── */
.ua-search {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid var(--ua-border);
  background: var(--ua-card);
}
.ua-search svg { width: 14px; height: 14px; color: var(--ua-dim); flex-shrink: 0; }
.ua-search input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--ua-text);
  font-size: 12px;
  outline: none;
}
.ua-search input::placeholder { color: var(--ua-dim); }
.ua-search-clear {
  flex-shrink: 0;
  display: flex;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--ua-dim);
  cursor: pointer;
}
.ua-search-clear svg { width: 13px; height: 13px; }
.ua-search-clear:hover { color: var(--ua-text); }

/* ── 列表 / 空态 ── */
.ua-list { display: flex; flex-direction: column; gap: 10px; }
.ua-empty {
  padding: 32px 16px;
  border-radius: 12px;
  border: 1px dashed var(--ua-border-2);
  text-align: center;
  font-size: 12px;
  color: var(--ua-dim);
}
</style>
