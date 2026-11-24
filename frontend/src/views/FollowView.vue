<script setup lang="ts">
// 带单（跟单）页：左侧带单员列表，右侧「我的跟单」
// 能力语义：跟单默认人人可做；带单员由管理员授予 follow_leader 角色能力产生。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElDialog, ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { fetchLeaders, fetchLeaderDetail, fetchMyFollowings, joinFollow, leaveFollow, setFollowPaused, setMyFollowState } from '@/api/follow'
import type {
  FollowDetailResponse,
  FollowLeaderSummary,
  FollowMeResponse,
  FollowMyGroup,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import api from '@/api/client'
import type { FollowChannelsOut, Position } from '@/api/types'
import type { FollowApplication } from '@/api/types'
import { trackFollowUi } from '@/utils/followAudit'
import { pageChrome } from '@/utils/pageChrome'

/** 「我的跟单」聚合后的单条节点（已按 leader 聚合、按 node_id 去重） */
interface MyFollowedNode {
  node_id: string
  name: string | null
  mt5_login: number | null
  status: string | null
  joined_at: number | null
  /** 该节点被跟随的**全部分组**（同一 leader 下可能同时跟多个分组，移出时必须一次性清掉） */
  groups: { group_id: string; group_name: string | null }[]
}

/** 「我的跟单」聚合后的单条 leader 卡片数据 */
interface MyFollowingEntry {
  leader: FollowLeaderSummary
  nodes: MyFollowedNode[]
  /** 我在该 leader 下跟的分组（分组级活跃任务数只此一份，不能按节点重复累加） */
  groups: FollowMyGroup[]
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
const pickerOpen = ref(false)
const pickerQuery = ref('')
const pickerPanel = ref<HTMLElement | null>(null)
let pickerTrigger: HTMLElement | null = null
let followTimer: number | undefined
let pickerSearchTimer: number | undefined

const mine = ref<FollowMeResponse>({ following: [], applications: [], summary: { following_count: 0, follow_node_count: 0 } })
const mineLoading = ref(false)
let mineRequest = 0

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
  const request = ++mineRequest
  mineLoading.value = true
  try {
    const result = await fetchMyFollowings()
    if (request === mineRequest) mine.value = result
  } catch (e) {
    ElMessage.error(errText(e, '加载我的跟单失败'))
  } finally {
    if (request === mineRequest) mineLoading.value = false
  }
}

function refreshFollow(): void {
  trackFollowUi('refresh')
  void Promise.all([loadLeaders(), loadMine()])
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

const selectableGroups = computed(() =>
  (detail.value?.groups ?? []).filter((g) => g.selectable),
)
/**
 * 目标分组 = 全部可跟分组。
 *
 * 早期让客户在弹窗里手选分组，学习成本高且容易漏选（跟了一半才发现少了一组）。
 * 「跟单」是节点级语义：勾了节点就是这几组全跟，所以直接默认全选，不再暴露分组勾选。
 */
const targetGroupIds = computed(() => selectableGroups.value.map((g) => g.group_id))

/** 跟自己的带单时，主节点已是带单源成员，不能再作为跟单节点加入同一分组。 */
function isOwnSourceNode(id: string): boolean {
  const leader = detail.value?.leader
  return !!leader && isMyLeader(leader) && leader.primary_node?.node_id === id
}

// 至少一个目标分组尚未生效或待审时，节点仍可补申请。未选分组时先允许勾节点。
function isNodeSelectable(id: string): boolean {
  if (detailPaused.value || isOwnSourceNode(id)) return false
  const groups = targetGroupIds.value
  if (!groups.length) return selectableGroups.value.length > 1
  return groups.some((gid) => !occupiedPairs.value.has(`${gid}:${id}`))
}

function toggleNode(id: string) {
  // 暂停期间整体不可选：跟单员不该在「选了也加不进去」的地方反复勾选。
  if (!isNodeSelectable(id)) return
  const s = new Set(selectedNodes.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedNodes.value = [...s]
  trackFollowUi('select_nodes', { node_ids: selectedNodes.value })
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
  const groupIds = targetGroupIds.value
  const nodeIds = selectedNodes.value.filter(isNodeSelectable)
  if (!groupIds.length || !nodeIds.length) return
  submitting.value = true
  try {
    const res = await joinFollow({
      leader_user_id: detail.value.leader.leader_user_id,
      group_ids: groupIds,
      node_ids: nodeIds,
    })
    const nodes = new Set(res.pending.map(row => row.node_id)).size
    if (!res.pending.length) {
      ElMessage.warning(res.skipped[0]?.reason || '所选节点已跟随或等待审核')
      await loadMine()
      selectedNodes.value = []
      return
    }
    ElMessage.success(`已提交 ${nodes} 个节点的 ${res.pending.length} 条跟单申请，等待带单员审核`)
    if (res.skipped.length) ElMessage.warning(`${res.skipped.length} 项已跳过：${res.skipped[0]?.reason}`)
    selectedNodes.value = []
    pickerOpen.value = false
    pickerTrigger?.focus()
    pageMode.value = 'mine'
    mineTab.value = 'list'
    await Promise.all([loadLeaders(), loadMine()])
    if (mobileSheetOpen.value) afterJoinSettled()
  } catch (e) {
    ElMessage.error(errText(e, '加入跟单失败'))
  } finally {
    submitting.value = false
  }
}

/**
 * 「我的跟单」按 leader 聚合、按 node_id 去重，分组不再展示。
 * 后端响应里仍嵌套 `groups`，此处扁平化为 leader → nodes。
 *
 * 关键：同一节点可能在同一 leader 的**多个分组**下被跟随，
 * 所以聚合时必须记全该节点所属的全部分组 —— 移出时要一次性全清，
 * 只清一个分组会残留「半个跟单」状态（仍在其它分组收信号）。
 *
 * 两类分组**不进【带单】列表**：
 *   ① **AI 市场（tpl_1）** —— 两个市场共用同一张关系表，AI 跟单归
 *      【AI 交易】页管，在这里展示会让客户以为在带单市场里跟。
 *      按后端给的 `market === 'ai'` 判定（后端已把模版解析好，前端不认模版白名单）。
 *   ② **已失效 / 已移出** —— 后端 `my_following` 返回的是关系表里的**存量关系**，
 *      分组被禁用、换主节点、带单员下架后关系仍保留（便于清理），但已不再对外接单。
 *
 * 「暂停」不属于失效：暂停保留关系与在跑任务，只是这个带单员不再接新单，
 * 所以**仍然展示**（标「已暂停」），由后端的 `paused` 字段区分。
 */
const myFollowing = computed<MyFollowingEntry[]>(() =>
  mine.value.following.flatMap((f) => {
    const liveGroups = f.groups.filter((g) => g.market !== 'ai' && (g.marketable || g.paused))
    if (!liveGroups.length) return []
    const nodesById = new Map<string, MyFollowedNode>()
    for (const g of liveGroups) {
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
            groups: [{ group_id: g.group_id, group_name: g.group_name ?? null }],
          })
        }
      }
    }
    return {
      leader: f.leader,
      nodes: [...nodesById.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
      groups: liveGroups,
    }
  }),
)

/**
 * AI 智能加仓策略模版 ID（与后端 strategy_templates.TEMPLATE_1_ID 对齐）。
 * AI 市场（tpl_1）的跟单归【AI 交易】页管，带单列表里一律不展示。
 */
const AI_TEMPLATE_ID = 'tpl_1'
function isAiApplication(row: FollowApplication): boolean {
  return (row.template_id || '').toLowerCase() === AI_TEMPLATE_ID
}

const occupiedPairs = computed(() => {
  const pairs = new Map<string, 'following' | 'pending'>()
  for (const row of mine.value.applications || []) {
    if (row.status !== 'pending') continue
    if (isAiApplication(row)) continue
    pairs.set(`${row.group_id}:${row.node_id}`, 'pending')
  }
  // 申请和关系分别读取，已生效的关系覆盖同一组节点的旧待审状态。
  for (const leader of mine.value.following) {
    // AI 市场（tpl_1）的关系归【AI 交易】页管，不参与带单侧的「已跟随」判定
    for (const group of leader.groups) {
      if (group.market === 'ai') continue
      for (const node of group.my_nodes) pairs.set(`${group.group_id}:${node.node_id}`, 'following')
    }
  }
  return pairs
})
const pickerNodes = computed(() => {
  const needle = pickerQuery.value.trim().toLowerCase()
  return (detail.value?.my_nodes || []).filter(node =>
    !needle || [node.name, node.node_id, node.mt5_login].some(value => String(value ?? '').toLowerCase().includes(needle)))
})
const pickerSymbols = computed(() => [...new Set((detail.value?.groups || []).filter(group => group.selectable).map(group => group.symbol).filter(Boolean))])
/**
 * 跟单申请按带单员聚合。
 *
 * 「已移出 / 失效」的申请（status = cancelled，或后端标了 available=false，或
 * 理由写明分组/机器人已失效）与跟单列表口径一致 —— 不再展示，避免客户误以为
 * 还有一个在跑的跟单。
 */
function isDeadApplication(row: FollowApplication): boolean {
  if (row.status === 'pending') return false
  if (row.status === 'cancelled' || row.available === false) return true
  return /已移出|已失效/.test(row.reason || '')
}
/** AI 市场（tpl_1）的跟单申请归【AI 交易】页管，带单列表里不展示。 */
const applicationGroups = computed(() => {
  const grouped = new Map<number, { leader_user_id: number; leader_name: string; nodes: FollowApplication[] }>()
  for (const row of mine.value.applications || []) {
    if (row.status === 'approved') continue
    if (isDeadApplication(row)) continue
    if (isAiApplication(row)) continue
    if (!grouped.has(row.leader_user_id)) grouped.set(row.leader_user_id, { leader_user_id: row.leader_user_id, leader_name: row.leader_name, nodes: [] })
    grouped.get(row.leader_user_id)!.nodes.push(row)
  }
  return [...grouped.values()]
})
const recordLeaderCount = computed(() => new Set([...myFollowing.value.map(row => row.leader.leader_user_id), ...applicationGroups.value.map(row => row.leader_user_id)]).size)
const applicationOnlyGroups = computed(() => applicationGroups.value.filter(group => !myFollowing.value.some(entry => entry.leader.leader_user_id === group.leader_user_id)))

function applicationRows(leaderId: number): FollowApplication[] {
  return applicationGroups.value.find(group => group.leader_user_id === leaderId)?.nodes || []
}

function nodeFollowHint(id: string): string {
  if (isOwnSourceNode(id)) return '主节点已在带单分组内'
  const groups = targetGroupIds.value
  if (!groups.length) return '暂无可申请分组'
  let following = 0
  let pending = 0
  for (const gid of groups) {
    const status = occupiedPairs.value.get(`${gid}:${id}`)
    if (status === 'following') following++
    else if (status === 'pending') pending++
  }
  if (following === groups.length) return '已跟随'
  if (pending === groups.length) return '待审核'
  return [
    following ? `已跟随 ${following} 组` : '',
    pending ? `待审核 ${pending} 组` : '',
  ].filter(Boolean).join(' · ')
}
function searchPicker(): void {
  window.clearTimeout(pickerSearchTimer)
  pickerSearchTimer = window.setTimeout(() => trackFollowUi('search', { value: pickerQuery.value }), 350)
}
function closePicker(): void {
  if (submitting.value) return
  pickerOpen.value = false
  trackFollowUi('cancel_dialog', { value: 'follow-picker' })
  pickerTrigger?.focus()
}
function trapPickerFocus(event: KeyboardEvent): void {
  const elements = pickerPanel.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')
  if (!elements?.length) return
  const first = elements[0]!
  const last = elements[elements.length - 1]!
  if (event.shiftKey && (document.activeElement === first || document.activeElement === pickerPanel.value)) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus()
  }
}
async function retryApplication(row: FollowApplication): Promise<void> {
  const leader = leaders.value.find(item => item.leader_user_id === row.leader_user_id)
  if (!leader) { ElMessage.warning('该带单员目前未开放跟单'); return }
  await openFollowPicker(leader)
  if (isNodeSelectable(row.node_id)) selectedNodes.value = [row.node_id]
}
function groupIsRunning(group: FollowMyGroup): boolean {
  return group.marketable && !group.paused && group.my_nodes.some(n => (n.follow_state || 'running') === 'running' && !n.leader_paused)
}

