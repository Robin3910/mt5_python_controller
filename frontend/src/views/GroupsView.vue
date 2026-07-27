<script setup lang="ts">
// 分组管理页：strategy 信号（Webhook model=strategy）的分发单元
// 新建/编辑/删除分组、启停、维护成员节点、设置分组级分发模式，并查看该分组处理过的信号明细
import { computed, onMounted, ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useHubStore } from '@/stores/hub'
import type {
  GroupDispatchMode,
  GroupOut,
  GroupSignalTaskRecord,
  NodeOut,
} from '@/api/types'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()
const router = useRouter()

const searchQuery = ref('')
const appliedQuery = ref('')
const loading = ref(false)

function currentSearchOptions(): { q?: string } {
  return appliedQuery.value ? { q: appliedQuery.value } : {}
}

async function loadGroups(): Promise<void> {
  loading.value = true
  try {
    await hub.fetchGroups(currentSearchOptions())
  } finally {
    loading.value = false
  }
}

async function runSearch(): Promise<void> {
  appliedQuery.value = searchQuery.value.trim()
  await loadGroups()
}

onMounted(async () => {
  await Promise.all([loadGroups(), hub.fetchNodes()])
})

const DISPATCH_MODE_LABEL: Record<GroupDispatchMode, string> = {
  sync: '全员同步',
  poll: '轮询轮转（单节点领取）',
}

const FIELD_HELP = {
  name: '分组名称，全局唯一。会展示在信号明细与操作审计中，便于追溯是哪个分组下发的订单。',
  enabled:
    '禁用后该分组不再接收任何 strategy 信号；已下发的历史任务不受影响。',
  dispatch_mode:
    '分组级分发模式，作用于整个分组、不区分币种：全员同步 = 组内所有有效节点并发下发；轮询轮转 = 一条信号只交给组内队首的一个有效节点，成功后该节点移到队尾。',
  remark: '备注，仅用于后台展示。',
  nodes:
    '加入本分组的节点。信号进入时只有「已启用且在线」的成员才算有效节点；勾选顺序即轮询轮转的初始顺序。',
}

// ---- 新建 / 编辑 ----
const showForm = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const saving = ref(false)
const formError = ref('')

const form = reactive({
  name: '',
  enabled: true,
  dispatch_mode: 'sync' as GroupDispatchMode,
  remark: '',
  node_ids: [] as string[],
})

// 成员选择：已选节点按选择顺序排列（即轮询顺序），其余节点排在后面
const memberNodes = computed<NodeOut[]>(() =>
  form.node_ids
    .map((id) => hub.nodes.find((n) => n.node_id === id))
    .filter((n): n is NodeOut => !!n),
)
const availableNodes = computed<NodeOut[]>(() =>
  hub.nodes.filter((n) => !form.node_ids.includes(n.node_id)),
)

function addMember(nodeId: string): void {
  if (nodeId && !form.node_ids.includes(nodeId)) form.node_ids.push(nodeId)
}
function removeMember(nodeId: string): void {
  form.node_ids = form.node_ids.filter((id) => id !== nodeId)
}
function moveMember(index: number, delta: number): void {
  const next = index + delta
  if (next < 0 || next >= form.node_ids.length) return
  const list = [...form.node_ids]
  ;[list[index], list[next]] = [list[next], list[index]]
  form.node_ids = list
}

function openCreate(): void {
  formMode.value = 'create'
  editingId.value = ''
  formError.value = ''
  Object.assign(form, {
    name: '',
    enabled: true,
    dispatch_mode: 'sync' as GroupDispatchMode,
    remark: '',
    node_ids: [],
  })
  showForm.value = true
}

