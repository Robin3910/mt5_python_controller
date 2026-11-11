import { ElMessage } from 'element-plus'
import { auditFollowUi, type FollowUiAction } from '@/api/followChannel'

export function trackFollowUi(action: FollowUiAction, data: { node_ids?: string[]; value?: string } = {}): void {
  void auditFollowUi(action, data).catch(() => ElMessage.warning('操作记录写入失败，请检查连接'))
}
