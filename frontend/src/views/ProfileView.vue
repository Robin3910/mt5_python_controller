<script setup lang="ts">
// 个人中心：账号头部 + 个人信息 + 带单员 + 修改密码 + 双因素认证
// 样式 1:1 复刻 UI/H5.html 原型的「个人中心」页（组件 l1）。
//
// 「修改密码」与「2FA」从 ConfigView 搬到这里 —— 它们是**个人**设置，
// 而 ConfigView 现在只留管理员的系统配置（节点令牌 / Webhook / 短信）。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { confirmAction } from '@/utils/confirm'

const auth = useAuthStore()
const router = useRouter()

const userLabel = computed(() => auth.me?.display_name || auth.me?.username || '')
const roleNames = computed(() => (auth.me?.roles || []).map((r) => r.name).join('、'))
/** 带单员展示名：没单独设过就回退到姓名 —— 带单广场也是这个口径 */
const leaderLabel = computed(() => auth.me?.leader_name || auth.me?.display_name || auth.me?.username || '')
/** 带单员介绍区块只对带单相关的人显示（普通用户看到的是无意义的空表单） */
const canBeLeader = computed(
  () => auth.isAdmin || auth.canBeFollowLeader || auth.canUseAiStrategy,
)

const savedFlag = ref('')
const profileError = ref('')

// ── 个人信息编辑 ────────────────────────────────────────────────
const editingProfile = ref(false)
const profileSaving = ref(false)
const profileForm = reactive({ display_name: '', email: '' })

function openProfileEdit(): void {
  profileForm.display_name = auth.me?.display_name || ''
  profileForm.email = auth.me?.email || ''
  profileError.value = ''
  editingProfile.value = true
}

function cancelProfileEdit(): void {
  editingProfile.value = false
  profileError.value = ''
}

async function saveProfile(): Promise<void> {
  profileError.value = ''
  const name = profileForm.display_name.trim()
  const email = profileForm.email.trim()
  if (!name) {
    profileError.value = '姓名不能为空'
    return
  }
  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    profileError.value = '邮箱格式不正确'
    return
  }
  profileSaving.value = true
  try {
    await auth.updateProfile({ display_name: name, email })
    editingProfile.value = false
    flash('个人信息已保存')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    profileError.value = err?.response?.data?.detail || '保存失败，请稍后重试'
  } finally {
    profileSaving.value = false
  }
}

// ── 带单员资料编辑 ──────────────────────────────────────────────
const editingLeader = ref(false)
const leaderSaving = ref(false)
const leaderError = ref('')
const leaderForm = reactive({ leader_name: '', leader_intro: '' })

function openLeaderEdit(): void {
  leaderForm.leader_name = auth.me?.leader_name || ''
  leaderForm.leader_intro = auth.me?.leader_intro || ''
  leaderError.value = ''
  editingLeader.value = true
}

function cancelLeaderEdit(): void {
  editingLeader.value = false
  leaderError.value = ''
}

async function saveLeader(): Promise<void> {
  leaderError.value = ''
  const name = leaderForm.leader_name.trim()
  const intro = leaderForm.leader_intro.trim()
  if (!name) {
    leaderError.value = '带单员名称不能为空'
    return
  }
  if (!intro) {
    leaderError.value = '带单员介绍不能为空'
    return
  }
  leaderSaving.value = true
  try {
    await auth.updateProfile({ leader_name: name, leader_intro: intro })
    editingLeader.value = false
    flash('带单员信息已保存')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    leaderError.value = err?.response?.data?.detail || '保存失败，请稍后重试'
  } finally {
    leaderSaving.value = false
  }
}

// ── 修改密码 ────────────────────────────────────────────────────
const pwdOpen = ref(false)
const pwdError = ref('')
const pwdLoading = ref(false)
const pwd = reactive({ current: '', new: '', confirm: '' })

function togglePwd(): void {
  pwdOpen.value = !pwdOpen.value
  if (!pwdOpen.value) {
    pwd.current = ''
    pwd.new = ''
    pwd.confirm = ''
    pwdError.value = ''
  }
}

