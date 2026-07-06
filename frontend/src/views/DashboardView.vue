<script setup lang="ts">
// 总览页：汇总统计 + 各节点账户/持仓卡片（实时）+ 远程平仓 + 事件流
import { computed, onMounted } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { AccountSnapshot, NodeOut } from '@/api/types'

const hub = useHubStore()

onMounted(async () => {
  await hub.fetchNodes()
})

function statusOf(n: NodeOut): string {
  return hub.statuses[n.node_id] || n.status
}
function acct(id: string): AccountSnapshot | undefined {
  return hub.accounts[id]
}
function fmt(n: number | undefined): string {
  return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}

const totalEquity = computed(() => hub.totalEquity)
const totalPositions = computed(() =>
  Object.values(hub.accounts).reduce((a, s) => a + (s?.positions?.length || 0), 0),
)

async function closeNodeAll(n: NodeOut): Promise<void> {
  if (!confirm(`确认平掉节点「${n.name}」的全部持仓？`)) return
  await hub.closeNode(n.node_id, { target: 'all' })
}
async function closeTicket(n: NodeOut, ticket: number): Promise<void> {
  if (!confirm(`平掉订单 #${ticket}？`)) return
  await hub.closeNode(n.node_id, { target: 'ticket', ticket })
}
</script>

<template>
  <div class="page-header">
    <div class="h1">实时总览</div>
  </div>

  <div class="grid cols-auto" style="margin-bottom: 16px">
    <div class="card card-pad stat"><span class="k">在线节点</span><span class="v">{{ hub.onlineCount }} / {{ hub.nodes.length }}</span></div>
    <div class="card card-pad stat"><span class="k">合计净值</span><span class="v">{{ fmt(totalEquity) }}</span></div>
    <div class="card card-pad stat"><span class="k">合计持仓</span><span class="v">{{ totalPositions }}</span></div>
  </div>

  <div class="grid layout-split">
    <div class="grid cols-auto">
      <div v-for="n in hub.nodes" :key="n.node_id" class="card card-pad">
        <div class="row between">
          <div class="row" style="gap: 8px">
            <span class="dot" :class="statusOf(n)"></span>
            <strong>{{ n.name }}</strong>
          </div>
          <span class="tag" :class="statusOf(n) === 'online' ? 'green' : ''">{{ statusOf(n) === 'online' ? '在线' : '离线' }}</span>
        </div>
        <div class="muted" style="font-size: 12px; margin: 4px 0 12px">
          {{ acct(n.node_id)?.login || n.mt5_login || '—' }} @ {{ acct(n.node_id)?.server || n.mt5_server || '—' }}
        </div>

        <div class="row node-metrics" style="gap: 18px; margin-bottom: 12px">
          <div class="stat"><span class="k">余额</span><span class="v" style="font-size: 15px">{{ fmt(acct(n.node_id)?.balance) }}</span></div>
          <div class="stat"><span class="k">净值</span><span class="v" style="font-size: 15px">{{ fmt(acct(n.node_id)?.equity) }}</span></div>
          <div class="stat"><span class="k">占用保证金</span><span class="v" style="font-size: 15px">{{ fmt(acct(n.node_id)?.margin) }}</span></div>
        </div>

        <div class="list-cards mobile-only" v-if="acct(n.node_id)?.positions?.length">
          <div v-for="p in acct(n.node_id)!.positions" :key="p.ticket" class="list-card-nested">
            <div class="list-field"><span class="k">品种</span><span class="v">{{ p.symbol }}</span></div>
            <div class="list-field">
              <span class="k">方向</span>
              <span class="v"><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></span>
            </div>
            <div class="list-field"><span class="k">手数</span><span class="v">{{ p.volume }}</span></div>
            <div class="list-field">
              <span class="k">盈亏</span>
              <span class="v" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</span>
            </div>
            <div class="list-card-actions" style="border-top: none; padding-top: 4px; margin-top: 0">
              <button class="btn-sm btn-ghost" @click="closeTicket(n, p.ticket)">平仓</button>
            </div>
          </div>
        </div>
        <table v-if="acct(n.node_id)?.positions?.length" class="desktop-only">
          <thead>
            <tr><th>品种</th><th>方向</th><th class="right">手数</th><th class="right">盈亏</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in acct(n.node_id)!.positions" :key="p.ticket">
              <td>{{ p.symbol }}</td>
              <td><span class="tag" :class="p.type === 'BUY' ? 'green' : 'blue'">{{ p.type }}</span></td>
              <td class="right">{{ p.volume }}</td>
              <td class="right" :class="p.profit >= 0 ? 'profit-pos' : 'profit-neg'">{{ fmt(p.profit) }}</td>
              <td class="right"><button class="btn-sm btn-ghost" @click="closeTicket(n, p.ticket)">平</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="muted" style="font-size: 12px; padding: 8px 0">无持仓</div>

        <div class="row" style="margin-top: 12px">
          <button class="btn-sm btn-danger" :disabled="!acct(n.node_id)?.positions?.length" @click="closeNodeAll(n)">平掉该节点全部</button>
        </div>
      </div>

      <div v-if="!hub.nodes.length" class="card card-pad muted">还没有节点，请到「节点」页面创建。</div>
    </div>

    <div class="card card-pad">
      <div class="row between" style="margin-bottom: 8px"><strong>实时事件</strong><span class="muted" style="font-size: 12px">{{ hub.events.length }}</span></div>
      <div class="events">
        <div v-for="(e, i) in hub.events" :key="i" class="event" :class="e.kind">
          <span class="ts">{{ new Date(e.ts).toLocaleTimeString() }}</span>
          <span class="text">{{ e.text }}</span>
        </div>
        <div v-if="!hub.events.length" class="muted" style="font-size: 12px">暂无事件</div>
      </div>
    </div>
  </div>
</template>
