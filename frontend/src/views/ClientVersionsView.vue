<script setup lang="ts">
// 客户端版本管理页：当前发布版本 / 上一版本与回滚 / 上传安装包 / 已上传版本列表。
// 版式 1:1 复刻 UI/H5.html 的「客户端版本管理」tab，交互仍走既有后端接口。
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { useClientVersionStore } from '@/stores/clientVersion'
import { useHubStore } from '@/stores/hub'
import { confirmAction } from '@/utils/confirm'
import type { ClientVersionOut } from '@/api/types'

const store = useClientVersionStore()
const hub = useHubStore()

const DOWNGRADE_CONFIRM_TEXT = '确认降级'
const ROLLBACK_CONFIRM_TEXT = '确认回滚'

const fileInput = ref<HTMLInputElement | null>(null)
const picked = ref<File | null>(null)
const formVersion = ref('')
const formNotes = ref('')
const uploading = ref(false)
const uploadError = ref('')

const uploadPct = computed(() => store.uploadProgress)

/** 未上报版本的节点：多为尚未升级到会上报版本的旧客户端 */
const unknownCount = computed(() => store.unknownNodeCount)

/** 当前发布版本的完整条目（大小 / 校验和都从这里取） */
const currentItem = computed<ClientVersionOut | null>(() => store.current)

function fmtTime(sec: number | null | undefined): string {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtDate(sec: number | null | undefined): string {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fmtSize(bytes: number): string {
  if (!bytes) return '—'
  const mb = bytes / 1024 / 1024
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

function shortHash(hash: string): string {
  return hash ? `${hash.slice(0, 12)}…` : '—'
}

/** 后端把版本号按语义序排好了，列表里第一条即最新版本 */
function isNewest(v: ClientVersionOut): boolean {
  return store.items.length > 0 && store.items[0].version === v.version
}

/** 与后端 compare_versions 同序；仅用于前端判断是否属于降级 */
function isAtLeast(a: string, b: string): boolean {
  const pa = (a.match(/\d+/g) || []).map(Number)
  const pb = (b.match(/\d+/g) || []).map(Number)
  const n = Math.max(pa.length, pb.length)
  for (let i = 0; i < n; i++) {
    const x = pa[i] ?? 0
    const y = pb[i] ?? 0
    if (x !== y) return x > y
  }
  return true
}

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: string } }; message?: string }
  return err?.response?.data?.detail || err?.message || fallback
}

function onPick(e: Event): void {
  const input = e.target as HTMLInputElement
  picked.value = input.files?.[0] || null
  uploadError.value = ''
}

/** 打开系统文件选择框 */
function openFilePicker(): void {
  fileInput.value?.click()
}

