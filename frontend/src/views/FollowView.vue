<script setup lang="ts">
// 带单（跟单）页：左侧带单员列表，右侧「我的跟单」
// 能力语义：跟单默认人人可做；带单员由管理员授予 follow_leader 角色能力产生。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import { useHubStore } from '@/stores/hub'
import api from '@/api/client'
import type { FollowChannelsOut, Position } from '@/api/types'

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
  groups: { group_id: string; group_name: string | null; active_task_count: number; symbol: string | null; strategy_name: string | null }[]
}

const auth = useAuthStore()
const hub = useHubStore()

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

/** 统计窗口。广场不放切换条，默认近 30 天，与后端 deal_stats 口径一致。 */
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
 * 暂停提示文案。
 *
 * 与后端 `follow_rules.PAUSED_FOLLOW_REASON` 保持同一句 —— 前端写侧兜底提示与
 * 服务端 400 的 detail 长得不一样时，客户会以为是两个问题。
 */
const PAUSED_FOLLOW_HINT = '该带单员已暂停带单，不再接收新的跟单'

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

/**
 * 节点当前是否**可勾选**。
 *
 * 三个不可选原因：带单员已暂停（整体收口）、已跟随该带单员、主节点（后端已剔除）。
 * 模板里到处要用这个判定（高亮 / 置灰 / aria 状态 / tabindex），
 * 各写一遍很容易走偏 —— 统一收敛到这里。
 */
function isNodeSelectable(id: string): boolean {
  if (detailPaused.value) return false
  return !alreadyFollowingNodeIds.value.has(id)
}

