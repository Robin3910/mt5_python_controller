<script setup lang="ts">
// 节点管理页：创建/编辑/删除节点、启停、重置令牌、批量全平（令牌仅创建时显示一次）
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import FormLabel from '@/components/FormLabel.vue'
import { NODE_FORM_FIELD_HELP } from '@/constants/nodeFormHelp'
import { useHubStore } from '@/stores/hub'
import type { NodeOut } from '@/api/types'

const hub = useHubStore()
const router = useRouter()
onMounted(() => hub.fetchNodes())

function goDetail(n: NodeOut): void {
  router.push(`/nodes/${n.node_id}`)
}

const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const saving = ref(false)

const form = reactive({
  name: '',
  mt5_login: null as number | null,
  lot_mode: 'global' as 'global' | 'fixed' | 'signal',
  lot: 0.1 as number | null,
  follow_sync: true,
  follow_poll: true,
  poll_order: 0,
  enabled: true,
})

const tokenModal = reactive({ show: false, token: '', title: '' })
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
  selectedIds.value = checked ? new Set(hub.nodes.map((n) => n.node_id)) : new Set()
}

async function closeSelected(): Promise<void> {
  const nodes = hub.nodes.filter((n) => selectedIds.value.has(n.node_id))
  if (!nodes.length) return
  const names = nodes.map((n) => `· ${n.name} (${n.node_id})`).join('\n')
  if (!confirm(`确认对以下 ${nodes.length} 个节点执行全部平仓？\n\n${names}\n\n此操作不可撤销。`)) return
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

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  Object.assign(form, { name: '', mt5_login: null, lot_mode: 'global', lot: 0.1, follow_sync: true, follow_poll: true, poll_order: 0, enabled: true })
  showForm.value = true
}

