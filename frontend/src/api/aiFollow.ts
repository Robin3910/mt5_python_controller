import api from './client'
import type {
  AiJoinPayload,
  AiJoinResponse,
  AiLeavePayload,
  AiLeaveResponse,
  AiMeResponse,
  AiPausePayload,
  AiPauseResponse,
  AiRobotDetailResponse,
  AiRobotListResponse,
} from './types'

/** 机器人广场列表（q 匹配用户名 / 显示名 / 主节点名 / MT5 号）
 *  window 控制绩效统计窗口（7d / 30d / 90d / all），默认 30d */
export async function fetchAiRobots(q = '', window = '30d'): Promise<AiRobotListResponse> {
  const { data } = await api.get<AiRobotListResponse>('/api/ai-follow/robots', {
    params: q || window ? { ...(q ? { q } : {}), ...(window ? { window } : {}) } : undefined,
  })
  return data
}

/** 机器人详情：可开放的 AI 分组 + 我的可跟单节点 */
export async function fetchAiRobotDetail(
  leaderUserId: number,
  window = '30d',
): Promise<AiRobotDetailResponse> {
  const { data } = await api.get<AiRobotDetailResponse>(
    `/api/ai-follow/robots/${leaderUserId}`,
    { params: window ? { window } : undefined },
  )
  return data
}

/** 我的 AI 跟单 */
export async function fetchMyAiFollowings(): Promise<AiMeResponse> {
  const { data } = await api.get<AiMeResponse>('/api/ai-follow/me')
  return data
}

/** 加入 AI 跟单 */
export async function joinAiFollow(payload: AiJoinPayload): Promise<AiJoinResponse> {
  const { data } = await api.post<AiJoinResponse>('/api/ai-follow/join', payload)
  return data
}

/** 移出 AI 跟单（在跑任务默认不停止） */
export async function leaveAiFollow(payload: AiLeavePayload): Promise<AiLeaveResponse> {
  const { data } = await api.post<AiLeaveResponse>('/api/ai-follow/leave', payload)
  return data
}

/**
 * 暂停 / 恢复【AI 交易】市场对外接单。
 *
 * 暂停后：新客户无法加入 AI 分组；新信号只发主节点。
 * 已有跟单关系与在跑任务**不动** —— 等跟单员自行移出。
 * 只影响 AI 市场（AI 智能加仓），带单市场用 setFollowPaused。
 */
export async function setAiFollowPaused(payload: AiPausePayload): Promise<AiPauseResponse> {
  const { data } = await api.post<AiPauseResponse>('/api/ai-follow/pause', payload)
  return data
}
