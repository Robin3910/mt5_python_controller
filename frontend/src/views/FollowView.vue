<script setup lang="ts">
// 带单（跟单）页：左侧带单员列表，右侧「我的跟单」
// 能力语义：跟单默认人人可做；带单员由管理员授予 follow_leader 角色能力产生。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { fetchLeaders, fetchLeaderDetail, fetchMyFollowings, joinFollow, leaveFollow } from '@/api/follow'
import type {
  FollowDetailResponse,
  FollowLeaderSummary,
  FollowMeResponse,
} from '@/api/types'

/** 「我的跟单」聚合后的单条节点（已按 leader 聚合、按 node_id 去重） */
interface MyFollowedNode {
  node_id: string
  name: string | null
  mt5_login: number | null
  status: string | null
  joined_at: number | null
  /** 该节点在该 leader 下所有相关分组的活跃任务合计 */
  active_task_count: number
  /** 该节点来自哪个 group（去重后保留首条，用于移出接口） */
  group_id: string
}

/** 「我的跟单」聚合后的单条 leader 卡片数据 */
interface MyFollowingEntry {
  leader: FollowLeaderSummary
  marketable: boolean
  nodes: MyFollowedNode[]
}

const router = useRouter()

const leaders = ref<FollowLeaderSummary[]>([])
const loading = ref(false)
const keyword = ref('')

const detail = ref<FollowDetailResponse | null>(null)
const detailLoading = ref(false)
const selectedNodes = ref<string[]>([])
const submitting = ref(false)

const mine = ref<FollowMeResponse>({ following: [], summary: { following_count: 0, follow_node_count: 0 } })
const mineLoading = ref(false)

// 已选分组集合（分组列表已移除，保留以备将来可选分组场景）
// @ts-ignore
const _groupSet = computed(() => new Set<string>())

// 我的可跟单节点数（后端已剔除主节点）
const myNodeCount = computed(() => detail.value?.my_nodes.length ?? 0)

// ---------------------------------------------------------------------------
// 跟单统计（后续由历史订单数据驱动；目前使用 mock）
// ---------------------------------------------------------------------------
interface LeaderStats {
  total_profit: number      // 累计收益（USD）
  profit_rate: number       // 收益率（%，带单员账户）
  follower_count: number    // 当前跟单人数
  win_rate: number          // 胜率（%）
  total_trades: number      // 总交易笔数
  win_trades: number        // 盈利笔数
}

// TODO: 后续由历史订单数据驱动。存储结构：_MOCK_STATS[leaderId] = stats
// @ts-ignore
const _statsDb: Record<number, LeaderStats> = {}

function getMockStats(leaderId: number): LeaderStats {
  // 用 leaderId 的哈希模拟不同带单员数据，后续接入真实统计时替换为 store 查询
  const base = (leaderId * 7 + 13) % 100
  return {
    total_profit: parseFloat((base * 0.123).toFixed(2)),
    profit_rate: parseFloat(((base % 60) + 2).toFixed(1)),
    follower_count: (base % 20) + 1,
    win_rate: parseFloat(((base % 30) + 50).toFixed(1)),
    total_trades: (base + 1) * 10,
    win_trades: Math.round(((base % 30) + 50) / 100 * ((base + 1) * 10)),
  }
}

const currentStats = computed<LeaderStats | null>(() => {
  if (!detail.value) return null
  return getMockStats(detail.value.leader.leader_user_id)
})

const ZERO_STATS: LeaderStats = {
  total_profit: 0, profit_rate: 0, follower_count: 0, win_rate: 0,
  total_trades: 0, win_trades: 0,
}

/** 列表里每个带单员的统计（按 id 缓存，渲染时不再重复计算） */
const leaderStatsMap = computed<Record<number, LeaderStats>>(() => {
  const out: Record<number, LeaderStats> = {}
  for (const l of leaders.value) out[l.leader_user_id] = getMockStats(l.leader_user_id)
  return out
})

function statsOf(leaderId: number): LeaderStats {
  return leaderStatsMap.value[leaderId] ?? ZERO_STATS
}

function fmtProfit(v: number): string {
  return (v >= 0 ? '+' : '') + '$' + v.toFixed(2)
}
function fmtRate(v: number): string {
  return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'
}
function pctColor(v: number, pos = true): string {
  const ok = pos ? v >= 0 : v <= 0
  return ok ? 'var(--primary)' : 'var(--danger, #f56c6c)'
}

