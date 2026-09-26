import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import router from '@/router'

// 后台 WS 关闭码（与后端 connections.py 一致）
const WS_CLOSE_REVOKED = 4401 // 会话已吊销（禁用 / 改密 / 重置密码）：退出登录，不再重连
const WS_CLOSE_PERM_CHANGED = 4001 // 权限已变更（角色 / 菜单 / 节点归属）：重新拉取后重连

// 后台实时 WebSocket 单连接管理：带断线自动重连与心跳保活
let socket: WebSocket | null = null
let retryTimer: number | undefined
let keepAlive: number | undefined

// 权限变更后刷新当前用户与本人数据列表；App.vue 监听 me 变化，当前页无权访问时自动跳走
async function refreshAfterPermChange(): Promise<void> {
  const auth = useAuthStore()
  const hub = useHubStore()
  try {
    await auth.fetchMe()
  } catch {
    return
  }
  await Promise.allSettled([hub.fetchNodes(), hub.fetchGroups(), hub.fetchStrategies()])
}

// 建立连接（token 走 URL 查询参数）；同一时刻只保留一条连接
export function connectAdminWs(token: string): void {
  disconnectAdminWs()
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/ws/admin?token=${encodeURIComponent(token)}`
  const hub = useHubStore()

  socket = new WebSocket(url)
  // 收到实时事件 -> 交给 hub store 更新状态
  socket.onmessage = (ev) => {
    try {
      hub.applyWs(JSON.parse(ev.data))
    } catch {
      /* 忽略非法报文 */
    }
  }
  // 心跳保活，避免被中间代理因空闲断开
  socket.onopen = () => {
    keepAlive = window.setInterval(() => {
      socket?.readyState === WebSocket.OPEN && socket.send(JSON.stringify({ type: 'ping' }))
    }, 25000)
  }
  // 断线后定时重连；会话被吊销时直接退出登录
  socket.onclose = (ev) => {
    if (keepAlive) clearInterval(keepAlive)
    if (ev.code === WS_CLOSE_REVOKED) {
      socket = null
      useAuthStore().logout()
      router.push({ name: 'login' })
      return
    }
    if (ev.code === WS_CLOSE_PERM_CHANGED) void refreshAfterPermChange()
    retryTimer = window.setTimeout(() => connectAdminWs(token), 3000)
  }
}

// 主动断开（退出登录时调用），并清理定时器
export function disconnectAdminWs(): void {
  if (retryTimer) clearTimeout(retryTimer)
  if (keepAlive) clearInterval(keepAlive)
  if (socket) {
    socket.onclose = null
    socket.close()
    socket = null
  }
}
