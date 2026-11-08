<script setup lang="ts">
// 自助改绑手机号弹窗：三步向导
//
//   ① 验证新手机号   —— 新号收验证码，证明「新号是你的」
//   ② 验证本人身份   —— 二选一：登录密码（+2FA） / 原手机号验证码
//   ③ 完成
//
// 为什么第 ② 步不能省：手机号是验证码登录的唯一凭据，只验新号等于「谁抢到
// 号码就接管账号」。旧号验证（路径 B）是给「手机号注册且没设密码」的用户
// 准备的 —— 那批用户不知道自己的密码，只有旧号能证明身份。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useAuthStore } from '@/stores/auth'
import { useAliyunCaptcha } from '@/utils/aliyunCaptcha'
import type { PhoneRebindAuthMethod } from '@/api/types'

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'success', phone: string): void
}>()

const auth = useAuthStore()

// ── 步骤 ────────────────────────────────────────────────────────
type Step = 'new' | 'identity' | 'done'
const step = ref<Step>('new')

const error = ref('')
const busy = ref(false)

// ── 第 ① 步：验证新号 ───────────────────────────────────────────
const newPhone = ref('')
const newCode = ref('')
const newTicket = ref('')
const sendingNew = ref(false)
const newCooldown = ref(0)

// ── 第 ② 步：验证本人身份 ───────────────────────────────────────
const authMethod = ref<PhoneRebindAuthMethod>('password')
const password = ref('')
const totpCode = ref('')
const oldCode = ref('')
const sendingOld = ref(false)
const oldCooldown = ref(0)

/** 账号是否开了 2FA（开了就得多要一个 TOTP） */
const twofaEnabled = ref(false)
/** 短信服务不可用（前端直接禁用发码入口） */
const smsUnavailable = ref(false)

const captchaPrefix = ref('')
const captchaSceneId = ref('')
const captchaEncrypted = ref('')

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

let cooldownTimer: number | undefined

/** 账号当前是否已绑定手机号 —— 没绑定就跳过第 ② 步 */
const hasBoundPhone = computed(() => !!auth.me?.phone)

onMounted(async () => {
  const status = await auth.fetchSmsStatus()
  smsUnavailable.value = !status.enabled
  captchaPrefix.value = status.captcha_prefix || ''
  captchaSceneId.value = status.captcha_scene_id || ''
  try {
    const t = await auth.fetch2faStatus()
    twofaEnabled.value = t.enabled
  } catch {
    twofaEnabled.value = false
  }
})

onUnmounted(() => {
  if (cooldownTimer) window.clearInterval(cooldownTimer)
})

// 每次打开都重置状态（上次失败残留不该带到这次）
watch(
  () => props.open,
  (v) => {
    if (v) reset()
  },
)

function reset(): void {
  step.value = 'new'
  error.value = ''
  busy.value = false
  newPhone.value = ''
  newCode.value = ''
  newTicket.value = ''
  sendingNew.value = false
  newCooldown.value = 0
  authMethod.value = 'password'
  password.value = ''
  totpCode.value = ''
  oldCode.value = ''
  sendingOld.value = false
  oldCooldown.value = 0
}

function detailOf(e: unknown): string | undefined {
  const err = e as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail
}

function mapError(detail?: string): string {
  if (detail === '验证码错误或已过期') return '验证码错误或已过期'
  if (detail === '手机号格式不正确') return '请输入正确的 11 位手机号'
  if (detail === 'invalid or expired ticket') return '验证已过期，请重新验证新手机号'
  if (detail === '密码不正确') return '密码不正确'
  if (detail === '该手机号已被其他账号绑定') return '该手机号已被其他账号绑定'
  if (detail === '账号未绑定手机号') return '账号未绑定手机号'
  if (detail === '需要完成人机验证') return '需要完成滑块验证'
  if (detail === '人机验证未通过，请重试') return '人机验证未通过，请重试'
  if (detail === 'not found') return '短信服务未配置，请联系管理员'
  return detail || '操作失败，请稍后重试'
}

