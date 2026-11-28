import { ElMessageBox } from 'element-plus'
import { previewNodeDeletion, deletionBlocked, deletionImpact } from '@/api/nodeDeletion'
import { useHubStore } from '@/stores/hub'

export async function deleteNodeWithConfirmation(nodeId: string, options?: { q?: string }): Promise<'pending' | 'deleted' | undefined> {
  const preview = await previewNodeDeletion(nodeId)
  const state = preview.blocked.length ? `\n\n当前状态\n${deletionBlocked(preview)}` : ''
  const cleanup = '系统会终止该节点的策略任务，平掉全部持仓并撤销全部挂单。节点离线或全平失败时，恢复后会自动继续，核验账户清空后删除。'
  try {
    await ElMessageBox.confirm(`${deletionImpact(preview)}${state}\n\n${cleanup}`, '删除系统节点', {
      type: 'warning', confirmButtonText: '确认全平并删除', cancelButtonText: '取消',
      closeOnClickModal: false,
    })
  } catch { return }
  return await useHubStore().deleteNode(nodeId, options, preview.version)
}
