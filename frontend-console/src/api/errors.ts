/**
 * 前端脚本错误采集：把 window.onerror / unhandledrejection 收集起来回传给
 * 本机桥，落盘到 `logs/dashboard.log`。
 *
 * 为什么必须有这个
 * ----------------
 * 打包后的面板是 pywebview 窗口，用户按不出 F12，也看不到开发者工具。
 * 前端 JS 一旦崩了（Vue 没挂上），用户看到的就是**一片空白**，
 * 而任何错误信息都留在了看不见的地方，只能靠猜。
 *
 * 采集后在日志里能直接看到「哪个文件第几行报的什么错」。
 */
import { sessionToken } from './bridge'

export interface ClientLogEntry {
  level: 'error' | 'warn' | 'info'
  message: string
  source?: string
  line?: number
  column?: number
  stack?: string
  time: string
}

const entries: ClientLogEntry[] = []
const MAX_ENTRIES = 200
let installed = false
let flushTimer: number | null = null

function push(entry: ClientLogEntry): void {
  entries.push(entry)
  if (entries.length > MAX_ENTRIES) {
    entries.splice(0, entries.length - MAX_ENTRIES)
  }
  scheduleFlush()
}

/** 攒一小会儿再一起发，避免每个错误都打一次 HTTP。 */
function scheduleFlush(): void {
  if (flushTimer !== null) return
  flushTimer = window.setTimeout(() => {
    flushTimer = null
    void flush()
  }, 1500)
}

async function flush(): Promise<void> {
  if (!entries.length) return
  const batch = entries.splice(0, entries.length)
  try {
    const token = sessionToken()
    await fetch(`/api/client-log?token=${encodeURIComponent(token)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: batch }),
    })
  } catch {
    // 回传失败也不能再抛 —— 这段代码本身报错会让排查更困难。
    // 失败时把内容放回队列，等下一次窗口。
    entries.unshift(...batch)
    if (entries.length > MAX_ENTRIES) {
      entries.splice(MAX_ENTRIES)
    }
  }
}

function describe(error: unknown): { message: string; stack?: string } {
  if (error instanceof Error) {
    return { message: error.message || String(error), stack: error.stack ?? '' }
  }
  if (typeof error === 'string') {
    return { message: error }
  }
  try {
    return { message: JSON.stringify(error) }
  } catch {
    return { message: String(error) }
  }
}

/**
 * 安装错误采集。应在应用入口最早期调用。
 *
 * 采集三类信息：
 *  1. `window.onerror` —— 同步异常，含文件/行/列；
 *  2. `unhandledrejection` —— 未处理的 Promise 拒绝；
 *  3. Vue 挂载失败兜底 —— 挂不上就是白屏，必须留下证据。
 */
export function installErrorCollector(): void {
  if (installed) return
  installed = true

  window.addEventListener('error', (event: ErrorEvent) => {
    push({
      level: 'error',
      message: event.message || '未知脚本错误',
      source: event.filename || '',
      line: event.lineno || 0,
      column: event.colno || 0,
      stack: event.error instanceof Error ? event.error.stack ?? '' : '',
      time: new Date().toISOString(),
    })
  })

  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    const info = describe(event.reason)
    push({
      level: 'error',
      message: `未处理的 Promise 拒绝：${info.message}`,
      stack: info.stack ?? '',
      time: new Date().toISOString(),
    })
  })

  // 页面卸载前尽力把剩下的错误送出去。
  window.addEventListener('beforeunload', () => {
    if (entries.length) {
      const token = sessionToken()
      const body = JSON.stringify({ entries: entries.splice(0) })
      // 卸载阶段不能用 await，只能用 keepalive 同步发出。
      void fetch(`/api/client-log?token=${encodeURIComponent(token)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      }).catch(() => undefined)
    }
  })
}

/**
 * 挂载兜底：Vue 没挂上就是白屏。
 *
 * 轮询一小会儿，确认 `#container` 里确实有内容；没有就把「白屏」这件事
 * 连同当前页面状态一起回传 —— 这是最直接的证据。
 */
export function installMountWatchdog(): void {
  const started = Date.now()
  const timer = window.setInterval(() => {
    const container = document.querySelector('#container')
    const children = container ? container.children.length : 0
    if (children > 0) {
      window.clearInterval(timer)
      return
    }
    // 超过 8 秒仍是空的 —— 白屏确认。
    if (Date.now() - started > 8000) {
      window.clearInterval(timer)
      push({
        level: 'error',
        message:
          '前端挂载失败：#container 一直是空的，界面表现为空白。' +
          '请把本段日志发回以便定位。',
        source: location.href,
        time: new Date().toISOString(),
      })
      void flush()
    }
  }, 1000)
}

/** 取当前已采集的错误（设置面板展示用）。 */
export function collectedEntries(): ClientLogEntry[] {
  return [...entries]
}
