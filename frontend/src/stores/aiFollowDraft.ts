import { defineStore } from 'pinia'
import type { AiFollowSettings } from '@/api/types'

export const useAiFollowDraftStore = defineStore('ai-follow-draft', {
  state: () => ({
    userId: 0,
    leaderId: 0,
    groupId: '',
    nodeIds: [] as string[],
    step: 1 as 1 | 2,
    settings: { risk_mode: 'leader', loss_ratio: null, run_mode: 'loop' } as AiFollowSettings,
  }),
  actions: {
    select(userId: number, leaderId: number, groupId: string, nodeIds: string[]): void {
      if (this.userId !== userId || this.leaderId !== leaderId || this.groupId !== groupId) this.$reset()
      this.userId = userId
      this.leaderId = leaderId
      this.groupId = groupId
      this.nodeIds = [...nodeIds]
    },
  },
})
