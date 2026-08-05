import { defineStore } from 'pinia'
import api from '@/api/client'
import type {
  AccountSnapshot,
  CloseRequest,
  CloseBatchResult,
  FilterRulesConfig,
  GroupCreatePayload,
  GroupOut,
  GroupUpdatePayload,
  HubEvent,
  ManualSignalPayload,
  ManualSignalResult,
  PurgeTradeLogsResult,
  NodeCreatePayload,
  NodeFeedItem,
  NodeOut,
  NodeTokenInfo,
  NodeUpdatePayload,
  RiskFeedItem,
  GroupTaskEventRecord,
  PaginatedAudits,
  PaginatedGroupSignals,
  PaginatedNodeDispatches,
  PaginatedSignalEvents,
  StrategyCreatePayload,
  StrategyOut,
  StrategyTemplateOut,
  StrategyUpdatePayload,
} from '@/api/types'

// 业务总线 store：集中保存节点、账户、配置与实时事件
interface HubState {
  nodes: NodeOut[]                          // 节点列表（来自 REST，字段最全）
  groups: GroupOut[]                        // 分组列表（strategy 信号的分发单元）
  strategies: StrategyOut[]                 // 策略实例列表（基于模版创建）
  accounts: Record<string, AccountSnapshot> // node_id -> 最新账户快照（实时 WS 更新）
  statuses: Record<string, string>          // node_id -> 在线状态（实时 WS 更新）
  filters: FilterRulesConfig          // 区间过滤
  events: HubEvent[]                        // 实时事件流（用于总览页展示）
  nodeFeed: Record<string, NodeFeedItem[]>  // node_id -> 实时分发/回报（详情页“成交回报”用）
  riskFeed: Record<string, RiskFeedItem[]>  // node_id -> 账户级风控执行回报
}

