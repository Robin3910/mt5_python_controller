<script setup lang="ts">
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

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
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
  <section class="grid">
    <div class="page-header span-full">
      <div>
        <div class="h1">客户端版本管理</div>
        <p class="page-desc">
          上传 node_client 安装包并指定发布版本；本机运维面板检测到新版本后，可批量更新、降级或回滚各节点。
        </p>
      </div>
    </div>

    <div class="card card-pad span-full">
      <div class="row between" style="align-items: flex-start">
        <div>
          <div class="muted" style="font-size: 12px">当前发布版本</div>
          <div class="h1" style="margin-top: 4px">
            {{ store.release.version ? `v${store.release.version}` : '尚未发布' }}
            <span v-if="store.current" class="tag green" style="margin-left: 8px">
              {{ store.current.node_count }} 个节点已在此版本
            </span>
          </div>
          <div class="muted" style="font-size: 12px; margin-top: 6px">
            <template v-if="store.release.version">
              发布于 {{ fmtTime(store.release.updated_at) }}
              <template v-if="store.release.previous">
                　·　上一版本 v{{ store.release.previous }}
              </template>
            </template>
            <template v-else>
              上传安装包后，点「设为发布版本」即可让各节点面板检测到更新
            </template>
          </div>
          <div v-if="unknownCount" class="muted" style="font-size: 12px; margin-top: 4px">
            另有 {{ unknownCount }} 个节点未上报版本（旧客户端不上报版本号，升级后即可显示）
          </div>
        </div>
        <button
          class="btn-danger"
          :disabled="!store.canRollback"
          :title="store.canRollback ? '' : '没有可回滚的历史版本'"
          @click="rollback"
        >
          回滚到 v{{ store.release.previous || '—' }}
        </button>
      </div>
    </div>

    <div class="card card-pad span-full">
      <div class="h1" style="font-size: 16px">上传安装包</div>
      <p class="muted" style="font-size: 12px; margin: 4px 0 12px">
        zip 内为相对客户端目录的文件树（至少包含 node_client.exe，建议附 version.txt）。
        覆盖安装时 .env 不会被替换。版本号留空则读取包内 version.txt。
      </p>

      <div class="form-grid three">
        <div>
          <label style="font-size: 12px">安装包</label>
          <input ref="fileInput" type="file" accept=".zip" :disabled="uploading" @change="onPick" />
        </div>
        <div>
          <label style="font-size: 12px">版本号（可选）</label>
          <input v-model="formVersion" placeholder="留空读包内 version.txt" :disabled="uploading" />
        </div>
        <div>
          <label style="font-size: 12px">更新说明（可选）</label>
          <input v-model="formNotes" placeholder="本次变更内容" :disabled="uploading" />
        </div>
      </div>

      <div v-if="uploading" class="muted" style="font-size: 12px; margin-top: 10px">
        上传中… {{ uploadPct }}%
      </div>
      <div v-if="uploadError" class="alert alert-error" style="margin-top: 10px">
        {{ uploadError }}
      </div>

      <div class="row" style="gap: 8px; margin-top: 12px">
        <button class="btn-primary" :disabled="uploading || !picked" @click="upload">
          {{ uploading ? `上传中… ${uploadPct}%` : '上传' }}
        </button>
        <button class="btn-ghost" :disabled="uploading" @click="resetForm">清空</button>
      </div>
    </div>

    <div class="card table-scroll desktop-only span-full">
      <table>
        <thead>
          <tr>
            <th>版本</th>
            <th>大小</th>
            <th>校验和</th>
            <th>更新说明</th>
            <th>节点数</th>
            <th>上传者</th>
            <th>上传时间</th>
            <th class="right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in store.items" :key="v.version">
            <td>
              v{{ v.version }}
              <span v-if="v.is_current" class="tag green" style="margin-left: 6px">发布中</span>
              <span v-else-if="isNewest(v)" class="tag blue" style="margin-left: 6px">最新</span>
            </td>
            <td>{{ fmtSize(v.size) }}</td>
            <td class="muted" :title="v.sha256">{{ shortHash(v.sha256) }}</td>
            <td>{{ v.notes || '—' }}</td>
            <td>{{ v.node_count }}</td>
            <td>{{ v.uploaded_by || '—' }}</td>
            <td>{{ fmtTime(v.created_at) }}</td>
            <td class="right">
              <button class="btn-primary btn-sm" :disabled="v.is_current" @click="publish(v)">
                {{ v.is_current ? '发布中' : '设为发布版本' }}
              </button>
              <button
                class="btn-danger btn-sm"
                style="margin-left: 6px"
                :disabled="v.is_current"
                :title="v.is_current ? '当前发布版本不可删除' : ''"
                @click="remove(v)"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="!store.items.length && !store.loading">
            <td colspan="8" class="muted" style="padding: 18px">
              暂无安装包，请先在上方上传客户端 zip
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="list-cards mobile-only span-full">
      <div v-for="v in store.items" :key="v.version" class="list-card card">
        <div class="list-card-head row between">
          <strong>v{{ v.version }}</strong>
          <span v-if="v.is_current" class="tag green">发布中</span>
          <span v-else-if="isNewest(v)" class="tag blue">最新</span>
        </div>
        <div class="list-field">
          <span class="k">大小</span><span class="v">{{ fmtSize(v.size) }}</span>
        </div>
        <div class="list-field">
          <span class="k">校验和</span>
          <span class="v muted" style="font-size: 12px">{{ shortHash(v.sha256) }}</span>
        </div>
        <div class="list-field">
          <span class="k">说明</span><span class="v">{{ v.notes || '—' }}</span>
        </div>
        <div class="list-field">
          <span class="k">节点数</span><span class="v">{{ v.node_count }}</span>
        </div>
        <div class="list-field">
          <span class="k">上传者</span><span class="v">{{ v.uploaded_by || '—' }}</span>
        </div>
        <div class="list-field">
          <span class="k">上传时间</span><span class="v">{{ fmtTime(v.created_at) }}</span>
        </div>
        <div class="list-card-actions row" style="gap: 8px">
          <button class="btn-primary btn-sm" :disabled="v.is_current" @click="publish(v)">
            {{ v.is_current ? '发布中' : '设为发布版本' }}
          </button>
          <button class="btn-danger btn-sm" :disabled="v.is_current" @click="remove(v)">
            删除
          </button>
        </div>
      </div>
      <div v-if="!store.items.length && !store.loading" class="card card-pad muted">
        暂无安装包，请先在上方上传客户端 zip
      </div>
    </div>

    <div class="card card-pad span-full">
      <div class="h1" style="font-size: 16px">节点版本分布</div>
      <p class="muted" style="font-size: 12px; margin: 4px 0 12px">
        版本由节点在鉴权时上报，反映各节点实际运行的程序版本。
      </p>
      <div class="kv-grid">
        <div v-for="n in hub.nodes" :key="n.node_id" class="kv">
          <span class="k">{{ n.name }}</span>
          <span class="v">
            {{ n.client_version ? `v${n.client_version}` : '未知' }}
            <span
              v-if="
                n.client_version &&
                store.release.version &&
                n.client_version !== store.release.version
              "
              class="tag amber"
              style="margin-left: 6px"
            >
              待更新
            </span>
          </span>
        </div>
        <div v-if="!hub.nodes.length" class="muted">暂无节点</div>
      </div>
    </div>
  </section>
</template>