function openEdit(n: NodeOut): void {
  formMode.value = 'edit'
  editingId.value = n.node_id
  Object.assign(form, {
    name: n.name,
    mt5_login: n.mt5_login,
    lot_mode: n.lot_mode,
    lot: n.lot ?? 0.1,
    follow_sync: n.follow_sync,
    follow_poll: n.follow_poll,
    poll_order: n.poll_order,
    enabled: n.enabled,
  })
  showForm.value = true
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const payload = {
      name: form.name,
      lot_mode: form.lot_mode,
      lot: form.lot_mode === 'fixed' ? form.lot : null,
      follow_sync: form.follow_sync,
      follow_poll: form.follow_poll,
      poll_order: form.poll_order,
    }
    if (formMode.value === 'create') {
      if (!form.mt5_login) return
      const created = await hub.createNode({ ...payload, mt5_login: form.mt5_login })
      tokenModal.token = created.token
      tokenModal.title = '节点创建成功 — 请保存令牌（只显示一次）'
      tokenModal.show = true
    } else {
      await hub.updateNode(editingId.value, { ...payload, enabled: form.enabled })
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

async function remove(n: NodeOut): Promise<void> {
  if (!confirm(`确认删除节点「${n.name}」？该操作不可恢复。`)) return
  await hub.deleteNode(n.node_id)
}

async function doRotate(n: NodeOut): Promise<void> {
  if (!confirm(`重置节点「${n.name}」的令牌？旧令牌将立即失效。`)) return
  const res = await hub.rotateToken(n.node_id)
  tokenModal.token = res.token
  tokenModal.title = '新令牌（只显示一次）'
  tokenModal.show = true
}

async function toggleEnabled(n: NodeOut): Promise<void> {
  await hub.updateNode(n.node_id, { enabled: !n.enabled })
}

function copyToken(): void {
  navigator.clipboard?.writeText(tokenModal.token)
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
      <div class="list-field">
        <span class="k">手数策略</span>
        <span class="v">
          <span class="tag blue">{{ n.lot_mode }}</span>
          <span v-if="n.lot_mode === 'fixed'"> {{ n.lot }}</span>
        </span>
      </div>
      <div class="list-field">
        <span class="k">跟单</span>
        <span class="v">
          <span class="tag" :class="n.follow_sync ? 'green' : ''">同步</span>
          <span class="tag" :class="n.follow_poll ? 'green' : ''">轮询</span>
        </span>
      </div>
      <div class="list-field"><span class="k">轮询顺序</span><span class="v">{{ n.poll_order }}</span></div>
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
        <button class="btn-sm btn-ghost" @click="doRotate(n)">令牌</button>
        <button class="btn-sm btn-danger" @click="remove(n)">删除</button>
      </div>
    </div>
    <div v-if="!hub.nodes.length" class="card card-pad muted">暂无节点</div>
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
          <th>状态</th><th>名称</th><th>MT5</th><th>手数策略</th><th>跟单</th><th>轮询序</th><th>启用</th><th class="right">操作</th>
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
          <td>
            <span class="tag blue">{{ n.lot_mode }}</span>
            <span v-if="n.lot_mode === 'fixed'"> {{ n.lot }}</span>
          </td>
          <td>
            <span class="tag" :class="n.follow_sync ? 'green' : ''">同步</span>
            <span class="tag" :class="n.follow_poll ? 'green' : ''">轮询</span>
          </td>
          <td>{{ n.poll_order }}</td>
          <td>
            <button class="btn-sm" :class="n.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(n)">
              {{ n.enabled ? '已启用' : '已禁用' }}
            </button>
          </td>
          <td class="right">
            <button class="btn-sm btn-ghost" @click="goDetail(n)">详情</button>
            <button class="btn-sm btn-ghost" @click="openEdit(n)">编辑</button>
            <button class="btn-sm btn-ghost" @click="doRotate(n)">令牌</button>
            <button class="btn-sm btn-danger" @click="remove(n)">删除</button>
          </td>
        </tr>
        <tr v-if="!hub.nodes.length"><td colspan="9" class="muted" style="padding: 18px">暂无节点</td></tr>
      </tbody>
    </table>
  </div>

  <!-- create / edit modal -->
  <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
    <div class="card card-pad modal">
      <div class="h1">{{ formMode === 'create' ? '新建节点' : '编辑节点' }}</div>
      <div class="form-grid">
        <div>
          <FormLabel field-id="node-name" text="名称" :help="NODE_FORM_FIELD_HELP.name" />
          <input id="node-name" v-model="form.name" placeholder="例如：东京-VPS-01" />
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
        <div class="form-grid two">
          <div>
            <FormLabel field-id="node-lot-mode" text="手数策略" :help="NODE_FORM_FIELD_HELP.lot_mode" />
            <select id="node-lot-mode" v-model="form.lot_mode">
              <option value="global">跟随全局</option>
              <option value="fixed">固定手数</option>
              <option value="signal">跟随信号</option>
            </select>
          </div>
          <div>
            <FormLabel field-id="node-lot" text="固定手数" :help="NODE_FORM_FIELD_HELP.lot" />
            <input id="node-lot" v-model.number="form.lot" type="number" step="0.01" :disabled="form.lot_mode !== 'fixed'" />
          </div>
        </div>
        <div class="form-grid two">
          <div>
            <FormLabel field-id="node-follow-sync" text="跟随同步模式" :help="NODE_FORM_FIELD_HELP.follow_sync" />
            <select id="node-follow-sync" v-model="form.follow_sync"><option :value="true">是</option><option :value="false">否</option></select>
          </div>
          <div>
            <FormLabel field-id="node-follow-poll" text="跟随轮询模式" :help="NODE_FORM_FIELD_HELP.follow_poll" />
            <select id="node-follow-poll" v-model="form.follow_poll"><option :value="true">是</option><option :value="false">否</option></select>
          </div>
        </div>
        <div>
          <FormLabel field-id="node-poll-order" text="轮询顺序（越小越先）" :help="NODE_FORM_FIELD_HELP.poll_order" />
          <input id="node-poll-order" v-model.number="form.poll_order" type="number" />
        </div>
        <div v-if="formMode === 'edit'">
          <FormLabel field-id="node-enabled" text="启用状态" :help="NODE_FORM_FIELD_HELP.enabled" />
          <select id="node-enabled" v-model="form.enabled"><option :value="true">启用</option><option :value="false">禁用</option></select>
        </div>
        <div class="row between" style="margin-top: 6px">
          <button class="btn-ghost" @click="showForm = false">取消</button>
          <button
            class="btn-primary"
            :disabled="saving || !form.name || (formMode === 'create' && !form.mt5_login)"
            @click="save"
          >{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>

  <!-- token modal -->
  <div v-if="tokenModal.show" class="modal-mask" @click.self="tokenModal.show = false">
    <div class="card card-pad modal">
      <div class="h1">{{ tokenModal.title }}</div>
      <p class="muted" style="font-size: 12px">将此令牌填入对应节点的 <code>.env</code> 的 <code>NODE_TOKEN</code>。</p>
      <div class="token-box">{{ tokenModal.token }}</div>
      <div class="row between" style="margin-top: 14px">
        <button class="btn-ghost" @click="copyToken">复制</button>
        <button class="btn-primary" @click="tokenModal.show = false">我已保存</button>
      </div>
    </div>
  </div>
</template>
