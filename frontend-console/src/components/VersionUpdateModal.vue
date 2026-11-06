<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  api,
  type UpdateDetect,
  type UpdateInstance,
  type UpdatePrepare,
  type UpdateProgress,
  type UpdateVersionItem,
} from '../api/bridge'
import AppModal from './AppModal.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'done'): void }>()

// ── 阶段 ──
const stage = ref<'list' | 'confirm' | 'running' | 'done'>('list')

// ── 数据 ──
const instances = ref<UpdateInstance[]>([])
const detect = ref<UpdateDetect | null>(null)
const published = ref('')
const notes = ref('')

// ── 选择 ──
const selected = ref<Record<string, boolean>>({})
const targetVersion = ref('')
const rollbackVersion = ref('')
const rollbackMode = ref(false)

/** 待执行的操作类型（update | rollback），在确认阶段用。 */
const pendingKind = ref<'update' | 'rollback'>('update')

/** 确认阶段锁定的目标版本 —— 确认摘要里展示的就是它，不能随界面改动漂移。 */
const confirmVersion = ref('')

// ── 状态 ──
const error = ref('')
const status = ref('')
const busy = ref(false)
const confirmText = ref('')
const downgrades = ref<string[]>([])
const progress = ref<UpdateProgress | null>(null)

let pollTimer: number | null = null

const selectedIds = computed(() =>
  instances.value.filter((i) => selected.value[i.id]).map((i) => i.id),
)

/** 目标版本的判定文案：需更新 / 将降级 / 已最新。 */
function verdictOf(inst: UpdateInstance): { text: string; tone: string } {
  const target = rollbackMode.value ? rollbackVersion.value : targetVersion.value
  if (!target) return { text: '', tone: '' }
  if (!inst.version) return { text: '版本未知', tone: 'dim' }
  const a = inst.version.match(/\d+/g)?.map(Number) ?? []
  const b = target.match(/\d+/g)?.map(Number) ?? []
  const width = Math.max(a.length, b.length)
  for (let i = 0; i < width; i += 1) {
    const x = a[i] ?? 0
    const y = b[i] ?? 0
    if (x < y) return { text: '需更新', tone: 'up' }
    if (x > y) return { text: '将降级', tone: 'down' }
  }
  return { text: '已最新', tone: 'dim' }
}

/**
 * 更新到当前发布版本。
 *
 * 目标版本先落到 `targetVersion` 再调 prepare —— 模板里的内联 handler
 * 写成 `targetVersion = published; prepare('update')` 时，两条语句在同一
 * 个表达式里求值，prepare 读到的还是旧值。
 */
async function updateToPublished(): Promise<void> {
  targetVersion.value = published.value
  await prepare('update')
}

/** 当前发布版本对应的 sha256（更新到发布版时要校验）。 */
const publishedItem = computed<UpdateVersionItem | null>(
  () =>
    detect.value?.versions.find((v) => v.version === published.value) ?? null,
)

const selectedVersionItem = computed<UpdateVersionItem | null>(
  () =>
    detect.value?.versions.find((v) => v.version === targetVersion.value) ?? null,
)

/** 可回滚版本：所选实例共有的本机备份（新版在前）。 */
const commonBackups = computed(() => {
  const ids = selectedIds.value
  const picked = instances.value.filter((i) => ids.includes(i.id))
  if (!picked.length) return []
  let common = new Set(picked[0].backups)
  for (const inst of picked.slice(1)) {
    common = new Set([...common].filter((v) => inst.backups.includes(v)))
  }
  return [...common].sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
})

async function loadInstances() {
  try {
    const res = await api.updateInstances()
    instances.value = res.instances
    published.value = res.published
    const picked: Record<string, boolean> = {}
    for (const inst of res.instances) picked[inst.id] = false
    selected.value = picked
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  }
}

async function runDetect() {
  busy.value = true
  error.value = ''
  status.value = '正在检测后端发布版本…'
  try {
    const res = await api.updateDetect()
    detect.value = res
    if (!res.ok) {
      error.value = res.error || '检测失败'
      status.value = ''
      return
    }
    published.value = res.published
    notes.value = res.notes
    status.value = res.published
      ? `后端发布版本 v${res.published}`
      : '后端尚未发布任何客户端版本'
    // 默认选中当前发布版本；没有发布版就用清单里最新的一个
    if (!targetVersion.value) {
      const hit =
        res.versions.find((v) => v.version === res.published) ?? res.versions[0]
      targetVersion.value = hit?.version ?? ''
    }
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
    status.value = ''
  } finally {
    busy.value = false
  }
}