async function changePassword(): Promise<void> {
  pwdError.value = ''
  if (!pwd.current || !pwd.new) {
    pwdError.value = '请填写原密码和新密码'
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
    if (detail === 'invalid current password') pwdError.value = '原密码不正确'
    else if (detail === 'password too short') pwdError.value = '新密码至少 6 位'
    else if (detail === 'password unchanged') pwdError.value = '新密码不能与原密码相同'
    else pwdError.value = '修改失败，请稍后重试'
  } finally {
    pwdLoading.value = false
  }
}

// ── 双因素认证 (2FA) ────────────────────────────────────────────
const twofa = reactive({ enabled: false, bound: false, pending_setup: false })
const twofaSetup = ref<{ secret: string; qr_data_uri: string } | null>(null)
const twofaCode = ref('')
const twofaPwd = ref('')
const twofaError = ref('')
const twofaLoading = ref(false)

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

onMounted(load2faStatus)
</script>

<template>
  <div class="profile-page">
    <!-- 账号头部 -->
    <section class="pf-card profile-head">
      <span class="profile-avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1" />
        </svg>
      </span>
      <div class="profile-head-info">
        <div class="profile-head-row">
          <p class="profile-head-name">{{ userLabel }}</p>
          <span class="profile-online">
            <span class="profile-online-dot"></span>
            在线
          </span>
        </div>
        <p class="profile-head-sub">
          <template v-if="auth.me?.email">{{ auth.me.email }} · </template>
          <template v-if="roleNames">{{ roleNames }}</template>
          <template v-if="!auth.me?.email && !roleNames">@{{ auth.me?.username }}</template>
        </p>
      </div>
    </section>

    <!-- 个人信息 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>个人信息</p>
        <button v-if="!editingProfile" type="button" class="pf-btn" @click="openProfileEdit">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
          修改资料
        </button>
      </div>

      <template v-if="editingProfile">
        <div class="pf-grid-3">
          <div>
            <label class="pf-label" for="pf-name">姓名</label>
            <input id="pf-name" v-model="profileForm.display_name" class="pf-input" placeholder="请输入姓名" />
          </div>
          <div>
            <label class="pf-label" for="pf-phone">电话</label>
            <input
              id="pf-phone"
              class="pf-input"
              :value="auth.me?.phone || '未绑定'"
              readonly
              title="自助改绑需短信验证，暂未开放"
            />
            <p class="pf-hint">自助改绑需短信验证，暂未开放</p>
          </div>
          <div>
            <label class="pf-label" for="pf-email">邮箱</label>
            <input id="pf-email" v-model="profileForm.email" class="pf-input" placeholder="请输入邮箱" inputmode="email" />
          </div>
        </div>
        <p v-if="profileError" class="pf-error">{{ profileError }}</p>
        <div class="pf-form-actions">
          <button type="button" class="pf-btn" @click="cancelProfileEdit">取消</button>
          <button type="button" class="pf-btn pf-btn-primary" :disabled="profileSaving" @click="saveProfile">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5" />
            </svg>
            保存
          </button>
        </div>
      </template>

      <ul v-else class="pf-list">
        <li>
          <span class="pf-list-k">姓名</span>
          <span class="pf-list-v strong">{{ userLabel }}</span>
        </li>
        <li>
          <span class="pf-list-k">电话</span>
          <span class="pf-list-v">{{ auth.me?.phone || '未绑定' }}</span>
        </li>
        <li>
          <span class="pf-list-k">邮箱</span>
          <span class="pf-list-v">{{ auth.me?.email || '—' }}</span>
        </li>
      </ul>
    </section>

    <!-- 带单员 -->
    <section v-if="canBeLeader" class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>带单员</p>
        <button v-if="!editingLeader" type="button" class="pf-btn" @click="openLeaderEdit">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
          编辑
        </button>
      </div>

      <template v-if="editingLeader">
        <div class="profile-form">
          <div>
            <label class="pf-label" for="pf-leader-name">带单员名称</label>
            <input id="pf-leader-name" v-model="leaderForm.leader_name" class="pf-input" placeholder="请输入带单员名称" />
          </div>
          <div>
            <label class="pf-label" for="pf-leader-intro">带单员介绍</label>
            <textarea
              id="pf-leader-intro"
              v-model="leaderForm.leader_intro"
              class="pf-input"
              rows="4"
              placeholder="请输入带单员介绍"
            ></textarea>
          </div>
        </div>
        <p v-if="leaderError" class="pf-error">{{ leaderError }}</p>
        <div class="pf-form-actions">
          <button type="button" class="pf-btn" @click="cancelLeaderEdit">取消</button>
          <button type="button" class="pf-btn pf-btn-primary" :disabled="leaderSaving" @click="saveLeader">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5" />
            </svg>
            保存
          </button>
        </div>
      </template>

      <ul v-else class="pf-list">
        <li>
          <span class="pf-list-k">带单员名称</span>
          <span class="pf-list-v strong">{{ leaderLabel }}</span>
        </li>
        <li>
          <span class="pf-list-k">带单员介绍</span>
          <span class="pf-list-v wrap">{{ auth.me?.leader_intro || '—' }}</span>
        </li>
      </ul>
    </section>

    <!-- 修改密码 -->
    <section class="pf-card">
      <button type="button" class="pf-collapse-head" :aria-expanded="pwdOpen" @click="togglePwd">
        <span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
            <path d="M3 3v5h5" />
          </svg>
          修改密码
        </span>
        <span class="pf-chevron" :class="{ open: pwdOpen }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="m6 9 6 6 6-6" />
          </svg>
        </span>
      </button>

      <div v-if="pwdOpen" class="pf-collapse-body">
        <div class="pf-grid-3">
          <div>
            <label class="pf-label" for="pf-old-pwd">原密码</label>
            <input id="pf-old-pwd" v-model="pwd.current" type="password" class="pf-input" placeholder="请输入原密码" autocomplete="current-password" />
          </div>
          <div>
            <label class="pf-label" for="pf-new-pwd">新密码</label>
            <input id="pf-new-pwd" v-model="pwd.new" type="password" class="pf-input" placeholder="至少 6 位" autocomplete="new-password" />
          </div>
          <div>
            <label class="pf-label" for="pf-confirm-pwd">确认新密码</label>
            <input id="pf-confirm-pwd" v-model="pwd.confirm" type="password" class="pf-input" placeholder="再次输入新密码" autocomplete="new-password" />
          </div>
        </div>
        <p v-if="pwdError" class="pf-error">{{ pwdError }}</p>
        <div class="pf-form-actions">
          <button type="button" class="pf-btn" @click="togglePwd">取消</button>
          <button type="button" class="pf-btn pf-btn-primary" :disabled="pwdLoading" @click="changePassword">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
            </svg>
            确认修改
          </button>
        </div>
      </div>
    </section>

    <!-- 双因素认证 (2FA) -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>双因素认证 (2FA)</p>
        <span class="pf-count" :class="twofa.enabled ? 'ok' : twofa.bound ? 'warn' : ''">
          {{ twofa.enabled ? '已开启' : twofa.bound ? '已绑定未开启' : '未绑定' }}
        </span>
      </div>

      <div class="profile-form">
        <p class="pf-hint">
          使用 Google Authenticator 等 App 扫码绑定；开启后登录需额外输入 6 位验证码。
        </p>

        <template v-if="twofaSetup">
          <div class="twofa-qr">
            <img :src="twofaSetup.qr_data_uri" alt="2FA QR" width="180" height="180" />
          </div>
          <div>
            <label class="pf-label" for="pf-2fa-secret">手动输入密钥</label>
            <input id="pf-2fa-secret" :value="twofaSetup.secret" readonly class="pf-input pf-mono" />
          </div>
          <div>
            <label class="pf-label" for="pf-2fa-code">验证码（完成绑定）</label>
            <input id="pf-2fa-code" v-model="twofaCode" class="pf-input" inputmode="numeric" maxlength="6" placeholder="000000" />
          </div>
          <div class="pf-form-actions">
            <button type="button" class="pf-btn pf-btn-primary" :disabled="twofaLoading" @click="confirm2faBind">
              确认绑定并开启
            </button>
          </div>
        </template>

        <template v-else-if="!twofa.bound">
          <div class="pf-form-actions">
            <button type="button" class="pf-btn pf-btn-primary" :disabled="twofaLoading" @click="start2faSetup">
              {{ twofaLoading ? '生成中…' : '开始绑定' }}
            </button>
          </div>
        </template>

        <template v-else>
          <div v-if="!twofa.enabled">
            <label class="pf-label" for="pf-2fa-reenable">验证码（重新开启）</label>
            <input id="pf-2fa-reenable" v-model="twofaCode" class="pf-input" inputmode="numeric" maxlength="6" placeholder="000000" />
          </div>
          <div>
            <label class="pf-label" for="pf-2fa-pwd">当前密码</label>
            <input id="pf-2fa-pwd" v-model="twofaPwd" type="password" class="pf-input" autocomplete="current-password" />
          </div>
          <div v-if="twofa.enabled">
            <label class="pf-label" for="pf-2fa-disable-code">验证码</label>
            <input id="pf-2fa-disable-code" v-model="twofaCode" class="pf-input" inputmode="numeric" maxlength="6" placeholder="关闭/重置时需填写" />
          </div>
          <div class="pf-form-actions">
            <button v-if="twofa.enabled" type="button" class="pf-btn" :disabled="twofaLoading" @click="disable2fa">
              关闭 2FA
            </button>
            <button type="button" class="pf-btn" :disabled="twofaLoading" @click="reset2fa">重置绑定</button>
            <button v-if="twofa.enabled" type="button" class="pf-btn" :disabled="twofaLoading" @click="start2faSetup">
              更换密钥
            </button>
            <button v-if="!twofa.enabled" type="button" class="pf-btn pf-btn-primary" :disabled="twofaLoading" @click="enable2fa">
              开启 2FA
            </button>
          </div>
        </template>

        <p v-if="twofaError" class="pf-error">{{ twofaError }}</p>
      </div>
    </section>

    <p v-if="savedFlag" class="profile-flash">{{ savedFlag }}</p>
  </div>
