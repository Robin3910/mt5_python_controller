<script setup lang="ts">
// 带单（跟单）页：左侧带单员列表，右侧「我的跟单」
// 能力语义：跟单默认人人可做；带单员由管理员授予 follow_leader 角色能力产生。
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElDialog, ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { fetchLeaders, fetchLeaderDetail, fetchMyFollowings, joinFollow, leaveFollow, setFollowPaused } from '@/api/follow'
import type {
  FollowDetailResponse,
  FollowLeaderSummary,
  FollowMeResponse,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import UserAvatar from '@/components/UserAvatar.vue'

// 收益柱状图带 echarts，异步加载让图表库不进带单广场首屏 chunk
// （与节点详情页的 TrendPanel 同一处理）
const ProfitBarChart = defineAsyncComponent(() => import('@/components/ProfitBarChart.vue'))

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
const auth = useAuthStore()

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

/**
 * 这个带单员是不是我自己。
 *
 * 带单员列表是全市场视角（我也出现在别人的列表里），不加标记的话
 * 「哪一栏是自己开的单」只能靠回忆用户名来判断。名字后面挂一个「我的」
 * 标签，扫一眼就能定位。
 *
 * 只认 `leader_user_id` —— 用户名可改，用 ID 比对才不会在改名后失效。
 * `me` 还没拉到时返回 false：宁可先不显示，也不要在身份未确认时误标。
 */
function isMyLeader(leader: FollowLeaderSummary): boolean {
  const myId = auth.me?.user_id
  return myId != null && leader.leader_user_id === myId
}

// ---------------------------------------------------------------------------
// 暂停带单（我的带单才有的开关）
// ---------------------------------------------------------------------------
//
// 「暂停」= 主人对外停止接单，但**不动**已有的跟单关系与在跑任务：
//   ① 新客户无法再加入我的跟单分组（服务端写侧拦截，前端只做提示）；
//   ② 已有跟单员与在跑任务**保留**，等他们自行移出；
//   ③ 之后触发的信号只发主节点，不再下发给跟单节点（服务端分发侧收窄）。
//
// 这与「交易」界面的「带单」开关关闭是同一件事，只是那里的开关是**单次信号**
// 级别的（前端把 node_ids 收窄到主节点），这里的是**持久**状态（服务端收窄，
// 覆盖 webhook / 限价监听等所有触发路径）。
//
// 只作用于带单市场（趋势 / 网格）；AI 市场（AI 智能加仓）有独立开关，
// 在【AI 交易】页操作，两边互不影响。

/** 暂停操作进行中（防止重复点击） */
const pauseBusy = ref(false)

/** 当前详情页是否处于「已暂停带单」状态 */
const detailPaused = computed<boolean>(() => detail.value?.leader.follow_paused === true)

/**
 * 切换暂停状态。
 *
 * 暂停是不可逆的「对外接单」动作 —— 跟单员点进来会看到「已暂停」，
 * 所以必须二次确认，把影响说清楚再执行。
 */
async function togglePause() {
  if (!detail.value) return
  const leader = detail.value.leader
  const myId = auth.me?.user_id
  // 只有本人（或管理员代操作他人）才能改这个开关 —— 服务端也会再校验一次。
  if (myId == null || leader.leader_user_id !== myId) return

  const next = !leader.follow_paused
  const name = leader.display_name || leader.username
  try {
    if (next) {
      await ElMessageBox.confirm(
        `暂停后，${name} 将不再接收新的跟单申请。\n\n` +
          `· 已有跟单关系与在跑任务**保留**，等跟单员自行移出\n` +
          `· 之后触发的信号只下发主节点，不再下发给跟单节点\n` +
          `· 只影响带单市场（趋势 / 网格），不影响 AI 智能加仓分组\n\n` +
          `确认暂停带单吗？`,
        '暂停带单',
        { type: 'warning', confirmButtonText: '确认暂停', cancelButtonText: '再想想' },
      )
    } else {
      await ElMessageBox.confirm(
        `恢复后，${name} 将重新接受新的跟单申请，新信号也会重新下发给跟单节点。`,
        '恢复带单',
        { type: 'info', confirmButtonText: '确认恢复', cancelButtonText: '取消' },
      )
    }
  } catch {
    // 用户取消
    return
  }

  pauseBusy.value = true
  try {
    const res = await setFollowPaused({ leader_user_id: leader.leader_user_id, paused: next })
    // 就地更新：详情页与列表共用同一份 leader 摘要，避免整页重拉。
    detail.value.leader.follow_paused = res.follow_paused
    const i = leaders.value.findIndex((l) => l.leader_user_id === leader.leader_user_id)
    if (i >= 0) leaders.value[i].follow_paused = res.follow_paused
    // 「我的跟单」里也可能有自己（自跟单），一并同步。
    const mineLeader = mine.value.following.find((f) => f.leader.leader_user_id === leader.leader_user_id)
    if (mineLeader) mineLeader.leader.follow_paused = res.follow_paused
    ElMessage.success(res.follow_paused ? '已暂停带单' : '已恢复带单')
    // 列表与「我的跟单」的不可跟单标记依赖服务端口径，重拉一次最保险。
    await Promise.all([loadLeaders(), loadMine()])
  } catch (e) {
    ElMessage.error(errText(e, next ? '暂停带单失败' : '恢复带单失败'))
  } finally {
    pauseBusy.value = false
  }
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

/**
 * 近 7 日收益序列（柱状图数据）。
 *
 * 数据由后端按与绩效**同一套口径**算好（主节点 + 出场类成交的 net_profit），
 * 前端不自己统计 —— 自己算必然与页面头部的「累计收益」对不上。
 *
 * 固定 7 日，**不随页面统计窗口（7d/30d/90d/all）变化**，所以标题写明「近 7 日」。
 */
const dailyProfit = computed(() => detail.value?.daily_profit ?? [])

/** 数据新鲜度：成交是准实时的，绩效必然滞后，UI 必须标注 */
function updatedText(leader: FollowLeaderSummary): string {
  const ts = leader.performance?.updated_at
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `数据更新于 ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 金额格式化：符号在货币符号**之前**（-$123.45 / +$123.45）。
 *
 * 原来负数写成 `$-123.45`（负号在 $ 后面），不符合金额书写惯例，
 * 在深色背景上 `-$` 连在一起也容易看漏方向。桌面端和手机端共用这个函数，
 * 改一处即可同时修好两端。
 */
function fmtProfit(v: number): string {
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return sign + '$' + Math.abs(v).toFixed(2)
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
    // 手机端收起详情并跳到「我的跟单」：成功结果在那里才看得见。
    // 桌面端保持留在详情页（detail 仍非空，原有行为不变）。
    if (mobileSheetOpen.value) afterJoinSettled()
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

/** 全选框的勾选态（空列表时不能显示为已全选） */
const leaveAllSelected = computed<boolean>(() => {
  const d = leaveDialog.value
  if (!d || !d.entry.nodes.length) return false
  return d.selected.length === d.entry.nodes.length
})

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

// ---------------------------------------------------------------------------
// 手机端布局状态
// ---------------------------------------------------------------------------
//
// 手机端不复用桌面的「左列表 + 右详情」两栏（窄屏下会退化成嵌套滚动），
// 改为：顶部分段切换「带单员 / 我的跟单」+ 点带单员进全屏详情页。
// 这些状态只在 ≤768px 生效，桌面端看不到，所以可以与桌面共用同一批数据源。

/** 分段切换当前面板 */
const mobileTab = ref<'leaders' | 'mine'>('leaders')

/** 详情全屏页是否打开（与桌面共用 detail 数据，只是手机端多一层遮罩） */
const mobileSheetOpen = ref(false)

/** 手机端打开详情：立刻弹页 + 拉数据，避免点完没有反馈 */
async function openMobileLeader(l: FollowLeaderSummary) {
  mobileSheetOpen.value = true
  await openLeader(l)
}

/** 关闭详情页并回到列表 */
function closeMobileSheet() {
  mobileSheetOpen.value = false
  selectedNodes.value = []
  detail.value = null
}

// 详情页打开时锁住背景滚动：手机端全屏页是 fixed 覆盖层，
// 不锁的话手指在页面上滑会连带把背后的列表一起滚掉。
watch(mobileSheetOpen, (open) => {
  document.body.style.overflow = open ? 'hidden' : ''
})

onBeforeUnmount(() => {
  document.body.style.overflow = ''
})

// 跟单成功后：手机端收起详情页并切到「我的跟单」，让结果立刻可见。
// 只改移动端状态，不动 detail —— 桌面端保持「留在详情页」的原有行为。
function afterJoinSettled() {
  mobileSheetOpen.value = false
  mobileTab.value = 'mine'
}

/** 当前统计窗口的中文标签（详情页标题里要显示"这些数字算的是哪一段"） */
const perfWindowLabel = computed(
  () => WINDOW_OPTIONS.find((o) => o.value === perfWindow.value)?.label ?? '',
)

/**
 * 手机端紧凑日期。
 *
 * 列表里只需要"哪天跟的"，`toLocaleString` 产出的
 * `2026/10/23 10:34:00` 在窄屏上会把整行节点信息挤爆。
 */
function fmtJoinedShort(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return sameYear
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
}

onMounted(() => {
  void loadLeaders()
  void loadMine()
})
</script>

<template>
  <div class="follow-page">
    <div class="row between page-header">
      <div class="page-head-l">
        <h2 class="page-title">带单广场</h2>
        <span class="page-head-tag">热门带单员</span>
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
            <div class="row between leader-head">
              <span class="row leader-name">
                <UserAvatar class="leader-avatar" :name="l.display_name || l.username" :size="30" />
                <strong class="leader-name-text">{{ l.display_name || l.username }}</strong>
                <span v-if="isMyLeader(l)" class="tag blue leader-mine">我的</span>
                <span v-if="l.follow_paused" class="tag amber leader-paused">已暂停带单</span>
              </span>
              <span v-if="l.my_follow_node_count" class="tag green">
                已跟
              </span>
              <span v-else class="tag">未跟</span>
            </div>

            <!-- 4 项统计（真实成交聚合，与订单历史页同一口径）
                 手机端：累计收益 / 收益率 提为主视觉大数字，胜率 / 跟单人数退为小格子 -->
            <div class="leader-stats">
              <div class="ls-hero">
                <div class="ls-hero-main">
                  <span class="ls-hero-k">累计收益</span>
                  <span v-if="!l.performance?.data_available" class="ls-hero-v muted">暂无数据</span>
                  <span
                    v-else
                    class="ls-hero-v"
                    :class="statsOf(l).total_profit >= 0 ? 'pos' : 'neg'"
                  >
                    {{ fmtProfit(statsOf(l).total_profit) }}
                  </span>
                </div>
                <div class="ls-hero-side">
                  <span class="ls-hero-k">收益率</span>
                  <span v-if="!l.performance?.data_available" class="ls-hero-sub muted">暂无数据</span>
                  <span v-else-if="!statsOf(l).profit_rate_valid" class="ls-hero-sub muted">—</span>
                  <span
                    v-else
                    class="ls-hero-sub"
                    :class="statsOf(l).profit_rate >= 0 ? 'pos' : 'neg'"
                  >
                    {{ fmtRate(statsOf(l).profit_rate) }}
                  </span>
                </div>
              </div>

              <div class="ls-mini-row">
                <div class="ls-mini">
                  <span class="ls-mini-k">胜率</span>
                  <span v-if="!l.performance?.data_available" class="ls-mini-v muted">暂无数据</span>
                  <span v-else class="ls-mini-v">{{ statsOf(l).win_rate.toFixed(2) }}%</span>
                </div>
                <div class="ls-mini">
                  <span class="ls-mini-k">跟单人数</span>
                  <span class="ls-mini-v">{{ followerCountOf(l) }} 人</span>
                </div>
              </div>

              <div v-if="updatedText(l)" class="ls-updated muted">{{ updatedText(l) }}</div>
            </div>
          </li>
        </ul>
      </section>

      <!-- 右：详情 / 我的跟单 -->
      <section class="card card-pad">
        <div v-if="detailLoading" class="muted" style="padding: 12px 0">加载中...</div>

        <template v-else-if="detail">
          <div class="row between" style="margin-bottom: 4px">
            <strong class="row leader-name">
              <UserAvatar class="leader-avatar" :name="detail.leader.display_name || detail.leader.username" :size="30" />
              <span class="leader-name-text">跟单 · {{ detail.leader.display_name || detail.leader.username }}</span>
              <span v-if="isMyLeader(detail.leader)" class="tag blue leader-mine">我的</span>
              <span v-if="detailPaused" class="tag amber leader-paused">已暂停带单</span>
            </strong>
            <div class="row" style="gap: 8px">
              <button
                v-if="isMyLeader(detail.leader)"
                class="btn-sm"
                :class="detailPaused ? 'btn-ghost' : 'btn-warn-ghost'"
                :disabled="pauseBusy"
                @click="togglePause"
              >
                {{ detailPaused ? '恢复带单' : '暂停带单' }}
              </button>
              <button class="btn-sm btn-ghost" @click="detail = null">返回列表</button>
            </div>
          </div>

          <!-- 暂停提示：说清「不动什么、动什么」，避免主人误以为暂停会踢掉跟单员 -->
          <div v-if="detailPaused" class="pause-banner">
            <strong>带单已暂停</strong>
            <span>
              新的客户无法再加入你的跟单分组；已有的跟单关系与在跑任务保持不变，
              等跟单员自行移出。之后触发的信号只下发主节点。
            </span>
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

          <!-- 近 7 日收益柱状图（与手机端全屏详情同一份数据） -->
          <div class="detail-chart">
            <ProfitBarChart
              v-if="currentStats?.data_available !== false"
              :points="dailyProfit"
              :height="170"
            />
            <p v-else class="muted empty">近 7 日暂无收益数据</p>
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
                <strong class="row leader-name">
                  <UserAvatar class="leader-avatar" :name="f.leader.display_name || f.leader.username" :size="30" />
                  <span class="leader-name-text">
                    {{ f.leader.display_name || f.leader.username }}
                    <span class="muted" style="font-weight: 400">
                      · {{ f.leader.primary_node?.name }}
                      <template v-if="f.leader.primary_node?.mt5_login">
                        （MT5 {{ f.leader.primary_node.mt5_login }}）
                      </template>
                    </span>
                  </span>
                  <span v-if="isMyLeader(f.leader)" class="tag blue leader-mine">我的</span>
                  <span v-if="f.leader.follow_paused" class="tag amber leader-paused">已暂停带单</span>
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
                  </span>
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
      :close-on-click-modal="false"
      align-center
      class="leave-dialog"
      :style="{ '--leave-radius': '16px' }"
    >
      <template #header>
        <div class="lv-header">
          <div class="lv-title">
            <span class="lv-title-main">移出跟单</span>
            <span class="lv-title-sub">
              带单员「{{ leaveDialog.leader.display_name || leaveDialog.leader.username }}」
            </span>
          </div>

        </div>
      </template>

      <div class="lv-body">
        <!-- 提示 + 全选：粘在列表顶部，不随列表滚走 -->
        <div class="lv-hint muted">
          勾选要移出的节点，移出后该带单员不再向它们下发信号。
        </div>

        <div class="leave-all">
          <label>
            <input
              type="checkbox"
              :checked="leaveAllSelected"
              :disabled="!leaveDialog.entry.nodes.length"
              @change="toggleAllLeaveNodes"
            />
            <span class="leave-all-text">全选</span>
            <span class="leave-all-count">{{ leaveDialog.entry.nodes.length }} 个节点</span>
          </label>
          <span v-if="leaveDialog.selected.length" class="leave-all-selected">
            已选 {{ leaveDialog.selected.length }}
          </span>
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
              <span class="lv-node-main">
                <span class="pick-name">{{ n.name || n.node_id }}</span>
                <span class="lv-node-meta mono">
                  MT5 {{ n.mt5_login ?? '—' }}
                  <template v-if="n.active_task_count">
                    <span class="lv-node-tasks">运行中 {{ n.active_task_count }} 单</span>
                  </template>
                </span>
              </span>
            </label>
          </li>
        </ul>

        <!-- 选中后会影响到的分组 / 任务：给用户一个可预期的后果说明 -->
        <div v-if="leaveDialog.selected.length" class="lv-impact">

          <div v-if="leaveActiveTaskCount" class="lv-impact-row lv-impact-warn">
            <span class="lv-impact-k">进行中任务</span>
            <span class="lv-impact-v">{{ leaveActiveTaskCount }} 个 · 移出时可选择是否平仓</span>
          </div>
          <div v-else class="lv-impact-row">
            <span class="lv-impact-k">进行中任务</span>
            <span class="lv-impact-v muted">无</span>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="lv-footer">
          <span class="lv-footer-hint muted">
            {{ leaveDialog.selected.length ? `已选 ${leaveDialog.selected.length} 个节点` : '请选择要移出的节点' }}
          </span>
          <div class="lv-footer-btns">
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

    <!-- ══════════════════════════════════════════════════════════════
         手机端专属层（≤768px 可见，桌面端 display:none）
         结构：分段切换「带单员 / 我的跟单」+ 全屏详情页
         ══════════════════════════════════════════════════════════════ -->

    <!-- 顶部分段切换：手机上「带单员」和「我的跟单」是两份互斥信息，
         堆在一起会让页面变成很长的滚动条。 -->
    <div class="m-only m-tabs" role="tablist" aria-label="带单广场">
      <button
        v-for="t in ([
          { key: 'leaders', label: '带单员', badge: leaders.length },
          { key: 'mine', label: '我的跟单', badge: myFollowing.length },
        ] as const)"
        :key="t.key"
        class="m-tab"
        role="tab"
        :aria-selected="mobileTab === t.key"
        :class="{ on: mobileTab === t.key }"
        @click="mobileTab = t.key"
      >
        {{ t.label }}
        <span v-if="t.badge" class="m-tab-badge">{{ t.badge }}</span>
      </button>
    </div>

    <!-- ── 分段一：带单员列表 ── -->
    <div v-show="mobileTab === 'leaders'" class="m-only m-panel">
      <!-- 统计周期：手机端做成可横向滑动的分段控件，避免换行占掉一屏。
           「统计周期」标签和按钮排同一行，不额外占一行高度。 -->
      <div class="m-win-row">
        <span class="m-win-label">统计周期</span>
        <div class="m-win" role="group" aria-label="统计周期">
          <button
            v-for="opt in WINDOW_OPTIONS"
            :key="opt.value"
            class="m-win-btn"
            :class="{ on: perfWindow === opt.value }"
            :aria-pressed="perfWindow === opt.value"
            @click="perfWindow = opt.value"
          >
            {{ opt.label }}
          </button>
        </div>
      </div>

      <div v-if="loading" class="m-empty muted">加载中...</div>
      <div v-else-if="!leaders.length" class="m-empty muted">
        当前没有开放跟单的带单员
      </div>

      <ul v-else class="m-leader-list">
        <li
          v-for="l in leaders"
          :key="l.leader_user_id"
          class="m-leader"
          @click="openMobileLeader(l)"
        >
          <!-- 头部：头像 + 昵称 + 状态标签 -->
          <div class="m-leader-top">
            <span class="m-leader-id">
              <UserAvatar :name="l.display_name || l.username" :size="40" />
              <span class="m-leader-id-text">
                <span class="m-leader-name">{{ l.display_name || l.username }}</span>
                <span class="m-leader-sub">
                  <template v-if="isMyLeader(l)">我开设的带单</template>
                  <template v-else-if="l.primary_node">{{ l.primary_node.name || '主节点' }}</template>
                  <template v-if="updatedText(l)">
                    {{ isMyLeader(l) || l.primary_node ? ' · ' : '' }}{{ updatedText(l) }}
                  </template>
                </span>
              </span>
            </span>
            <span class="m-leader-badge" :class="l.my_follow_node_count ? 'is-followed' : ''">
              {{ l.my_follow_node_count ? '已跟' : '跟单' }}
            </span>
          </div>

          <!-- 主视觉：累计收益 + 收益率。设计稿里收益是大号主数字，
               收益率作为右上角小字，比例和参考图一致。 -->
          <div class="m-pnl">
            <div v-if="!l.performance?.data_available" class="m-pnl-none muted">暂无成交数据</div>
            <template v-else>
              <div class="m-pnl-left">
                <span class="m-pnl-main" :class="statsOf(l).total_profit >= 0 ? 'pos' : 'neg'">
                  {{ fmtProfit(statsOf(l).total_profit) }}
                </span>
                <span class="m-pnl-label">累计收益</span>
              </div>
              <div class="m-pnl-right">
                <span
                  v-if="statsOf(l).profit_rate_valid"
                  class="m-pnl-rate"
                  :class="statsOf(l).profit_rate >= 0 ? 'pos' : 'neg'"
                >
                  {{ fmtRate(statsOf(l).profit_rate) }}
                </span>
                <span v-else class="m-pnl-rate muted">—</span>
                <span class="m-pnl-label">收益率</span>
              </div>
            </template>
          </div>

          <!-- 次要指标：三个等宽小格，比例对齐设计稿底部一行 -->
          <div class="m-mini-row">
            <span class="m-mini">
              <span class="m-mini-k">胜率</span>
              <span v-if="!l.performance?.data_available" class="m-mini-v muted">—</span>
              <span v-else class="m-mini-v">{{ statsOf(l).win_rate.toFixed(1) }}%</span>
            </span>
            <span class="m-mini">
              <span class="m-mini-k">跟单</span>
              <span class="m-mini-v">{{ followerCountOf(l) }} 人</span>
            </span>
            <span class="m-mini">
              <span class="m-mini-k">交易</span>
              <span v-if="!l.performance?.data_available" class="m-mini-v muted">—</span>
              <span v-else class="m-mini-v">{{ statsOf(l).total_trades }} 笔</span>
            </span>
          </div>
        </li>
      </ul>
    </div>

    <!-- ── 分段二：我的跟单 ── -->
    <div v-show="mobileTab === 'mine'" class="m-only m-panel">
      <div v-if="mineLoading" class="m-empty muted">加载中...</div>
      <div v-else-if="!mine.following.length" class="m-empty">
        <p class="muted m-empty-text">还没有跟单</p>
        <button class="btn-sm btn-ghost m-empty-btn" @click="mobileTab = 'leaders'">
          去挑一个带单员
        </button>
      </div>
      <div v-else class="m-mine-list">
        <div v-for="f in myFollowing" :key="f.leader.leader_user_id" class="m-mine">
          <div class="m-mine-head">
            <div class="m-mine-id">
              <span class="m-mine-name-row">
                <UserAvatar :name="f.leader.display_name || f.leader.username" :size="34" />
                <span class="m-mine-name">{{ f.leader.display_name || f.leader.username }}</span>
                <span v-if="isMyLeader(f.leader)" class="tag blue m-mine-flag">我的</span>
                <span v-if="f.leader.follow_paused" class="tag amber m-mine-flag">已暂停带单</span>
              </span>
              <span v-if="!f.marketable" class="tag amber m-mine-tag">
                {{ f.unmarketable_reason || '已关闭带单' }}
              </span>
            </div>
            <button
              v-if="f.nodes.length"
              class="btn-sm btn-warn-ghost m-mine-btn"
              @click="openLeaveDialog(f.leader)"
            >
              移出节点
            </button>
          </div>

          <p v-if="!f.nodes.length" class="muted m-mine-none">暂未跟单任何节点</p>

          <ul v-else class="m-mine-nodes">
            <li v-for="n in f.nodes" :key="n.node_id" class="m-mine-node">
              <span class="m-mine-node-name">{{ n.name || n.node_id }}</span>
              <span v-if="n.mt5_login" class="mono m-mine-node-login">MT5 {{ n.mt5_login }}</span>
              <span class="m-mine-node-meta muted">
                {{ fmtJoinedShort(n.joined_at) }} 加入
                <template v-if="n.active_task_count">
                  · 运行中 {{ n.active_task_count }} 单
                </template>
              </span>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <!-- ── 全屏详情页：点带单员后覆盖整个视口，带返回按钮 ──
         详情原来排在列表下方，手机上要点完再滚过去；这里改为整页，
         彻底消除"列表内滚动 + 页面滚动"的双层滚动。 -->
    <div v-if="mobileSheetOpen" class="m-sheet" role="dialog" aria-modal="true" aria-label="带单员详情">
      <header class="m-sheet-bar">
        <button class="m-back" aria-label="返回带单员列表" @click="closeMobileSheet">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <span class="m-sheet-title">跟单详情</span>
      </header>

      <div class="m-sheet-body">
        <div v-if="detailLoading" class="m-empty muted">加载中...</div>

        <template v-else-if="detail">
          <!-- 带单员身份 + 关键数字 -->
          <div class="m-d-lead">
            <div class="m-d-name-row">
              <UserAvatar :name="detail.leader.display_name || detail.leader.username" :size="44" />
              <div class="m-d-name">{{ detail.leader.display_name || detail.leader.username }}</div>
              <span v-if="isMyLeader(detail.leader)" class="tag blue m-mine-flag">我的</span>
              <span v-if="detailPaused" class="tag amber m-mine-flag">已暂停带单</span>
            </div>

            <!-- 暂停带单（我的带单才有的开关）：手机端放这里，桌面端在详情标题右侧 -->
            <button
              v-if="isMyLeader(detail.leader)"
              class="btn-sm m-d-pause-btn"
              :class="detailPaused ? 'btn-ghost' : 'btn-warn-ghost'"
              :disabled="pauseBusy"
              @click="togglePause"
            >
              {{ detailPaused ? '恢复带单' : '暂停带单' }}
            </button>

            <!-- 暂停提示：说清「不动什么、动什么」 -->
            <div v-if="detailPaused" class="pause-banner">
              <strong>带单已暂停</strong>
              <span>
                新的客户无法再加入你的跟单分组；已有的跟单关系与在跑任务保持不变，
                等跟单员自行移出。之后触发的信号只下发主节点。
              </span>
            </div>

            <div class="m-d-grid">
              <div class="m-d-stat">
                <span class="m-d-k">累计收益</span>
                <span v-if="!currentStats?.data_available" class="m-d-v muted">暂无数据</span>
                <span v-else class="m-d-v" :class="currentStats.total_profit >= 0 ? 'pos' : 'neg'">
                  {{ fmtProfit(currentStats.total_profit) }}
                </span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">收益率</span>
                <span v-if="!currentStats?.data_available" class="m-d-v muted">暂无数据</span>
                <span v-else-if="!currentStats.profit_rate_valid" class="m-d-v muted">—</span>
                <span v-else class="m-d-v" :class="currentStats.profit_rate >= 0 ? 'pos' : 'neg'">
                  {{ fmtRate(currentStats.profit_rate) }}
                </span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">胜率</span>
                <span v-if="!currentStats?.data_available" class="m-d-v muted">暂无数据</span>
                <span v-else class="m-d-v">{{ currentStats.win_rate.toFixed(1) }}%</span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">跟单人数</span>
                <span class="m-d-v">{{ currentFollowerCount }} 人</span>
              </div>
            </div>
            <div v-if="updatedText(detail.leader)" class="m-d-updated muted">
              {{ perfWindowLabel }} · {{ updatedText(detail.leader) }}
            </div>
          </div>

          <!-- 近 7 日收益柱状图：放在「选择要跟随的节点」上方，
               让人在选节点前先看一眼这个带单员最近赚不赚钱。 -->
          <div class="m-d-chart">
            <ProfitBarChart
              :points="dailyProfit"
              :height="140"
              v-if="currentStats?.data_available !== false"
            />
            <p v-else class="muted m-empty-text">近 7 日暂无收益数据</p>
          </div>

          <!-- 选节点：整行 56px 触控区，选中态用整块高亮 + 左侧色条 -->
          <div class="m-d-sec-title">
            选择要跟随的节点
            <span class="m-d-sec-sub muted">可多选</span>
          </div>

          <p v-if="!detail.my_nodes.length" class="muted m-empty-text">
            你没有可用于跟单的节点（主节点不可跟单）
            <button class="btn-sm btn-ghost m-empty-btn" @click="router.push('/nodes')">去节点页</button>
          </p>

          <ul v-else class="m-pick-list">
            <li
              v-for="n in detail.my_nodes"
              :key="n.node_id"
              class="m-pick"
              :class="{
                on: !alreadyFollowingNodeIds.has(n.node_id) && nodeSet.has(n.node_id),
                done: alreadyFollowingNodeIds.has(n.node_id),
              }"
              @click="alreadyFollowingNodeIds.has(n.node_id) ? null : toggleNode(n.node_id)"
            >
              <span class="m-pick-box" aria-hidden="true">
                <svg v-if="!alreadyFollowingNodeIds.has(n.node_id) && nodeSet.has(n.node_id)" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              </span>
              <span class="m-pick-main">
                <span class="m-pick-name">{{ n.name || n.node_id }}</span>
                <span class="m-pick-meta muted mono">MT5 {{ n.mt5_login ?? '—' }}</span>
              </span>
              <span v-if="alreadyFollowingNodeIds.has(n.node_id)" class="tag green">已跟随</span>
            </li>
          </ul>
        </template>
      </div>

      <!-- 底部固定操作条：跟主页面的滚动区域分离，按钮永远够得着 -->
      <footer v-if="detail" class="m-sheet-foot">
        <span class="m-foot-count muted">
          已选 <strong class="m-foot-n">{{ selectedNodes.length }}</strong> 个节点
        </span>
        <button
          class="m-foot-btn"
          :disabled="!selectedNodes.length || submitting"
          @click="submitJoin"
        >
          {{ submitting ? '提交中...' : '确认跟单' }}
        </button>
      </footer>
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

/* 标题 + 「热门带单员」标签同一行（桌面 / 手机共用） */
.page-head-l {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

/* 透明底标签：只有描边和字色，没有填充 */
.page-head-tag {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  background: transparent;
  border: 1px solid rgba(117, 62, 228, 0.45);
  color: #b9a3f5;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
  white-space: nowrap;
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

/* 带单员名 + 「我的」标记
   名字和标签同一行居中对齐；名字过长时省略收缩，标签固定不缩 ——
   否则长名字会把标签挤到第二行，「谁的」和「叫什么」就分家了。 */
.leader-head {
  gap: 8px;
}
.leader-name {
  gap: 6px;
  min-width: 0;
}
/* 详情页标题 / 我的跟单标题：文字部分自己承担省略，标签不被挤到第二行 */
.leader-name-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 头像排在名字前，与名字留 8px（比 .leader-name 的 6px 稍宽，圆和字更好区分） */
.leader-avatar {
  margin: 0 2px;
}
.leader-mine {
  flex-shrink: 0;
  align-self: center;
}

/* 「已暂停带单」标记：与「我的」同款标签，但用告警色 —— 主人一眼能看出
   自己的带单当前是收口状态（列表里的「我的」那栏也要挂） */
.leader-paused {
  flex-shrink: 0;
  align-self: center;
}

/* 暂停提示条：说清「不动什么、动什么」，避免主人误以为暂停会踢掉跟单员 */
.pause-banner {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 10px 0 4px;
  padding: 10px 12px;
  border: 1px solid rgba(245, 158, 11, 0.28);
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.08);
  color: #fbbf24;
  font-size: 12px;
  line-height: 1.6;
}
.pause-banner strong {
  font-size: 13px;
}
.pause-banner span {
  color: var(--muted);
}

/* 带单员列表里的 4 项统计
   —— 桌面：主指标 + 副指标一行，胜率/跟单人数并排
   —— 手机：主指标放大成 hero 行，胜率/跟单人数压成 2 个小格子 */
.leader-stats {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--glass-border);
}

.ls-hero {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}
.ls-hero-main {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}
.ls-hero-side {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-shrink: 0;
}
.ls-hero-k {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
  flex-shrink: 0;
}
.ls-hero-v {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  letter-spacing: -0.01em;
}
.ls-hero-v.pos { color: var(--green); }
.ls-hero-v.neg { color: var(--red); }
.ls-hero-sub {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ls-hero-sub.pos { color: var(--green); }
.ls-hero-sub.neg { color: var(--red); }

.ls-mini-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.ls-mini {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
  padding: 4px 8px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: 6px;
}
.ls-mini-k {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
  flex-shrink: 0;
}
.ls-mini-v {
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ls-updated {
  font-size: 11px;
  text-align: right;
}

/* 手机端：hero 换行避免数字被挤压，字号保持与桌面一致 */
@media (max-width: 480px) {
  .ls-hero {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }
  .ls-hero-main { justify-content: space-between; }
  .ls-hero-side {
    justify-content: space-between;
    padding-top: 4px;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
  }
  .ls-mini { padding: 5px 8px; }
}

.sec-title {
  font-size: 13px;
  margin: 16px 0 8px;
  color: var(--muted);
}

/* 桌面端详情：近 7 日收益柱状图（统计卡片与「我的节点」之间） */
.detail-chart {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
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
/* 说明：ElDialog 会把内容 teleport 到 body，scoped 样式作用不到内部结构，
   所以「弹窗外壳」（el-dialog 自身的 header/body/footer 盒模型）必须写在
   文件末尾的非 scoped <style> 里；弹窗内容（lv-* / leave-*）是本组件的
   节点，留在 scoped 这段即可。 */

/* Header：标题 + 带单员名 + 关闭按钮 */
.lv-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.lv-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.lv-title-main {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
}
.lv-title-sub {
  font-size: 12px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lv-close {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass);
  color: var(--muted);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), border-color var(--transition);
}
.lv-close:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--glass-highlight);
}
.lv-close:disabled { opacity: 0.4; cursor: not-allowed; }

/* Body：flex 列，列表区可滚动 */
.lv-body {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-height: 0;
  flex: 1;
}
.lv-hint {
  font-size: 12px;
  line-height: 1.6;
  margin-bottom: 10px;
}

.leave-all {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 10px;
  border: 1px dashed var(--glass-border);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  background: var(--glass);
}

.leave-all label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 13px;
  min-width: 0;
}
.leave-all-text { font-weight: 600; flex-shrink: 0; }
.leave-all-count { color: var(--muted); font-size: 12px; }
.leave-all-selected {
  font-size: 11px;
  font-weight: 700;
  color: var(--primary);
  flex-shrink: 0;
}

.leave-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 46vh;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.leave-item {
  padding: 9px 11px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  transition: border-color var(--transition), background var(--transition);
}

/* 手机端没有 hover，选中态必须够醒目才反馈得到 */
.leave-item.on {
  border-color: var(--primary);
  background: rgba(37, 99, 235, 0.08);
}
.leave-item:active { background: rgba(37, 99, 235, 0.12); }

.leave-item label {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: inherit;
  min-width: 0;
}

/* 节点名 + MT5 账号竖排，长账号换行而不是把整行撑破 */
.lv-node-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.lv-node-meta {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lv-node-tasks {
  margin-left: 4px;
  color: var(--gold, #fbbf24);
}

/* 后果说明：让用户点确认前就知道会影响几个分组 / 几个运行中任务 */
.lv-impact {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 10px;
  padding: 8px 10px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
}
.lv-impact-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  min-width: 0;
}
.lv-impact-k { color: var(--muted); flex-shrink: 0; }
.lv-impact-v { font-weight: 600; text-align: right; }
.lv-impact-warn .lv-impact-v { color: var(--gold, #fbbf24); }

/* Footer：提示文字 + 操作按钮 */
.lv-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.lv-footer-hint {
  font-size: 12px;
  text-align: left;
  min-width: 0;
}
.lv-footer-btns {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* ── 手机端：弹窗贴顶全高，列表区滚动，按钮加大可点 ── */
@media (max-width: 600px) {
  .leave-list { max-height: none; }
  .lv-impact-row { flex-direction: column; align-items: flex-start; gap: 2px; }
  .lv-impact-v { text-align: left; }
}
</style>

<style>
/* 非 scoped：覆盖被 teleport 到 body 的 el-dialog 内部结构 */
.leave-dialog {
  --el-border-color-lighter: rgba(255, 255, 255, 0.06);
  --el-border-color: rgba(255, 255, 255, 0.1);
}
.leave-dialog .el-dialog {
  border-radius: var(--leave-radius, 16px);
  overflow: hidden;
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
}
.leave-dialog .el-dialog__header {
  padding: 14px 16px 12px;
  margin-right: 0;
  border-bottom: 1px solid var(--glass-border);
  flex-shrink: 0;
}
.leave-dialog .el-dialog__body {
  padding: 12px 16px;
  /* 用 flex 让「提示 + 全选」固定、只有列表滚动 */
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.leave-dialog .el-dialog__footer {
  padding: 10px 16px 14px;
  border-top: 1px solid var(--glass-border);
  background: transparent;
  flex-shrink: 0;
}

@media (max-width: 600px) {
  .leave-dialog { margin: 0 !important; }
  .leave-dialog .el-dialog {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100dvh !important;
    max-height: 100dvh !important;
    border-radius: 0 !important;
    margin: 0 !important;
  }
  /* 手机端撑满全屏后列表才有意义：吃掉除头尾以外的全部高度 */
  .leave-dialog .leave-list {
    flex: 1;
    min-height: 0;
    max-height: none;
    overflow-y: auto;
  }
  /* 按钮加大到可点的高度，并让「已选 N 个」独占一行 */
  .leave-dialog .lv-footer {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .leave-dialog .lv-footer-hint { text-align: center; }
  .leave-dialog .lv-footer-btns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .leave-dialog .lv-footer-btns button {
    height: 44px;
    width: 100%;
    font-size: 15px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
}
</style>

<!-- ══════════════════════════════════════════════════════════════════════
     手机端样式（ui-ux-pro-max: dark OLED · 触控 ≥44pt · 4/8dp 节奏 · 安全区）
     全部规则挂在 .m-only / .m-* 前缀上，桌面端 display:none，零影响。
     ══════════════════════════════════════════════════════════════════════ -->
<style scoped>
/* ── 可见性闸门：桌面端隐藏手机端专属层 ── */
.m-only { display: none; }

@media (max-width: 768px) {
  .m-only { display: block; }

  /* 横向边距只由外层 <main class="content"> 负责（≤640px 是 14px）。
     .follow-page 自己再叠一层 12px，窄屏上两侧实际空出 26px（平板更到 36px），
     卡片被推得离屏幕边很远。这里把自身横向内边距清零，只留纵向节奏和底部安全区。 */
  .follow-page {
    padding: 10px 0 calc(4px + env(safe-area-inset-bottom, 0px));
  }

  /* 桌面端的两栏（.follow-grid）在手机端整体隐藏。
     手机端改用下面的分段切换 + 全屏详情页。
     漏掉 .follow-grid 会导致桌面布局在手机上照样渲染，
     变成「上半截桌面 UI + 下半截手机 UI」重复两套 —— 必须隐藏。
     注意 .page-header 不再隐藏：手机端是单栏信息流，没有标题用户不知道自己在哪一页。 */
  .follow-grid {
    display: none;
  }

  /* 标题栏在手机端保留：手机端是单栏信息流，没有标题用户不知道自己在哪一页。
     全局 768px 规则会把 .page-header 改成 column + stretch，这里改回单行左对齐。 */
  .page-header {
    flex-direction: row;
    align-items: center;
    flex-wrap: nowrap;
    gap: 8px;
    margin-bottom: 12px;
  }
  /* 标题 + 「热门带单员」标签同一行 */
  .page-title {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.01em;
    margin: 0;
  }

  /* ── 分段切换（带单员 / 我的跟单）─────────────────────────────
     手机端这两份信息互斥，堆一起会变成超长滚动条。
     按钮高 36px（仍高于 32px 触控建议），底部滑动块做选中指示。 */
  .m-tabs {
    position: sticky;
    /* 顶栏（.topbar）也是 sticky top:0、z-index:20，尺寸 60px（≤640px 是 46px）。
       分段切换必须让开顶栏高度，否则滚动时会滑到顶栏底下被盖住。 */
    top: 60px;
    z-index: 12;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 3px;
    padding: 3px;
    margin: 0 0 10px;
    background: rgba(6, 10, 18, 0.9);
    border: 1px solid var(--glass-border);
    border-radius: 10px;
    backdrop-filter: blur(var(--blur)) saturate(1.3);
    -webkit-backdrop-filter: blur(var(--blur)) saturate(1.3);
  }
  .m-tab {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    /* 高度从 44px 收到 36px：仍高于 32px 的最小触控建议，
       但整行占用的垂直空间更省，留给下面的卡片。 */
    min-height: 36px;
    padding: 0 8px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: var(--muted);
    font-size: 13px;
    font-weight: 600;
    transition: color var(--transition), background var(--transition);
  }
  .m-tab:active { background: rgba(255, 255, 255, 0.05); }
  .m-tab.on {
    color: #ffffff;
    /* 参考设计主色是紫 #753EE4，顶部分段控件跟着换 */
    background: linear-gradient(135deg, #753ee4 0%, #5b21b6 100%);
    border-color: transparent;
    box-shadow: 0 2px 12px rgba(117, 62, 228, 0.4);
  }
  .m-tab-badge {
    min-width: 18px;
    padding: 0 5px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.1);
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    line-height: 1.5;
  }
  .m-tab.on .m-tab-badge {
    background: rgba(4, 21, 16, 0.28);
    color: #ffffff;
  }

  .m-panel { display: block; }

  /* ── 搜索框 ── */
  /* 字号必须 ≥16px：iOS Safari 聚焦小于 16px 的输入框会自动放大整个页面，
     用户无法缩回去。14/15px 会触发这个行为。 */
  .m-search-wrap { margin-bottom: 10px; }
  .m-search {
    min-height: 44px;
    border-radius: 12px;
    font-size: 16px;
    /* 搜索框跟着设计稿走：卡片实色底 + 聚焦紫色描边 */
    background: #121828;
    border-color: rgba(255, 255, 255, 0.08);
  }
  .m-search:focus {
    border-color: rgba(117, 62, 228, 0.65);
    box-shadow: 0 0 0 3px rgba(117, 62, 228, 0.16);
  }

  /* ── 统计周期：标签 + 横向滑动的分段控件，同一行 ── */
  .m-win-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    min-width: 0;
  }
  .m-win-label {
    flex-shrink: 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    white-space: nowrap;
  }
  .m-win {
    display: flex;
    gap: 6px;
    /* 4 个按钮在 320px 宽放不下，允许横向滑动而不是压缩文字 */
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    /* 让按钮组吃掉剩余宽度，标签不被挤掉 */
    flex: 1 1 auto;
    min-width: 0;
  }
  .m-win::-webkit-scrollbar { display: none; }
  .m-win-btn {
    flex: 1 0 auto;
    /* 高度从 36px 收到 30px，横向内边距 14→10，整行更紧凑 */
    min-height: 30px;
    padding: 0 10px;
    border-radius: 999px;
    /* 未选中态也带一点紫调，和参考设计的紫色主色统一 */
    border: 1px solid rgba(117, 62, 228, 0.28);
    background: rgba(117, 62, 228, 0.1);
    color: #b9a3f5;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    touch-action: manipulation;
    transition: color var(--transition), background var(--transition), border-color var(--transition);
  }
  .m-win-btn:active { background: rgba(117, 62, 228, 0.22); }
  .m-win-btn.on {
    color: #ffffff;
    /* 选中态实心紫，和顶部分段切换同一套紫色主色 */
    background: linear-gradient(135deg, #753ee4 0%, #5b21b6 100%);
    border-color: transparent;
    box-shadow: 0 2px 12px rgba(117, 62, 228, 0.4);
  }

  /* ── 带单员卡片 ──────────────────────────────────────────────
     版式对齐「带单广场」参考设计：卡片底色 #121828、紫色主色 #753EE4、
     蓝色辅色 #3762E4，比例（头像 40px、收益主数字 28px、三等分小格）
     也照抄设计稿。功能与数据源保持原样，只换呈现。 */
  .m-leader-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .m-leader {
    padding: 14px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 16px;
    /* 参考设计的卡片底色比全局 --panel 更实、更亮，这里用实色而非半透明 */
    background: #121828;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.32);
    cursor: pointer;
    touch-action: manipulation;
    transition: border-color var(--transition), background var(--transition);
  }
  /* 手机端没有 hover，用 active 给出点按反馈 */
  .m-leader:active {
    border-color: rgba(117, 62, 228, 0.55);
    background: #16203a;
  }
  .m-leader-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
    margin-bottom: 14px;
  }
  /* 头像 + 昵称（昵称在上、副标题在下） */
  .m-leader-id {
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 0;
  }
  .m-leader-id-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .m-leader-name {
    font-size: 15px;
    font-weight: 600;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 副标题：主节点名 / 「我开设的带单」+ 数据更新时间 */
  .m-leader-sub {
    font-size: 11px;
    color: var(--muted);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 右上角状态胶囊：设计稿是实心紫底 */
  .m-leader-badge {
    flex-shrink: 0;
    padding: 5px 12px;
    border-radius: 999px;
    background: rgba(117, 62, 228, 0.18);
    border: 1px solid rgba(117, 62, 228, 0.4);
    color: #a78bfa;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.2;
    white-space: nowrap;
  }
  .m-leader-badge.is-followed {
    background: rgba(55, 98, 228, 0.18);
    border-color: rgba(55, 98, 228, 0.45);
    color: #93b4fb;
  }

  /* 主视觉：收益在左（大号），收益率在右（小号），左右分栏对齐设计稿 */
  .m-pnl {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 14px;
    margin-bottom: 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }
  .m-pnl-left,
  .m-pnl-right {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }
  .m-pnl-right { text-align: right; }
  .m-pnl-main {
    font-family: var(--mono);
    font-size: 28px;
    font-weight: 700;
    line-height: 1.05;
    letter-spacing: -0.03em;
    font-variant-numeric: tabular-nums;
  }
  .m-pnl-label {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
  }
  .m-pnl-rate {
    font-family: var(--mono);
    font-size: 18px;
    font-weight: 700;
    line-height: 1.05;
    font-variant-numeric: tabular-nums;
  }
  .m-pnl-rate.pos { color: var(--green); }
  .m-pnl-rate.neg { color: var(--red); }
  .m-pnl-main.pos { color: var(--green); }
  .m-pnl-main.neg { color: var(--red); }
  .m-pnl-none { font-size: 13px; }

  /* 次要指标：三个等宽小格，卡片底部一行 */
  .m-mini-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .m-mini {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 6px;
    min-width: 0;
    padding: 8px 10px;
    /* 小格底色比卡片略深，做出设计稿里「卡片内嵌小格」的层次 */
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
  }
  .m-mini-k { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .m-mini-v {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  /* ── 空态 / 加载态 ── */
  .m-empty {
    padding: 40px 16px;
    text-align: center;
    font-size: 13px;
  }
  .m-empty-text { margin: 0 0 12px; font-size: 13px; }
  .m-empty-btn { min-height: 40px; }

  /* ── 我的跟单 ── */
  .m-mine-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .m-mine {
    padding: 14px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 16px;
    background: #121828;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.32);
  }
  .m-mine-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 10px;
  }
  .m-mine-id {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  /* 名字 + 头像 + 「我的」同排：名字过长时省略，头像和标签不被挤掉 */
  .m-mine-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .m-mine-name {
    font-size: 15px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 「我的」标记：手机端有多处（列表 / 我的跟单 / 详情页），统一一套尺寸 */
  .m-mine-flag {
    flex-shrink: 0;
    align-self: center;
  }
  .m-mine-tag { align-self: flex-start; }
  .m-mine-btn {
    flex-shrink: 0;
    min-height: 40px;
    padding: 0 14px;
  }
  .m-mine-none { margin: 0; font-size: 12px; }

  /* 节点条目：原来是"名称（MT5）· 加入于 时间戳 · 运行中 N 单"挤一行，
     窄屏必爆。改成两行：名称一行 + 账号/时间/任务数一行小字。 */
  .m-mine-nodes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .m-mine-node {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
  }
  .m-mine-node-name {
    font-size: 14px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .m-mine-node-login {
    font-size: 12px;
    color: var(--text);
  }
  .m-mine-node-meta { font-size: 11px; }

  /* ── 全屏详情页 ───────────────────────────────────────────────
     fixed 覆盖整个视口，顶部返回 + 底部固定操作条，中间内容滚动。
     用 100dvh 而不是 100vh：iOS 动态地址栏下 100vh 会超出可见区域。 */
  .m-sheet {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh;
    background: var(--bg);
    animation: m-sheet-in 0.22s cubic-bezier(0.32, 0.72, 0, 1);
  }
  @keyframes m-sheet-in {
    from { transform: translateY(12px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }

  .m-sheet-bar {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 4px;
    height: 52px;
    padding: 0 6px;
    padding-top: env(safe-area-inset-top, 0px);
    height: calc(52px + env(safe-area-inset-top, 0px));
    background: rgba(6, 10, 18, 0.92);
    border-bottom: 1px solid var(--glass-border);
    backdrop-filter: blur(var(--blur));
    -webkit-backdrop-filter: blur(var(--blur));
  }
  .m-back {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    padding: 0;
    background: transparent;
    border: none;
    color: var(--text);
    border-radius: 10px;
    touch-action: manipulation;
  }
  .m-back:active { background: rgba(255, 255, 255, 0.07); }
  .m-sheet-title {
    font-size: 16px;
    font-weight: 600;
  }

  /* 全屏详情页脱离 .content，独立控制内边距：横向收到 10px */
  .m-sheet-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: 14px 10px 14px;
  }

  /* 带单员身份区 */
  .m-d-lead { margin-bottom: 20px; }
  /* 名字 + 头像 + 「我的」同排，名字过长时省略而不是把标签挤到下一行 */
  .m-d-name-row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .m-d-name {
    font-size: 19px;
    font-weight: 700;
    letter-spacing: -0.01em;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 暂停带单按钮：手机端整行通栏，触控区够大（不与返回按钮抢位置） */
  .m-d-pause-btn {
    width: 100%;
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 13px;
  }
  .m-d-node { margin-top: 3px; font-size: 12px; }

  .m-d-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 14px;
  }
  .m-d-stat {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 11px 12px;
    background: #121828;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
  }
  .m-d-k { font-size: 11px; color: var(--muted); }
  .m-d-v {
    font-family: var(--mono);
    font-size: 17px;
    font-weight: 700;
    line-height: 1.2;
    font-variant-numeric: tabular-nums;
  }
  .m-d-v.pos { color: var(--green); }
  .m-d-v.neg { color: var(--red); }
  .m-d-updated { margin-top: 10px; font-size: 11px; }

  /* 手机端详情：近 7 日收益柱状图（在「选择要跟随的节点」上方） */
  .m-d-chart {
    margin-top: 16px;
    padding: 12px 12px 8px;
    background: #121828;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
  }

  .m-d-sec-title {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 20px 0 20px!important;
    font-size: 14px;
    font-weight: 600;
  }
  .m-d-sec-sub { font-size: 12px; font-weight: 400; }

  /* ── 节点选择项：整行 56px 触控区 ── */
  .m-pick-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .m-pick {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 56px;
    padding: 10px 12px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    background: #121828;
    cursor: pointer;
    touch-action: manipulation;
    transition: border-color var(--transition), background var(--transition);
  }
  .m-pick:active { background: #16203a; }
  /* 选中态：整块高亮 + 边框加粗，不靠颜色单独承载信息（另配勾选框） */
  .m-pick.on {
    border-color: rgba(117, 62, 228, 0.6);
    background: rgba(117, 62, 228, 0.12);
  }
  .m-pick.done { opacity: 0.55; cursor: not-allowed; }

  /* 自绘勾选框：原生 checkbox 在深色下对比度差且样式不可控 */
  .m-pick-box {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: 2px solid var(--glass-highlight);
    border-radius: 6px;
    color: transparent;
    transition: background var(--transition), border-color var(--transition), color var(--transition);
  }
  .m-pick.on .m-pick-box {
    background: #753ee4;
    border-color: #753ee4;
    color: #ffffff;
  }
  .m-pick-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .m-pick-name {
    font-size: 14px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .m-pick-meta { font-size: 11px; }

  /* ── 底部固定操作条 ──
     与滚动区分离，"确认跟单"永远在拇指可达的位置。 */
  .m-sheet-foot {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 10px;
    padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px));
    background: rgba(6, 10, 18, 0.94);
    border-top: 1px solid var(--glass-border);
    backdrop-filter: blur(var(--blur));
    -webkit-backdrop-filter: blur(var(--blur));
  }
  .m-foot-count { font-size: 13px; flex-shrink: 0; }
  .m-foot-n {
    font-family: var(--mono);
    font-size: 15px;
    color: var(--text);
  }
  .m-foot-btn {
    flex: 1;
    min-height: 48px;
    padding: 0 20px;
    background: linear-gradient(135deg, #753ee4 0%, #5b21b6 100%);
    color: #ffffff;
    font-size: 15px;
    font-weight: 600;
    border-radius: 12px;
    box-shadow: 0 4px 16px rgba(117, 62, 228, 0.4);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    touch-action: manipulation;
  }
  .m-foot-btn:disabled {
    opacity: 0.4;
    background: var(--panel-2);
    color: var(--muted);
    box-shadow: none;
    cursor: not-allowed;
  }
}

/* ── 减少动态效果偏好：关掉入场动画，避免前庭不适 ── */
@media (max-width: 768px) and (prefers-reduced-motion: reduce) {
  .m-sheet { animation: none; }
  .m-leader,
  .m-tab,
  .m-win-btn,
  .m-pick,
  .m-back,
  .m-foot-btn {
    transition: none;
  }
}

/* ── 顶栏在 ≤640px 收窄到 46px，分段切换的 sticky 偏移要跟着变小 ── */
@media (max-width: 640px) {
  .m-tabs { top: 46px; }
}

/* ── 极窄屏（iPhone SE 320px）：主视觉再收一档，避免换行 ── */
@media (max-width: 360px) {
  .m-pnl-main { font-size: 23px; }
  .m-pnl { gap: 8px; }
  .m-tab { font-size: 13px; }
  .m-d-v { font-size: 15px; }
  /* 320px 上三格仍然放得下（每格约 86px），只把内边距收紧保住三个指标 */
  .m-mini { padding: 7px 7px; }
  .m-mini-k { font-size: 10px; }
  .m-mini-v { font-size: 12px; }
}
</style>