function errText(e: unknown, fallback: string): string {
  const ax = e as { response?: { data?: { detail?: string } } }
  return ax?.response?.data?.detail || fallback
}

async function loadLeaders() {
  loading.value = true
  try {
    leaders.value = await fetchLeaders(keyword.value.trim())
  } catch (e) {
    ElMessage.error(errText(e, '加载带单员失败'))
  } finally {
    loading.value = false
  }
}

async function loadMine() {
  mineLoading.value = true
  try {
    mine.value = await fetchMyFollowings()
  } catch (e) {
    ElMessage.error(errText(e, '加载我的跟单失败'))
  } finally {
    mineLoading.value = false
  }
}

async function openLeader(l: FollowLeaderSummary) {
  detailLoading.value = true
  selectedNodes.value = []
  try {
    detail.value = await fetchLeaderDetail(l.leader_user_id)
  } catch (e) {
    detail.value = null
    ElMessage.error(errText(e, '加载带单员详情失败'))
  } finally {
    detailLoading.value = false
  }
}

// 自动选所有可跟分组
const allSelectableGroupIds = computed<string[]>(() =>
  (detail.value?.groups ?? [])
    .filter((g) => g.selectable)
    .map((g) => g.group_id),
)

const nodeSet = computed(() => new Set(selectedNodes.value))

