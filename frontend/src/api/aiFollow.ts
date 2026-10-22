import api from './client'
import type {
  AiJoinPayload,
  AiJoinResponse,
  AiLeavePayload,
  AiLeaveResponse,
  AiMeResponse,
  AiRobotDetailResponse,
  AiRobotListResponse,
} from './types'

/** 机器人广场列表（q 匹配用户名 / 显示名 / 主节点名 / MT5 号） */
export async function fetchAiRobots(q = ''): Promise<AiRobotListResponse> {
  const { data } = await api.get<AiRobotListResponse>('/api/ai-follow/robots', {
    params: q ? { q } : undefined,
  })
  return data
}

/** 机器人详情：可开放的 AI 分组 + 我的可跟单节点 */
export async function fetchAiRobotDetail(leaderUserId: number): Promise<AiRobotDetailResponse> {
  const { data } = await api.get<AiRobotDetailResponse>(`/api/ai-follow/robots/${leaderUserId}`)
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
