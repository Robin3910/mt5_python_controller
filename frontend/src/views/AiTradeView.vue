<script setup lang="ts">
// AI 交易（跟单）页：左侧 AI 机器人广场，右侧 AI 机器人详情 / 我的 AI 跟单
//
// 能力语义：
//   - 跟单员 = 所有登录用户（assignable 菜单，DEFAULT_USER_MENUS 自动包含）
//   - 机器人主人 = 同时持有 ai_strategy 与 follow_leader 两个能力（管理员自动放行）
//   - 分组级开关 = accept_ai_follow（默认关，A 加仓代价远高于普通带单）
//
// 与 FollowView 的关键差异：
//   1. 手数不可缩放（决策：跟单员不控手数），所以广场卡片第一眼就要展示风险上限
//   2. AI 智能加仓策略不支持信号并发 — 同节点同时只跑一单（已在 D1 写明）
//   3. tpl_1 有热推（M2）— 机器人主人改参数会**实时**作用到跟单员在跑任务
//   4. 移出默认不停止在跑任务（决策 = 与带单一致，由 node_client 跑完）
//
// 跟单员收不到实时 WS 事件（M3）—— 用 45s 轮询替代。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import StrategyFormModal from '@/components/StrategyFormModal.vue'
import AiFollowNodesPanel from '@/components/AiFollowNodesPanel.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import {
  fetchAiRobotDetail,
  fetchAiFollowNodes,
  setAiFollowNodeState,
  closeAiFollowNodes,
  fetchAiRobots,
  fetchMyAiFollowings,
  joinAiFollow,
  leaveAiFollow,
  setAiFollowPaused,
} from '@/api/aiFollow'
import type {
  AiMyGroup,
  AiFollowNodeState,
  AiFollowNodesResponse,
  AiMyRobot,
  AiRobotDetailResponse,
  AiRobotSummary,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import api from '@/api/client'
import type { FollowChannelsOut, Position } from '@/api/types'

const robots = ref<AiRobotSummary[]>([])
const loading = ref(false)
const robotsError = ref('')
const keyword = ref('')

const auth = useAuthStore()
const hub = useHubStore()
const router = useRouter()

/** 绩效统计窗口（四档，与后端 deal_stats.WINDOWS 对齐） */
const WINDOW_OPTIONS = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: '90d', label: '近 90 天' },
  { value: 'all', label: '全部' },
] as const
const perfWindow = ref<string>('30d')

const detail = ref<AiRobotDetailResponse | null>(null)
const detailLoading = ref(false)

const mine = ref({ following: [], summary: {} } as {
  following: AiMyRobot[]
  summary: Record<string, number>
})
const mineLoading = ref(false)
const nodeStats = ref<AiFollowNodesResponse | null>(null)
const nodeStatsLoading = ref(false)
const nodeStatsError = ref('')
const nodeStateBusy = ref(false)
let pollTimer: number | undefined

const selectedGroupIds = ref<string[]>([])
const selectedNodeIds = ref<string[]>([])
const submitting = ref(false)
const strategyFormVisible = ref(false)
const editingStrategyId = ref('')

function openStrategyEditor(robot: AiRobotSummary): void {
  if (!robot.editable_strategy_id) return
  editingStrategyId.value = robot.editable_strategy_id
  strategyFormVisible.value = true
}

/** 当前可加入的分组 ID 集合（详情页按它筛） */
const selectableGroups = computed(() =>
  (detail.value?.groups ?? []).filter((g) => g.selectable),
)
const selectableNodeIds = computed<Set<string>>(
  () => new Set((detail.value?.my_nodes ?? []).map((n) => n.node_id)),
)

/** 自动选所有可加入分组（首次打开时方便一键跟单） */
const allSelectableGroupIds = computed<string[]>(() =>
  selectableGroups.value.map((g) => g.group_id),
)

interface MyFollowedNode {
  node_id: string
  name: string | null
  mt5_login: number | null
  joined_at: number | null
  active_task_count: number
  batch_limit: number
  /** 当前机器人分组内的跟单关系 */
  groups: { group_id: string; group_name: string | null }[]
}

interface MyRobotEntry {
  is_owner: boolean
  robot: AiRobotSummary
  marketable: boolean
  groups: AiMyGroup[]
  /** 聚合后每个节点的详情（去重），用于「我的 AI 跟单」区 */
  nodes: MyFollowedNode[]
}

