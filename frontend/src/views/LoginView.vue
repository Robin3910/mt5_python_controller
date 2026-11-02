<script setup lang="ts">
// 登录页：账号密码 + 验证码登录（双 Tab），可选 2FA 验证码
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const username = ref('admin')
const password = ref('')
const totpCode = ref('')
const error = ref('')
const loading = ref(false)
const step = ref<'credentials' | '2fa'>('credentials')
const loginToken = ref('')

/** 登录方式 Tab：password = 账号密码；sms = 手机验证码 */
const tab = ref<'password' | 'sms'>('password')
/** 短信服务是否可用（未配置时隐藏验证码入口） */
const smsEnabled = ref(false)
/** 短信登录表单 */
const smsPhone = ref('')
const smsCode = ref('')
const smsSent = ref(false)
const smsCooldown = ref(0)
const smsLoading = ref(false)

const auth = useAuthStore()
const router = useRouter()

let cooldownTimer: number | undefined

onMounted(async () => {
  const status = await auth.fetchSmsStatus()
  smsEnabled.value = status.enabled
})

function mapError(detail?: string): string {
  if (detail === 'invalid credentials') return '账号或密码错误'
  if (detail === 'invalid totp code') return '验证码错误或已过期'
  if (detail === 'invalid or expired login token') return '登录已过期，请重新输入账号密码'
  if (detail === '验证码错误或已过期') return '验证码错误或已过期'
  if (detail === '该手机号尚未注册') return '该手机号尚未注册，请先注册'
  if (detail === '账号已被禁用，请联系管理员') return '账号已被禁用，请联系管理员'
  if (detail === 'not found') return '短信服务未配置，请使用账号密码登录'
  return detail || '登录失败'
}

function detailOf(e: unknown): string | undefined {
  const err = e as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail
}

/** 60s 重发倒计时 */
function startCooldown(seconds: number): void {
  smsCooldown.value = seconds
  if (cooldownTimer) window.clearInterval(cooldownTimer)
  cooldownTimer = window.setInterval(() => {
    smsCooldown.value -= 1
    if (smsCooldown.value <= 0 && cooldownTimer) {
      window.clearInterval(cooldownTimer)
      cooldownTimer = undefined
    }
  }, 1000)
}

async function submitCredentials(): Promise<void> {
  error.value = ''
  loading.value = true
  try {
    const result = await auth.login(username.value, password.value)
    if (result.requires2fa) {
      loginToken.value = result.loginToken || ''
      step.value = '2fa'
      totpCode.value = ''
      return
    }
    router.push({ name: 'home' })
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    loading.value = false
  }
}

async function submit2fa(): Promise<void> {
  error.value = ''
  if (!totpCode.value.trim()) {
    error.value = '请输入 6 位验证码'
    return
  }
  loading.value = true
  try {
    await auth.login2fa(loginToken.value, totpCode.value.trim())
    router.push({ name: 'home' })
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    loading.value = false
  }
}

function backToCredentials(): void {
  step.value = 'credentials'
  loginToken.value = ''
  totpCode.value = ''
  error.value = ''
}

/** 发送短信验证码 */
async function sendSmsCode(): Promise<void> {
  error.value = ''
  const phone = smsPhone.value.trim()
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  smsLoading.value = true
  try {
    const { cooldown } = await auth.sendSmsCode(phone, 'login')
    smsSent.value = true
    startCooldown(cooldown)
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    smsLoading.value = false
  }
}

/** 验证码登录（2FA 用户走现有的两步流程） */
async function submitSmsLogin(): Promise<void> {
  error.value = ''
  const phone = smsPhone.value.trim()
  const code = smsCode.value.trim()
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  if (!code) {
    error.value = '请输入验证码'
    return
  }
  loading.value = true
  try {
    const result = await auth.loginBySms(phone, code)
    if (result.requires2fa) {
      loginToken.value = result.loginToken || ''
      step.value = '2fa'
      totpCode.value = ''
      return
    }
    router.push({ name: 'home' })
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    loading.value = false
  }
}

