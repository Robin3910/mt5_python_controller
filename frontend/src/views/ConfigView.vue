<script setup lang="ts">
// 配置页：个人设置（改密码 / 2FA，所有登录用户）+ 系统配置（节点令牌 / Webhook 开关，仅管理员）
import { onMounted, reactive, ref } from 'vue'
import { ElSwitch } from 'element-plus'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { copyToClipboard } from '@/utils/clipboard'
import { confirmAction } from '@/utils/confirm'

const hub = useHubStore()
const auth = useAuthStore()
const router = useRouter()

const savedFlag = ref('')

const pwd = reactive({ current: '', new: '', confirm: '' })
const pwdError = ref('')
const pwdLoading = ref(false)

const twofa = reactive({ enabled: false, bound: false, pending_setup: false })
const twofaSetup = ref<{ secret: string; qr_data_uri: string } | null>(null)
const twofaCode = ref('')
const twofaPwd = ref('')
const twofaError = ref('')
const twofaLoading = ref(false)

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
  await load2faStatus()
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

async function load2faStatus(): Promise<void> {
  try {
    const s = await auth.fetch2faStatus()
    twofa.enabled = s.enabled
    twofa.bound = s.bound
    twofa.pending_setup = s.pending_setup
  } catch {
    twofaError.value = '无法加载 2FA 状态'
  }
}

function map2faError(detail?: string): string {
  if (detail === 'invalid password') return '密码不正确'
  if (detail === 'invalid totp code') return '验证码错误或已过期'
  if (detail === '2fa already enabled') return '2FA 已开启，请先关闭或重置'
  if (detail === '2fa not bound') return '尚未绑定验证器'
  return '操作失败，请稍后重试'
}

async function start2faSetup(): Promise<void> {
  if (!(await confirmAction('确认开始绑定双因素认证？\n\n将生成新的验证器密钥。', '确认绑定 2FA'))) return
  twofaError.value = ''
  twofaLoading.value = true
  try {
    twofaSetup.value = await auth.setup2fa()
    twofaCode.value = ''
    await load2faStatus()
    flash('请扫描二维码并输入验证码完成绑定')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    twofaError.value = map2faError(err?.response?.data?.detail)
  } finally {
    twofaLoading.value = false
  }
}

async function confirm2faBind(): Promise<void> {
  twofaError.value = ''
  if (!twofaCode.value.trim()) {
    twofaError.value = '请输入验证码'
    return
  }
  if (!(await confirmAction('确认绑定验证器并开启双因素认证？', '确认开启 2FA'))) return
  twofaLoading.value = true
  try {
    await auth.confirm2fa(twofaCode.value.trim())
    twofaSetup.value = null
    twofaCode.value = ''
    await load2faStatus()
    flash('双因素认证已开启')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    twofaError.value = map2faError(err?.response?.data?.detail)
  } finally {
    twofaLoading.value = false
  }
}

async function enable2fa(): Promise<void> {
  twofaError.value = ''
  if (!twofaCode.value.trim()) {
    twofaError.value = '请输入验证码以开启 2FA'
    return
  }
  if (!(await confirmAction('确认开启双因素认证？\n\n登录时将需要额外输入验证码。', '确认开启 2FA'))) return
  twofaLoading.value = true
  try {
    await auth.enable2fa(twofaCode.value.trim())
    twofaCode.value = ''
    await load2faStatus()
    flash('双因素认证已开启')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    twofaError.value = map2faError(err?.response?.data?.detail)
  } finally {
    twofaLoading.value = false
  }
}

async function disable2fa(): Promise<void> {
  twofaError.value = ''
  if (!twofaPwd.value) {
    twofaError.value = '请输入当前密码'
    return
  }
  if (!(await confirmAction('确认关闭双因素认证？\n\n关闭后登录将不再需要验证码。', '确认关闭 2FA'))) return
  twofaLoading.value = true
  try {
    await auth.disable2fa(twofaPwd.value, twofa.enabled ? twofaCode.value.trim() : undefined)
    twofaPwd.value = ''
    twofaCode.value = ''
    await load2faStatus()
    flash('双因素认证已关闭')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    twofaError.value = map2faError(err?.response?.data?.detail)
  } finally {
    twofaLoading.value = false
  }
}

async function reset2fa(): Promise<void> {
  twofaError.value = ''
  if (!twofaPwd.value) {
    twofaError.value = '请输入当前密码'
    return
  }
  if (!(await confirmAction('确认重置双因素认证绑定？\n\n需重新扫码绑定验证器。', '确认重置 2FA'))) return
  twofaLoading.value = true
  try {
    await auth.reset2fa(twofaPwd.value, twofa.enabled ? twofaCode.value.trim() : undefined)
    twofaSetup.value = null
    twofaPwd.value = ''
    twofaCode.value = ''
    await load2faStatus()
    flash('双因素认证已重置')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    twofaError.value = map2faError(err?.response?.data?.detail)
  } finally {
    twofaLoading.value = false
  }
}

function flash(msg: string): void {
  savedFlag.value = msg
  setTimeout(() => (savedFlag.value = ''), 1800)
}

