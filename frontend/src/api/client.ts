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

export default api
