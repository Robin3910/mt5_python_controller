import api from './client'

/** 财经日历：我的黑名单设置 */
export interface NewsSettings {
  /** 新闻黑名单总开关（默认开） */
  enabled: boolean
  /** 事件前多少分钟开始停止新开仓（0–120，默认 30） */
  before_min: number
  /** 事件后多少分钟恢复开仓（0–120，默认 30） */
  after_min: number
  /** 最低影响等级：high / medium / low（默认 high，只拦高影响） */
  min_impact: string
  /** 关注币种（默认 ['USD']，黄金 XAUUSD 归 USD） */
  currencies: string[]
  /** 品种级覆盖（空 = 按币种推导） */
  symbols: string[]
  /** 手动停止做单（急停），与新闻黑名单是两层 */
  manual_paused: boolean
  manual_paused_at: number | null
  manual_pause_note: string | null
  /** 顶部滚动提示条被关闭的当天（北京时间 YYYY-MM-DD）；null = 当天未关闭 */
  banner_dismissed_date: string | null
}

/** 当前黑名单状态 */
export interface NewsStatus {
  /** 当前是否禁止新开仓 */
  blocked: boolean
  reason: string | null
  code: string | null
  /** 黑名单结束时刻（epoch 秒），未阻塞为 null */
  until: number | null
  /** 剩余秒数（前端倒计时用） */
  remaining_sec: number
  /** 服务端当前时刻（epoch 秒），用于校准本地时钟偏移 */
  server_time: number
  settings: NewsSettings
}

/** 一条财经日历事件 */
export interface NewsEvent {
  event_id: string
  /** 事件时刻（epoch 秒） */
  event_time: number
  /** 北京时间展示串（服务端已格式化） */
  event_time_text: string
  title: string
  country: string
  impact: string
  forecast: string | null
  previous: string | null
  /** 是否落入我的黑名单 */
  in_blackout: boolean
  /** 不在黑名单时说明为什么（币种不匹配 / 等级不足） */
  block_reason: string | null
}

/** 日历数据新鲜度 */
export interface NewsCalendarInfo {
  fetched_at: number
  fetched_at_text: string | null
  /** 数据是否可能过期（抓取失败或太久没更新） */
  stale: boolean
  event_count: number
  timezone: string
}

/** 顶部滚动提示条的状态 */
export interface NewsBanner {
  /** 今天是否有黑名单时段且尚未被关闭 —— 前端据此决定是否显示顶栏 */
  show: boolean
  /** 今天是否已被用户关闭过 */
  dismissed: boolean
  /** 服务器当前的北京日期（YYYY-MM-DD） */
  today: string
  /** 一句话说明（提示条文案） */
  reason: string
  /** 今天落进黑名单的事件 */
  events: Array<{
    event_id: string
    event_time: number
    title: string
    country: string
    impact: string
  }>
  server_time: number
}

export interface NewsOverview {
  settings: NewsSettings
  status: NewsStatus
  banner: NewsBanner
  events: NewsEvent[]
  calendar: NewsCalendarInfo
}

/** PUT /api/me/news/settings 的请求体：字段级「传什么改什么」 */
export interface NewsSettingsUpdate {
  enabled?: boolean
  before_min?: number
  after_min?: number
  min_impact?: string
  currencies?: string[]
  symbols?: string[]
  manual_paused?: boolean
  manual_pause_note?: string | null
}

export const fetchNewsOverview = async (symbol = ''): Promise<NewsOverview> =>
  (await api.get('/api/me/news', { params: symbol ? { symbol } : {} })).data

export const saveNewsSettings = async (payload: NewsSettingsUpdate): Promise<NewsSettings> => {
  const { data } = await api.put<{ settings: NewsSettings }>('/api/me/news/settings', payload)
  return data.settings
}

export const refreshNewsCalendar = async (): Promise<{ ok: boolean; message: string; count: number }> =>
  (await api.post('/api/me/news/refresh', {})).data

/**
 * 关闭顶部滚动提示条：**当天**不再显示，次日自动重新提示。
 *
 * day 传北京时间日期；不传则由服务端取当前日期。
 */
export const dismissNewsBanner = async (day?: string): Promise<{ ok: boolean; day: string }> =>
  (await api.post('/api/me/news/banner/dismiss', day ? { day } : {})).data
