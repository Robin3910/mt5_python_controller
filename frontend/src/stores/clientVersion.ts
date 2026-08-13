import { defineStore } from 'pinia'
import api from '@/api/client'
import type { ClientReleaseOut, ClientVersionListOut, ClientVersionOut } from '@/api/types'

const EMPTY_RELEASE: ClientReleaseOut = { version: '', previous: '', updated_at: 0 }

// 客户端版本管理独立成 store：hub.ts 已承载节点/分组/策略三条主线，不再往里堆
interface ClientVersionState {
  items: ClientVersionOut[]
  release: ClientReleaseOut
  unknownNodeCount: number
  loading: boolean
  /** 上传进度百分比，0 表示当前没有上传任务 */
  uploadProgress: number
}

export const useClientVersionStore = defineStore('clientVersion', {
  state: (): ClientVersionState => ({
    items: [],
    release: { ...EMPTY_RELEASE },
    unknownNodeCount: 0,
    loading: false,
    uploadProgress: 0,
  }),

  getters: {
    // 当前发布版本的完整条目（用于展示大小、校验和等）
    current: (s): ClientVersionOut | null =>
      s.items.find((i) => i.version === s.release.version) || null,
    canRollback: (s): boolean => Boolean(s.release.previous),
  },

  actions: {
    async fetch(): Promise<void> {
      this.loading = true
      try {
        const data: ClientVersionListOut = (await api.get('/api/client-versions')).data
        this.items = data.items || []
        this.release = data.release || { ...EMPTY_RELEASE }
        this.unknownNodeCount = data.unknown_node_count || 0
      } finally {
        this.loading = false
      }
    },

    /** 上传安装包。安装包动辄几十 MB，必须覆盖 axios 默认的 15 秒超时。 */
    async upload(file: File, opts?: { version?: string; notes?: string }): Promise<void> {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('version', opts?.version || '')
      fd.append('notes', opts?.notes || '')
      this.uploadProgress = 0
      try {
        await api.post('/api/client-versions', fd, {
          timeout: 0,
          onUploadProgress: (e) => {
            this.uploadProgress = e.total ? Math.round((e.loaded / e.total) * 100) : 0
          },
        })
        await this.fetch()
      } finally {
        this.uploadProgress = 0
      }
    },

    /** 设为当前发布版本；降级需 confirmDowngrade，否则后端返回 409。 */
    async release_(version: string, confirmDowngrade = false): Promise<void> {
      await api.post(`/api/client-versions/${encodeURIComponent(version)}/release`, {
        confirm_downgrade: confirmDowngrade,
      })
      await this.fetch()
    },

    async rollback(): Promise<void> {
      await api.post('/api/client-versions/rollback')
      await this.fetch()
    },

    async remove(version: string): Promise<void> {
      await api.delete(`/api/client-versions/${encodeURIComponent(version)}`)
      await this.fetch()
    },
  },
})
