<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from 'vue'
import {
  api,
  subscribe,
  type Snapshot,
  type DetailPayload,
  type ConfigStatus,
  type SmsLoginOutcome,
  type SmsStatus,
} from './api/bridge'
import LoginView from './components/LoginView.vue'
import ConsoleShell from './components/ConsoleShell.vue'

const snapshot = ref<Snapshot | null>(null)
const detail = ref<DetailPayload | null>(null)
const selectedId = ref<string>('')
const lockReason = ref<string>('')
const connectionError = ref<string>('')
/**
 * 动作失败提示（启动 / 停止 / 移除 …）。
 *
 * 单独一个 ref，不能复用 connectionError：SSE 每次推快照都会走 applySnapshot，
 * 而 applySnapshot 会清掉 connectionError —— 动作失败后紧接着的 refresh()
 * 会把刚写进去的错误消息抹掉，用户只看到「点了没反应」。
 */
const actionError = ref<string>('')
const busyLabel = ref<string>('')
const config = ref<ConfigStatus | null>(null)
/** 短信登录是否可用（后端短信服务没开时为 false）。 */
const smsStatus = ref<SmsStatus | null>(null)
/**
 * 2FA 中间态。
 *
 * 只记「来源」不记凭证：
 * - 账号密码链路的中间态（login_token）由桥接层保管，前端再调 /api/login
 *   时桥接层会自动带上它去后端换 token；
 * - 短信链路的中间态由 /api/sms/login 的响应直接带回前端。
 *
 * 两条链路不能混：走错分支会让 2FA 失败，用户只能从头再来。
 */
const twofaSource = ref<'password' | 'sms' | ''>('')
const twofaSmsToken = ref<string>('')

let unsubscribe: (() => void) | null = null

const authenticated = computed(() => snapshot.value?.authenticated === true)
const nodes = computed(() => snapshot.value?.nodes ?? [])
const hero = computed(() => snapshot.value?.hero ?? null)
const smsEnabled = computed(() => smsStatus.value?.enabled === true)

function applySnapshot(next: Snapshot): void {
  snapshot.value = next
  if (next.type === 'locked' || !next.authenticated) {
    lockReason.value = next.reason ?? ''
    detail.value = null
    selectedId.value = ''
    return
  }
  lockReason.value = ''
  connectionError.value = ''

  // 选中的实例被移除时清掉选中态，避免详情面板指向不存在的实例。
  if (selectedId.value && !nodes.value.some((n) => n.id === selectedId.value)) {
    selectedId.value = ''
    detail.value = null
  }
}

async function loadDetail(id: string): Promise<void> {
  if (!id) {
    detail.value = null
    return
  }
  try {
    detail.value = await api.detail(id)
  } catch (exc) {
    detail.value = null
    console.error('[detail]', exc)
  }
}

async function select(id: string): Promise<void> {
  selectedId.value = id
  await loadDetail(id)
}