interface Target {
  leader_id: number
  group_id: string
  node_id: string
  paused: boolean
  locked: boolean
  label: string
}
const relations = computed<Target[]>(() => myFollowing.value.flatMap(entry =>
  entry.groups.flatMap(g => g.my_nodes.map(n => ({
    group_id: g.group_id, node_id: n.node_id, leader_id: entry.leader.leader_user_id,
    paused: n.follow_state === 'paused', locked: !!g.paused || !!n.leader_paused || g.market !== 'follow',
    label: [leaderName(entry.leader), g.group_name || g.group_id, n.name || n.node_id].join(' / '),
  }))),
))
const mutationBusy = ref(false)
const stopTargets = ref<Target[]>([])
const stopWithClose = ref(false)
const selectedStops = ref<Target[]>([])
function canPause(targets: Target[]) { return targets.some(t => !t.locked && !t.paused) }
async function toggleRelations(targets: Target[], batch = false) {
  if (mutationBusy.value) return
  const paused = canPause(targets)
  const changes = targets.filter(t => !t.locked && t.paused !== paused)
  if (!changes.length) return
  if (batch) {
    try {
      await ElMessageBox.confirm(`涉及全部 ${new Set(changes.map(t => t.node_id)).size} 个节点、${changes.length} 条关系（含筛选外）。\n${changes.map(t => t.label).join('\n')}`,
        paused ? '一键暂停' : '一键启动', { confirmButtonText: '确认', cancelButtonText: '取消' })
    } catch { trackFollowUi('cancel_dialog', { value: 'follow-state' }); return }
  }
  mutationBusy.value = true
  try {
    const changed = await setMyFollowState(changes.map(({ group_id, node_id }) => ({ group_id, node_id })), paused)
    ElMessage.success(`${changed} 条跟单关系已${paused ? '暂停' : '恢复'}`)
  } catch (e) { ElMessage.error(errText(e, '跟单状态更新失败')) }
  finally { await loadMine(); mutationBusy.value = false }
}
function cancelStop() {
  if (!mutationBusy.value && stopTargets.value.length) {
    stopTargets.value = []
    trackFollowUi('cancel_dialog', { value: 'stop-follow' })
  }
}
function askStop(targets: Target[]) {
  stopWithClose.value = false
  stopTargets.value = [...targets]
  selectedStops.value = [...targets]
  trackFollowUi('open_picker', { value: 'stop-follow' })
}
async function stopSelected() {
  if (mutationBusy.value || !selectedStops.value.length) return
  mutationBusy.value = true
  const failed: string[] = []
  let removed = 0, closing = 0, queued = 0
  const groups = new Map<string, Target[]>()
  for (const t of selectedStops.value) {
    if (!groups.has(t.group_id)) groups.set(t.group_id, [])
    groups.get(t.group_id)!.push(t)
  }
  try {
    for (const [gid, members] of groups) {
      // 移出接口每次最多接受 50 个节点。
      for (let start = 0; start < members.length; start += 50) {
        const targets = members.slice(start, start + 50)
        try {
          const res = await leaveFollow({ leader_user_id: targets[0].leader_id, group_ids: [gid],
            node_ids: targets.map(t => t.node_id), close_tasks: stopWithClose.value })
          const done = new Set(res.removed.flatMap(r => r.node_ids))
          removed += done.size
          closing += res.closing.length
          queued += res.forced.length
          failed.push(...targets.filter(t => !done.has(t.node_id)).map(t => t.label))
        } catch (e) { failed.push(...targets.map(t => t.label + '：' + errText(e, '操作失败'))) }
      }
    }
    const result = `已停止 ${removed} 条关系，平仓处理中 ${closing} 项，离线排队 ${queued} 项`
    if (failed.length) ElMessage.warning(result + '。未完成：' + failed.join('、'))
    else ElMessage.success(result)
    stopTargets.value = []
  } finally { await Promise.all([loadLeaders(), loadMine()]); mutationBusy.value = false }
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
watch([mobileSheetOpen, pickerOpen], ([sheet, picker]) => {
  document.body.style.overflow = sheet || picker ? 'hidden' : ''
})

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  window.clearInterval(followTimer)
  window.clearTimeout(pickerSearchTimer)
  if (['带单员展示', '我的跟单'].includes(pageChrome.value?.title || '')) pageChrome.value = null
})

// 跟单成功后：手机端收起详情页并切到「我的跟单」，让结果立刻可见。
// 只改移动端状态，不动 detail —— 桌面端保持「留在详情页」的原有行为。
function afterJoinSettled() {
  mobileSheetOpen.value = false
  mobileTab.value = 'mine'
}

onMounted(() => {
  trackFollowUi('page_view', { value: 'follow' })
  void loadLeaders()
  void loadMine()
  void hub.fetchNodes()
  followTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !submitting.value && !mutationBusy.value) void loadMine()
  }, 45_000)
})

const pageMode = ref<'home' | 'mine' | 'showcase'>('home')
const showcaseMode = ref<'trend_market' | 'trend_limit' | 'grid'>('trend_market')
/**
 * 详情页的返回目标。
 *
 * 详情页有两个入口（带单广场 / 我的跟单），返回时必须回**来处**，
 * 否则从「我的跟单」点进来会被扔回广场，白跑一趟。
 */
const showcaseReturn = ref<'home' | 'mine'>('home')
function closeShowcase(): void {
  pageMode.value = showcaseReturn.value
}
let mineScroll = 0
watch(pageMode, async (mode, previous) => {
  if (previous === 'mine') mineScroll = window.scrollY
  if (previous === 'showcase' || mode === 'home') trackFollowUi('page_view', { value: mode })
  pageChrome.value = mode === 'showcase' ? { title: '带单员展示', back: closeShowcase }
    : mode === 'mine' ? { title: '我的跟单', back: () => { pageMode.value = 'home' } } : null
  await nextTick()
  window.scrollTo(0, mode === 'mine' && previous === 'showcase' ? mineScroll : 0)
})
const mineTab = ref<'list' | 'nodes'>('list')
const nodeQuery = ref('')
const nodeFilter = ref<'all' | 'online' | 'pos' | 'win' | 'loss' | 'off'>('all')
watch(nodeFilter, value => trackFollowUi('filter', { value: `nodes:${value}` }))
watch(nodeQuery, value => {
  window.clearTimeout(pickerSearchTimer)
  pickerSearchTimer = window.setTimeout(() => trackFollowUi('search', { value }), 350)
})
const selfDetail = ref<FollowDetailResponse | null>(null)