function toggleNode(id: string) {
  // 暂停期间整体不可选：跟单员不该在「选了也加不进去」的地方反复勾选。
  if (!isNodeSelectable(id)) return
  const s = new Set(selectedNodes.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedNodes.value = [...s]
}

async function submitJoin() {
  if (!detail.value) return
  // 暂停判定放在最前面：暂停时所有分组都不可选，下面「没有可加入分组」的分支
  // 会先一步返回，跟单员就看不到任何原因提示了 —— 按钮虽然已置灰，
  // 但置灰的**理由**必须说清楚，不能让人猜。
  if (detailPaused.value) {
    ElMessage.warning(PAUSED_FOLLOW_HINT)
    return
  }
  if (!allSelectableGroupIds.value.length || !selectedNodes.value.length) return
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
    pickerOpen.value = false
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
        symbol: g.symbol ?? null,
        strategy_name: g.strategy_name ?? null,
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

onMounted(() => {
  void loadLeaders()
  void loadMine()
  void hub.fetchNodes()
})

const pickerOpen = ref(false)
const pageMode = ref<'home' | 'mine'>('home')
const mineTab = ref<'list' | 'nodes'>('list')
const nodeQuery = ref('')
const nodeFilter = ref<'all' | 'online' | 'pos' | 'win' | 'loss' | 'off'>('all')
const selfDetail = ref<FollowDetailResponse | null>(null)

function leaderName(leader: FollowLeaderSummary): string {
  return leader.display_name || leader.username
}
function hasPerf(leader: FollowLeaderSummary): boolean {
  return !!leader.performance?.data_available
}
function fmtU(n: number): string {
  const abs = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return n >= 0 ? `+${abs}` : `-${abs}`
}
function fmtPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return '—'
  const digits = Math.abs(n) >= 50 ? 2 : 5
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
function nodeBalance(id: string): string {
  const n = hub.accounts[id]?.balance
  if (n == null) return '—'
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const myLeader = computed(() => leaders.value.find((l) => isMyLeader(l)) || null)

interface MagicHit { magic: number; group_id: string }
const followMagics = ref<Record<string, MagicHit[]>>({})
async function loadFollowBooks(nodeIds: string[]) {
  await Promise.all(nodeIds.map(async (id) => {
    if (!hub.accounts[id]) {
      try { await hub.fetchNodeAccount(id) } catch { /* 没有快照就先不展示持仓 */ }
    }
    if (followMagics.value[id]) return
    try {
      const { data } = await api.get<FollowChannelsOut>(`/api/nodes/${id}/follow-channels`)
      followMagics.value = {
        ...followMagics.value,
        [id]: data.magics.filter((m) => m.market === 'follow').map((m) => ({ magic: m.magic, group_id: m.group_id })),
      }
    } catch { /* 频道读取失败时跳过该节点 */ }
  }))
}
watch(myFollowing, (list) => {
  const ids = [...new Set(list.flatMap((e) => e.nodes.map((n) => n.node_id)))]
  if (ids.length) void loadFollowBooks(ids)
}, { immediate: true })

function entryPositions(entry: MyFollowingEntry): Array<Position & { nodeId: string }> {
  const groups = new Set(entry.groups.map((g) => g.group_id))
  const rows: Array<Position & { nodeId: string }> = []
  for (const node of entry.nodes) {
    const hits = (followMagics.value[node.node_id] || []).filter((m) => !groups.size || groups.has(m.group_id))
    const magics = new Set(hits.map((m) => m.magic))
    for (const p of hub.accounts[node.node_id]?.positions || []) {
      if (magics.has(p.magic)) rows.push({ ...p, nodeId: node.node_id })
    }
  }
  return rows
}
function bookStats(rows: Array<Position & { nodeId: string }>) {
  let volume = 0
  let profit = 0
  for (const p of rows) {
    volume += p.volume || 0
    profit += p.profit || 0
  }
  return { volume, profit, count: rows.length }
}

async function openFollowPicker(leader: FollowLeaderSummary) {
  await openLeader(leader)
  if (!detail.value) return
  selectedNodes.value = []
  const ids = (detail.value.my_nodes || []).map((n) => n.node_id)
  void loadFollowBooks(ids)
  pickerOpen.value = true
}
async function pauseMine() {
  if (!myLeader.value) return
  await openLeader(myLeader.value)
  await togglePause()
}
const closeBusy = ref(false)
async function closeRows(rows: Array<Position & { nodeId: string }>, title: string) {
  if (!rows.length) { ElMessage.info('没有可平的持仓'); return }
  try {
    await ElMessageBox.confirm(`确认平掉${title}的 ${rows.length} 笔持仓？`, '一键平仓', {
      type: 'warning', confirmButtonText: '确认平仓', cancelButtonText: '取消',
    })
  } catch { return }
  closeBusy.value = true
  try {
    const nodeIds = [...new Set(rows.map((r) => r.nodeId))]
    for (const row of rows) {
      await api.post(`/api/nodes/${row.nodeId}/follow-channels/close`, { market: 'follow', mode: 'ticket', ticket: row.ticket })
    }
    ElMessage.success('已提交平仓')
    await Promise.all(nodeIds.map((id) => hub.fetchNodeAccount(id).catch(() => {})))
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally { closeBusy.value = false }
}
function leaderBlurb(leader: FollowLeaderSummary): string {
  const node = leader.primary_node?.name || leader.primary_node?.node_id
  const head = node ? `${node} · ${leader.followable_group_count} 个可跟分组` : `${leader.followable_group_count} 个可跟分组`
  if (!hasPerf(leader)) return `${head} · ${followerCountOf(leader)} 人跟随`
  const perf = statsOf(leader)
  const rate = perf.profit_rate_valid ? ` · 收益率 ${(perf.profit_rate >= 0 ? '+' : '') + perf.profit_rate.toFixed(1)}%` : ''
  return `${head} · 净盈亏 ${fmtU(perf.total_profit)}${rate} · ${followerCountOf(leader)} 人跟随`
}
function fmtTime(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function groupTitle(name: string | null, strategy: string | null): string {
  return strategy || name || '跟单分组'
}
function posRows(nodeId: string, groupId: string): Array<Position & { nodeId: string }> {
  const hits = (followMagics.value[nodeId] || []).filter((m) => m.group_id === groupId)
  const magics = new Set(hits.map((m) => m.magic))
  return (hub.accounts[nodeId]?.positions || []).filter((p) => magics.has(p.magic)).map((p) => ({ ...p, nodeId }))
}
function entryGroupPositions(entry: MyFollowingEntry, groupId: string) {
  const rows: Array<Position & { nodeId: string }> = []
  for (const node of entry.nodes) {
    if (!node.groups.some((g) => g.group_id === groupId)) continue
    rows.push(...posRows(node.node_id, groupId))
  }
  return rows
}
function openMine() {
  pageMode.value = 'mine'
  mineTab.value = 'list'
}

const previewChips = computed(() => {
  const chips: { key: string; title: string; profit: number; hint: string; known: boolean }[] = []
  for (const entry of myFollowing.value) {
    for (const g of entry.groups) {
      const rows = entryGroupPositions(entry, g.group_id)
      const known = entry.nodes.some((n) => followMagics.value[n.node_id])
      chips.push({
        key: g.group_id,
        title: groupTitle(g.group_name, g.strategy_name),
        profit: bookStats(rows).profit,
        hint: `汇总 ${entry.nodes.filter((n) => n.groups.some((x) => x.group_id === g.group_id)).length} 个节点`,
        known,
      })
      if (chips.length >= 3) return chips
    }
  }
  return chips
})

const followStats = computed(() => {
  const running = myFollowing.value.filter((e) => e.marketable).length
  const perfs = myFollowing.value.map((e) => e.leader.performance).filter((p) => p?.data_available)
  const rates = perfs.filter((p) => p.profit_rate_valid)
  let floatProfit = 0
  let volume = 0
  let count = 0
  for (const entry of myFollowing.value) {
    const st = bookStats(entryPositions(entry))
    floatProfit += st.profit
    volume += st.volume
    count += st.count
  }
  return {
    running,
    profitKnown: perfs.length > 0,
    profit: perfs.reduce((s, p) => s + (p.total_profit || 0), 0),
    rateKnown: rates.length > 0,
    rate: rates.length ? rates.reduce((s, p) => s + p.profit_rate, 0) / rates.length : 0,
    floatProfit,
    volume,
    count,
  }
})

interface NodeCard {
  node_id: string
  name: string
  login: number | null
  online: boolean
  symbols: string
  leaders: string
  count: number
  volume: number
  profit: number
  balance: string
  balanceNum: number | null
}
const nodeCards = computed<NodeCard[]>(() => {
  const map = new Map<string, NodeCard & { syms: Set<string>; leadersSet: Set<string> }>()
  for (const entry of myFollowing.value) {
    for (const node of entry.nodes) {
      let card = map.get(node.node_id)
      if (!card) {
        const raw = hub.accounts[node.node_id]?.balance
        card = {
          node_id: node.node_id,
          name: node.name || node.node_id,
          login: node.mt5_login,
          online: hub.statuses[node.node_id] === 'online' || node.status === 'online',
          symbols: '',
          leaders: '',
          count: 0,
          volume: 0,
          profit: 0,
          balance: nodeBalance(node.node_id),
          balanceNum: raw == null ? null : raw,
          syms: new Set<string>(),
          leadersSet: new Set<string>(),
        }
        map.set(node.node_id, card)
      }
      card.leadersSet.add(leaderName(entry.leader))
      for (const g of node.groups) {
        const meta = entry.groups.find((x) => x.group_id === g.group_id)
        if (meta?.symbol) card.syms.add(meta.symbol)
      }
      const rows = node.groups.flatMap((g) => posRows(node.node_id, g.group_id))
      const seen = new Set<number>()
      for (const row of rows) {
        if (seen.has(row.ticket)) continue
        seen.add(row.ticket)
        card.count += 1
        card.volume += row.volume || 0
        card.profit += row.profit || 0
        if (row.symbol) card.syms.add(row.symbol)
      }
    }
  }
  return [...map.values()].map((card) => ({
    ...card,
    symbols: [...card.syms].join(' · ') || '—',
    leaders: [...card.leadersSet].join(' · '),
  }))
})
const nodeFilterCounts = computed(() => {
  const all = nodeCards.value
  return {
    all: all.length,
    online: all.filter((n) => n.online).length,
    pos: all.filter((n) => n.count > 0).length,
    win: all.filter((n) => n.profit > 0).length,
    loss: all.filter((n) => n.profit < 0).length,
    off: all.filter((n) => !n.online).length,
  }
})
const shownNodes = computed(() => {
  const q = nodeQuery.value.trim().toLowerCase()
  return nodeCards.value.filter((n) => {
    if (nodeFilter.value === 'online' && !n.online) return false
    if (nodeFilter.value === 'off' && n.online) return false
    if (nodeFilter.value === 'pos' && n.count === 0) return false
    if (nodeFilter.value === 'win' && !(n.profit > 0)) return false
    if (nodeFilter.value === 'loss' && !(n.profit < 0)) return false
    if (!q) return true
    return `${n.name} ${n.login ?? ''} ${n.symbols} ${n.leaders}`.toLowerCase().includes(q)
  })
})
const nodeSummary = computed(() => {
  const cards = nodeCards.value
  const wins = cards.filter((n) => n.profit > 0).length
  const volume = cards.reduce((s, n) => s + n.volume, 0)
  const profit = cards.reduce((s, n) => s + n.profit, 0)
  const count = cards.reduce((s, n) => s + n.count, 0)
  const balances = cards.map((n) => n.balanceNum)
  const balanceKnown = balances.length > 0 && balances.every((n) => n != null)
  const balance = balanceKnown ? balances.reduce((s, n) => s + (n || 0), 0) : null
  return { wins, volume, profit, count, balance }
})

function closeGroup(entry: MyFollowingEntry, groupId: string, title: string) {
  return closeRows(entryGroupPositions(entry, groupId), `「${title}」`)
}
function closeNodeCard(card: NodeCard) {
  const rows = [...new Map(
    myFollowing.value.flatMap((entry) => entry.groups.flatMap((g) => entryGroupPositions(entry, g.group_id)))
      .filter((row) => row.nodeId === card.node_id)
      .map((row) => [row.ticket, row]),
  ).values()]
  return closeRows(rows, `节点「${card.name}」`)
}
function closeAllMine() {
  const rows = [...new Map(
    myFollowing.value.flatMap((entry) => entryPositions(entry)).map((row) => [`${row.nodeId}:${row.ticket}`, row]),
  ).values()]
  return closeRows(rows, '全部跟单')
}
async function leaveOneGroup(entry: MyFollowingEntry, groupId: string, title: string) {
  const nodeIds = entry.nodes.filter((n) => n.groups.some((g) => g.group_id === groupId)).map((n) => n.node_id)
  if (!nodeIds.length) return
  try {
    await ElMessageBox.confirm(
      `将 ${nodeIds.length} 个节点从「${title}」移出。移出后不再接收该分组的新信号。`,
      '移出跟单',
      { type: 'warning', confirmButtonText: '确认移出', cancelButtonText: '取消' },
    )
  } catch { return }
  await doLeave(entry.leader, [groupId], nodeIds, false)
}
async function stopNode(card: NodeCard) {
  const hits = myFollowing.value.filter((entry) => entry.nodes.some((n) => n.node_id === card.node_id))
  if (!hits.length) return
  try {
    await ElMessageBox.confirm(
      `将节点「${card.name}」从 ${hits.length} 个带单关系中移出。进行中的任务保留，不再接收新信号。`,
      '移出跟单',
      { type: 'warning', confirmButtonText: '确认移出', cancelButtonText: '取消' },
    )
  } catch { return }
  for (const entry of hits) {
    const node = entry.nodes.find((n) => n.node_id === card.node_id)
    const groupIds = (node?.groups || []).map((g) => g.group_id)
    if (groupIds.length) await doLeave(entry.leader, groupIds, [card.node_id], false)
  }
}

const selfGroups = computed(() => selfDetail.value?.groups || [])
function selfPositions(groupId: string) {
  const id = myLeader.value?.primary_node?.node_id
  return id ? posRows(id, groupId) : []
}
function closeSelfGroup(groupId: string, title: string) {
  return closeRows(selfPositions(groupId), `「${title}」`)
}
async function loadSelfDetail() {
  const leader = myLeader.value
  if (!leader || !auth.canBeFollowLeader) { selfDetail.value = null; return }
  try {
    selfDetail.value = await fetchLeaderDetail(leader.leader_user_id, perfWindow.value)
  } catch {
    selfDetail.value = null
  }
  const id = leader.primary_node?.node_id
  if (!id) return
  if (!hub.accounts[id]) {
    try { await hub.fetchNodeAccount(id) } catch { /* 主节点没有快照时分组卡片仍可展示 */ }
  }
  void loadFollowBooks([id])
}
watch([myLeader, () => auth.canBeFollowLeader], () => { void loadSelfDetail() })
</script>

<template>
  <div class="fd">
    <template v-if="pageMode === 'home'">
      <section class="fd-intro">
        <div class="fd-intro-top">
        <div class="fd-intro-head">
          <span class="fd-mark">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M18 11v2h4v-2h-4zm-2 6.61c.96.71 2.21 1.65 3.2 2.39.4-.53.8-1.07 1.2-1.6-.99-.74-2.24-1.68-3.2-2.4-.4.54-.8 1.08-1.2 1.61zM20.4 5.6c-.4-.53-.8-1.07-1.2-1.6-.99.74-2.24 1.68-3.2 2.4.4.53.8 1.07 1.2 1.6.96-.72 2.21-1.65 3.2-2.4zM4 9c-1.1 0-2 .9-2 2v2c0 1.1.9 2 2 2h1v4h2v-4h1l5 3V6L8 9H4zm11.5 3c0-1.33-.58-2.53-1.5-3.35v6.69c.92-.81 1.5-2.01 1.5-3.34z"/></svg>
          </span>
          <div class="fd-intro-title">
            <p>带单介绍</p>
            <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M6.4 18 5 16.6 14.6 7H6V5h12v12h-2V8.4z"/></svg>带单跟单 · 策略同步频道</p>
          </div>
          <span class="fd-badge"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6zM10.6 15.2l5.6-5.6-1.4-1.4-4.2 4.2-1.8-1.8-1.4 1.4z"/></svg>平台认证</span>
        </div>
        <p class="fd-copy">本频道汇聚通过平台认证的带单员，选择信任的带单员点击「跟单」，即可自动同步其开仓 / 平仓操作，让专业策略替你打理行情，省去盯盘时间。</p>
        </div>
        <div class="fd-feats">
          <div class="purple"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5zm-1.06 14.54L7.4 12l1.41-1.41 2.12 2.12 4.24-4.24 1.41 1.41z"/></svg><div><b>认证带单员</b><span>平台严格审核入驻</span></div></div>
          <div class="blue"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 0 0 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg><div><b>策略自动同步</b><span>开平仓实时跟随</span></div></div>
          <div class="green"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg><div><b>收益透明可查</b><span>历史表现公开</span></div></div>
          <div class="amber"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M9 11.24V7.5a2.5 2.5 0 0 1 5 0v3.74c1.21-.81 2-2.18 2-3.74C16 5.01 13.99 3 11.5 3S7 5.01 7 7.5c0 1.56.79 2.93 2 3.74zm9.84 4.63-4.54-2.26c-.17-.07-.35-.11-.54-.11H13v-6c0-.83-.67-1.5-1.5-1.5S10 6.67 10 7.5v10.74l-3.43-.72c-.36-.07-.72.09-.92.4l-.55.83 5.18 2.59c.48.24 1.02.26 1.52.06l6.28-2.51c.66-.27.9-1.09.48-1.64z"/></svg><div><b>一键跟单</b><span>操作简单快捷</span></div></div>
        </div>
        <p class="fd-risk"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M11 17h2v-6h-2zm0-8h2V7h-2zm1-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/></svg>跟单前请仔细查看带单员历史表现与风险提示，市场有风险，跟单需谨慎</p>
      </section>

      <section class="fd-sec">
        <header>
          <p class="fd-kicker purple">带单广场<span>热门带单员</span></p>
          <button type="button" class="fd-ghost" @click="loadLeaders"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 0 1-8.66 3.46l-1.42 1.42A7 7 0 0 0 19 12a6.96 6.96 0 0 0-1.35-5.65zM6 12a6 6 0 0 1 6-6v1.5A4.5 4.5 0 1 0 16.5 12H18a6 6 0 0 1-12 0z"/></svg>刷新</button>
        </header>
        <p v-if="loading" class="fd-empty">加载中…</p>
        <p v-else-if="!leaders.length" class="fd-empty">暂无开放的带单员</p>
        <div v-else class="fd-grid">
          <article v-for="l in leaders" :key="l.leader_user_id" class="fd-leader">
            <span class="fd-avatar"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5z"/></svg></span>
            <div class="fd-leader-text">
              <p>
                <b>{{ leaderName(l) }}</b>
                <svg v-if="l.marketable" class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                <em v-if="l.marketable" class="ok">已认证</em>
                <em v-if="isMyLeader(l)" class="me">我的</em>
                <em v-if="l.follow_paused" class="pause">已暂停</em>
              </p>
              <p class="fd-blurb">{{ leaderBlurb(l) }}</p>
            </div>
            <button type="button" class="fd-follow" :disabled="!l.marketable || l.follow_paused || isMyLeader(l)" @click="openFollowPicker(l)">跟单<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
          </article>
        </div>
      </section>

      <section class="fd-sec">
        <header>
          <p class="fd-kicker green">我的跟单<span>{{ mine.summary.following_count }}</span></p>
          <button type="button" class="fd-ghost" @click="openMine">查看全部<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
        </header>
        <button type="button" class="fd-preview" @click="openMine">
          <span class="fd-preview-top">
            <span class="fd-swap"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg></span>
            <span>
              <b>我跟随的带单员</b>
              <small>{{ mine.summary.following_count }} 条跟单记录 · 点击查看收益与运行状态</small>
            </span>
            <svg class="fd-chev" viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>
          </span>
          <span v-if="previewChips.length" class="fd-chips">
            <span v-for="chip in previewChips" :key="chip.key">
              <b><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>{{ chip.title }}</b>
              <strong :class="!chip.known ? '' : chip.profit >= 0 ? 'up' : 'down'">{{ chip.known ? fmtU(chip.profit) + ' U' : '暂无持仓' }}</strong>
              <small>{{ chip.hint }}</small>
            </span>
          </span>
          <span v-else class="fd-preview-empty">还没有跟随的带单员</span>
        </button>
      </section>

      <section v-if="auth.canBeFollowLeader" class="fd-sec">
        <header>
          <p class="fd-kicker blue">我的带单<span>{{ selfGroups.length || (myLeader ? 1 : 0) }}</span></p>
          <em>仅带单员可见</em>
        </header>
        <p v-if="!myLeader" class="fd-empty">你有带单能力，但还没有可被跟随的主节点分组。</p>
        <article v-else class="fd-card">
          <header class="fd-card-top">
            <span class="fd-avatar lg"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5z"/></svg></span>
            <div>
              <p>
                <b>{{ leaderName(myLeader) }}</b>
                <svg class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                <em class="ok">已认证</em>
              </p>
              <small>{{ myLeader.primary_node ? (myLeader.primary_node.name || myLeader.primary_node.node_id) : '无主节点' }} · {{ myLeader.followable_group_count }} 个分组 · {{ followerCountOf(myLeader) }} 个节点跟随</small>
            </div>
            <div class="fd-card-side">
              <em :class="myLeader.follow_paused ? 'off' : 'on'"><i></i>{{ myLeader.follow_paused ? '已暂停' : '带单中' }}</em>
              <button type="button" class="fd-link" :disabled="pauseBusy" @click="pauseMine">{{ myLeader.follow_paused ? '恢复带单' : '暂停带单' }}</button>
            </div>
          </header>
          <p v-if="!selfGroups.length" class="fd-empty">主节点上还没有可展示的带单分组。</p>
          <div v-for="g in selfGroups" :key="g.group_id" class="fd-item">
            <div class="fd-item-top">
              <p>
                <svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>
                <b>{{ groupTitle(g.name, g.strategy_name ?? null) }}</b>
                <em v-if="g.symbol">{{ g.symbol }}</em>
                <span>{{ g.follower_count }} 个节点跟随 · 进行中 {{ g.active_task_count }}</span>
              </p>
              <em :class="g.active_task_count ? 'on' : 'off'"><i></i>{{ g.active_task_count ? '同步中' : '空闲' }}</em>
            </div>
            <div class="fd-book">
              <div class="fd-book-head"><span>订单列表</span><span>{{ g.symbol || '—' }} · {{ bookStats(selfPositions(g.group_id)).count }} 笔持仓 · 浮动 {{ fmtU(bookStats(selfPositions(g.group_id)).profit) }} U</span></div>
              <p v-if="!selfPositions(g.group_id).length" class="fd-empty">暂无持仓</p>
              <div v-for="p in selfPositions(g.group_id)" :key="p.ticket" class="fd-pos">
                <div>
                  <p><em :class="(p.type || '').toLowerCase() === 'buy' ? 'buy' : 'sell'">{{ (p.type || '').toLowerCase() === 'buy' ? 'BUY' : 'SELL' }}</em>{{ p.symbol }}</p>
                  <small>开仓 {{ fmtTime(p.time) }} · {{ (p.volume || 0).toFixed(2) }} 手 @ {{ fmtPx(p.price_open) }} · SL {{ fmtPx(p.sl) }} / TP {{ fmtPx(p.tp) }}</small>
                </div>
                <div class="fd-pos-side"><span class="dot">正在持仓</span><strong :class="(p.profit || 0) >= 0 ? 'up' : 'down'">{{ fmtU(p.profit || 0) }} U</strong></div>
              </div>
            </div>
            <div class="fd-stats">
              <div><span>持仓笔数</span><b>{{ bookStats(selfPositions(g.group_id)).count }} 笔</b></div>
              <div><span>合计手数</span><b>{{ bookStats(selfPositions(g.group_id)).volume.toFixed(2) }} 手</b></div>
              <div><span>浮动盈亏</span><b :class="bookStats(selfPositions(g.group_id)).profit >= 0 ? 'up' : 'down'">{{ fmtU(bookStats(selfPositions(g.group_id)).profit) }} U</b></div>
            </div>
            <footer>
              <button type="button" class="fd-close" :disabled="closeBusy || !selfPositions(g.group_id).length" @click="closeSelfGroup(g.group_id, groupTitle(g.name, g.strategy_name ?? null))"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>一键平仓</button>
            </footer>
          </div>
        </article>
      </section>
    </template>

    <template v-else>
      <button type="button" class="fd-back" @click="pageMode = 'home'">返回带单广场</button>
      <section class="fd-mine-hero">
        <div class="fd-mine-title">
          <span class="fd-mark"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg></span>
          <div><p>我的跟单</p><small>我跟随的带单员 · 策略同步与收益一览</small></div>
        </div>
        <div class="fd-stat3">
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 0 0 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>跟单中</span><b class="up">{{ myFollowing.length }} 个</b></div>
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>累计收益</span><b :class="followStats.profitKnown ? (followStats.profit >= 0 ? 'up' : 'down') : ''">{{ followStats.profitKnown ? fmtU(followStats.profit) : '暂无数据' }}</b></div>
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 11h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2zM5 5h2v2H5zm0 4h2v2H5zm0 4h2v2H5zM19 5h-8v2h8zm0 4h-8v2h8zm0 4h-8v2h8zM5 17h2v2H5zm4 0h10v2H9z"/></svg>平均收益率</span><b :class="followStats.rateKnown ? (followStats.rate >= 0 ? 'up' : 'down') : ''">{{ followStats.rateKnown ? (followStats.rate >= 0 ? '+' : '') + followStats.rate.toFixed(2) + '%' : '暂无数据' }}</b></div>
        </div>
        <p class="fd-note"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M11 17h2v-6h-2zm0-8h2V7h-2zm1-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/></svg>跟单按分组同步开仓和平仓。主节点不能用来跟单，绩效与成交历史同一口径，没有成交时显示暂无数据。</p>
      </section>

      <div class="fd-tabs">
        <button type="button" :class="{ on: mineTab === 'list' }" @click="mineTab = 'list'"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg>跟单列表<span>{{ myFollowing.length }}</span></button>
        <button type="button" :class="{ on: mineTab === 'nodes' }" @click="mineTab = 'nodes'"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg>子节点跟单统计<span>{{ nodeCards.length }}</span></button>
      </div>

      <section v-if="mineTab === 'list'" class="fd-sec">
        <header>
          <p class="fd-kicker blue">跟单列表<span>{{ myFollowing.length }} 位带单员</span></p>
        </header>
        <p v-if="mineLoading && !myFollowing.length" class="fd-empty">加载中…</p>
        <p v-else-if="!myFollowing.length" class="fd-empty">还没有跟随的带单员</p>
        <div v-else class="fd-grid">
          <article v-for="entry in myFollowing" :key="entry.leader.leader_user_id" class="fd-card">
            <header class="fd-card-top">
              <span class="fd-avatar lg"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5z"/></svg></span>
              <div>
                <p>
                  <b>{{ leaderName(entry.leader) }}</b>
                  <svg v-if="entry.marketable" class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                  <em v-if="entry.marketable" class="ok">已认证</em>
                  <em v-else class="pause">已结束</em>
                </p>
                <small>{{ entry.groups.map((g) => g.symbol || g.strategy_name || g.group_name).filter(Boolean).join(' · ') || '跟随分组' }}</small>
              </div>
              <div class="fd-card-side">
                <em :class="entry.marketable ? 'on' : 'off'"><i></i>{{ entry.nodes.length }} 个节点</em>
                <button type="button" class="fd-link" @click="openLeaveDialog(entry.leader)">移出节点</button>
              </div>
            </header>
            <p v-if="entry.unmarketable_reason" class="fd-empty">{{ entry.unmarketable_reason }}</p>
            <div v-for="g in entry.groups" :key="g.group_id" class="fd-item">
              <div class="fd-item-top">
                <p>
                  <svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>
                  <b>{{ groupTitle(g.group_name, g.strategy_name) }}</b>
                  <em v-if="g.symbol">{{ g.symbol }}</em>
                  <span>进行中 {{ g.active_task_count }}</span>
                </p>
                <em :class="g.active_task_count ? 'on' : 'off'"><i></i>{{ g.active_task_count ? '跟单中' : '空闲' }}</em>
              </div>
              <div class="fd-book">
                <div class="fd-book-head"><span>订单列表</span><span>{{ g.symbol || '—' }} · {{ bookStats(entryGroupPositions(entry, g.group_id)).count }} 笔持仓 · 浮动 {{ fmtU(bookStats(entryGroupPositions(entry, g.group_id)).profit) }} U</span></div>
                <p v-if="!entryGroupPositions(entry, g.group_id).length" class="fd-empty">暂无持仓</p>
                <div v-for="p in entryGroupPositions(entry, g.group_id)" :key="p.nodeId + p.ticket" class="fd-pos">
                  <div>
                    <p><em :class="(p.type || '').toLowerCase() === 'buy' ? 'buy' : 'sell'">{{ (p.type || '').toLowerCase() === 'buy' ? 'BUY' : 'SELL' }}</em>{{ p.symbol }}</p>
                    <small>开仓 {{ fmtTime(p.time) }} · {{ (p.volume || 0).toFixed(2) }} 手 @ {{ fmtPx(p.price_open) }} · SL {{ fmtPx(p.sl) }} / TP {{ fmtPx(p.tp) }}</small>
                  </div>
                  <div class="fd-pos-side"><span class="dot">正在持仓</span><strong :class="(p.profit || 0) >= 0 ? 'up' : 'down'">{{ fmtU(p.profit || 0) }} U</strong></div>
                </div>
              </div>
              <div class="fd-stats">
                <div><span>持仓笔数</span><b>{{ bookStats(entryGroupPositions(entry, g.group_id)).count }} 笔</b></div>
                <div><span>合计手数</span><b>{{ bookStats(entryGroupPositions(entry, g.group_id)).volume.toFixed(2) }} 手</b></div>
                <div><span>浮动盈亏</span><b :class="bookStats(entryGroupPositions(entry, g.group_id)).profit >= 0 ? 'up' : 'down'">{{ fmtU(bookStats(entryGroupPositions(entry, g.group_id)).profit) }} U</b></div>
              </div>
              <footer>
                <button type="button" class="fd-leave" @click="leaveOneGroup(entry, g.group_id, groupTitle(g.group_name, g.strategy_name))">移出</button>
                <button type="button" class="fd-close" :disabled="closeBusy" @click="closeGroup(entry, g.group_id, groupTitle(g.group_name, g.strategy_name))"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>一键平仓</button>
              </footer>
            </div>
          </article>
        </div>
      </section>

      <section v-else class="fd-sec">
        <header>
          <p class="fd-kicker blue">子节点跟单统计<span>{{ nodeCards.length }} 个节点</span></p>
          <button type="button" class="fd-close" :disabled="closeBusy || !followStats.count" @click="closeAllMine"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>一键平仓</button>
        </header>
        <p class="fd-sub">每个子节点独立统计持仓、手数和浮动盈亏。平仓只平该节点上属于带单频道的持仓。</p>
        <div class="fd-filters">
          <button type="button" :class="{ on: nodeFilter === 'all' }" @click="nodeFilter = 'all'">全部 {{ nodeFilterCounts.all }}</button>
          <button type="button" :class="{ on: nodeFilter === 'online' }" @click="nodeFilter = 'online'">在线 {{ nodeFilterCounts.online }}</button>
          <button type="button" :class="{ on: nodeFilter === 'pos' }" @click="nodeFilter = 'pos'">持仓 {{ nodeFilterCounts.pos }}</button>
          <button type="button" :class="{ on: nodeFilter === 'win' }" @click="nodeFilter = 'win'">浮盈 {{ nodeFilterCounts.win }}</button>
          <button type="button" :class="{ on: nodeFilter === 'loss' }" @click="nodeFilter = 'loss'">浮亏 {{ nodeFilterCounts.loss }}</button>
          <button type="button" :class="{ on: nodeFilter === 'off' }" @click="nodeFilter = 'off'">离线 {{ nodeFilterCounts.off }}</button>
        </div>
        <label class="fd-search"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg><input v-model="nodeQuery" placeholder="搜索子节点：名称 / 账户 / 品种 / 带单员" /></label>
        <div class="fd-sum4">
          <div><span>跟单节点</span><b>{{ nodeCards.length }} 个</b></div>
          <div><span>持仓手数</span><b>{{ nodeSummary.volume.toFixed(2) }} 手</b></div>
          <div><span>账户余额</span><b>{{ nodeSummary.balance == null ? '—' : nodeSummary.balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</b></div>
          <div><span>总浮动盈亏</span><b :class="nodeSummary.profit >= 0 ? 'up' : 'down'">{{ fmtU(nodeSummary.profit) }}</b></div>
        </div>
        <p class="fd-sub">盈利节点 {{ nodeSummary.wins }} / {{ nodeCards.length }} · 共持仓 {{ nodeSummary.count }} 笔</p>
        <p v-if="!shownNodes.length" class="fd-empty">没有符合条件的节点</p>
        <article v-for="card in shownNodes" :key="card.node_id" class="fd-node-card">
          <header>
            <span class="fd-avatar"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M4 6h16v2H4zm2 4h12v8H6z"/></svg></span>
            <div>
              <b>{{ card.name }}</b>
              <small>{{ card.login ?? '—' }} · {{ card.symbols }} · {{ card.leaders }}</small>
            </div>
            <em :class="card.online ? 'on' : 'off'"><i></i>{{ card.online ? '在线' : '离线' }}</em>
          </header>
          <div class="fd-pnl" :class="card.profit >= 0 ? 'up' : 'down'"><span>浮动盈亏</span><b>{{ fmtU(card.profit) }}</b></div>
          <div class="fd-stats">
            <div><span>持仓笔数</span><b>{{ card.count }} 笔</b></div>
            <div><span>手数</span><b>{{ card.volume.toFixed(2) }}</b></div>
            <div><span>账户余额</span><b>{{ card.balance }}</b></div>
          </div>
          <footer>
            <button type="button" class="fd-leave" @click="stopNode(card)">移出</button>
            <button type="button" class="fd-close" :disabled="closeBusy || !card.count" @click="closeNodeCard(card)">{{ card.count ? '一键平仓' : '已平仓' }}</button>
          </footer>
        </article>
      </section>
    </template>

    <div v-if="pickerOpen && detail" class="fd-mask" @click.self="pickerOpen = false">
      <section class="fd-dialog" role="dialog" aria-label="选择跟单节点">
        <header>
          <div>
            <strong>选择跟单节点</strong>
            <p>跟单对象：{{ leaderName(detail.leader) }}</p>
          </div>
          <button type="button" class="fd-x" @click="pickerOpen = false">×</button>
        </header>
        <p v-if="detailPaused" class="fd-warn">{{ PAUSED_FOLLOW_HINT }}</p>
        <p v-else-if="detailLoading" class="fd-empty">加载节点…</p>
        <p v-else-if="!(detail.my_nodes || []).length" class="fd-empty">没有可跟单的节点。主节点不能用于跟单。</p>
        <button
          v-for="n in detail.my_nodes"
          :key="n.node_id"
          type="button"
          class="fd-pick"
          :class="{ on: selectedNodes.includes(n.node_id), off: !isNodeSelectable(n.node_id) }"
          :disabled="!isNodeSelectable(n.node_id)"
          @click="toggleNode(n.node_id)"
        >
          <span class="box">{{ selectedNodes.includes(n.node_id) ? '✓' : '' }}</span>
          <span>
            <b>{{ n.name || n.node_id }}</b>
            <small>{{ n.node_id }} · 余额 {{ nodeBalance(n.node_id) }}</small>
          </span>
        </button>
        <footer>
          <button type="button" @click="pickerOpen = false">取消</button>
          <button type="button" class="primary" :disabled="submitting || detailPaused || !selectedNodes.length" @click="submitJoin">使用 {{ selectedNodes.length }} 个节点跟单</button>
        </footer>
      </section>
    </div>

    <ElDialog v-model="leaveDialogVisible" title="移出跟单" width="420px">
      <p v-if="leaveDialog" class="fd-empty">从「{{ leaderName(leaveDialog.leader) }}」移出选中的节点。节点会退出它跟随的全部分组。</p>
      <label v-if="leaveDialog" class="fd-pick">
        <input type="checkbox" :checked="leaveAllSelected" @change="toggleAllLeaveNodes" />
        全选
      </label>
      <label v-for="n in leaveDialog?.entry.nodes || []" :key="n.node_id" class="fd-pick">
        <input type="checkbox" :checked="leaveDialog?.selected.includes(n.node_id)" @change="toggleLeaveNode(n.node_id)" />
        {{ n.name || n.node_id }}
      </label>
      <template #footer>
        <button type="button" :disabled="leaveSubmitting" @click="closeLeaveDialog">取消</button>
        <button type="button" class="primary" :disabled="leaveSubmitting || !leaveDialog?.selected.length" @click="confirmLeave">确认移出</button>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.fd { display: flex; flex-direction: column; gap: 12px; color: #E6EBF4; }
.fd svg { width: 14px; height: 14px; flex: 0 0 auto; }
.fd-intro, .fd-card, .fd-leader, .fd-preview, .fd-node-card, .fd-dialog, .fd-mine-hero {
  border: 1px solid #1C2740; border-radius: 12px; background: #101828;
  box-shadow: 0 2px 12px rgba(0,0,0,.35);
}
.fd-intro { position: relative; overflow: hidden; }
.fd-intro-top { position: relative; padding: 16px 16px 14px; background: linear-gradient(90deg, #141034, #1B1A45 55%, #101828); }
.fd-intro-top::before, .fd-intro-top::after { content: ''; position: absolute; border-radius: 99px; pointer-events: none; }
.fd-intro-top::before { right: -24px; top: -32px; width: 96px; height: 96px; background: rgba(124,58,237,.2); filter: blur(16px); }
.fd-intro-top::after { right: 40px; bottom: -36px; width: 80px; height: 80px; background: rgba(37,99,235,.15); filter: blur(16px); }
.fd-intro-head, .fd-sec header, .fd-card-top, .fd-item-top, .fd-book-head, .fd-pos, .fd-node-card header, .fd-node-card footer, .fd-item footer, .fd-dialog header, .fd-dialog footer, .fd-pick, .fd-preview-top, .fd-mine-title, .fd-tabs, .fd-filters {
  display: flex; align-items: center; gap: 8px;
}
.fd-intro-head { position: relative; padding: 0; }
.fd-mark, .fd-avatar, .fd-swap { display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
.fd-mark { width: 44px; height: 44px; border-radius: 16px; color: #fff; background: linear-gradient(135deg, #7C3AED, #2563EB); box-shadow: 0 4px 14px rgba(124,58,237,.45); }
.fd-mark svg { width: 22px; height: 22px; }
.fd-intro-title { min-width: 0; flex: 1; }
.fd-intro-title p:first-child, .fd-mine-title p { margin: 0; font-size: 15px; font-weight: 700; }
.fd-intro-title p:last-child, .fd-mine-title small, .fd-blurb, .fd-empty, .fd-sub, .fd-leader small, .fd-card small, .fd-preview small, .fd-note, .fd-node-card small {
  display: flex; align-items: center; gap: 4px; margin: 2px 0 0; color: #8B96A8; font-size: 10px;
}
.fd-intro-title svg { width: 12px; height: 12px; color: #5B6B85; }
.fd-badge { display: flex; align-items: center; gap: 4px; border: 1px solid rgba(124,58,237,.4); background: rgba(124,58,237,.15); color: #C4B5FD; border-radius: 999px; padding: 4px 10px; font-size: 10px; }
.fd-copy { position: relative; display: block; margin: 12px 0 0; color: #8B96A8; line-height: 1.625; font-size: 11px; }
.fd-feats { position: relative; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 12px; }
.fd-feats > div { display: flex; gap: 8px; align-items: flex-start; border-radius: 8px; padding: 10px; }
.fd-feats svg { width: 17px; height: 17px; }
.fd-feats b { display: block; font-size: 11px; font-weight: 650; color: #E6EBF4; }
.fd-feats span { display: block; margin-top: 2px; color: #8B96A8; font-size: 9px; }
.fd-feats .purple { border: 1px solid rgba(124,58,237,.3); background: rgba(124,58,237,.1); color: #A78BFA; }
.fd-feats .blue { border: 1px solid rgba(37,99,235,.3); background: rgba(37,99,235,.1); color: #60A5FA; }
.fd-feats .green { border: 1px solid rgba(22,163,74,.3); background: rgba(22,163,74,.1); color: #4ADE80; }
.fd-feats .amber { border: 1px solid rgba(217,119,6,.3); background: rgba(217,119,6,.1); color: #FBBF24; }
.fd-risk, .fd-note { display: flex; gap: 6px; align-items: flex-start; margin: 0 12px 12px; border: 1px solid rgba(180,83,9,.25); border-radius: 8px; padding: 8px 12px; background: linear-gradient(90deg, rgba(180,83,9,.1), transparent); color: #9AA6B8; font-size: 10px; line-height: 1.5; }
.fd-risk svg, .fd-note svg { color: #F59E0B; width: 13px; height: 13px; }
.fd-note { margin: 12px 0 0; }
.fd-sec header { justify-content: space-between; }
.fd-kicker { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 12px; font-weight: 650; }
.fd-kicker::before { content: ''; width: 2px; height: 12px; border-radius: 2px; background: #7C3AED; }
.fd-kicker.green::before { background: #22C55E; }
.fd-kicker.blue::before { background: #2563EB; }
.fd-kicker span, .fd-sec header em { border: 1px solid #22304A; background: #0C1220; color: #5B6B85; border-radius: 999px; padding: 0 6px; font-size: 10px; font-style: normal; font-weight: 500; }
.fd-ghost, .fd-follow, .fd-close, .fd-leave, .fd-link, .fd-back, .fd-tabs button, .fd-filters button, .fd-dialog button, .fd-pick { cursor: pointer; }
.fd-ghost { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 4px 8px; font-size: 10px; }
.fd-grid { margin-top: 8px; display: grid; gap: 10px; }
.fd-leader, .fd-card, .fd-node-card { padding: 12px; }
.fd-leader { display: flex; align-items: center; gap: 8px; }
.fd-leader:hover, .fd-card:hover, .fd-preview:hover { border-color: rgba(124,58,237,.5); }
.fd-avatar { width: 32px; height: 32px; border-radius: 8px; color: #A78BFA; background: rgba(124,58,237,.15); }
.fd-avatar.lg { width: 36px; height: 36px; }
.fd-avatar svg { width: 16px; height: 16px; }
.fd-leader-text, .fd-card-top > div, .fd-node-card header > div { min-width: 0; flex: 1; }
.fd-leader-text p, .fd-card-top p { display: flex; align-items: center; gap: 4px; margin: 0; min-width: 0; }
.fd-leader-text b, .fd-card-top b, .fd-node-card b { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fd-verified { width: 12px; height: 12px; color: #F59E0B; }
.fd-leader em, .fd-card em, .fd-item em, .fd-node-card em { font-style: normal; font-size: 9px; font-weight: 650; border-radius: 999px; padding: 1px 6px; }
.fd em.ok, .fd em.on { color: #22C55E; background: rgba(34,197,94,.1); border: 1px solid rgba(34,197,94,.4); }
.fd em.me { color: #93C5FD; background: rgba(37,99,235,.15); border: 1px solid rgba(37,99,235,.3); }
.fd em.pause, .fd em.off { color: #8B96A8; background: rgba(91,107,133,.15); border: 1px solid #22304A; }
.fd em.on, .fd em.off { display: inline-flex; align-items: center; gap: 4px; }
.fd em i { width: 6px; height: 6px; border-radius: 99px; background: currentColor; }
.fd-blurb { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.5; }
.fd-follow { display: flex; align-items: center; gap: 2px; border: 0; border-radius: 8px; background: #7C3AED; color: #fff; font-size: 10px; font-weight: 700; padding: 6px 10px; }
.fd-follow:disabled, .fd-close:disabled { opacity: .4; cursor: not-allowed; }
.fd-preview { width: 100%; margin-top: 8px; text-align: left; padding: 12px; color: inherit; background: linear-gradient(90deg, #101828, #0F1E2E); }
.fd-swap { width: 36px; height: 36px; border-radius: 12px; color: #22C55E; background: rgba(34,197,94,.15); }
.fd-swap svg { width: 18px; height: 18px; }
.fd-preview-top b { display: block; font-size: 12px; }
.fd-chev { color: #4A5568; }
.fd-chips { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; width: 100%; margin-top: 10px; padding-top: 10px; border-top: 1px solid #1E2B47; }
.fd-chips > span, .fd-stats > div, .fd-sum4 > div, .fd-stat3 > div { min-width: 0; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 6px 8px; }
.fd-chips b { display: flex; align-items: center; gap: 4px; font-size: 9px; }
.fd-chips b svg { color: #A78BFA; width: 11px; height: 11px; }
.fd-chips strong, .fd-stat3 b, .fd-stats b, .fd-sum4 b { display: block; margin-top: 2px; font-size: 11px; }
.fd-chips small, .fd-stats span, .fd-sum4 span, .fd-stat3 span { display: flex; align-items: center; gap: 4px; color: #5B6B85; font-size: 9px; }
.fd-preview-empty { display: block; margin-top: 8px; color: #5B6B85; font-size: 11px; }
.fd-card { margin-top: 8px; }
.fd-card-side { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.fd-link { border: 0; background: transparent; color: #3B82F6; font-size: 10px; }
.fd-item { margin-top: 10px; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 10px; }
.fd-item-top { justify-content: space-between; }
.fd-item-top p { display: flex; align-items: center; gap: 6px; min-width: 0; margin: 0; }
.fd-item-top svg { color: #A78BFA; }
.fd-item-top b { font-size: 11px; }
.fd-item-top em { border-radius: 4px; }
.fd-item-top span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5B6B85; font-size: 9px; }
.fd-book { margin-top: 8px; overflow: hidden; border: 1px solid #1E2B47; border-radius: 6px; background: #0A101E; }
.fd-book-head { justify-content: space-between; padding: 6px 8px; background: #0C1220; border-bottom: 1px solid #1E2B47; color: #8B96A8; font-size: 9px; }
.fd-book .fd-empty { padding: 8px; }
.fd-pos { justify-content: space-between; gap: 8px; padding: 6px 8px; border-bottom: 1px solid #161F30; }
.fd-pos:last-child { border-bottom: 0; }
.fd-pos p { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 11px; }
.fd-pos em { font-style: normal; font-size: 9px; font-weight: 700; border-radius: 4px; padding: 1px 4px; }
.fd-pos em.buy { color: #22C55E; background: rgba(34,197,94,.15); }
.fd-pos em.sell { color: #F6465D; background: rgba(246,70,93,.15); }
.fd-pos small { color: #5B6B85; font-size: 9px; }
.fd-pos-side { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.fd-pos-side .dot { color: #60A5FA; font-size: 10px; }
.fd-pos-side .dot::before { content: ''; display: inline-block; width: 6px; height: 6px; margin-right: 4px; border-radius: 99px; background: #3B82F6; }
.up { color: #22C55E; } .down { color: #F6465D; }
.fd-stats, .fd-sum4 { margin-top: 8px; display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; }
.fd-sum4 { grid-template-columns: 1fr 1fr; }
.fd-stats > div, .fd-sum4 > div { background: #0A101E; border-color: #1E2B47; }
.fd-item footer, .fd-node-card footer { justify-content: flex-end; margin-top: 8px; padding-top: 8px; border-top: 1px solid #1E2B47; }
.fd-close { display: inline-flex; align-items: center; gap: 4px; border: 0; border-radius: 8px; background: #F6465D; color: #fff; font-size: 11px; font-weight: 650; padding: 6px 12px; box-shadow: 0 2px 8px rgba(246,70,93,.35); }
.fd-leave { border: 1px solid rgba(246,70,93,.7); background: transparent; color: #F6465D; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 650; }
.fd-back { align-self: flex-start; border: 0; background: transparent; color: #8B96A8; font-size: 12px; }
.fd-mine-hero { padding: 12px; background: linear-gradient(135deg, #101828, #101C38); }
.fd-stat3 { margin-top: 12px; display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 8px; }
.fd-stat3 > div { background: #101828; }
.fd-stat3 b { font-size: 16px; }
.fd-tabs { flex-wrap: wrap; }
.fd-tabs button { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 650; }
.fd-tabs button span { border-radius: 999px; background: rgba(0,0,0,.25); padding: 0 6px; font-size: 9px; }
.fd-tabs button.on { border: 0; color: #fff; background: linear-gradient(90deg, #2563EB, #1D4ED8); box-shadow: 0 2px 8px rgba(37,99,235,.35); }
.fd-sub { margin: 8px 0 0; }
.fd-filters { flex-wrap: wrap; margin-top: 8px; }
.fd-filters button { border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 999px; padding: 4px 10px; font-size: 11px; }
.fd-filters button.on { color: #fff; background: #2563EB; border-color: #2563EB; }
.fd-search { display: flex; align-items: center; gap: 6px; margin-top: 8px; border: 1px solid #22304A; background: #0C1220; border-radius: 10px; padding: 0 10px; color: #5B6B85; }
.fd-search input { flex: 1; height: 34px; border: 0; background: transparent; color: #E6EBF4; outline: none; font-size: 12px; }
.fd-node-card { margin-top: 8px; }
.fd-node-card header { justify-content: space-between; }
.fd-pnl { display: flex; justify-content: space-between; margin-top: 8px; border-radius: 8px; padding: 8px 10px; font-size: 12px; }
.fd-pnl.up { background: rgba(22,163,74,.16); }
.fd-pnl.down { background: rgba(246,70,93,.12); }
.fd-mask { position: fixed; inset: 0; z-index: 40; display: flex; align-items: center; justify-content: center; padding: 16px; background: rgba(0,0,0,.55); }
.fd-dialog { width: min(560px, 100%); max-height: 86vh; overflow: auto; padding: 14px; background: #0D1626; }
.fd-dialog header, .fd-dialog footer { justify-content: space-between; }
.fd-dialog p { margin: 2px 0 0; color: #8B96A8; font-size: 12px; }
.fd-x { width: 28px; height: 28px; border-radius: 8px; border: 1px solid #22304A; background: #0A0F1A; color: #8B96A8; }
.fd-pick { width: 100%; text-align: left; margin-top: 8px; padding: 8px; border-radius: 10px; border: 1px solid #22304A; background: #0C1220; color: inherit; }
.fd-pick.on { border-color: #7C3AED; }
.fd-pick.off { opacity: .45; }
.fd-pick .box { width: 16px; height: 16px; border: 1px solid #3D4C66; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; }
.fd-pick small { display: block; color: #8B96A8; }
.fd-dialog .primary, .fd :deep(.el-dialog__footer) .primary { background: #7C3AED; color: #fff; border: 0; border-radius: 8px; padding: 6px 12px; }
.fd-warn { color: #FBBF24; font-size: 12px; }
@media (min-width: 1024px) {
  .fd-grid { grid-template-columns: 1fr 1fr; }
  .fd-sum4 { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 720px) {
  .fd-chips, .fd-stat3, .fd-stats { grid-template-columns: 1fr; }
  .fd-leader, .fd-card-top, .fd-item-top { flex-wrap: wrap; }
}
</style>
