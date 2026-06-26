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
    <div class="card card-pad login-card">
      <div class="h1">登录管理后台</div>

      <form v-if="step === 'credentials'" class="form-grid" @submit.prevent="submitCredentials">
        <div>
          <label>用户名</label>
          <input v-model="username" autocomplete="username" />
        </div>
        <div>
          <label>密码</label>
          <input v-model="password" type="password" autocomplete="current-password" />
        </div>
        <div v-if="error" style="color: var(--red); font-size: 13px">{{ error }}</div>
        <button class="btn-primary" :disabled="loading">{{ loading ? '登录中…' : '登录' }}</button>
      </form>

      <form v-else class="form-grid" @submit.prevent="submit2fa">
        <p class="muted" style="font-size: 13px">已开启双因素认证，请输入验证器 App 中的 6 位验证码。</p>
        <div>
          <label>验证码</label>
          <input
            v-model="totpCode"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="6"
            placeholder="000000"
          />
        </div>
        <div v-if="error" style="color: var(--red); font-size: 13px">{{ error }}</div>
        <div class="row" style="gap: 8px">
          <button class="btn-primary" :disabled="loading">{{ loading ? '验证中…' : '验证并登录' }}</button>
          <button class="btn-ghost" type="button" @click="backToCredentials">返回</button>
        </div>
      </form>
    </div>
  </div>
</template>
