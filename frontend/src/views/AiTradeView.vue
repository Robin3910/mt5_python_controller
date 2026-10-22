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
import { computed, onMounted, onUnmounted, ref } from 'vue'
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

function fmtNullable(v: number | null | undefined, suffix = '', digits = 2): string {
  if (v == null) return '—'
  return Number(v).toFixed(digits) + suffix
}

function fmtIntNullable(v: number | null | undefined): string {
  if (v == null) return '—'
  return String(Math.trunc(v))
}

function primaryText(robot: AiRobotSummary): string {
  const n = robot.primary_node
  if (!n) return '无主节点'
  return `${n.name ?? n.node_id} · MT5 ${n.mt5_login ?? '?'} · ${n.online ? '在线' : '离线'}`
}

// ---------- 加载 ----------
async function loadRobots() {
  loading.value = true
  try {
    const res = await fetchAiRobots(keyword.value.trim())
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
    detail.value = await fetchAiRobotDetail(r.leader_user_id)
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
  const nodeIds = (detail.value.my_nodes ?? [])
    .map((n) => n.node_id)
    .filter((nid) => selectedNodeIds.value.includes(nid))
  if (!nodeIds.length) {
    ElMessage.warning('请选择至少一个可用节点（主节点不可跟单）')
    return
  }
  if (alreadyFollowingNodeIds.value.size && nodeIds.some((n) => alreadyFollowingNodeIds.value.has(n))) {
    if (!(await new Promise<boolean>((resolve) => {
      ElMessageBox.confirm(
        '所选节点中包含已跟单该机器人的节点，会跳过重复项。是否继续？',
        '部分节点已跟',
        { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' },
      )
        .then(() => resolve(true))
        .catch(() => resolve(false))
    }))) return
  }
  if (!(await new Promise<boolean>((resolve) => {
    ElMessageBox.confirm(
      '确认跟单该 AI 机器人？\n\n'
        + '你的节点将按机器人设定的完整手数执行 AI 加仓。\n'
        + '机器人主人修改策略会实时同步到你在跑的任务，无法锁定。\n'
        + '移出跟单不会自动平仓，在跑任务会继续运行至结束。',
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
    detail.value = await fetchAiRobotDetail(detail.value.robot.leader_user_id)
    selectedGroupIds.value = allSelectableGroupIds.value
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
      '确认移出 AI 跟单？\n\n'
        + '移出后关系解除，不再下发新信号。\n'
        + '**已有任务不会自动平仓**，将由 node_client 继续按策略运行至结束。',
      '确认移出',
      {
        type: 'warning',
        confirmButtonText: '移出（不停止在跑任务）',
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
      detail.value = await fetchAiRobotDetail(robot.leader_user_id)
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
          <input
            v-model="keyword"
            placeholder="搜索用户名 / 显示名 / 主节点名 / MT5 号"
            @keyup.enter="loadRobots"
          />
          <button class="btn-sm" :disabled="loading" @click="loadRobots">搜索</button>
        </header>

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
              <span class="muted">@{{ r.username }}</span>
            </div>
            <div class="robot-sub">
              <span>{{ primaryText(r) }}</span>
            </div>
            <div class="robot-risk">
              <span class="risk-label">风险上限</span>
              <span>批次 {{ fmtIntNullable(r.max_allow_num) }}</span>
              <span>手数 {{ fmtNullable(r.total_lot_limit, '', 2) }}</span>
            </div>
            <div class="robot-meta">
              <span class="muted">AI 分组 {{ r.ai_group_count }}</span>
              <span class="muted">被跟 {{ r.follower_node_count }}</span>
              <span
                v-if="r.my_follow_group_count > 0"
                class="badge ok"
              >已跟 {{ r.my_follow_group_count }} 组 / {{ r.my_follow_node_count }} 节点</span>
            </div>
          </li>
        </ul>
      </section>

      <!-- 右：详情 + 我的 AI 跟单 -->
      <section class="panel">
        <header class="panel-head">
          <h2>
            <template v-if="detail">{{ detail.robot.display_name || detail.robot.username }} 的 AI 分组</template>
            <template v-else>详情 / 我的 AI 跟单</template>
          </h2>
          <button v-if="detail" class="btn-sm" @click="closeDetail">关闭</button>
        </header>

        <!-- 详情区 -->
        <div v-if="detailLoading" class="empty">加载中…</div>
        <div v-else-if="detail">
          <header class="robot-detail-head">
            <div>
              <span class="robot-name large">{{ detail.robot.display_name || detail.robot.username }}</span>
              <span class="muted">@{{ detail.robot.username }}</span>
            </div>
            <div class="muted">{{ primaryText(detail.robot) }}</div>
          </header>

          <h3 class="sec-title">我的节点（可多选）</h3>
          <ul v-if="(detail.my_nodes ?? []).length" class="pick-list">
            <li v-for="n in detail.my_nodes" :key="n.node_id" class="pick-item">
              <label>
                <input
                  type="checkbox"
                  :value="n.node_id"
                  :disabled="!selectableNodeIds.has(n.node_id)"
                  v-model="selectedNodeIds"
                />
                <span class="pick-name">{{ n.name || n.node_id }}</span>
                <span class="muted">MT5 {{ n.mt5_login ?? '?' }}</span>
                <span v-if="alreadyFollowingNodeIds.has(n.node_id)" class="tag amber">已跟过</span>
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

        <div v-else class="empty muted">从左侧选一个机器人查看详情</div>

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
              <span class="muted">@{{ e.robot.username }}</span>
              <span v-if="e.marketable" class="tag ok">运行中</span>
              <span v-else class="tag amber">已下架</span>
            </header>
            <table class="mine-table">
              <thead>
                <tr><th>分组</th><th>节点</th><th>活跃任务</th><th>手数上限</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="n in e.nodes" :key="e.robot.leader_user_id + '|' + n.node_id">
                  <td>
                    <span v-for="(g, gi) in n.groups" :key="g.group_id">
                      {{ gi > 0 ? '、' : '' }}{{ g.group_name || g.group_id }}
                    </span>
                  </td>
                  <td>
                    {{ n.name || n.node_id }}
                    <span class="muted">· MT5 {{ n.mt5_login ?? '?' }}</span>
                  </td>
                  <td>{{ n.active_task_count }}</td>
                  <td>{{ fmtNullable(n.batch_limit, '', 0) }}</td>
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

.badge {
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 4px;
}
.badge.ok { background: rgba(64, 158, 255, 0.16); color: var(--primary); }
</style>