function leaderName(leader: FollowLeaderSummary): string {
  return leader.display_name || leader.username
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

function entryPositions(entry: MyFollowingEntry) {
  return entry.groups.flatMap(g => groupPositions(g))
}
function groupPositions(g: FollowMyGroup): Array<Position & { nodeId: string; groupId: string }> {
  return g.my_nodes.flatMap(n => {
    const magics = new Set(g.magics?.[n.node_id] || [])
    const positions = hub.accounts[n.node_id]?.positions || n.positions || []
    return positions.filter(p => magics.has(p.magic)).map(p => ({ ...p, nodeId: n.node_id, groupId: g.group_id }))
  })
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
  if (submitting.value || detailLoading.value) return
  pickerTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
  await Promise.all([openLeader(leader), loadMine()])
  if (!detail.value) return
  selectedNodes.value = []
  pickerQuery.value = ''
  pickerOpen.value = true
  trackFollowUi('open_picker', { value: String(leader.leader_user_id) })
  await nextTick()
  pickerPanel.value?.focus()
}
async function pauseMine() {
  if (!myLeader.value) return
  await openLeader(myLeader.value)
  await togglePause()
}
async function openShowcase(leader?: FollowLeaderSummary, from: 'home' | 'mine' = 'home'): Promise<void> {
  // 带单员详情：广场里任何人（包括已暂停 / 已结束的）都能点开看数据。
  // 「查看带单员」按钮走的是自己（myLeader），广场卡片走的是参数里的那位。
  const target = leader || myLeader.value
  if (!target || detailLoading.value) return
  await openLeader(target)
  if (!detail.value) return
  const first = SHOWCASE_MODES.find((key) => (detail.value?.groups || []).some((g) => showcaseGroupMode(g) === key))
  showcaseMode.value = first || 'trend_market'
  showcaseReturn.value = from
  pageMode.value = 'showcase'
  trackFollowUi('page_view', { value: 'leader-showcase' })
}
function applyShowcase(): void {
  if (!detail.value) return
  void openFollowPicker(detail.value.leader)
}
const closeBusy = ref(false)
async function closeRows(rows: Array<Position & { nodeId: string; groupId?: string }>, title: string) {
  if (closeBusy.value) return
  if (!rows.length) { ElMessage.info('没有可平的持仓'); return }
  try {
    await ElMessageBox.confirm(`确认平掉${title}的 ${rows.length} 笔持仓？涉及 ${new Set(rows.map(r => r.nodeId)).size} 个节点，品种 ${[...new Set(rows.map(r => r.symbol))].join("、")}，浮动盈亏 ${fmtU(bookStats(rows).profit)} U。`, '一键平仓', {
      type: 'warning', confirmButtonText: '确认平仓', cancelButtonText: '取消',
    })
  } catch { trackFollowUi('cancel_dialog', { value: 'close-follow' }); return }
  closeBusy.value = true
  try {
    const nodeIds = [...new Set(rows.map((r) => r.nodeId))]
    const failures: string[] = []
    for (const row of rows) {
      try {
        const { data } = await api.post(`/api/nodes/${row.nodeId}/follow-channels/close`, {
          market: 'follow', mode: 'ticket', ticket: row.ticket, group_id: row.groupId,
        })
        if (data.closed !== 1 || data.failed?.length) failures.push(String(row.ticket))
      } catch (e) { failures.push(`${row.nodeId} #${row.ticket}：${errText(e, '未提交')}`) }
    }
    const msg = `已提交 ${rows.length - failures.length} 笔平仓`
    if (failures.length) ElMessage.warning(msg + '，失败 ' + failures.join('、'))
    else ElMessage.success(msg)
    await Promise.all([loadMine(), ...nodeIds.map(id => hub.fetchNodeAccount(id).catch(() => {}))])
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally { closeBusy.value = false }
}
function leaderBlurb(leader: FollowLeaderSummary): string {
  return leader.intro?.trim() || '跟单申请经审核后生效，策略开平仓自动同步。'
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
function entryGroupPositions(entry: MyFollowingEntry, groupId: string) {
  const group = entry.groups.find(g => g.group_id === groupId)
  return group ? groupPositions(group) : []
}
function magicLabel(g: FollowMyGroup) {
  return 'MF ' + [...new Set(Object.values(g.magics || {}).flat())].join(' / ')
}
const books = computed(() => Object.fromEntries(myFollowing.value.flatMap(entry => entry.groups.map(g => {
  const live = groupPositions(g)
  const open = live.map(p => ({
    key: `p:${p.nodeId}:${p.ticket}`, direction: p.type.toUpperCase(), symbol: p.symbol, time: p.time,
    profit: p.profit, status: '正在持仓',
    description: `开仓 ${fmtTime(p.time)} · ${p.volume.toFixed(2)} 手 @ ${fmtPx(p.price_open)} · SL ${fmtPx(p.sl)} / TP ${fmtPx(p.tp)}`,
  }))
  const history = (g.history || []).map(p => ({
    key: `d:${p.node_id}:${p.deal_id}`, direction: p.direction, symbol: p.symbol, time: p.closed_at,
    profit: p.profit, status: p.status,
    description: `平仓 ${fmtTime(p.closed_at)} · ${p.volume.toFixed(2)} 手 @ ${fmtPx(p.price)}`,
  }))
  return [g.group_id, { ...bookStats(live), orders: [...open, ...history].sort((a, b) => b.time - a.time) }]
}))))
function openMine() {
  pageMode.value = 'mine'
  mineTab.value = 'list'
  trackFollowUi('open_mine')
  void loadMine()
}

const previewChips = computed(() => {
  const chips: { key: string; title: string; profit: number; hint: string; known: boolean }[] = []
  for (const entry of myFollowing.value) {
    for (const g of entry.groups) {
      const rows = entryGroupPositions(entry, g.group_id)
      const known = g.my_nodes.some(n => n.balance != null || hub.accounts[n.node_id]?.balance != null || n.positions?.length)
      chips.push({
        key: g.group_id,
        // chip 只有一格宽，塞不下「模式 + 品种」，这里只给开仓方式
        title: groupDisplayTitle(g.group_name, g.strategy_name),
        profit: bookStats(rows).profit,
        hint: `${g.symbol ? g.symbol + ' · ' : ''}汇总 ${entry.nodes.filter((n) => n.groups.some((x) => x.group_id === g.group_id)).length} 个节点`,
        known,
      })
      if (chips.length >= 3) return chips
    }
  }
  return chips
})

const followStats = computed(() => {
  const { net_profit: profit, average_rate: rate } = mine.value.summary
  return { profitKnown: profit != null, profit: profit ?? 0, rateKnown: rate != null, rate: rate ?? 0,
    running: myFollowing.value.reduce((sum, e) => sum + e.groups.filter(groupIsRunning).length, 0) }
})
const latestSync = computed(() => Math.max(0, ...myFollowing.value.flatMap(e => e.groups.map(g => g.last_sync || 0))))
function entryMeta(entry: MyFollowingEntry) {
  const symbols = [...new Set(entry.groups.map(g => g.symbol).filter(Boolean))]
  const since = entry.nodes.flatMap(n => n.joined_at ? [n.joined_at] : [])
  const leverages = [...new Set(entry.groups.flatMap(g => g.my_nodes.map(n => hub.accounts[n.node_id]?.leverage || n.leverage)).filter(Boolean))]
  return [...symbols, since.length && '开始于 ' + fmtTime(Math.min(...since)), leverages.length && leverages.join(' / ') + 'X 杠杆'].filter(Boolean).join(' · ')
}

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
  server: string
  state: '跟单中' | '已暂停' | '待审核' | '已停止'
}
const nodeCards = computed<NodeCard[]>(() => {
  const map = new Map<string, NodeCard & { syms: Set<string>; leadersSet: Set<string> }>()
  for (const entry of myFollowing.value) {
    for (const node of entry.nodes) {
      let card = map.get(node.node_id)
      if (!card) {
        const snapshot = entry.groups.flatMap(g => g.my_nodes).find(n => n.node_id === node.node_id)
        const account = hub.accounts[node.node_id]
        const raw = account?.balance ?? snapshot?.balance
        card = {
          node_id: node.node_id,
          name: node.name || node.node_id,
          login: node.mt5_login,
          online: hub.isNodeOnline(node),
          symbols: '',
          leaders: '',
          count: 0,
          volume: 0,
          profit: 0,
          balance: raw == null ? '—' : raw.toFixed(2),
          server: account?.server || snapshot?.server || hub.nodes.find(n => n.node_id === node.node_id)?.mt5_server || '',
          state: '已暂停',
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
      if (entry.groups.some(g => g.marketable && !g.paused && g.my_nodes.some(n =>
        n.node_id === node.node_id && !n.leader_paused && (n.follow_state ?? 'running') === 'running'))) card.state = '跟单中'
      const rows = entryPositions(entry).filter(p => p.nodeId === node.node_id)
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
  for (const row of mine.value.applications || []) {
    if (!row.available || isAiApplication(row)) continue
    const pending = row.status === 'pending'
    if (!pending && !(row.status === 'cancelled' && row.reason === '跟单已移出')) continue
    const existing = map.get(row.node_id)
    if (existing) {
      if (pending && existing.state === '已停止') existing.state = '待审核'
      continue
    }
    const node = hub.nodes.find(n => n.node_id === row.node_id)
    map.set(row.node_id, { node_id: row.node_id, name: row.name || row.node_id, login: row.mt5_login,
      server: node?.mt5_server || '', online: !!node && hub.isNodeOnline(node), state: pending ? '待审核' : '已停止',
      count: 0, volume: 0, profit: 0, balance: nodeBalance(row.node_id), leaders: '', symbols: '',
      syms: new Set(row.symbol ? [row.symbol] : []), leadersSet: new Set([row.leader_name]) })
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
    return `${n.name} ${n.login ?? ''} ${n.symbols} ${n.leaders} ${n.server} ${n.online ? '在线' : '离线'} ${n.state} ${n.state === '跟单中' ? '运行中' : ''}`.toLowerCase().includes(q)
  })
})
const nodeSummary = computed(() => {
  const cards = nodeCards.value.filter(n => n.state === '跟单中' || n.state === '已暂停')
  const wins = cards.filter((n) => n.profit > 0).length
  const volume = cards.reduce((s, n) => s + n.volume, 0)
  const profit = cards.reduce((s, n) => s + n.profit, 0)
  const count = cards.reduce((s, n) => s + n.count, 0)
  const balance = mine.value.summary.first_balance
  return { wins, volume, profit, count, balance, active: cards.length }
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
const groupTargets = (gid: string) => relations.value.filter(t => t.group_id === gid)
const nodeTargets = (nid: string) => relations.value.filter(t => t.node_id === nid)
const stopPositions = computed(() => myFollowing.value.flatMap(entryPositions).filter(p =>
  selectedStops.value.some(t => t.node_id === p.nodeId && t.group_id === p.groupId)).length)

const selfGroups = computed(() => selfDetail.value?.groups || [])
const selfNodeId = computed(() => auth.canBeFollowLeader ? myLeader.value?.primary_node?.node_id : undefined)
const selfPositions = computed(() => selfNodeId.value ? hub.accounts[selfNodeId.value]?.positions || [] : [])
const selfMagicKey = computed(() => [...new Set(selfPositions.value.map(p => p.magic))].sort().join(','))
const selfChannels = ref<FollowChannelsOut['magics']>([])
watch([selfNodeId, selfMagicKey], async ([nodeId, magics], [previousId], onCleanup) => {
  if (nodeId !== previousId || !magics) selfChannels.value = []
  if (!nodeId || !magics) return
  let current = true
  onCleanup(() => { current = false })
  try {
    const { data } = await api.get<FollowChannelsOut>(`/api/nodes/${nodeId}/follow-channels`)
    if (current) selfChannels.value = data.magics.filter(channel => channel.market === 'follow')
  } catch { /* 保留最近一次成功读取的频道归属 */ }
})
const selfOrders = computed(() => {
  const nodeId = selfNodeId.value
  if (!nodeId) return []
  const groups = new Map(selfGroups.value.map(group => [group.group_id, group]))
  const byMagic = new Map(selfChannels.value.map(channel => [channel.magic, groups.get(channel.group_id)]))
  return selfPositions.value.flatMap(position => {
    const group = byMagic.get(position.magic)
    return group ? [{ ...position, nodeId, title: selfItemTitle(group) }] : []
  }).sort((a, b) => b.time - a.time || b.ticket - a.ticket)
})
const selfMagicBooks = computed(() => {
  const ordersByMagic = new Map<number, typeof selfOrders.value>()
  for (const order of selfOrders.value) {
    const orders = ordersByMagic.get(order.magic)
    if (orders) orders.push(order)
    else ordersByMagic.set(order.magic, [order])
  }
  return Array.from(ordersByMagic, ([magic, orders]) => ({
    magic, orders, title: orders[0].title, nodeId: orders[0].nodeId, ...bookStats(orders),
  }))
})
const selfMeta = computed(() => {
  const leader = myLeader.value
  const parts: string[] = []
  const symbols = [...new Set(selfGroups.value.map((g) => g.symbol).filter((s): s is string => !!s))]
  if (symbols.length) parts.push(symbols.join(' · '))
  const nodeId = leader?.primary_node?.node_id
  const created = nodeId ? hub.nodes.find((n) => n.node_id === nodeId)?.created_at : 0
  if (created) parts.push(`开始于 ${fmtTime(created)}`)
  const leverage = nodeId ? hub.accounts[nodeId]?.leverage : 0
  if (leverage) parts.push(`${leverage}X 杠杆`)
  if (parts.length) return parts.join(' · ')
  const node = leader?.primary_node
  return node ? (node.name || node.node_id) : '无主节点'
})
const SELF_MODE_LABEL: Record<string, string> = {
  trend_market: '市价开仓',
  trend_limit: '限价开仓',
  grid: '智能吸筹',
}
const SHOWCASE_MODES = ['trend_market', 'trend_limit', 'grid'] as const

/**
 * 从分组名 / 策略名解析「开仓方式」key。
 *
 * 分组名是机器生成的：`{strategy_type}-{用户名}-{MT5号}-{品种}`
 * （如 `grid-alice123-9527-XAUUSD`），策略名还会再拼一段 `-grid-策略`。
 * 前缀就是策略类型，据此才能翻成人话。
 */
function groupModeKey(name: string | null | undefined, strategy?: string | null): string {
  for (const raw of [name, strategy]) {
    const key = (raw || '').split('-')[0]
    if (key in SELF_MODE_LABEL) return key
  }
  return ''
}

/**
 * 分组的展示标题。
 *
 * 直接显示机器名（`grid-alice123-9527-XAUUSD-grid-策略`）对用户毫无意义，
 * 所以优先翻成「市价开仓 / 限价开仓 / 智能吸筹」；
 * 认不出来（非标准命名）时才回落原名 —— 不能显示成空白。
 *
 * `symbol` 传了就拼在后面：同一个开仓方式可能有多个品种分组，
 * 光说「智能吸筹」用户分不清是哪一个。
 */
function groupDisplayTitle(name: string | null, strategy: string | null, symbol?: string | null): string {
  const key = groupModeKey(name, strategy)
  const label = (key && SELF_MODE_LABEL[key]) || groupTitle(name, strategy)
  return symbol ? `${label} · ${symbol}` : label
}

function applicationTitle(row: FollowApplication): string {
  const name = row.group_name || ''
  const symbol = row.symbol || (groupModeKey(name) && name.match(/-\d+-([^-]+)$/)?.[1])
  return groupDisplayTitle(name || '跟单申请', row.strategy_name, symbol)
}

function showcaseGroupMode(g: { name: string | null; strategy_name?: string | null }): string {
  return groupModeKey(g.name, g.strategy_name)
}
const showcaseCounts = computed(() => {
  const counts: Record<string, number> = { trend_market: 0, trend_limit: 0, grid: 0 }
  for (const g of detail.value?.groups || []) {
    const key = showcaseGroupMode(g)
    if (key in counts) counts[key] += 1
  }
  return counts
})
const showcaseCards = computed(() =>
  (detail.value?.groups || []).filter((g) => showcaseGroupMode(g) === showcaseMode.value),
)
const showcaseDays = computed(() => {
  // 主节点的建节点时间：别人的主节点不在我的节点列表里，只能读后端带出来的 created_at。
  const created = detail.value?.leader.primary_node?.created_at
    || (() => {
      const id = detail.value?.leader.primary_node?.node_id
      return id ? hub.nodes.find((n) => n.node_id === id)?.created_at : 0
    })()
  if (!created) return null
  return Math.max(0, Math.floor((Date.now() / 1000 - created) / 86400))
})
const showcaseIntro = computed(() => {
  const leader = detail.value?.leader
  // 他自己写的简介优先；没写就用统一说明。
  // 注意：**不能**只认自己（`isMyLeader`）—— 详情页现在对所有人开放，
  // 别人的简介也要显示出来，否则点进来看到的永远是同一句模板文案。
  return leader?.intro?.trim() || '跟单申请经审核后生效，策略开平仓自动同步。'
})
const showcaseNodeCount = computed(() => detail.value?.leader.follower_node_count || 0)
function pickShowcaseMode(key: 'trend_market' | 'trend_limit' | 'grid'): void {
  showcaseMode.value = key
  trackFollowUi('filter', { value: `showcase:${key}` })
}
/**
 * 带单员绩效（主节点口径）。
 *
 * 绩效**拆不到单个分组**（后端只聚合主节点），所以它属于「带单员」这一层，
 * 不属于「某个分组」这一层 —— 早期版本把它塞进分组卡，还用「只有一个分组
 * 才显示」来掩盖口径错位，结果多方单员全是「暂无数据」。现在单独成块展示。
 */
const showcasePerf = computed(() => {
  const perf = detail.value?.leader.performance
  return perf?.data_available ? perf : null
})
/** 近 7 日已实现收益（柱状图）。后端按 UTC+8 自然日分好，前端直接显示。 */
const showcaseDaily = computed(() => detail.value?.daily_profit || [])
const showcaseDailyMax = computed(() =>
  Math.max(1, ...showcaseDaily.value.map((point) => Math.abs(point.profit))),
)
const showcaseDailyHasDeals = computed(() => showcaseDaily.value.some((point) => point.deals > 0))
/** 该带单员当前还能不能跟单（已暂停 / 已结束都不行）。 */
const showcaseCanFollow = computed<boolean>(() => {
  const leader = detail.value?.leader
  if (!leader) return false
  if (!leader.marketable) return false
  if (leader.follow_paused) return false
  return selectableGroups.value.length > 0
})
/** 不可跟单时给一句人话原因（别让人猜为什么按钮是灰的）。 */
const showcaseFollowHint = computed(() => {
  const leader = detail.value?.leader
  if (!leader) return ''
  if (!leader.marketable) return '该带单员已结束带单，不再接收新的跟单'
  if (leader.follow_paused) return PAUSED_FOLLOW_HINT
  if (!selectableGroups.value.length) return '该带单员当前没有可跟单的分组'
  return ''
})
function selfModeLabel(name: string | null | undefined): string {
  return SELF_MODE_LABEL[(name || '').split('-')[0]] || ''
}
function selfItemTitle(g: { name: string | null; strategy_name?: string | null; strategy_template?: string | null }): string {
  return selfModeLabel(g.name) || selfModeLabel(g.strategy_name) || g.strategy_template || g.strategy_name || g.name || '跟单分组'
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
  if (id && !hub.accounts[id]) await hub.fetchNodeAccount(id)
}
watch([myLeader, () => auth.canBeFollowLeader], () => { void loadSelfDetail() })
</script>

<template>
  <div class="fd" :class="{ 'fd-mine': pageMode === 'mine' }">
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
          <button type="button" class="fd-ghost" @click="refreshFollow"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 0 1-8.66 3.46l-1.42 1.42A7 7 0 0 0 19 12a6.96 6.96 0 0 0-1.35-5.65zM6 12a6 6 0 0 1 6-6v1.5A4.5 4.5 0 1 0 16.5 12H18a6 6 0 0 1-12 0z"/></svg>刷新</button>
        </header>
        <p v-if="loading" class="fd-empty">加载中…</p>
        <p v-else-if="!leaders.length" class="fd-empty">暂无开放的带单员</p>
        <div v-else class="fd-grid">
          <article
            v-for="l in leaders"
            :key="l.leader_user_id"
            class="fd-leader fd-leader-link"
            role="button"
            tabindex="0"
            :aria-label="`查看 ${leaderName(l)} 的带单详情`"
            @click="openShowcase(l)"
            @keydown.enter.prevent="openShowcase(l)"
            @keydown.space.prevent="openShowcase(l)"
          >
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
            <button type="button" class="fd-follow" :disabled="!l.marketable || l.follow_paused" @click.stop="openFollowPicker(l)">跟单<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
          </article>
        </div>
      </section>

      <section class="fd-sec">
        <header>
          <p class="fd-kicker green">我的跟单<span>{{ recordLeaderCount }}</span></p>
          <button type="button" class="fd-ghost" @click="openMine">查看全部<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
        </header>
        <button type="button" class="fd-preview" @click="openMine">
          <span class="fd-preview-top">
            <span class="fd-swap"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg></span>
            <span>
              <b>我跟随的带单员</b>
              <small>{{ recordLeaderCount }} 位带单员 · {{ mine.summary.pending_node_count || 0 }} 个节点待审核 · 点击查看运行状态</small>
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
          <span v-else class="fd-preview-empty">{{ applicationGroups.length ? '跟单申请与审核状态 · 点击查看' : '还没有跟随的带单员' }}</span>
        </button>
      </section>

      <section v-if="auth.canBeFollowLeader" class="fd-sec">
        <header>
          <p class="fd-kicker blue">我的带单<span>{{ selfOrders.length }}</span></p>
          <em class="fd-sec-note">仅带单员可见</em>
        </header>
        <p v-if="!myLeader" class="fd-empty">你有带单能力，但还没有可被跟随的主节点分组。</p>
        <div v-else class="fd-self-grid">
        <article class="fd-card fd-self">
          <header class="fd-self-top">
            <span class="fd-avatar lg"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5z"/></svg></span>
            <div class="fd-self-id">
              <p>
                <b>{{ leaderName(myLeader) }}</b>
                <svg class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                <em class="ok">已认证</em>
              </p>
              <small>{{ selfMeta }}</small>
            </div>
            <div class="fd-self-side">
              <em class="fd-self-run"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 0 0 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>{{ myLeader.follow_paused ? '带单已暂停' : selfOrders.length + ' 笔持仓' }}</em>
              <button type="button" class="fd-self-hold" :class="{ on: myLeader.follow_paused }" :disabled="pauseBusy" @click="pauseMine">
                <svg v-if="myLeader.follow_paused" viewBox="0 0 24 24"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>
                <svg v-else viewBox="0 0 24 24"><path fill="currentColor" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                {{ myLeader.follow_paused ? '恢复带单' : '暂停带单' }}
              </button>
              <button type="button" class="fd-self-link" @click="openShowcase()">查看带单员<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
            </div>
          </header>
          <p v-if="!selfOrders.length" class="fd-empty">暂无带单持仓。</p>
          <div v-else class="fd-self-list">
            <div v-for="book in selfMagicBooks" :key="`${book.nodeId}:${book.magic}`" class="fd-self-item">
              <div class="fd-self-row">
                <p>
                  <svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>
                  <b>{{ book.title }}</b>
                </p>
                <div class="fd-self-actions">
                  <em class="live"><i></i>持仓中</em>
                </div>
              </div>
              <div class="fd-book">
                <div class="fd-book-head">
                  <span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 10H7v-2h10v2zm0-4H7V7h10v2z"/></svg>订单列表</span>
                  <span class="fd-book-meta"><b><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zM3 9h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>{{ book.count }} 笔持仓</b><i>浮动 {{ fmtU(book.profit) }} U</i></span>
                </div>
                <div class="fd-self-orders">
                  <div v-for="p in book.orders" :key="p.ticket" class="fd-self-pos">
                    <div>
                      <em :class="(p.type || '').toLowerCase() === 'buy' ? 'buy' : 'sell'">{{ (p.type || '').toLowerCase() === 'buy' ? 'BUY' : 'SELL' }}</em>
                      <span>{{ p.symbol }}</span>
                      <span>#{{ p.ticket }}</span>
                      <strong :class="(p.profit || 0) >= 0 ? 'up' : 'down'">{{ fmtU(p.profit || 0) }} U</strong>
                      <button type="button" class="fd-close" :disabled="closeBusy" @click="closeRows([p], `订单 #${p.ticket}`)"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>平此单</button>
                    </div>
                    <p>开仓 {{ fmtTime(p.time) }} · {{ (p.volume || 0).toFixed(2) }} 手 @ {{ fmtPx(p.price_open) }} · SL {{ fmtPx(p.sl) }} / TP {{ fmtPx(p.tp) }}</p>
                  </div>
                </div>
              </div>
              <div class="fd-stats">
                <div><span>持仓笔数</span><b>{{ book.count }} 笔</b></div>
                <div><span>持仓手数</span><b>{{ book.volume.toFixed(2) }} 手</b></div>
                <div><span>浮动盈亏</span><b :class="book.profit >= 0 ? 'up' : 'down'">{{ fmtU(book.profit) }} U</b></div>
              </div>
              <footer>
                <button type="button" class="fd-close" :disabled="closeBusy" @click="closeRows(book.orders, `魔术号 ${book.magic}`)">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>一键平仓
                </button>
              </footer>
            </div>
          </div>
        </article>
        </div>
      </section>
    </template>

    <template v-else-if="pageMode === 'mine'">
      <section class="fd-mine-hero">
        <div class="fd-mine-title">
          <span class="fd-mark"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg></span>
          <div><p>我的跟单</p><small>我跟随的带单员 · 策略同步与收益一览</small></div>
        </div>
        <div class="fd-stat3">
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 0 0 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>跟单中</span><b class="up">{{ followStats.running }} 个</b></div>
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>累计收益</span><b :class="followStats.profitKnown ? (followStats.profit >= 0 ? 'up' : 'down') : ''">{{ followStats.profitKnown ? fmtU(followStats.profit) : '暂无数据' }}</b></div>
          <div><span><svg viewBox="0 0 24 24"><path fill="currentColor" d="m5 19 14-14-1.5-1.5-14 14zM7 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4zm10 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4z"/></svg>平均收益率</span><b :class="followStats.rateKnown ? (followStats.rate >= 0 ? 'up' : 'down') : ''">{{ followStats.rateKnown ? (followStats.rate >= 0 ? '+' : '') + followStats.rate.toFixed(2) + '%' : '暂无数据' }}</b></div>
        </div>
        <p class="fd-note"><svg viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M12 2a10 10 0 1 1 0 20 10 10 0 0 1 0-20zm-1 5v2h2V7zm0 4v6h2v-6z"/></svg>每位带单员可提供市价开仓、限价开仓和智能吸筹。跟单申请通过后，按分组同步后续信号。</p>
      </section>

      <div class="fd-tabs">
        <button type="button" :class="{ on: mineTab === 'list' }" @click="mineTab = 'list'; trackFollowUi('filter', { value: 'list' })"><svg viewBox="0 0 24 24"><path fill="currentColor" d="m6.99 11 2.51-2.5L8.09 7 3 12.09 8.09 17.2l1.41-1.41L6.99 13.2H14v-2.2zM21 6.91 15.91 1.8 14.5 3.21l2.51 2.5H10v2.2h7.01l-2.51 2.5 1.41 1.41z"/></svg>跟单列表<span>{{ recordLeaderCount }}</span></button>
        <button type="button" :class="{ on: mineTab === 'nodes' }" @click="mineTab = 'nodes'; trackFollowUi('filter', { value: 'nodes' })"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg>子节点跟单统计<span>{{ nodeCards.length }}</span></button>
      </div>

      <section v-if="mineTab === 'list'" class="fd-sec">
        <header>
          <p class="fd-kicker blue">跟单列表<span>{{ recordLeaderCount }} 位带单员</span></p>
          <small v-if="latestSync" class="fd-sync">最近同步 {{ fmtTime(latestSync) }}</small>
        </header>
        <div v-if="applicationOnlyGroups.length" class="fd-grid">
          <article v-for="group in applicationOnlyGroups" :key="'application-' + group.leader_user_id" class="fd-card fd-applications">
            <header class="fd-card-top"><span class="fd-avatar lg">{{ group.leader_name.slice(0, 1) }}</span><div><b>{{ group.leader_name }}</b><small>跟单申请 · {{ group.nodes.filter(node => node.status === 'pending').length }} 条待通过</small></div></header>
            <div v-for="node in group.nodes" :key="node.request_id" class="fd-item">
              <div class="fd-item-top"><p><b>{{ applicationTitle(node) }}</b></p><em :class="node.status === 'pending' ? 'fd-pending' : 'off'">{{ node.status === 'pending' ? '待通过' : node.status === 'rejected' ? '已拒绝' : '已移出 / 失效' }}</em></div>
              <p class="fd-application-node">{{ node.name || node.node_id }} <span v-if="node.is_primary">主节点</span> · {{ node.mt5_login || node.node_id }}</p>
              <p class="fd-application-note">{{ node.status === 'pending' ? '⌛ 等待带单员通过申请' : node.reason || '可重新选择节点申请跟单' }}</p>
              <button v-if="node.status !== 'pending' && node.available" type="button" class="fd-link" @click="retryApplication(node)">重新申请 ›</button>
            </div>
          </article>
        </div>
        <p v-if="mineLoading && !myFollowing.length" class="fd-empty">加载中…</p>
        <div v-else-if="!myFollowing.length && !applicationGroups.length" class="fd-mine-empty">
          <b>还没有跟单</b><p>前往带单广场选择带单员，申请加入跟单。</p><button class="fd-link" @click="pageMode = 'home'">去带单广场 ›</button>
        </div>
        <div v-else class="fd-grid">
          <article v-for="entry in myFollowing" :key="entry.leader.leader_user_id" class="fd-card">
            <header class="fd-self-top">
              <span class="fd-avatar lg"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5z"/></svg></span>
              <div class="fd-self-id">
                <p>
                  <b>{{ leaderName(entry.leader) }}</b>
                  <svg class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                  <em v-if="entry.leader.follow_paused" class="pause">已暂停</em>
                  <em v-else class="ok">已认证</em>
                </p>
                <small>{{ entryMeta(entry) }}</small>
              </div>
              <div class="fd-self-side">
                <em class="fd-self-run"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 4V1L8 5l4 4V6a6 6 0 0 1 5.5 8.4l1.5 1.5A8 8 0 0 0 12 4zm0 16v3l4-4-4-4v3a6 6 0 0 1-5.5-8.4L5 8.1A8 8 0 0 0 12 20z"/></svg>{{ entry.groups.filter(groupIsRunning).length }}/{{ entry.groups.length }} 个跟单中</em>
                <button type="button" class="fd-self-link" @click="openShowcase(entry.leader, 'mine')">查看带单员 ›</button>
              </div>
            </header>
            <p v-if="entry.leader.follow_paused" class="fd-warn">{{ PAUSED_FOLLOW_HINT }}</p>
            <div v-for="node in applicationRows(entry.leader.leader_user_id)" :key="'request-' + node.request_id" class="fd-item">
              <div class="fd-item-top"><p><b>{{ applicationTitle(node) }}</b></p><em :class="node.status === 'pending' ? 'fd-pending' : 'off'">{{ node.status === 'pending' ? '待通过' : node.status === 'rejected' ? '已拒绝' : '已移出 / 失效' }}</em></div>
              <p class="fd-application-node">{{ node.name || node.node_id }} <span v-if="node.is_primary">主节点</span> · {{ node.mt5_login || node.node_id }}</p>
              <p class="fd-application-note">{{ node.status === 'pending' ? '⌛ 等待带单员通过申请' : node.reason || '可重新选择节点申请跟单' }}</p>
              <button v-if="node.status !== 'pending' && node.available" type="button" class="fd-link" @click="retryApplication(node)">重新申请 ›</button>
            </div>
            <div v-for="g in entry.groups" :key="g.group_id" class="fd-item">
              <div class="fd-self-row">
                <p>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="(g.mode || groupModeKey(g.group_name, g.strategy_name)) === 'trend_limit' ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 2a8 8 0 1 1 0 16 8 8 0 1 1 0-16zm-1 2v7l5 3 1-2-4-2V6z' : 'M7 2v11h3v9l7-12h-4l4-8z'"/></svg>
                  <b>{{ SELF_MODE_LABEL[g.mode || ''] || groupDisplayTitle(g.group_name, g.strategy_name) }}</b>
                  <em v-if="Object.values(g.magics || {}).flat().length" class="fd-mf" :title="magicLabel(g)">{{ magicLabel(g) }}</em>
                  <em v-if="g.group_name" class="fd-node" :title="g.group_name"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 3h18v8H3zm0 10h18v8H3zm3-8v3h3V5zm0 10v3h3v-3z"/></svg>{{ g.group_name }}</em>
                  <span v-if="g.last_sync">同步 {{ fmtTime(g.last_sync) }}</span>
                </p>
                <div class="fd-self-actions">
                  <em :class="groupIsRunning(g) ? 'live' : 'pause'"><i></i>{{ groupIsRunning(g) ? '跟单中' : '已暂停' }}</em>
                  <button class="fd-self-hold" :class="{ on: !canPause(groupTargets(g.group_id)) }"
                    :disabled="mutationBusy || groupTargets(g.group_id).every(t => t.locked)" @click="toggleRelations(groupTargets(g.group_id))">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="canPause(groupTargets(g.group_id)) ? 'M6 19h4V5H6v14zm8-14v14h4V5h-4z' : 'M8 5v14l11-7z'"/></svg>
                    {{ canPause(groupTargets(g.group_id)) ? '暂停跟单' : '恢复跟单' }}
                  </button>
                </div>
              </div>
              <div class="fd-book">
                <div class="fd-book-head"><span><b aria-hidden="true">▤</b> 订单列表</span>
                  <span class="fd-book-meta"><b>♢ {{ g.symbol || '—' }}</b><i>{{ books[g.group_id].count }} 笔持仓 · 浮动 {{ fmtU(books[g.group_id].profit) }} U</i></span>
                </div>
                <p v-if="!books[g.group_id].orders.length" class="fd-empty">暂无同步订单</p>
                <div v-else class="fd-self-orders" tabindex="0" :aria-label="(g.group_name || '跟单') + '订单列表'">
                  <div v-for="p in books[g.group_id].orders" :key="p.key" class="fd-self-pos">
                    <div><em :class="p.direction.toLowerCase()">{{ p.direction }}</em><span>{{ p.symbol }}</span>
                      <span class="hold" :class="{ tp: p.status === '已止盈', sl: p.status === '已止损', closed: p.status === '已平仓' }"><i></i>{{ p.status }}</span>
                      <strong :class="p.profit >= 0 ? 'up' : 'down'">{{ fmtU(p.profit) }} U</strong>
                    </div>
                    <p>{{ p.description }}</p>
                  </div>
                </div>
              </div>
              <div class="fd-stats">
                <div><span>持仓笔数</span><b>{{ books[g.group_id].count }} 笔</b></div>
                <div><span>合计手数</span><b>{{ books[g.group_id].volume.toFixed(2) }} 手</b></div>
                <div><span>浮动盈亏</span><b :class="books[g.group_id].profit >= 0 ? 'up' : 'down'">{{ fmtU(books[g.group_id].profit) }} U</b></div>
              </div>
              <footer>
                <button type="button" class="fd-close" :disabled="closeBusy || !books[g.group_id].count" @click="closeGroup(entry, g.group_id, groupDisplayTitle(g.group_name, g.strategy_name, g.symbol))"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>一键平仓</button>
              </footer>
            </div>
          </article>
        </div>
      </section>

      <section v-else class="fd-sec fd-nodes">
        <header>
          <p class="fd-kicker blue">子节点跟单统计<span>{{ shownNodes.length === nodeCards.length ? nodeCards.length : shownNodes.length + '/' + nodeCards.length }} 个节点</span></p>
          <div class="fd-batch">
            <button class="fd-self-hold" :class="{ resume: !canPause(relations) }" :disabled="mutationBusy || relations.every(t => t.locked)" @click="toggleRelations(relations, true)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" :d="canPause(relations) ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM11 16H9V8h2zM15 16h-2V8h2z' : 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM9.5 16.5v-9l7 4.5z'"/></svg>{{ canPause(relations) ? '一键暂停' : '一键启动' }}</button>
            <button type="button" class="fd-close" :disabled="closeBusy || !nodeSummary.count" @click="closeAllMine"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M22 3.41l-5.29 5.29L20 12h-8V4l3.29 3.29L20.59 2 22 3.41zM3.41 22l5.29-5.29L12 20v-8H4l3.29 3.29L2 20.59 3.41 22z"/></svg>一键平仓</button>
            <button class="fd-leave" :disabled="mutationBusy || !relations.length" @click="askStop(relations)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM16 16H8V8h8z"/></svg>一键停止</button>
          </div>
        </header>

        <p class="fd-sub">每个子节点独立统计，通过审核后按分组同步带单信号。</p>
        <div class="fd-filters">
          <button type="button" :class="{ on: nodeFilter === 'all' }" @click="nodeFilter = 'all'">全部 <span>{{ nodeFilterCounts.all }}</span></button>
          <button type="button" :class="{ on: nodeFilter === 'online' }" @click="nodeFilter = 'online'">在线 <span>{{ nodeFilterCounts.online }}</span></button>
          <button type="button" :class="{ on: nodeFilter === 'pos' }" @click="nodeFilter = 'pos'">持仓 <span>{{ nodeFilterCounts.pos }}</span></button>
          <button type="button" :class="{ on: nodeFilter === 'win' }" @click="nodeFilter = 'win'">浮盈 <span>{{ nodeFilterCounts.win }}</span></button>
          <button type="button" :class="{ on: nodeFilter === 'loss' }" @click="nodeFilter = 'loss'">浮亏 <span>{{ nodeFilterCounts.loss }}</span></button>
          <button type="button" :class="{ on: nodeFilter === 'off' }" @click="nodeFilter = 'off'">离线 <span>{{ nodeFilterCounts.off }}</span></button>
        </div>
        <label class="fd-search"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg><input v-model="nodeQuery" placeholder="搜索子节点：名称 / 账户 / 服务器 / 品种 / 状态" /><button v-if="nodeQuery" aria-label="清空搜索" @click="nodeQuery = ''">×</button></label>
        <div class="fd-sum4">
          <div><span>跟单节点</span><b>{{ nodeCards.length }} 个</b></div>
          <div><span>持仓手数</span><b>{{ nodeSummary.volume.toFixed(2) }} 手</b></div>
          <div><span>首单账户余额</span><b>{{ nodeSummary.balance == null ? '暂无数据' : nodeSummary.balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</b></div>
          <div><span>总浮动盈亏</span><b :class="nodeSummary.profit >= 0 ? 'up' : 'down'">{{ fmtU(nodeSummary.profit) }}</b></div>
        </div>
        <p class="fd-sub">盈利节点 {{ nodeSummary.wins }} / {{ nodeSummary.active }} · 共持仓 {{ nodeSummary.count }} 笔 · 汇总数据仅统计运行中与已暂停节点</p>
        <p v-if="!shownNodes.length" class="fd-empty">没有符合条件的节点</p>
        <div class="fd-grid"><article v-for="card in shownNodes" :key="card.node_id" class="fd-node-card">
          <header>
            <span class="fd-avatar"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg></span>
            <div>
              <b>{{ card.name }}</b>
              <small>{{ [card.login, card.server, card.symbols].filter(Boolean).join(' · ') }}</small>
            </div>
            <em :class="card.state === '跟单中' ? 'on' : 'off'"><i></i>{{ card.state }}</em>
          </header>
          <div class="fd-pnl" :class="card.profit >= 0 ? 'up' : 'down'"><span><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="card.state === '已停止' ? 'M19 13H5v-2h14z' : 'M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z'"/></svg>浮动盈亏</span><b>{{ fmtU(card.profit) }}</b></div>
          <div class="fd-stats">
            <div><span>持仓笔数</span><b>{{ card.count }} 笔</b></div>
            <div><span>手数</span><b>{{ card.volume.toFixed(2) }}</b></div>
            <div><span>账户余额</span><b>{{ card.balance }}</b></div>
          </div>
          <footer>
            <span class="fd-symbol">{{ card.symbols }}</span>
            <small v-if="card.state === '待审核' || card.state === '已停止'">{{ card.state === '待审核' ? '等待带单员通过申请' : '已停止跟单' }}</small>
            <template v-else>
            <button type="button" class="fd-self-hold" :aria-label="canPause(nodeTargets(card.node_id)) ? '暂停跟单' : '恢复跟单'" :title="canPause(nodeTargets(card.node_id)) ? '暂停跟单' : '恢复跟单'" :disabled="mutationBusy || nodeTargets(card.node_id).every(t => t.locked)" @click="toggleRelations(nodeTargets(card.node_id))"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="canPause(nodeTargets(card.node_id)) ? 'M6 19h4V5H6zm8-14v14h4V5z' : 'M8 5v14l11-7z'"/></svg></button>
            <button type="button" class="fd-close" :disabled="closeBusy || !card.count" @click="closeNodeCard(card)" :aria-label="card.count ? '一键平仓' : '已平仓'" :title="card.count ? '一键平仓' : '已平仓'"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M22 3.41l-5.29 5.29L20 12h-8V4l3.29 3.29L20.59 2 22 3.41zM3.41 22l5.29-5.29L12 20v-8H4l3.29 3.29L2 20.59 3.41 22z"/></svg></button>
            <button type="button" class="fd-leave" :disabled="mutationBusy" @click="askStop(nodeTargets(card.node_id))" aria-label="停止跟单" title="停止跟单"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM16 16H8V8h8z"/></svg></button>
            </template>
          </footer>
        </article></div>
      </section>
    </template>

    <template v-else-if="pageMode === 'showcase' && detail">
      <section class="fd-card fd-show">
        <div class="fd-show-banner"></div>
        <div class="fd-show-body">
          <div class="fd-show-id">
            <div class="fd-show-who">
              <span class="fd-show-avatar">{{ leaderName(detail.leader).slice(0, 1) }}</span>
              <div>
                <p>
                  <b>{{ leaderName(detail.leader) }}</b>
                  <svg class="fd-verified" viewBox="0 0 24 24"><path fill="currentColor" d="m12 2 2.4 2.2 3.2-.6.8 3.1 2.9 1.5-1.1 3 1.1 3-2.9 1.5-.8 3.1-3.2-.6L12 22l-2.4-2.2-3.2.6-.8-3.1L2.7 15.8l1.1-3-1.1-3 2.9-1.5.8-3.1 3.2.6z"/></svg>
                </p>
                <small>{{ showcaseDays == null ? '已运行 —' : `已运行 ${showcaseDays} 天` }}</small>
              </div>
            </div>
            <em :class="detail.leader.marketable ? (detail.leader.follow_paused ? 'pause' : 'fd-show-cert') : 'pause'">{{ detail.leader.marketable ? (detail.leader.follow_paused ? '已暂停' : '已认证') : '已结束' }}</em>
          </div>
          <div class="fd-show-nodes">
            <span class="fd-show-mark"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg></span>
            <div>
              <p><svg viewBox="0 0 24 24"><path fill="currentColor" d="M5 18h14v2H5zm9.84-3.47 4.24-4.24-1.41-1.41-2.83 2.83-2.12-2.12-1.41 1.41zM5 4h14v2H5z"/></svg>已加载跟单节点</p>
              <strong>{{ showcaseNodeCount.toLocaleString() }}<span>个节点同步跟单</span></strong>
            </div>
            <em :class="showcaseNodeCount ? 'on' : 'off'"><i></i>{{ showcaseNodeCount ? '已加载' : '未加载' }}</em>
          </div>
          <p class="fd-show-bio">{{ showcaseIntro }}</p>
          <div class="fd-show-actions">
            <button type="button" class="fd-show-apply" :disabled="detailLoading || !showcaseCanFollow" @click="applyShowcase"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M15 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zM6 10V7H4v3H1v2h3v3h2v-3h3v-2H6zm9 4c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>跟单申请</button>
            <button type="button" class="fd-show-back" @click="closeShowcase">{{ showcaseReturn === 'mine' ? '返回我的跟单' : '返回带单广场' }}</button>
          </div>
          <p v-if="showcaseFollowHint" class="fd-show-warn">{{ showcaseFollowHint }}</p>
        </div>
      </section>

      <!-- 带单员整体绩效：主节点口径，拆不到单个分组，所以单独成块 -->
      <section class="fd-sec">
        <header>
          <p class="fd-kicker">绩效概览<span>{{ detail.leader.perf_window }}</span></p>
          <span class="fd-show-hint">统计口径：带单员主节点</span>
        </header>
        <div class="fd-show-stats fd-show-perf">
          <div><span>累计收益</span><b :class="showcasePerf ? (showcasePerf.total_profit >= 0 ? 'up' : 'down') : 'muted'">{{ showcasePerf ? fmtU(showcasePerf.total_profit) + ' U' : '暂无数据' }}</b></div>
          <div><span>收益率</span><b :class="showcasePerf?.profit_rate_valid ? (showcasePerf.profit_rate >= 0 ? 'up' : 'down') : 'muted'">{{ showcasePerf?.profit_rate_valid ? `${showcasePerf.profit_rate >= 0 ? '+' : ''}${showcasePerf.profit_rate.toFixed(2)}%` : '暂无数据' }}</b></div>
          <div><span>胜率</span><b :class="showcasePerf ? '' : 'muted'">{{ showcasePerf ? `${showcasePerf.win_rate.toFixed(1)}%` : '暂无数据' }}</b></div>
          <div><span>跟单人数</span><b>{{ (detail.leader.follower_count || 0).toLocaleString() }}</b></div>
        </div>
        <div class="fd-show-stats fd-show-perf">
          <div><span>交易笔数</span><b :class="showcasePerf ? '' : 'muted'">{{ showcasePerf ? showcasePerf.total_trades.toLocaleString() : '暂无数据' }}</b></div>
          <div><span>盈利 / 亏损</span><b :class="showcasePerf ? '' : 'muted'">{{ showcasePerf ? `${showcasePerf.win_trades} / ${showcasePerf.lose_trades}` : '暂无数据' }}</b></div>
          <div><span>成交手数</span><b :class="showcasePerf ? '' : 'muted'">{{ showcasePerf ? showcasePerf.gross_volume.toFixed(2) : '暂无数据' }}</b></div>
          <div><span>手续费 + 库存费</span><b :class="showcasePerf ? 'down' : 'muted'">{{ showcasePerf ? fmtU(-(showcasePerf.commission_total + showcasePerf.swap_total)) + ' U' : '暂无数据' }}</b></div>
        </div>
      </section>

      <!-- 近 7 日已实现收益：与上面的 7d 窗口同口径，柱子之和 === 该窗口累计收益 -->
      <section class="fd-sec">
        <header>
          <p class="fd-kicker green">近 7 日收益<span>U</span></p>
          <span class="fd-show-hint">按北京时间自然日统计已平仓成交净收益</span>
        </header>
        <div v-if="showcaseDaily.length" class="fd-chart" role="img" :aria-label="showcaseDaily.map((point) => `${point.date}：${fmtU(point.profit)} U`).join('；')">
          <div v-for="point in showcaseDaily" :key="point.ts" class="fd-column">
            <div class="fd-bar-space"><div class="fd-bar" :class="{ negative: point.profit < 0, zero: point.profit === 0 }" :style="{ height: `${Math.max(1, Math.abs(point.profit) / showcaseDailyMax * 100)}%` }"><span>{{ fmtU(point.profit) }}</span></div></div>
            <span class="fd-date">{{ point.date }}</span>
          </div>
        </div>
        <p v-else class="fd-empty">收益数据暂不可用</p>
        <p class="fd-sub">{{ showcaseDailyHasDeals ? '近 7 日已平仓成交的净收益' : '近 7 日暂无已平仓成交' }}</p>
      </section>
      <section class="fd-sec">
        <header>
          <p class="fd-kicker">{{ leaderName(detail.leader) }} 的带单<span>{{ detail.groups.length }}</span></p>
          <span class="fd-show-hint">打开带单状态后即可跟随</span>
        </header>
        <div class="fd-modes">
          <button v-for="key in SHOWCASE_MODES" :key="key" type="button" :class="{ on: showcaseMode === key }" @click="pickShowcaseMode(key)">
            <svg v-if="key === 'trend_market'" viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>
            <svg v-else-if="key === 'trend_limit'" viewBox="0 0 24 24"><path fill="currentColor" d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>
            <svg v-else viewBox="0 0 24 24"><path fill="currentColor" d="m19 9 1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25L19 9zM11.5 9.5 9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5L17 12l-5.5-2.5z"/></svg>
            <span>{{ SELF_MODE_LABEL[key] }}</span>
            <em>{{ showcaseCounts[key] }}</em>
          </button>
        </div>
        <p v-if="!showcaseCards.length" class="fd-empty">该方式下暂无带单</p>
        <div v-else class="fd-show-grid">
          <article v-for="g in showcaseCards" :key="g.group_id" class="fd-card fd-show-card">
            <header>
              <span class="fd-show-mark"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M18 11v2h4v-2h-4zm-2 6.61c.96.71 2.21 1.65 3.2 2.39.4-.53.8-1.07 1.2-1.6-.99-.74-2.24-1.68-3.2-2.4-.4.54-.8 1.08-1.2 1.61zM20.4 5.6c-.4-.53-.8-1.07-1.2-1.6-.99.74-2.24 1.68-3.2 2.4.4.53.8 1.07 1.2 1.6.96-.72 2.21-1.65 3.2-2.4zM4 9c-1.1 0-2 .9-2 2v2c0 1.1.9 2 2 2h1v4h2v-4h1l5 3V6L8 9H4zm11.5 3c0-1.33-.58-2.53-1.5-3.35v6.69c.92-.81 1.5-2.01 1.5-3.34z"/></svg></span>
              <div>
                <b>{{ g.symbol || '未绑定品种' }}</b>
                <p>
                  <svg v-if="showcaseGroupMode(g) === 'trend_market'" viewBox="0 0 24 24"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>
                  <svg v-else-if="showcaseGroupMode(g) === 'trend_limit'" viewBox="0 0 24 24"><path fill="currentColor" d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>
                  <svg v-else viewBox="0 0 24 24"><path fill="currentColor" d="m19 9 1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25L19 9zM11.5 9.5 9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5L17 12l-5.5-2.5z"/></svg>
                  {{ SELF_MODE_LABEL[showcaseGroupMode(g)] || '带单' }}
                </p>
              </div>
              <em :class="g.selectable ? 'on' : 'off'"><i></i>{{ g.selectable ? '带单中' : '已暂停' }}</em>
            </header>
            <div class="fd-show-stats">
              <div><span>跟单人数</span><b>{{ g.follower_count.toLocaleString() }}</b></div>
              <div><span>在线节点</span><b>{{ g.online_count.toLocaleString() }}</b></div>
              <div><span>进行中任务</span><b>{{ g.active_task_count.toLocaleString() }}</b></div>
              <div><span>累计信号</span><b>{{ g.signal_count.toLocaleString() }}</b></div>
            </div>
            <p v-if="!g.selectable" class="fd-show-warn">{{ g.unselectable_reason || '当前不可跟单' }}</p>
          </article>
        </div>
      </section>
    </template>

    <div v-if="pickerOpen && detail" class="fd-mask" @click.self="closePicker">
      <section ref="pickerPanel" class="fd-dialog" role="dialog" aria-modal="true" aria-labelledby="follow-picker-title" tabindex="-1" @keydown.esc.prevent="closePicker" @keydown.tab="trapPickerFocus">
        <header>
          <span class="fd-picker-mark"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 3h7v7H3zm11 11h7v7h-7zM8 12h4V8h2v6h-2v4H8v-2h2v-2H8z"/></svg></span>
          <div><strong id="follow-picker-title">选择跟单节点</strong><p>跟单对象：{{ leaderName(detail.leader) }}</p></div>
          <button type="button" class="fd-x" aria-label="关闭跟单节点选择" :disabled="submitting" @click="closePicker">×</button>
        </header>
        <div class="fd-picker-body">
          <div class="fd-picker-leader"><span class="fd-avatar">✦</span><div><b>{{ leaderName(detail.leader) }}</b><p>交易品种 {{ pickerSymbols.join(' / ') || '暂无绑定品种' }}</p></div><span v-if="pickerSymbols.length" class="fd-picker-symbol">{{ pickerSymbols.join(' / ') }}</span></div>
          <!-- <p v-if="selectableGroups.length" class="fd-picker-label"><span>跟单分组</span><em>{{ selectableGroups.length }} 个</em><small>已全部包含</small></p> -->
          <!-- <p v-if="selectableGroups.length > 1" class="fd-picker-groups">{{ selectableGroups.map(g => groupTitle(g.name, g.strategy_name ?? null)).join(' · ') }}</p> -->
          <p class="fd-picker-label"><span>▣ 选择跟单节点</span><em>{{ detail.my_nodes.length }} 个</em><small>可多选</small></p>
          <label class="fd-picker-search"><svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6" fill="none" stroke="currentColor" stroke-width="2"/><path d="m15 15 5 5" fill="none" stroke="currentColor" stroke-width="2"/></svg><input v-model="pickerQuery" aria-label="搜索跟单节点" placeholder="搜索节点名称 / 账号" :disabled="submitting" @input="searchPicker"></label>
          <p v-if="detailPaused" class="fd-warn">{{ PAUSED_FOLLOW_HINT }}</p>
          <div class="fd-picker-list">
            <p v-if="!detail.my_nodes.length" class="fd-empty">没有可用于跟单的已开通节点。</p>
            <p v-else-if="!pickerNodes.length" class="fd-empty">没有匹配的节点</p>
            <label v-for="n in pickerNodes" :key="n.node_id" class="fd-pick" :class="{ on: selectedNodes.includes(n.node_id), off: !isNodeSelectable(n.node_id) }">
              <input type="checkbox" :checked="selectedNodes.includes(n.node_id)" :disabled="submitting || !isNodeSelectable(n.node_id)" :aria-label="`选择节点 ${n.name || n.node_id}`" @change="toggleNode(n.node_id)">
              <i class="fd-online-dot" :class="{ online: n.online || n.status === 'online' }"></i>
              <span class="fd-pick-info"><b>{{ n.name || n.node_id }} <em v-if="n.is_primary">♙ 主节点</em></b><small>{{ n.mt5_login || n.node_id }} · {{ auth.me?.roles.map(role => role.name).join(' / ') || '我的节点' }} · 余额 {{ n.balance != null ? n.balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : nodeBalance(n.node_id) }} U</small><small v-if="nodeFollowHint(n.node_id)" class="fd-pick-hint">{{ nodeFollowHint(n.node_id) }}</small></span>
            </label>
          </div>
        </div>
        <footer>
          <div class="fd-picker-buttons"><button type="button" class="fd-picker-cancel" :disabled="submitting" @click="closePicker">取消</button><button type="button" class="primary" :disabled="submitting || detailPaused || !targetGroupIds.length || !selectedNodes.some(isNodeSelectable)" @click="submitJoin">{{ submitting ? '提交中…' : `⇄ 使用 ${selectedNodes.length} 个节点跟单` }}</button></div>
          <p>{{ selectableGroups.length > 1 ? `可同时勾选多个节点，所选节点将跟随该带单员的全部 ${selectableGroups.length} 个分组` : '可同时勾选多个节点，审核通过后同步所选分组的策略信号' }}</p>
        </footer>
      </section>
    </div>

    <ElDialog :model-value="!!stopTargets.length" title="确认停止跟单" width="min(420px, 92vw)"
      :close-on-click-modal="false" :close-on-press-escape="!mutationBusy" :show-close="!mutationBusy"
      @close="cancelStop">
      <p>将停止选中的 {{ selectedStops.length }} 条跟单关系，当前快照可见 {{ stopPositions }} 笔持仓。</p>
      <div class="fd-stop-list"><label v-for="t in stopTargets" :key="t.group_id + ':' + t.node_id">
        <input v-model="selectedStops" type="checkbox" :value="t" :disabled="mutationBusy"
          @change="trackFollowUi('select_nodes', { value: 'stop-follow', node_ids: [t.node_id] })" /> {{ t.label }}
      </label></div>
      <label><input v-model="stopWithClose" type="checkbox" @change="trackFollowUi('filter', { value: stopWithClose ? 'stop-and-close' : 'stop-and-keep' })" :disabled="mutationBusy" /> 同时平仓并关闭相关任务</label>
      <p>默认保留持仓，进行中的任务继续按策略执行。</p>
      <template #footer>
        <button type="button" :disabled="mutationBusy" @click="cancelStop">取消</button>
        <button type="button" class="fd-close" :disabled="mutationBusy || !selectedStops.length" @click="stopSelected">确认停止</button>
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
.fd-intro-head, .fd-sec header, .fd-card-top, .fd-item-top, .fd-book-head, .fd-node-card header, .fd-node-card footer, .fd-item footer, .fd-self-item footer, .fd-dialog header, .fd-dialog footer, .fd-pick, .fd-preview-top, .fd-mine-title, .fd-tabs, .fd-filters {
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
.fd-ghost, .fd-follow, .fd-close, .fd-leave, .fd-link, .fd-tabs button, .fd-filters button, .fd-dialog button, .fd-pick { cursor: pointer; }
.fd-ghost { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 4px 8px; font-size: 10px; }
/* 列宽用 minmax(0,1fr)：默认 auto 列会被卡片内容的 min-content 撑开，
   窄屏下卡片就会顶出屏幕；子项 + min-width:0 才能配合收缩。 */
.fd-grid { margin-top: 8px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; min-width: 0; max-width: 100%; }
.fd-leader, .fd-card, .fd-node-card { padding: 12px; }
.fd-leader { display: flex; align-items: center; gap: 8px; }
.fd-leader:hover, .fd-card:hover, .fd-preview:hover { border-color: rgba(124,58,237,.5); }
/* 广场卡片整张可点：进带单员详情页。键盘也要能进（tabindex + enter/space）。 */
.fd-leader-link { cursor: pointer; }
.fd-leader-link:focus-visible { outline: 2px solid rgba(124,58,237,.7); outline-offset: 2px; }
.fd-leader-link .fd-follow { position: relative; z-index: 1; }
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
.fd-leader-text > p:first-child { flex-wrap: nowrap; }
.fd-leader-text > p:first-child em, .fd-leader-text > p:first-child .fd-verified { flex: 0 0 auto; }
.fd-leader-text p.fd-blurb { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin: 2px 0 0; color: #8B96A8; font-size: 10px; line-height: 1.625; white-space: normal; }
.fd-follow { display: flex; align-items: center; gap: 2px; border: 0; border-radius: 8px; background: #7C3AED; color: #fff; font-size: 10px; font-weight: 700; padding: 6px 10px; }
.fd-follow:disabled, .fd-close:disabled { opacity: .4; cursor: not-allowed; }
.fd-preview { width: 100%; margin-top: 8px; text-align: left; padding: 12px; color: inherit; background: linear-gradient(90deg, #101828, #0F1E2E); }
.fd-swap { width: 36px; height: 36px; border-radius: 12px; color: #22C55E; background: rgba(34,197,94,.15); }
.fd-swap svg { width: 18px; height: 18px; }
.fd-preview-top b { display: block; font-size: 12px; }
.fd-chev { margin-inline-start: auto; flex-shrink: 0; color: #4A5568; }
.fd-chips { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; width: 100%; margin-top: 10px; padding-top: 10px; border-top: 1px solid #1E2B47; }
.fd-chips > span, .fd-stats > div, .fd-sum4 > div, .fd-stat3 > div { min-width: 0; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 6px 8px; }
.fd-chips b { display: flex; align-items: center; gap: 4px; font-size: 9px; }
.fd-chips b svg { color: #A78BFA; width: 11px; height: 11px; }
.fd-chips strong, .fd-stat3 b, .fd-stats b, .fd-sum4 b { display: block; margin-top: 2px; font-size: 11px; }
.fd-chips small, .fd-stats span, .fd-sum4 span, .fd-stat3 span { display: flex; align-items: center; gap: 4px; color: #5B6B85; font-size: 9px; }
.fd-preview-empty { display: block; margin-top: 8px; color: #5B6B85; font-size: 11px; }
.fd-card { margin-top: 8px; min-width: 0; max-width: 100%; overflow: hidden; }
.fd-sec header em.fd-sec-note { border: 0; background: transparent; padding: 0; color: #5B6B85; font-size: 10px; font-weight: 400; }
.fd-self-top, .fd-self-row, .fd-self-pos > div, .fd-self .fd-book-head > span, .fd-book-meta, .fd-book-meta b, .fd-self-link, .fd-self-run { display: flex; align-items: center; }
.fd-self-grid { margin-top: 8px; display: grid; gap: 10px; }
.fd-self-grid > .fd-card { margin-top: 0; }
.fd-self { min-width: 0; max-width: 100%; }
.fd-self-top { gap: 10px; min-width: 0; }
.fd-self-id { min-width: 0; flex: 1; }
.fd-self-id b { font-size: 14px; font-weight: 700; }
.fd-self-side { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex: 0 0 auto; }
.fd-self .fd-self-run { gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 999px; padding: 2px 8px; font-size: 10px; font-weight: 650; font-style: normal; }
.fd-self-run svg { width: 11px; height: 11px; color: #22C55E; }
.fd-self-link { gap: 2px; border: 0; background: transparent; color: #3B82F6; font-size: 10px; font-weight: 500; cursor: pointer; }
.fd-self-link svg { width: 12px; height: 12px; }
.fd-self-link:disabled { opacity: .4; cursor: not-allowed; }
.fd-self-list { margin-top: 10px; display: grid; gap: 6px; min-width: 0; }
.fd-self-item { border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 10px; min-width: 0; max-width: 100%; }
.fd-self-row { justify-content: space-between; gap: 8px; min-width: 0; max-width: 100%; }
.fd-self-row p { display: flex; align-items: center; gap: 6px; min-width: 0; margin: 0; flex: 1; overflow: hidden; }
.fd-self-row p > svg { width: 13px; height: 13px; color: #A78BFA; }
.fd-self-row b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.fd-self .fd-mf, .fd-self .fd-node { border-radius: 4px; padding: 1px 4px; font-size: 9px; font-weight: 500; font-style: normal; flex: 0 0 auto; }
.fd-mf { border: 1px solid #22304A; background: #0A101E; color: #5B6B85; font-family: ui-monospace, monospace; }
.fd-node { display: inline-flex; align-items: center; gap: 2px; border: 1px solid rgba(124,58,237,.4); background: rgba(124,58,237,.1); color: #C4B5FD; }
.fd-node svg { width: 10px; height: 10px; }
.fd-self-row > p > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5B6B85; font-size: 9px; }
.fd-self-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.fd-self-actions > em { display: inline-flex; align-items: center; gap: 4px; border: 0; border-radius: 999px; padding: 2px 8px; font-size: 10px; font-weight: 650; font-style: normal; }
.fd-self-actions > em.live { color: #22C55E; background: rgba(34,197,94,.15); }
.fd-self-actions > em.pause, .fd-self-actions > em.idle { color: #8B96A8; background: rgba(91,107,133,.15); }
.fd-self-actions > em i { width: 6px; height: 6px; border-radius: 99px; background: currentColor; }
.fd-self-actions > em.live i { animation: fd-pulse 1.6s ease-out infinite; }
.fd-self-hold { display: inline-flex; align-items: center; gap: 4px; border: 1px solid rgba(246,70,93,.4); background: rgba(246,70,93,.1); color: #F6465D; border-radius: 8px; padding: 4px 8px; font-size: 10px; font-weight: 700; cursor: pointer; }
.fd-self-hold svg { width: 12px; height: 12px; }
.fd-self-hold.on { border: 0; background: #22C55E; color: #fff; }
.fd-self-hold:disabled { opacity: .45; cursor: not-allowed; }
.fd-self .fd-book-head > span { gap: 4px; min-width: 0; }
.fd-self .fd-book-head svg { width: 11px; height: 11px; }
.fd-self .fd-book-head > span:first-child svg { color: #38BDF8; }
.fd-book-meta { gap: 6px; min-width: 0; color: #5B6B85; font-size: 9px; font-variant-numeric: tabular-nums; }
.fd-book-meta b { gap: 4px; color: #E6EBF4; font-size: 9px; }
.fd-book-meta b svg { color: #A78BFA; }
.fd-book-meta i { font-style: normal; flex: 0 0 auto; }
.fd-self-orders { max-height: 240px; overflow-y: auto; }
.fd-self-pos { padding: 6px 8px; border-bottom: 1px solid #161F30; }
.fd-self-pos:last-child { border-bottom: 0; }
.fd-self-pos > div { gap: 6px; }
.fd-self-pos em { font-style: normal; font-size: 9px; font-weight: 700; border-radius: 4px; padding: 1px 4px; }
.fd-self-pos em.buy { color: #22C55E; background: rgba(34,197,94,.15); }
.fd-self-pos em.sell { color: #F6465D; background: rgba(246,70,93,.15); }
.fd-self-pos > div > span { font-size: 9px; color: #8B96A8; font-variant-numeric: tabular-nums; }
.fd-self-pos .hold { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; color: #38BDF8; font-size: 9px; font-weight: 650; }
.fd-self-pos .hold i { width: 6px; height: 6px; border-radius: 99px; background: #38BDF8; }
.fd-self-pos strong { width: 64px; flex: 0 0 auto; text-align: right; font-size: 10px; font-variant-numeric: tabular-nums; }
.fd-self-pos > p { margin: 2px 0 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5B6B85; font-size: 9px; font-variant-numeric: tabular-nums; }
.fd-self .fd-stats b { color: #E6EBF4; }
.fd-self .fd-stats b.up { color: #22C55E; }
.fd-self .fd-stats b.down { color: #F6465D; }
.fd-self-item .fd-self-pos > div { flex-wrap: wrap; }
.fd-self-item .fd-self-pos strong { margin-left: auto; }
.fd-self .fd-close:hover:not(:disabled) { background: #E03A50; }
@keyframes fd-pulse { 0% { box-shadow: 0 0 0 0 rgba(34,197,94,.55); } 100% { box-shadow: 0 0 0 5px rgba(34,197,94,0); } }
.fd-link { border: 0; background: transparent; color: #3B82F6; font-size: 10px; }
.fd-item { margin-top: 10px; min-width: 0; max-width: 100%; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 10px; }
.fd-item-top { justify-content: space-between; }
.fd-item-top p { display: flex; align-items: center; gap: 6px; min-width: 0; margin: 0; flex: 1; }
.fd-item-top p b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fd-item-top > em { flex: 0 0 auto; }
.fd-item-top svg { color: #A78BFA; }
.fd-item-top b { font-size: 11px; }
.fd-item-top em { border-radius: 4px; }
.fd-item-top span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5B6B85; font-size: 9px; }
.fd-book { margin-top: 8px; min-width: 0; max-width: 100%; overflow: hidden; border: 1px solid #1E2B47; border-radius: 6px; background: #0A101E; }
.fd-book-head { justify-content: space-between; gap: 8px; padding: 6px 8px; background: #0C1220; border-bottom: 1px solid #1E2B47; color: #8B96A8; font-size: 9px; }
.fd-book-head > span:last-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }
.fd-book .fd-empty { padding: 8px; }
/* 持仓行：左侧「BUY XAUUSD + 开仓详情」可能很长，必须能收缩省略，
   否则窄屏下会把右侧盈亏挤出卡片。 */
.up { color: #22C55E; } .down { color: #F6465D; }
.fd-stats, .fd-sum4 { margin-top: 8px; display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; }
.fd-sum4 { grid-template-columns: 1fr 1fr; }
.fd-stats > div, .fd-sum4 > div { background: #0A101E; border-color: #1E2B47; }
.fd-self-item footer, .fd-item footer, .fd-node-card footer { justify-content: flex-end; margin-top: 8px; padding-top: 8px; border-top: 1px solid #1E2B47; }
.fd-close { display: inline-flex; align-items: center; gap: 4px; border: 0; border-radius: 8px; background: #F6465D; color: #fff; font-size: 11px; font-weight: 650; padding: 6px 12px; box-shadow: 0 2px 8px rgba(246,70,93,.35); }
.fd-leave { border: 1px solid rgba(246,70,93,.7); background: transparent; color: #F6465D; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 650; }
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
.fd-mask { position: fixed; inset: 0; z-index: 40; background: rgba(0, 0, 0, 0.32); display: flex; align-items: center; justify-content: center; padding: 16px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-bottom: max(16px, env(safe-area-inset-bottom, 0px)); }
/* dvh + 安全区：底部按钮要让开 iOS Home Indicator */
.fd-dialog { width: min(480px, 100%); max-height: calc(100dvh - 32px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); display: flex; flex-direction: column; padding: 0; background: #0D1220; border-radius: 16px; box-shadow: 0 24px 80px #0008; outline: none; }
.fd-dialog header, .fd-dialog footer { justify-content: space-between; }
.fd-dialog p { margin: 2px 0 0; color: #8B96A8; font-size: 12px; }
.fd-x { width: 28px; height: 28px; border-radius: 8px; border: 1px solid #22304A; background: #0A0F1A; color: #8B96A8; }
.fd-pick { width: 100%; text-align: left; margin-top: 8px; padding: 8px; border-radius: 10px; border: 1px solid #22304A; background: #0C1220; color: inherit; }
.fd-pick.on { border-color: #7C3AED; }
.fd-pick.off { opacity: .45; }
.fd-pick .box { width: 16px; height: 16px; border: 1px solid #3D4C66; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; }
.fd-pick small { display: block; color: #8B96A8; }
.fd-dialog .primary, .fd :deep(.el-dialog__footer) .primary { background: #7C3AED; color: #fff; border: 0; border-radius: 8px; padding: 6px 12px; }
.fd-dialog>header { padding: 12px 16px; border-bottom: 1px solid #263047; flex-shrink: 0; justify-content: flex-start; }
.fd-dialog>header strong { font-size: 16px; }
.fd-dialog>header p { font-size: 11px; margin-top: 3px; }
.fd-dialog .fd-x { margin-left: auto; border: 0; background: transparent; font-size: 21px; }
.fd-picker-mark { width: 40px; height: 40px; display: grid; place-items: center; border-radius: 12px; background: linear-gradient(135deg,#4c1d95,#172554); color: #c4b5fd; }
.fd-picker-mark svg { width: 23px; height: 23px; }
.fd-picker-body { padding: 12px 16px; overflow-y: auto; min-height: 0; }
.fd-picker-leader { display: flex; gap: 10px; align-items: center; border: 1px solid #273147; border-radius: 10px; padding: 10px; background: #090f19; }
.fd-picker-leader b { font-size: 12px; }.fd-picker-leader p { font-size: 10px; }
.fd-picker-symbol { margin-left: auto; max-width: 45%; color: #94a3b8; background: #121b29; border: 1px solid #273147; border-radius: 5px; padding: 3px 6px; font-size: 10px; overflow-wrap: anywhere; }
.fd-dialog .fd-picker-label { display: flex; gap: 7px; align-items: center; margin: 13px 0 8px; font-size: 11px; }
.fd-picker-label>span { color: #cbd5e1; }.fd-picker-label>em { color: #7f8da5; font-style: normal; border: 1px solid #263047; border-radius: 12px; padding: 1px 6px; font-size: 10px; }.fd-picker-label>small { color: #22d3ee; font-size: 10px; }
.fd-picker-search { display: flex; gap: 8px; align-items: center; border: 1px solid #263047; border-radius: 7px; padding: 8px 10px; background: #090f19; }.fd-picker-search svg { width: 14px; height: 14px; color: #64748b; }.fd-picker-search input { width: 100%; min-width: 0; border: 0; outline: none; background: transparent; color: #e2e8f0; font: inherit; font-size: 12px; }
.fd-picker-list { max-height: 224px; overflow-y: auto; margin-top: 8px; padding-right: 5px; scrollbar-width: thin; scrollbar-color: #4b347c #141c2a; }
.fd-picker-groups { margin: 0 0 2px; padding: 7px 10px; border: 1px dashed #263047; border-radius: 8px; background: #111a2a; color: #a5b4c9; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.fd-picker-list .fd-pick { min-height: 56px; margin: 0 0 6px; padding: 9px 10px; cursor: pointer; gap: 7px; }.fd-picker-list .fd-pick.on { background: #23173d; box-shadow: inset 0 0 0 1px #7c3aed55; }.fd-picker-list input { width: 16px; height: 16px; margin: 0; accent-color: #7c3aed; flex-shrink: 0; }.fd-pick-info { min-width: 0; }.fd-pick-info>b { font-size: 13px; }.fd-pick-info em { display: inline-block; border: 1px solid #785b1e; border-radius: 4px; background: #4b3819; color: #fbbf24; font-size: 9px; padding: 1px 4px; font-style: normal; vertical-align: middle; }.fd-pick-info small { font-size: 10px; margin-top: 3px; overflow-wrap: anywhere; }.fd-pick-info .fd-pick-hint { color: #fbbf24; }.fd-online-dot { width: 7px; height: 7px; flex-shrink: 0; background: #64748b; border-radius: 50%; }.fd-online-dot.online { background: #22c55e; box-shadow: 0 0 5px #22c55e55; }
.fd-dialog>footer { display: block; padding: 12px 16px; border-top: 1px solid #263047; flex-shrink: 0; }.fd-picker-buttons { display: flex; gap: 8px; }.fd-picker-cancel { flex: 1; border: 1px solid #263047; background: #111a2a; color: #a0aec0; border-radius: 10px; padding: 11px; }.fd-picker-buttons .primary { flex: 2; padding: 11px; background: linear-gradient(100deg,#7c3aed,#2563eb); font-weight: 600; box-shadow: 0 4px 12px #7c3aed33; }.fd-dialog button:disabled { opacity: .45; cursor: not-allowed; }.fd-dialog>footer>p { text-align: center; font-size: 10px; margin-top: 9px; line-height: 1.5; }.fd-picker-list .fd-pick.off { cursor: not-allowed; }.fd-pending { color: #fbbf24!important; background: #78350f33; border: 1px solid #92400e55; }.fd-application-node { font-size: 11px; color: #a5b4c9; margin-bottom: 4px; }.fd-application-node span { color: #fbbf24; font-size: 10px; }.fd-application-note { color: #94a3b8; font-size: 11px; margin: 8px 0; }.fd-applications .fd-item>button { padding: 2px 0; }
.fd-picker-search input { height: 16px; padding: 0; line-height: 16px; box-shadow: none; }
.fd-picker-list .fd-pick { text-transform: none; letter-spacing: normal; color-scheme: dark; }
.fd-warn { color: #FBBF24; font-size: 12px; }
.fd-show { padding: 0; overflow: hidden; }
.fd-show-banner { height: 64px; background: linear-gradient(90deg, rgba(124,58,237,.4), rgba(37,99,235,.3), #101828); }
.fd-show-body { padding: 0 12px 12px; }
.fd-show-id, .fd-show-who, .fd-show-nodes, .fd-show-actions, .fd-show-card header, .fd-show-nodes p, .fd-show-nodes strong, .fd-show-card header p { display: flex; align-items: center; }
.fd-show-id { align-items: flex-end; justify-content: space-between; gap: 8px; }
.fd-show-who { align-items: flex-end; gap: 12px; min-width: 0; }
.fd-show-avatar { width: 56px; height: 56px; margin-top: -20px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 16px; border: 2px solid #101828; background: linear-gradient(135deg, #7C3AED, #2563EB); color: #fff; font-size: 20px; font-weight: 700; box-shadow: 0 4px 16px rgba(124,58,237,.45); }
.fd-show-who > div { min-width: 0; margin-bottom: 4px; }
.fd-show-who p { gap: 4px; margin: 0; min-width: 0; }
.fd-show-who b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.fd-show-who small { display: block; margin-top: 2px; color: #5B6B85; font-size: 10px; }
.fd-show em.fd-show-cert, .fd-show em.pause { flex: 0 0 auto; border-radius: 999px; padding: 4px 10px; font-size: 10px; font-weight: 650; font-style: normal; }
.fd-show em.fd-show-cert { border: 1px solid rgba(245,158,11,.4); background: rgba(245,158,11,.1); color: #F59E0B; }
.fd-show-nodes { gap: 10px; margin-top: 10px; border: 1px solid #22304A; border-radius: 12px; padding: 10px 12px; background: linear-gradient(90deg, #0C1220, #131C30); }
.fd-show-mark { width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 8px; background: rgba(34,211,238,.1); color: #22D3EE; }
.fd-show-card .fd-show-mark { background: rgba(37,99,235,.15); color: #3B82F6; }
.fd-show-nodes > div { min-width: 0; flex: 1; }
.fd-show-nodes p { gap: 4px; margin: 0; color: #5B6B85; font-size: 10px; }
.fd-show-nodes p svg { width: 11px; height: 11px; color: #22D3EE; }
.fd-show-nodes strong { align-items: baseline; gap: 4px; margin-top: 2px; font-size: 16px; font-variant-numeric: tabular-nums; }
.fd-show-nodes strong span { color: #5B6B85; font-size: 10px; font-weight: 500; }
.fd-show .fd-show-nodes > em { flex: 0 0 auto; border-radius: 999px; padding: 4px 8px; font-size: 10px; }
.fd-show-nodes > em i { width: 6px; height: 6px; border-radius: 99px; background: currentColor; }
.fd-show-nodes > em.on i { animation: fd-pulse 1.6s ease-out infinite; }
.fd-show-bio { margin: 10px 0 0; border: 1px solid #22304A; border-radius: 8px; background: #0C1220; padding: 10px 12px; color: #8B96A8; font-size: 11px; line-height: 1.625; }
.fd-show-actions { align-items: stretch; flex-direction: column; gap: 8px; margin-top: 12px; }
.fd-show-apply, .fd-show-back { border-radius: 12px; padding: 10px 16px; font-size: 12px; font-weight: 700; cursor: pointer; }
.fd-show-apply { display: flex; flex: 1; align-items: center; justify-content: center; gap: 4px; border: 0; color: #fff; background: linear-gradient(90deg, #7C3AED, #6D28D9); box-shadow: 0 2px 10px rgba(124,58,237,.4); }
.fd-show-apply svg { width: 15px; height: 15px; }
.fd-show-apply:disabled { opacity: .45; cursor: not-allowed; }
.fd-show-back { border: 1px solid #22304A; background: #0C1220; color: #8B96A8; }
.fd-show-hint { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5B6B85; font-size: 10px; }
.fd-modes { margin-top: 8px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; border: 1px solid #1C2740; background: #101828; border-radius: 12px; padding: 6px; }
.fd-modes button { display: flex; align-items: center; justify-content: center; gap: 4px; min-width: 0; border: 0; border-radius: 8px; background: transparent; color: #8B96A8; padding: 8px 4px; font-size: 11px; font-weight: 650; cursor: pointer; }
.fd-modes button.on { color: #fff; background: linear-gradient(90deg, #7C3AED, #6D28D9); box-shadow: 0 2px 8px rgba(124,58,237,.35); }
.fd-modes svg { width: 14px; height: 14px; }
.fd-modes span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fd-modes em { flex: 0 0 auto; border: 0; border-radius: 999px; background: #1A2438; color: #5B6B85; padding: 0 6px; font-size: 9px; font-style: normal; font-weight: 650; }
.fd-modes button.on em { background: rgba(255,255,255,.2); color: #fff; }
.fd-show-grid { margin-top: 8px; display: grid; gap: 10px; }
.fd-show-card { margin-top: 0; }
.fd-show-card header { justify-content: space-between; gap: 8px; }
.fd-show-card header > div { min-width: 0; flex: 1; }
.fd-show-card header b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; letter-spacing: .02em; }
.fd-show-card header p { gap: 4px; width: fit-content; max-width: 100%; margin: 2px 0 0; border-radius: 6px; background: rgba(124,58,237,.15); color: #A78BFA; padding: 1px 6px; font-size: 10px; font-weight: 500; }
.fd-show-card header p svg { width: 11px; height: 11px; }
.fd-show-card header > em { flex: 0 0 auto; padding: 2px 8px; font-size: 10px; }
.fd-show-card header > em.on i { animation: fd-pulse 1.6s ease-out infinite; }
.fd-show-stats, .fd-show-pair { display: grid; gap: 6px; margin-top: 10px; }
.fd-show-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.fd-show-pair { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.fd-show-stats > div, .fd-show-pair > div { min-width: 0; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 8px 6px; }
.fd-show-pair > div { padding: 8px 12px; }
.fd-show-stats span, .fd-show-pair span { display: block; color: #5B6B85; font-size: 9px; }
.fd-show-stats b, .fd-show-pair b { display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #E6EBF4; font-size: 12px; font-variant-numeric: tabular-nums; }
.fd-show-stats b.up, .fd-show-pair b.up { color: #22C55E; }
.fd-show-stats b.down, .fd-show-pair b.down { color: #F6465D; }
.fd-show-stats b.muted, .fd-show-pair b.muted { color: #8B96A8; font-weight: 650; }
/* 绩效概览块：两组各 4 个指标，视觉上比分组卡更「数据」 */
.fd-show-perf { margin-top: 8px; }
.fd-show-perf + .fd-show-perf { margin-top: 6px; }
.fd-show-perf > div { padding: 8px 10px; }
.fd-show-perf b { font-size: 13px; }
/* 不可跟单时的原因提示：置灰按钮必须说清楚理由 */
.fd-show-warn { margin: 8px 0 0; border: 1px solid rgba(245,158,11,.35); border-radius: 8px; background: rgba(245,158,11,.08); color: #F5B041; padding: 8px 10px; font-size: 10px; line-height: 1.6; }
/* 近 7 日收益柱状图 */
.fd-chart { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 6px; padding-top: 22px; }
.fd-column { display: flex; min-width: 0; flex-direction: column; gap: 5px; text-align: center; }
.fd-bar-space { display: flex; height: 62px; align-items: flex-end; border-bottom: 1px solid #22304A; }
.fd-bar { position: relative; width: 100%; border-radius: 4px 4px 0 0; background: #22C55E; }
.fd-bar.negative { background: #F6465D; }
.fd-bar.zero { background: #334155; }
.fd-bar span { position: absolute; bottom: calc(100% + 4px); left: 50%; transform: translateX(-50%); color: #8B96A8; font-size: 9px; white-space: nowrap; }
.fd-date { color: #5B6B85; font-size: 9px; }
.fd-show-mark svg { width: 18px; height: 18px; }
@media (min-width: 640px) {
  .fd-show-banner { height: 80px; }
  .fd-show-who b { font-size: 16px; }
  .fd-show-actions { flex-direction: row; }
  .fd-show-back { flex: 0 0 auto; }
}
@media (min-width: 1024px) {
  .fd-grid, .fd-show-grid, .fd-self-grid { grid-template-columns: 1fr 1fr; }
  .fd-sum4 { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 720px) {
  .fd-leader, .fd-card-top, .fd-item-top, .fd-self-top, .fd-self-row, .fd-self .fd-book-head, .fd-self-pos > div { flex-wrap: nowrap; }
  .fd-leader-text b, .fd-card-top b, .fd-item-top b, .fd-self-id b, .fd-self-id small { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fd-item-top p { flex: 1; }
  .fd-self-id small { display: block; }
  .fd-self .fd-node { min-width: 0; max-width: 88px; flex: 0 1 auto; overflow: hidden; }
  .fd-self-row > p > span { max-width: 92px; }
  .fd-book-meta { min-width: 0; overflow: hidden; }
  .fd-book-meta i { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fd-stats > div, .fd-stat3 > div, .fd-chips > span { padding: 6px 4px; }
}

.fd-mine .fd-mark { width: 36px; height: 36px; border-radius: 12px; background: linear-gradient(135deg,#2563eb,#7c3aed); }
:global(.app-shell.is-customer:has(.fd-mine)) { background: #0b111c; }
:global(.app-shell.is-customer:has(.fd-mine) .brand-back) { width: 28px; height: 28px; justify-content: center; margin-right: 4px; color: #8b96a8; }
.fd-mine .fd-self-link { padding: 0; line-height: 15px; }
.fd-mine .fd-self-id em.ok { padding: 3px 6px; }
.fd-mine .fd-self-id p { line-height: 18px; }
.fd-mine .fd-self-pos > div { line-height: 16px; }
.fd-mine .fd-self-pos > p { line-height: 14px; }
.fd-mine .fd-self-row p > b { flex: none; max-width: 110px; }
.fd-mine .fd-book-meta b { flex: none; }
.fd-mine .fd-stats > div { padding: 6px 8px; }
.fd-mine .fd-stats span { line-height: 14px; }
.fd-mine .fd-stats b { line-height: 17px; }
.fd-mine .fd-mine-title { gap: 10px; }
.fd-mine .fd-mine-title p { font-size: 14px; line-height: 20px; }
.fd-mine .fd-mine-title small { display: block; margin-top: 2px; line-height: 15px; }
.fd-mine .fd-sec > header { min-height: 21px; }
.fd-mine .fd-sec > .fd-grid { margin-top: 10px; }
.fd-mine .fd-mine-hero .fd-note { line-height: 15px; }
.fd-mine .fd-node-card .fd-avatar { width: 36px; height: 36px; background: #2563eb28; color: #60a5fa; }
.fd-mine .fd-nodes .fd-pnl { margin-top: 10px; padding: 10px 12px; }
.fd-mine .fd-nodes .fd-pnl span { color: #8b96a8; font-size: 10px; }
.fd-mine .fd-nodes .fd-pnl b { font-size: 16px; }
.fd-mine .fd-batch button { padding: 6px 10px; font-size: 11px; line-height: 16px; border-radius: 8px; }
.fd-mine .fd-nodes .fd-self-hold { color: #f59e0b; background: #f59e0b19; border-color: #f59e0b66; }
.fd-mine .fd-nodes .fd-close, .fd-mine .fd-nodes .fd-leave { color: #f6465d; background: #f6465d19; border: 1px solid #f6465d66; box-shadow: none; }
.fd-mine .fd-nodes .fd-filters { gap: 6px; flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; margin-top: 10px; }
.fd-mine .fd-filters button { flex: none; font-size: 10px; font-weight: 600; }
.fd-mine .fd-filters button.on { color: #60a5fa; background: #2563eb26; border-color: #2563eb99; }
.fd-mine .fd-filters button span { border-radius: 8px; padding: 0 4px; background: #1a2438; }
.fd-mine .fd-nodes .fd-sum4 > div { padding: 8px; border-radius: 8px; }
.fd-mine .fd-nodes .fd-sum4 b { font-size: 14px; margin-top: 4px; }
.fd-nodes .fd-symbol { margin-right: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 1px solid #22304a; padding: 2px 5px; border-radius: 5px; font: 9px/1.5 ui-monospace,monospace; color: #8b96a8; }
.fd-mine .fd-stat3 > div { padding: 12px; border-radius: 12px; border-color: #1c2740; box-shadow: 0 2px 12px #0005; }
.fd-mine .fd-stat3 span { font-size: 10px; line-height: 15px; }
.fd-mine .fd-stat3 b, .fd-mine .fd-sum4 b { overflow-wrap: anywhere; }
.fd-mine .fd-stat3 b { font-size: 16px; margin-top: 4px; line-height: 24px; }
.fd-mine .fd-note { background: #0c1220; color: #5b6b85; border: 1px solid #22304a; border-radius: 8px; padding: 8px 12px; font-size: 10px; }
.fd-mine .fd-note svg { color: #8b96a8; }
.fd-mine .fd-tabs, .fd-mine .fd-sec > header { padding-inline: 2px; }
.fd-mine .fd-card, .fd-mine .fd-node-card { margin-top: 0; }
.fd-mine .fd-self-id p { display: flex; align-items: center; gap: 4px; margin: 0; }
.fd-mine .fd-self-id b, .fd-mine .fd-self-id small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fd-mine .fd-self-id small { display: block; margin-top: 3px; color: #5b6b85; }
.fd-mine .fd-self-run { gap: 4px; border: 1px solid #22304a; border-radius: 999px; background: #0c1220; color: #8b96a8; padding: 2px 8px; font-size: 10px; }
.fd-mine .fd-mf, .fd-mine .fd-node { display: inline-block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100px; border-radius: 4px; font: 9px/16px ui-monospace,monospace; padding: 0 4px; }
.fd-mine .fd-node svg { width: 10px; height: 10px; vertical-align: middle; }
.fd-sync { color: #5b6b85; font-size: 10px; }
.fd-mine .fd-book-head > span:first-child { flex-shrink: 0; }
.fd-mine .fd-book-head > span:first-child b { color: #38bdf8; }
.fd-mine .fd-self-pos .hold { color: #38bdf8; }
.fd-mine .fd-self-pos .tp { color: #22c55e; }
.fd-mine .fd-self-pos .sl { color: #f6465d; }
.fd-mine .fd-self-pos .closed { color: #8b96a8; }
.fd-mine .fd-self-pos .hold i { background: currentColor; }
.fd-mine .fd-item + .fd-item { margin-top: 6px; }
.fd-mine-empty { border: 1px dashed #22304a; background: #0c1220; border-radius: 12px; text-align: center; padding: 32px 16px; color: #8b96a8; margin-top: 8px; font-size: 12px; }
.fd-batch, .fd-nodes > header { display: flex; flex-wrap: wrap; gap: 4px; }
.fd-batch button { padding: 4px 8px; font-size: 10px; }
.fd-mine button:disabled { cursor: not-allowed; opacity: .45; }
.fd-mine .fd-search input { min-width: 0; }
.fd-mine .fd-search button { border: 0; background: transparent; color: #8b96a8; }
.fd-mine .fd-node-card { border-radius: 8px; border-color: #22304a; background: #0c1220; }
.fd-node-card header small, .fd-node-card header b { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fd-nodes .fd-node-card .fd-pnl { border: 1px solid #22c55e40; background: #22c55e12; }
.fd-nodes .fd-node-card .fd-pnl.down { border-color: #f6465d40; background: #f6465d12; }
.fd-nodes .fd-node-card .fd-stats { gap: 0; border-radius: 8px; border: 1px solid #1a2438; overflow: hidden; }
.fd-nodes .fd-node-card .fd-stats > div { text-align: center; border: 0; border-radius: 0; }
.fd-nodes .fd-node-card .fd-stats span { justify-content: center; }
.fd-nodes .fd-node-card footer { gap: 6px; margin-top: 10px; border-color: #1a2438; }
.fd-nodes .fd-node-card footer button { display: inline-flex; flex: none; width: 32px; height: 32px; padding: 0; align-items: center; justify-content: center; }
.fd-nodes .fd-node-card footer svg { width: 15px; height: 15px; }
.fd-nodes .fd-batch button, .fd-nodes .fd-pnl > span { display: inline-flex; align-items: center; gap: 4px; }
.fd-nodes .fd-batch svg { width: 13px; height: 13px; }
.fd-nodes .fd-pnl svg { color: #22c55e; }
.fd-nodes .fd-pnl.down svg { color: #f6465d; transform: scaleY(-1); }
.fd-nodes .fd-avatar svg { width: 16px; height: 16px; }
.fd-mine .fd-nodes .fd-batch .resume { color: #22c55e; border-color: #22c55e66; background: #22c55e1a; }
.fd-mine .fd-nodes .fd-self-hold:hover:enabled { background: #f59e0b33; }
.fd-mine .fd-nodes .fd-batch .resume:hover:enabled { background: #22c55e33; }
.fd-mine .fd-nodes :is(.fd-close, .fd-leave):hover:enabled { background: #f6465d33; }
.fd-mine .fd-nodes :is(.fd-close, .fd-leave, .fd-self-hold):disabled { opacity: 1; color: #4a5568; border-color: #22304a; background: #0a101e; }
.fd-stop-list { display: grid; gap: 6px; max-height: 30vh; overflow: auto; padding: 8px 0; overflow-wrap: anywhere; }
@media (min-width: 640px) { .fd-mine .fd-sum4 { grid-template-columns: repeat(4,minmax(0,1fr)); } }
@media (min-width: 1024px) {
  .fd-mine { margin-top: -4px; }
  .fd-mine .fd-mine-hero { padding: 16px; }
}
@media (max-width: 639px) {
  .fd-mine .fd-sum4 { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .fd-mine .fd-self-row { flex-wrap: wrap; gap: 4px; }
  .fd-mine .fd-self-row p { flex: 1 1 100%; flex-wrap: wrap; }
  .fd-mine .fd-self-actions { margin-left: auto; }
  .fd-mine .fd-mf, .fd-mine .fd-node { max-width: 110px; }
  .fd-mine .fd-stat3 > div { padding: 12px; }
}
</style>
