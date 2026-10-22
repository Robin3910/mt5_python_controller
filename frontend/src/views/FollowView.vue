<script setup lang="ts">
// 带单（跟单）页：左侧带单员列表，右侧「我的跟单」
// 能力语义：跟单默认人人可做；带单员由管理员授予 follow_leader 角色能力产生。
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElDialog, ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
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
  /** 该节点被跟随的**全部分组**（同一 leader 下可能同时跟多个分组，移出时必须一次性清掉） */
  groups: { group_id: string; group_name: string | null }[]
}

/** 「我的跟单」聚合后的单条 leader 卡片数据 */
interface MyFollowingEntry {
  leader: FollowLeaderSummary
  marketable: boolean
  /** 不可跟单的人读原因（带单员已撤销 / 切换主节点 / 不在本市场等） */
  unmarketable_reason?: string | null
  nodes: MyFollowedNode[]
  /** 我在该 leader 下跟的分组（分组级活跃任务数只此一份，不能按节点重复累加） */
  groups: { group_id: string; group_name: string | null; active_task_count: number }[]
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
// 带单员绩效（来自历史成交聚合，与订单历史页同一套口径）
// ---------------------------------------------------------------------------
//
// **没有 mock**：绩效全部读后端返回的 `leader.performance`。
// 后端预聚合在 Redis 里（deal_agg），前端只读缓存，不自己算 ——
// 统计口径唯一化是硬要求：两个页面胜率不一致会直接摧毁数据可信度。
//
// 「暂无数据」与「0」是两回事：`data_available=false` 表示该带单员名下
// 节点未上线 / 无成交 / 同步未开，UI 要明示「暂无数据」而不是显示 0。

/** 统计窗口（四档，与后端 deal_stats.WINDOWS 对齐） */
const WINDOW_OPTIONS = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: '90d', label: '近 90 天' },
  { value: 'all', label: '全部' },
] as const
const perfWindow = ref<string>('30d')

/** 列表里每个带单员的绩效（后端已算好，这里只做取值） */
function statsOf(leader: FollowLeaderSummary): FollowLeaderSummary['performance'] {
  return leader.performance
}

/** 跟单人数：来自跟单关系表（与订单数据无关，可独立成立） */
function followerCountOf(leader: FollowLeaderSummary): number {
  return leader.follower_count || leader.follower_node_count || 0
}

/** 当前详情页的绩效 */
const currentStats = computed<FollowLeaderSummary['performance'] | null>(() => {
  if (!detail.value) return null
  return detail.value.leader.performance
})

/** 当前详情页的跟单人数 */
const currentFollowerCount = computed<number>(() => {
  if (!detail.value) return 0
  return followerCountOf(detail.value.leader)
})

