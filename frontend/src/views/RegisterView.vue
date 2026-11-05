<script setup lang="ts">
// 注册页：手机号 → 验证码 → 昵称 / 密码，两屏式
//
// **从登录页跳转过来时可以跳过验证码**：登录页校验通过后若发现该号码未注册，
// 会把「手机号 + 票据」交给本页（见 stores/register.ts），本页直接进第二步。
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useRegisterStore } from '@/stores/register'
import { useAliyunCaptcha } from '@/utils/aliyunCaptcha'

const phone = ref('')
const code = ref('')
const ticket = ref('')
const nickname = ref('')
const username = ref('')
const password = ref('')
// —— KYC 实名材料（姓名 / 邮箱，都可选）——
const realName = ref('')
const email = ref('')

const error = ref('')
const loading = ref(false)
const sending = ref(false)
const sent = ref(false)
const cooldown = ref(0)
/** 已发送验证码并通过校验，进入第二步 */
const verified = ref(false)
/** 审核被驳回（next_action='rejected'）→ 允许继续到第二步重新提交 */
const resubmitMode = ref(false)
/** 驳回原因（提示用） */
const rejectReason = ref('')
/** 注册结果：待审核 / 已直接登录 */
const done = ref(false)
const pendingApproval = ref(false)
/** 短信服务未配置（前端直接隐藏注册入口） */
const unavailable = ref(false)
/** 阿里云验证码配置（自适应：有风险时才弹滑块） */
const captchaPrefix = ref('')
const captchaSceneId = ref('')
/** 加密模式下的短效串（服务端动态生成）；空串 = 非加密模式 */
const captchaEncrypted = ref('')

const auth = useAuthStore()
const registerStore = useRegisterStore()
const router = useRouter()

let cooldownTimer: number | undefined

const captcha = useAliyunCaptcha({
  get prefix() {
    return captchaPrefix.value
  },
  get sceneId() {
    return captchaSceneId.value
  },
  encryptedSceneId: async () => {
    const scene = await auth.fetchCaptchaScene()
    captchaSceneId.value = scene.sceneId
    captchaEncrypted.value = scene.encrypted
    return scene.encrypted
  },
})

onMounted(async () => {
  const status = await auth.fetchSmsStatus()
  if (!status.enabled) {
    unavailable.value = true
    return
  }
  captchaPrefix.value = status.captcha_prefix || ''
  captchaSceneId.value = status.captcha_scene_id || ''

  // 登录页发现「未注册」后交接过来的票据 —— 直接进第二步，不用再收验证码
  const handoff = registerStore.takeHandoff()
  if (handoff) {
    phone.value = handoff.phone
    ticket.value = handoff.ticket
    verified.value = true
  }
})

onUnmounted(() => {
  if (cooldownTimer) window.clearInterval(cooldownTimer)
})

function detailOf(e: unknown): string | undefined {
  const err = e as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail
}

function mapError(detail?: string): string {
  if (detail === '验证码错误或已过期') return '验证码错误或已过期'
  if (detail === '该手机号已注册，请直接登录') return '该手机号已注册，请直接登录'
  if (detail === '该用户名已被占用，请自定义') return '该用户名已被占用，请换一个'
  if (detail === '手机号格式不正确') return '请输入正确的 11 位手机号'
  if (detail === 'invalid or expired ticket') return '验证已过期，请重新获取验证码'
  if (detail === '需要完成人机验证') return '请完成滑块验证'
  if (detail === '人机验证未通过，请重试') return '人机验证未通过，请重试'
  if (detail === '姓名长度不正确') return '姓名长度不正确'
  if (detail === '邮箱格式不正确') return '邮箱格式不正确'
  if (detail === 'not found') return '短信服务未配置'
  return detail || '操作失败，请稍后重试'
}

function startCooldown(seconds: number): void {
  cooldown.value = seconds
  if (cooldownTimer) window.clearInterval(cooldownTimer)
  cooldownTimer = window.setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0 && cooldownTimer) {
      window.clearInterval(cooldownTimer)
      cooldownTimer = undefined
    }
  }, 1000)
}