</template>

<style scoped>
/* ── 1:1 复刻 UI/H5.html 原型「个人中心」页 ── */
.profile-page {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 账号头部 ── */
.profile-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.profile-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  flex-shrink: 0;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
  box-shadow: 0 2px 10px rgba(99, 102, 241, 0.4);
  color: #fff;
}
.profile-avatar svg { width: 24px; height: 24px; }

.profile-head-info { min-width: 0; flex: 1; }

.profile-head-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.profile-head-name {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--pf-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.profile-online {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
  font-size: 10px;
  font-weight: 600;
}
.profile-online-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  animation: profile-pulse 2s ease-in-out infinite;
}
@keyframes profile-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.profile-head-sub {
  margin: 2px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 表单容器 ── */
.profile-form {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 提示与错误文案、2FA 密钥的等宽字体都在全局 styles.css 里，这里只留本页布局。 */

/* ── 2FA ── */
.twofa-qr {
  display: flex;
  justify-content: center;
  padding: 4px 0;
}
.twofa-qr img { border-radius: 8px; }

/* 状态徽章（2FA 状态）的配色在全局 styles.css 的 .pf-count.ok / .pf-count.warn 里，
   这里只保留本页布局相关的样式。 */

/* ── 保存成功提示 ── */
.profile-flash {
  margin: 0;
  text-align: center;
  font-size: 11px;
  color: #22c55e;
}

/* ── 移动端 ── */
@media (max-width: 640px) {
  .profile-page { gap: 10px; }
}
</style>