async function refresh(): Promise<void> {
  try {
    const next = await api.state()
    applySnapshot(next)
  } catch (exc) {
    connectionError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

async function login(username: string, password: string, code: string): Promise<string> {
  const result = await api.login(username, password, code)
  if (result.requires_2fa) {
    // 需要 2FA：桥接层已记下中间态凭证，第二步再调 /api/login 时它会带上。
    //
    // 这里**不保留密码**：密码没必要在前端多待一份，
    // 中间态凭证本来就只在桥接层内存里。
    twofaSource.value = 'password'
    return result.error || '请输入 2FA 验证码'
  }
  twofaSource.value = ''
  await refresh()
  return ''
}

/** 发送短信验证码。 */
async function sendSmsCode(phone: string): Promise<{ expire_in: number; cooldown: number }> {
  return api.smsSend(phone)
}

/**
 * 短信验证码登录。
 *
 * 两步：先校验验证码换票据，再用票据换 token。
 * 开了 2FA 的用户第二步会返回 requires_2fa —— 此时把中间态凭证存下来，
 * 等用户输入 TOTP 后调 /api/sms/2fa。
 */
async function smsLogin(phone: string, code: string): Promise<SmsLoginOutcome> {
  const verified = await api.smsVerify(phone, code)
  if (!verified.ok || verified.next_action !== 'login') {
    // register / pending_approval / rejected —— 后端已经给了要展示的话术，
    // 面板只做登录，一律引导去管理后台处理。
    return {
      requires2fa: false,
      loginToken: '',
      message: verified.message || '验证码校验未通过，请重试',
    }
  }

  const result = await api.smsLogin(phone, verified.ticket)
  if (result.requires_2fa) {
    twofaSource.value = 'sms'
    twofaSmsToken.value = result.login_token ?? ''
    return {
      requires2fa: true,
      loginToken: result.login_token ?? '',
      message: result.error || '请输入 2FA 验证码',
    }
  }
  twofaSource.value = ''
  await refresh()
  return { requires2fa: false, loginToken: '', message: '' }
}

/**
 * 2FA 第二步。
 *
 * 走哪条链路由 twofaSource 决定：
 * - 'sms'     → /api/sms/2fa（短信链路的中间态凭证在前端手里）
 * - 'password'→ /api/login（中间态凭证在桥接层，它会自动带上）
 *
 * 失败时**不清状态**：用户能直接改码重试，不用从头再来一遍。
 */
async function submitTwofa(code: string): Promise<void> {
  if (twofaSource.value === 'sms') {
    const token = twofaSmsToken.value
    if (!token) throw new Error('2FA 会话已过期，请重新用验证码登录')
    await api.smsLogin2fa(token, code)
    twofaSource.value = ''
    twofaSmsToken.value = ''
    await refresh()
    return
  }
  if (twofaSource.value === 'password') {
    // 账号密码链路的中间态在桥接层，这里把 TOTP 码交回去即可。
    await api.login('', '', code)
    twofaSource.value = ''
    await refresh()
    return
  }
  throw new Error('2FA 会话已过期，请重新登录')
}

async function logout(): Promise<void> {
  await api.logout()
  await refresh()
}

/** 执行一个实例动作，成功后刷新状态。 */
async function runAction(action: string, id: string): Promise<void> {
  busyLabel.value = action
  actionError.value = ''
  try {
    await api.action(action, id)
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyLabel.value = ''
    await refresh()
    if (selectedId.value) await loadDetail(selectedId.value)
  }
}

async function runBatch(action: string): Promise<void> {
  busyLabel.value = action
  actionError.value = ''
  try {
    await api.batch(action)
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyLabel.value = ''
    await refresh()
  }
}

onMounted(async () => {
  await refresh()
  try {
    config.value = await api.config()
  } catch (exc) {
    console.error('[config]', exc)
  }
  // 短信登录可用性：后端短信服务没开时不显示验证码入口。
  // 拉失败也按「不可用」处理 —— 入口露出来点了只会报错。
  try {
    smsStatus.value = await api.smsStatus()
  } catch (exc) {
    console.error('[sms-status]', exc)
    smsStatus.value = { enabled: false, reason: '短信服务不可用', code_ttl_seconds: 300, send_interval_seconds: 60 }
  }
  unsubscribe = subscribe(applySnapshot)
})

onUnmounted(() => {
  unsubscribe?.()
})
</script>

<template>
  <LoginView
    v-if="!authenticated"
    :reason="lockReason"
    :error="connectionError"
    :on-login="login"
    :sms-enabled="smsEnabled"
    :on-sms-send="sendSmsCode"
    :on-sms-login="smsLogin"
    :on-twofa="submitTwofa"
    :config="config"
  />

  <ConsoleShell
    v-else
    :hero="hero"
    :nodes="nodes"
    :username="snapshot?.username ?? ''"
    :is-admin="snapshot?.is_admin ?? false"
    :selected-id="selectedId"
    :detail="detail"
    :busy-label="busyLabel"
    :connection-error="connectionError"
    :action-error="actionError"
    :update-badge="snapshot?.update"
    @select="select"
    @action="runAction"
    @batch="runBatch"
    @refresh="refresh"
    @logout="logout"
  />
</template>