function resetForm(): void {
  picked.value = null
  formVersion.value = ''
  formNotes.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

async function upload(): Promise<void> {
  if (!picked.value) {
    uploadError.value = '请先选择安装包 zip'
    return
  }
  uploading.value = true
  uploadError.value = ''
  try {
    await store.upload(picked.value, {
      version: formVersion.value.trim(),
      notes: formNotes.value.trim(),
    })
    ElMessage.success('安装包已上传')
    resetForm()
  } catch (e: unknown) {
    uploadError.value = errText(e, '上传失败，请稍后重试')
    await ElMessageBox.alert(uploadError.value, '无法上传', {
      type: 'warning',
      confirmButtonText: '知道了',
    })
  } finally {
    uploading.value = false
  }
}

/** 发布：目标版本低于当前发布版本时，追加一道输入确认 */
async function publish(v: ClientVersionOut): Promise<void> {
  const current = store.release.version
  const isDowngrade = Boolean(current) && !isAtLeast(v.version, current)

  if (!isDowngrade) {
    const ok = await confirmAction(
      `确认把 v${v.version} 设为当前发布版本？\n\n运维面板检测更新后将据此更新各节点。`,
    )
    if (!ok) return
  } else {
    try {
      const { value } = await ElMessageBox.prompt(
        `v${v.version} 低于当前发布版本 v${current}，这是一次降级。\n\n降级可能把已修复的问题带回线上。\n请输入「${DOWNGRADE_CONFIRM_TEXT}」以继续。`,
        '降级二次确认',
        {
          confirmButtonText: '确认降级',
          cancelButtonText: '取消',
          inputPattern: new RegExp(`^${DOWNGRADE_CONFIRM_TEXT}$`),
          inputErrorMessage: `请输入：${DOWNGRADE_CONFIRM_TEXT}`,
          type: 'warning',
          closeOnClickModal: false,
        },
      )
      if (value !== DOWNGRADE_CONFIRM_TEXT) return
    } catch {
      return
    }
  }

  try {
    await store.release_(v.version, isDowngrade)
    ElMessage.success(isDowngrade ? `已降级发布到 v${v.version}` : `已发布 v${v.version}`)
  } catch (e: unknown) {
    ElMessage.error(errText(e, '发布失败，请稍后重试'))
  }
}

async function rollback(): Promise<void> {
  const prev = store.release.previous
  if (!prev) return
  try {
    const { value } = await ElMessageBox.prompt(
      `将把发布版本从 v${store.release.version} 回退到 v${prev}。\n\n运维面板下次检测更新时会据此把节点改回旧版本。\n请输入「${ROLLBACK_CONFIRM_TEXT}」以继续。`,
      '回滚二次确认',
      {
        confirmButtonText: '确认回滚',
        cancelButtonText: '取消',
        inputPattern: new RegExp(`^${ROLLBACK_CONFIRM_TEXT}$`),
        inputErrorMessage: `请输入：${ROLLBACK_CONFIRM_TEXT}`,
        type: 'warning',
        closeOnClickModal: false,
      },
    )
    if (value !== ROLLBACK_CONFIRM_TEXT) return
  } catch {
    return
  }
  try {
    await store.rollback()
    ElMessage.success(`已回滚到 v${prev}`)
  } catch (e: unknown) {
    ElMessage.error(errText(e, '回滚失败，请稍后重试'))
  }
}

async function remove(v: ClientVersionOut): Promise<void> {
  const ok = await confirmAction(
    `确认删除版本 v${v.version}？\n\n安装包文件会一并删除，该操作不可恢复。`,
    '确认删除',
  )
  if (!ok) return
  try {
    await store.remove(v.version)
    ElMessage.success(`已删除 v${v.version}`)
  } catch (e: unknown) {
    ElMessage.error(errText(e, '删除失败，请稍后重试'))
  }
}

onMounted(async () => {
  await store.fetch()
  if (!hub.nodes.length) await hub.fetchNodes()
})
</script>

<template>
  <div class="pf-page cv-page">
    <!-- 当前发布版本 -->
    <section class="pf-card">
      <div class="pf-card-head">
        <div class="pf-card-head-main">
          <span class="pf-card-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z" />
              <path d="M3.27 6.96 12 12l8.73-5.04M12 22V12" />
            </svg>
          </span>
          <div>
            <div class="pf-card-title-row">
              <p class="pf-card-title">当前发布版本</p>
              <span v-if="store.release.version" class="pf-pill">
                <span class="pf-pill-dot" aria-hidden="true"></span>发布中
              </span>
            </div>
            <p class="cv-release-version">
              {{ store.release.version ? `v${store.release.version}` : '尚未发布' }}
            </p>
          </div>
        </div>
        <div class="cv-release-time">
          <p class="pf-metric-k">发布时间</p>
          <p class="pf-metric-v">{{ fmtTime(store.release.updated_at) }}</p>
        </div>
      </div>

      <div class="pf-metrics cv-metrics">
        <div class="pf-metric">
          <p class="pf-metric-k">已在此版本节点</p>
          <p class="pf-metric-v">{{ currentItem ? `${currentItem.node_count} 个` : '—' }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">安装包大小</p>
          <p class="pf-metric-v">{{ currentItem ? fmtSize(currentItem.size) : '—' }}</p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">校验和</p>
          <p class="pf-metric-v" :title="currentItem?.sha256 || ''">
            {{ currentItem ? shortHash(currentItem.sha256) : '—' }}
          </p>
        </div>
        <div class="pf-metric">
          <p class="pf-metric-k">发布状态</p>
          <p class="pf-metric-v">{{ store.release.version ? '分发中' : '未发布' }}</p>
        </div>
      </div>
    </section>

    <!-- 上一版本 + 回滚 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>上一版本</p>
      </div>
      <div class="cv-prev">
        <div class="cv-prev-main">
          <p class="cv-prev-version">
            {{ store.release.previous ? `v${store.release.previous}` : '暂无上一版本' }}
          </p>
          <p class="pf-hint cv-prev-hint">
            <template v-if="store.release.previous">
              {{ unknownCount }} 个节点未上报版本号（旧客户端不上报版本号，升级后即可显示）
            </template>
            <template v-else>发布过一次版本后，这里会出现可回滚的历史版本。</template>
          </p>
        </div>
        <button
          type="button"
          class="pf-btn cv-rollback-btn"
          :disabled="!store.canRollback"
          :title="store.canRollback ? '' : '没有可回滚的历史版本'"
          @click="rollback"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 7v6h6" />
            <path d="M3.51 13a9 9 0 1 0 2.13-9.36L3 7" />
          </svg>
          回滚到上一版本
        </button>
      </div>
    </section>

    <!-- 上传安装包 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>上传安装包</p>
      </div>

      <div class="pf-note ok cv-upload-note">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <path d="m9 11 3 3L22 4" />
        </svg>
        <p>
          zip 内为相对客户端目录的文件树，至少包含 node_client.exe，建议附 version.txt；
          覆盖安装时 .env 不会被覆盖；版本号留空则取包内 version.txt。
        </p>
      </div>

      <div class="pf-grid-3 cv-upload-grid">
        <div>
          <label class="pf-label" for="cv-file">安装包（zip）</label>
          <input ref="fileInput" id="cv-file" type="file" accept=".zip" class="cv-file-input" :disabled="uploading" @change="onPick" />
          <button type="button" class="pf-file-picker" :disabled="uploading" @click="openFilePicker">
            <span class="pf-file-picker-main">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="2" y="3" width="20" height="5" rx="1" />
                <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
                <path d="M10 12h4" />
              </svg>
              <span>{{ picked ? picked.name : '选择文件（留空读取包内 version.txt）' }}</span>
            </span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <path d="M17 8l-5-5-5 5" />
              <path d="M12 3v12" />
            </svg>
          </button>
        </div>
        <div>
          <label class="pf-label" for="cv-version">版本号（可选）</label>
          <input id="cv-version" v-model="formVersion" class="pf-input" placeholder="留空读取包内 version.txt" :disabled="uploading" />
        </div>
        <div>
          <label class="pf-label" for="cv-notes">本次变更内容</label>
          <textarea
            id="cv-notes"
            v-model="formNotes"
            class="pf-input cv-notes"
            rows="3"
            placeholder="填写本次版本变更内容，将随版本记录展示"
            :disabled="uploading"
          ></textarea>
        </div>
      </div>

      <p v-if="uploading" class="pf-hint cv-progress">上传中… {{ uploadPct }}%</p>
      <p v-if="uploadError" class="pf-error">{{ uploadError }}</p>

      <div class="cv-upload-actions">
        <button type="button" class="pf-btn pf-btn-primary cv-upload-btn" :disabled="uploading || !picked" @click="upload">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <path d="M17 8l-5-5-5 5" />
            <path d="M12 3v12" />
          </svg>
          {{ uploading ? `上传中… ${uploadPct}%` : '上传并发布' }}
        </button>
        <button type="button" class="pf-btn" :disabled="uploading" @click="resetForm">清空</button>
      </div>
    </section>

    <!-- 已上传版本 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>已上传版本</p>
        <span class="pf-count">共 {{ store.items.length }} 个版本</span>
      </div>

      <div class="pf-rows">
        <div class="pf-rows-inner">
          <div class="pf-row cv-thead">
            <span>版本号</span>
            <span>状态</span>
            <span>大小</span>
            <span>校验和</span>
            <span>更新说明</span>
            <span>节点数</span>
            <span>上传时间</span>
            <span>操作</span>
          </div>
          <div v-for="v in store.items" :key="v.version" class="pf-row cv-row">
            <span class="c-mono c-strong">v{{ v.version }}</span>
            <span>
              <span v-if="v.is_current" class="pf-pill">
                <span class="pf-pill-dot" aria-hidden="true"></span>发布中
              </span>
              <span v-else-if="isNewest(v)" class="pf-pill blue">最新</span>
              <span v-else class="pf-pill grey">历史</span>
            </span>
            <span class="c-muted">{{ fmtSize(v.size) }}</span>
            <span class="c-mono c-muted" :title="v.sha256">{{ shortHash(v.sha256) }}</span>
            <span class="c-muted" :title="v.notes || ''">{{ v.notes || '—' }}</span>
            <span class="c-muted">{{ v.node_count }}</span>
            <span class="c-muted">{{ fmtDate(v.created_at) }}</span>
            <span class="cv-actions">
              <button type="button" class="cv-action-link" :disabled="v.is_current" @click="publish(v)">
                {{ v.is_current ? '发布中' : '设为发布版本' }}
              </button>
              <button type="button" class="cv-action-link danger" :disabled="v.is_current" @click="remove(v)">删除</button>
            </span>
          </div>
          <p v-if="!store.items.length && !store.loading" class="pf-empty">暂无更多已上传版本</p>
          <p v-else-if="store.loading" class="pf-empty">加载中…</p>
        </div>
      </div>
    </section>

    <!-- 节点版本分布 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>节点版本分布</p>
        <span class="pf-count">{{ hub.nodes.length }} 个节点</span>
      </div>
      <p class="pf-hint cv-dist-hint">版本由节点在鉴权时上报，反映各节点实际运行的程序版本。</p>
      <ul class="pf-list cv-dist">
        <li v-for="n in hub.nodes" :key="n.node_id">
          <span class="pf-list-k">{{ n.name }}</span>
          <span class="pf-list-v">
            <span class="pf-mono">{{ n.client_version ? `v${n.client_version}` : '未知' }}</span>
            <span
              v-if="n.client_version && store.release.version && n.client_version !== store.release.version"
              class="pf-pill amber"
            >
              待更新
            </span>
          </span>
        </li>
        <li v-if="!hub.nodes.length">
          <span class="pf-list-k">暂无节点</span>
          <span class="pf-list-v">—</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
/* 版式 1:1 复刻 UI/H5.html 的「客户端版本管理」tab；
   卡片 / 按钮 / 药丸等共用外观见 styles.css 的 pf- 前缀类。 */
.cv-page {
  max-width: 860px;
}

.pf-card-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.cv-release-version {
  margin: 2px 0 0;
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--pf-text);
  word-break: break-all;
}

.cv-release-time {
  flex-shrink: 0;
  text-align: right;
}
.cv-release-time .pf-metric-v {
  margin-top: 2px;
}

.cv-metrics {
  margin-top: 12px;
}

/* ── 上一版本 ── */
.cv-prev {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
}

.cv-prev-main { min-width: 0; }

.cv-prev-version {
  margin: 0;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--pf-text);
  word-break: break-all;
}

