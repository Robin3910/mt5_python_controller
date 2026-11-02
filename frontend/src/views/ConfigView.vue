<script setup lang="ts">
// 配置页：**系统级**配置（节点令牌 / Webhook 开关 / 短信服务，仅管理员）。
//
// 个人自己的设置（个人信息 / 修改密码 / 2FA）已搬到「个人中心」/profile ——
// 那是登录即可进的地方；配置页留给管理员的系统级开关。
import { onMounted, reactive, ref } from 'vue'
import { ElSwitch } from 'element-plus'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { copyToClipboard } from '@/utils/clipboard'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()
const auth = useAuthStore()

const savedFlag = ref('')

const nodeToken = reactive({ value: '', updated_at: 0 })
const nodeTokenShow = ref(false)
const nodeTokenLoading = ref(false)
const nodeTokenError = ref('')
const webhookAuthEnabled = ref(true)
const webhookAuthLoading = ref(false)

const sms = reactive({
  enabled: false,
  provider: 'aliyun_sms_auth',
  endpoint: 'dypnsapi.aliyuncs.com',
  sign_name: '',
  template_code: '100001',
  require_approval: true,
  code_ttl_seconds: 300,
  send_interval_seconds: 60,
  daily_limit_per_phone: 10,
  hourly_limit_per_ip: 20,
  max_verify_attempts: 5,
  today_sent: 0,
  credentials_present: false,
})
const smsLoading = ref(false)
const smsError = ref('')

onMounted(async () => {
  if (!auth.isAdmin) return
  await loadNodeToken()
  await loadWebhookAuth()
  await loadSmsConfig()
})

async function loadSmsConfig(): Promise<void> {
  try {
    const cfg = await hub.fetchSmsConfig()
    Object.assign(sms, cfg)
  } catch {
    smsError.value = '无法加载短信配置'
  }
}

async function saveSmsConfig(): Promise<void> {
  smsError.value = ''
  smsLoading.value = true
  try {
    const cfg = await hub.setSmsConfig({ ...sms })
    Object.assign(sms, cfg)
    flash('短信配置已保存')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    smsError.value = err?.response?.data?.detail || '保存失败，请稍后重试'
  } finally {
    smsLoading.value = false
  }
}

function onSmsEnabledChange(value: boolean | string | number): void {
  sms.enabled = value === true || value === 'true' || value === 1
}

function onSmsApprovalChange(value: boolean | string | number): void {
  sms.require_approval = value === true || value === 'true' || value === 1
}

async function loadNodeToken(): Promise<void> {
  try {
    const t = await hub.fetchNodeToken()
    nodeToken.value = t.token
    nodeToken.updated_at = t.updated_at
  } catch {
    nodeTokenError.value = '无法加载节点令牌'
  }
}

async function copyNodeToken(): Promise<void> {
  if (!nodeToken.value) return
  nodeTokenError.value = ''
  const ok = await copyToClipboard(nodeToken.value)
  if (ok) {
    flash('节点令牌已复制到剪贴板')
    return
  }
  nodeTokenShow.value = true
  nodeTokenError.value = '自动复制失败，请手动选中上方令牌后 Ctrl+C'
}

async function rotateNodeToken(): Promise<void> {
  if (!(await confirmAction('重置全局节点令牌？\n\n所有节点的 .env 都需更新后才能继续接入。', '确认重置令牌'))) return
  nodeTokenLoading.value = true
  nodeTokenError.value = ''
  try {
    const t = await hub.rotateNodeToken()
    nodeToken.value = t.token
    nodeToken.updated_at = t.updated_at
    nodeTokenShow.value = true
    flash('节点令牌已重置，请尽快同步到所有节点 .env')
  } catch {
    nodeTokenError.value = '重置失败，请稍后重试'
  } finally {
    nodeTokenLoading.value = false
  }
}

async function loadWebhookAuth(): Promise<void> {
  try {
    const info = await hub.fetchWebhookAuth()
    webhookAuthEnabled.value = info.enabled
  } catch {
    nodeTokenError.value = nodeTokenError.value || '无法加载 Webhook 鉴权开关'
  }
}

async function onWebhookAuthChange(value: boolean | string | number): Promise<void> {
  const next = value === true || value === 'true' || value === 1
  if (next === webhookAuthEnabled.value || webhookAuthLoading.value) return
  if (!next) {
    const ok = await confirmAction(
      '关闭后，调用 /webhook 不再校验 token。知道该地址的请求都可以下发信号。',
      '关闭 Webhook 鉴权',
    )
    if (!ok) return
  }
  webhookAuthLoading.value = true
  nodeTokenError.value = ''
  try {
    const info = await hub.setWebhookAuth(next)
    webhookAuthEnabled.value = info.enabled
    flash(info.enabled ? '已开启 Webhook token 校验' : '已关闭 Webhook token 校验')
  } catch {
    nodeTokenError.value = 'Webhook 鉴权开关保存失败'
  } finally {
    webhookAuthLoading.value = false
  }
}

function fmtTokenUpdatedAt(): string {
  if (!nodeToken.updated_at) return '—'
  return new Date(nodeToken.updated_at * 1000).toLocaleString()
}

function flash(msg: string): void {
  savedFlag.value = msg
  setTimeout(() => (savedFlag.value = ''), 1800)
}

</script>

