import { ElMessage } from 'element-plus'
import { auditAiUi, type AiUiAction } from '@/api/aiChannel'

export function trackAiUi(action: AiUiAction, data: { group_id?: string; node_ids?: string[]; value?: string } = {}): void {
  void auditAiUi(action, data).catch(() => ElMessage.warning('操作记录写入失败，请检查连接'))
}
