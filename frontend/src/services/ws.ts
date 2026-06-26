import { useHubStore } from '@/stores/hub'

// 后台实时 WebSocket 单连接管理：带断线自动重连与心跳保活
let socket: WebSocket | null = null
let retryTimer: number | undefined
let keepAlive: number | undefined

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
  // 断线后定时重连
  socket.onclose = () => {
    if (keepAlive) clearInterval(keepAlive)
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