function openEdit(g: GroupOut): void {
  formMode.value = 'edit'
  editingId.value = g.group_id
  formError.value = ''
  Object.assign(form, {
    name: g.name,
    enabled: g.enabled,
    dispatch_mode: g.dispatch_mode,
    remark: g.remark || '',
    node_ids: g.nodes.map((n) => n.node_id),
  })
  showForm.value = true
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) {
    formError.value = '请填写分组名称'
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const payload = {
      name,
      enabled: form.enabled,
      dispatch_mode: form.dispatch_mode,
      remark: form.remark.trim() || null,
      node_ids: form.node_ids,
    }
    const summary = `分发模式：${DISPATCH_MODE_LABEL[form.dispatch_mode]}\n成员节点：${form.node_ids.length} 个`
    const verb = formMode.value === 'create' ? '创建' : '更新'
    if (!(await confirmAction(`确认${verb}分组「${name}」？\n\n${summary}`))) return
    try {
      if (formMode.value === 'create') {
        await hub.createGroup(payload, currentSearchOptions())
      } else {
        await hub.updateGroup(editingId.value, payload, currentSearchOptions())
      }
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } }
      formError.value = err?.response?.data?.detail || `${verb}失败，请稍后重试`
      await ElMessageBox.alert(formError.value, '无法保存', {
        type: 'warning',
        confirmButtonText: '知道了',
      })
      return
    }
    showForm.value = false
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(g: GroupOut): Promise<void> {
  const next = g.enabled ? '禁用' : '启用'
  const effect = g.enabled ? '不再接收任何 strategy 信号' : '恢复接收 strategy 信号'
  if (!(await confirmAction(`确认${next}分组「${g.name}」？\n\n${next}后该分组将${effect}。`))) return
  await hub.updateGroup(g.group_id, { enabled: !g.enabled }, currentSearchOptions())
}

async function remove(g: GroupOut): Promise<void> {
  if (!(await confirmAction(`确认删除分组「${g.name}」？\n\n该操作不可恢复（历史信号任务记录会保留）。`, '确认删除'))) return
  await hub.deleteGroup(g.group_id, currentSearchOptions())
}

// ---- 信号明细弹窗 ----
const showSignals = ref(false)
const signalGroup = ref<GroupOut | null>(null)
const signals = ref<GroupSignalTaskRecord[]>([])
const signalPage = ref(1)
const signalPageSize = ref(20)
const signalTotal = ref(0)
const loadingSignals = ref(false)
const expanded = ref<Record<string, boolean>>({})

const signalTotalPages = computed(() =>
  Math.max(1, Math.ceil(signalTotal.value / signalPageSize.value)),
)

async function openSignals(g: GroupOut): Promise<void> {
  signalGroup.value = g
  showSignals.value = true
  signalPage.value = 1
  expanded.value = {}
  await loadSignals()
}

async function loadSignals(): Promise<void> {
  if (!signalGroup.value) return
  loadingSignals.value = true
  try {
    const res = await hub.fetchGroupSignals(
      signalGroup.value.group_id,
      signalPage.value,
      signalPageSize.value,
    )
    signals.value = res.items
    signalTotal.value = res.total
    if (res.page !== signalPage.value) signalPage.value = res.page
  } finally {
    loadingSignals.value = false
  }
}

function goSignalPage(next: number): void {
  const p = Math.min(Math.max(1, next), signalTotalPages.value)
  if (p === signalPage.value) return
  signalPage.value = p
  loadSignals()
}

function onSignalPageSizeChange(): void {
  signalPage.value = 1
  loadSignals()
}

function toggleRow(key: string | number): void {
  const k = String(key)
  expanded.value[k] = !expanded.value[k]
}
function isExpanded(key: string | number): boolean {
  return !!expanded.value[String(key)]
}

// ---- 格式化 ----
function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function fmtPayload(raw: unknown): string {
  if (raw == null) return '—'
  if (typeof raw === 'string') {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }
  return JSON.stringify(raw, null, 2)
}

function taskTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    pending: { cls: 'blue', text: '待处理' },
    dispatching: { cls: 'blue', text: '分发中' },
    done: { cls: 'green', text: '完成' },
    partial: { cls: 'amber', text: '部分成功' },
    failed: { cls: 'red', text: '失败' },
    skipped: { cls: '', text: '未下发' },
  }
  return m[status] || { cls: '', text: status }
}

function dispatchTag(status: string): { cls: string; text: string } {
  const m: Record<string, { cls: string; text: string }> = {
    done: { cls: 'green', text: '成功' },
    failed: { cls: 'red', text: '失败' },
    offline: { cls: 'red', text: '离线' },
    skipped: { cls: '', text: '跳过' },
    sent: { cls: 'blue', text: '已下发' },
    pending: { cls: 'blue', text: '等待' },
  }
  return m[status] || { cls: 'blue', text: status }
}

