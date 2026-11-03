import { defineStore } from 'pinia'

/**
 * 「登录页发现未注册 → 直接进注册第二步」的中转状态。
 *
 * 为什么用 store 而不是 URL query：
 * 票据（JWT）放进 URL 会进浏览器历史、Referer 头和服务器访问日志 —— 泄露风险。
 * 放内存里，用户刷新页面就丢了（需要重新收码），这是可接受的降级。
 */
export interface RegisterHandoff {
  /** 手机号（归一化前的用户输入） */
  phone: string
  /** 「手机号已验证」票据 */
  ticket: string
}

interface RegisterState {
  handoff: RegisterHandoff | null
}

export const useRegisterStore = defineStore('register', {
  state: (): RegisterState => ({ handoff: null }),
  actions: {
    /** 登录页校验通过后，发现该号码未注册 → 交给注册页 */
    setHandoff(handoff: RegisterHandoff): void {
      this.handoff = handoff
    },
    /** 注册页取走票据（一次性） */
    takeHandoff(): RegisterHandoff | null {
      const value = this.handoff
      this.handoff = null
      return value
    },
    clear(): void {
      this.handoff = null
    },
  },
})