const myFollowing = computed<MyRobotEntry[]>(() =>
  mine.value.following.map((r) => {
    const groups = r.groups || []
    const nodesById = new Map<string, MyFollowedNode>()
    for (const g of groups) {
      const batchLimit = g.batch_limit || 0
      for (const n of g.my_nodes || []) {
        const prev = nodesById.get(n.node_id)
        if (prev) {
          prev.active_task_count += g.active_task_count
          if (!prev.groups.some((x) => x.group_id === g.group_id)) {
            prev.groups.push({ group_id: g.group_id, group_name: g.group_name ?? null })
          }
          if (batchLimit > prev.batch_limit) prev.batch_limit = batchLimit
          if (n.joined_at != null && (prev.joined_at == null || n.joined_at < prev.joined_at)) {
            prev.joined_at = n.joined_at
          }
        } else {
          nodesById.set(n.node_id, {
            node_id: n.node_id,
            name: n.name ?? null,
            mt5_login: n.mt5_login ?? null,
            joined_at: n.joined_at ?? null,
            active_task_count: g.active_task_count,
            batch_limit: batchLimit,
            groups: [{ group_id: g.group_id, group_name: g.group_name ?? null }],
          })
        }
      }
    }
    return {
      is_owner: r.is_owner,
      robot: r.robot,
      marketable: r.robot.marketable,
      groups,
      nodes: [...nodesById.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
    }
  }),
)

/**
 * 当前机器人分组中该节点的跟单关系，不跨策略移出。
 */
const followNodeCount = computed(() => nodeStats.value?.summary.node_count ?? new Set(myFollowing.value.flatMap((entry) => entry.nodes.map((node) => node.node_id))).size)

function findGroupIdsForNode(robot: AiRobotSummary, nodeId: string): string[] {
  const entry = myFollowing.value.find((entry) => entry.robot.group_id === robot.group_id)
  const node = entry?.nodes.find((n) => n.node_id === nodeId)
  if (!node) return []
  return node.groups.map((g) => g.group_id)
}

/**
 * 已跟过的节点（**按分组维度**判定）。
 *
 * 不能用「我的 AI 跟单」的全局聚合：那里面混着两个市场的关系
 * （客户跟了主人的 tpl_2 开仓策略分组，跟了 AI 分组是两回事）。
 * 客户只跟了开仓策略分组时，AI 分组里并不存在该节点的关系，
 * 这里显示「已跟过」是错的 —— 会误导客户以为已经跟了 AI。
 *
 * 口径 = **将要加入的分组**（已选 ∩ 可选）的 my_follow_node_ids 并集：
 * 跟单关系是 (group_id, node_id) 维度的（uq_follow_group_node），
 * 同一节点跟了 A 组可以再跟 B 组，所以「是否已跟过」必须落到具体分组上。
 *
 * **暂停期间**分组全部不可选，上面那个口径会退化成空集 —— 已有的跟单员
 * 会突然看不到「已跟」标记，像是关系被清掉了。暂停不动存量关系，
 * 所以这里改用「该机器人名下全部分组」来判定，保持标记如实显示。
 */
const alreadyFollowingNodeIds = computed<Set<string>>(() => {
  const ids = new Set<string>()
  if (!detail.value) return ids
  const selected = new Set(selectedGroupIds.value)
  for (const g of detail.value.groups ?? []) {
    if (!detailPaused.value && !selected.has(g.group_id)) continue
    for (const nid of g.my_follow_node_ids ?? []) ids.add(nid)
  }
  return ids
})

// ---------- 显示工具 ----------
function errText(e: unknown, fallback: string): string {
  const ax = e as { response?: { data?: { detail?: string } } }
  return ax?.response?.data?.detail || fallback
}

/**
 * 机器人的展示标题 = **策略名称**。
 *
 * 「机器人在跑什么策略」才是用户识别一个机器人的依据；
 * 用户名 / 主节点名 / MT5 账号是主人的账户信息，不是机器人的身份，
 * 放在标题位置会让人对不上号（同一主人可能挂着多个机器人）。
 *
 * 策略名取不到（分组未绑策略 / 策略被删除）时回落到用户名 ——
 * 总比一个空白标题强。
 */
function robotTitle(robot: AiRobotSummary): string {
  return robot.strategy_name || robot.display_name || robot.username
}

/** 副标题：策略名有多个时提示「还有 N 个策略」 */
function robotStrategyExtra(robot: AiRobotSummary): string {
  const n = robot.strategy_names?.length ?? 0
  return n > 1 ? `等 ${n} 个策略` : ''
}

function primaryText(robot: AiRobotSummary): string {
  const n = robot.primary_node
  if (!n) return '无主节点'
  return `${n.name ?? n.node_id} · MT5 ${n.mt5_login ?? '?'}`
}

// ---------- 绩效（与 FollowView 同一套口径与展示）----------
/** 绩效是否有真实成交数据（false = 节点没上线/同步未开，不是「赚 0」） */
function perfOf(robot: AiRobotSummary): AiRobotSummary['performance'] {
  return robot.performance
}
function hasPerf(robot: AiRobotSummary): boolean {
  return !!robot.performance?.data_available
}

/** 跟单人数：优先后端算好的，回落到全量关系数 */
function followerCountOf(robot: AiRobotSummary): number {
  return robot.follower_count || robot.follower_node_count || 0
}

/**
 * 手机端紧凑日期。
 *
 * 表格里的 `toLocaleString` 时间戳在窄屏必爆，只取到日就够看。
 */
function fmtJoinedShort(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return sameYear
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
}

/**
 * 节点当前是否**可勾选**。
 *
 * 三个不可选原因：主节点（不可被跟单）、「已跟过该机器人的分组」、
 * 主人已暂停 AI 跟单（整体收口）。
 * 模板里到处要用这个判定（高亮 / 置灰 / aria 状态 / tabindex），
 * 各写一遍很容易走偏 —— 统一收敛到这里。
 */
function isNodeSelectable(nodeId: string): boolean {
  if (detailPaused.value) return false
  return selectableNodeIds.value.has(nodeId) && !alreadyFollowingNodeIds.value.has(nodeId)
}

// ---------- 加载 ----------
async function loadRobots() {
  loading.value = true
  robotsError.value = ''
  try {
    const res = await fetchAiRobots(keyword.value.trim(), perfWindow.value)
    robots.value = res.robots
  } catch (e) {
    robotsError.value = errText(e, '加载机器人信息失败')
    ElMessage.error(robotsError.value)
  } finally {
    loading.value = false
  }
}

async function loadMine() {
  mineLoading.value = true
  try {
    mine.value = await fetchMyAiFollowings()
  } catch (e) {
    ElMessage.error(errText(e, '加载我的 AI 跟单失败'))
  } finally {
    mineLoading.value = false
  }
}

async function openRobot(r: AiRobotSummary) {
  detailLoading.value = true
  selectedGroupIds.value = []
  selectedNodeIds.value = []
  try {
    detail.value = await fetchAiRobotDetail(r.leader_user_id, perfWindow.value, r.group_id)
    selectedGroupIds.value = allSelectableGroupIds.value
  } catch (e) {
    detail.value = null
    ElMessage.error(errText(e, '加载机器人详情失败'))
  } finally {
    detailLoading.value = false
  }
}

async function loadNodeStats(): Promise<void> {
  nodeStatsLoading.value = true
  try {
    nodeStats.value = await fetchAiFollowNodes()
    nodeStatsError.value = ''
  } catch (cause) {
    nodeStatsError.value = errText(cause, '加载子节点统计失败')
  } finally { nodeStatsLoading.value = false }
}

// ---------------------------------------------------------------------------
// 暂停 AI 跟单（我的机器人才有的开关）
// ---------------------------------------------------------------------------
//
// 「暂停」= 主人名下 AI 分组停止新开仓，但**不动**已有的跟单关系与在跑任务：
//   ① 新客户无法再加入该机器人的 AI 分组（服务端写侧拦截，前端只做提示）；
//   ② 已有跟单员与在跑任务**保留**，等他们自行移出；
//   ③ 分组所有节点（含主节点）不再接收新开仓，CLOSE / 平仓仍可执行。
//
// 与【带单】页的「暂停带单」是**两个独立开关**：
// 一个主人可能同时做普通带单与 AI 机器人，停一边不该连累另一边。
// 服务端按分组绑定的策略模版决定取用哪个标志（tpl_1 -> AI，tpl_2/tpl_3 -> 带单）。

/** 暂停操作进行中（防止重复点击） */
const pauseBusy = ref(false)

/** 当前详情页是否处于「已暂停 AI 跟单」状态 */
const detailPaused = computed<boolean>(() => detail.value?.robot.ai_follow_paused === true)

/**
 * 暂停提示文案。
 *
 * 与后端 `follow_rules.PAUSED_AI_REASON` 保持同一句 —— 前端写侧兜底提示与
 * 服务端 400 的 detail 长得不一样时，客户会以为是两个问题。
 */
const PAUSED_AI_HINT = '该机器人主人已暂停 AI 跟单，不再接收新的跟单'

/** 这个机器人是不是我自己的（主人才能暂停） */
function isMyRobot(robot: AiRobotSummary): boolean {
  const myId = auth.me?.user_id
  return myId != null && robot.leader_user_id === myId
}

/**
 * 切换暂停状态。
 *
 * 暂停是对外接单的动作 —— 跟单员点进来会看到「已暂停」，
 * 所以必须二次确认，把影响说清楚再执行。
 */
async function togglePause(robot: AiRobotSummary) {
  const myId = auth.me?.user_id
  // 只有本人才能改这个开关 —— 服务端也会再校验一次。
  if (myId == null || robot.leader_user_id !== myId) return

  const next = !robot.ai_follow_paused
  const name = robot.display_name || robot.username
  try {
    if (next) {
      await ElMessageBox.confirm(
        `暂停后，${name} 名下所有 AI 分组将停止接收新的开仓信号，并停止接收新的 AI 跟单申请。\n\n` +
          `· 已有跟单关系与在跑任务**保留**，等跟单员自行移出\n` +
          `· 分组所有节点（包括主节点、自有成员和跟单节点）均暂停新开仓\n` +
          `· 不自动平仓，已有任务继续运行，平仓 / CLOSE 仍可执行\n` +
          `· 只影响 AI 智能加仓分组，不影响普通带单（趋势 / 网格）\n\n` +
          `确认暂停 AI 机器人吗？`,
        '暂停 AI 机器人',
        { type: 'warning', confirmButtonText: '确认暂停', cancelButtonText: '再想想' },
      )
    } else {
      await ElMessageBox.confirm(
        `恢复后，${name} 名下所有 AI 分组将恢复接收新的开仓信号，并重新接受新的 AI 跟单申请。`,
        '恢复 AI 机器人',
        { type: 'info', confirmButtonText: '确认恢复', cancelButtonText: '取消' },
      )
    }
  } catch {
    // 用户取消
    return
  }

  pauseBusy.value = true
  try {
    const res = await setAiFollowPaused({ leader_user_id: robot.leader_user_id, paused: next })
    // 就地更新：详情页与列表共用同一份摘要，避免整页重拉。
    if (detail.value?.robot.leader_user_id === robot.leader_user_id) {
      detail.value.robot.ai_follow_paused = res.ai_follow_paused
    }
    for (const item of robots.value) {
      if (item.leader_user_id === robot.leader_user_id) item.ai_follow_paused = res.ai_follow_paused
    }
    for (const item of mine.value.following) {
      if (item.robot.leader_user_id === robot.leader_user_id) item.robot.ai_follow_paused = res.ai_follow_paused
    }
    ElMessage.success(res.ai_follow_paused ? '已暂停 AI 机器人' : '已恢复 AI 机器人')
    // 列表与「我的 AI 跟单」的不可跟单标记依赖服务端口径，重拉一次最保险。
    await refreshAll()
  } catch (e) {
    ElMessage.error(errText(e, next ? '暂停 AI 机器人失败' : '恢复 AI 机器人失败'))
  } finally {
    pauseBusy.value = false
  }
}

async function refreshAll() {
  await Promise.all([loadRobots(), loadMine(), loadNodeStats()])
}

// 统计窗口切换：重拉列表 + 当前详情（绩效是纯读缓存，切换零延迟）。
// 不重拉「我的 AI 跟单」—— 它不含绩效，没必要跟着抖一下。
watch(perfWindow, () => {
  void loadRobots()
  if (detail.value) {
    void openRobot(detail.value.robot)
  }
})

onMounted(() => {
  void refreshAll()
  // M3 跟单员收不到 WS 事件 —— 用 45s 轮询替代
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void refreshAll()
  }, 45_000)
})

