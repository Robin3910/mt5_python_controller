import { defineStore } from 'pinia'
import api from '@/api/client'
import type {
  UserReviewList,
  UserReviewOut,
  UserReviewStatus,
} from '@/api/types'

interface UserReviewState {
  list: UserReviewList
  loading: boolean
  /** 状态筛选；'all' = 不筛（三种状态全取） */
  status: UserReviewStatus | 'all'
  keyword: string
}

function emptyList(): UserReviewList {
  return {
    stats: {
      total: 0,
      pending: 0,
      approved: 0,
      rejected: 0,
      node_total: 0,
      node_online: 0,
    },
    items: [],
  }
}

/**
 * 注册用户 KYC 实名审核。
 *
 * 筛选是**后端**做的（status / keyword 一起发给接口），不是前端过滤 ——
 * 统计块要反映全局数，前端过滤会让 Tab 徽章与列表对不上。
 */
export const useUserReviewStore = defineStore('userReview', {
  state: (): UserReviewState => ({
    list: emptyList(),
    loading: false,
    status: 'pending',
    keyword: '',
  }),
  getters: {
    /** 待审核数量（全局数，不受筛选影响） */
    pendingCount: (s): number => s.list.stats.pending,
    approvedCount: (s): number => s.list.stats.approved,
    /** 已审核 = 已通过 + 已驳回 */
    auditedCount: (s): number => s.list.stats.approved + s.list.stats.rejected,
  },
  actions: {
    /** 按当前 status / keyword 拉列表 */
    async fetchReviews(): Promise<void> {
      this.loading = true
      try {
        const params: Record<string, string> = {}
        if (this.status && this.status !== 'all') params.status = this.status
        if (this.keyword.trim()) params.keyword = this.keyword.trim()
        const { data } = await api.get<UserReviewList>('/api/user-reviews', { params })
        this.list = data
      } finally {
        this.loading = false
      }
    },

    /** 取全部三种状态（供「已审核」Tab 本地分档用） */
    async fetchAll(): Promise<void> {
      this.status = 'all'
      await this.fetchReviews()
    },

    async setStatus(status: UserReviewStatus | 'all'): Promise<void> {
      this.status = status
      await this.fetchReviews()
    },

    /**
     * 审核通过。`nodeIds` 非空 = 同时把节点分配给该用户。
     *
     * 后端分配节点时可能返回 409（节点有活动子任务 / 仍在分组里），
     * 这类错误**原样抛给调用方**，由页面弹确认后带 confirm 重试。
     */
    async approve(id: number, nodeIds: string[] = [], confirm = false): Promise<void> {
      await api.post(`/api/user-reviews/${id}/approve`, {
        node_ids: nodeIds,
        confirm,
      })
      await this.fetchReviews()
    },

    /** 审核驳回（必须带原因） */
    async reject(id: number, reason: string): Promise<void> {
      await api.post(`/api/user-reviews/${id}/reject`, { reason })
      await this.fetchReviews()
    },

    /** 批量通过；ids 为空 = 全部 pending。返回 {approved, failed[]} */
    async bulkApprove(ids: number[] = []): Promise<{ approved: number; failed: unknown[] }> {
      const { data } = await api.post<{ approved: number; failed: unknown[] }>(
        '/api/user-reviews/bulk-approve',
        { review_ids: ids },
      )
      await this.fetchReviews()
      return data
    },
  },
})

export type { UserReviewOut }
