import { defineStore } from 'pinia'
import api from '@/api/client'
import type { MeInfo, PhoneRegisterPayload, PhoneRegisterResult, SmsPurpose, SmsStatus } from '@/api/types'

interface AuthState {
  token: string
  /** 当前登录用户（角色 / 菜单）；换 token 或退出时清空，由路由守卫按需拉取 */
  me: MeInfo | null
}

export interface LoginResult {
  requires2fa: boolean
  loginToken?: string
}

/** 短信服务可用性（前端据此隐藏验证码入口）；null = 还没查过 */
let smsStatusCache: SmsStatus | null = null

// 登录态：token 持久化在 localStorage，刷新页面后仍保持登录
export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({ token: localStorage.getItem('token') || '', me: null }),
  getters: {
    isAuthed: (s): boolean => !!s.token,
    /** 超级管理员：可见全部数据与管理员操作（前端只做显示控制，拦截在后端） */
    isAdmin: (s): boolean => !!s.me?.is_admin,
    /** 是否拥有某菜单；未拉到用户信息前一律视为没有 */
    hasMenu: (s) => (code: string): boolean => !!s.me && s.me.menus.includes(code),
    /** 是否拥有某能力；未拉到用户信息前一律视为没有。管理员永远返回 true。 */
    hasCapability: (s) => (cap: string): boolean =>
      !!s.me?.is_admin || (!!s.me && s.me.capabilities?.includes(cap)) || false,
    /** 能否使用 AI 智能加仓策略（tpl_1） */
    canUseAiStrategy: (s): boolean => s.me?.is_admin || (!!s.me && s.me.capabilities?.includes('ai_strategy')) || false,
    /** 能否作为带单员对外接单（follow_leader 能力）；跟单方向默认人人可做，不看这里 */
    canBeFollowLeader: (s): boolean =>
      s.me?.is_admin || (!!s.me && s.me.capabilities?.includes('follow_leader')) || false,
  },
  actions: {
    setToken(token: string): void {
      this.token = token
      this.me = null
      localStorage.setItem('token', token)
    },
    async fetchMe(): Promise<MeInfo> {
      const { data } = await api.get<MeInfo>('/api/me')
      this.me = data
      return data
    },
    async login(username: string, password: string): Promise<LoginResult> {
      const { data } = await api.post('/api/login', { username, password })
      if (data.requires_2fa) {
        return { requires2fa: true, loginToken: data.login_token }
      }
      this.setToken(data.token)
      return { requires2fa: false }
    },
    async login2fa(loginToken: string, totpCode: string): Promise<void> {
      const { data } = await api.post('/api/login/2fa', {
        login_token: loginToken,
        totp_code: totpCode,
      })
      this.setToken(data.token)
    },
    logout(): void {
      this.token = ''
      this.me = null
      localStorage.removeItem('token')
    },
    async changePassword(currentPassword: string, newPassword: string): Promise<void> {
      await api.post('/api/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
    },
    async fetch2faStatus(): Promise<{ enabled: boolean; bound: boolean; pending_setup: boolean }> {
      const { data } = await api.get('/api/2fa/status')
      return data
    },
    async setup2fa(): Promise<{ secret: string; otpauth_uri: string; qr_data_uri: string }> {
      const { data } = await api.post('/api/2fa/setup')
      return data
    },
    async confirm2fa(totpCode: string): Promise<void> {
      await api.post('/api/2fa/confirm', { totp_code: totpCode })
    },
    async enable2fa(totpCode: string): Promise<void> {
      await api.post('/api/2fa/enable', { totp_code: totpCode })
    },
    async disable2fa(password: string, totpCode?: string): Promise<void> {
      await api.post('/api/2fa/disable', { password, totp_code: totpCode || undefined })
    },
    async reset2fa(password: string, totpCode?: string): Promise<void> {
      await api.post('/api/2fa/reset', { password, totp_code: totpCode || undefined })
    },

    // ----------------- 手机号注册 / 验证码登录 -----------------
    /** 短信服务是否可用；未配置或已关闭时前端隐藏验证码入口。 */
    async fetchSmsStatus(force = false): Promise<SmsStatus> {
      if (smsStatusCache && !force) return smsStatusCache
      try {
        const { data } = await api.get<SmsStatus>('/api/auth/sms/status')
        smsStatusCache = data
        return data
      } catch {
        // 接口不可用（含 404）视为未配置
        const fallback: SmsStatus = {
          enabled: false,
          require_approval: true,
          code_ttl_seconds: 300,
          send_interval_seconds: 60,
        }
        smsStatusCache = fallback
        return fallback
      }
    },
    /** 发送验证码；返回有效期与重发冷却（秒） */
    async sendSmsCode(phone: string, purpose: SmsPurpose): Promise<{ expire_in: number; cooldown: number }> {
      const { data } = await api.post('/api/auth/sms/send', { phone, purpose })
      return { expire_in: data.expire_in, cooldown: data.cooldown }
    },
    /** 校验验证码；通过则返回「手机号已验证」票据 */
    async verifySmsCode(phone: string, code: string, purpose: SmsPurpose): Promise<string> {
      const { data } = await api.post('/api/auth/sms/verify', { phone, code, purpose })
      return data.ticket
    },
    /** 手机号注册；待审核时 pending_approval=true 且没有 token */
    async registerByPhone(payload: PhoneRegisterPayload): Promise<PhoneRegisterResult> {
      const { data } = await api.post<PhoneRegisterResult>('/api/auth/register', payload)
      if (data.token) this.setToken(data.token)
      return data
    },
    /** 验证码登录；2FA 用户返回 requires2fa + loginToken（复用现有 2FA 分支） */
    async loginBySms(phone: string, code: string): Promise<LoginResult> {
      const { data } = await api.post('/api/auth/sms/login', { phone, code })
      if (data.requires_2fa) {
        return { requires2fa: true, loginToken: data.login_token }
      }
      this.setToken(data.token)
      return { requires2fa: false }
    },
  },
})