/** 全选 / 全不选。 */
function selectAll(checked: boolean) {
  const picked: Record<string, boolean> = {}
  for (const inst of instances.value) picked[inst.id] = checked
  selected.value = picked
}

/** 勾选 / 取消勾选一个实例。 */
function toggle(id: string) {
  selected.value = { ...selected.value, [id]: !selected.value[id] }
}

function targetOf(): string {
  return rollbackMode.value ? rollbackVersion.value : targetVersion.value
}

/** 拉起二次确认摘要。 */
async function prepare(kind: 'update' | 'rollback') {
  error.value = ''
  status.value = ''
  const version = targetOf()
  if (!version) {
    error.value = rollbackMode.value ? '请先选择要回滚到的备份版本' : '请先选择目标版本'
    return
  }
  if (!selectedIds.value.length) {
    error.value = '请先勾选要操作的实例'
    return
  }
  busy.value = true
  try {
    const res: UpdatePrepare = await api.updatePrepare({
      version,
      ids: selectedIds.value,
    })
    if (!res.ok) {
      error.value = res.error || '无法开始操作'
      return
    }
    confirmText.value = res.summary
    downgrades.value = res.downgrades
    pendingKind.value = kind
    confirmVersion.value = version
    stage.value = 'confirm'
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

/** 用户确认摘要后真正启动任务。 */
async function confirmAndRun() {
  error.value = ''
  busy.value = true
  const version = confirmVersion.value || targetOf()
  try {
    const res =
      pendingKind.value === 'rollback'
        ? await api.updateRollback({ version, ids: selectedIds.value })
        : await api.updateStart({
            version,
            sha256:
              selectedVersionItem.value?.sha256 ?? publishedItem.value?.sha256 ?? '',
            ids: selectedIds.value,
          })
    if (!res.ok) {
      error.value = res.error || '启动版本操作失败'
      return
    }
    stage.value = 'running'
    progress.value = null
    startPolling(res.task_id)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

function startPolling(taskId: string) {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const snap = await api.updateProgress(taskId)
      progress.value = snap
      if (snap.done) {
        stopPolling()
        if (snap.error) {
          error.value = snap.error
          stage.value = 'list'
        } else {
          stage.value = 'done'
          emit('done')
        }
      }
    } catch (exc) {
      stopPolling()
      error.value = exc instanceof Error ? exc.message : String(exc)
      stage.value = 'list'
    }
  }, 700)
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

const overallPct = computed(() => {
  const steps = progress.value?.steps ?? []
  if (!steps.length) return 0
  const active = steps.find((s) => s.status === 'active')
  if (active) return Math.round(active.pct * 100)
  const done = steps.filter((s) => s.status === 'done').length
  return Math.round((done / steps.length) * 100)
})

const resultRows = computed(() => progress.value?.result?.results ?? [])

watch(
  () => props.open,
  (open) => {
    if (!open) {
      stopPolling()
      return
    }
    stage.value = 'list'
    error.value = ''
    status.value = ''
    confirmText.value = ''
    downgrades.value = []
    progress.value = null
    rollbackMode.value = false
    targetVersion.value = ''
    rollbackVersion.value = ''
    void loadInstances()
    void runDetect()
  },
)

// 切到回滚模式时，目标版本改用本机备份
watch(rollbackMode, (on) => {
  if (on) {
    rollbackVersion.value = commonBackups.value[0] ?? ''
  }
})

// 勾选变化会改变「共同备份」集合：原先选中的备份可能已经不在里面了。
watch(commonBackups, (list) => {
  if (!rollbackMode.value) return
  if (!list.includes(rollbackVersion.value)) {
    rollbackVersion.value = list[0] ?? ''
  }
})

onUnmounted(stopPolling)

function close() {
  if (stage.value === 'running') return
  emit('close')
}
</script>

<template>
  <AppModal
    v-if="open"
    title="客户端版本更新"
    subtitle="检测后端版本 · 批量更新 · 指定版本 · 本机回滚"
    @close="close"
  >
    <!-- ══ 阶段一：清单 ══ -->
    <template v-if="stage === 'list'">
      <div class="vu-toolbar">
        <button class="btn-sm primary" :disabled="busy" @click="runDetect">
          检测更新
        </button>
        <span class="vu-status">{{ status || '尚未检测' }}</span>
      </div>

      <p v-if="notes" class="hint-line">更新说明：{{ notes }}</p>
      <p v-if="error" class="modal-error">{{ error }}</p>

      <!-- 目标版本 -->
      <div class="form-field">
        <label>目标版本</label>
        <!-- 模式切换：两个按钮始终可点，当前模式用 primary 高亮。

             不能用 disabled 表达「当前选中」：disabled 会带 pointer-events:none，
             浏览器层根本点不到另一个模式，切换路径直接断掉。 -->
        <div class="vu-mode-row">
          <button
            class="btn-sm"
            :class="{ primary: !rollbackMode }"
            :aria-pressed="!rollbackMode"
            @click="rollbackMode = false"
          >
            从后端更新
          </button>
          <button
            class="btn-sm"
            :class="{ primary: rollbackMode }"
            :aria-pressed="rollbackMode"
            @click="rollbackMode = true"
          >
            本机回滚
          </button>
        </div>

        <div v-if="!rollbackMode" class="version-row vu-target-row">
          <div class="select-wrap">
            <select v-model="targetVersion" class="input-field">
              <option v-if="!detect?.versions.length" value="">（后端没有可下载版本）</option>
              <option v-for="v in detect?.versions ?? []" :key="v.version" :value="v.version">
                {{ v.version }}{{ v.version === published ? '（当前发布）' : '' }}
              </option>
            </select>
          </div>
          <span v-if="selectedVersionItem" class="hint-line">
            {{ (selectedVersionItem.size / 1024 / 1024).toFixed(1) }} MB
          </span>
        </div>

        <div v-else class="version-row vu-target-row">
          <div class="select-wrap">
            <select v-model="rollbackVersion" class="input-field">
              <option v-if="!commonBackups.length" value="">（所选实例没有共同备份）</option>
              <option v-for="v in commonBackups" :key="v" :value="v">{{ v }}</option>
            </select>
          </div>
          <span class="hint-line">回滚不依赖网络，直接用本机 .backups 里的副本。</span>
        </div>
      </div>

      <!-- 实例清单 -->
      <div class="form-field">
        <label>实例清单</label>
        <div class="vu-list-head">
          <button class="btn-sm" @click="selectAll(true)">全选</button>
          <button class="btn-sm" @click="selectAll(false)">全不选</button>
          <span class="section-note">已选 {{ selectedIds.length }} / {{ instances.length }}</span>
        </div>

        <div class="vu-instance-list">
          <div v-for="inst in instances" :key="inst.id" class="vu-instance-row">
            <span
              class="check-box"
              :class="{ on: selected[inst.id] }"
              @click="toggle(inst.id)"
            >
              {{ selected[inst.id] ? '✓' : '' }}
            </span>
            <span class="vu-instance-name" @click="toggle(inst.id)">{{ inst.name }}</span>
            <span class="mono vu-instance-ver">v{{ inst.version || '?' }}</span>
            <span class="vu-instance-verdict" :class="verdictOf(inst).tone">
              {{ verdictOf(inst).text }}
            </span>
          </div>
          <p v-if="!instances.length" class="hint-line">本机还没有实例。</p>
        </div>
      </div>

      <div class="vu-actions">
        <button
          class="btn-md primary"
          :disabled="busy || !selectedIds.length || !targetOf()"
          @click="prepare(rollbackMode ? 'rollback' : 'update')"
        >
          {{ rollbackMode ? '回滚到本机备份' : '更新到目标版本' }}
        </button>
        <button
          v-if="!rollbackMode && published"
          class="btn-md"
          :disabled="busy || !selectedIds.length"
          @click="updateToPublished"
        >
          更新到发布版本
        </button>
      </div>
      <p class="hint-line">
        更新会先停止实例，备份当前版本后覆盖，再自动启动；.env 不会被覆盖。
      </p>
    </template>

    <!-- ══ 阶段二：确认 ══ -->
    <template v-else-if="stage === 'confirm'">
      <div class="modal-note">
        <p class="note-title">
          {{ pendingKind === 'rollback' ? '回滚确认' : '更新确认' }}
        </p>
        <pre class="note-body">{{ confirmText }}</pre>
      </div>

      <div v-if="downgrades.length" class="modal-note vu-warn">
        <p class="note-title">降级二次确认</p>
        <pre class="note-body">
以下实例的目标版本低于当前版本：
  · {{ downgrades.join('\n  · ') }}

降级可能把已修复的问题带回线上。确认要降级吗？</pre
        >
      </div>

      <p v-if="error" class="modal-error">{{ error }}</p>

      <div class="vu-actions">
        <button class="btn-md primary" :disabled="busy" @click="confirmAndRun">
          确认执行
        </button>
        <button class="btn-md" :disabled="busy" @click="stage = 'list'">
          返回修改
        </button>
      </div>
    </template>

    <!-- ══ 阶段三：进度 ══ -->
    <template v-else-if="stage === 'running'">
      <div class="import-screen">
        <div class="import-head">
          <div class="import-spinner">↻</div>
          <div>
            <h3 class="import-title">
              {{ pendingKind === 'rollback' ? '正在回滚' : '正在更新' }}
            </h3>
            <p class="import-subtitle">
              目标版本 v{{ confirmVersion || targetOf() }} · 已用时 {{ progress?.elapsed ?? 0 }}s
            </p>
          </div>
        </div>

        <div class="import-progress-area">
          <div class="import-progress-top">
            <span class="import-progress-label">总体进度</span>
            <span class="import-progress-pct">{{ overallPct }}%</span>
          </div>
          <div class="import-progress-track">
            <div class="import-progress-fill" :style="{ width: overallPct + '%' }" />
          </div>
        </div>

        <div class="import-steps">
          <div
            v-for="s in progress?.steps ?? []"
            :key="s.key"
            class="import-step"
            :class="{ done: s.status === 'done', active: s.status === 'active' }"
          >
            <span class="import-step-dot">
              {{ s.status === 'done' ? '✓' : s.status === 'failed' ? '✕' : '·' }}
            </span>
            <span class="import-step-label">{{ s.label }}</span>
            <span v-if="s.status === 'active'" class="import-step-tag">
              {{ Math.round(s.pct * 100) }}%
            </span>
          </div>
        </div>

        <div v-if="progress?.log.length" class="import-log">
          <div v-for="(line, i) in progress.log" :key="i" class="import-log-line">
            {{ line }}
          </div>
        </div>
      </div>
    </template>

    <!-- ══ 阶段四：完成 ══ -->
    <template v-else>
      <div class="import-screen">
        <div class="import-head">
          <div class="import-spinner">✓</div>
          <div>
            <h3 class="import-title">版本操作完成</h3>
            <p class="import-subtitle">
              成功 {{ progress?.result?.ok_count ?? 0 }} / {{ progress?.result?.total ?? 0 }}
            </p>
          </div>
        </div>

        <div v-if="resultRows.length" class="vu-result-list">
          <div v-for="r in resultRows" :key="r.id" class="vu-result-row">
            <span class="vu-result-mark" :class="r.ok ? 'ok' : 'bad'">
              {{ r.ok ? '✓' : '✗' }}
            </span>
            <span class="vu-instance-name">{{ r.name }}</span>
            <span class="mono vu-instance-ver">
              v{{ r.old_version || '-' }} → v{{ r.new_version || '-' }}
            </span>
            <span class="vu-result-msg">{{ r.message }}</span>
          </div>
        </div>
      </div>
    </template>

    <template #footer>
      <button v-if="stage !== 'running'" class="btn-md" @click="close">关闭</button>
    </template>
  </AppModal>
</template>

<style scoped>
/* 面板比默认弹窗宽：实例清单要放得下勾选框 + 名称 + 版本 + 判定。 */
:deep(.modal-panel) {
  max-width: 720px;
}

.vu-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}

.vu-status {
  font-size: 12px;
  color: var(--dim);
}

.vu-mode-row {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}

.vu-target-row {
  flex-wrap: wrap;
}

.vu-list-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0;
}

.vu-instance-list {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #070b13;
}

.vu-instance-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line-soft);
}

.vu-instance-row:last-child {
  border-bottom: none;
}

.vu-instance-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}

.vu-instance-ver {
  font-size: 11.5px;
  color: var(--dim);
}

.vu-instance-verdict {
  width: 64px;
  text-align: right;
  font-size: 11.5px;
}

.vu-actions {
  display: flex;
  gap: 8px;
}

.vu-warn {
  background: #f2b84b0f;
  border-color: rgba(242, 184, 75, 0.28);
  border-left-color: var(--amber);
}

.vu-warn .note-title {
  color: var(--amber);
}

.vu-result-list {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #070b13;
}

.vu-result-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}

.vu-result-row:last-child {
  border-bottom: none;
}

.vu-result-mark {
  flex: none;
  width: 16px;
  text-align: center;
  font-weight: 700;
}

.vu-result-mark.ok {
  color: var(--green);
}

.vu-result-mark.bad {
  color: var(--red);
}

.vu-result-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--mute);
  text-align: right;
}

.check-box {
  cursor: pointer;
}
</style>
