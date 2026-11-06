<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  api,
  type DirCheck,
  type ImportProgress,
  type ImportVersionItem,
} from '../api/bridge'
import AppModal from './AppModal.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'done'): void }>()

// ── 表单 ──
const directory = ref('')
const name = ref('')
const account = ref('')
const writeEnv = ref(true)
const versions = ref<ImportVersionItem[]>([])
const selectedVersion = ref('')
const currentPublished = ref('')

// ── 状态 ──
const stage = ref<'form' | 'running' | 'done'>('form')
const check = ref<DirCheck | null>(null)
const error = ref('')
const confirmText = ref('')
const progress = ref<ImportProgress | null>(null)
const busy = ref(false)

let pollTimer: number | null = null

const selectedItem = computed(
  () => versions.value.find((v) => v.version === selectedVersion.value) ?? null,
)

const overallPct = computed(() => {
  const steps = progress.value?.steps ?? []
  if (!steps.length) return 0
  const active = steps.find((s) => s.status === 'active')
  if (active) return Math.round(active.pct * 100)
  const done = steps.filter((s) => s.status === 'done').length
  return Math.round((done / steps.length) * 100)
})

const stepOrder = [
  'validate',
  'enroll',
  'download',
  'extract',
  'stop',
  'deploy',
  'env',
  'register',
]

const orderedSteps = computed(() => {
  const steps = progress.value?.steps ?? []
  const map = new Map(steps.map((s) => [s.key, s]))
  return stepOrder
    .map((key) => map.get(key))
    .filter((s): s is NonNullable<typeof s> => !!s)
})

/** 目录变了就重新校验。 */
async function recheck() {
  error.value = ''
  if (!directory.value.trim()) {
    check.value = null
    return
  }
  try {
    check.value = await api.checkDir(directory.value.trim())
    if (check.value.ok && !name.value) {
      name.value = check.value.suggested_name ?? ''
    }
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  }
}

