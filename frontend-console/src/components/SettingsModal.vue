<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, type ConnectionInfo, type EnvPayload, type VersionInfo } from '../api/bridge'
import AppModal from './AppModal.vue'

const props = defineProps<{
  open: boolean
  selectedId: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const tab = ref<'connection' | 'version' | 'env' | 'diagnostics'>('connection')

const conn = ref<ConnectionInfo | null>(null)
const version = ref<VersionInfo | null>(null)
const env = ref<EnvPayload | null>(null)
/** 启动体检报告（白屏排查用）。 */
const diagnose = ref<Record<string, any> | null>(null)
/** 前端脚本错误。 */
const clientLogs = ref<{ count: number; entries: any[] } | null>(null)

const envText = ref('')
const message = ref('')
const error = ref('')
const busy = ref(false)

async function loadConnection() {
  busy.value = true
  error.value = ''
  try {
    conn.value = await api.connection()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

async function testConnection() {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const res = await api.testConnection()
    message.value = res.message
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

async function loadVersion() {
  busy.value = true
  error.value = ''
  try {
    version.value = await api.versionInfo()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

/** 载入启动体检报告与前端脚本错误。 */
async function loadDiagnostics() {
  busy.value = true
  error.value = ''
  try {
    diagnose.value = await api.diagnose()
    clientLogs.value = await api.clientLogs()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

async function loadEnv() {
  if (!props.selectedId) {
    error.value = '请先在列表中选择一个实例'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const res = await api.readEnv(props.selectedId)
    env.value = res
    envText.value = res.text
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

async function saveEnv() {
  if (!props.selectedId) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    await api.writeEnv(props.selectedId, envText.value)
    message.value = '已保存。配置在 node_client 进程内是一次性加载的，需重启实例后生效。'
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

// 打开弹窗时回到第一个页签并加载它的数据。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    switchTab('connection')
  },
)

/**
 * 切换页签。
 *
 * 点当前页签也要重新加载 —— 用户很可能就是想刷新一下。
 *
 * 只在这里加载，不要再挂一个 watch(tab)：`tab.value = next` 在值没变时
 * 不触发 watcher，所以 watch 方案覆盖不了「点当前页签刷新」；
 * 而两者并存会让每次切换都加载两次。
 */
function switchTab(next: typeof tab.value): void {
  tab.value = next
  message.value = ''
  error.value = ''
  if (next === 'connection') void loadConnection()
  else if (next === 'version') void loadVersion()
  else if (next === 'env') void loadEnv()
  else if (next === 'diagnostics') void loadDiagnostics()
}
</script>

<template>
  <AppModal
    v-if="open"
    title="系统设置"
    subtitle="后端连接 · 版本信息 · 实例环境"
    @close="emit('close')"
  >
    <div class="settings-tabs">
      <button
        class="btn-sm"
        :class="{ primary: tab === 'connection' }"
        @click="switchTab('connection')"
      >
        后端连接
      </button>
      <button
        class="btn-sm"
        :class="{ primary: tab === 'version' }"
        @click="switchTab('version')"
      >
        版本信息
      </button>
      <button
        class="btn-sm"
        :class="{ primary: tab === 'env' }"
        @click="switchTab('env')"
      >
        实例环境
      </button>
      <button
        class="btn-sm"
        :class="{ primary: tab === 'diagnostics' }"
        @click="switchTab('diagnostics')"
      >
        启动诊断
      </button>
    </div>

    <p v-if="error" class="settings-error">{{ error }}</p>
    <p v-if="message" class="settings-ok">{{ message }}</p>

    <!-- 后端连接 -->
    <template v-if="tab === 'connection'">
      <div class="form-field">
        <label>API 地址</label>
        <input class="input-field" :value="conn?.base ?? ''" disabled />
        <p class="hint-line">后端地址读取程序目录 .env 的 APP_URL，不能在此修改。</p>
      </div>
      <div class="form-field">
        <label>当前用户</label>
        <input class="input-field" :value="conn?.username ?? ''" disabled />
      </div>
      <p class="hint-line">
        可见节点 {{ conn?.node_count ?? 0 }} 个 ·
        {{ conn?.is_admin ? '管理员账号' : '普通账号' }}
      </p>
      <button class="btn-sm" :disabled="busy" @click="testConnection">
        验证当前连接
      </button>
    </template>

    <!-- 版本信息 -->
    <template v-else-if="tab === 'version'">
      <div class="form-field">
        <label>后端当前发布版本</label>
        <div class="version-info">
          <span class="mono">{{ version?.published || '（未发布任何版本）' }}</span>
          <span v-if="version?.published" class="latest-tag">最新</span>
        </div>
      </div>
      <div class="form-field">
        <label>可下载版本</label>
        <div class="version-list">
          <span v-for="item in version?.available ?? []" :key="String(item.version)" class="mono">
            {{ item.version }}
          </span>
          <span v-if="(version?.available ?? []).length === 0" class="hint-line">
            后端还没有可下载的安装包
          </span>
        </div>
      </div>
    </template>

    <!-- 实例环境 -->
    <template v-else-if="tab === 'env'">
      <div v-if="!selectedId" class="hint-line">请先在节点列表中选择一个实例。</div>
      <template v-else>
        <div class="form-field">
          <label>node_client .env</label>
          <textarea v-model="envText" class="input-field env-editor" spellcheck="false" />
          <p class="hint-line">{{ env?.env_path }}</p>
        </div>
        <button class="btn-sm primary" :disabled="busy" @click="saveEnv">保存</button>
      </template>
    </template>

    <!-- 启动诊断 -->
    <template v-else>
      <div class="form-field">
        <label>体检结论</label>
        <p :class="diagnose?.ok ? 'diag-ok' : 'diag-bad'">
          {{ diagnose?.verdict ?? '（未加载）' }}
        </p>
      </div>

      <div v-if="diagnose?.problems?.length" class="form-field">
        <label>发现的问题</label>
        <ul class="diag-list">
          <li v-for="p in diagnose.problems" :key="p">{{ p }}</li>
        </ul>
      </div>

      <div class="form-field">
        <label>环境检查项</label>
        <table class="diag-table">
          <tbody>
            <tr>
              <td>Edge WebView2 Runtime</td>
              <td :class="diagnose?.webview2_runtime?.ok ? 'diag-ok' : 'diag-bad'">
                {{ diagnose?.webview2_runtime?.ok ? '正常' : '异常' }}
              </td>
              <td class="diag-detail">{{ diagnose?.webview2_runtime?.detail }}</td>
            </tr>
            <tr>
              <td>.NET Framework</td>
              <td :class="diagnose?.dotnet?.ok ? 'diag-ok' : 'diag-bad'">
                {{ diagnose?.dotnet?.ok ? '正常' : '异常' }}
              </td>
              <td class="diag-detail">{{ diagnose?.dotnet?.detail }}</td>
            </tr>
            <tr>
              <td>前端产物</td>
              <td :class="diagnose?.frontend_dist?.ok ? 'diag-ok' : 'diag-bad'">
                {{ diagnose?.frontend_dist?.ok ? '正常' : '异常' }}
              </td>
              <td class="diag-detail">{{ diagnose?.frontend_dist?.detail }}</td>
            </tr>
            <tr>
              <td>pywebview / CLR 依赖</td>
              <td :class="diagnose?.webview_package?.webview_ok ? 'diag-ok' : 'diag-bad'">
                {{ diagnose?.webview_package?.webview_ok ? '正常' : '异常' }}
              </td>
              <td class="diag-detail">{{ diagnose?.webview_package?.detail }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="form-field">
        <label>前端脚本错误（{{ clientLogs?.count ?? 0 }}）</label>
        <div v-if="clientLogs?.entries?.length" class="diag-errors">
          <div v-for="(e, i) in clientLogs.entries" :key="i" class="diag-error-row">
            <span class="diag-error-msg">{{ e.message }}</span>
            <span v-if="e.source" class="diag-error-src mono">
              {{ e.source }}:{{ e.line }}:{{ e.column }}
            </span>
          </div>
        </div>
        <p v-else class="hint-line">没有捕获到前端脚本错误。</p>
      </div>

      <p class="hint-line">
        日志文件：{{ diagnose?.log_file }}<br />
        完整报告可用命令行 <code>--diagnose</code> 生成。
      </p>
    </template>

    <template #footer>
      <button class="btn-sm" @click="emit('close')">关闭</button>
    </template>
  </AppModal>
</template>

<style scoped>
.settings-tabs {
  display: flex;
  gap: 8px;
}

.settings-error {
  margin: 0;
  padding: 9px 11px;
  font-size: 12.5px;
  color: var(--red);
  background: #f2645f1a;
  border: 1px solid rgba(242, 100, 95, 0.28);
  border-radius: 6px;
}

.settings-ok {
  margin: 0;
  padding: 9px 11px;
  font-size: 12.5px;
  color: var(--green);
  background: #2fd3811a;
  border: 1px solid rgba(47, 211, 129, 0.28);
  border-radius: 6px;
}

.version-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--dim);
}

.env-editor {
  height: 240px;
  padding: 10px 12px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}

.diag-ok {
  color: var(--green);
  font-size: 12.5px;
}

.diag-bad {
  color: var(--red);
  font-size: 12.5px;
}

.diag-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  color: var(--red);
}

.diag-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.diag-table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--line-soft);
  vertical-align: top;
}

.diag-detail {
  color: var(--dim);
}

.diag-errors {
  max-height: 180px;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #070b13;
}

.diag-error-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 11px;
  border-bottom: 1px solid var(--line-soft);
}

.diag-error-row:last-child {
  border-bottom: none;
}

.diag-error-msg {
  font-size: 12px;
  color: var(--text);
  word-break: break-all;
}

.diag-error-src {
  font-size: 11px;
  color: var(--dim);
}
</style>
