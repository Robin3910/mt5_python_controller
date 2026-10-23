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
import { computed, onBeforeUnmount, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import {
  fetchAiRobotDetail,
  fetchAiRobots,
  fetchMyAiFollowings,
  joinAiFollow,
  leaveAiFollow,
} from '@/api/aiFollow'
import type {
  AiMyGroup,
  AiMyRobot,
  AiRobotDetailResponse,
  AiRobotSummary,
} from '@/api/types'

const robots = ref<AiRobotSummary[]>([])
const loading = ref(false)
const keyword = ref('')

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
let pollTimer: number | undefined

const selectedGroupIds = ref<string[]>([])
const selectedNodeIds = ref<string[]>([])
const submitting = ref(false)

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
  /** 该节点被跟随的**全部分组**（移出时必须一次性清掉，只清一个会留「半个跟单」） */
  groups: { group_id: string; group_name: string | null }[]
}

interface MyRobotEntry {
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
          // 记全所属分组（去重）：同一节点可能在同一机器人下的多个分组被跟随，
          // 移出时只清一个分组会残留「半个跟单」（仍在其它分组收信号）。
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
      robot: r.robot,
      marketable: r.robot.marketable,
      groups,
      nodes: [...nodesById.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
    }
  }),
)

/**
 * 该节点在**该机器人**下被跟随的全部分组 ID。
 *
 * 必须取**全部**而不是第一个：跟单关系是 (group_id, node_id) 维度的，
 * 同一节点可以同时跟同一个机器人的多个 AI 分组。移出时只发一个分组会
 * 残留「半个跟单」—— 节点仍在其它分组里收信号，用户以为已经退干净了。
 */
