<script setup lang="ts">
// 登录页：账号密码 / 手机验证码（双 Tab），可选 2FA 验证码。
//
// 两种登录方式的 2FA 是**同一套**交互，但来源不同：
// - 账号密码登录 → 走 /api/login/2fa
// - 短信登录     → 走 /api/sms/2fa（独立接口，语义更清晰）
// 用 `twofaSource` 记住来源，前端才知道该调哪个。
import { ref } from 'vue'
import type { ConfigStatus, SmsLoginOutcome } from '../api/bridge'

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
  /** 短信登录是否可用（后端短信服务没开时为 false，此时不显示 Tab）。 */
  smsEnabled: boolean
  /** 发送验证码；返回有效期与重发冷却（秒）。 */
  onSmsSend: (phone: string) => Promise<{ expire_in: number; cooldown: number }>
  /** 验证码登录。 */
  onSmsLogin: (phone: string, code: string) => Promise<SmsLoginOutcome>
  /**
   * 2FA 第二步。
   *
   * 只传 TOTP 码 —— 2FA 中间态凭证由父组件保管（账号密码与短信两条链路
   * 的凭证来源不同，混在本组件里容易走错分支）。
   */
  onTwofa: (code: string) => Promise<void>
  config: ConfigStatus | null
}>()

const username = ref('')
const password = ref('')
const code = ref('')
const needs2fa = ref(false)
const status = ref('')
const submitting = ref(false)

/** 登录方式 Tab：password = 账号密码；sms = 手机验证码 */
const tab = ref<'password' | 'sms'>('password')
/** 短信登录表单 */
const phone = ref('')
const smsCode = ref('')
const smsSent = ref(false)
const smsCooldown = ref(0)
const smsSending = ref(false)

let cooldownTimer: number | undefined

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

/** 倒计时：到 0 才能重新发送。 */
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

/** 发送验证码。号码格式先在前端拦一道，省一次无谓的请求。 */
async function sendSmsCode(): Promise<void> {
  status.value = ''
  const number = phone.value.trim()
  if (!/^1[3-9]\d{9}$/.test(number)) {
    status.value = '请输入正确的 11 位手机号'
    return
  }
  smsSending.value = true
  try {
    const { cooldown } = await props.onSmsSend(number)
    smsSent.value = true
    startCooldown(cooldown)
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    smsSending.value = false
  }
}

/**
 * 验证码登录。
 *
 * 校验通过后由服务端告知去向：
 * - login            → 用票据换 token 登录
 * - register         → 该号码未注册 → 提示去管理后台注册（面板不做注册）
 * - pending_approval → 已注册但待审核 → 提示等待
 */
async function submitSms(): Promise<void> {
  if (submitting.value) return
  status.value = ''
  const number = phone.value.trim()
  const value = smsCode.value.trim()
  if (!/^1[3-9]\d{9}$/.test(number)) {
    status.value = '请输入正确的 11 位手机号'
    return
  }
  if (!value) {
    status.value = '请输入验证码'
    return
  }
  submitting.value = true
  try {
    const outcome = await props.onSmsLogin(number, value)
    if (outcome.requires2fa) {
      needs2fa.value = true
      status.value = outcome.message || '请输入 2FA 验证码'
    } else if (outcome.message) {
      status.value = outcome.message
    }
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    submitting.value = false
  }
}

/** 2FA 第二步。 */
async function submit2fa(): Promise<void> {
  if (submitting.value) return
  const value = code.value.trim()
  if (!value) {
    status.value = '请输入 2FA 验证码'
    return
  }
  submitting.value = true
  status.value = '正在验证…'
  try {
    await props.onTwofa(value)
    status.value = ''
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    submitting.value = false
  }
}

/** 返回上一步（2FA → 凭据）。 */
function back(): void {
  needs2fa.value = false
  code.value = ''
  status.value = ''
}

function switchTab(next: 'password' | 'sms'): void {
  tab.value = next
  status.value = ''
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

      <!-- 登录方式 Tab（短信不可用时只显示账号密码） -->
      <div v-if="!needs2fa && smsEnabled" class="login-tabs">
        <button
          type="button"
          class="login-tab"
          :class="{ active: tab === 'password' }"
          @click="switchTab('password')"
        >
          账号密码
        </button>
        <button
          type="button"
          class="login-tab"
          :class="{ active: tab === 'sms' }"
          @click="switchTab('sms')"
        >
          验证码登录
        </button>
      </div>

      <!-- 2FA 第二步：两种来源共用这一段 -->
      <form v-if="needs2fa" class="login-form" @submit.prevent="submit2fa">
        <div class="login-field">
          <label for="login-code">2FA 验证码</label>
          <div class="input-wrap">
            <input
              id="login-code"
              v-model="code"
              class="input-field input-code"
              type="text"
              inputmode="numeric"
              autocomplete="one-time-code"
              placeholder="6 位验证码"
            />
          </div>
        </div>

        <button class="btn-sm primary login-submit" type="submit" :disabled="submitting">
          {{ submitting ? '正在验证…' : '验证并登录' }}
        </button>
        <button class="btn-back-login" type="button" @click="back">返回</button>
      </form>

      <!-- 账号密码登录 -->
      <form
        v-else-if="tab === 'password'"
        class="login-form"
        @submit.prevent="submit"
      >
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

        <button class="btn-sm primary login-submit" type="submit" :disabled="submitting">
          {{ submitting ? '正在验证…' : '登录' }}
        </button>
      </form>

      <!-- 手机验证码登录 -->
      <form v-else class="login-form" @submit.prevent="submitSms">
        <div class="login-field">
          <label for="login-phone">手机号</label>
          <div class="input-wrap">
            <input
              id="login-phone"
              v-model="phone"
              class="input-field"
              type="text"
              inputmode="numeric"
              autocomplete="tel"
              maxlength="13"
              placeholder="请输入 11 位手机号"
            />
          </div>
        </div>

        <div class="login-field">
          <label for="login-sms-code">验证码</label>
          <div class="input-wrap">
            <input
              id="login-sms-code"
              v-model="smsCode"
              class="input-field input-code"
              type="text"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="6 位验证码"
            />
            <button
              class="btn-sms-code"
              type="button"
              :disabled="smsSending || smsCooldown > 0"
              @click="sendSmsCode"
            >
              {{ smsCooldown > 0 ? `${smsCooldown}s` : smsSent ? '重新发送' : '获取验证码' }}
            </button>
          </div>
        </div>

        <button class="btn-sm primary login-submit" type="submit" :disabled="submitting">
          {{ submitting ? '正在验证…' : '登录' }}
        </button>

        <p class="login-hint">未注册？请联系管理员在管理后台开通账号</p>
      </form>

      <!-- 状态提示：成功时为空串，失败 / 2FA 提示时显示 -->
      <p v-if="status" class="login-status">{{ status }}</p>
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

/* ── 登录方式 Tab ── */
.login-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  margin-bottom: 18px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 7px;
}

.login-tab {
  flex: 1;
  padding: 7px 10px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--mute);
  font-size: 12.5px;
  cursor: pointer;
  transition: color 0.15s ease, background 0.15s ease;
}

.login-tab:hover {
  color: var(--text);
}

.login-tab.active {
  background: var(--panel);
  color: var(--text);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
}
</style>