.cv-prev-hint { margin-top: 4px; }

.cv-rollback-btn {
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.4);
  background: rgba(245, 158, 11, 0.1);
}
.cv-rollback-btn:hover:not(:disabled) {
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.55);
  background: rgba(245, 158, 11, 0.2);
}

/* ── 上传安装包 ── */
.cv-upload-note {
  margin-top: 10px;
}

.cv-upload-grid {
  margin-top: 12px;
}

.cv-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.cv-notes {
  min-height: 74px;
}

.cv-progress { margin-top: 12px; }

.cv-upload-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.cv-upload-btn {
  height: 38px;
  padding: 0 24px;
  font-size: 13px;
}

/* ── 已上传版本 ── */
.cv-thead,
.cv-row {
  grid-template-columns: minmax(130px, 1.3fr) 88px 84px 132px minmax(0, 1.4fr) 64px 108px 150px;
}

.cv-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  overflow: visible;
}

.cv-action-link {
  padding: 0;
  border: none;
  background: none;
  color: #3b82f6;
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: color 0.15s;
}
.cv-action-link:hover:not(:disabled) { color: #60a5fa; }
.cv-action-link:disabled { color: var(--pf-faint); cursor: not-allowed; }
.cv-action-link.danger { color: #f87171; }
.cv-action-link.danger:hover:not(:disabled) { color: #fca5a5; }

/* ── 节点版本分布 ── */
.cv-dist-hint { margin-top: 8px; }

.cv-dist .pf-list-v {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
