import { ElMessageBox } from 'element-plus'
import { previewNodeDeletion, deletionBlocked, deletionImpact } from '@/api/nodeDeletion'
import { useHubStore } from '@/stores/hub'

export async function deleteNodeWithConfirmation(nodeId: string, options?: { q?: string }): Promise<boolean> {
  const preview = await previewNodeDeletion(nodeId)
  if (preview.blocked.length) {
    await ElMessageBox.alert(deletionBlocked(preview), '暂不能删除节点', { type: 'warning', confirmButtonText: '知道了' })
    return false
  }
  try {
    await ElMessageBox.confirm(deletionImpact(preview), '删除系统节点', {
      type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消',
      closeOnClickModal: false,
    })
  } catch { return false }
  await useHubStore().deleteNode(nodeId, options, preview.version)
  return true
}