async function changePassword(): Promise<void> {
  pwdError.value = ''
  if (!pwd.current || !pwd.new) {
    pwdError.value = '请填写当前密码和新密码'
    return
  }
  if (pwd.new.length < 6) {
    pwdError.value = '新密码至少 6 位'
    return
  }
  if (pwd.new !== pwd.confirm) {
    pwdError.value = '两次输入的新密码不一致'
    return
  }
  if (!(await confirmAction('确认修改登录密码？\n\n修改成功后需使用新密码重新登录。', '确认修改密码'))) return
  pwdLoading.value = true
  try {
    await auth.changePassword(pwd.current, pwd.new)
    flash('密码已修改，请重新登录')
    pwd.current = ''
    pwd.new = ''
    pwd.confirm = ''
    setTimeout(() => {
      auth.logout()
      router.push({ name: 'login' })
    }, 1200)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    const detail = err?.response?.data?.detail
    if (detail === 'invalid current password') pwdError.value = '当前密码不正确'
    else if (detail === 'password too short') pwdError.value = '新密码至少 6 位'
    else if (detail === 'password unchanged') pwdError.value = '新密码不能与当前密码相同'
    else pwdError.value = '修改失败，请稍后重试'
  } finally {
    pwdLoading.value = false
  }
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

    <div class="card card-pad">
      <strong>修改密码</strong>
      <p class="muted" style="font-size: 12px">修改当前账号的登录密码；成功后需使用新密码重新登录。</p>
      <div class="form-grid">
        <div>
          <label>当前密码</label>
          <input v-model="pwd.current" type="password" autocomplete="current-password" />
        </div>
        <div>
          <label>新密码</label>
          <input v-model="pwd.new" type="password" autocomplete="new-password" />
        </div>
        <div>
          <label>确认新密码</label>
          <input v-model="pwd.confirm" type="password" autocomplete="new-password" />
        </div>
        <div v-if="pwdError" style="color: var(--red); font-size: 13px">{{ pwdError }}</div>
        <button class="btn-primary" :disabled="pwdLoading" @click="changePassword">
          {{ pwdLoading ? '保存中…' : '修改密码' }}
        </button>
      </div>
    </div>

    <div class="card card-pad">
      <strong>双因素认证 (2FA)</strong>
      <p class="muted" style="font-size: 12px">
        使用 Google Authenticator 等 App 扫码绑定；开启后登录需额外输入 6 位验证码。
      </p>
      <div class="form-grid">
        <div>
          <span class="tag" :class="twofa.enabled ? 'green' : twofa.bound ? 'amber' : 'blue'">
            {{ twofa.enabled ? '已开启' : twofa.bound ? '已绑定未开启' : '未绑定' }}
          </span>
        </div>

        <template v-if="twofaSetup">
          <div style="text-align: center">
            <img :src="twofaSetup.qr_data_uri" alt="2FA QR" width="180" height="180" />
          </div>
          <div>
            <label>手动输入密钥</label>
            <input :value="twofaSetup.secret" readonly style="font-family: ui-monospace, monospace" />
          </div>
          <div>
            <label>验证码（完成绑定）</label>
            <input v-model="twofaCode" inputmode="numeric" maxlength="6" placeholder="000000" />
          </div>
          <button class="btn-primary" :disabled="twofaLoading" @click="confirm2faBind">
            {{ twofaLoading ? '提交中…' : '确认绑定并开启' }}
          </button>
        </template>

        <template v-else-if="!twofa.bound">
          <button class="btn-primary" :disabled="twofaLoading" @click="start2faSetup">
            {{ twofaLoading ? '生成中…' : '开始绑定' }}
          </button>
        </template>

        <template v-else>
          <div v-if="!twofa.enabled">
            <label>验证码（重新开启）</label>
            <input v-model="twofaCode" inputmode="numeric" maxlength="6" placeholder="000000" />
            <button class="btn-primary btn-sm" style="margin-top: 8px" :disabled="twofaLoading" @click="enable2fa">
              开启 2FA
            </button>
          </div>
          <div>
            <label>当前密码</label>
            <input v-model="twofaPwd" type="password" autocomplete="current-password" />
          </div>
          <div v-if="twofa.enabled">
            <label>验证码</label>
            <input v-model="twofaCode" inputmode="numeric" maxlength="6" placeholder="关闭/重置时需填写" />
          </div>
          <div class="row" style="gap: 8px">
            <button
              v-if="twofa.enabled"
              class="btn-ghost btn-sm"
              :disabled="twofaLoading"
              @click="disable2fa"
            >
              关闭 2FA
            </button>
            <button class="btn-ghost btn-sm" :disabled="twofaLoading" @click="reset2fa">重置绑定</button>
            <button
              v-if="twofa.enabled"
              class="btn-ghost btn-sm"
              :disabled="twofaLoading"
              @click="start2faSetup"
            >
              更换密钥
            </button>
          </div>
        </template>

        <div v-if="twofaError" style="color: var(--red); font-size: 13px">{{ twofaError }}</div>
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
