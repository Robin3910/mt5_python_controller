import { createApp } from 'vue'
import App from './App.vue'
import './styles/reference.css'
import { installErrorCollector, installMountWatchdog } from './api/errors'

// 错误采集必须最先装：挂载失败也要能把原因送出去。
installErrorCollector()

const app = createApp(App)

app.config.errorHandler = (err, _instance, info) => {
  // Vue 内部捕获到的异常（组件渲染/事件处理里抛的），
  // window.onerror 收不到，得单独兜住。
  console.error('[vue]', info, err)
  window.dispatchEvent(
    new ErrorEvent('error', {
      message: err instanceof Error ? err.message : String(err),
      error: err instanceof Error ? err : undefined,
    }),
  )
}

app.mount('#container')

// 挂载后盯着 #container：一直是空的就是白屏，留证据。
installMountWatchdog()