function validPhone(): boolean {
  return /^1[3-9]\d{9}$/.test(phone.value.trim())
}

/**
 * 发送验证码（自适应人机验证）。
 *
 * 默认直接发；服务端返回 428（需要人机验证）时才弹滑块，通过后带参数重试。
 */
async function sendCode(): Promise<void> {
  error.value = ''
  if (!validPhone()) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  sending.value = true
  try {
    const { cooldown: cd } = await auth.sendSmsCode(phone.value.trim(), 'auth')
    sent.value = true
    startCooldown(cd)
  } catch (e: unknown) {
    const detail = detailOf(e)
    if (detail === '需要完成人机验证') {
      const param = captcha.ready() ? await captcha.run() : ''
      if (!param) {
        error.value = '人机验证未通过，请重试'
        sending.value = false
        return
      }
      try {
        const { cooldown: cd } = await auth.sendSmsCode(phone.value.trim(), 'auth', param)
        sent.value = true
        startCooldown(cd)
      } catch (retryErr: unknown) {
        error.value = mapError(detailOf(retryErr))
      }
      sending.value = false
      return
    }
    error.value = mapError(detail)
  } finally {
    sending.value = false
  }
}

/** 校验验证码 → 拿到票据，进入第二步 */
async function verifyCode(): Promise<void> {
  error.value = ''
  if (!validPhone()) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  if (!code.value.trim()) {
    error.value = '请输入验证码'
    return
  }
  loading.value = true
  try {
    const result = await auth.verifySmsCode(phone.value.trim(), code.value.trim(), 'auth')
    ticket.value = result.ticket
    verified.value = true
    // 校验通过但发现已注册：直接引导去登录，别让用户白填一遍资料
    if (result.next_action === 'login' || result.next_action === 'pending_approval') {
      error.value =
        result.next_action === 'login'
          ? '该手机号已注册，请返回登录'
          : '该手机号已注册，等待管理员审核通过后即可登录'
    }
    // 审核被驳回 → **放行**到第二步，让用户重新提交实名材料。
    // 这与上面两个分支相反：那两个是「已注册，别重复注册」，
    // 这个是「就是来重提的」。
    if (result.next_action === 'rejected') {
      resubmitMode.value = true
      rejectReason.value = result.reject_reason || ''
    }
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    loading.value = false
  }
}

/** 提交注册 */
async function submitRegister(): Promise<void> {
  error.value = ''
  if (!ticket.value) {
    error.value = '请先完成短信验证'
    return
  }
  if (password.value && password.value.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  if (email.value.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim())) {
    error.value = '邮箱格式不正确'
    return
  }
  loading.value = true
  try {
    const result = await auth.registerByPhone({
      phone: phone.value.trim(),
      ticket: ticket.value,
      nickname: nickname.value.trim() || null,
      username: username.value.trim() || null,
      password: password.value || null,
      real_name: realName.value.trim() || null,
      email: email.value.trim() || null,
    })
    done.value = true
    pendingApproval.value = result.pending_approval
    if (!result.pending_approval) {
      // 免审核：后端已签发 token，直接进首页
      router.push({ name: 'home' })
    }
  } catch (e: unknown) {
    error.value = mapError(detailOf(e))
  } finally {
    loading.value = false
  }
}

function backToPhone(): void {
  verified.value = false
  ticket.value = ''
  code.value = ''
  error.value = ''
}
</script>