function toggleNode(id: string) {
  const s = new Set(selectedNodes.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedNodes.value = [...s]
}

async function submitJoin() {
  if (!detail.value || !allSelectableGroupIds.value.length || !selectedNodes.value.length) return
  submitting.value = true
  try {
    const res = await joinFollow({
      leader_user_id: detail.value.leader.leader_user_id,
      group_ids: allSelectableGroupIds.value,
      node_ids: selectedNodes.value,
    })
    const added = res.joined.reduce((n, j) => n + j.node_ids.length, 0)
    const already = res.joined.reduce((n, j) => n + j.already_node_ids.length, 0)
    let msg = `已加入 ${added} 个节点到 ${res.joined.length} 个分组`
    if (already) msg += `，${already} 个节点此前已跟`
    if (res.skipped.length) {
      msg += `；${res.skipped.length} 项被跳过：${res.skipped[0].reason}`
    }
    ElMessage.success(msg)
    selectedNodes.value = []
    await Promise.all([loadLeaders(), loadMine()])
  } catch (e) {
    ElMessage.error(errText(e, '加入跟单失败'))
  } finally {
    submitting.value = false
  }
}

async function doLeave(
  leader: FollowLeaderSummary, groupId: string,
  nodeIds: string[], closeTasks: boolean,
) {
  try {
    const res = await leaveFollow({
      leader_user_id: leader.leader_user_id,
      group_ids: [groupId],
      node_ids: nodeIds,
      close_tasks: closeTasks,
    })
    let msg = `已移出 ${res.removed.reduce((n, r) => n + r.node_ids.length, 0)} 个节点`
    if (res.closing.length) msg += `，${res.closing.length} 个任务正在平仓`
    if (res.forced.length) msg += `，${res.forced.length} 个离线节点已排队补发平仓`
    if (res.warning) ElMessage.warning(res.warning)
    ElMessage.success(msg)
    await Promise.all([loadLeaders(), loadMine()])
    if (detail.value?.leader.leader_user_id === leader.leader_user_id) {
      detail.value = await fetchLeaderDetail(leader.leader_user_id)
    }
  } catch (e) {
    ElMessage.error(errText(e, '移出跟单失败'))
  }
}

/**
 * 「我的跟单」按 leader 聚合、按 node_id 去重，分组不再展示。
 * 后端响应里仍嵌套 `groups`，此处扁平化为 leader → nodes。
 */
const myFollowing = computed<MyFollowingEntry[]>(() =>
  mine.value.following.map((f) => {
    const nodesById = new Map<string, MyFollowedNode>()
    for (const g of f.groups) {
      for (const n of g.my_nodes) {
        const prev = nodesById.get(n.node_id)
        if (prev) {
          prev.active_task_count += g.active_task_count
          // joined_at 取最早的那一条作为代表
          if (n.joined_at != null && (prev.joined_at == null || n.joined_at < prev.joined_at)) {
            prev.joined_at = n.joined_at
          }
        } else {
          nodesById.set(n.node_id, {
            node_id: n.node_id,
            name: n.name ?? null,
            mt5_login: n.mt5_login ?? null,
            status: n.status ?? null,
            joined_at: n.joined_at ?? null,
            active_task_count: g.active_task_count,
            group_id: g.group_id,
          })
        }
      }
    }
    return {
      leader: f.leader,
      marketable: f.leader.marketable,
      nodes: [...nodesById.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
    }
  }),
)

/** 反查：给 leader + nodeId 找到其所属的 group_id（聚合时记录的那条） */
function findGroupIdForNode(leader: FollowLeaderSummary, nodeId: string): string | null {
  const entry = myFollowing.value.find((x) => x.leader.leader_user_id === leader.leader_user_id)
  return entry?.nodes.find((n) => n.node_id === nodeId)?.group_id ?? null
}

/** 详情卡片打开时："我已跟随该 leader 的节点"集合 → 用于置灰 */
const alreadyFollowingNodeIds = computed<Set<string>>(() => {
  const ids = new Set<string>()
  if (!detail.value) return ids
  const entry = myFollowing.value.find(
    (x) => x.leader.leader_user_id === detail.value!.leader.leader_user_id,
  )
  if (entry) for (const n of entry.nodes) ids.add(n.node_id)
  return ids
})

async function confirmLeave(leader: FollowLeaderSummary, nodeId: string) {
  const groupId = findGroupIdForNode(leader, nodeId)
  if (!groupId) return
  const entry = myFollowing.value.find((x) => x.leader.leader_user_id === leader.leader_user_id)
  const node = entry?.nodes.find((n) => n.node_id === nodeId)
  const active = node?.active_task_count ?? 0
  if (!active) {
    await doLeave(leader, groupId, [nodeId], false)
    return
  }
  // 有进行中的任务：让用户在「平仓后移出」和「保留任务」之间二选一
  try {
    const choice = await ElMessageBox({
      title: '移出跟单',
      type: 'warning',
      dangerouslyUseHTMLString: true,
      message:
        `节点「${node?.name ?? nodeId}」在带单员「${leader.display_name || leader.username}」下还有 ${active} 个进行中的任务。<br/>` +
        '移出后该带单员不再向它下发新信号，现有任务如何处理？',
      distinguishCancelAndClose: true,
      confirmButtonText: '平仓并关闭任务后移出',
      cancelButtonText: '保留任务，仅停止接收新信号',
    })
      .then((r) => r.value)
      .catch((r) => (r === 'cancel' ? 'keep' : 'close'))
    if (choice === 'close') return
    await doLeave(leader, groupId, [nodeId], choice === 'confirm')
  } catch {
    // 关闭弹窗：不处理
  }
}

onMounted(() => {
  void loadLeaders()
  void loadMine()
})
</script>

<template>
  <div class="follow-page">
    <div class="row between page-header">
      <div>
        <h2 class="page-title">带单广场</h2>
        <!-- <p class="muted page-sub">把你的非主节点加入带单员的分组，接收他触发的信号。</p> -->
      </div>
      <!-- <RouterLink to="/nodes" class="btn-ghost btn-sm">管理我的节点</RouterLink> -->
    </div>

    <div class="follow-grid">
      <!-- 左：带单员列表 -->
      <section class="card card-pad">
        <div class="row between" style="margin-bottom: 12px">
          <strong>带单员</strong>
          <span class="muted" style="font-size: 12px">{{ leaders.length }} 人开放</span>
        </div>
        <input
          v-model="keyword"
          class="inp"
          placeholder="搜索用户名 / 显示名 / 主节点名 / MT5 号"
          style="width: 100%; margin-bottom: 12px"
          @keyup.enter="loadLeaders"
        />
        <div v-if="loading" class="muted" style="padding: 12px 0">加载中...</div>
        <div v-else-if="!leaders.length" class="muted empty">
          当前没有开放跟单的带单员
        </div>
        <ul v-else class="leader-list">
          <li
            v-for="l in leaders"
            :key="l.leader_user_id"
            class="leader-item"
            :class="{ active: detail?.leader.leader_user_id === l.leader_user_id }"
            @click="openLeader(l)"
          >
            <div class="row between">
              <strong>{{ l.display_name || l.username }}</strong>
              <span v-if="l.my_follow_node_count" class="tag green">
                已跟
              </span>
              <span v-else class="tag">未跟</span>
            </div>

            <!-- 4 项统计（后续由历史订单数据驱动） -->
            <div class="leader-stats">
              <div class="ls-item">
                <span class="ls-k">累计收益</span>
                <span class="ls-v" :style="{ color: pctColor(statsOf(l.leader_user_id).total_profit) }">
                  {{ fmtProfit(statsOf(l.leader_user_id).total_profit) }}
                </span>
              </div>
              <div class="ls-item">
                <span class="ls-k">收益率</span>
                <span class="ls-v" :style="{ color: pctColor(statsOf(l.leader_user_id).profit_rate) }">
                  {{ fmtRate(statsOf(l.leader_user_id).profit_rate) }}
                </span>
              </div>
              <div class="ls-item">
                <span class="ls-k">跟单人数</span>
                <span class="ls-v">{{ statsOf(l.leader_user_id).follower_count }} 人</span>
              </div>
              <div class="ls-item">
                <span class="ls-k">胜率</span>
                <span class="ls-v">{{ statsOf(l.leader_user_id).win_rate }}%</span>
              </div>
            </div>
          </li>
        </ul>
      </section>

      <!-- 右：详情 / 我的跟单 -->
      <section class="card card-pad">
        <div v-if="detailLoading" class="muted" style="padding: 12px 0">加载中...</div>

        <template v-else-if="detail">
          <div class="row between" style="margin-bottom: 4px">
            <strong>跟单 · {{ detail.leader.display_name || detail.leader.username }}</strong>
            <button class="btn-sm btn-ghost" @click="detail = null">返回列表</button>
          </div>

          <!-- 跟单统计卡片（后续由历史订单数据驱动） -->
          <div v-if="currentStats" class="stats-grid">
            <div class="stat-card">
              <div class="stat-label">累计收益</div>
              <div class="stat-value" :style="{ color: pctColor(currentStats.total_profit) }">
                {{ fmtProfit(currentStats.total_profit) }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">收益率</div>
              <div class="stat-value" :style="{ color: pctColor(currentStats.profit_rate) }">
                {{ fmtRate(currentStats.profit_rate) }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">跟单人数</div>
              <div class="stat-value">{{ currentStats.follower_count }} 人</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">胜率</div>
              <div class="stat-value">{{ currentStats.win_rate }}%</div>
            </div>
          </div>

          <h4 class="sec-title">我的节点（可多选）</h4>
          <p v-if="!detail.my_nodes.length" class="muted empty">
            你没有可用于跟单的节点（主节点不可跟单）
            <button class="btn-sm btn-ghost" @click="router.push('/nodes')">去节点页</button>
          </p>
          <ul v-else class="pick-list">
            <li
              v-for="n in detail.my_nodes"
              :key="n.node_id"
              class="pick-item"
              :class="{
                on: !alreadyFollowingNodeIds.has(n.node_id) && nodeSet.has(n.node_id),
                disabled: alreadyFollowingNodeIds.has(n.node_id),
              }"
              @click="alreadyFollowingNodeIds.has(n.node_id) ? null : toggleNode(n.node_id)"
            >
              <label>
                <input
                  type="checkbox"
                  :checked="!alreadyFollowingNodeIds.has(n.node_id) && nodeSet.has(n.node_id)"
                  :disabled="alreadyFollowingNodeIds.has(n.node_id)"
                  @click.stop
                  @change="alreadyFollowingNodeIds.has(n.node_id) ? null : toggleNode(n.node_id)"
                />
                <span class="pick-name">{{ n.name || n.node_id }}</span>
              </label>
              <span class="muted">MT5 {{ n.mt5_login ?? '—' }}</span>
              <span v-if="alreadyFollowingNodeIds.has(n.node_id)" class="tag green">已跟随</span>
            </li>
          </ul>

          <div class="join-bar">
            <span class="muted">
              已选：{{ selectedNodes.length }} 个节点
              <template v-if="myNodeCount"> · 可用节点 {{ myNodeCount }}</template>
            </span>
            <button
              class="btn-sm btn-primary"
              :disabled="!selectedNodes.length || submitting"
              @click="submitJoin"
            >
              {{ submitting ? '提交中...' : '确认跟单' }}
            </button>
          </div>

        </template>

        <template v-else>
          <div class="row between" style="margin-bottom: 12px">
            <strong>我的跟单</strong>
          </div>
          <div v-if="mineLoading" class="muted" style="padding: 12px 0">加载中...</div>
          <div v-else-if="!mine.following.length" class="muted empty">
            还没有跟单，从左侧选择一个带单员开始
          </div>
          <div v-else class="mine-list">
            <div v-for="f in myFollowing" :key="f.leader.leader_user_id" class="mine-block">
              <div class="row between mine-head">
                <strong>
                  {{ f.leader.display_name || f.leader.username }}
                  <span class="muted" style="font-weight: 400">
                    · {{ f.leader.primary_node?.name }}
                    <template v-if="f.leader.primary_node?.mt5_login">
                      （MT5 {{ f.leader.primary_node.mt5_login }}）
                    </template>
                  </span>
                </strong>
                <span v-if="!f.marketable" class="tag amber">该带单员已关闭带单</span>
              </div>
              <p
                v-if="!f.nodes.length"
                class="muted"
                style="margin: 4px 0 0; font-size: 12px"
              >
                暂未跟单任何节点
              </p>
              <div v-else>
                <div v-for="n in f.nodes" :key="n.node_id" class="mine-node">
                  <span class="muted">
                    {{ n.name || n.node_id }}
                    <template v-if="n.mt5_login">（MT5 {{ n.mt5_login }}）</template>
                    · 加入于 {{ n.joined_at ? new Date(n.joined_at * 1000).toLocaleString('zh-CN') : '—' }}
                    <template v-if="n.active_task_count">
                      · 运行中 {{ n.active_task_count }} 单
                    </template>
                  </span>
                  <button class="btn-sm btn-ghost" @click="confirmLeave(f.leader, n.node_id)">
                    移出
                  </button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.follow-page {
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
}

.page-title {
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 4px;
}

.page-sub {
  font-size: 13px;
  margin: 0;
  line-height: 1.5;
}

.follow-grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}

@media (max-width: 900px) {
  .follow-grid {
    grid-template-columns: 1fr;
  }
}

.leader-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 60vh;
  overflow-y: auto;
}

.leader-item {
  padding: 10px 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--transition), background var(--transition);
}

