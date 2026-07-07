<script setup lang="ts">
// 节点管理页：创建/编辑/删除节点、启停、重置令牌、批量全平（令牌仅创建时显示一次）
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import FormLabel from '@/components/FormLabel.vue'
import FilterRulesEditor from '@/components/FilterRulesEditor.vue'
import { NODE_FORM_FIELD_HELP } from '@/constants/nodeFormHelp'
import { useHubStore } from '@/stores/hub'
import type { NodeDispatchFiltersConfig, NodeOut } from '@/api/types'
import { parseFilterRules, parseNodeDispatchFilters, serializeNodeDispatchFilters, validateNodeDispatchFilters, validateNodeGlobalLotMode } from '@/utils/filterRules'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()
const router = useRouter()

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
const saving = ref(false)
const createError = ref('')

// 默认值与「自动注册」保持一致：启用；按币种配置见 filters
const form = reactive({
  name: '',
  mt5_login: null as number | null,
  enabled: true,
  filters: {} as NodeDispatchFiltersConfig,
})

function filterSymbolCount(n: NodeOut): string {
  const count = Object.keys(parseNodeDispatchFilters(n.filters)).length
  return count ? `${count} 个品种` : '—'
}

const selectedIds = ref<Set<string>>(new Set())
const closing = ref(false)

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
  if (!(await confirmAction(`确认对以下 ${nodes.length} 个节点执行全部平仓？\n\n${names}\n\n此操作不可撤销。`, '确认平仓'))) return
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
    enabled: n.enabled,
    filters: parseNodeDispatchFilters(n.filters),
  })
  showForm.value = true
  void ensureGlobalFiltersLoaded()
}

