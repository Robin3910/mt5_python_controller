import api from './client'
import type { AiFollowSettings } from './types'

export interface AiApplicationNode {
  node_id: string
  name: string | null
  mt5_login: number | null
  is_primary: boolean
  status: string
  available: boolean
  joined_at: number
  leader_paused?: boolean
  follow_state?: string | null
  settings?: AiFollowSettings
}

export interface AiApplication {
  request_id: number
  group_id: string
  leader_user_id: number
  follower_user_id: number
  applicant_name: string
  robot_name: string
  symbol: string | null
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  source: 'join' | 'inherit' | 'legacy'
  reason: string | null
  risk: { max_allow_num?: number | null; total_lot_limit?: number | null }
  nodes: AiApplicationNode[]
  created_at: number
}

export interface AiChannelGroup {
  group_id: string
  robot_name: string
  member_count: number
  pending_count: number
  pending_node_count: number
  approved_count: number
  rejected_count: number
  pending_request_ids: number[]
  leader_paused: boolean
  requests: AiApplication[]
}

export interface AiChannelRequests {
  groups: AiChannelGroup[]
  total: number
  page: number
  page_size: number
  all_pending_request_ids: number[]
  all_pending_node_count: number
  stats: { pending_requests: number; reviewed_requests: number }
}

export interface AiChannelProfile {
  display_name: string
  intro: string
  level: number
  level_name: string
  running_days: number
  symbols: string[]
  running_robots: number
  channel_status: 'online' | 'paused' | 'offline'
  total_profit: number | null
  pending_requests: number
  reviewed_requests: number
}

export const fetchAiChannelProfile = async (): Promise<AiChannelProfile> =>
  (await api.get('/api/ai-channel/profile')).data

export const fetchAiChannelRequests = async (status: string, q: string, page: number): Promise<AiChannelRequests> =>
  (await api.get('/api/ai-channel/requests', { params: { status, q, page } })).data

export async function approveAiApplications(requestIds: number[]): Promise<{ approved: unknown[]; failed: { request_id: number; detail: string }[] }> {
  return (await api.post('/api/ai-channel/requests/approve-batch', { request_ids: requestIds })).data
}

export async function rejectAiApplication(requestId: number): Promise<void> {
  await api.post(`/api/ai-channel/requests/${requestId}/reject`, {})
}

export async function setAiChannelPaused(groupId: string, paused: boolean, followerUserId?: number): Promise<void> {
  await api.post('/api/ai-channel/members/state', { group_id: groupId, paused, follower_user_id: followerUserId })
}

export async function deleteAiChannelMembers(groupId: string, nodeIds: string[], version: string, followerUserId?: number): Promise<void> {
  await api.post('/api/ai-channel/members/delete', { group_id: groupId, node_ids: nodeIds, version, follower_user_id: followerUserId })
}

export type AiUiAction = 'page_view' | 'search' | 'filter' | 'open_picker' | 'select_nodes' | 'cancel_dialog' | 'collapse_group' | 'open_delete' | 'refresh' | 'follow_step' | 'follow_settings'

export async function auditAiUi(action: AiUiAction, data: { group_id?: string; node_ids?: string[]; value?: string } = {}): Promise<void> {
  await api.post('/api/ai-follow/ui-events', { action, ...data })
}