.leader-item:hover {
  border-color: var(--primary);
}

.leader-item.active {
  border-color: var(--primary);
  background: var(--glass);
}

.leader-sub {
  font-size: 12px;
  margin-top: 4px;
}

/* 带单员列表里的 4 项统计：窄栏用 2×2，比横排 4 列易读 */
.leader-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 10px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--glass-border);
}

.ls-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
}

.ls-k {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}

.ls-v {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.sec-title {
  font-size: 13px;
  margin: 16px 0 8px;
  color: var(--muted);
}

/* 跟单统计卡片 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 4px;
}

.stat-card {
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  padding: 10px 12px;
  text-align: center;
}

.stat-label {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
  letter-spacing: 0.04em;
}

.stat-value {
  font-size: 18px;
  font-weight: 700;
  line-height: 1.2;
}

/* 手机端：2×2 网格，减小字体 */
@media (max-width: 480px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }
  .stat-value {
    font-size: 15px;
  }
  .stat-label {
    font-size: 10px;
  }
  .stat-card {
    padding: 8px 8px;
  }
}

.pick-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pick-item {
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 13px;
}

.pick-item:hover {
  border-color: var(--primary);
}

.pick-item.on {
  border-color: var(--primary);
  background: var(--glass);
}

.pick-item.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.pick-item label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: inherit;
  margin-right: 10px;
}

.pick-name {
  font-weight: 600;
}

.pick-sub {
  font-size: 12px;
  margin-top: 4px;
  margin-left: 24px;
}

.join-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--glass-border);
  flex-wrap: wrap;
}

.warn {
  font-size: 12px;
  color: var(--muted);
  margin: 10px 0 0;
  line-height: 1.6;
}

.empty {
  padding: 20px 0;
  text-align: center;
}

.ok {
  color: var(--primary);
}

.off {
  color: var(--muted);
}

.mine-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.mine-block {
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  padding: 12px;
}

.mine-head {
  margin-bottom: 8px;
}

.mine-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 0 0 12px;
  font-size: 12px;
}
</style>