onUnmounted(() => {
  if (pollTimer != null) window.clearInterval(pollTimer)
})

// ---------- 加入 / 移出 ----------
async function submitJoin() {
  if (!detail.value) return
  // 暂停判定放在最前面：暂停时所有分组都不可选，下面「没有可加入分组」的分支
  // 会先一步返回，跟单员就看不到任何原因提示了 —— 按钮虽然已置灰，
  // 但置灰的**理由**必须说清楚，不能让人猜。
  if (detailPaused.value) {
    ElMessage.warning(PAUSED_AI_HINT)
    return
  }
  const groupIds = selectedGroupIds.value.filter((id) =>
    selectableGroups.value.some((g) => g.group_id === id),
  )
  if (!groupIds.length) {
    ElMessage.warning('请选择至少一个可加入的分组')
    return
  }
  // 只提交「现在仍然可选」的节点。
// 已跟过的节点虽然置灰禁用了，但 selectedNodeIds 里可能还留着它 —— 勾选是
// 分组切换之前的事（先勾节点，再勾一个它已跟过的分组，那个勾就失效了）。
// 这里再按禁用条件过滤一次，提交侧就不依赖 UI 有没有正确置灰。
const nodeIds = (detail.value.my_nodes ?? [])
  .filter((n) => selectableNodeIds.value.has(n.node_id))
  .map((n) => n.node_id)
  .filter((nid) => selectedNodeIds.value.includes(nid))
  .filter((nid) => !alreadyFollowingNodeIds.value.has(nid))
  if (!nodeIds.length) {
    ElMessage.warning('请选择至少一个可用节点（主节点不可跟单）')
    return
  }
  // 预聚合勾选里混进了已跟过的节点（用户先勾后切分组导致）—— 告诉用户被剔除了哪些，
  // 而不是静默丢弃：静默少跟一个节点，用户事后对不上账。
  const dropped = (detail.value.my_nodes ?? [])
    .map((n) => n.node_id)
    .filter((nid) => selectedNodeIds.value.includes(nid) && alreadyFollowingNodeIds.value.has(nid))
  if (dropped.length) {
    if (!(await new Promise<boolean>((resolve) => {
      ElMessageBox.confirm(
        `所选节点中 ${dropped.length} 个已跟过该机器人的分组，本次将自动跳过。是否继续？`,
        '部分节点已跟',
        { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' },
      )
        .then(() => resolve(true))
        .catch(() => resolve(false))
    }))) return
  }
  if (!(await new Promise<boolean>((resolve) => {
    ElMessageBox.confirm(
      '确认跟单该 AI 机器人？',
      '确认跟单',
      {
        type: 'warning',
        confirmButtonText: '确认跟单',
        cancelButtonText: '取消',
        customClass: 'ai-join-confirm',
      },
    )
      .then(() => resolve(true))
      .catch(() => resolve(false))
  }))) return
  submitting.value = true
  try {
    const res = await joinAiFollow({
      leader_user_id: detail.value.robot.leader_user_id,
      group_ids: groupIds,
      node_ids: nodeIds,
    })
    ElMessage.success(`已加入 ${res.joined.length} 个分组`)
    await refreshAll()
    detail.value = await fetchAiRobotDetail(detail.value.robot.leader_user_id, perfWindow.value, detail.value.robot.group_id)
    selectedGroupIds.value = allSelectableGroupIds.value
    pickerOpen.value = false
  } catch (e) {
    ElMessage.error(errText(e, '加入 AI 跟单失败'))
  } finally {
    submitting.value = false
  }
}

async function changeNodeFollowState(nodeIds: string[], state: AiFollowNodeState): Promise<void> {
  if (!nodeIds.length || nodeStateBusy.value || closeBusy.value) return
  const action = { running: '恢复跟单', paused: '暂停跟单', stopped: '停止跟单' }[state]
  try {
    await ElMessageBox.confirm(
      `确认对 ${nodeIds.length} 个子节点${action}？\n\n仅作用于这些节点的 AI 跟单，不影响主节点、其他节点或普通带单。\n${state === 'running' ? '恢复后接收后续新开仓，停止的关系需重新跟单。' : '停止接收新开仓；已有任务与持仓继续运行，平仓 / CLOSE 仍可执行。'}${state === 'stopped' ? '\n停止记录保留，可重新跟单。' : ''}`,
      action, { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch { return }
  nodeStateBusy.value = true
  try {
    await setAiFollowNodeState(nodeIds, state)
    ElMessage.success(`已${action}`)
    await refreshAll()
  } catch (cause) {
    ElMessage.error(errText(cause, `${action}失败`))
  } finally { nodeStateBusy.value = false }
}

async function closeNodeFollowPositions(nodeIds: string[]): Promise<void> {
  if (!nodeIds.length || nodeStateBusy.value || closeBusy.value) return
  try {
    await ElMessageBox.confirm(
      `确认平掉 ${nodeIds.length} 个子节点上的 AI 跟单持仓？\n\n包含已停止节点保留的 AI 跟单持仓，仅处理这些节点当前关联的 AI 分组，不影响手工单、其他策略或普通带单。`,
      '一键平仓', { type: 'warning', confirmButtonText: '确认平仓', cancelButtonText: '取消' },
    )
  } catch { return }
  closeBusy.value = true
  try {
    const result = await closeAiFollowNodes(nodeIds)
    if (result.failed.length) ElMessage.warning(`已提交 ${result.submitted} 笔平仓，${result.failed.length} 个节点未完成：${result.failed[0].detail}`)
    else ElMessage.success(result.submitted ? `已提交 ${result.submitted} 笔 AI 跟单平仓` : '当前没有可平仓的 AI 跟单持仓')
    await Promise.all(nodeIds.map((nodeId) => hub.fetchNodeAccount(nodeId).catch(() => {})))
    await refreshAll()
  } catch (cause) {
    ElMessage.error(errText(cause, '子节点平仓失败'))
  } finally { closeBusy.value = false }
}

const rankOn = ref(false)
const onlineRobotCount = computed(() =>
  robots.value.filter((r) => r.primary_node?.online || r.primary_node?.status === 'online').length,
)
const mineTab = ref<'run' | 'hist' | 'nodes'>('run')
const pickerOpen = ref(false)
const pickerMode = ref<'join' | 'settings'>('join')

const shownRobots = computed(() => {
  const list = [...robots.value]
  if (!rankOn.value) return list
  return list.sort((a, b) => (b.performance?.total_profit || 0) - (a.performance?.total_profit || 0))
})
const runningMine = computed(() =>
  myFollowing.value.filter((entry) => entry.is_owner || entry.marketable || entry.nodes.some((node) => node.active_task_count > 0)),
)
const historyMine = computed(() =>
  myFollowing.value.filter((entry) => !entry.is_owner && !entry.marketable && entry.nodes.every((node) => node.active_task_count === 0)),
)

interface MagicHit { magic: number; group_id: string }
const aiMagics = ref<Record<string, MagicHit[]>>({})

async function loadAiBooks(nodeIds: string[]) {
  await Promise.all(nodeIds.map(async (id) => {
    if (!hub.accounts[id]) {
      try { await hub.fetchNodeAccount(id) } catch { /* 快照没有时持仓区留空 */ }
    }
    try {
      const { data } = await api.get<FollowChannelsOut>(`/api/nodes/${id}/follow-channels`)
      aiMagics.value = {
        ...aiMagics.value,
        [id]: data.magics.filter((m) => m.market === 'ai').map((m) => ({ magic: m.magic, group_id: m.group_id })),
      }
    } catch { /* 频道接口失败时这台节点不展示持仓 */ }
  }))
}

function robotNodeIds(entry: MyRobotEntry): string[] {
  const ids = entry.nodes.map((node) => node.node_id)
  const primaryId = entry.robot.primary_node?.node_id
  if (entry.is_owner && primaryId) ids.push(primaryId)
  return [...new Set(ids)]
}

watch(myFollowing, (list) => {
  const ids = [...new Set(list.flatMap(robotNodeIds))]
  if (ids.length) void loadAiBooks(ids)
}, { immediate: true })

function robotPositions(entry: MyRobotEntry): Array<Position & { nodeId: string }> {
  const groups = new Set(entry.groups.map((g) => g.group_id))
  const rows: Array<Position & { nodeId: string }> = []
  for (const nodeId of robotNodeIds(entry)) {
    const hits = (aiMagics.value[nodeId] || []).filter((m) => !groups.size || groups.has(m.group_id))
    const magics = new Set(hits.map((m) => m.magic))
    for (const position of hub.accounts[nodeId]?.positions || []) {
      if (magics.has(position.magic)) rows.push({ ...position, nodeId })
    }
  }
  return rows
}
function bookStats(rows: Array<Position & { nodeId: string }>) {
  let buy = 0
  let sell = 0
  let volume = 0
  let profit = 0
  for (const p of rows) {
    if ((p.type || '').toLowerCase() === 'buy') buy += 1
    else sell += 1
    volume += p.volume || 0
    profit += p.profit || 0
  }
  return { buy, sell, volume, profit, count: rows.length }
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

async function openPicker(robot: AiRobotSummary, mode: 'join' | 'settings') {
  pickerMode.value = mode
  await openRobot(robot)
  if (!detail.value) return
  selectedNodeIds.value = mode === 'settings' ? [...alreadyFollowingNodeIds.value] : []
  const ids = (detail.value.my_nodes || []).map((n) => n.node_id)
  void loadAiBooks(ids)
  pickerOpen.value = true
}
function togglePick(nodeId: string) {
  if (pickerMode.value === 'join' && !isNodeSelectable(nodeId)) return
  const next = new Set(selectedNodeIds.value)
  next.has(nodeId) ? next.delete(nodeId) : next.add(nodeId)
  selectedNodeIds.value = [...next]
}
async function submitPicker() {
  if (pickerMode.value === 'join') {
    await submitJoin()
    return
  }
  if (!detail.value) return
  const robot = detail.value.robot
  const already = alreadyFollowingNodeIds.value
  const selected = new Set(selectedNodeIds.value)
  const add = [...selected].filter((id) => !already.has(id) && selectableNodeIds.value.has(id))
  const remove = [...already].filter((id) => !selected.has(id))
  if (!add.length && !remove.length) { pickerOpen.value = false; return }
  if (detailPaused.value && add.length) { ElMessage.warning(PAUSED_AI_HINT); return }
  try {
    await ElMessageBox.confirm('确认更新跟单节点？新增的节点开始跟随，取消的节点移出跟单。', '设置跟单', {
      type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消',
    })
  } catch { return }
  submitting.value = true
  try {
    if (add.length && allSelectableGroupIds.value.length) {
      await joinAiFollow({
        leader_user_id: robot.leader_user_id,
        group_ids: allSelectableGroupIds.value,
        node_ids: add,
      })
    }
    for (const nodeId of remove) {
      const groupIds = findGroupIdsForNode(robot, nodeId)
      if (!groupIds.length) continue
      await leaveAiFollow({
        leader_user_id: robot.leader_user_id,
        group_ids: groupIds,
        node_ids: [nodeId],
        close_tasks: false,
      })
    }
    ElMessage.success('已更新跟单节点')
    pickerOpen.value = false
    await refreshAll()
  } catch (e) {
    ElMessage.error(errText(e, '更新跟单节点失败'))
  } finally {
    submitting.value = false
  }
}

const closeBusy = ref(false)
async function closeRobotTicket(nodeId: string, ticket: number) {
  if (closeBusy.value) return
  try {
    await ElMessageBox.confirm('确认平掉这张持仓？', '平仓', { type: 'warning', confirmButtonText: '平仓', cancelButtonText: '取消' })
  } catch { return }
  closeBusy.value = true
  try {
    await api.post(`/api/nodes/${nodeId}/follow-channels/close`, { market: 'ai', mode: 'ticket', ticket })
    ElMessage.success('已提交平仓')
    await hub.fetchNodeAccount(nodeId)
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally { closeBusy.value = false }
}
async function closeRobotAll(entry: MyRobotEntry) {
  const rows = robotPositions(entry)
  if (!rows.length) { ElMessage.info('没有可平的持仓'); return }
  try {
    await ElMessageBox.confirm(`确认平掉「${robotTitle(entry.robot)}」在你节点上的 ${rows.length} 笔 AI 持仓？`, '一键平仓', {
      type: 'warning', confirmButtonText: '确认平仓', cancelButtonText: '取消',
    })
  } catch { return }
  closeBusy.value = true
  try {
    for (const row of rows) {
      await api.post(`/api/nodes/${row.nodeId}/follow-channels/close`, { market: 'ai', mode: 'ticket', ticket: row.ticket })
    }
    ElMessage.success('已提交平仓')
    await Promise.all(robotNodeIds(entry).map((nodeId) => hub.fetchNodeAccount(nodeId).catch(() => {})))
  } catch (e) {
    ElMessage.error(errText(e, '平仓失败'))
  } finally { closeBusy.value = false }
}

onMounted(() => { void hub.fetchNodes() })
</script>

<template>
  <div class="ai">
    <section class="ai-hero">
      <div class="ai-hero-top">
        <span class="ai-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm7 4H8v-2h8v2zm-1-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>
        </span>
        <div class="ai-hero-text">
          <p>AI 智能交易机器人</p>
          <p>策略自动化运行 · 7×24 小时监控</p>
        </div>
        <div class="ai-hero-side">
          <span class="ai-online"><i></i>在线 {{ onlineRobotCount }} 台</span>
          <button type="button" class="ai-rank" :class="{ on: rankOn }" @click="rankOn = !rankOn">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M7.5 21H2V9h5.5v12zm7.25-18h-5.5v18h5.5V3zM22 11h-5.5v10H22V11z"/></svg>
            榜单
          </button>
        </div>
      </div>

      <div class="ai-sec">
        <p class="ai-kicker">机器人信息<span>{{ shownRobots.length }}</span></p>
        <div v-if="rankOn || keyword || perfWindow !== '30d'" class="ai-tools">
          <input v-model="keyword" placeholder="搜索名称" @keyup.enter="loadRobots" />
          <button v-for="opt in WINDOW_OPTIONS" :key="opt.value" type="button" :class="{ on: perfWindow === opt.value }" @click="perfWindow = opt.value">{{ opt.label }}</button>
        </div>
        <p v-if="loading && !robots.length" class="ai-empty">加载中…</p>
        <div v-else-if="robotsError" class="ai-empty" role="alert">
          <p>{{ robotsError }}</p>
          <button type="button" class="ai-refresh" @click="loadRobots">重试</button>
        </div>
        <div v-else-if="!shownRobots.length" class="ai-empty">
          <p>{{ keyword.trim() ? '未找到匹配的 AI 机器人' : '暂无开放的 AI 机器人' }}</p>
          <p v-if="!keyword.trim()" class="ai-empty-hint">机器人主人需在策略分组中开启「开放 AI 跟单」，机器人才能在这里展示。</p>
        </div>
        <div v-else class="ai-list">
          <div v-for="r in shownRobots" :key="r.group_id" class="ai-row">
            <span class="ai-ico" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm7 4H8v-2h8v2zm-1-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>
            </span>
            <div class="ai-row-text">
              <div class="ai-row-title">
                <p>{{ robotTitle(r) }}</p>
                <span>{{ r.strategy_name || robotStrategyExtra(r) || 'AI智能交易' }}</span>
              </div>
              <p class="ai-desc">{{ r.strategy_names?.slice(0, 2).join(' · ') || primaryText(r) }}<template v-if="!r.marketable"> · 已下架</template><template v-if="r.ai_follow_paused"> · 已暂停</template></p>
              <p class="ai-run"><i></i>{{ followerCountOf(r).toLocaleString('en-US') }} 个节点跟随</p>
            </div>
            <div class="ai-row-actions">
              <button type="button" class="ai-follow" :disabled="!r.marketable || r.ai_follow_paused" @click="openPicker(r, 'join')">
                <svg viewBox="0 0 24 24"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>
                跟单
              </button>
              <button v-if="r.editable_strategy_id" type="button" class="ai-set" @click="openStrategyEditor(r)">
                <svg viewBox="0 0 24 24"><path fill="currentColor" d="M19.14 12.94a7.43 7.43 0 0 0 .05-.94 7.43 7.43 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.28 7.28 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54a7.28 7.28 0 0 0-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.71 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.43 7.43 0 0 0-.05.94c0 .32.02.63.05.94L2.83 14.5a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .6.22l2.39-.96c.5.39 1.05.71 1.63.94l.36 2.54a.5.5 0 0 0 .5.42h3.84a.5.5 0 0 0 .5-.42l.36-2.54c.58-.23 1.13-.55 1.63-.94l2.39.96a.5.5 0 0 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58zM12 15.5A3.5 3.5 0 1 1 15.5 12 3.5 3.5 0 0 1 12 15.5z"/></svg>
                设置
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="ai-mine-wrap">
      <div class="ai-mine-head">
        <p class="ai-kicker">我的机器人<span>{{ mineTab === 'nodes' ? followNodeCount : myFollowing.length }}</span></p>
        <button type="button" class="ai-refresh" @click="refreshAll">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7a5 5 0 1 1-5 5H5a7 7 0 1 0 12.65-5.65z"/></svg>
          刷新
        </button>
      </div>
      <div class="ai-tabs">
        <button type="button" :class="{ on: mineTab === 'run' }" @click="mineTab = 'run'">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm7 4H8v-2h8v2zm-1-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>
          运行中<span>{{ runningMine.length }}</span>
        </button>
        <button type="button" :class="{ on: mineTab === 'hist' }" @click="mineTab = 'hist'">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M13 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-2.05-4.95L16 9h5V4l-1.64 1.64A8.96 8.96 0 0 0 13 3zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8z"/></svg>
          历史机器人<span>{{ historyMine.length }}</span>
        </button>
        <button type="button" class="push" :class="{ on: mineTab === 'nodes' }" @click="mineTab = 'nodes'">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z"/></svg>
          子节点跟单统计<span>{{ followNodeCount }}</span>
        </button>
      </div>

      <div v-if="mineTab === 'run'" class="ai-cards">
        <p v-if="mineLoading && !runningMine.length" class="ai-empty">加载中…</p>
        <p v-else-if="!runningMine.length" class="ai-empty">还没有正在跟随的机器人</p>
        <article v-for="entry in runningMine" :key="entry.robot.group_id" class="ai-card">
          <div class="ai-card-top">
            <div class="ai-card-heading">
              <span class="ai-ico" aria-hidden="true"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 9V7a2 2 0 0 0-2-2h-3a3 3 0 0 0-6 0H6a2 2 0 0 0-2 2v2a3 3 0 0 0 0 6v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a3 3 0 0 0 0-6zM9 13a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm7 4H8v-2h8v2zm-1-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg></span>
              <div>
                <p>{{ robotTitle(entry.robot) }}</p>
                <p>{{ entry.groups.map((g) => g.symbol || g.group_name).filter(Boolean).join(' · ') || '—' }}<template v-if="entry.nodes[0]?.joined_at"> · 开始于 {{ fmtJoinedShort(entry.nodes[0].joined_at) }}</template></p>
              </div>
            </div>
            <em :class="entry.robot.ai_follow_paused ? 'off' : 'on'"><i></i>{{ entry.robot.ai_follow_paused ? '已暂停' : '运行中' }}</em>
          </div>
          <div class="ai-stats">
            <div><span>总浮动盈亏</span><strong :class="bookStats(robotPositions(entry)).profit >= 0 ? 'up' : 'down'">{{ fmtU(bookStats(robotPositions(entry)).profit) }} U</strong></div>
            <div><span>持仓笔数</span><strong>{{ bookStats(robotPositions(entry)).count }} 笔</strong></div>
            <div><span>合计手数</span><strong>{{ bookStats(robotPositions(entry)).volume.toFixed(2) }} 手</strong></div>
            <div><span>投资额</span><strong>—</strong></div>
            <div><span>总利润</span><strong :class="hasPerf(entry.robot) ? (perfOf(entry.robot).total_profit >= 0 ? 'up' : 'down') : ''">{{ hasPerf(entry.robot) ? fmtU(perfOf(entry.robot).total_profit) : '—' }}</strong></div>
            <div><span>月化收益</span><strong>—</strong></div>
          </div>
          <div class="ai-card-actions">
            <button type="button" class="link" @click="router.push({ name: 'ai-robot-detail', params: { leaderUserId: entry.robot.leader_user_id, groupId: entry.robot.group_id } })">查看详情<svg viewBox="0 0 24 24"><path fill="currentColor" d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>
            <div>
              <button v-if="isMyRobot(entry.robot) && entry.marketable" type="button" class="amber" :disabled="pauseBusy" @click="togglePause(entry.robot)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" :d="entry.robot.ai_follow_paused ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-2 15V7l7 5-7 5z' : 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 14H8V8h3v8zm5 0h-3V8h3v8z'"/></svg>
                {{ entry.robot.ai_follow_paused ? '启动机器人' : '暂停机器人' }}
              </button>
              <button type="button" class="solid" :disabled="closeBusy" @click="closeRobotAll(entry)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="m11 21 1-7H7L13 3l-1 7h5l-6 11z"/></svg>一键平仓</button>
            </div>
          </div>
          <div v-if="robotPositions(entry).length" class="ai-table">
            <div class="ai-trade-label"><p><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>当前交易</p><span>{{ bookStats(robotPositions(entry)).count }} 笔 · {{ bookStats(robotPositions(entry)).volume.toFixed(2) }} 手</span></div>
            <div class="ai-grid head"><span>#</span><span>方向</span><span>手数</span><span>开仓价</span><span>止盈/止损</span><span>浮动盈亏</span><span>操作</span></div>
            <div v-for="(p, i) in robotPositions(entry)" :key="p.nodeId + '-' + p.ticket" class="ai-grid">
              <span class="dim">{{ i + 1 }}</span>
              <span><i :class="(p.type || '').toLowerCase() === 'buy' ? 'buy' : 'sell'">{{ (p.type || '').toLowerCase() === 'buy' ? '多' : '空' }}</i></span>
              <span>{{ (p.volume || 0).toFixed(2) }}</span>
              <span>{{ fmtPx(p.price_open) }}</span>
              <span class="sltp"><em class="up">{{ fmtPx(p.tp) }}</em><em class="down">{{ fmtPx(p.sl) }}</em></span>
              <strong :class="(p.profit || 0) >= 0 ? 'up' : 'down'">{{ fmtU(p.profit || 0) }}</strong>
              <button type="button" :disabled="closeBusy" @click="closeRobotTicket(p.nodeId, p.ticket)">平仓</button>
            </div>
            <footer><span>多 {{ bookStats(robotPositions(entry)).buy }} / 空 {{ bookStats(robotPositions(entry)).sell }}</span><b :class="bookStats(robotPositions(entry)).profit >= 0 ? 'up' : 'down'">合计 {{ fmtU(bookStats(robotPositions(entry)).profit) }}</b></footer>
          </div>
        </article>
      </div>

      <div v-else-if="mineTab === 'hist'" class="ai-list">
        <p v-if="!historyMine.length" class="ai-empty">没有已结束的跟单</p>
        <div v-for="entry in historyMine" :key="entry.robot.group_id" class="ai-row">
          <span class="ai-ico"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M13 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-2.05-4.95L16 9h5V4l-1.64 1.64A8.96 8.96 0 0 0 13 3z"/></svg></span>
          <div class="ai-row-text">
            <div class="ai-row-title"><p>{{ robotTitle(entry.robot) }}</p></div>
            <p class="ai-desc">{{ entry.robot.unmarketable_reason || '已无进行中的任务' }}</p>
          </div>
        </div>
      </div>

      <AiFollowNodesPanel v-else :data="nodeStats" :loading="nodeStatsLoading" :error="nodeStatsError" :busy="nodeStateBusy || closeBusy" @retry="loadNodeStats" @state="changeNodeFollowState" @close="closeNodeFollowPositions" />
    </section>

    <StrategyFormModal
      v-model="strategyFormVisible"
      mode="edit"
      :strategy-id="editingStrategyId"
      preset-template-id="tpl_1"
      :preset-symbol-locked="true"
      @saved="refreshAll"
    />

    <div v-if="pickerOpen && detail" class="ai-mask" @click.self="pickerOpen = false">
      <section class="ai-dialog" role="dialog" aria-label="选择跟单节点">
        <header>
          <div>
            <strong>选择跟单节点</strong>
            <p>跟单对象：{{ robotTitle(detail.robot) }}</p>
          </div>
          <button type="button" class="ai-x" @click="pickerOpen = false">×</button>
        </header>
        <p v-if="detailPaused" class="ai-warn">{{ PAUSED_AI_HINT }}</p>
        <p v-else-if="detailLoading" class="ai-empty">加载节点…</p>
        <p v-else-if="!(detail.my_nodes || []).length" class="ai-empty">没有可跟单的节点。主节点不能用于跟单。</p>
        <button
          v-for="n in detail.my_nodes"
          :key="n.node_id"
          type="button"
          class="ai-pick"
          :class="{ on: selectedNodeIds.includes(n.node_id), off: pickerMode === 'join' && !isNodeSelectable(n.node_id) }"
          :disabled="pickerMode === 'join' && !isNodeSelectable(n.node_id)"
          @click="togglePick(n.node_id)"
        >
          <span class="box">{{ selectedNodeIds.includes(n.node_id) ? '✓' : '' }}</span>
          <span>
            <b>{{ n.name || n.node_id }} <em v-if="n.is_primary">主节点</em></b>
            <small>{{ n.node_id }} · 余额 {{ nodeBalance(n.node_id) }}</small>
          </span>
        </button>
        <footer>
          <button type="button" @click="pickerOpen = false">取消</button>
          <button type="button" class="primary" :disabled="submitting || (detailPaused && pickerMode === 'join')" @click="submitPicker">
            {{ pickerMode === 'settings' ? '保存设置' : `使用 ${selectedNodeIds.length} 个节点跟单` }}
          </button>
        </footer>
      </section>
    </div>
  </div>
</template>

<style scoped>
:global(.app-shell.is-customer:has(.ai)) { color: #E6EBF4; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif; font-size: 16px; line-height: 1.5; }
:global(.brand-back-ai) { width: 28px; height: 28px; flex-shrink: 0; margin-right: 4px; border-radius: 8px; color: #8B96A8; font-size: 16px; font-weight: 400; line-height: 24px; }
:global(.brand-back-ai svg) { width: 19px; height: 19px; }
:global(.brand-back-ai:hover) { background: #16213A; color: #E6EBF4; }
:global(.app-shell.is-customer:has(.ai) .brand-page) { line-height: 20px; letter-spacing: normal; }
:global(.app-shell.is-customer:has(.ai) .nav-customer a) { line-height: 16px; transition: color .15s, background-color .15s, border-color .15s; }
.ai { display: flex; flex-direction: column; gap: 12px; color: #E6EBF4; font-size: 16px; line-height: 1.5; }
:global(.app-shell.is-customer:has(.ai) .topbar-customer) { height: auto; }
.ai button { line-height: 1.5; backdrop-filter: none; -webkit-backdrop-filter: none; }
.ai-hero {
  border: 1px solid #1C2740; border-radius: 12px; padding: 12px;
  background: linear-gradient(to bottom right, #101828, #0D1424);
  box-shadow: 0 2px 12px rgba(0,0,0,.35);
}
.ai-hero-top, .ai-hero-side, .ai-row, .ai-row-title, .ai-row-actions, .ai-kicker, .ai-mine-head, .ai-tabs, .ai-card-top, .ai-card-actions, .ai-run, .ai-dialog header, .ai-dialog footer, .ai-pick, .ai-node-line {
  display: flex; align-items: center; gap: 8px;
}
.ai-hero-top { gap: 10px; }
.ai-mark, .ai-ico { display: flex; align-items: center; justify-content: center; flex: 0 0 auto; color: #3B82F6; background: rgba(37,99,235,.15); border-radius: 8px; }
.ai-mark { width: 36px; height: 36px; border-radius: 12px; color: #fff; background: linear-gradient(135deg, #2563EB, #7C3AED); box-shadow: 0 2px 10px rgba(99,102,241,.4); }
.ai-ico { width: 32px; height: 32px; }
.ai-mark svg { width: 19px; height: 19px; } .ai-ico svg { width: 15px; height: 15px; } .ai-row-actions svg, .ai-tabs svg { width: 13px; height: 13px; } .ai-refresh svg { width: 12px; height: 12px; }
.ai-hero-text { min-width: 0; }
.ai-hero-text p:first-child { margin: 0; font-size: 14px; font-weight: 700; line-height: 20px; }
.ai-hero-text p:last-child, .ai-desc, .ai-run, .ai-empty, .ai-card-top p:last-child { margin: 2px 0 0; color: #5B6B85; font-size: 10px; }
.ai-hero-text p:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-hero-side { margin-left: auto; flex-shrink: 0; }
.ai-online { display: none; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; border-radius: 999px; padding: 4px 10px; color: #8B96A8; font-size: 10px; }
.ai-online i, .ai-run i, .ai-card-top em i { width: 6px; height: 6px; border-radius: 99px; background: #22C55E; display: inline-block; }
.ai-rank, .ai-follow, .ai-set, .ai-refresh, .ai-tabs button, .ai-card-actions button, .ai-grid button, .ai-dialog button, .ai-node-line button { cursor: pointer; }
.ai-rank { display: flex; align-items: center; gap: 4px; border: 1px solid rgba(245,158,11,.3); background: rgba(245,158,11,.1); color: #F59E0B; border-radius: 8px; padding: 6px 10px; font-size: 11px; font-weight: 700; }
.ai-rank svg { width: 14px; height: 14px; }
.ai-rank.on { background: rgba(245,158,11,.22); }
.ai-sec { margin-top: 14px; }
.ai-sec > .ai-kicker { padding: 0 2px; }
.ai-kicker { gap: 6px; margin: 0; font-size: 12px; font-weight: 600; line-height: 16px; }
.ai-kicker::before { content: ''; width: 2px; height: 12px; border-radius: 2px; background: #2563EB; }
.ai-kicker span { border: 1px solid #22304A; background: #0C1220; color: #5B6B85; border-radius: 999px; padding: 1px 6px; font-size: 10px; font-weight: 500; line-height: 16px; }
.ai-tools { display: flex; gap: 4px; flex-wrap: wrap; margin: 8px 0; }
.ai-tools input, .ai-tools button { height: 26px; border-radius: 8px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; font-size: 11px; }
.ai-tools input { width: 120px; padding: 0 8px; color: #E6EBF4; }
.ai-tools button.on { color: #fff; background: #2563EB; border-color: #2563EB; }
.ai-list { display: flex; flex-direction: column; gap: 6px; }
.ai-sec > .ai-list { margin-top: 8px; }
.ai-row { gap: 10px; border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 8px 10px; }
.ai-row:hover { border-color: rgba(37,99,235,.5); }
.ai-row-text { flex: 1; min-width: 0; }
.ai-row-title { gap: 6px; }
.ai-row-title p { margin: 0; font-size: 12px; font-weight: 700; line-height: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-row-title span { flex: 0 0 auto; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-radius: 4px; background: #16213A; color: #8B96A8; font-size: 9px; padding: 1px 6px; }
.ai-desc, .ai-run { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-desc { line-height: 16px; }
.ai-run { gap: 4px; color: #8B96A8; font-variant-numeric: tabular-nums; }
.ai-run i { width: 4px; height: 4px; flex-shrink: 0; }
.ai-row-actions { flex: 0 0 auto; gap: 6px; }
.ai-follow { display: flex; align-items: center; gap: 4px; border: 0; border-radius: 8px; padding: 6px 12px; color: #fff; font-size: 11px; font-weight: 700; background: linear-gradient(90deg, #2563EB, #1D4ED8); box-shadow: 0 2px 8px rgba(37,99,235,.35); }
.ai-set { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 6px 10px; font-size: 11px; font-weight: 700; }
.ai-follow:disabled { opacity: .4; cursor: not-allowed; }
.ai-follow:hover { background: linear-gradient(90deg, #1D4ED8, #1E40AF); }
.ai-set:hover { border-color: rgba(37,99,235,.5); background: #0C1220; color: #3B82F6; }
.ai-mine-head { justify-content: space-between; padding: 0 2px; }
.ai-refresh { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 4px 8px; font-size: 10px; }
.ai-tabs { margin-top: 10px; flex-wrap: wrap; gap: 6px; padding: 0 2px; }
.ai-tabs button { display: flex; align-items: center; gap: 4px; border: 1px solid #22304A; background: #0C1220; color: #8B96A8; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 600; }
.ai-tabs button span { border-radius: 999px; background: rgba(0,0,0,.2); padding: 0 6px; font-size: 9px; }
.ai-tabs button.on { border: 0; color: #fff; background: linear-gradient(90deg, #2563EB, #1D4ED8); box-shadow: 0 2px 8px rgba(37,99,235,.35); }
.ai-tabs .push { margin-left: auto; }
.ai-cards { margin-top: 8px; display: grid; gap: 10px; align-items: start; }
.ai-card { min-width: 0; text-align: left; border: 1px solid #1C2740; background: #101828; border-radius: 12px; padding: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.35); }
.ai-card:hover { border-color: rgba(37,99,235,.5); }
.ai-card-top { justify-content: space-between; }
.ai-card-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ai-card-heading > div { min-width: 0; }
.ai-card-heading p { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-card-top p:first-child { margin: 0; font-size: 12px; font-weight: 700; line-height: 16px; }
.ai-card-top em { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; font-style: normal; font-size: 10px; font-weight: 600; border-radius: 9999px; padding: 2px 8px; }
.ai-card-top em.on { color: #22C55E; background: rgba(34,197,94,.15); }
.ai-card-top em.off { color: #F59E0B; background: rgba(245,158,11,.15); }
.ai-card-top em.off i { background: #F59E0B; }
.ai-stats { margin-top: 6px; display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; font-variant-numeric: tabular-nums; }
.ai-stats div { border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 6px 8px; }
.ai-stats span { display: block; color: #5B6B85; font-size: 9px; }
.ai-stats strong { display: block; margin-top: 2px; font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.up { color: #22C55E; } .down { color: #F6465D; } .dim { color: #4A5568; }
.ai-card-actions { margin-top: 10px; justify-content: space-between; border-top: 1px solid #1E2B47; padding-top: 8px; }
.ai-card-actions .link { display: flex; align-items: center; gap: 2px; border: 0; border-radius: 0; background: transparent; color: #3B82F6; font-size: 10px; font-weight: 500; padding: 0; }
.ai-card-actions .link svg { width: 12px; height: 12px; }
.ai-card-actions > div { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.ai-card-actions .amber, .ai-card-actions .solid { display: flex; align-items: center; gap: 4px; }
.ai-card-actions .amber svg, .ai-card-actions .solid svg { width: 13px; height: 13px; }
.ai-card-actions .amber { border: 1px solid rgba(245,158,11,.4); background: rgba(245,158,11,.1); color: #F59E0B; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 600; }
.ai-card-actions .solid, .ai-grid button { border: 0; background: #F6465D; color: #fff; border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 600; box-shadow: 0 2px 8px rgba(246,70,93,.35); }
.ai-grid button { background: rgba(246,70,93,.1); color: #F6465D; box-shadow: none; border: 1px solid rgba(246,70,93,.4); border-radius: 4px; padding: 2px 8px; font-size: 10px; font-weight: 500; }
.ai-table { margin-top: 10px; overflow-x: auto; border: 1px solid #1E2B47; border-radius: 8px; background: #0A1120; }
.ai-trade-label { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; border-bottom: 1px solid #16213A; }
.ai-trade-label p { display: flex; align-items: center; gap: 4px; margin: 0; font-size: 11px; font-weight: 600; }
.ai-trade-label svg { width: 13px; height: 13px; color: #3B82F6; }
.ai-trade-label span { color: #5B6B85; font-size: 10px; }
.ai-grid { display: grid; grid-template-columns: 14px 22px 34px minmax(56px,1fr) minmax(58px,1fr) minmax(64px,1fr) 48px; min-width: 352px; gap: 6px; align-items: center; padding: 4px 10px; font-size: 11px; font-variant-numeric: tabular-nums; text-align: right; border-bottom: 1px dashed #1B2A48; }
.ai-grid > :first-child { text-align: left; font-size: 10px; }
.ai-grid > :nth-child(2) { text-align: center; }
.ai-grid > :nth-child(3) { font-size: 10px; }
.ai-grid > button { justify-self: end; }
.ai-grid.head { color: #5B6B85; font-size: 10px; font-weight: 500; background: #0C1424; border-bottom: 1px solid #16213A; }
.ai-grid:last-of-type { border-bottom: 0; }
.ai-grid strong { font-weight: 600; }
.ai-grid i { display: inline-flex; align-items: center; justify-content: center; height: 15px; min-width: 20px; font-style: normal; font-size: 10px; font-weight: 700; line-height: 1; border-radius: 3px; padding: 0 2px; }
.ai-grid i.buy { color: #22C55E; background: rgba(34,197,94,.15); }
.ai-grid i.sell { color: #F6465D; background: rgba(246,70,93,.15); }
.sltp em { display: block; font-style: normal; line-height: 1.25; }
.ai-table footer { display: flex; justify-content: space-between; padding: 6px 10px; border-top: 1px solid #16213A; background: #0C1424; font-size: 10px; font-variant-numeric: tabular-nums; }
.ai-table footer > span { color: #5B6B85; }
.ai-table footer b { font-weight: 600; }
.ai-node-card { border: 1px solid #22304A; background: #0C1220; border-radius: 8px; padding: 10px; }
.ai-node-card > p { margin: 0 0 6px; font-size: 12px; font-weight: 700; }
.ai-node-line { justify-content: space-between; gap: 8px; padding: 6px 0; border-top: 1px dashed #1B2A48; font-size: 12px; }
.ai-node-line button { border: 1px solid rgba(246,70,93,.4); background: transparent; color: #F6465D; border-radius: 8px; padding: 2px 8px; }
.ai-empty { padding: 8px 2px; }
.ai-empty p { margin: 0; }
.ai-empty .ai-empty-hint { margin-top: 4px; color: #8B96A8; }
.ai-empty .ai-refresh { margin-top: 8px; }
.ai-mask { position: fixed; inset: 0; z-index: 40; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center; padding: 16px; }
.ai-dialog { width: min(560px, 100%); max-height: 86vh; overflow: auto; padding: 14px; border: 1px solid #1E2B47; border-radius: 16px; background: #0D1626; }
.ai-dialog header, .ai-dialog footer { display: flex; align-items: center; justify-content: space-between; }
.ai-dialog header p { margin: 4px 0 0; color: #8B96A8; font-size: 11px; }
.ai-x { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; padding: 0; border-radius: 8px; border: 1px solid #22304A; background: #0A0F1A; color: #8B96A8; }
.ai-pick { width: 100%; margin-top: 8px; padding: 8px; text-align: left; border-radius: 10px; border: 1px solid #22304A; background: #0C1220; color: inherit; }
.ai-pick.on { border-color: #2563EB; }
.ai-pick.off { opacity: .45; }
.ai-pick .box { width: 16px; height: 16px; border: 1px solid #3D4C66; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; }
.ai-pick small { display: block; color: #8B96A8; }
.ai-pick em { font-style: normal; color: #FBBF24; font-size: 10px; }
.ai-dialog footer { margin-top: 12px; }
.ai-dialog .primary { border: 0; background: #2563EB; color: #fff; border-radius: 8px; padding: 8px 12px; }
.ai-warn { color: #FBBF24; font-size: 12px; }
button:disabled { opacity: .45; cursor: not-allowed; }
@media (min-width: 640px) {
  :global(.app-shell.is-customer:has(.ai) .nav-customer a) { padding: 0 10px; font-size: 12px; }
  .ai-online { display: flex; }
}
@media (min-width: 1024px) {
  :global(.app-shell.is-customer .content:has(> .ai)) { padding-top: 12px; }
  .ai-hero { padding: 20px; }
  .ai-cards { grid-template-columns: 1fr 1fr; }
}
</style>
