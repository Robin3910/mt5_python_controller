<script setup lang="ts">
// 登录页：账号密码 + 可选 2FA 验证码
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const username = ref('admin')
const password = ref('')
const totpCode = ref('')
const error = ref('')
const loading = ref(false)
const step = ref<'credentials' | '2fa'>('credentials')
const loginToken = ref('')

const auth = useAuthStore()
const router = useRouter()

function mapError(detail?: string): string {
  if (detail === 'invalid credentials') return '账号或密码错误'
  if (detail === 'invalid totp code') return '验证码错误或已过期'
  if (detail === 'invalid or expired login token') return '登录已过期，请重新输入账号密码'
  return detail || '登录失败'
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
    router.push({ name: 'dashboard' })
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    error.value = mapError(err?.response?.data?.detail)
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
    router.push({ name: 'dashboard' })
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    error.value = mapError(err?.response?.data?.detail)
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
        <h1 class="login-title">MT5 云端集控中枢</h1>
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

      <form v-if="step === 'credentials'" class="form-grid" @submit.prevent="submitCredentials">
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

      <div class="login-footer">
        <span class="secure-badge secure-badge-sm">
          <span class="secure-dot"></span>
          TLS 加密传输
        </span>
      </div>
    </div>
  </div>
</template>
