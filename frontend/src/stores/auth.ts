import { defineStore } from 'pinia'
import api from '@/api/client'

interface AuthState {
  token: string
}

export interface LoginResult {
  requires2fa: boolean
  loginToken?: string
}

// 登录态：token 持久化在 localStorage，刷新页面后仍保持登录
export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({ token: localStorage.getItem('token') || '' }),
  getters: {
    isAuthed: (s): boolean => !!s.token,
  },
  actions: {
    setToken(token: string): void {
      this.token = token
      localStorage.setItem('token', token)
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
  },
})