function startCooldown(target: 'new' | 'old', seconds: number): void {
  if (target === 'new') {
    newCooldown.value = seconds
  } else {
    oldCooldown.value = seconds
  }
  if (cooldownTimer) window.clearInterval(cooldownTimer)
  cooldownTimer = window.setInterval(() => {
    newCooldown.value = Math.max(0, newCooldown.value - 1)
    oldCooldown.value = Math.max(0, oldCooldown.value - 1)
    if (newCooldown.value <= 0 && oldCooldown.value <= 0 && cooldownTimer) {
      window.clearInterval(cooldownTimer)
      cooldownTimer = undefined
    }
  }, 1000)
}

function validPhone(p: string): boolean {
  return /^1[3-9]\d{9}$/.test(p.trim())
}

/** 发码（自适应人机验证：服务端命中风控时返回 428，弹滑块后带参重试） */
async function sendCode(target: 'new' | 'old'): Promise<void> {
  error.value = ''
  if (target === 'new' && !validPhone(newPhone.value)) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  const sending = target === 'new' ? sendingNew : sendingOld
  sending.value = true
  try {
    const { cooldown } = await auth.sendRebindCode(
      target,
      target === 'new' ? newPhone.value.trim() : '',
    )
    startCooldown(target, cooldown)
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
        const { cooldown } = await auth.sendRebindCode(
          target,
          target === 'new' ? newPhone.value.trim() : '',
          param,
        )
        startCooldown(target, cooldown)
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

/** 校验新号验证码 → 换票据 → 进入第 ② 步 */
async function verifyNew(): Promise<void> {
  error.value = ''
  if (!validPhone(newPhone.value)) {
    error.value = '请输入正确的 11 位手机号'
    return
  }
  if (!newCode.value.trim()) {
    error.value = '请输入验证码'
    return
  }
  busy.value = true
  try {
    const r = await auth.verifyRebindNewPhone(newPhone.value.trim(), newCode.value.trim())
    newTicket.value = r.ticket
    // 账号本来就没绑定号码 → 没有旧凭据可被抢占，直接完成改绑
    if (!hasBoundPhone.value) {
      await submitRebind()
      return
    }
    step.value = 'identity'
  } catch (e: unknown) {
    const detail = detailOf(e)
    // 票据失效（10 分钟过期 / 票据对不上）→ 回到第 ① 步重新验证新号，
    // 否则用户会卡在第 ② 步反复提交一个用不了的票据。
    if (detail === 'invalid or expired ticket') {
      newTicket.value = ''
      step.value = 'new'
      error.value = '验证已过期，请重新验证新手机号'
    } else {
      error.value = mapError(detail)
    }
  } finally {
    busy.value = false
  }
}
async function submitRebind(): Promise<void> {
  error.value = ''
  if (hasBoundPhone.value) {
    if (authMethod.value === 'password') {
      if (!password.value) {
        error.value = '请输入当前密码'
        return
      }
      if (twofaEnabled.value && !totpCode.value.trim()) {
        error.value = '请输入 2FA 验证码'
        return
      }
    } else if (!oldCode.value.trim()) {
      error.value = '请输入原手机号收到的验证码'
      return
    }
  }
  busy.value = true
  try {
    const r = await auth.rebindPhone({
      new_phone: newPhone.value.trim(),
      ticket: newTicket.value,
      auth_method: authMethod.value,
      password: authMethod.value === 'password' ? password.value : undefined,
      totp_code: authMethod.value === 'password' && twofaEnabled.value ? totpCode.value.trim() : undefined,
      old_code: authMethod.value === 'old_code' ? oldCode.value.trim() : undefined,
    })
    step.value = 'done'
    ElMessage.success('手机号已修改')
    emit('success', r.phone)
  } catch (e: unknown) {
    const detail = detailOf(e)
    // 票据失效 → 回到第 ① 步重新验证新号（否则用户会卡在第 ② 步
    // 反复提交一个用不了的票据，只能关窗重来）。
    if (detail === 'invalid or expired ticket') {
      newTicket.value = ''
      step.value = 'new'
      error.value = '验证已过期，请重新验证新手机号'
    } else {
      error.value = mapError(detail)
    }
  } finally {
    busy.value = false
  }
}

function close(): void {
  emit('close')
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="close">
    <div class="card card-pad rb-modal">
      <!-- 标题 -->
      <div class="rb-head">
        <span class="rb-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="5" y="2" width="14" height="20" rx="2" />
            <path d="M11 18h2" />
          </svg>
        </span>
        <div>
          <div class="rb-title">修改绑定手机号</div>
          <div class="rb-sub">
            当前号码：{{ auth.me?.phone || '未绑定' }}
          </div>
        </div>
      </div>

      <!-- 步骤指示 -->
      <div class="rb-steps">
        <span class="rb-step" :class="{ on: step === 'new', done: step !== 'new' }">1 验证新号</span>
        <span class="rb-step" :class="{ on: step === 'identity', done: step === 'done' }">2 验证身份</span>
        <span class="rb-step" :class="{ on: step === 'done' }">3 完成</span>
      </div>

      <!-- 短信服务不可用 -->
      <div v-if="smsUnavailable" class="rb-block">
        <p class="pf-error" style="margin: 0">
          短信服务未配置，暂时无法自助修改手机号。请联系管理员在「用户权限」页修改。
        </p>
      </div>

      <!-- ① 验证新号 -->
      <div v-else-if="step === 'new'" class="rb-block">
        <div>
          <label class="pf-label" for="rb-new-phone">新手机号</label>
          <input
            id="rb-new-phone"
            v-model="newPhone"
            class="pf-input"
            inputmode="numeric"
            autocomplete="tel"
            maxlength="11"
            placeholder="请输入 11 位手机号"
          />
        </div>
        <div>
          <label class="pf-label" for="rb-new-code">验证码</label>
          <div class="rb-code-row">
            <input
              id="rb-new-code"
              v-model="newCode"
              class="pf-input rb-code-input"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="000000"
            />
            <button
              type="button"
              class="pf-btn"
              :disabled="sendingNew || newCooldown > 0"
              @click="sendCode('new')"
            >
              {{ newCooldown > 0 ? `${newCooldown}s 后重发` : '获取验证码' }}
            </button>
          </div>
        </div>
        <p class="pf-hint">验证码会发送到新手机号，用于确认该号码属于你。</p>
      </div>

      <!-- ② 验证本人身份 -->
      <div v-else-if="step === 'identity'" class="rb-block">
        <p class="rb-verified">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5" /></svg>
          新号码 {{ newPhone }} 已验证
        </p>

        <div class="rb-tabs">
          <button
            type="button"
            class="rb-tab"
            :class="{ on: authMethod === 'password' }"
            @click="authMethod = 'password'"
          >
            用登录密码
          </button>
          <button
            type="button"
            class="rb-tab"
            :class="{ on: authMethod === 'old_code' }"
            @click="authMethod = 'old_code'"
          >
            用原手机号验证码
          </button>
        </div>

        <template v-if="authMethod === 'password'">
          <div>
            <label class="pf-label" for="rb-pwd">当前密码</label>
            <input
              id="rb-pwd"
              v-model="password"
              type="password"
              class="pf-input"
              autocomplete="current-password"
              placeholder="请输入登录密码"
            />
          </div>
          <div v-if="twofaEnabled">
            <label class="pf-label" for="rb-totp">双因素验证码</label>
            <input
              id="rb-totp"
              v-model="totpCode"
              class="pf-input"
              inputmode="numeric"
              maxlength="6"
              placeholder="验证器 App 上的 6 位验证码"
            />
          </div>
        </template>

        <template v-else>
          <div>
            <label class="pf-label" for="rb-old-code">原手机号验证码</label>
            <div class="rb-code-row">
              <input
                id="rb-old-code"
                v-model="oldCode"
                class="pf-input rb-code-input"
                inputmode="numeric"
                autocomplete="one-time-code"
                maxlength="6"
                placeholder="000000"
              />
              <button
                type="button"
                class="pf-btn"
                :disabled="sendingOld || oldCooldown > 0"
                @click="sendCode('old')"
              >
                {{ oldCooldown > 0 ? `${oldCooldown}s 后重发` : '获取验证码' }}
              </button>
            </div>
          </div>
          <p class="pf-hint">验证码已发送到你当前绑定的手机号，用于确认是你本人在操作。</p>
        </template>
      </div>

      <!-- ③ 完成 -->
      <div v-else class="rb-block">
        <p class="rb-done">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5" /></svg>
          手机号已修改为 {{ auth.me?.phone }}
        </p>
        <p class="pf-hint">下次登录请使用新手机号接收验证码。</p>
      </div>

      <p v-if="error" class="pf-error">{{ error }}</p>

      <div class="modal-footer rb-actions">
        <button type="button" class="btn-ghost" :disabled="busy" @click="close">
          {{ step === 'done' ? '关闭' : '取消' }}
        </button>
        <button
          v-if="step === 'new'"
          type="button"
          class="btn-primary"
          :disabled="busy || smsUnavailable"
          @click="verifyNew"
        >
          {{ busy ? '验证中…' : '下一步' }}
        </button>
        <button
          v-else-if="step === 'identity'"
          type="button"
          class="btn-primary"
          :disabled="busy"
          @click="submitRebind"
        >
          {{ busy ? '提交中…' : '确认修改' }}
        </button>
      </div>
    </div>

    <!-- 阿里云验证码需要的占位元素 -->
    <div id="aliyun-captcha-element" class="aliyun-captcha-holder" aria-hidden="true"></div>
    <button id="aliyun-captcha-button" type="button" class="aliyun-captcha-holder" tabindex="-1" aria-hidden="true"></button>
  </div>
</template>

<style scoped>
.rb-modal {
  width: 100%;
  max-width: 420px;
  border-color: #2a3b5c;
  background: #0f172a;
}

/* ── 头部 ── */
.rb-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.rb-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: rgba(37, 99, 235, 0.15);
  color: #3b82f6;
}
.rb-icon svg { width: 18px; height: 18px; }
.rb-title { font-size: 14px; font-weight: 700; color: #e6ebf4; }
.rb-sub {
  margin-top: 2px;
  font-size: 10px;
  color: #8b96a8;
}

/* ── 步骤条 ── */
.rb-steps {
  display: flex;
  gap: 6px;
  margin-top: 12px;
}
.rb-step {
  flex: 1;
  text-align: center;
  padding: 5px 4px;
  border-radius: 6px;
  border: 1px solid #24304a;
  background: #0c1220;
  color: #5b6b85;
  font-size: 10px;
  white-space: nowrap;
}
.rb-step.on {
  border-color: rgba(37, 99, 235, 0.55);
  background: rgba(37, 99, 235, 0.12);
  color: #60a5fa;
}
.rb-step.done {
  border-color: rgba(34, 197, 94, 0.35);
  color: #22c55e;
}

/* ── 主体 ── */
.rb-block {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.rb-code-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.rb-code-input { flex: 1; min-width: 0; }

.rb-verified,
.rb-done {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  color: #22c55e;
}
.rb-verified svg,
.rb-done svg { width: 14px; height: 14px; flex-shrink: 0; }

/* ── 身份验证方式切换 ── */
.rb-tabs {
  display: flex;
  gap: 6px;
}
.rb-tab {
  flex: 1;
  padding: 7px 8px;
  border-radius: 8px;
  border: 1px solid #24304a;
  background: #0c1220;
  color: #8b96a8;
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}
.rb-tab:hover { border-color: #2b3a5c; color: #b8c4d9; }
.rb-tab.on {
  border-color: rgba(37, 99, 235, 0.55);
  background: rgba(37, 99, 235, 0.12);
  color: #60a5fa;
}

/* ── 底部按钮 ── */
.rb-actions { justify-content: flex-end; }
</style>
