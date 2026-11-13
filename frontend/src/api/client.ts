import axios from 'axios'

// 全局 axios 实例：同源部署时 baseURL 留空，由 nginx 把 /api 转发到后端
const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 15000,
})

// 请求拦截器：自动带上登录后保存的 JWT
api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('token')
  if (token) {
    ;(cfg.headers as Record<string, string>).Authorization = `Bearer ${token}`
  }
  return cfg
})

// 响应拦截器：遇到 401（令牌失效）则清除并跳回登录页
api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error?.response?.status === 401) {
      localStorage.removeItem('token')
      if (location.pathname !== '/login') location.href = '/login'
    }
    return Promise.reject(error)
  },
)

/**
 * 从 axios 错误里取出后端给的中文提示。
 *
 * 后端有两种报错形态：
 * - FastAPI 的 `HTTPException(detail="文字")` → `detail` 是字符串；
 * - `HTTPException(detail={"message": "...", ...})` → `detail` 是对象（路由冲突会带 task_ids）。
 *
 * 都取不到时回落到调用方给的兜底文案 —— **不要把 axios 的英文报错透给用户**。
 */
export function extractApiError(error: unknown, fallback = '操作失败'): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
    ?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (detail && typeof detail === 'object') {
    const msg = (detail as { message?: unknown }).message
    if (typeof msg === 'string' && msg.trim()) return msg.trim()
  }
  const raw = (error as { response?: { data?: unknown } })?.response?.data
  if (typeof raw === 'string' && raw.trim()) return raw.trim()
  return fallback
}

export default api
