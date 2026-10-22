import api from './client'
import type {
  FollowDetailResponse,
  FollowJoinPayload,
  FollowJoinResponse,
  FollowLeavePayload,
  FollowLeaveResponse,
  FollowLeaderSummary,
  FollowMeResponse,
} from './types'

/** 带单员列表（q 匹配用户名 / 显示名 / 主节点名 / MT5 号） */
export async function fetchLeaders(q = '', window = '30d'): Promise<FollowLeaderSummary[]> {
  const { data } = await api.get<{ leaders: FollowLeaderSummary[] }>('/api/follow/leaders', {
    params: { ...(q ? { q } : {}), window },
  })
  return data.leaders
}

/** 带单员详情：可跟单分组 + 我可用于跟单的节点 */
export async function fetchLeaderDetail(
  leaderUserId: number,
  window = '30d',
): Promise<FollowDetailResponse> {
  const { data } = await api.get<FollowDetailResponse>(
    `/api/follow/leaders/${leaderUserId}`,
    { params: { window } },
  )
  return data
}

/** 我的跟单（按带单员聚合） */
export async function fetchMyFollowings(): Promise<FollowMeResponse> {
  const { data } = await api.get<FollowMeResponse>('/api/follow/me')
  return data
}

/** 加入跟单（不需要 follow_leader 能力 —— 跟单默认人人可做） */
export async function joinFollow(payload: FollowJoinPayload): Promise<FollowJoinResponse> {
  const { data } = await api.post<FollowJoinResponse>('/api/follow/join', payload)
  return data
}

/** 移出跟单（带单员已被撤销能力时也能移出，用于自行清理） */
export async function leaveFollow(payload: FollowLeavePayload): Promise<FollowLeaveResponse> {
  const { data } = await api.post<FollowLeaveResponse>('/api/follow/leave', payload)
  return data
}