async function save(): Promise<void> {
  saving.value = true
  createError.value = ''
  try {
    await ensureGlobalFiltersLoaded()
    const filtersPayload = serializeNodeDispatchFilters(form.filters)
    const globalRules = parseFilterRules(hub.filters)
    const filterErrors = [
      ...validateNodeDispatchFilters(filtersPayload),
      ...validateNodeGlobalLotMode(filtersPayload, globalRules),
    ]
    if (filterErrors.length) {
      createError.value = filterErrors[0]
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
        return
      }
    } else {
      const label = form.name || editingId.value
      const enabledText = form.enabled ? '启用' : '禁用'
      if (!(await confirmAction(`确认更新节点「${label}」？\n\n启用状态：${enabledText}`))) return
      await hub.updateNode(editingId.value, { ...payload, enabled: form.enabled }, currentSearchOptions())
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

async function remove(n: NodeOut): Promise<void> {
  if (!(await confirmAction(`确认删除节点「${n.name}」？\n\n该操作不可恢复。`, '确认删除'))) return
  await hub.deleteNode(n.node_id, currentSearchOptions())
}

async function toggleEnabled(n: NodeOut): Promise<void> {
  const next = n.enabled ? '禁用' : '启用'
  if (!(await confirmAction(`确认${next}节点「${n.name}」？\n\n${next}后将${n.enabled ? '无法接入且不参与分发' : '恢复正常跟单'}。`))) return
  await hub.updateNode(n.node_id, { enabled: !n.enabled }, currentSearchOptions())
}
</script>

<template>
  <div class="row between page-header">
    <div>
      <div class="h1">节点管理</div>
      <div v-if="someSelected" class="muted" style="font-size: 12px; margin-top: 4px">已选 {{ selectedCount }} 个节点</div>
    </div>
    <div class="row">
      <button
        class="btn-danger btn-sm"
        :disabled="!someSelected || closing"
        @click="closeSelected"
      >
        {{ closing ? '下发中…' : `全部平仓${someSelected ? ` (${selectedCount})` : ''}` }}
      </button>
      <button class="btn-primary" @click="openCreate">+ 新建节点</button>
    </div>
  </div>

  <div class="card card-pad node-search-bar" style="margin-bottom: 12px">
    <div class="row" style="gap: 8px">
      <input
        v-model="searchQuery"
        type="search"
        placeholder="搜索节点名称或 MT5 账号…"
        aria-label="搜索节点名称或 MT5 账号"
        :disabled="searching"
        style="width: 25%"
        @keydown.enter="runSearch"
      />
      <button class="btn-primary btn-sm" :disabled="searching" @click="runSearch">
        {{ searching ? '搜索中…' : '搜索' }}
      </button>
    </div>
    <p v-if="appliedQuery && !hub.nodes.length" class="muted" style="font-size: 12px; margin: 8px 0 0">
      无匹配节点
    </p>
    <p v-else-if="appliedQuery" class="muted" style="font-size: 12px; margin: 8px 0 0">
      找到 {{ hub.nodes.length }} 个节点
    </p>
  </div>

  <div v-if="hub.nodes.length" class="row mobile-only" style="margin-bottom: 10px">
    <label class="row" style="gap: 6px; font-size: 13px; cursor: pointer">
      <input type="checkbox" :checked="allSelected" @change="toggleSelectAll(($event.target as HTMLInputElement).checked)" />
      全选
    </label>
  </div>

  <div class="list-cards mobile-only">
    <div v-for="n in hub.nodes" :key="n.node_id" class="list-card card" :class="{ 'row-selected': isSelected(n.node_id) }">
      <div class="list-card-head row between">
        <div class="row" style="gap: 8px">
          <input
            type="checkbox"
            :checked="isSelected(n.node_id)"
            @change="toggleSelect(n.node_id, ($event.target as HTMLInputElement).checked)"
          />
          <span class="dot" :class="hub.statuses[n.node_id] || n.status"></span>
          <a class="node-link" @click="goDetail(n)">{{ n.name }}</a>
        </div>
        <span class="tag" :class="(hub.statuses[n.node_id] || n.status) === 'online' ? 'green' : ''">
          {{ (hub.statuses[n.node_id] || n.status) === 'online' ? '在线' : '离线' }}
        </span>
      </div>
      <div class="list-field"><span class="k">节点 ID</span><span class="v muted" style="font-weight: 500; font-size: 12px">{{ n.node_id }}</span></div>
      <div class="list-field"><span class="k">MT5 账号</span><span class="v">{{ n.mt5_login || '—' }}</span></div>
      <div class="list-field"><span class="k">MT5 服务器</span><span class="v">{{ n.mt5_server || '—' }}</span></div>
      <div class="list-field"><span class="k">分发配置</span><span class="v muted">{{ filterSymbolCount(n) }}</span></div>
      <div class="list-field">
        <span class="k">启用</span>
        <span class="v">
          <button class="btn-sm" :class="n.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(n)">
            {{ n.enabled ? '已启用' : '已禁用' }}
          </button>
        </span>
      </div>
      <div class="list-card-actions">
        <button class="btn-sm btn-ghost" @click="goDetail(n)">详情</button>
        <button class="btn-sm btn-ghost" @click="openEdit(n)">编辑</button>
        <button class="btn-sm btn-danger" @click="remove(n)">删除</button>
      </div>
    </div>
    <div v-if="!hub.nodes.length && !searching" class="card card-pad muted">
      {{ appliedQuery ? '无匹配节点' : '暂无节点' }}
    </div>
  </div>

  <div class="card table-scroll desktop-only">
    <table>
      <thead>
        <tr>
          <th style="width: 36px">
            <input
              type="checkbox"
              :checked="allSelected"
              @change="toggleSelectAll(($event.target as HTMLInputElement).checked)"
            />
          </th>
          <th>状态</th><th>名称</th><th>MT5</th><th>币种配置</th><th>启用</th><th class="right">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="n in hub.nodes" :key="n.node_id" :class="{ 'row-selected': isSelected(n.node_id) }">
          <td>
            <input
              type="checkbox"
              :checked="isSelected(n.node_id)"
              @change="toggleSelect(n.node_id, ($event.target as HTMLInputElement).checked)"
            />
          </td>
          <td><span class="dot" :class="hub.statuses[n.node_id] || n.status"></span></td>
          <td>
            <a class="node-link" @click="goDetail(n)">{{ n.name }}</a>
            <div class="muted" style="font-size: 11px">{{ n.node_id }}</div>
          </td>
          <td class="muted" style="font-size: 12px">{{ n.mt5_login || '—' }}<br />{{ n.mt5_server || '' }}</td>
          <td class="muted" style="font-size: 12px">{{ filterSymbolCount(n) }}</td>
          <td>
            <button class="btn-sm" :class="n.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(n)">
              {{ n.enabled ? '已启用' : '已禁用' }}
            </button>
          </td>
          <td class="right">
            <button class="btn-sm btn-ghost" @click="goDetail(n)">详情</button>
            <button class="btn-sm btn-ghost" @click="openEdit(n)">编辑</button>
            <button class="btn-sm btn-danger" @click="remove(n)">删除</button>
          </td>
        </tr>
        <tr v-if="!hub.nodes.length && !searching">
          <td colspan="7" class="muted" style="padding: 18px">
            {{ appliedQuery ? '无匹配节点' : '暂无节点' }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- create / edit modal -->
  <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
    <div class="card card-pad modal">
      <div class="h1">{{ formMode === 'create' ? '新建节点' : '编辑节点' }}</div>
      <p v-if="formMode === 'create'" class="muted" style="font-size: 12px; margin: 4px 0 10px">
        提示：若 node_client 用同一 MT5 登录号首次连接，系统会自动注册（默认配置同此表单），通常无需在此手动新建。
      </p>
      <div class="form-grid">
        <div>
          <FormLabel field-id="node-name" text="名称" :help="NODE_FORM_FIELD_HELP.name" />
          <input id="node-name" v-model="form.name" :placeholder="form.mt5_login ? `留空将自动生成：node-${form.mt5_login}` : '留空将自动生成 node-{mt5_login}'" />
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
        <div class="span-full">
          <FormLabel text="按币种配置" :help="NODE_FORM_FIELD_HELP.filters" />
          <FilterRulesEditor v-model="form.filters" mode="node" />
        </div>
        <div v-if="formMode === 'edit'">
          <FormLabel field-id="node-enabled" text="启用状态" :help="NODE_FORM_FIELD_HELP.enabled" />
          <select id="node-enabled" v-model="form.enabled"><option :value="true">启用</option><option :value="false">禁用</option></select>
        </div>
        <div v-if="createError" style="color: var(--red); font-size: 13px">{{ createError }}</div>
        <div class="row between" style="margin-top: 6px">
          <button class="btn-ghost" @click="showForm = false">取消</button>
          <button
            class="btn-primary"
            :disabled="saving || (formMode === 'create' && !form.mt5_login)"
            @click="save"
          >{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