/** 数据新鲜度：成交是准实时的，绩效必然滞后，UI 必须标注 */
function updatedText(leader: FollowLeaderSummary): string {
  const ts = leader.performance?.updated_at
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `数据更新于 ${pad(d.getHours())}:${pad(d.getMinutes())}`
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
    leaders.value = await fetchLeaders(keyword.value.trim(), perfWindow.value)
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
    detail.value = await fetchLeaderDetail(l.leader_user_id, perfWindow.value)
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
  leader: FollowLeaderSummary,
  groupIds: string[],
  nodeIds: string[],
  closeTasks: boolean,
) {
  if (!groupIds.length || !nodeIds.length) return
  try {
    const res = await leaveFollow({
      leader_user_id: leader.leader_user_id,
      group_ids: groupIds,
      node_ids: nodeIds,
      close_tasks: closeTasks,
    })
    // 后端返回的是「节点 × 分组」对，同一节点被跟多个分组会被计多次 ——
    // 提示语按去重后的节点数算，否则「已移出 3 个节点」实际只移了 2 个。
    const removedNodeCount = new Set(
      res.removed.flatMap((r) => r.node_ids),
    ).size
    let msg = `已移出 ${removedNodeCount} 个节点`
    if (res.closing.length) msg += `，${res.closing.length} 个任务正在平仓`
    if (res.forced.length) msg += `，${res.forced.length} 个离线节点已排队补发平仓`
    if (res.warning) ElMessage.warning(res.warning)
    // 跨市场分组：关系**未**被删除，单独提示（不能混进「已移出」的计数里）
    if (res.skipped.length) {
      ElMessage.warning(`有 ${res.skipped.length} 个分组属于【AI 交易】市场，未在本次移出：${res.skipped[0].reason}`)
    }
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
 *
 * 关键：同一节点可能在同一 leader 的**多个分组**下被跟随，
 * 所以聚合时必须记全该节点所属的全部分组 —— 移出时要一次性全清，
 * 只清一个分组会残留「半个跟单」状态（仍在其它分组收信号）。
 */
const myFollowing = computed<MyFollowingEntry[]>(() =>
  mine.value.following.map((f) => {
    const nodesById = new Map<string, MyFollowedNode>()
    // 取任一分组的不可跟单原因（同一 leader 多分组时原因通常一致）
    let unmarketableReason: string | null = null
    for (const g of f.groups) {
      if (!g.marketable && g.unmarketable_reason) unmarketableReason = g.unmarketable_reason
      for (const n of g.my_nodes) {
        const prev = nodesById.get(n.node_id)
        if (prev) {
          // joined_at 取最早的那一条作为代表
          if (n.joined_at != null && (prev.joined_at == null || n.joined_at < prev.joined_at)) {
            prev.joined_at = n.joined_at
          }
          // 记全所属分组（去重）
          if (!prev.groups.some((x) => x.group_id === g.group_id)) {
            prev.groups.push({ group_id: g.group_id, group_name: g.group_name ?? null })
          }
        } else {
          nodesById.set(n.node_id, {
            node_id: n.node_id,
            name: n.name ?? null,
            mt5_login: n.mt5_login ?? null,
            status: n.status ?? null,
            joined_at: n.joined_at ?? null,
            active_task_count: g.active_task_count,
            groups: [{ group_id: g.group_id, group_name: g.group_name ?? null }],
          })
        }
      }
    }
    // 运行中任务数：按**去重后的分组**求和，避免同一分组被重复计数
    for (const node of nodesById.values()) {
      const ids = new Set(node.groups.map((x) => x.group_id))
      let sum = 0
      for (const g of f.groups) {
        if (ids.has(g.group_id)) sum += g.active_task_count
      }
      node.active_task_count = sum
    }
    return {
      leader: f.leader,
      marketable: f.leader.marketable,
      unmarketable_reason: unmarketableReason,
      nodes: [...nodesById.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
      groups: f.groups.map((g) => ({
        group_id: g.group_id,
        group_name: g.group_name ?? null,
        active_task_count: g.active_task_count,
      })),
    }
  }),
)

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

// ---------------------------------------------------------------------------
// 移出跟单（多选）
// ---------------------------------------------------------------------------
//
// 交互：点「移出」弹出选择框 → 勾选要移出的节点 → 确认。
// 语义：勾选的节点会从**它被跟随的全部分组**中一次性移出，
// 因为「跟不跟某个带单员」是节点级语义，不是分组级语义 ——
// 只从其中一组移出会让节点继续在其它分组收信号，用户以为已经退干净了。
interface LeaveDialogState {
  leader: FollowLeaderSummary
  entry: MyFollowingEntry
  /** 待移出的节点 id */
  selected: string[]
}

const leaveDialog = ref<LeaveDialogState | null>(null)
const leaveSubmitting = ref(false)

/**
 * 弹窗的 v-model 桥接：开 = leaveDialog 非空，关 = 置空。
 * 用 v-model 而不是静态 :model-value，ESC / 右上角 X 才能正常关闭。
 */
const leaveDialogVisible = computed<boolean>({
  get: () => leaveDialog.value !== null,
  set: (v) => {
    if (!v) closeLeaveDialog()
  },
})

/** 打开移出选择框；presetNodeId 非空时预勾选该节点 */
function openLeaveDialog(leader: FollowLeaderSummary, presetNodeId?: string) {
  const entry = myFollowing.value.find((x) => x.leader.leader_user_id === leader.leader_user_id)
  if (!entry || !entry.nodes.length) return
  leaveDialog.value = {
    leader,
    entry,
    selected: presetNodeId ? [presetNodeId] : [],
  }
}

function closeLeaveDialog() {
  if (leaveSubmitting.value) return
  leaveDialog.value = null
}

function toggleLeaveNode(nodeId: string) {
  const d = leaveDialog.value
  if (!d) return
  const s = new Set(d.selected)
  s.has(nodeId) ? s.delete(nodeId) : s.add(nodeId)
  d.selected = [...s]
}

function toggleAllLeaveNodes() {
  const d = leaveDialog.value
  if (!d) return
  d.selected = d.selected.length === d.entry.nodes.length ? [] : d.entry.nodes.map((n) => n.node_id)
}

/** 选中的节点涉及的分组并集（移出时要覆盖全部） */
const leaveTargetGroupIds = computed<string[]>(() => {
  const d = leaveDialog.value
  if (!d) return []
  const ids = new Set<string>()
  for (const n of d.entry.nodes) {
    if (!d.selected.includes(n.node_id)) continue
    for (const g of n.groups) ids.add(g.group_id)
  }
  return [...ids]
})

/**
 * 选中的节点涉及的运行中任务数。
 *
 * 按**分组去重**求和，而不是按节点累加 —— 多个被选节点可能同属一个分组，
 * 那个分组的活跃任务数只有一份，按节点累加会把同一个任务算好几次，
 * 导致「平仓关闭」提示的数量虚高。
 */
const leaveActiveTaskCount = computed<number>(() => {
  const d = leaveDialog.value
  if (!d) return 0
  const ids = new Set(leaveTargetGroupIds.value)
  let sum = 0
  for (const g of d.entry.groups) {
    if (ids.has(g.group_id)) sum += g.active_task_count
  }
  return sum
})

async function confirmLeave() {
  const d = leaveDialog.value
  if (!d || !d.selected.length) return
  const nodeIds = [...d.selected]
  const groupIds = leaveTargetGroupIds.value
  const active = leaveActiveTaskCount.value

  // 无进行中任务：直接移出
  if (!active) {
    leaveSubmitting.value = true
    await doLeave(d.leader, groupIds, nodeIds, false)
    leaveSubmitting.value = false
    leaveDialog.value = null
    return
  }

  // 有进行中的任务：让用户在「平仓后移出」和「保留任务」之间二选一
  try {
    const choice = await ElMessageBox({
      title: '移出跟单',
      type: 'warning',
      dangerouslyUseHTMLString: true,
      message:
        `选中的 ${nodeIds.length} 个节点在带单员「${d.leader.display_name || d.leader.username}」下还有 ${active} 个进行中的任务。<br/>` +
        '移出后该带单员不再向它们下发新信号，现有任务如何处理？',
      distinguishCancelAndClose: true,
      confirmButtonText: '平仓并关闭任务后移出',
      cancelButtonText: '保留任务，仅停止接收新信号',
    })
      .then((r) => r.value)
      .catch((r) => (r === 'cancel' ? 'keep' : 'close'))
    if (choice === 'close') return
    leaveSubmitting.value = true
    await doLeave(d.leader, groupIds, nodeIds, choice === 'confirm')
    leaveSubmitting.value = false
    leaveDialog.value = null
  } catch {
    // 关闭弹窗：不处理
  }
}

// 统计窗口切换：重拉列表 + 当前详情（窗口是纯读缓存，切换零延迟）
watch(perfWindow, () => {
  void loadLeaders()
  if (detail.value) {
    void openLeader(detail.value.leader)
  }
})

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
        <!-- 统计窗口：所有带单员同一窗口，否则没法横向比较 -->
        <div class="row" style="gap: 6px; margin-bottom: 10px; flex-wrap: wrap">
          <span class="muted" style="font-size: 12px">统计窗口</span>
          <button
            v-for="opt in WINDOW_OPTIONS"
            :key="opt.value"
            class="btn-sm"
            :class="perfWindow === opt.value ? 'btn-primary' : 'btn-ghost'"
            @click="perfWindow = opt.value"
          >
            {{ opt.label }}
          </button>
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

            <!-- 4 项统计（真实成交聚合，与订单历史页同一口径） -->
            <div class="leader-stats">
              <div class="ls-item">
                <span class="ls-k">累计收益</span>
                <span v-if="!l.performance?.data_available" class="ls-v muted">暂无数据</span>
                <span v-else class="ls-v" :style="{ color: pctColor(statsOf(l).total_profit) }">
                  {{ fmtProfit(statsOf(l).total_profit) }}
                </span>
              </div>
              <div class="ls-item">
                <span class="ls-k">收益率</span>
                <span v-if="!l.performance?.data_available" class="ls-v muted">暂无数据</span>
                <span v-else class="ls-v" :style="{ color: pctColor(statsOf(l).profit_rate) }">
                  {{ fmtRate(statsOf(l).profit_rate) }}
                </span>
              </div>
              <div class="ls-item">
                <span class="ls-k">跟单人数</span>
                <span class="ls-v">{{ followerCountOf(l) }} 人</span>
              </div>
              <div class="ls-item">
                <span class="ls-k">胜率</span>
                <span v-if="!l.performance?.data_available" class="ls-v muted">暂无数据</span>
                <span v-else class="ls-v">{{ statsOf(l).win_rate.toFixed(2) }}%</span>
              </div>
            </div>
            <div v-if="updatedText(l)" class="ls-updated muted">{{ updatedText(l) }}</div>
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

          <!-- 跟单统计卡片（真实成交聚合） -->
          <div v-if="currentStats" class="stats-grid">
            <div class="stat-card">
              <div class="stat-label">累计收益</div>
              <div v-if="!currentStats.data_available" class="stat-value muted">暂无数据</div>
              <div v-else class="stat-value" :style="{ color: pctColor(currentStats.total_profit) }">
                {{ fmtProfit(currentStats.total_profit) }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">收益率</div>
              <div v-if="!currentStats.data_available" class="stat-value muted">暂无数据</div>
              <div v-else-if="!currentStats.profit_rate_valid" class="stat-value muted">—</div>
              <div v-else class="stat-value" :style="{ color: pctColor(currentStats.profit_rate) }">
                {{ fmtRate(currentStats.profit_rate) }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">跟单人数</div>
              <div class="stat-value">{{ currentFollowerCount }} 人</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">胜率</div>
              <div v-if="!currentStats.data_available" class="stat-value muted">暂无数据</div>
              <div v-else class="stat-value">{{ currentStats.win_rate.toFixed(2) }}%</div>
            </div>
          </div>
          <div v-if="updatedText(detail.leader)" class="muted" style="font-size: 12px; margin-top: -4px">
            {{ updatedText(detail.leader) }}
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
                <div class="row" style="gap: 8px">
                  <span v-if="!f.marketable" class="tag amber">{{ f.unmarketable_reason || '该带单员已关闭带单' }}</span>
                  <button
                    v-if="f.nodes.length"
                    class="btn-sm btn-ghost"
                    @click="openLeaveDialog(f.leader)"
                  >
                    移出节点
                  </button>
                </div>
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
                    <template v-if="n.groups.length > 1">
                      · 跟自 {{ n.groups.length }} 个分组
                    </template>
                  </span>
                  <button class="btn-sm btn-ghost" @click="openLeaveDialog(f.leader, n.node_id)">
                    移出
                  </button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </section>
    </div>

    <!-- 移出跟单：选择要移出的节点（一次清空该节点被跟随的全部分组） -->
    <ElDialog
      v-if="leaveDialog"
      v-model="leaveDialogVisible"
      width="460px"
      title="移出跟单"
      :close-on-click-modal="false"
      align-center
      class="leave-dialog"
    >
      <p class="muted" style="margin: 0 0 10px; font-size: 12px">
        带单员「{{ leaveDialog.leader.display_name || leaveDialog.leader.username }}」
        · 勾选要移出的节点，移出后该带单员不再向它们下发信号
      </p>
      <div class="leave-all">
        <label>
          <input
            type="checkbox"
            :checked="leaveDialog.selected.length === leaveDialog.entry.nodes.length && leaveDialog.entry.nodes.length > 0"
            @change="toggleAllLeaveNodes"
          />
          <span>全选（{{ leaveDialog.entry.nodes.length }} 个节点）</span>
        </label>
      </div>
      <ul class="leave-list">
        <li
          v-for="n in leaveDialog.entry.nodes"
          :key="n.node_id"
          class="leave-item"
          :class="{ on: leaveDialog.selected.includes(n.node_id) }"
          @click="toggleLeaveNode(n.node_id)"
        >
          <label>
            <input
              type="checkbox"
              :checked="leaveDialog.selected.includes(n.node_id)"
              @click.stop
              @change="toggleLeaveNode(n.node_id)"
            />
            <span class="pick-name">{{ n.name || n.node_id }}</span>
          </label>
          <span class="muted" style="font-size: 12px">
            MT5 {{ n.mt5_login ?? '—' }}
            <template v-if="n.groups.length">
              · {{ n.groups.map((g) => g.group_name || g.group_id).join('、') }}
            </template>
            <template v-if="n.active_task_count"> · 运行中 {{ n.active_task_count }} 单</template>
          </span>
        </li>
      </ul>
      <p v-if="leaveActiveTaskCount" class="muted" style="margin: 10px 0 0; font-size: 12px">
        选中的节点还有 {{ leaveActiveTaskCount }} 个进行中的任务，移出时可选择是否平仓关闭。
      </p>
      <template #footer>
        <div class="row between" style="width: 100%">
          <span class="muted" style="font-size: 12px">
            已选 {{ leaveDialog.selected.length }} 个节点 · 涉及 {{ leaveTargetGroupIds.length }} 个分组
          </span>
          <div class="row" style="gap: 8px">
            <button class="btn-sm btn-ghost" :disabled="leaveSubmitting" @click="closeLeaveDialog">
              取消
            </button>
            <button
              class="btn-sm btn-primary"
              :disabled="!leaveDialog.selected.length || leaveSubmitting"
              @click="confirmLeave"
            >
              {{ leaveSubmitting ? '提交中...' : '确认移出' }}
            </button>
          </div>
        </div>
      </template>
    </ElDialog>
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

/* ── 移出跟单选择弹窗 ───────────────────────────────── */
.leave-all {
  padding: 6px 10px;
  border: 1px dashed var(--glass-border);
  border-radius: var(--radius);
  margin-bottom: 8px;
}

.leave-all label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 13px;
}

.leave-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 45vh;
  overflow-y: auto;
}

.leave-item {
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 13px;
}

.leave-item:hover {
  border-color: var(--primary);
}

.leave-item.on {
  border-color: var(--primary);
  background: var(--glass);
}

.leave-item label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: inherit;
  margin-right: 10px;
}
</style>