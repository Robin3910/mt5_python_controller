import api from './client'
import type { FollowApplication } from './types'

export interface FollowChannelStats {
  pending_nodes: number
  reviewed_nodes: number
  following_nodes: number
}
export interface FollowChannelProfile extends FollowChannelStats {
  display_name: string
  intro: string
  level: number
  level_name: string
  running_days: number
  symbols: string[]
  total_profit: number | null
  follow_paused: boolean
}
export interface FollowApplicant {
  follower_user_id: number
  applicant_name: string
  nodes: FollowApplication[]
}
export interface FollowChannelRequests {
  applicants: FollowApplicant[]
  total: number
  page: number
  page_size: number
  all_pending_request_ids: number[]
  stats: FollowChannelStats
}
export const fetchFollowChannelProfile = async (): Promise<FollowChannelProfile> =>
  (await api.get('/api/follow-channel/profile')).data

export const fetchFollowChannelRequests = async (status: string, q: string, page: number): Promise<FollowChannelRequests> =>
  (await api.get('/api/follow-channel/requests', { params: { status, q, page } })).data

export const approveFollowRequests = async (requestIds: number[]): Promise<{ approved: unknown[]; failed: { request_id: number; detail: string }[] }> =>
  (await api.post('/api/follow-channel/requests/approve-batch', { request_ids: requestIds })).data

export async function rejectFollowRequest(requestId: number): Promise<void> {
  await api.post(`/api/follow-channel/requests/${requestId}/reject`, { reason: '带单员暂不接收该节点' })
}
export async function setFollowMembersPaused(requestIds: number[], paused: boolean): Promise<void> {
  await api.post('/api/follow-channel/members/state', { request_ids: requestIds, paused })
}
export async function removeFollowMembers(requestIds: number[]): Promise<void> {
  await api.post('/api/follow-channel/members/remove', { request_ids: requestIds })
}

export type FollowUiAction = 'page_view' | 'search' | 'filter' | 'open_picker' | 'select_nodes' | 'cancel_dialog' | 'collapse_group' | 'refresh' | 'page' | 'open_mine'
export async function auditFollowUi(action: FollowUiAction, data: { node_ids?: string[]; value?: string } = {}): Promise<void> {
  await api.post('/api/follow-channel/ui-events', { action, ...data })
}
