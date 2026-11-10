import api from './client'

export interface DeletionPreview {
  node_ids: string[]
  nodes: { node_id: string; name: string; mt5_login: number; is_primary: boolean }[]
  node_count: number
  member_count: number
  binding_count: number
  other_group_count: number
  cancelled_application_count: number
  version: string
  blocked: { node_id: string; name: string; reasons: string[] }[]
}

export async function previewNodeDeletion(nodeId: string): Promise<DeletionPreview> {
  return (await api.get(`/api/nodes/${nodeId}/deletion-preview`)).data
}

export async function previewChannelDeletion(groupId: string, followerUserId?: number): Promise<DeletionPreview> {
  return (await api.post('/api/ai-channel/members/deletion-preview', { group_id: groupId, follower_user_id: followerUserId })).data
}

export function deletionImpact(data: DeletionPreview): string {
  return `将删除 ${data.member_count} 名成员关联的 ${data.node_count} 个系统节点。\n` +
    `共清理 ${data.binding_count} 条绑定，影响其他 ${data.other_group_count} 个分组或机器人。\n` +
    `同时取消 ${data.cancelled_application_count} 笔待审核申请。\n` +
    `目标：${data.nodes.map((node) => `${node.name}（${node.mt5_login}）`).join('、')}\n\n` +
    '节点凭证及全部绑定一并移除，交易历史与操作审计保留，此操作不可恢复。'
}

export function deletionBlocked(data: DeletionPreview): string {
  return data.blocked.map((node) => `${node.name}：${node.reasons.join('、')}`).join('\n')
}
