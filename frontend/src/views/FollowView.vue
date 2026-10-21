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
  FollowMyGroup,
} from '@/api/types'

const router = useRouter()

const leaders = ref<FollowLeaderSummary[]>([])
const loading = ref(false)
const keyword = ref('')

const detail = ref<FollowDetailResponse | null>(null)
const detailLoading = ref(false)
const selectedGroups = ref<string[]>([])
const selectedNodes = ref<string[]>([])
const submitting = ref(false)

const mine = ref<FollowMeResponse>({ following: [], summary: { following_count: 0, follow_node_count: 0 } })
const mineLoading = ref(false)

// 已选分组集合
const groupSet = computed(() => new Set(selectedGroups.value))
const nodeSet = computed(() => new Set(selectedNodes.value))
// 我的可跟单节点数（后端已剔除主节点）
const myNodeCount = computed(() => detail.value?.my_nodes.length ?? 0)

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
  selectedGroups.value = []
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

function toggleGroup(id: string) {
  const s = new Set(selectedGroups.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedGroups.value = [...s]
}

function toggleNode(id: string) {
  const s = new Set(selectedNodes.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedNodes.value = [...s]
}

async function submitJoin() {
  if (!detail.value || !selectedGroups.value.length || !selectedNodes.value.length) return
  submitting.value = true
  try {
    const res = await joinFollow({
      leader_user_id: detail.value.leader.leader_user_id,
      group_ids: selectedGroups.value,
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
    selectedGroups.value = []
    selectedNodes.value = []
    await Promise.all([loadLeaders(), loadMine()])
  } catch (e) {
    ElMessage.error(errText(e, '加入跟单失败'))
  } finally {
    submitting.value = false
  }
}

async function doLeave(
  leader: FollowLeaderSummary, group: FollowMyGroup,
  nodeIds: string[], closeTasks: boolean,
) {
  try {
    const res = await leaveFollow({
      leader_user_id: leader.leader_user_id,
      group_ids: [group.group_id],
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

async function confirmLeave(leader: FollowLeaderSummary, group: FollowMyGroup, nodeId: string) {
  const node = group.my_nodes.find((n) => n.node_id === nodeId)
  const active = group.active_task_count
  if (!active) {
    await doLeave(leader, group, [nodeId], false)
    return
  }
  // 有进行中的任务：让用户在「平仓后移出」和「保留任务」之间二选一
  try {
    const choice = await ElMessageBox({
      title: '移出跟单',
      type: 'warning',
      dangerouslyUseHTMLString: true,
      message:
        `节点「${node?.name ?? nodeId}」在该分组还有进行中的任务。<br/>` +
        '移出后该分组不再向它下发新信号，现有任务如何处理？',
      distinguishCancelAndClose: true,
      confirmButtonText: '平仓并关闭任务后移出',
      cancelButtonText: '保留任务，仅停止接收新信号',
    })
      .then((r) => r.value)
      .catch((r) => (r === 'cancel' ? 'keep' : 'close'))
    if (choice === 'close') return
    await doLeave(leader, group, [nodeId], choice === 'confirm')
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
        <h2 class="page-title">带单</h2>
        <p class="muted page-sub">把你的非主节点加入带单员的分组，接收他触发的信号。</p>
      </div>
      <RouterLink to="/nodes" class="btn-ghost btn-sm">管理我的节点</RouterLink>
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
              <span v-if="l.my_follow_group_count" class="tag green">已跟 {{ l.my_follow_group_count }}</span>
              <span v-else class="tag">未跟</span>
            </div>
            <div class="muted leader-sub">
              主节点：{{ l.primary_node?.name ?? '—' }}
              <template v-if="l.primary_node?.mt5_login">（MT5 {{ l.primary_node.mt5_login }}）</template>
              <span :class="l.primary_node?.status === 'online' ? 'ok' : 'off'">
                · {{ l.primary_node?.status === 'online' ? '在线' : '离线' }}
              </span>
            </div>
            <div class="muted leader-sub">
              带单源分组 {{ l.followable_group_count }} 个 · 被 {{ l.follower_node_count }} 个节点跟随
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
          <p class="muted page-sub">
            带单源：{{ detail.leader.primary_node?.name }}
            <template v-if="detail.leader.primary_node?.mt5_login">
              （MT5 {{ detail.leader.primary_node.mt5_login }}）
            </template>
            — 他触发信号时，信号会下发给所选分组的全部成员（含你的节点）。
          </p>

          <h4 class="sec-title">分组（可多选）</h4>
          <ul class="pick-list">
            <li
              v-for="g in detail.groups"
              :key="g.group_id"
              class="pick-item"
              :class="{ disabled: !g.selectable, on: groupSet.has(g.group_id) }"
              @click="g.selectable && toggleGroup(g.group_id)"
            >
              <label>
                <input
                  type="checkbox"
                  :checked="groupSet.has(g.group_id)"
                  :disabled="!g.selectable"
                  @click.stop
                  @change="toggleGroup(g.group_id)"
                />
                <span class="pick-name">{{ g.name || g.group_id }}</span>
              </label>
              <span v-if="!g.selectable" class="tag amber">{{ g.unselectable_reason }}</span>
              <div class="muted pick-sub">
                {{ g.strategy_name || '未绑定策略' }}
                <template v-if="g.signal_concurrent_enabled"> · 全员同步</template>
                · 成员 {{ g.member_count }} + 跟单 {{ g.follower_count }} · 在线 {{ g.online_count }}
                · 已触发 {{ g.signal_count }} 次
                <template v-if="g.my_follow_node_ids.length">
                  · <span class="ok">你已跟 {{ g.my_follow_node_ids.length }} 个节点</span>
                </template>
              </div>
            </li>
          </ul>

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
              :class="{ on: nodeSet.has(n.node_id) }"
              @click="toggleNode(n.node_id)"
            >
              <label>
                <input
                  type="checkbox"
                  :checked="nodeSet.has(n.node_id)"
                  @click.stop
                  @change="toggleNode(n.node_id)"
                />
                <span class="pick-name">{{ n.name || n.node_id }}</span>
              </label>
              <span class="muted">MT5 {{ n.mt5_login ?? '—' }}</span>
              <span :class="n.status === 'online' ? 'ok' : 'off'">
                {{ n.status === 'online' ? '在线' : '离线' }}
              </span>
            </li>
          </ul>

          <div class="join-bar">
            <span class="muted">
              已选：{{ selectedNodes.length }} 个节点 × {{ selectedGroups.length }} 个分组
              <template v-if="myNodeCount"> · 可用节点 {{ myNodeCount }}</template>
            </span>
            <button
              class="btn-sm btn-primary"
              :disabled="!selectedNodes.length || !selectedGroups.length || submitting"
              @click="submitJoin"
            >
              {{ submitting ? '提交中...' : '确认跟单' }}
            </button>
          </div>
          <p class="warn">
            加入后将按带单员绑定的策略在你的节点上执行，<strong>手数、止损、加仓规则均以带单员为准</strong>；移出不会自动平仓。
          </p>
        </template>

        <template v-else>
          <div class="row between" style="margin-bottom: 12px">
            <strong>我的跟单</strong>
            <span class="muted" style="font-size: 12px">
              {{ mine.summary.following_count }} 个带单员 · {{ mine.summary.follow_node_count }} 个节点
            </span>
          </div>
          <div v-if="mineLoading" class="muted" style="padding: 12px 0">加载中...</div>
          <div v-else-if="!mine.following.length" class="muted empty">
            还没有跟单，从左侧选择一个带单员开始
          </div>
          <div v-else class="mine-list">
            <div v-for="f in mine.following" :key="f.leader.leader_user_id" class="mine-block">
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
                <span v-if="!f.leader.marketable" class="tag amber">该带单员已关闭带单</span>
              </div>
              <div v-for="g in f.groups" :key="g.group_id" class="mine-group">
                <div class="row between">
                  <span>
                    {{ g.group_name || g.group_id }}
                    <span v-if="g.active_task_count" class="muted">· 进行中 {{ g.active_task_count }}</span>
                  </span>
                  <span v-if="!g.marketable" class="tag amber">{{ g.unmarketable_reason }}</span>
                </div>
                <div v-for="n in g.my_nodes" :key="n.node_id" class="mine-node">
                  <span class="muted">
                    {{ n.name || n.node_id }}
                    <template v-if="n.mt5_login">（MT5 {{ n.mt5_login }}）</template>
                    · 加入于 {{ n.joined_at ? new Date(n.joined_at * 1000).toLocaleString('zh-CN') : '—' }}
                  </span>
                  <button class="btn-sm btn-ghost" @click="confirmLeave(f.leader, g, n.node_id)">
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

.sec-title {
  font-size: 13px;
  margin: 16px 0 8px;
  color: var(--muted);
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

.mine-group {
  padding: 8px 0;
  border-top: 1px solid var(--glass-border);
  font-size: 13px;
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