function findGroupIdsForNode(robot: AiRobotSummary, nodeId: string): string[] {
  const entry = myFollowing.value.find((x) => x.robot.leader_user_id === robot.leader_user_id)
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
 */
const alreadyFollowingNodeIds = computed<Set<string>>(() => {
  const ids = new Set<string>()
  if (!detail.value) return ids
  const selected = new Set(selectedGroupIds.value)
  for (const g of selectableGroups.value) {
    if (!selected.has(g.group_id)) continue
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

/** 数据新鲜度：绩效由预聚合任务刷新，必然滞后，UI 必须标注 */
function updatedText(robot: AiRobotSummary): string {
  const ts = robot.performance?.updated_at
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `数据更新于 ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fmtProfit(v: number): string {
  // 符号在货币符号**之前**（-$123.45 / +$123.45）。
  // 原来负数写成 `$-123.45`，符号跑到 $ 后面不符合金额书写惯例。
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return sign + '$' + Math.abs(v).toFixed(2)
}
function fmtRate(v: number): string {
  return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'
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

// ---------------------------------------------------------------------------
// 手机端布局状态
// ---------------------------------------------------------------------------
//
// 与 FollowView 同一套结构：顶部分段切换「机器人 / 我的跟单」+ 点机器人进全屏详情。
// 桌面端的两栏（.main-grid）在窄屏会退化成「长列表 + 长详情」的超长滚动条，
// 且 .mine-table 真表格在窄屏无法阅读 —— 两端必须用不同的信息架构。

/** 分段切换当前面板 */
const mobileTab = ref<'robots' | 'mine'>('robots')

/** 详情全屏页是否打开 */
const mobileSheetOpen = ref(false)

/** 当前统计窗口的中文标签（详情页里要说明「这些数字算的是哪一段」） */
const perfWindowLabel = computed(
  () => WINDOW_OPTIONS.find((o) => o.value === perfWindow.value)?.label ?? '',
)

/** 手机端打开详情：立刻弹页再拉数据，避免点完没有反馈 */
async function openMobileRobot(r: AiRobotSummary) {
  mobileSheetOpen.value = true
  await openRobot(r)
}

/** 关闭详情页并回到列表 */
function closeMobileSheet() {
  mobileSheetOpen.value = false
  selectedGroupIds.value = []
  selectedNodeIds.value = []
  detail.value = null
}

// 详情页打开时锁住背景滚动：全屏页是 fixed 覆盖层，
// 不锁的话手指在页面上滑会连带把背后的列表一起滚掉。
watch(mobileSheetOpen, (open) => {
  document.body.style.overflow = open ? 'hidden' : ''
})

onBeforeUnmount(() => {
  document.body.style.overflow = ''
})

/**
 * 跟单成功后：手机端收起详情页并切到「我的跟单」。
 * 桌面端保持留在详情页（原有行为不变）。
 */
function afterJoinSettled() {
  mobileSheetOpen.value = false
  mobileTab.value = 'mine'
}

/**
 * 手机端节点勾选。
 *
 * 桌面端用原生 checkbox + v-model，窄屏上原生控件在小、深色背景下对比度差
 * 且不好点，所以手机端改成自绘勾选框 + 整行点击。
 * 语义必须与 v-model 一致：已跟过的节点不可勾（保持不可选），
 * 可选节点点一下切换选中状态。
 */
function toggleMobileNode(nodeId: string) {
  if (!selectableNodeIds.value.has(nodeId)) return
  if (alreadyFollowingNodeIds.value.has(nodeId)) return
  const s = new Set(selectedNodeIds.value)
  s.has(nodeId) ? s.delete(nodeId) : s.add(nodeId)
  selectedNodeIds.value = [...s]
}

// ---------- 加载 ----------
async function loadRobots() {
  loading.value = true
  try {
    const res = await fetchAiRobots(keyword.value.trim(), perfWindow.value)
    robots.value = res.robots
  } catch (e) {
    ElMessage.error(errText(e, '加载机器人广场失败'))
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
    detail.value = await fetchAiRobotDetail(r.leader_user_id, perfWindow.value)
    selectedGroupIds.value = allSelectableGroupIds.value
  } catch (e) {
    detail.value = null
    ElMessage.error(errText(e, '加载机器人详情失败'))
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detail.value = null
}

async function refreshAll() {
  await Promise.all([loadRobots(), loadMine()])
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
    if (document.visibilityState === 'visible') void loadMine()
  }, 45_000)
})

onUnmounted(() => {
  if (pollTimer != null) window.clearInterval(pollTimer)
})

// ---------- 加入 / 移出 ----------
async function submitJoin() {
  if (!detail.value) return
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
    detail.value = await fetchAiRobotDetail(detail.value.robot.leader_user_id, perfWindow.value)
    selectedGroupIds.value = allSelectableGroupIds.value
    // 手机端收起详情并跳到「我的跟单」：成功结果在那里才看得见。
    // 桌面端保持留在详情页（detail 仍非空，原有行为不变）。
    if (mobileSheetOpen.value) afterJoinSettled()
  } catch (e) {
    ElMessage.error(errText(e, '加入 AI 跟单失败'))
  } finally {
    submitting.value = false
  }
}

async function confirmLeave(robot: AiRobotSummary, nodeId: string) {
  const groupIds = findGroupIdsForNode(robot, nodeId)
  if (!groupIds.length) return
  const ok = await new Promise<boolean>((resolve) => {
    ElMessageBox.confirm(
      '确认移出 AI 跟单？',
      '确认移出',
      {
        type: 'warning',
        confirmButtonText: '移出',
        cancelButtonText: '取消',
        customClass: 'ai-leave-confirm',
      },
    )
      .then(() => resolve(true))
      .catch(() => resolve(false))
  })
  if (!ok) return
  try {
    const res = await leaveAiFollow({
      leader_user_id: robot.leader_user_id,
      // 发**全部分组**：同一节点可能同时跟了该机器人的多个 AI 分组，
      // 只发一个会残留「半个跟单」（节点仍在其它分组收信号）。
      group_ids: groupIds,
      node_ids: [nodeId],
      close_tasks: false,
    })
    const removedCount = res.removed.reduce((n, r) => n + r.node_ids.length, 0)
    let msg = `已移出 ${removedCount} 个节点`
    if (res.closing.length) msg += `，${res.closing.length} 个任务正在平仓`
    if (res.forced.length) msg += `，${res.forced.length} 个离线节点已排队补发平仓`
    if (res.warning) ElMessage.warning(res.warning)
    // 跨市场分组：关系**未**被删除，单独提示（不能混进「已移出」的计数里）
    if (res.skipped.length) {
      ElMessage.warning(`有 ${res.skipped.length} 个分组属于【带单】市场，未在本次移出：${res.skipped[0].reason}`)
    }
    ElMessage.success(msg)
    await refreshAll()
    if (detail.value?.robot.leader_user_id === robot.leader_user_id) {
      detail.value = await fetchAiRobotDetail(robot.leader_user_id, perfWindow.value)
    }
  } catch (e) {
    ElMessage.error(errText(e, '移出 AI 跟单失败'))
  }
}
</script>

<template>
  <div class="page follow-wrap">

    <div class="main-grid">
      <!-- 左：广场 -->
      <section class="panel">
        <header class="panel-head">
          <h2>AI 机器人广场</h2>
          <span class="muted" style="font-size: 12px; margin-left: auto">{{ robots.length }} 个</span>
        </header>
        <!-- 统计窗口：所有机器人同一窗口，否则没法横向比较 -->
        <div class="win-row">
          <span class="muted win-label">统计窗口</span>
          <div class="win-btns">
            <button
              v-for="opt in WINDOW_OPTIONS"
              :key="opt.value"
              class="btn-sm"
              :class="perfWindow === opt.value ? 'btn-primary' : 'btn-ghost'"
              :disabled="loading"
              @click="perfWindow = opt.value"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>
        <div class="search-row">
          <input
            v-model="keyword"
            placeholder="搜索用户名 / 显示名 / 主节点名 / MT5 号"
            @keyup.enter="loadRobots"
          />
          <button class="btn-sm" :disabled="loading" @click="loadRobots">搜索</button>
        </div>

        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!robots.length" class="empty">暂无机器人</div>
        <ul v-else class="card-list">
          <li
            v-for="r in robots"
            :key="r.leader_user_id"
            class="robot-card"
            :class="{ active: detail?.robot.leader_user_id === r.leader_user_id }"
            @click="openRobot(r)"
          >
            <div class="robot-head">
              <span class="robot-name">{{ r.display_name || r.username }}</span>
            </div>
            <div class="robot-sub">
              <span>{{ primaryText(r) }}</span>
            </div>

            <!-- 绩效（主节点口径）：与 FollowView 带单广场同一套组件与口径 -->
            <div class="leader-stats">
              <div class="ls-hero">
                <div class="ls-hero-main">
                  <span class="ls-hero-k">累计收益</span>
                  <span v-if="!hasPerf(r)" class="ls-hero-v muted">暂无数据</span>
                  <span v-else class="ls-hero-v" :class="perfOf(r).total_profit >= 0 ? 'pos' : 'neg'">
                    {{ fmtProfit(perfOf(r).total_profit) }}
                  </span>
                </div>
                <div class="ls-hero-side">
                  <span class="ls-hero-k">收益率</span>
                  <span v-if="!hasPerf(r)" class="ls-hero-sub muted">暂无数据</span>
                  <span v-else-if="!perfOf(r).profit_rate_valid" class="ls-hero-sub muted">—</span>
                  <span
                    v-else
                    class="ls-hero-sub"
                    :class="perfOf(r).profit_rate >= 0 ? 'pos' : 'neg'"
                  >
                    {{ fmtRate(perfOf(r).profit_rate) }}
                  </span>
                </div>
              </div>

              <div class="ls-mini-row">
                <div class="ls-mini">
                  <span class="ls-mini-k">胜率</span>
                  <span v-if="!hasPerf(r)" class="ls-mini-v muted">暂无数据</span>
                  <span v-else class="ls-mini-v">{{ perfOf(r).win_rate.toFixed(2) }}%</span>
                </div>
                <div class="ls-mini">
                  <span class="ls-mini-k">跟单人数</span>
                  <span class="ls-mini-v">{{ followerCountOf(r) }} 人</span>
                </div>
              </div>

              <div v-if="updatedText(r)" class="ls-updated muted">{{ updatedText(r) }}</div>
            </div>
          </li>
        </ul>
      </section>

      <!-- 右：详情 + 我的 AI 跟单 -->
      <section class="panel">
        <header class="panel-head">
          <h2>
            <template v-if="detail">{{ detail.robot.display_name || detail.robot.username }} 的 AI 分组</template>
            <template v-else>详情</template>
          </h2>
          <button v-if="detail" class="btn-sm" @click="closeDetail">关闭</button>
        </header>

        <!-- 详情区 -->
        <div v-if="detailLoading" class="empty">加载中…</div>
        <div v-else-if="detail">
          <header class="robot-detail-head">
            <div>
              <span class="robot-name large">{{ detail.robot.display_name || detail.robot.username }}</span>
            </div>
            <div class="muted">{{ primaryText(detail.robot) }}</div>
          </header>

          <h3 class="sec-title">我的节点（可多选）</h3>
          <ul v-if="(detail.my_nodes ?? []).length" class="pick-list">
            <!-- 已经跟着的节点：**不可勾选**（置灰）。
                 跟单关系是 (group_id, node_id) 维度的，同一节点跟了 A 组仍然可以跟 B 组，
                 所以「已跟过」必须落到**当前选中的分组**上（alreadyFollowingNodeIds）。
                 把它做成 disabled 而不是只改样式：真正禁止勾选，提交时也不会带上。 -->
            <li
              v-for="n in detail.my_nodes"
              :key="n.node_id"
              class="pick-item"
              :class="{ 'is-followed': alreadyFollowingNodeIds.has(n.node_id) }"
            >
              <label>
                <input
                  type="checkbox"
                  :value="n.node_id"
                  :disabled="!selectableNodeIds.has(n.node_id) || alreadyFollowingNodeIds.has(n.node_id)"
                  v-model="selectedNodeIds"
                />
                <span class="pick-name">{{ n.name || n.node_id }}</span>
                <span class="muted">MT5 {{ n.mt5_login ?? '?' }}</span>
                <span v-if="alreadyFollowingNodeIds.has(n.node_id)" class="tag amber">已跟</span>
              </label>
            </li>
          </ul>
          <div v-else class="empty muted">尚无可用于跟单的节点（主节点不可跟单）</div>

          <div class="join-bar">
            <button
              class="btn-primary"
              :disabled="submitting"
              @click="submitJoin"
            >{{ submitting ? '提交中…' : '确认跟单' }}</button>
          </div>
        </div>

        <div v-else class="empty muted">选一个机器人查看详情</div>

        <!-- 我的 AI 跟单 -->
        <header class="panel-head sub">
          <h2>我的 AI 跟单</h2>
          <button class="btn-sm" :disabled="mineLoading" @click="loadMine">刷新</button>
        </header>
        <div v-if="mineLoading && !myFollowing.length" class="empty">加载中…</div>
        <div v-else-if="!myFollowing.length" class="empty muted">
          暂无 AI 跟单
        </div>
        <ul v-else class="mine-list">
          <li v-for="e in myFollowing" :key="e.robot.leader_user_id" class="mine-block">
            <header class="mine-head">
              <span class="robot-name">{{ e.robot.display_name || e.robot.username }}</span>
              <span v-if="e.marketable" class="tag ok">运行中</span>
              <span v-else class="tag amber">已下架</span>
            </header>
            <!-- 这里只回答「我跟着哪些节点跑」+「怎么退」。
                 分组 / 活跃任务 / 手数上限不展示：分组与手数上限是机器人主人的
                 策略参数（不是我的持仓），活跃任务是对方侧的运行态，退单时看它没意义。
                 节点的所属分组仍留在数据里 —— 移出时必须一次性清干净。 -->
            <table class="mine-table">
              <thead>
                <tr><th>节点</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="n in e.nodes" :key="e.robot.leader_user_id + '|' + n.node_id">
                  <td>
                    {{ n.name || n.node_id }}
                    <span class="muted">· MT5 {{ n.mt5_login ?? '?' }}</span>
                  </td>
                  <td>
                    <button
                      v-if="e.marketable"
                      class="btn-sm btn-danger"
                      @click="confirmLeave(e.robot, n.node_id)"
                    >移出</button>
                    <span v-else class="muted">等待清理</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </li>
        </ul>
      </section>
    </div>

    <!-- ══════════════════════════════════════════════════════════════
         手机端专属层（≤768px 可见，桌面端 display:none）
         结构：分段切换「机器人 / 我的跟单」+ 全屏详情页
         ══════════════════════════════════════════════════════════════ -->

    <!-- 顶部分段切换：手机上「广场」和「我的跟单」是两份互斥信息 -->
    <div class="m-only m-tabs" role="tablist" aria-label="AI 机器人广场">
      <button
        v-for="t in ([
          { key: 'robots', label: '机器人', badge: robots.length },
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

    <!-- ── 分段一：机器人广场 ── -->
    <div v-show="mobileTab === 'robots'" class="m-only m-panel">
      <div class="m-search-row">
        <input
          v-model="keyword"
          class="m-search"
          type="search"
          enterkeyhint="search"
          placeholder="搜索用户名 / 节点 / MT5 号"
          @keyup.enter="loadRobots"
        />
        <button class="m-search-btn" :disabled="loading" @click="loadRobots">搜索</button>
      </div>

      <!-- 统计窗口：横向滑动的分段控件，不再换行占一屏 -->
      <div class="m-win" role="group" aria-label="统计窗口">
        <button
          v-for="opt in WINDOW_OPTIONS"
          :key="opt.value"
          class="m-win-btn"
          :class="{ on: perfWindow === opt.value }"
          :aria-pressed="perfWindow === opt.value"
          :disabled="loading"
          @click="perfWindow = opt.value"
        >
          {{ opt.label }}
        </button>
      </div>

      <div v-if="loading" class="m-empty muted">加载中…</div>
      <div v-else-if="!robots.length" class="m-empty muted">暂无机器人</div>

      <ul v-else class="m-robot-list">
        <li
          v-for="r in robots"
          :key="r.leader_user_id"
          class="m-robot"
          @click="openMobileRobot(r)"
        >
          <div class="m-robot-top">
            <span class="m-robot-name">{{ robotTitle(r) }}</span>
            <span v-if="robotStrategyExtra(r)" class="m-robot-extra muted">{{ robotStrategyExtra(r) }}</span>
            <span v-if="!r.marketable" class="tag amber">已下架</span>
            <span v-else-if="r.my_follow_node_count" class="tag green">已跟 {{ r.my_follow_node_count }}</span>
          </div>

          <!-- 主视觉：收益 + 收益率放大成一行大数字，先给结论再看细节 -->
          <div class="m-pnl">
            <div v-if="!hasPerf(r)" class="m-pnl-none muted">暂无成交数据</div>
            <template v-else>
              <span class="m-pnl-main" :class="perfOf(r).total_profit >= 0 ? 'pos' : 'neg'">
                {{ fmtProfit(perfOf(r).total_profit) }}
              </span>
              <span
                v-if="perfOf(r).profit_rate_valid"
                class="m-pnl-rate"
                :class="perfOf(r).profit_rate >= 0 ? 'pos' : 'neg'"
              >
                {{ fmtRate(perfOf(r).profit_rate) }}
              </span>
            </template>
            <span v-if="updatedText(r)" class="m-pnl-time muted">{{ updatedText(r) }}</span>
          </div>

          <div class="m-mini-row">
            <span class="m-mini">
              <span class="m-mini-k">胜率</span>
              <span v-if="!hasPerf(r)" class="m-mini-v muted">—</span>
              <span v-else class="m-mini-v">{{ perfOf(r).win_rate.toFixed(1) }}%</span>
            </span>
            <span class="m-mini">
              <span class="m-mini-k">跟单</span>
              <span class="m-mini-v">{{ followerCountOf(r) }} 人</span>
            </span>
          </div>
        </li>
      </ul>
    </div>

    <!-- ── 分段二：我的 AI 跟单 ── -->
    <div v-show="mobileTab === 'mine'" class="m-only m-panel">
      <div v-if="mineLoading && !myFollowing.length" class="m-empty muted">加载中…</div>
      <div v-else-if="!myFollowing.length" class="m-empty">
        <p class="muted m-empty-text">暂无 AI 跟单</p>
        <button class="m-empty-btn btn-sm btn-ghost" @click="mobileTab = 'robots'">
          去挑一个机器人
        </button>
      </div>
      <div v-else class="m-mine-list">
        <div v-for="e in myFollowing" :key="e.robot.leader_user_id" class="m-mine">
          <div class="m-mine-head">
            <span class="m-mine-name">{{ robotTitle(e.robot) }}</span>
            <span v-if="e.marketable" class="tag ok">运行中</span>
            <span v-else class="tag amber">已下架</span>
          </div>

          <p v-if="!e.nodes.length" class="muted m-mine-none">暂未跟单任何节点</p>

          <!-- 桌面端是 <table>，窄屏没法读；这里用卡片行代替表格。
               每行一个节点：名称 + 账号一行，移出按钮独立成行。 -->
          <ul v-else class="m-mine-nodes">
            <li v-for="n in e.nodes" :key="e.robot.leader_user_id + '|' + n.node_id" class="m-mine-node">
              <div class="m-mine-node-main">
                <span class="m-mine-node-name">{{ n.name || n.node_id }}</span>
                <span class="m-mine-node-meta muted">
                  <template v-if="n.joined_at">{{ fmtJoinedShort(n.joined_at) }} 加入</template>
                </span>
              </div>
              <button
                v-if="e.marketable"
                class="m-mine-leave"
                @click="confirmLeave(e.robot, n.node_id)"
              >移出</button>
              <span v-else class="m-mine-wait muted">等待清理</span>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <!-- ── 全屏详情页 ──
         详情原来排在列表下方，手机上要点完再滚过去；改为整页覆盖。 -->
    <div v-if="mobileSheetOpen" class="m-sheet" role="dialog" aria-modal="true" aria-label="AI 机器人详情">
      <header class="m-sheet-bar">
        <button class="m-back" aria-label="返回机器人列表" @click="closeMobileSheet">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <span class="m-sheet-title">机器人详情</span>
      </header>

      <div class="m-sheet-body">
        <div v-if="detailLoading" class="m-empty muted">加载中…</div>

        <template v-else-if="detail">
          <div class="m-d-lead">
            <div class="m-d-name">{{ robotTitle(detail.robot) }}</div>
            <!-- 副标题只说明「还有几个策略」，不再显示节点名 / MT5 账号 -->
            <div v-if="robotStrategyExtra(detail.robot)" class="m-d-node muted">
              {{ robotStrategyExtra(detail.robot) }}
            </div>

            <div class="m-d-grid">
              <div class="m-d-stat">
                <span class="m-d-k">累计收益</span>
                <span v-if="!hasPerf(detail.robot)" class="m-d-v muted">暂无数据</span>
                <span v-else class="m-d-v" :class="perfOf(detail.robot).total_profit >= 0 ? 'pos' : 'neg'">
                  {{ fmtProfit(perfOf(detail.robot).total_profit) }}
                </span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">收益率</span>
                <span v-if="!hasPerf(detail.robot)" class="m-d-v muted">暂无数据</span>
                <span v-else-if="!perfOf(detail.robot).profit_rate_valid" class="m-d-v muted">—</span>
                <span v-else class="m-d-v" :class="perfOf(detail.robot).profit_rate >= 0 ? 'pos' : 'neg'">
                  {{ fmtRate(perfOf(detail.robot).profit_rate) }}
                </span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">胜率</span>
                <span v-if="!hasPerf(detail.robot)" class="m-d-v muted">暂无数据</span>
                <span v-else class="m-d-v">{{ perfOf(detail.robot).win_rate.toFixed(1) }}%</span>
              </div>
              <div class="m-d-stat">
                <span class="m-d-k">跟单人数</span>
                <span class="m-d-v">{{ followerCountOf(detail.robot) }} 人</span>
              </div>
            </div>
            <div v-if="updatedText(detail.robot)" class="m-d-updated muted">
              {{ perfWindowLabel }} · {{ updatedText(detail.robot) }}
            </div>
          </div>

          <!-- 选节点：整行 56px 触控区 -->
          <div class="m-d-sec-title">
            选择要跟随的节点
            <span class="m-d-sec-sub muted">可多选</span>
          </div>

          <p v-if="!(detail.my_nodes ?? []).length" class="muted m-empty-text">
            尚无可用于跟单的节点（主节点不可跟单）
          </p>

          <ul v-else class="m-pick-list">
            <li
              v-for="n in detail.my_nodes"
              :key="n.node_id"
              class="m-pick"
              :class="{
                on: selectableNodeIds.has(n.node_id) && selectedNodeIds.includes(n.node_id),
                done: !selectableNodeIds.has(n.node_id) || alreadyFollowingNodeIds.has(n.node_id),
              }"
              @click="
                !selectableNodeIds.has(n.node_id) || alreadyFollowingNodeIds.has(n.node_id)
                  ? null
                  : toggleMobileNode(n.node_id)
              "
            >
              <span class="m-pick-box" aria-hidden="true">
                <svg
                  v-if="selectableNodeIds.has(n.node_id) && selectedNodeIds.includes(n.node_id)"
                  viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
                  stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"
                >
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              </span>
              <span class="m-pick-main">
                <span class="m-pick-name">{{ n.name || n.node_id }}</span>
              </span>
              <span v-if="alreadyFollowingNodeIds.has(n.node_id)" class="tag amber">已跟</span>
            </li>
          </ul>
        </template>
      </div>

      <!-- 底部固定操作条 -->
      <footer v-if="detail" class="m-sheet-foot">
        <span class="m-foot-count muted">
          已选 <strong class="m-foot-n">{{ selectedNodeIds.length }}</strong> 个节点
        </span>
        <button
          class="m-foot-btn"
          :disabled="!selectedNodeIds.length || submitting"
          @click="submitJoin"
        >
          {{ submitting ? '提交中…' : '确认跟单' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.follow-wrap {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.banner {
  padding: 10px 14px;
  border-radius: var(--radius);
  border: 1px solid var(--danger, #f56c6c);
  background: rgba(245, 108, 108, 0.08);
  color: var(--text);
  line-height: 1.5;
}
.banner strong { margin-right: 6px; }
.banner.danger { border-color: var(--danger, #f56c6c); }

.main-grid {
  display: grid;
  grid-template-columns: minmax(280px, 380px) 1fr;
  gap: 14px;
  align-items: start;
}
@media (max-width: 960px) {
  .main-grid { grid-template-columns: 1fr; }
}

.panel {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  padding: 14px;
}
.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.panel-head h2 { font-size: 18px; margin: 0; }
.panel-head.sub { margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--glass-border); }
.panel-head input { flex: 1; }

.card-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.robot-card {
  padding: 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color 0.15s ease;
}
.robot-card:hover { border-color: var(--primary); }
.robot-card.active { border-color: var(--primary); background: rgba(64, 158, 255, 0.06); }

/* 统计窗口 + 搜索：窄栏（280~380px）里各占一行，别和标题挤在一起 */
.win-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.win-label { font-size: 12px; white-space: nowrap; }
.win-btns {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  min-width: 0;
}
.search-row {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.search-row input { flex: 1; min-width: 0; }

/* ── 绩效统计（与 FollowView 带单广场同一套 DOM 与样式）───────── */
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

.robot-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.robot-name { font-weight: 600; }
.robot-name.large { font-size: 18px; }
.robot-sub { color: var(--muted); font-size: 12px; }
.robot-risk {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px dashed var(--glass-border);
}
.risk-label {
  font-weight: 600;
  color: var(--muted);
}
.robot-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
}

.robot-detail-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
}
.risk-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 13px;
  padding: 8px 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  margin-bottom: 14px;
}

.sec-title {
  font-size: 14px;
  font-weight: 600;
  margin: 16px 0 8px;
}

.pick-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pick-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  background: var(--bg-soft, rgba(255, 255, 255, 0.02));
}
.pick-item label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
/* 已跟过的节点：置灰 + 光标变 not-allowed，让「点不动」一眼可辨，
   而不是让人反复点才发现 checkbox 灰了。
   只压暗「节点信息」，不压暗整行 —— 整行 opacity 会连「已跟过」标签一起淡下去，
   标签正是这里唯一说明原因的东西，必须保持可读。 */
.pick-item.is-followed {
  background: transparent;
  border-style: dashed;
}
.pick-item.is-followed label { cursor: not-allowed; }
.pick-item.is-followed .pick-name,
.pick-item.is-followed .muted { opacity: 0.45; }
.pick-item input:disabled { cursor: not-allowed; }
.pick-name { font-weight: 600; }
.pick-sub { font-size: 12px; margin-left: 24px; }

.join-bar {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--glass-border);
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.warn { font-size: 12px; color: var(--muted); line-height: 1.6; flex: 1; min-width: 280px; margin: 0; }

.empty {
  padding: 20px 0;
  text-align: center;
}
.empty.muted { color: var(--muted); }

.tag {
  display: inline-block;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 4px;
}
.tag.ok { background: rgba(64, 158, 255, 0.16); color: var(--primary); }
.tag.amber { background: rgba(230, 162, 60, 0.16); color: #c4823a; }

.ok { color: var(--primary); }
.off { color: var(--muted); }

.mine-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mine-block {
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  padding: 12px;
}
.mine-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.mine-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.mine-table th, .mine-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px dashed var(--glass-border);
}
.mine-table th { font-weight: 600; color: var(--muted); }
/* 末列是操作按钮：贴右，不要被节点名挤走 */
.mine-table th:last-child, .mine-table td:last-child { text-align: right; white-space: nowrap; }

.badge {
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 4px;
}
.badge.ok { background: rgba(64, 158, 255, 0.16); color: var(--primary); }
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

  .follow-wrap {
    gap: 0;
    padding: 12px 12px calc(12px + env(safe-area-inset-bottom, 0px));
  }

  /* 桌面端的两栏（.main-grid）在手机端整体隐藏。
     漏掉它会导致桌面布局在手机上照样渲染，
     变成「上半截桌面 UI + 下半截手机 UI」重复两套 —— 必须整个隐藏。 */
  .main-grid { display: none; }

  /* ── 分段切换 ── */
  .m-tabs {
    position: sticky;
    top: 0;
    z-index: 12;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4px;
    padding: 4px;
    margin: 0 0 12px;
    background: rgba(6, 10, 18, 0.9);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    backdrop-filter: blur(var(--blur)) saturate(1.3);
    -webkit-backdrop-filter: blur(var(--blur)) saturate(1.3);
  }
  .m-tab {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-height: 44px;
    padding: 0 8px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    color: var(--muted);
    font-size: 14px;
    font-weight: 600;
    transition: color var(--transition), background var(--transition);
  }
  .m-tab:active { background: rgba(255, 255, 255, 0.05); }
  .m-tab.on {
    color: #ffffff;
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
    border-color: transparent;
    box-shadow: 0 2px 12px var(--primary-glow);
  }
  .m-tab-badge {
    min-width: 20px;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.1);
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    line-height: 1.5;
  }
  .m-tab.on .m-tab-badge {
    background: rgba(4, 21, 16, 0.28);
    color: #ffffff;
  }

  .m-panel { display: block; }

  /* ── 搜索行：输入框 + 搜索按钮并排，按钮 44px 可点 ── */
  .m-search-row {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  /* 字号必须 ≥16px：iOS Safari 聚焦小于 16px 的输入框会自动放大整个页面，
     用户无法缩回去。 */
  .m-search {
    flex: 1;
    min-width: 0;
    min-height: 44px;
    border-radius: 12px;
    font-size: 16px;
  }
  .m-search-btn {
    flex-shrink: 0;
    min-height: 44px;
    padding: 0 16px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
    touch-action: manipulation;
  }

  /* ── 统计窗口：横向滑动的分段控件 ── */
  .m-win {
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .m-win::-webkit-scrollbar { display: none; }
  .m-win-btn {
    flex: 1 0 auto;
    min-height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--glass-border);
    background: var(--glass);
    color: var(--muted);
    font-size: 13px;
    font-weight: 600;
    white-space: nowrap;
    touch-action: manipulation;
    transition: color var(--transition), background var(--transition), border-color var(--transition);
  }
  .m-win-btn:active { background: rgba(255, 255, 255, 0.07); }
  .m-win-btn.on {
    color: #ffffff;
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
    border-color: transparent;
    box-shadow: 0 2px 12px var(--primary-glow);
  }

  /* ── 机器人卡片 ── */
  .m-robot-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .m-robot {
    padding: 14px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius);
    background: var(--panel);
    backdrop-filter: blur(var(--blur));
    -webkit-backdrop-filter: blur(var(--blur));
    cursor: pointer;
    touch-action: manipulation;
    transition: border-color var(--transition), background var(--transition);
  }
  /* 手机端没有 hover，用 active 给出点按反馈 */
  .m-robot:active {
    border-color: var(--primary);
    background: rgba(37, 99, 235, 0.08);
  }
  .m-robot-top {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    margin-bottom: 2px;
  }
  .m-robot-name {
    font-size: 15px;
    font-weight: 600;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 副标题：策略名有多个时提示「等 N 个策略」 */
  .m-robot-extra {
    flex-shrink: 0;
    font-size: 11px;
    white-space: nowrap;
  }
  /* 主视觉：收益/收益率放大 */
  .m-pnl {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    padding-bottom: 10px;
    margin-bottom: 10px;
    border-bottom: 1px dashed var(--glass-border);
  }
  .m-pnl-main {
    font-family: var(--mono);
    font-size: 26px;
    font-weight: 700;
    line-height: 1.1;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }
  .m-pnl-rate {
    font-family: var(--mono);
    font-size: 15px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 6px;
    font-variant-numeric: tabular-nums;
  }
  .m-pnl-rate.pos { color: var(--green); background: rgba(16, 185, 129, 0.12); }
  .m-pnl-rate.neg { color: var(--red); background: rgba(239, 68, 68, 0.12); }
  .m-pnl-main.pos { color: var(--green); }
  .m-pnl-main.neg { color: var(--red); }
  .m-pnl-none { font-size: 13px; }
  .m-pnl-time { margin-left: auto; font-size: 11px; white-space: nowrap; }

  /* 次要指标：2 个小格子（胜率 / 跟单人数） */
  .m-mini-row {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .m-mini {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-width: 0;
    padding: 7px 10px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    border-radius: 8px;
  }
  .m-mini-k { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .m-mini-v {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
  }

  /* ── 空态 / 加载态 ── */
  .m-empty {
    padding: 40px 16px;
    text-align: center;
    font-size: 13px;
  }
  .m-empty-text { margin: 0 0 12px; font-size: 13px; }
  .m-empty-btn { min-height: 40px; }

  /* ── 我的 AI 跟单 ── */
  .m-mine-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .m-mine {
    padding: 14px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius);
    background: var(--panel);
    backdrop-filter: blur(var(--blur));
    -webkit-backdrop-filter: blur(var(--blur));
  }
  .m-mine-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
  }
  .m-mine-name {
    font-size: 15px;
    font-weight: 600;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .m-mine-none { margin: 0; font-size: 12px; }

  /* 桌面端是 <table>，窄屏没法读；这里用卡片行代替表格 */
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
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-height: 56px;
    padding: 10px 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
  }
  .m-mine-node-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .m-mine-node-name {
    font-size: 14px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .m-mine-node-meta { font-size: 11px; }
  .m-mine-leave {
    flex-shrink: 0;
    min-height: 40px;
    padding: 0 16px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    background: rgba(239, 68, 68, 0.14);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.3);
    touch-action: manipulation;
  }
  .m-mine-leave:active { background: rgba(239, 68, 68, 0.24); }
  .m-mine-wait { flex-shrink: 0; font-size: 12px; }

  /* ── 全屏详情页 ──
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
  .m-sheet-title { font-size: 16px; font-weight: 600; }

  .m-sheet-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: 16px 14px;
  }

  /* 机器人身份区 */
  .m-d-lead { margin-bottom: 20px; }
  .m-d-name {
    font-size: 19px;
    font-weight: 700;
    letter-spacing: -0.01em;
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
    background: var(--glass);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
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

  .m-d-sec-title {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 0 0 10px;
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
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    background: var(--glass);
    cursor: pointer;
    touch-action: manipulation;
    transition: border-color var(--transition), background var(--transition);
  }
  .m-pick:active { background: rgba(37, 99, 235, 0.12); }
  .m-pick.on {
    border-color: var(--primary);
    background: rgba(37, 99, 235, 0.1);
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
    background: var(--primary);
    border-color: var(--primary);
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

  /* ── 底部固定操作条 ── */
  .m-sheet-foot {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
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
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-d) 100%);
    color: #041510;
    font-size: 15px;
    font-weight: 600;
    border-radius: 12px;
    box-shadow: 0 4px 16px var(--primary-glow);
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
  .m-robot,
  .m-tab,
  .m-win-btn,
  .m-pick,
  .m-back,
  .m-foot-btn,
  .m-mine-leave {
    transition: none;
  }
}

/* ── 极窄屏（iPhone SE 320px）：主视觉再收一档，避免换行 ── */
@media (max-width: 360px) {
  .m-pnl-main { font-size: 23px; }
  .m-pnl { gap: 8px; }
  .m-tab { font-size: 13px; }
  .m-d-v { font-size: 15px; }
  /* 3 个格子在 320px 会挤，改为 2 列让「风险上限」独占一行 */
  .m-mini-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
