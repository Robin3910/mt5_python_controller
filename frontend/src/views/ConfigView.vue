<script setup lang="ts">
// 配置页：系统配置 / 账户设置（Tab）
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'

const hub = useHubStore()
const auth = useAuthStore()
const router = useRouter()

type ConfigTab = 'system' | 'account'
const tab = ref<ConfigTab>('system')
const tabs: { key: ConfigTab; label: string }[] = [
  { key: 'system', label: '系统配置' },
  { key: 'account', label: '账户设置' },
]

const lot = reactive({ enabled: false, value: 0.1 })
const dispatch = reactive({ mode: 'sync' as 'sync' | 'poll', position_scope: 'symbol' as 'symbol' | 'account' })
const filtersText = ref('{}')
const filtersError = ref('')
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

// 节点令牌（全局共享 NODE_TOKEN）
const nodeToken = reactive({ value: '', updated_at: 0 })
const nodeTokenShow = ref(false)
const nodeTokenLoading = ref(false)
const nodeTokenError = ref('')

const FILTER_EXAMPLE = `{
  "XAUUSD": {
    "enabled": true,
    "default_action": "block",
    "intervals": [
      { "low": 2300, "high": 2350, "allow": ["BUY"] },
      { "low": 2350, "high": 2400, "allow": ["SELL"] }
    ]
  }
}`

onMounted(async () => {
  await hub.fetchConfig()
  lot.enabled = hub.lot.enabled
  lot.value = hub.lot.value
  dispatch.mode = hub.dispatch.mode
  dispatch.position_scope = hub.dispatch.position_scope
  filtersText.value = JSON.stringify(hub.filters ?? {}, null, 2)
  await load2faStatus()
  await loadNodeToken()
})

async function loadNodeToken(): Promise<void> {
  try {
    const t = await hub.fetchNodeToken()
    nodeToken.value = t.token
    nodeToken.updated_at = t.updated_at
  } catch {
    nodeTokenError.value = '无法加载节点令牌'
  }
}

function copyNodeToken(): void {
  if (!nodeToken.value) return
  navigator.clipboard?.writeText(nodeToken.value)
  flash('节点令牌已复制到剪贴板')
}

async function rotateNodeToken(): Promise<void> {
  if (!confirm('重置全局节点令牌？所有节点的 .env 都需更新后才能继续接入，请确认。')) return
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

async function saveLot(): Promise<void> {
  await hub.saveLot({ enabled: lot.enabled, value: lot.value })
  flash('全局手数已保存')
}
async function saveDispatch(): Promise<void> {
  await hub.saveDispatch({ mode: dispatch.mode, position_scope: dispatch.position_scope })
  flash('分发策略已保存')
}
async function saveFilters(): Promise<void> {
  filtersError.value = ''
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(filtersText.value || '{}')
  } catch {
    filtersError.value = 'JSON 格式错误，请检查'
    return
  }
  await hub.saveFilters(parsed)
  flash('区间过滤规则已保存')
}
function loadExample(): void {
  filtersText.value = FILTER_EXAMPLE
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

  <div class="tabs">
    <button
      v-for="t in tabs"
      :key="t.key"
      type="button"
      class="tab"
      :class="{ active: tab === t.key }"
      @click="tab = t.key"
    >
      {{ t.label }}
    </button>
  </div>

  <div v-if="tab === 'system'" class="grid layout-config">
    <div class="card card-pad">
      <strong>全局手数</strong>
      <p class="muted" style="font-size: 12px">开启后，所有「跟随全局」策略的节点统一使用该手数。</p>
      <div class="form-grid">
        <div>
          <label>启用全局手数</label>
          <select v-model="lot.enabled"><option :value="true">启用</option><option :value="false">关闭</option></select>
        </div>
        <div><label>手数</label><input v-model.number="lot.value" type="number" step="0.01" :disabled="!lot.enabled" /></div>
        <button class="btn-primary" @click="saveLot">保存</button>
      </div>
    </div>

    <div class="card card-pad">
      <strong>分发策略</strong>
      <p class="muted" style="font-size: 12px">同步模式：所有节点并发执行；轮询模式：按顺序依次领取执行。</p>
      <div class="form-grid">
        <div>
          <label>分发模式</label>
          <select v-model="dispatch.mode"><option value="sync">全员同步</option><option value="poll">轮询领取</option></select>
        </div>
        <div>
          <label>持仓判定范围</label>
          <select v-model="dispatch.position_scope">
            <option value="symbol">按品种（同品种无持仓才开）</option>
            <option value="account">按账户（账户无任何持仓才开）</option>
          </select>
        </div>
        <button class="btn-primary" @click="saveDispatch">保存</button>
      </div>
    </div>

    <div class="card card-pad span-full">
      <div class="row between">
        <strong>多区间方向过滤</strong>
        <button class="btn-sm btn-ghost" @click="loadExample">载入示例</button>
      </div>
      <p class="muted" style="font-size: 12px">
        以品种为键配置价格区间允许的方向。<code>default_action</code> 为不在任何区间时的处理（block 拦截 / pass 放行）。
        留空 <code>{}</code> 表示不过滤。
      </p>
      <textarea v-model="filtersText" rows="14" style="font-family: ui-monospace, monospace"></textarea>
      <div v-if="filtersError" style="color: var(--red); font-size: 12px; margin-top: 6px">{{ filtersError }}</div>
      <div class="row" style="margin-top: 12px"><button class="btn-primary" @click="saveFilters">保存过滤规则</button></div>
    </div>
  </div>

  <div v-else class="grid layout-config">
    <div class="card card-pad span-full">
      <div class="row between">
        <strong>节点令牌 (NODE_TOKEN)</strong>
        <span class="muted" style="font-size: 12px">最近更新：{{ fmtTokenUpdatedAt() }}</span>
      </div>
      <p class="muted" style="font-size: 12px">
        所有 node_client 共享此令牌进行接入鉴权。将其填入每个节点 <code>.env</code> 的
        <code>NODE_TOKEN</code>。点击「重置」会立即作废旧令牌，所有节点必须更新后才能重新接入。
      </p>
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

    <div class="card card-pad">
      <strong>修改密码</strong>
      <p class="muted" style="font-size: 12px">修改管理员登录密码；成功后需使用新密码重新登录。</p>
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
