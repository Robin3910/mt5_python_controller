<script setup lang="ts">
import { deleteNodeWithConfirmation } from '@/composables/useNodeDeletion'
// 节点管理页：创建/编辑/删除节点、启停、重置令牌、批量全平（令牌仅创建时显示一次）。
// 普通用户仅管理自有节点；新建与按币种配置仅管理员。
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import FilterRulesEditor from '@/components/FilterRulesEditor.vue'
import { NODE_FORM_FIELD_HELP } from '@/constants/nodeFormHelp'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { useRbacStore } from '@/stores/rbac'
import type { NodeAssignConflict, NodeDispatchFiltersConfig, NodeOut } from '@/api/types'
import { parseFilterRules, parseNodeDispatchFilters, serializeNodeDispatchFilters, validateNodeDispatchFilters, validateNodeGlobalLotMode } from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'
import { isPendingNode, nodeApprovalHint, nodeEnableIntent, nodeEnableLabel } from '@/utils/nodeApproval'
import { setNodePrimary } from '@/utils/nodePrimary'

const hub = useHubStore()

const ENABLED_TAGS: TagOption<boolean>[] = [
  { value: true, label: '启用', tone: 'on' },
  { value: false, label: '禁用', tone: 'off' },
]
const auth = useAuthStore()
const router = useRouter()
const rbac = useRbacStore()

const searchQuery = ref('')
const appliedQuery = ref('')
const searching = ref(false)

function currentSearchOptions(): { q?: string } {
  return appliedQuery.value ? { q: appliedQuery.value } : {}
}

async function loadNodes(): Promise<void> {
  searching.value = true
  try {
    await hub.fetchNodes(currentSearchOptions())
  } finally {
    searching.value = false
  }
}

async function runSearch(): Promise<void> {
  appliedQuery.value = searchQuery.value.trim()
  await loadNodes()
}

onMounted(() => {
  void loadNodes()
})

onUnmounted(() => {
  if (appliedQuery.value) {
    void hub.fetchNodes()
  }
})

function goDetail(n: NodeOut): void {
  router.push(`/nodes/${n.node_id}`)
}

const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const editingNode = computed(() => hub.nodes.find((n) => n.node_id === editingId.value))
const saving = ref(false)
const createError = ref('')

// 手动新建默认启用；自动注册默认禁用（入库后需管理员启用）
const form = reactive({
  name: '',
  mt5_login: null as number | null,
  enabled: true,
  filters: {} as NodeDispatchFiltersConfig,
})

const selectedIds = ref<Set<string>>(new Set())
const closing = ref(false)

/* ── 筛选 / 排序（对齐 UI/H5.html「节点管理」分段筛选条）── */
type NodeFilter = 'all' | 'online' | 'offline' | 'enabled' | 'disabled'
type NodeSort = 'default' | 'name' | 'status'

const filter = ref<NodeFilter>('all')
const sort = ref<NodeSort>('default')

function nodeStatus(n: NodeOut): string {
  return hub.statuses[n.node_id] || n.status
}

function isOnline(n: NodeOut): boolean {
  return nodeStatus(n) === 'online'
}

/** 节点持仓手数（只展示持仓规模，不展示盈亏） */
function nodePositions(n: NodeOut): { count: number; volume: number } {
  const acc = hub.accounts[n.node_id]
  const list = acc?.positions || []
  return {
    count: list.length,
    volume: list.reduce((sum, p) => sum + (p.volume || 0), 0),
  }
}

function nodeBalance(n: NodeOut): number | null {
  const acc = hub.accounts[n.node_id]
  return acc ? acc.balance : null
}

