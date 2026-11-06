<script setup lang="ts">
import { ref } from 'vue'
import type { ConfigStatus } from '../api/bridge'

const props = defineProps<{
  reason: string
  error: string
  /**
   * 登录由父组件执行。这里用 prop 传函数，而不是 `emit('login')`：
   *
   * Vue 的 `emit()` 返回 undefined，不是 handler 返回的 Promise。
   * `await emit('login', ...)` 拿到的是 undefined，父组件里
   * `api.login()` 抛出的异常会变成「未处理的 Promise 拒绝」，
   * 本组件的 try/catch 根本接不到 —— 点登录就毫无反应。
   *
   * 直接把函数当 prop 传，才能真正 await 到结果与异常。
   */
  onLogin: (username: string, password: string, code: string) => Promise<string>
  config: ConfigStatus | null
}>()

const username = ref('')
const password = ref('')
const code = ref('')
const needs2fa = ref(false)
const status = ref('')
const submitting = ref(false)

async function submit(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  status.value = '正在验证…'
  try {
    const message = await props.onLogin(username.value, password.value, code.value)
    if (message) {
      // 需要 2FA：bridge 返回提示，前端切到验证码态。
      needs2fa.value = true
      status.value = message
    } else {
      status.value = ''
    }
  } catch (exc) {
    needs2fa.value = false
    status.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-root">
    <div class="login-bg-grid" />

    <div class="login-frame">
      <div class="login-brand">
        <div class="login-logo">
          <div class="login-logo-inner">亿</div>
        </div>
        <div>
          <h1 class="login-title">亿点点</h1>
          <p class="login-subtitle">TRADING CONSOLE</p>
        </div>
      </div>

      <div class="login-divider" />

      <div v-if="reason" class="login-error">
        <span class="login-error-icon">!</span>
        <span>{{ reason }}</span>
      </div>

      <div v-if="error" class="login-error">
        <span class="login-error-icon">!</span>
        <span>{{ error }}</span>
      </div>

      <!-- 配置有问题时给出可操作的提示（不显示后端地址本身） -->
      <p v-if="config && !config.ok" class="login-status">
        {{ config.error }}
      </p>

      <form class="login-form" @submit.prevent="submit">
        <div class="login-field">
          <label for="login-user">账号</label>
          <div class="input-wrap">
            <input
              id="login-user"
              v-model="username"
              class="input-field"
              type="text"
              autocomplete="username"
              placeholder="请输入账号"
            />
          </div>
        </div>

        <div class="login-field">
          <label for="login-pass">密码</label>
          <div class="input-wrap">
            <input
              id="login-pass"
              v-model="password"
              class="input-field"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
          </div>
        </div>

        <div v-if="needs2fa" class="login-field">
          <label for="login-code">2FA 验证码</label>
          <div class="input-wrap">
            <input
              id="login-code"
              v-model="code"
              class="input-field input-code"
              type="text"
              inputmode="numeric"
              placeholder="6 位验证码"
            />
          </div>
        </div>

        <button class="btn-sm primary login-submit" type="submit" :disabled="submitting">
          {{ submitting ? '正在验证…' : '登录' }}
        </button>
      </form>

      <p v-if="status && !needs2fa" class="login-status">{{ status }}</p>
    </div>
  </div>
</template>

<style scoped>
.login-logo-inner {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0b0f1a;
  color: var(--gold);
  font-weight: 700;
  font-size: 18px;
}

.login-error-icon {
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--red);
  color: #101826;
  font-weight: 700;
  font-size: 11px;
}

.login-submit {
  width: 100%;
  height: 40px;
  font-size: 14px;
  margin-top: 4px;
}

.login-status {
  margin: 14px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--dim);
}
</style>
