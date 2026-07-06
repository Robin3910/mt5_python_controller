import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'

/** 二次确认弹窗；用户点「取消」或关闭时返回 false。 */
export async function confirmAction(message: string, title = '确认操作'): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      type: 'warning',
      closeOnClickModal: false,
    })
    return true
  } catch {
    return false
  }
}