<template>
  <div class="login-wrap register-wrap">
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
    </div>

    <div class="card card-pad login-card glass-card">
      <div class="login-card-header">
        <div class="login-shield" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
            <path d="M12 8v5" stroke-linecap="round"/>
            <circle cx="12" cy="16" r="0.8" fill="currentColor"/>
          </svg>
        </div>
        <div>
          <div class="h1 login-h1">手机号注册</div>
          <p class="muted login-hint">
            {{ done ? '注册提交完成' : verified ? '完善账号信息' : '用手机号快速注册' }}
          </p>
        </div>
      </div>

      <!-- 短信服务未配置 -->
      <div v-if="unavailable" class="form-grid">
        <div class="alert alert-error">短信服务未配置，暂时无法注册。请联系管理员。</div>
        <RouterLink to="/login" class="btn-primary btn-block">返回登录</RouterLink>
      </div>

      <!-- 注册完成 -->
      <div v-else-if="done" class="form-grid">
        <div v-if="pendingApproval" class="alert alert-ok">
          注册成功！请等待管理员审核，审核通过后即可用手机号登录。
        </div>
        <div v-else class="alert alert-ok">注册成功，正在进入控制台…</div>
        <RouterLink v-if="pendingApproval" to="/login" class="btn-primary btn-block">返回登录</RouterLink>
      </div>
      <!-- 第一步：手机号 + 验证码 -->
      <form v-else-if="!verified" class="form-grid" @submit.prevent="verifyCode">
        <div class="input-group">
          <label>手机号</label>
          <input
            v-model="phone"
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
              v-model="code"
              class="totp-input"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="000000"
            />
            <button
              type="button"
              class="btn-ghost sms-send-btn"
              :disabled="sending || cooldown > 0"
              @click="sendCode"
            >
              {{ cooldown > 0 ? `${cooldown}s 后重发` : sent ? '重新发送' : '获取验证码' }}
            </button>
          </div>
        </div>
        <div v-if="error" class="alert alert-error">{{ error }}</div>
        <button class="btn-primary btn-block" :disabled="loading">
          {{ loading ? '验证中…' : '下一步' }}
        </button>
      </form>

      <!-- 第二步：昵称 / 用户名 / 密码 -->
      <form v-else class="form-grid" @submit.prevent="submitRegister">
        <div class="verified-phone">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
          {{ phone }} 已验证
        </div>
        <div class="input-group">
          <label>昵称（可选）</label>
          <input v-model="nickname" maxlength="64" placeholder="展示名称" />
        </div>
        <div class="input-group">
          <label>用户名（可选）</label>
          <input v-model="username" maxlength="32" placeholder="留空则自动生成" />
        </div>
        <div class="input-group">
          <label>密码（可选）</label>
          <input
            v-model="password"
            type="password"
            autocomplete="new-password"
            placeholder="留空则只能用验证码登录"
          />
        </div>

        <!-- KYC 实名材料（姓名 / 邮箱，都可选） -->
        <div v-if="resubmitMode" class="alert alert-warn">
          上次实名审核未通过：{{ rejectReason || '请重新提交实名材料' }}
        </div>
        <div class="input-group">
          <label>姓名（选填，用于实名审核）</label>
          <input v-model="realName" maxlength="64" placeholder="真实姓名" />
        </div>
        <div class="input-group">
          <label>邮箱（选填）</label>
          <input v-model="email" type="email" autocomplete="email" placeholder="用于接收通知" />
        </div>
        <div class="muted register-hint">
          留空密码时系统会生成随机强密码，该账号只能通过手机号验证码登录。<br />
          填写姓名后需经管理员实名审核通过才能登录。
        </div>
        <div v-if="error" class="alert alert-error">{{ error }}</div>
        <div class="row login-actions">
          <button class="btn-primary" :disabled="loading">{{ loading ? '提交中…' : '完成注册' }}</button>
          <button class="btn-ghost" type="button" @click="backToPhone">返回</button>
        </div>
      </form>

      <div v-if="!done && !unavailable" class="login-alt-links">
        <RouterLink to="/login" class="login-alt-link">已有账号？返回登录</RouterLink>
      </div>

      <div class="login-footer">
        <span class="secure-badge secure-badge-sm">
          <span class="secure-dot"></span>
          TLS 加密传输
        </span>
      </div>
    </div>

    <!-- 阿里云验证码需要的占位元素 -->
    <div id="aliyun-captcha-element" class="aliyun-captcha-holder" aria-hidden="true"></div>
    <button id="aliyun-captcha-button" type="button" class="aliyun-captcha-holder" tabindex="-1" aria-hidden="true"></button>
  </div>
</template>