<template>
  <div class="row between page-header">
    <div class="h1">配置</div>
    <span v-if="savedFlag" class="tag green">{{ savedFlag }}</span>
  </div>

  <div class="grid layout-config">
    <div v-if="auth.isAdmin" class="card card-pad span-full">
      <div class="row between">
        <strong>节点令牌 (NODE_TOKEN)</strong>
        <span class="muted" style="font-size: 12px">最近更新：{{ fmtTokenUpdatedAt() }}</span>
      </div>
      <p class="muted" style="font-size: 12px">
        所有 node_client 共享此令牌进行接入鉴权。将其填入每个节点 <code>.env</code> 的
        <code>NODE_TOKEN</code>。点击「重置」会立即作废旧令牌，所有节点必须更新后才能重新接入。
      </p>
      <div class="webhook-auth-row">
        <span class="webhook-auth-label">Webhook 校验 token</span>
        <ElSwitch
          :model-value="webhookAuthEnabled"
          :loading="webhookAuthLoading"
          :disabled="webhookAuthLoading"
          @change="onWebhookAuthChange"
        />
        <span class="muted webhook-auth-hint">
          {{ webhookAuthEnabled ? '开启：调用 /webhook 必须携带 AUTH_TOKEN' : '关闭：调用 /webhook 不校验 token' }}
        </span>
      </div>
      <div class="form-grid">
        <div>
          <label>当前令牌</label>
          <div class="row" style="gap: 8px; align-items: center">
            <input
              :value="nodeToken.value"
              :type="nodeTokenShow ? 'text' : 'password'"
              readonly
              style="flex: 1; font-family: ui-monospace, monospace"
            />
            <button class="btn-sm btn-ghost" type="button" @click="nodeTokenShow = !nodeTokenShow">
              {{ nodeTokenShow ? '隐藏' : '显示' }}
            </button>
            <button class="btn-sm btn-ghost" type="button" :disabled="!nodeToken.value" @click="copyNodeToken">复制</button>
          </div>
        </div>
        <div v-if="nodeTokenError" style="color: var(--red); font-size: 13px">{{ nodeTokenError }}</div>
        <div class="row" style="gap: 8px">
          <button class="btn-danger btn-sm" :disabled="nodeTokenLoading" @click="rotateNodeToken">
            {{ nodeTokenLoading ? '重置中…' : '重置令牌' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="auth.isAdmin" class="card card-pad span-full">
      <div class="row between">
        <strong>短信服务（手机号注册 / 验证码登录）</strong>
        <span class="muted" style="font-size: 12px">今日已发送：{{ sms.today_sent }} 条</span>
      </div>
      <p class="muted" style="font-size: 12px">
        验证码由本系统生成并校验，阿里云短信认证只负责把短信送达。关闭开关后所有短信接口立即不可用，
        前端会自动隐藏验证码入口，无需重启。
      </p>
      <div class="webhook-auth-row">
        <span class="webhook-auth-label">启用短信服务</span>
        <ElSwitch
          :model-value="sms.enabled"
          :disabled="smsLoading"
          @change="onSmsEnabledChange"
        />
        <span class="muted webhook-auth-hint">
          {{ sms.enabled ? '已启用：注册与验证码登录入口可见' : '已停用：所有短信接口返回 404' }}
        </span>
      </div>
      <div class="webhook-auth-row">
        <span class="webhook-auth-label">注册需管理员审核</span>
        <ElSwitch
          :model-value="sms.require_approval"
          :disabled="smsLoading"
          @change="onSmsApprovalChange"
        />
        <span class="muted webhook-auth-hint">
          {{ sms.require_approval ? '开启：新注册用户默认禁用，管理员启用后才能登录' : '关闭：新注册用户注册即可登录' }}
        </span>
      </div>
      <div class="form-grid">
        <div>
          <label>短信签名</label>
          <input v-model="sms.sign_name" placeholder="阿里云控制台已审核通过的签名" />
        </div>
        <div>
          <label>模板 Code</label>
          <input v-model="sms.template_code" placeholder="例如 SMS_123456789" />
        </div>
        <div>
          <label>AccessKey</label>
          <span class="tag" :class="sms.credentials_present ? 'green' : 'amber'">
            {{ sms.credentials_present ? '已注入环境变量' : '未注入（无法发送）' }}
          </span>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            密钥只从后端环境变量读取，页面永不回显。
          </p>
        </div>
        <div>
          <label>验证码有效期（秒）</label>
          <input v-model.number="sms.code_ttl_seconds" type="number" min="60" max="1800" />
        </div>
        <div>
          <label>重发冷却（秒）</label>
          <input v-model.number="sms.send_interval_seconds" type="number" min="10" max="3600" />
        </div>
        <div>
          <label>每号码每日上限</label>
          <input v-model.number="sms.daily_limit_per_phone" type="number" min="1" max="1000" />
        </div>
        <div>
          <label>每 IP 每小时上限</label>
          <input v-model.number="sms.hourly_limit_per_ip" type="number" min="1" max="5000" />
        </div>
        <div>
          <label>验证码最大校验次数</label>
          <input v-model.number="sms.max_verify_attempts" type="number" min="1" max="20" />
        </div>
        <div v-if="smsError" style="color: var(--red); font-size: 13px">{{ smsError }}</div>
        <div class="row" style="gap: 8px">
          <button class="btn-primary" :disabled="smsLoading" @click="saveSmsConfig">
            {{ smsLoading ? '保存中…' : '保存短信配置' }}
          </button>
        </div>
      </div>
    </div>

      </div>
</template>

<style scoped>
.webhook-auth-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 4px 0 14px;
}
.webhook-auth-label {
  font-size: 13px;
  font-weight: 600;
}
.webhook-auth-hint {
  font-size: 12px;
}
</style>