function dispatchSummary(row: GroupSignalTaskRecord): string {
  const n = row.dispatches.length
  if (!n) return row.skip_reason || '无节点处理'
  const done = row.dispatches.filter((d) => d.status === 'done').length
  const failed = row.dispatches.filter((d) => d.status === 'failed' || d.status === 'offline').length
  const parts: string[] = [`${n} 节点`]
  if (done) parts.push(`${done} 成功`)
  if (failed) parts.push(`${failed} 失败`)
  return parts.join(' · ')
}
</script>

<template>
  <div class="groups-page">
    <div class="row between page-header">
      <div>
        <div class="h1">分组管理</div>
        <p class="muted" style="font-size: 13px; margin-top: 4px">
          分组是 strategy 信号的分发单元：Webhook 携带 <code>model=strategy</code> 时按分组分发，
          与按币种分发（<code>model=normal</code>，默认）的规则完全隔离
        </p>
      </div>
      <div class="row" style="gap: 8px">
        <button class="btn-ghost" @click="router.push('/strategies')">策略管理</button>
        <button class="btn-primary" @click="openCreate">+ 新建分组</button>
      </div>
    </div>

    <div class="card card-pad" style="margin-bottom: 12px">
      <div class="row" style="gap: 8px">
        <input
          v-model="searchQuery"
          type="search"
          placeholder="搜索分组名称…"
          aria-label="搜索分组名称"
          :disabled="loading"
          style="width: 25%"
          @keydown.enter="runSearch"
        />
        <button class="btn-primary btn-sm" :disabled="loading" @click="runSearch">
          {{ loading ? '搜索中…' : '搜索' }}
        </button>
      </div>
      <p v-if="appliedQuery" class="muted" style="font-size: 12px; margin: 8px 0 0">
        {{ hub.groups.length ? `找到 ${hub.groups.length} 个分组` : '无匹配分组' }}
      </p>
    </div>

    <!-- 移动端卡片 -->
    <div class="list-cards mobile-only">
      <div v-for="g in hub.groups" :key="g.group_id" class="list-card card">
        <div class="list-card-head row between">
          <strong>{{ g.name }}</strong>
          <span class="tag" :class="g.enabled ? 'green' : ''">{{ g.enabled ? '已启用' : '已禁用' }}</span>
        </div>
        <div class="list-field"><span class="k">分组 ID</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ g.group_id }}</span></div>
        <div class="list-field">
          <span class="k">分发模式</span>
          <span class="v"><span class="tag blue">{{ DISPATCH_MODE_LABEL[g.dispatch_mode] }}</span></span>
        </div>
        <div class="list-field"><span class="k">成员节点</span><span class="v">{{ g.node_count }}</span></div>
        <div class="list-field"><span class="k">有效节点</span><span class="v">{{ g.online_node_count }}</span></div>
        <div class="list-field">
          <span class="k">信号</span>
          <span class="v">
            <button class="btn-sm btn-ghost" @click="openSignals(g)">{{ g.signal_count }} 条</button>
          </span>
        </div>
        <div class="list-field"><span class="k">备注</span><span class="v muted" style="font-size: 12px; font-weight: 500">{{ g.remark || '—' }}</span></div>
        <div class="list-card-actions">
          <button class="btn-sm" :class="g.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(g)">
            {{ g.enabled ? '禁用' : '启用' }}
          </button>
          <button class="btn-sm btn-ghost" @click="openEdit(g)">编辑</button>
          <button class="btn-sm btn-danger" @click="remove(g)">删除</button>
        </div>
      </div>
      <div v-if="!hub.groups.length && !loading" class="card card-pad muted">
        {{ appliedQuery ? '无匹配分组' : '暂无分组' }}
      </div>
    </div>

    <!-- 桌面端表格 -->
    <div class="card table-scroll desktop-only">
      <table>
        <thead>
          <tr>
            <th>名称</th><th>分发模式</th><th class="right">成员节点</th>
            <th class="right">有效节点</th><th class="right">信号</th>
            <th>备注</th><th>启用</th><th class="right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in hub.groups" :key="g.group_id">
            <td>
              {{ g.name }}
              <div class="muted" style="font-size: 11px">{{ g.group_id }}</div>
            </td>
            <td><span class="tag blue">{{ DISPATCH_MODE_LABEL[g.dispatch_mode] }}</span></td>
            <td class="right">{{ g.node_count }}</td>
            <td class="right" :class="g.online_node_count ? '' : 'muted'">{{ g.online_node_count }}</td>
            <td class="right">
              <button class="btn-sm btn-ghost" @click="openSignals(g)">{{ g.signal_count }} 条</button>
            </td>
            <td class="muted" style="font-size: 12px">{{ g.remark || '—' }}</td>
            <td>
              <button class="btn-sm" :class="g.enabled ? 'btn-ghost' : 'btn-danger'" @click="toggleEnabled(g)">
                {{ g.enabled ? '已启用' : '已禁用' }}
              </button>
            </td>
            <td class="right">
              <button class="btn-sm btn-ghost" @click="openEdit(g)">编辑</button>
              <button class="btn-sm btn-danger" @click="remove(g)">删除</button>
            </td>
          </tr>
          <tr v-if="!hub.groups.length && !loading">
            <td colspan="8" class="muted" style="padding: 18px">
              {{ appliedQuery ? '无匹配分组' : '暂无分组，点击右上角「新建分组」开始配置' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 / 编辑弹窗 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="card card-pad modal modal-lg group-form-modal">
        <div class="modal-header">
          <div class="h1">{{ formMode === 'create' ? '新建分组' : '编辑分组' }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            分组的分发模式作用于整个分组、不区分币种；信号进入时只有「已启用且在线」的成员节点才会被下发。
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid two">
            <div>
              <FormLabel field-id="group-name" text="分组名称" :help="FIELD_HELP.name" />
              <input id="group-name" v-model="form.name" placeholder="例如：黄金策略组" />
            </div>
            <div>
              <FormLabel field-id="group-mode" text="分发模式" :help="FIELD_HELP.dispatch_mode" />
              <select id="group-mode" v-model="form.dispatch_mode">
                <option value="sync">全员同步</option>
                <option value="poll">轮询轮转（单节点领取）</option>
              </select>
            </div>
            <div>
              <FormLabel field-id="group-enabled" text="启用状态" :help="FIELD_HELP.enabled" />
              <select id="group-enabled" v-model="form.enabled">
                <option :value="true">启用</option>
                <option :value="false">禁用</option>
              </select>
            </div>
            <div>
              <FormLabel field-id="group-remark" text="备注" :help="FIELD_HELP.remark" />
              <input id="group-remark" v-model="form.remark" placeholder="选填" />
            </div>

            <div class="span-full">
              <FormLabel text="成员节点" :help="FIELD_HELP.nodes" />
              <div class="row" style="gap: 8px; margin-bottom: 10px">
                <select
                  :value="''"
                  :disabled="!availableNodes.length"
                  @change="addMember(($event.target as HTMLSelectElement).value)"
                >
                  <option value="" disabled>
                    {{ availableNodes.length ? '选择要加入的节点…' : '所有节点均已加入' }}
                  </option>
                  <option v-for="n in availableNodes" :key="n.node_id" :value="n.node_id">
                    {{ n.name }}（{{ n.mt5_login || '—' }}）
                  </option>
                </select>
              </div>
              <div v-if="!memberNodes.length" class="muted" style="font-size: 12px">
                尚未加入任何节点。分组无有效节点时不会处理 strategy 信号。
              </div>
              <div v-else class="member-list">
                <div v-for="(n, i) in memberNodes" :key="n.node_id" class="member-row">
                  <span class="member-order">{{ i + 1 }}</span>
                  <span class="dot" :class="hub.statuses[n.node_id] || n.status"></span>
                  <span class="member-name">{{ n.name }}</span>
                  <span class="muted member-meta">{{ n.mt5_login || '—' }}</span>
                  <span class="tag" :class="n.enabled ? 'green' : ''">{{ n.enabled ? '已启用' : '已禁用' }}</span>
                  <span class="member-actions">
                    <button class="btn-sm btn-ghost" :disabled="i === 0" @click="moveMember(i, -1)">↑</button>
                    <button class="btn-sm btn-ghost" :disabled="i === memberNodes.length - 1" @click="moveMember(i, 1)">↓</button>
                    <button class="btn-sm btn-danger" @click="removeMember(n.node_id)">移出</button>
                  </span>
                </div>
              </div>
            </div>

            <div v-if="formError" class="span-full" style="color: var(--red); font-size: 13px">{{ formError }}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showForm = false">取消</button>
          <button class="btn-primary" :disabled="saving || !form.name.trim()" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 信号明细弹窗 -->
    <div v-if="showSignals" class="modal-mask" @click.self="showSignals = false">
      <div class="card card-pad modal modal-lg group-signal-modal">
        <div class="modal-header">
          <div class="row between">
            <div>
              <div class="h1">分组信号 · {{ signalGroup?.name }}</div>
              <p class="muted" style="font-size: 12px; margin: 4px 0 0">
                共 {{ signalTotal }} 条主任务 · 点击行展开信号明细与各节点处理过程
              </p>
            </div>
            <button class="btn-sm btn-ghost" :disabled="loadingSignals" @click="loadSignals">
              {{ loadingSignals ? '刷新中…' : '刷新' }}
            </button>
          </div>
        </div>
        <div class="modal-body">
          <div v-if="signals.length" class="table-scroll">
            <table class="group-signal-table">
              <thead>
                <tr>
                  <th style="width: 22px"></th>
                  <th>时间</th><th>任务号</th><th>魔术号</th><th>动作</th><th>品种</th>
                  <th class="right">手数</th><th>分发模式</th><th>任务状态</th><th>节点处理</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="t in signals" :key="t.task_id">
                  <tr class="clickable" @click="toggleRow(t.task_id)">
                    <td class="muted">{{ isExpanded(t.task_id) ? '▾' : '▸' }}</td>
                    <td class="muted" style="font-size: 12px">{{ fmtTime(t.created_at) }}</td>
                    <td>#{{ t.task_id }}</td>
                    <td class="muted" style="font-size: 12px">{{ t.magic ?? '—' }}</td>
                    <td>
                      <span
                        v-if="t.action"
                        class="tag"
                        :class="t.action === 'BUY' ? 'green' : t.action === 'SELL' ? 'blue' : ''"
                      >{{ t.action }}</span>
                      <span v-else class="muted">—</span>
                    </td>
                    <td>{{ t.symbol || '—' }}</td>
                    <td class="right">{{ t.volume ?? '—' }}</td>
                    <td class="muted" style="font-size: 12px">
                      {{ t.dispatch_mode === 'poll' ? '轮询轮转' : '全员同步' }}
                    </td>
                    <td><span class="tag" :class="taskTag(t.status).cls">{{ taskTag(t.status).text }}</span></td>
                    <td class="muted" style="font-size: 12px">{{ dispatchSummary(t) }}</td>
                  </tr>
                  <tr v-if="isExpanded(t.task_id)" class="detail-row">
                    <td></td>
                    <td colspan="9">
                      <div class="kv-grid" style="margin: 6px 0 10px">
                        <div class="kv"><span class="k">信号 ID</span><span class="v" style="font-size: 12px">{{ t.signal_id }}</span></div>
                        <div class="kv"><span class="k">来源 IP</span><span class="v" style="font-size: 12px">{{ t.source_ip || '—' }}</span></div>
                        <div class="kv"><span class="k">SL</span><span class="v">{{ t.sl ?? '—' }}</span></div>
                        <div class="kv"><span class="k">TP</span><span class="v">{{ t.tp ?? '—' }}</span></div>
                        <div class="kv"><span class="k">备注</span><span class="v" style="font-size: 12px">{{ t.comment || '—' }}</span></div>
                        <div class="kv"><span class="k">下发节点数</span><span class="v">{{ t.node_count }}</span></div>
                        <div class="kv"><span class="k">完成时间</span><span class="v" style="font-size: 12px">{{ fmtTime(t.finished_at) }}</span></div>
                        <div v-if="t.skip_reason" class="kv span-full">
                          <span class="k">未下发原因</span><span class="v" style="font-size: 12px">{{ t.skip_reason }}</span>
                        </div>
                      </div>

                      <div class="muted" style="font-size: 12px; margin-bottom: 6px">原始信号</div>
                      <pre class="token-box group-payload">{{ fmtPayload(t.raw_payload) }}</pre>

                      <div class="muted" style="font-size: 12px; margin-bottom: 6px">下发数据（发送给节点的命令）</div>
                      <pre class="token-box group-payload">{{ fmtPayload(t.payload) }}</pre>

                      <div class="muted" style="font-size: 12px; margin-bottom: 8px">各节点处理情况</div>
                      <div v-if="t.dispatches.length" class="table-scroll">
                        <table class="group-detail-table">
                          <thead>
                            <tr>
                              <th>节点</th><th>状态</th><th class="right">下发手数</th>
                              <th>跳过原因</th><th>返回码</th><th>订单</th><th class="right">成交价</th>
                              <th>错误</th><th>下发时间</th><th>完成时间</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr v-for="d in t.dispatches" :key="d.id">
                              <td>{{ d.node_name || d.node_id }}</td>
                              <td><span class="tag" :class="dispatchTag(d.status).cls">{{ dispatchTag(d.status).text }}</span></td>
                              <td class="right">{{ d.decided_vol ?? '—' }}</td>
                              <td class="muted group-break">{{ d.skip_reason || '—' }}</td>
                              <td>{{ d.retcode ?? '—' }}</td>
                              <td>{{ d.order ?? '—' }}</td>
                              <td class="right">{{ d.price ?? '—' }}</td>
                              <td class="muted group-break">{{ d.error || '—' }}</td>
                              <td class="muted" style="white-space: nowrap">{{ fmtTime(d.dispatched_at) }}</td>
                              <td class="muted" style="white-space: nowrap">{{ fmtTime(d.finished_at) }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div v-else class="muted" style="font-size: 13px">该主任务未产生节点下发明细。</div>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
          <div v-else-if="loadingSignals" class="muted" style="font-size: 13px; padding: 8px 0">加载中…</div>
          <div v-else class="muted" style="font-size: 13px; padding: 8px 0">该分组暂无信号记录。</div>

          <div v-if="signalTotal > 0" class="pagination" style="margin-top: 12px">
            <span class="muted pagination-info">
              共 {{ signalTotal }} 条 · 第 {{ signalPage }} / {{ signalTotalPages }} 页
            </span>
            <div class="row pagination-actions">
              <select v-model.number="signalPageSize" class="pagination-size" @change="onSignalPageSizeChange">
                <option :value="10">10 条/页</option>
                <option :value="20">20 条/页</option>
                <option :value="50">50 条/页</option>
              </select>
              <button class="btn-sm btn-ghost" :disabled="loadingSignals || signalPage <= 1" @click="goSignalPage(signalPage - 1)">
                上一页
              </button>
              <button
                class="btn-sm btn-ghost"
                :disabled="loadingSignals || signalPage >= signalTotalPages"
                @click="goSignalPage(signalPage + 1)"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showSignals = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.groups-page {
  width: 100%;
  min-width: 0;
}

.member-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.member-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
}

.member-order {
  min-width: 20px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}

.member-name {
  font-weight: 500;
}

.member-meta {
  font-size: 12px;
}

.member-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.group-signal-modal .modal-body {
  min-width: 0;
}

.group-signal-table,
.group-detail-table {
  width: 100%;
  min-width: 0;
}

.group-signal-table th,
.group-signal-table td,
.group-detail-table th,
.group-detail-table td {
  font-size: 12px;
  vertical-align: top;
}

.group-payload {
  width: 100%;
  max-width: 100%;
  margin-bottom: 12px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
}

.group-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}

@media (max-width: 768px) {
  .group-form-modal,
  .group-signal-modal {
    width: 100%;
    max-width: 100%;
    max-height: calc(100dvh - 24px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
    border-radius: 16px;
  }
  .member-row {
    flex-wrap: wrap;
  }
  .member-actions {
    width: 100%;
    margin-left: 0;
    justify-content: flex-end;
  }
}
</style>