/** 打开系统目录选择器（后端在 GUI 线程弹原生对话框）。 */
async function browse() {
  error.value = ''
  busy.value = true
  try {
    const res = await api.pickDir()
    if (res.cancelled || !res.path) return
    directory.value = res.path
    await recheck()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

async function loadVersions() {
  error.value = ''
  try {
    const res = await api.importVersions()
    if (!res.ok) {
      error.value = res.error || '获取版本列表失败'
      versions.value = []
      return
    }
    versions.value = res.versions
    currentPublished.value = res.current
    if (!selectedVersion.value && res.versions.length) {
      // 默认选当前发布版；没发布过就用第一个
      const hit = res.versions.find((v) => v.is_current) ?? res.versions[0]
      selectedVersion.value = hit.version
    }
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  }
}

/** 先 prepare（解析账号 + 生成摘要），确认后再 start。 */
async function start() {
  error.value = ''
  confirmText.value = ''
  if (!check.value?.ok) {
    error.value = check.value?.message || '请先选择 MT5 安装目录'
    return
  }
  if (!selectedVersion.value) {
    error.value = '请先选择客户端版本'
    return
  }
  busy.value = true
  try {
    const payload = {
      directory: directory.value.trim(),
      name: name.value.trim(),
      version: selectedVersion.value,
      sha256: selectedItem.value?.sha256 ?? '',
      account: account.value.trim(),
      write_env: writeEnv.value,
    }
    const prepared = await api.prepareImport(payload)
    if (!prepared.ok) {
      error.value = prepared.error || '无法开始导入'
      return
    }
    confirmText.value = prepared.summary
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

/** 用户确认摘要后，真正启动导入任务。 */
async function confirmAndRun() {
  error.value = ''
  busy.value = true
  try {
    const res = await api.startImport({
      directory: directory.value.trim(),
      name: name.value.trim(),
      version: selectedVersion.value,
      sha256: selectedItem.value?.sha256 ?? '',
      account: account.value.trim(),
      write_env: writeEnv.value,
    })
    if (!res.ok) {
      error.value = res.error || '启动导入失败'
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
      const snap = await api.importProgress(taskId)
      progress.value = snap
      if (snap.done) {
        stopPolling()
        if (snap.error) {
          error.value = snap.error
          stage.value = 'form'
        } else {
          stage.value = 'done'
          emit('done')
        }
      }
    } catch (exc) {
      stopPolling()
      error.value = exc instanceof Error ? exc.message : String(exc)
      stage.value = 'form'
    }
  }, 700)
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

// 打开弹窗时重置状态并拉版本列表。
watch(
  () => props.open,
  (open) => {
    if (!open) {
      stopPolling()
      return
    }
    stage.value = 'form'
    error.value = ''
    confirmText.value = ''
    progress.value = null
    check.value = null
    void loadVersions()
    if (directory.value.trim()) void recheck()
  },
)

onUnmounted(stopPolling)

function close() {
  if (stage.value === 'running') return
  emit('close')
}
</script>

<template>
  <AppModal
    v-if="open"
    title="导入节点"
    subtitle="部署客户端到 MT5 目录"
    @close="close"
  >
    <!-- ══ 阶段一：表单 ══ -->
    <template v-if="stage === 'form'">
      <div class="form-field">
        <label>MT5 目录</label>
        <div class="dir-input-row">
          <input
            v-model="directory"
            class="input-field"
            placeholder="如 C:\Program Files\MetaTrader 5"
            @blur="recheck"
          />
          <button class="btn-sm browse-btn" :disabled="busy" @click="browse">浏览…</button>
        </div>
      </div>

      <p v-if="check?.ok" class="hint-line">{{ check.message }}</p>
      <p v-else-if="check && !check.ok" class="modal-error">{{ check.message }}</p>

      <div class="form-field">
        <label>实例标签</label>
        <input v-model="name" class="input-field" placeholder="留空则取目录名" />
      </div>

      <div class="form-field">
        <label>客户端版本</label>
        <div class="version-row">
          <div class="select-wrap">
            <select v-model="selectedVersion" class="input-field">
              <option v-if="!versions.length" value="">（尚未检测）</option>
              <option v-for="v in versions" :key="v.version" :value="v.version">
                {{ v.version }}{{ v.is_current ? '（当前发布）' : '' }}
              </option>
            </select>
          </div>
          <button class="btn-sm" :disabled="busy" @click="loadVersions">刷新列表</button>
          <span v-if="selectedItem" class="hint-line">
            {{ (selectedItem.size / 1024 / 1024).toFixed(1) }} MB
          </span>
        </div>
        <p v-if="selectedItem?.notes" class="hint-line">
          更新说明：{{ selectedItem.notes }}
        </p>
      </div>

      <div class="form-field">
        <label>MT5 账号</label>
        <input
          v-model="account"
          class="input-field"
          placeholder="留空则读取终端当前登录号"
        />
      </div>

      <div class="check-list">
        <label class="check-row">
          <span class="check-box" :class="{ on: writeEnv }" @click="writeEnv = !writeEnv">
            {{ writeEnv ? '✓' : '' }}
          </span>
          <span class="check-text">
            <span class="check-label">生成准备配置 .env（开通后自动配置专属令牌）</span>
            <span class="check-desc">
              导入仅准备本机客户端并申请节点。账号可留空，留空将读取终端当前登录号。
              后台节点归属分配与启用审核均完成后，启动时仍会核对该账号。
            </span>
          </span>
        </label>
      </div>

      <p v-if="error" class="modal-error">{{ error }}</p>

      <!-- 确认摘要 -->
      <div v-if="confirmText" class="modal-note">
        <p class="note-title">导入确认</p>
        <pre class="note-body">{{ confirmText }}</pre>
      </div>

      <div class="import-actions">
        <button
          v-if="!confirmText"
          class="btn-md primary"
          :disabled="busy || !check?.ok || !selectedVersion"
          @click="start"
        >
          开始导入
        </button>
        <template v-else>
          <button class="btn-md primary" :disabled="busy" @click="confirmAndRun">
            确认导入
          </button>
          <button class="btn-md" :disabled="busy" @click="confirmText = ''">返回修改</button>
        </template>
      </div>
    </template>

    <!-- ══ 阶段二：进度 ══ -->
    <template v-else-if="stage === 'running'">
      <div class="import-screen">
        <div class="import-head">
          <div class="import-spinner">↻</div>
          <div>
            <h3 class="import-title">正在导入</h3>
            <p class="import-subtitle">{{ directory }}</p>
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
            v-for="s in orderedSteps"
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

    <!-- ══ 阶段三：完成 ══ -->
    <template v-else>
      <div class="import-screen">
        <div class="import-head">
          <div class="import-spinner">✓</div>
          <div>
            <h3 class="import-title">导入完成</h3>
            <p class="import-subtitle">
              {{ String(progress?.result?.message ?? '') }}
            </p>
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
.import-actions {
  display: flex;
  gap: 8px;
}

.note-body {
  margin: 0;
  white-space: pre-wrap;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
}

.check-box {
  cursor: pointer;
}
</style>