function switchTab(next: 'password' | 'sms'): void {
  tab.value = next
  error.value = ''
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-hero">
      <div class="login-brand">
        <span class="brand-icon brand-icon-lg" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke-linejoin="round" />
          </svg>
        </span>
        <h1 class="login-title">亿点点</h1>
        <p class="login-subtitle">企业级交易节点管控平台</p>
      </div>

      <div class="security-features">
        <div class="security-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          <span>端到端加密</span>
        </div>
        <div class="security-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          <span>双因素认证</span>
        </div>
        <div class="security-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          <span>实时审计</span>
        </div>
      </div>
    </div>

    <div class="card card-pad login-card glass-card">
      <div class="login-card-header">
        <div class="login-shield" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
            <path d="M9 12l2 2 4-4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div>
          <div class="h1 login-h1">{{ step === 'credentials' ? '安全登录' : '身份验证' }}</div>
          <p class="muted login-hint">
            {{ step === 'credentials' ? '请输入管理员凭据以访问控制台' : '请输入验证器 App 中的 6 位验证码' }}
          </p>
        </div>
      </div>

      <!-- 登录方式 Tab（短信未配置时只显示账号密码） -->
      <div v-if="step === 'credentials' && smsEnabled" class="login-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          class="login-tab"
          :class="{ active: tab === 'password' }"
          :aria-selected="tab === 'password'"
          @click="switchTab('password')"
        >
          账号密码登录
        </button>
        <button
          type="button"
          role="tab"
          class="login-tab"
          :class="{ active: tab === 'sms' }"
          :aria-selected="tab === 'sms'"
          @click="switchTab('sms')"
        >
          验证码登录
        </button>
      </div>

      <form v-if="step === 'credentials' && tab === 'password'" class="form-grid" @submit.prevent="submitCredentials">
        <div class="input-group">
          <label>用户名</label>
          <input v-model="username" autocomplete="username" placeholder="admin" />
        </div>
        <div class="input-group">
          <label>密码</label>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="••••••••" />
        </div>
        <div v-if="error" class="alert alert-error">{{ error }}</div>
        <button class="btn-primary btn-block" :disabled="loading">
          <svg v-if="!loading" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          {{ loading ? '登录中…' : '安全登录' }}
        </button>
      </form>

      <form v-else-if="step === 'credentials' && tab === 'sms'" class="form-grid" @submit.prevent="submitSmsLogin">
        <div class="input-group">
          <label>手机号</label>
          <input
            v-model="smsPhone"
            inputmode="numeric"
            autocomplete="tel"
            maxlength="11"
            placeholder="请输入 11 位手机号"
          />
        </div>
        <div class="input-group">
          <label>验证码</label>
          <div class="sms-code-row">
            <input
              v-model="smsCode"
              class="totp-input"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="000000"
            />
            <button
              type="button"
              class="btn-ghost sms-send-btn"
              :disabled="smsLoading || smsCooldown > 0"
              @click="sendSmsCode"
            >
              {{ smsCooldown > 0 ? `${smsCooldown}s 后重发` : smsSent ? '重新发送' : '获取验证码' }}
            </button>
          </div>
        </div>
        <div v-if="error" class="alert alert-error">{{ error }}</div>
        <button class="btn-primary btn-block" :disabled="loading">
          {{ loading ? '登录中…' : '验证码登录' }}
        </button>
      </form>

      <form v-else class="form-grid" @submit.prevent="submit2fa">
        <div class="twofa-badge">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          2FA 已启用 — 额外安全层保护
        </div>
        <div class="input-group">
          <label>验证码</label>
          <input
            v-model="totpCode"
            class="totp-input"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="6"
            placeholder="000000"
          />
        </div>
        <div v-if="error" class="alert alert-error">{{ error }}</div>
        <div class="row login-actions">
          <button class="btn-primary" :disabled="loading">{{ loading ? '验证中…' : '验证并登录' }}</button>
          <button class="btn-ghost" type="button" @click="backToCredentials">返回</button>
        </div>
      </form>

      <div v-if="step === 'credentials'" class="login-alt-links">
        <RouterLink v-if="smsEnabled" to="/register" class="login-alt-link">还没有账号？手机号注册</RouterLink>
        <RouterLink v-else-if="!smsEnabled" to="/register" class="login-alt-link">注册账号</RouterLink>
      </div>

      <div class="login-footer">
        <span class="secure-badge secure-badge-sm">
          <span class="secure-dot"></span>
          TLS 加密传输
        </span>
      </div>
    </div>
  </div>
</template>