function fmtMoney(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return '—'
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtVol(v: number): string {
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 最近上报时间 */
function fmtLastSeen(n: NodeOut): string {
  const ts = n.last_seen
  if (!ts || !Number.isFinite(ts)) return '从未上报'
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (v: number) => String(v).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 按当前筛选条件过滤节点 */
const filteredNodes = computed<NodeOut[]>(() => {
  const list = hub.nodes.filter((n) => {
    switch (filter.value) {
      case 'online':
        return isOnline(n)
      case 'offline':
        return !isOnline(n)
      case 'enabled':
        return n.enabled
      case 'disabled':
        return !n.enabled
      default:
        return true
    }
  })
  if (sort.value === 'name') {
    return [...list].sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
  }
  if (sort.value === 'status') {
    // 在线优先，其余按名称
    return [...list].sort((a, b) => {
      const d = Number(isOnline(b)) - Number(isOnline(a))
      return d !== 0 ? d : a.name.localeCompare(b.name, 'zh-Hans-CN')
    })
  }
  return list
})

/** 按所有者分组（原型：管理员 / 风控 这样的分组卡）*/
const groupedNodes = computed(() => {
  const map = new Map<string, NodeOut[]>()
  for (const n of filteredNodes.value) {
    const key = n.owner_username || '管理员'
    const list = map.get(key)
    if (list) list.push(n)
    else map.set(key, [n])
  }
  return Array.from(map.entries()).map(([owner, items]) => ({
    owner,
    items,
    online: items.filter(isOnline).length,
    total: items.length,
  }))
})

/** 总览统计 */
const totalNodes = computed(() => hub.nodes.length)
const onlineNodes = computed(() => hub.nodes.filter(isOnline).length)
const enabledNodes = computed(() => hub.nodes.filter((n) => n.enabled).length)
const disabledNodes = computed(() => totalNodes.value - enabledNodes.value)
const pendingNodes = computed(() => hub.nodes.filter(isPendingNode).length)

const FILTER_OPTIONS: { value: NodeFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'online', label: '在线' },
  { value: 'offline', label: '离线' },
  { value: 'enabled', label: '已启用' },
  { value: 'disabled', label: '已禁用' },
]

function cycleSort(): void {
  sort.value = sort.value === 'default' ? 'name' : sort.value === 'name' ? 'status' : 'default'
}

function sortLabel(): string {
  return sort.value === 'name' ? '按名称' : sort.value === 'status' ? '按状态' : '排序'
}

const selectedCount = computed(() => selectedIds.value.size)
const allSelected = computed(
  () => hub.nodes.length > 0 && hub.nodes.every((n) => selectedIds.value.has(n.node_id)),
)
const someSelected = computed(() => selectedIds.value.size > 0)

watch(
  () => hub.nodes.map((n) => n.node_id),
  (ids) => {
    const valid = new Set(ids)
    const next = new Set([...selectedIds.value].filter((id) => valid.has(id)))
    if (next.size !== selectedIds.value.size) selectedIds.value = next
  },
)

function isSelected(id: string): boolean {
  return selectedIds.value.has(id)
}

function toggleSelect(id: string, checked: boolean): void {
  const next = new Set(selectedIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  selectedIds.value = next
}

function toggleSelectAll(checked: boolean): void {
  if (!checked) {
    const visible = new Set(hub.nodes.map((n) => n.node_id))
    selectedIds.value = new Set([...selectedIds.value].filter((id) => !visible.has(id)))
    return
  }
  selectedIds.value = new Set([...selectedIds.value, ...hub.nodes.map((n) => n.node_id)])
}

async function closeSelected(): Promise<void> {
  const nodes = hub.nodes.filter((n) => selectedIds.value.has(n.node_id))
  if (!nodes.length) return
  const names = nodes.map((n) => `· ${n.name} (${n.node_id})`).join('\n')
  if (!(await confirmAction(`确认对以下 ${nodes.length} 个节点执行全部平仓？\n\n${names}\n\n将同时终止这些节点上所有进行中的策略任务。此操作不可撤销。`, '确认平仓'))) return
  closing.value = true
  try {
    const res = await hub.closeBatch(
      nodes.map((n) => n.node_id),
      { target: 'all' },
    )
    if (res.sent.length) {
      hub.pushEvent(`已向 ${res.sent.length} 个节点下发全平`, 'ok')
    }
    if (res.failed.length) {
      const offline = res.failed.filter((f) => f.reason === 'offline').length
      const missing = res.failed.length - offline
      const parts: string[] = []
      if (offline) parts.push(`${offline} 个离线`)
      if (missing) parts.push(`${missing} 个不存在`)
      hub.pushEvent(`全平未下发：${parts.join('，')}`, 'warn')
    }
    selectedIds.value = new Set()
  } catch {
    hub.pushEvent('全平下发失败（所选节点均离线或不存在）', 'warn')
  } finally {
    closing.value = false
  }
}

async function ensureGlobalFiltersLoaded(): Promise<void> {
  if (!Object.keys(hub.filters).length) {
    await hub.fetchConfig()
  }
}

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  createError.value = ''
  Object.assign(form, { name: '', mt5_login: null, enabled: true, filters: {} })
  showForm.value = true
  void ensureGlobalFiltersLoaded()
}

function openEdit(n: NodeOut): void {
  formMode.value = 'edit'
  editingId.value = n.node_id
  createError.value = ''
  Object.assign(form, {
    name: n.name,
    mt5_login: n.mt5_login,
    enabled: nodeEnableIntent(n),
    filters: parseNodeDispatchFilters(n.filters),
  })
  showForm.value = true
  if (auth.isAdmin) void ensureGlobalFiltersLoaded()
}

/** 普通用户只能改名与启停（按币种配置属于 normal 链路，仅管理员） */
async function saveOwnNode(): Promise<void> {
  const label = form.name || editingId.value
  const enabledText = form.enabled ? '启用' : '禁用'
  if (!(await confirmAction(`确认更新节点「${label}」？\n\n启用状态：${enabledText}`))) return
  try {
    await hub.updateNode(editingId.value, {
      name: form.name,
      ...(editingNode.value && isPendingNode(editingNode.value) ? {} : { enabled: form.enabled }),
    }, currentSearchOptions())
    showForm.value = false
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    createError.value = err?.response?.data?.detail || '更新失败，请稍后重试'
  }
}

async function save(): Promise<void> {
  saving.value = true
  createError.value = ''
  try {
    if (!auth.isAdmin) {
      await saveOwnNode()
      return
    }
    await ensureGlobalFiltersLoaded()
    const filtersPayload = serializeNodeDispatchFilters(form.filters)
    const globalRules = parseFilterRules(hub.filters)
    const filterErrors = [
      ...validateNodeDispatchFilters(filtersPayload),
      ...validateNodeGlobalLotMode(filtersPayload, globalRules),
    ]
    if (filterErrors.length) {
      createError.value = filterErrors[0]
      await ElMessageBox.alert(filterErrors[0], '无法保存', {
        type: 'warning',
        confirmButtonText: '知道了',
      })
      return
    }
    const payload = {
      name: form.name,
      filters: Object.keys(filtersPayload).length ? filtersPayload : null,
    }
    if (formMode.value === 'create') {
      if (!form.mt5_login) return
      const login = form.mt5_login
      if (!(await confirmAction(`确认创建节点？\n\nMT5 登录号：${login}`))) return
      try {
        await hub.createNode({ ...payload, mt5_login: form.mt5_login }, currentSearchOptions())
      } catch (e: unknown) {
        const err = e as { response?: { status?: number; data?: { detail?: string } } }
        if (err?.response?.status === 409) {
          createError.value = `已存在 MT5 登录号为 ${form.mt5_login} 的节点`
          return
        }
        createError.value = err?.response?.data?.detail || '创建失败，请稍后重试'
        if (err?.response?.status === 400 && createError.value) {
          await ElMessageBox.alert(createError.value, '无法保存', {
            type: 'warning',
            confirmButtonText: '知道了',
          })
        }
        return
      }
    } else {
      const label = form.name || editingId.value
      const enabledText = form.enabled ? '启用' : '禁用'
      if (!(await confirmAction(`确认更新节点「${label}」？\n\n启用状态：${enabledText}`))) return
      try {
        await hub.updateNode(editingId.value, { ...payload, enabled: form.enabled }, currentSearchOptions())
      } catch (e: unknown) {
        const err = e as { response?: { status?: number; data?: { detail?: string } } }
        createError.value = err?.response?.data?.detail || '更新失败，请稍后重试'
        if (err?.response?.status === 400 && createError.value) {
          await ElMessageBox.alert(createError.value, '无法保存', {
            type: 'warning',
            confirmButtonText: '知道了',
          })
        }
        return
      }
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: unknown } } }
  const detail = err.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

async function remove(n: NodeOut): Promise<void> {
  try {
    await deleteNodeWithConfirmation(n.node_id, currentSearchOptions())
  } catch (e: unknown) {
    ElMessage.error(errText(e, '删除失败，请稍后重试'))
  }
}

async function toggleEnabled(n: NodeOut): Promise<void> {
  if (isPendingNode(n) && !auth.isAdmin) return
  const current = nodeEnableIntent(n)
  const next = current ? '禁用' : '启用'
  const hint = isPendingNode(n)
    ? '确认启用与分配给申请人两项齐全后才会开通；操作顺序不限。'
    : `${next}后将${current ? '无法接入且不参与分发' : '恢复正常跟单'}。`
  if (!(await confirmAction(`确认${next}节点「${n.name}」？\n\n${hint}`))) return
  await hub.updateNode(n.node_id, { enabled: !current }, currentSearchOptions())
}

/* ── 主节点：设置 / 取消 ── */
async function togglePrimary(n: NodeOut): Promise<void> {
  await setNodePrimary({
    node: n,
    nodes: hub.nodes,
    isAdmin: auth.isAdmin,
    apply: (nodeId, next) => hub.setNodePrimary(nodeId, next, currentSearchOptions()),
  })
}

/* ── 管理员授权：把节点划给某个用户 / 收回管理员名下 ── */
const showAssign = ref(false)
const assignTarget = ref<NodeOut | null>(null)
/** 选中值：正数 = 用户 id；-1 = 收回至管理员名下 */
const assignUserId = ref<number | null>(null)
const RECLAIM_TO_ADMIN = -1
const assignSaving = ref(false)
const assignError = ref('')
/** 节点仍在分组里时的二次确认（后端 409 confirm_required）*/
const assignMemberships = ref<Extract<NodeAssignConflict, { reason: 'confirm_required' }> | null>(null)

async function openAssign(n: NodeOut): Promise<void> {
  assignTarget.value = n
  assignUserId.value = null
  assignError.value = ''
  assignMemberships.value = null
  showAssign.value = true
  try {
    await rbac.fetchUsers()
  } catch {
    assignError.value = '用户列表加载失败，请稍后重试'
  }
}

/** 可选的授权对象：启用中的用户 */
const assignCandidates = computed(() => rbac.users.filter((u) => u.is_active))

function assignConflict(e: unknown): NodeAssignConflict | null {
  const err = e as { response?: { status?: number; data?: { detail?: unknown } } }
  if (err.response?.status !== 409) return null
  const d = err.response.data?.detail
  if (d && typeof d === 'object' && 'reason' in d) return d as NodeAssignConflict
  return null
}

async function submitAssign(confirm = false): Promise<void> {
  const target = assignTarget.value
  if (!target) return
  if (assignUserId.value == null) {
    assignError.value = '请选择授权方式'
    return
  }
  assignSaving.value = true
  assignError.value = ''
  try {
    // 用户权限接口是「整体替换该用户名下的节点」语义，所以这里必须带上
    // 该用户现有的全部 node_ids，不能只传这一个节点。
    let targetUserId: number
    let nextIds: string[]
    if (assignUserId.value === RECLAIM_TO_ADMIN) {
      // 收回管理员名下：从当前所有者的 node_ids 里去掉本节点，
      // 后端会把 removed 的节点 owner 置空（回到「管理员名下」）。
      const prevOwnerId = target.owner_user_id
      if (prevOwnerId == null) {
        assignError.value = '该节点已在管理员名下，无需收回'
        return
      }
      const prevOwner = rbac.users.find((u) => u.id === prevOwnerId)
      targetUserId = prevOwnerId
      nextIds = (prevOwner?.node_ids || []).filter((id) => id !== target.node_id)
    } else {
      targetUserId = assignUserId.value
      const user = rbac.users.find((u) => u.id === targetUserId)
      nextIds = Array.from(new Set([...(user?.node_ids || []), target.node_id]))
    }
    const result = await rbac.assignNodes(targetUserId, nextIds, confirm)
    await hub.fetchNodes(currentSearchOptions())
    const parts: string[] = []
    if (result.added.length) parts.push(`划入 ${result.added.length} 个`)
    if (result.removed.length) parts.push(`收回 ${result.removed.length} 个`)
    if (result.removed_memberships.length) parts.push(`移出 ${result.removed_memberships.length} 条分组成员`)
    ElMessage.success(parts.length ? `节点归属已更新（${parts.join('，')}）` : '节点归属未变化')
    showAssign.value = false
    assignMemberships.value = null
  } catch (e: unknown) {
    const conflict = assignConflict(e)
    if (conflict?.reason === 'active_tasks') {
      const names = conflict.nodes.map((n) => `${n.name || n.node_id}（${n.tasks} 个任务）`).join('、')
      assignError.value = `${conflict.message}：${names}`
      assignMemberships.value = null
    } else if (conflict?.reason === 'confirm_required') {
      assignMemberships.value = conflict
      assignError.value = conflict.message
    } else {
      assignError.value = errText(e, '授权失败，请稍后重试')
      assignMemberships.value = null
    }
  } finally {
    assignSaving.value = false
  }
}
</script>

<template>
  <div class="nodes-page">
    <!-- 页头 -->
    <div class="nodes-head">
      <div class="nodes-head-main">
        <p class="nodes-title">节点管理</p>
        <span v-if="someSelected" class="pf-count">已选 {{ selectedCount }} 个</span>
      </div>
      <div class="nodes-head-actions">
        <button
          type="button"
          class="pf-btn"
          :disabled="!someSelected || closing"
          @click="closeSelected"
        >
          {{ closing ? '下发中…' : `全部平仓${someSelected ? ` (${selectedCount})` : ''}` }}
        </button>
        <button v-if="auth.isAdmin" type="button" class="pf-btn pf-btn-primary" @click="openCreate">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
          新建节点
        </button>
      </div>
    </div>

    <!-- 节点总览（原型 stat 指标格）-->
    <section class="pf-card">
      <div class="nodes-overview-head">
        <p class="pf-section-title"><span class="pf-bar"></span>节点总览</p>
        <span class="nodes-online-pill">
          <span class="nodes-online-dot"></span>
          {{ onlineNodes }}/{{ totalNodes }} 在线
        </span>
      </div>
      <div class="nodes-metrics">
        <div class="pf-metric">
          <p class="pf-metric-k">节点总数</p>
          <p class="pf-metric-v">{{ totalNodes }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">在线节点</p>
          <p class="pf-metric-v">{{ onlineNodes }}<span class="nodes-metric-sub">/{{ totalNodes }}</span></p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">已启用</p>
          <p class="pf-metric-v">{{ enabledNodes }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">已禁用</p>
          <p class="pf-metric-v">{{ disabledNodes }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">待开通</p>
          <p class="pf-metric-v" :class="{ 'is-amber': pendingNodes > 0 }">{{ pendingNodes }}</p>
        </div>
      </div>
    </section>

    <!-- 搜索 + 分段筛选（原型 node-filter 条）-->
    <section class="pf-card">
      <div class="nodes-toolbar">
        <div class="nodes-search">
          <svg class="nodes-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-3.5-3.5" />
          </svg>
          <input
            v-model="searchQuery"
            class="pf-input nodes-search-input"
            type="search"
            placeholder="搜索节点名称 / 编号 / MT5 账号"
            aria-label="搜索节点名称 / 编号 / MT5 账号"
            :disabled="searching"
            @keydown.enter="runSearch"
          />
          <button type="button" class="pf-btn pf-btn-mini" :disabled="searching" @click="runSearch">
            {{ searching ? '搜索中…' : '搜索' }}
          </button>
        </div>
        <button type="button" class="pf-btn nodes-sort-btn" @click="cycleSort">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M7 4v16M7 20l-3-3M7 4L4 7M17 20V4M17 4l3 3M17 20l-3-3" />
          </svg>
          {{ sortLabel() }}
        </button>
      </div>

      <div class="pf-seg nodes-filter">
        <button
          v-for="opt in FILTER_OPTIONS"
          :key="opt.value"
          type="button"
          class="pf-seg-item"
          :class="{ active: filter === opt.value }"
          @click="filter = opt.value"
        >
          {{ opt.label }}
        </button>
      </div>

      <p v-if="appliedQuery && !hub.nodes.length" class="nodes-hint">无匹配节点</p>
      <p v-else-if="appliedQuery" class="nodes-hint">找到 {{ hub.nodes.length }} 个节点</p>
    </section>

    <!-- 全选 -->
    <div v-if="hub.nodes.length" class="nodes-select-all">
      <label class="nodes-select-label">
        <input type="checkbox" :checked="allSelected" @change="toggleSelectAll(($event.target as HTMLInputElement).checked)" />
        全选
      </label>
    </div>

    <!-- 按所有者分组的节点卡组（原型 node-item 列表）-->
    <div v-if="groupedNodes.length" class="nodes-groups">
      <section v-for="g in groupedNodes" :key="g.owner" class="nodes-group">
        <header class="nodes-group-head">
          <div class="nodes-group-owner">
            <span class="pf-owner-avatar">{{ g.owner.slice(0, 1) }}</span>
            <p class="pf-owner-title">{{ g.owner }}</p>
          </div>
          <div class="nodes-group-meta">
            <span class="nodes-group-count">{{ g.total }} 个节点</span>
            <span class="nodes-group-online">
              <span class="nodes-online-dot" :class="{ 'is-off': g.online === 0 }"></span>
              {{ g.online }}/{{ g.total }} 在线
            </span>
          </div>
        </header>

        <div class="nodes-group-body">
          <article
            v-for="n in g.items"
            :key="n.node_id"
            class="nodes-item"
            :class="{ 'is-selected': isSelected(n.node_id), 'is-primary': n.is_primary }"
          >
            <div class="nodes-item-top">
              <label class="nodes-item-check">
                <input
                  type="checkbox"
                  :checked="isSelected(n.node_id)"
                  @change="toggleSelect(n.node_id, ($event.target as HTMLInputElement).checked)"
                />
              </label>
              <span class="nodes-status-dot" :class="isOnline(n) ? 'is-on' : 'is-off'"></span>
              <a class="nodes-item-name" @click="goDetail(n)">{{ n.name }}</a>
              <span class="nodes-item-id">{{ n.node_id }}</span>
              <span v-if="n.is_primary" class="nodes-item-primary">
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M12 2 9.2 8.6 2 9.2l5.2 4.6L5.8 21 12 17.3 18.2 21l-1.4-7.2L22 9.2l-7.2-.6L12 2z" />
                </svg>
                主节点
              </span>
              <span class="nodes-item-state" :class="isOnline(n) ? 'is-on' : 'is-off'">
                {{ isOnline(n) ? '在线' : '离线' }}
              </span>
            </div>

            <div class="nodes-item-sub">
              <span class="nodes-item-meta">MT5 {{ n.mt5_login || '—' }}</span>
              <span class="nodes-item-meta nodes-item-server">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <rect x="3" y="4" width="18" height="7" rx="2" />
                  <path d="M6 15v3M10 15v3M14 15v3M18 15v3M3 20h18" stroke-linecap="round" />
                </svg>
                {{ n.mt5_server || '服务器未知' }}
              </span>
              <span class="nodes-item-meta">余额 <b>{{ fmtMoney(nodeBalance(n)) }}</b></span>
              <span class="nodes-item-meta">持仓 {{ nodePositions(n).count }} · {{ fmtVol(nodePositions(n).volume) }}手</span>
              <span class="nodes-item-meta">上报 {{ fmtLastSeen(n) }}</span>
            </div>

            <div class="nodes-item-actions">
              <button
                type="button"
                class="pf-btn pf-btn-mini"
                :class="nodeEnableIntent(n) ? '' : 'pf-btn-danger'"
                :disabled="isPendingNode(n) && !auth.isAdmin"
                @click="toggleEnabled(n)"
              >
                {{ nodeEnableLabel(n) }}
              </button>
              <button
                type="button"
                class="pf-btn pf-btn-mini"
                :class="n.is_primary ? 'pf-btn-primary' : ''"
                :title="n.is_primary ? '取消主节点标记' : '设为本账号的主节点'"
                @click="togglePrimary(n)"
                v-if="!n.is_primary || auth.isAdmin"
              >
                {{ n.is_primary ? '取消主节点' : '设为主节点' }}
              </button>
              <button v-if="auth.isAdmin" type="button" class="pf-btn pf-btn-mini" @click="openAssign(n)">
                授权
              </button>
              <button type="button" class="pf-btn pf-btn-mini" @click="goDetail(n)">详情</button>
              <button type="button" class="pf-btn pf-btn-mini" @click="openEdit(n)">编辑</button>
              <button
                v-if="auth.isAdmin || n.owner_user_id === auth.me?.user_id"
                type="button"
                class="pf-btn pf-btn-mini pf-btn-danger"
                @click="remove(n)"
              >
                删除
              </button>
            </div>

            <p v-if="isPendingNode(n)" class="nodes-item-hint">{{ nodeApprovalHint(n) }}</p>
          </article>
        </div>
      </section>
    </div>

    <div v-else-if="!searching" class="pf-card">
      <p class="pf-empty">
        {{ !totalNodes ? (appliedQuery ? '无匹配节点' : '暂无节点') : '当前筛选条件下没有节点' }}
      </p>
    </div>
  </div>

  <!-- create / edit modal：头/身/底三段，底部操作栏固定，避免 iPhone Safari 底栏遮挡 -->
  <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
    <div class="card card-pad modal modal-lg node-form-modal">
      <div class="modal-header">
        <div class="h1">{{ formMode === 'create' ? '新建节点' : '编辑节点' }}</div>
        <p v-if="formMode === 'create'" class="muted" style="font-size: 12px; margin: 4px 0 0">
          提示：若 node_client 用同一 MT5 登录号首次连接，系统会自动注册（默认禁用，需在此启用后才会上线）；手动新建默认启用。
        </p>
      </div>
      <div class="modal-body">
        <div class="form-grid two">
          <div>
            <FormLabel field-id="node-name" text="名称" :help="NODE_FORM_FIELD_HELP.name" />
            <input id="node-name" v-model="form.name" :placeholder="form.mt5_login ? `留空将自动生成：${hub.nodes.length + 1}-${form.mt5_login}` : '留空将自动生成 {序号}-{mt5_login}'" />
          </div>
          <div v-if="formMode === 'create'">
            <FormLabel field-id="node-mt5-login" text="MT5 账户登录号" :help="NODE_FORM_FIELD_HELP.mt5_login" />
            <input id="node-mt5-login" v-model.number="form.mt5_login" type="number" step="1" min="1" placeholder="例如：60108484" />
          </div>
          <div v-else>
            <FormLabel field-id="node-mt5-login-readonly" text="MT5 账户登录号" :help="NODE_FORM_FIELD_HELP.mt5_login" />
            <input id="node-mt5-login-readonly" :value="form.mt5_login ?? ''" type="number" disabled />
            <p class="muted" style="font-size: 12px; margin: 6px 0 0">创建后不可修改</p>
          </div>
          <div v-if="formMode === 'edit'">
            <FormLabel field-id="node-enabled" text="启用状态" :help="NODE_FORM_FIELD_HELP.enabled" />
            <TagSelect v-if="!editingNode || !isPendingNode(editingNode) || auth.isAdmin" id="node-enabled" v-model="form.enabled" :options="ENABLED_TAGS" aria-label="启用状态" />
            <p v-if="editingNode && isPendingNode(editingNode)" class="muted" style="font-size: 12px; margin: 6px 0 0">{{ nodeApprovalHint(editingNode) }}</p>
          </div>
          <div v-if="auth.isAdmin" class="span-full">
            <FormLabel text="按币种配置" :help="NODE_FORM_FIELD_HELP.filters" />
            <div class="node-filter-scroll">
              <FilterRulesEditor v-model="form.filters" mode="node" />
            </div>
          </div>
          <div v-if="createError" class="span-full" style="color: var(--red); font-size: 13px">{{ createError }}</div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-ghost" @click="showForm = false">取消</button>
        <button
          class="btn-primary"
          :disabled="saving || (formMode === 'create' && !form.mt5_login)"
          @click="save"
        >{{ saving ? '保存中…' : '保存' }}</button>
      </div>
    </div>
  </div>

  <!-- 管理员授权：把节点划给某个用户（复用用户权限页的 assign_nodes 接口）-->
  <div v-if="showAssign && assignTarget" class="modal-mask" @click.self="showAssign = false">
    <div class="card card-pad modal nodes-assign-modal">
      <div class="modal-header">
        <div class="h1">授权节点</div>
        <p class="muted" style="font-size: 12px; margin: 4px 0 0">
          把节点「{{ assignTarget.name }}」划给指定用户，该用户即可在自己的节点列表中看到并使用它。
        </p>
      </div>
      <div class="modal-body">
        <FormLabel text="授权给" :help="'节点当前归属：' + (assignTarget.owner_username || '管理员名下')" />
        <select v-model.number="assignUserId" class="pf-input nodes-assign-select">
          <option :value="null" disabled>请选择授权方式</option>
          <option v-if="assignTarget.owner_user_id != null" :value="-1">
            收回至管理员名下
          </option>
          <option v-for="u in assignCandidates" :key="u.id" :value="u.id">
            {{ u.display_name || u.username }}（{{ u.username }}）· {{ u.node_ids.length }} 个节点
          </option>
        </select>

        <div v-if="assignMemberships" class="pf-note warn nodes-assign-confirm">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 16v-4M12 8h.01" stroke-linecap="round" />
          </svg>
          <p>
            {{ assignMemberships.message }}
            <br />
            <span class="nodes-assign-confirm-list">
              {{ assignMemberships.memberships.map((m) => `${m.node_name || m.node_id} → ${m.group_name || m.group_id}`).join('、') }}
            </span>
          </p>
        </div>
        <p v-if="assignError" class="pf-error">{{ assignError }}</p>
      </div>
      <div class="modal-footer">
        <button class="btn-ghost" :disabled="assignSaving" @click="showAssign = false">取消</button>
        <button
          class="btn-primary"
          :disabled="assignSaving || assignUserId == null"
          @click="submitAssign(Boolean(assignMemberships))"
        >{{ assignSaving ? '提交中…' : (assignMemberships ? '确认移出分组并授权' : '确认授权') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── 节点管理页 —— 1:1 复刻 UI/H5.html 原型「节点管理」版式
   卡片 / 指标格 / 分段筛选 / 分组卡 / 按钮等共用外观见 styles.css 的 pf- 前缀类，
   这里只放本页独有的部分。 */
.nodes-page {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 页头 ── */
.nodes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.nodes-head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.nodes-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--pf-text);
}
.nodes-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* ── 总览 ── */
.nodes-overview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.nodes-online-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-muted);
  font-size: 10px;
  white-space: nowrap;
}
.nodes-online-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--pf-green);
  animation: nodes-pulse 2s ease-in-out infinite;
}
.nodes-online-dot.is-off {
  background: #4a5568;
  animation: none;
}
@keyframes nodes-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.nodes-metrics {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
@media (min-width: 640px) {
  .nodes-metrics { grid-template-columns: repeat(5, minmax(0, 1fr)); }
}
.nodes-metric-sub {
  font-size: 10px;
  font-weight: 400;
  color: var(--pf-dim);
  margin-left: 2px;
}
.is-amber { color: #fbbf24; }

/* ── 搜索 + 排序 ── */
.nodes-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.nodes-search {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.nodes-search-icon {
  position: absolute;
  left: 9px;
  width: 14px;
  height: 14px;
  color: var(--pf-dim);
  pointer-events: none;
}
.nodes-search-input {
  flex: 1 1 auto;
  min-width: 0;
  width: auto;
  height: 32px;
  padding-left: 28px;
  padding-right: 8px;
}
.nodes-search .pf-btn { flex: 0 0 auto; }
.nodes-sort-btn { flex: 0 0 auto; }

/* ── 分段筛选 ── */
.nodes-filter {
  margin-top: 10px;
}
.nodes-filter .pf-seg-item {
  flex: 1 1 0;
  text-align: center;
  padding: 6px 8px;
}

/* ── 全选 ── */
.nodes-select-all {
  display: flex;
  align-items: center;
  gap: 6px;
}
.nodes-select-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 11px;
  color: var(--pf-muted);
  text-transform: none;
  letter-spacing: 0;
  cursor: pointer;
}

.nodes-hint {
  margin: 8px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
}

/* ── 分组卡 ── */
.nodes-groups {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.nodes-group {
  border-radius: 12px;
  border: 1px solid var(--pf-border);
  background: rgba(12, 18, 32, 0.6);
  overflow: hidden;
}
.nodes-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--pf-border);
  background: rgba(16, 24, 40, 0.8);
}
.nodes-group-owner {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.nodes-group-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: var(--pf-dim);
  white-space: nowrap;
}
.nodes-group-count {
  color: var(--pf-muted);
}
.nodes-group-online {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--pf-soft);
  color: var(--pf-muted);
}
.nodes-group-body {
  display: grid;
  gap: 6px;
  padding: 8px;
}

/* ── 单个节点卡 ── */
.nodes-item {
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  padding: 8px 10px;
  transition: border-color 0.15s, background 0.15s;
}
.nodes-item:hover {
  border-color: #2b3a5c;
  background: #111a2c;
}
.nodes-item.is-selected {
  border-color: rgba(37, 99, 235, 0.6);
  background: rgba(37, 99, 235, 0.08);
}
.nodes-item.is-primary {
  border-color: rgba(245, 158, 11, 0.45);
}

.nodes-item-top {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.nodes-item-check {
  display: inline-flex;
  align-items: center;
  margin: 0;
  flex-shrink: 0;
}
.nodes-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.nodes-status-dot.is-on {
  background: var(--pf-green);
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.6);
}
.nodes-status-dot.is-off { background: #4a5568; }

.nodes-item-name {
  color: var(--pf-text);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nodes-item-name:hover { color: #60a5fa; }
.nodes-item-id {
  font-size: 10px;
  color: var(--pf-dim);
  font-family: var(--mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nodes-item-primary {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  font-size: 9px;
  font-weight: 700;
  white-space: nowrap;
}
.nodes-item-primary svg { width: 10px; height: 10px; }
.nodes-item-state {
  flex-shrink: 0;
  margin-left: auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 500;
  white-space: nowrap;
}
.nodes-item-state.is-on {
  background: rgba(34, 197, 94, 0.15);
  color: var(--pf-green);
}
.nodes-item-state.is-off {
  background: rgba(107, 122, 147, 0.15);
  color: var(--pf-muted);
}

.nodes-item-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  margin-top: 6px;
  font-size: 10px;
  color: var(--pf-muted);
}
.nodes-item-meta {
  white-space: nowrap;
}
.nodes-item-meta b {
  color: #c7d2e5;
  font-weight: 600;
  font-family: var(--mono);
}
.nodes-item-server {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nodes-item-server svg {
  width: 11px;
  height: 11px;
  flex-shrink: 0;
  color: #3d4c66;
}

.nodes-item-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.nodes-item-hint {
  margin: 6px 0 0;
  font-size: 10px;
  line-height: 1.5;
  color: #fbbf24;
}

/* ── 授权弹窗 ── */
.nodes-assign-modal { max-width: 420px; }
.nodes-assign-select {
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--pf-dim) 50%),
    linear-gradient(135deg, var(--pf-dim) 50%, transparent 50%);
  background-position: calc(100% - 14px) 15px, calc(100% - 10px) 15px;
  background-size: 4px 4px, 4px 4px;
  background-repeat: no-repeat;
  padding-right: 26px;
}
.nodes-assign-confirm { margin-top: 12px; }
.nodes-assign-confirm-list {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  color: #d9b36a;
  word-break: break-all;
}

/* 币种配置区随弹窗 body 滚动；桌面仍可限高，手机交给 modal-body */
.node-filter-scroll {
  margin-top: 4px;
}
@media (min-width: 769px) {
  .node-filter-scroll {
    max-height: clamp(260px, 45vh, 480px);
    overflow-y: auto;
  }
}
@media (max-width: 768px) {
  .node-form-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .node-filter-scroll {
    max-height: none;
    overflow: visible;
  }
  .modal-footer .btn-ghost,
  .modal-footer .btn-primary {
    flex: 1;
    min-height: 44px;
  }
  .nodes-head-actions {
    width: 100%;
  }
  .nodes-head-actions .pf-btn { flex: 1 1 0; }
}
</style>
