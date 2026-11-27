import type { NodeOut } from '@/api/types'

export function isPendingNode(node: NodeOut): boolean {
  return node.approval_status === 'pending'
}

/** 待开通节点的开关表示管理员确认意图，实际 enabled 仍为 false。 */
export function nodeEnableIntent(node: NodeOut): boolean {
  return isPendingNode(node) ? Boolean(node.admin_enable_requested) : node.enabled
}

function pendingNodeAssigned(node: NodeOut): boolean {
  return node.requested_by_user_id != null && node.owner_user_id === node.requested_by_user_id
}

export function nodeApprovalHint(node: NodeOut): string {
  if (!isPendingNode(node)) return ''
  const assigned = pendingNodeAssigned(node)
  const applicant = node.requested_by_username || `用户 #${node.requested_by_user_id ?? '—'}`
  if (!node.admin_enable_requested && !assigned) return `待开通：请确认启用，并在用户权限页分配给 ${applicant}（管理员申请无需分配）`
  if (!node.admin_enable_requested) return '已分配，待管理员确认启用'
  return `已确认启用，待在用户权限页分配给 ${applicant}`
}

/** 节点页确认启用时的说明。已归属申请人的，确认后即开通。 */
export function nodeEnableConfirmHint(node: NodeOut): string {
  if (pendingNodeAssigned(node)) return '确认启用后才会开通。'
  return '确认启用与分配给申请人两项齐全后才会开通；操作顺序不限。'
}

export function nodeEnableLabel(node: NodeOut): string {
  if (isPendingNode(node)) return node.admin_enable_requested ? '已确认启用' : '待确认启用'
  return node.enabled ? '已启用' : '已禁用'
}
