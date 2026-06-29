import { defineStore } from 'pinia'
import api from '@/api/client'
import type {
  AccountSnapshot,
  CloseRequest,
  CloseBatchResult,
  DispatchConfig,
  FilterRulesConfig,
  HubEvent,
  LotConfig,
  NodeCreatePayload,
  NodeFeedItem,
  NodeOut,
  NodeTokenInfo,
  NodeUpdatePayload,
  PaginatedNodeDispatches,
} from '@/api/types'

// 业务总线 store：集中保存节点、账户、配置与实时事件
interface HubState {
  nodes: NodeOut[]                          // 节点列表（来自 REST，字段最全）
  accounts: Record<string, AccountSnapshot> // node_id -> 最新账户快照（实时 WS 更新）
  statuses: Record<string, string>          // node_id -> 在线状态（实时 WS 更新）
  lot: LotConfig                            // 全局手数
  dispatch: DispatchConfig                  // 分发模式 / 持仓范围
  filters: FilterRulesConfig          // 区间过滤
  events: HubEvent[]                        // 实时事件流（用于总览页展示）
  nodeFeed: Record<string, NodeFeedItem[]>  // node_id -> 实时分发/回报（详情页“成交回报”用）
}

export const useHubStore = defineStore('hub', {
  state: (): HubState => ({
    nodes: [],
    accounts: {},
    statuses: {},
    lot: { enabled: false, value: 0.1 },
    dispatch: { mode: 'sync', position_scope: 'symbol' },
    filters: {},
    events: [],
    nodeFeed: {},
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
    async fetchNodes(): Promise<void> {
      this.nodes = (await api.get('/api/nodes')).data
      for (const n of this.nodes) {
        if (!(n.node_id in this.statuses)) this.statuses[n.node_id] = n.status
      }
    },
    async fetchConfig(): Promise<void> {
      this.lot = (await api.get('/api/config/lot')).data
      this.dispatch = (await api.get('/api/config/dispatch')).data
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
    // ---- 节点增删改 ----
    async createNode(payload: NodeCreatePayload): Promise<NodeOut> {
      const created = (await api.post('/api/nodes', payload)).data
      await this.fetchNodes()
      return created
    },
    async updateNode(id: string, patch: NodeUpdatePayload): Promise<void> {
      await api.patch(`/api/nodes/${id}`, patch)
      await this.fetchNodes()
    },
    async deleteNode(id: string): Promise<void> {
      await api.delete(`/api/nodes/${id}`)
      await this.fetchNodes()
    },
    // ---- 配置保存 ----
    async saveLot(cfg: LotConfig): Promise<void> {
      this.lot = (await api.put('/api/config/lot', cfg)).data
    },
    async saveDispatch(cfg: DispatchConfig): Promise<void> {
      this.dispatch = (await api.put('/api/config/dispatch', cfg)).data
    },
    async saveFilters(cfg: FilterRulesConfig): Promise<void> {
      this.filters = (await api.put('/api/config/filters', cfg)).data
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
        if (d.lot) this.lot = d.lot as LotConfig
        if (d.dispatch) this.dispatch = d.dispatch as DispatchConfig
      } else if (t === 'node_status') {
        // 节点上下线
        const id = d.node_id as string
        this.statuses[id] = d.status as string
        this.pushEvent(`节点 ${id} ${d.status === 'online' ? '上线' : '下线'}`, d.status === 'online' ? 'ok' : 'warn')
      } else if (t === 'node_rejected') {
        // 重复登录被拒绝（同一节点同一时刻只允许一个在线）
        const reasonText: Record<string, string> = {
          already_online: '已有在线连接',
          mt5_login_mismatch: 'MT5 登录号不匹配',
        }
        const why = reasonText[d.reason as string] || (d.reason as string) || '未知原因'
        this.pushEvent(`节点 ${d.node_id} 重复登录被拒绝（${why}）`, 'warn')
      } else if (t === 'node_registered') {
        // 新节点首次登录被自动注册入库
        this.pushEvent(
          `节点 ${d.name || d.node_id} (MT5: ${d.mt5_login}) 已自动注册`,
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
      }
    },
  },
})
