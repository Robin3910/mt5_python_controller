import { defineStore } from 'pinia'
import {
  dismissNewsBanner,
  fetchNewsOverview,
  refreshNewsCalendar,
  saveNewsSettings,
} from '@/api/news'
import type {
  NewsBanner,
  NewsCalendarInfo,
  NewsEvent,
  NewsSettings,
  NewsSettingsUpdate,
  NewsStatus,
} from '@/api/news'

/**
 * 财经日历（新闻黑名单）全局 store。
 *
 * 为什么独立成 store 而不是放在页面里：
 *  - 顶部滚动提示条挂在 App.vue 上，**每个页面都要能显示**；
 *  - 财经日历页面自己也要显示同一份状态；
 *  - 两者必须共享同一份数据，否则会出现「顶栏说有黑名单、页面说没有」
 *    的自相矛盾（尤其是用户在页面上改了设置、顶栏没跟着刷新）。
 *
 * 状态同步策略：页面与顶栏都从这里读；任何一处改了设置都调 save()，
 * 由 save() 统一回写并刷新状态，避免两处各自维护副本。
 */
interface NewsState {
  loading: boolean
  saving: boolean
  error: string
  /** 我的黑名单设置 */
  settings: NewsSettings
  /** 此刻的黑名单状态（是否禁止新开仓） */
  status: NewsStatus | null
  /** 顶部滚动提示条状态 */
  banner: NewsBanner | null
  /** 近期事件列表 */
  events: NewsEvent[]
  /** 日历数据新鲜度 */
  calendar: NewsCalendarInfo
  /** 服务端时钟与本地时钟的偏移（毫秒）—— 倒计时按服务端时钟算 */
  serverSkew: number
  /** 顶栏是否已被本次会话隐藏（点 X 后立刻消失，不等接口回来） */
  bannerHidden: boolean
}

/** 尚未加载时的占位设置：与后端默认值一致（开 / 30-30 / high / USD） */
function emptySettings(): NewsSettings {
  return {
    enabled: true,
    before_min: 30,
    after_min: 30,
    min_impact: 'high',
    currencies: ['USD'],
    symbols: [],
    manual_paused: false,
    manual_paused_at: null,
    manual_pause_note: null,
    banner_dismissed_date: null,
  }
}

function emptyStatus(): NewsStatus {
  return {
    blocked: false,
    reason: null,
    code: null,
    until: null,
    remaining_sec: 0,
    server_time: 0,
    settings: emptySettings(),
  }
}

function emptyBanner(): NewsBanner {
  return {
    show: false,
    dismissed: false,
    today: '',
    reason: '',
    events: [],
    server_time: 0,
  }
}

export const useNewsStore = defineStore('news', {
  state: (): NewsState => ({
    loading: false,
    saving: false,
    error: '',
    settings: emptySettings(),
    status: emptyStatus(),
    banner: emptyBanner(),
    events: [],
    calendar: {
      fetched_at: 0,
      fetched_at_text: null,
      stale: false,
      event_count: 0,
      timezone: '北京时间 UTC+8',
    },
    serverSkew: 0,
    bannerHidden: false,
  }),

  getters: {
    /** 此刻是否禁止新开仓 */
    blocked: (s): boolean => Boolean(s.status?.blocked),
    /** 手动急停（与新闻黑名单是两层） */
    manuallyPaused: (s): boolean => Boolean(s.settings.manual_paused),
    /** 顶部提示条最终要不要显示：服务端说显示，且本次会话没被点掉 */
    showBanner: (s): boolean => Boolean(s.banner?.show) && !s.bannerHidden,
    /** 黑名单结束时刻（epoch 秒） */
    until: (s): number | null => s.status?.until ?? null,
    /** 状态形态：ok / paused / blocked */
    state: (s): 'ok' | 'paused' | 'blocked' => {
      if (s.settings.manual_paused) return 'paused'
      if (s.status?.blocked) return 'blocked'
      return 'ok'
    },
  },

  actions: {
    /** 拉取完整的财经日历概览（设置 + 状态 + 提示条 + 事件） */
    async fetch(symbol = ''): Promise<void> {
      this.loading = true
      this.error = ''
      try {
        const data = await fetchNewsOverview(symbol)
        this.settings = { ...emptySettings(), ...data.settings }
        this.status = data.status
        this.banner = { ...emptyBanner(), ...data.banner }
        this.events = data.events || []
        this.calendar = { ...this.calendar, ...data.calendar }
        this.serverSkew = (data.status?.server_time ?? 0) * 1000 - Date.now()
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } } }
        this.error = err?.response?.data?.detail || '财经日历加载失败'
      } finally {
        this.loading = false
      }
    },

    /**
     * 保存设置并回刷状态。
     *
     * 只改设置（不碰提示条的关闭标记）—— 后端也这么保证，
     * 用户关掉提示条后再改币种，提示条不会又冒出来。
     */
    async save(payload: NewsSettingsUpdate): Promise<boolean> {
      this.saving = true
      this.error = ''
      try {
        const saved = await saveNewsSettings(payload)
        this.settings = { ...this.settings, ...saved }
        await this.fetch()
        return true
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } } }
        this.error = err?.response?.data?.detail || '保存失败，请稍后重试'
        return false
      } finally {
        this.saving = false
      }
    },

    /** 手动强制刷新一次日历数据 */
    async refresh(): Promise<string> {
      this.saving = true
      this.error = ''
      try {
        const res = await refreshNewsCalendar()
        await this.fetch()
        return res.message
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } } }
        this.error = err?.response?.data?.detail || '刷新失败'
        return '刷新失败'
      } finally {
        this.saving = false
      }
    },

    /**
     * 关闭顶部滚动提示条。
     *
     * 先本地隐藏（点 X 必须立刻消失，不能等接口往返），再调后端记「当天已关闭」。
     * 接口失败也保持隐藏 —— 用户已经明确表达了「别再显示」，不该因为网络问题
     * 又把提示条弹回来。
     */
    async dismissBanner(): Promise<void> {
      this.bannerHidden = true
      if (this.banner) this.banner = { ...this.banner, show: false, dismissed: true }
      try {
        await dismissNewsBanner(this.banner?.today || undefined)
      } catch {
        /* 失败也不回滚本地隐藏：用户意图已表达 */
      }
    },
  },
})