export const useHubStore = defineStore('hub', {
  state: (): HubState => ({
    nodes: [],
    groups: [],
    strategies: [],
    accounts: {},
    statuses: {},
    filters: {},
    events: [],
    nodeFeed: {},
    riskFeed: {},
  }),
  getters: {
    // 在线节点数（优先用实时状态，其次用 REST 字段）
    onlineCount: (s): number =>
      s.nodes.filter((n) => s.statuses[n.node_id] === 'online' || n.status === 'online').length,
    // 所有节点净值合计
    totalEquity: (s): number =>
      Object.values(s.accounts).reduce((acc, a) => acc + (a?.equity || 0), 0),
  },
  actions: {
    // ---- REST 拉取 ----
    async fetchNodes(options?: { q?: string }): Promise<void> {
      const q = options?.q?.trim()
      const params = q ? { q } : undefined
      this.nodes = (await api.get('/api/nodes', { params })).data
      for (const n of this.nodes) {
        if (!(n.node_id in this.statuses)) this.statuses[n.node_id] = n.status
      }
    },
    async fetchConfig(): Promise<void> {
      this.filters = (await api.get('/api/config/filters')).data
    },
    // 拉取单节点最新账户快照（详情页兜底；之后由 WS 实时刷新）
    async fetchNodeAccount(id: string): Promise<void> {
      try {
        this.accounts[id] = (await api.get(`/api/nodes/${id}/account`)).data
      } catch {
        /* 404 = 该节点尚未上报过快照，忽略 */
      }
    },
    // 拉取单节点的分发/成交历史（持久化）
    async fetchNodeDispatches(
      id: string,
      page = 1,
      pageSize = 20,
    ): Promise<PaginatedNodeDispatches> {
      try {
        return (
          await api.get(`/api/nodes/${id}/dispatches`, {
            params: { page, page_size: pageSize },
          })
        ).data
      } catch {
        return { items: [], total: 0, page, page_size: pageSize }
      }
    },
    async fetchSignalEvents(page = 1, pageSize = 20): Promise<PaginatedSignalEvents> {
      try {
        return (
          await api.get('/api/events/signals', {
            params: { page, page_size: pageSize },
          })
        ).data
      } catch {
        return { items: [], total: 0, page, page_size: pageSize }
      }
    },
    async fetchAudits(
      page = 1,
      pageSize = 20,
      category?: string | null,
    ): Promise<PaginatedAudits> {
      try {
        return (
          await api.get('/api/audits', {
            params: {
              page,
              page_size: pageSize,
              ...(category ? { category } : {}),
            },
          })
        ).data
      } catch {
        return { items: [], total: 0, page, page_size: pageSize }
      }
    },
    // ---- 节点增删改 ----
    async createNode(payload: NodeCreatePayload, options?: { q?: string }): Promise<NodeOut> {
      const created = (await api.post('/api/nodes', payload)).data
      await this.fetchNodes(options)
      return created
    },
    async updateNode(id: string, patch: NodeUpdatePayload, options?: { q?: string }): Promise<void> {
      await api.patch(`/api/nodes/${id}`, patch)
      await this.fetchNodes(options)
    },
    async deleteNode(id: string, options?: { q?: string }): Promise<void> {
      await api.delete(`/api/nodes/${id}`)
      await this.fetchNodes(options)
    },
    // ---- 分组增删改查（strategy 信号链路）----
    async fetchGroups(options?: { q?: string }): Promise<void> {
      const q = options?.q?.trim()
      const params = q ? { q } : undefined
      this.groups = (await api.get('/api/groups', { params })).data
    },
    async createGroup(payload: GroupCreatePayload, options?: { q?: string }): Promise<GroupOut> {
      const created = (await api.post('/api/groups', payload)).data
      await this.fetchGroups(options)
      return created
    },
    async updateGroup(
      id: string,
      patch: GroupUpdatePayload,
      options?: { q?: string },
    ): Promise<void> {
      await api.patch(`/api/groups/${id}`, patch)
      await this.fetchGroups(options)
    },
    async deleteGroup(id: string, options?: { q?: string }): Promise<void> {
      await api.delete(`/api/groups/${id}`)
      await this.fetchGroups(options)
    },
    /** 取全量分组但不写入 state：供手动触发弹窗预演命中范围，不受列表搜索条件影响 */
    async listGroups(): Promise<GroupOut[]> {
      return (await api.get('/api/groups')).data
    },
    // ---- 策略增删改查（基于模版的加仓规则实例）----
    async fetchStrategyTemplates(): Promise<StrategyTemplateOut[]> {
      return (await api.get('/api/strategies/templates')).data
    },
    async fetchStrategies(options?: { q?: string }): Promise<void> {
      const q = options?.q?.trim()
      const params = q ? { q } : undefined
      this.strategies = (await api.get('/api/strategies', { params })).data
    },
    async createStrategy(
      payload: StrategyCreatePayload,
      options?: { q?: string },
    ): Promise<StrategyOut> {
      const created = (await api.post('/api/strategies', payload)).data
      await this.fetchStrategies(options)
      return created
    },
    async updateStrategy(
      id: string,
      patch: StrategyUpdatePayload,
      options?: { q?: string },
    ): Promise<void> {
      await api.patch(`/api/strategies/${id}`, patch)
      await this.fetchStrategies(options)
    },
    async deleteStrategy(id: string, options?: { q?: string }): Promise<void> {
      await api.delete(`/api/strategies/${id}`)
      await this.fetchStrategies(options)
    },
    // 分组已处理的信号明细（主任务 + 各节点处理过程）
    async fetchGroupSignals(
      id: string,
      page = 1,
      pageSize = 20,
      status?: string,
    ): Promise<PaginatedGroupSignals> {
      try {
        const params: Record<string, string | number> = { page, page_size: pageSize }
        if (status) params.status = status
        return (
          await api.get(`/api/groups/${id}/signals`, { params })
        ).data
      } catch {
        return { items: [], total: 0, page, page_size: pageSize }
      }
    },
    /** 节点策略子任务的关联订单/事件流 */
    async fetchGroupDispatchEvents(
      groupId: string,
      dispatchId: number,
    ): Promise<GroupTaskEventRecord[]> {
      try {
        return (
          await api.get(`/api/groups/${groupId}/dispatches/${dispatchId}/events`)
        ).data
      } catch {
        return []
      }
    },
    /** 手动终止单个节点策略子任务（下发 strategy_stop） */
    async closeGroupDispatch(
      groupId: string,
      dispatchId: number,
    ): Promise<{ status: string; node_id?: string; reason?: string }> {
      return (await api.post(`/api/groups/${groupId}/dispatches/${dispatchId}/close`)).data
    },
    // ---- 配置保存 ----
    async saveFilters(cfg: FilterRulesConfig): Promise<void> {
      this.filters = (await api.put('/api/config/filters', cfg)).data
    },
    // ---- 中控台手动触发信号（复用 Webhook 分发流程）----
    async triggerManualSignal(payload: ManualSignalPayload): Promise<ManualSignalResult> {
      return (await api.post('/api/console/manual-signal', payload)).data
    },
    /** 清空全部交易日志表与记录表（分组/策略/节点配置保留） */
    async purgeTradeLogs(confirm: string): Promise<PurgeTradeLogsResult> {
      return (await api.post('/api/console/purge-trade-logs', { confirm })).data
    },
    // ---- 全局节点接入令牌（账户设置）----
    async fetchNodeToken(): Promise<NodeTokenInfo> {
      return (await api.get('/api/config/node-token')).data
    },
    async rotateNodeToken(): Promise<NodeTokenInfo> {
      return (await api.post('/api/config/node-token/rotate')).data
    },
    // ---- 远程平仓 ----
    async closeNode(id: string, body: CloseRequest): Promise<void> {
      await api.post(`/api/nodes/${id}/close`, body)
    },
    async closeBatch(nodeIds: string[], body: CloseRequest): Promise<CloseBatchResult> {
      return (
        await api.post('/api/close-batch', {
          node_ids: nodeIds,
          ...body,
        })
      ).data
    },
    // ---- 事件流 ----
    pushEvent(text: string, kind: HubEvent['kind'] = 'info'): void {
      this.events.unshift({ ts: Date.now(), text, kind })
      if (this.events.length > 120) this.events.pop()  // 限制长度，避免无限增长
    },
    // 按 signal_id 原地更新某节点的实时回报条目（同一信号的 dispatch→trade_result 合并为一行）
    upsertFeed(nodeId: string, item: NodeFeedItem): void {
      if (!nodeId) return
      const list = this.nodeFeed[nodeId] ? [...this.nodeFeed[nodeId]] : []
      const i = list.findIndex((x) => x.signal_id === item.signal_id)
      if (i >= 0) list[i] = { ...list[i], ...item }
      else list.unshift(item)
      this.nodeFeed[nodeId] = list.slice(0, 100)  // 每节点最多保留 100 条
    },
    // 处理来自后台 WS 的实时消息，按 type 分发更新本地状态
    applyWs(msg: { type: string; data?: Record<string, unknown> }): void {
      const t = msg.type
      const d = (msg.data || {}) as Record<string, unknown>
      if (t === 'snapshot') {
        // 连接建立后的全量快照
        const nodes = (d.nodes || []) as Array<Record<string, unknown>>
        for (const n of nodes) {
          const id = n.node_id as string
          this.statuses[id] = n.status as string
          if (n.account) this.accounts[id] = n.account as AccountSnapshot
        }
      } else if (t === 'node_status') {
        // 节点上下线
        const id = d.node_id as string
        this.statuses[id] = d.status as string
        this.pushEvent(`节点 ${id} ${d.status === 'online' ? '上线' : '下线'}`, d.status === 'online' ? 'ok' : 'warn')
      } else if (t === 'node_rejected') {
        // 节点被拒绝（重复在线 / 登录号不符等）
        const reasonText: Record<string, string> = {
          already_online: '已有在线连接',
          mt5_login_mismatch: 'MT5 登录号不匹配（终端换号）',
        }
        const why = reasonText[d.reason as string] || (d.reason as string) || '未知原因'
        this.pushEvent(`节点 ${d.node_id} 接入被拒绝（${why}）`, 'warn')
      } else if (t === 'node_registered') {
        // 新节点首次登录被自动注册入库
        this.pushEvent(
          `节点 ${d.name || d.node_id} (MT5: ${d.mt5_login}) 已自动注册（默认禁用，请启用后接入）`,
          'ok',
        )
        // 拉取最新节点列表，让侧栏/列表实时刷新
        this.fetchNodes().catch(() => {})
      } else if (t === 'account') {
        // 账户快照更新
        this.accounts[d.node_id as string] = d as unknown as AccountSnapshot
      } else if (t === 'dispatch') {
        // 一次分发动作
        const reason = d.reason ? `(${d.reason})` : ''
        this.pushEvent(`分发 ${d.symbol || ''} ${d.action || ''} → ${d.node_id} ${d.status}${reason}`)
        this.upsertFeed(d.node_id as string, {
          signal_id: (d.signal_id as string) || '',
          ts: Date.now(),
          symbol: d.symbol as string | undefined,
          action: d.action as string | undefined,
          status: (d.status as string) || 'pending',
          volume: d.volume as number | undefined,
          sl: d.sl as number | undefined,
          tp: d.tp as number | undefined,
          reason: d.reason as string | undefined,
        })
      } else if (t === 'group_dispatch') {
        // strategy 分组分发：一条主任务向某个节点的下发结果
        const reason = d.reason ? `(${d.reason})` : ''
        this.pushEvent(
          `分组 ${d.group_name || d.group_id} 任务 #${d.task_id} ${d.symbol || ''} ${d.action || ''} → ${d.node_id} ${d.status}${reason}`,
          d.status === 'offline' ? 'warn' : 'info',
        )
      } else if (t === 'trade_result') {
        // 成交回报
        const ok = !!d.success
        this.pushEvent(`回报 ${d.node_id} ${ok ? '成功' : '失败'} ${d.symbol || ''} ${d.error || ''}`, ok ? 'ok' : 'warn')
        this.upsertFeed(d.node_id as string, {
          signal_id: (d.signal_id as string) || '',
          ts: Date.now(),
          symbol: d.symbol as string | undefined,
          action: d.action as string | undefined,
          status: ok ? 'done' : 'failed',
          volume: d.volume as number | undefined,
          price: d.price as number | undefined,
          order: (d.order as number | undefined) ?? (d.ticket as number | undefined),
          error: d.error as string | undefined,
          detail: d.detail as string | undefined,
        })
      } else if (t === 'risk_event') {
        // 账户级风控触发 / 状态回写
        const nodeId = d.node_id as string
        const ok = d.success !== false
        const text = (d.message as string) || `风控 ${d.rule || ''} ${d.event || ''}`
        this.pushEvent(`风控 ${nodeId} ${text}`, 'warn')
        if (nodeId) {
          const item: RiskFeedItem = {
            ts: Date.now(),
            rule: d.rule as string | undefined,
            event: d.event as string | undefined,
            message: text,
            success: ok,
            remaining_times: d.remaining_times as number | undefined,
            disabled: d.disabled as boolean | undefined,
            current_ratio: d.current_ratio as number | undefined,
            ratio_threshold: d.ratio_threshold as number | undefined,
          }
          const list = this.riskFeed[nodeId] ? [item, ...this.riskFeed[nodeId]] : [item]
          this.riskFeed[nodeId] = list.slice(0, 50)
          if (d.risk) {
            const idx = this.nodes.findIndex((n) => n.node_id === nodeId)
            if (idx >= 0) {
              const nodes = [...this.nodes]
              nodes[idx] = { ...nodes[idx], risk: d.risk as NodeOut['risk'] }
              this.nodes = nodes
            }
          }
        }
      }
    },
  },
})
