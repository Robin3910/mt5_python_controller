// 设置 / 取消主节点。首页卡片和节点主页共用，预查与 409 文案保持同一套。
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import api from '@/api/client'
import type { NodeOut } from '@/api/types'
import { confirmAction } from '@/utils/confirm'

async function fetchFollowings(nodeId: string): Promise<{
  count: number
  items: Array<{ group_id: string; leader_user_id: number | null; joined_at: number | null }>
}> {
  const { data } = await api.get(`/api/nodes/${nodeId}/followings`)
  return data
}

async function fetchActiveTasks(nodeId: string): Promise<{
  count: number
  node_name: string
  items: Array<{ group_id: string; symbol: string | null; status: string; magic: number | null }>
}> {
  const { data } = await api.get(`/api/nodes/${nodeId}/active-tasks`)
  return data
}

/**
 * 旧主节点 id：设置主节点时找**同一 owner** 下当前的主节点。
 *
 * 主节点是「按 owner 维度唯一」的（后端 set_primary 只清同 owner 的其它主节点），
 * 所以这里必须按 owner_user_id 对齐，否则管理员看到多账号节点时，
 * 会把别人的主节点误当成自己要替换的那个。
 */
function findCurrentPrimaryId(nodes: NodeOut[], target: NodeOut): string | null {
  const sameOwner = nodes.filter(
    (x) => x.node_id !== target.node_id && x.is_primary
      && (x.owner_user_id ?? null) === (target.owner_user_id ?? null),
  )
  return sameOwner[0]?.node_id ?? null
}

async function showFollowingBlocked(f: {
  count: number
  node_name: string
  items: Array<{ group_id: string }>
}): Promise<void> {
  const lines = f.items.slice(0, 20).map((x) => `• ${x.group_id}`).join('\n')
  const more = f.items.length > 20 ? `\n…（共 ${f.items.length} 个）` : ''
  await ElMessageBox.alert(
    `节点「${f.node_name}」当前有 ${f.count} 个跟单分组，\n` +
      '主节点不可用于跟单（主节点需保持独立），主节点切换已被系统拒绝。\n' +
      '请先到【带单】页把该节点移出跟单，再回来重试。\n\n' +
      '跟单分组：\n' + lines + more,
    '主节点切换被拦截',
    { type: 'warning', confirmButtonText: '我知道了' },
  )
}

async function showTasksBlocked(active: {
  count: number
  node_name: string
  items: Array<{ group_id: string; symbol: string | null; status: string; magic: number | null }>
}): Promise<void> {
  const lines = active.items
    .slice(0, 20)
    .map((t) => {
      const sym = t.symbol ?? '-'
      const mg = t.magic != null ? `#${t.magic}` : '-'
      return `• ${t.group_id}  ${sym}  ${t.status}  ${mg}`
    })
    .join('\n')
  const more = active.items.length > 20 ? `\n…（共 ${active.items.length} 条）` : ''
  await ElMessageBox.alert(
    `旧主节点「${active.node_name}」仍有 ${active.count} 条在跑子任务，\n` +
      '主节点切换已被系统拒绝。请先到任务页收口这些任务后，再回来重试。\n\n' +
      '阻塞明细：\n' + lines + more,
    '主节点切换被拦截',
    { type: 'warning', confirmButtonText: '我知道了' },
  )
}

/** 客户只能设置主节点，不能取消。预查命中时直接弹出阻塞说明，不再进入二次确认。 */
export async function setNodePrimary(opts: {
  node: NodeOut
  nodes: NodeOut[]
  isAdmin: boolean
  apply: (id: string, isPrimary: boolean) => Promise<NodeOut>
}): Promise<void> {
  const n = opts.node
  const next = !n.is_primary
  if (!next && !opts.isAdmin) return

  if (next) {
    try {
      const fs = await fetchFollowings(n.node_id)
      if (fs.count > 0) {
        await showFollowingBlocked({ count: fs.count, node_name: n.name, items: fs.items })
        return
      }
    } catch (e) {
      console.warn('primary following precheck failed', e)
    }
  }

  const victimId = next ? findCurrentPrimaryId(opts.nodes, n) : n.is_primary ? n.node_id : null
  if (victimId) {
    try {
      const active = await fetchActiveTasks(victimId)
      if (active.count > 0) {
        await showTasksBlocked(active)
        return
      }
    } catch (e) {
      console.warn('primary precheck failed', e)
    }
  }

  const ok = next
    ? await confirmAction(
      `将「${n.name}」设置为本账号的主节点？\n同一账号下已有主节点时，原主节点会被自动取消。`,
      '设置为主节点',
    )
    : await confirmAction(`取消「${n.name}」的主节点标记？`, '取消主节点')
  if (!ok) return

  try {
    const updated = await opts.apply(n.node_id, next)
    ElMessage.success(
      updated.is_primary
        ? `已将「${updated.name}」设置为主节点`
        : `已取消「${updated.name}」的主节点标记`,
    )
  } catch (err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (detail && typeof detail === 'object') {
      const code = (detail as { code?: string }).code
      if (code === 'primary_switch_blocked') {
        const d = detail as {
          node_name?: string
          blocking_tasks?: Array<{ group_id: string; symbol: string | null; status: string; magic: number | null }>
        }
        await showTasksBlocked({
          count: d.blocking_tasks?.length ?? 0,
          node_name: d.node_name ?? '',
          items: d.blocking_tasks ?? [],
        })
        return
      }
      if (code === 'primary_switch_following_blocked') {
        const d = detail as {
          node_name?: string
          blocking_followings?: Array<{ group_id: string; leader_user_id: number | null; joined_at: number | null }>
        }
        await showFollowingBlocked({
          count: d.blocking_followings?.length ?? 0,
          node_name: d.node_name ?? '',
          items: d.blocking_followings ?? [],
        })
        return
      }
    }
    const text = typeof detail === 'string' ? detail : ''
    ElMessage.error(text || (next ? '设置主节点失败' : '取消主节点失败'))
  }
}
