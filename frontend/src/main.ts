import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { AUTH_EXPIRED_EVENT } from './api/client'
import { useAuthStore } from './stores/auth'
import './styles.css'

// 应用入口：Pinia 必须在 router 之前装载（路由守卫里会用到 store）
const pinia = createPinia()
createApp(App).use(pinia).use(router).mount('#app')

// 登录态失效（401）→ 用 vue-router 软路由跳回登录页。
//
// 为什么不用 location.href：那是整页硬导航。在 iOS 主屏独立应用里，
// 硬导航会被系统踢回 Safari —— 浏览器地址栏与底部工具栏随即出现，
// 「独立应用」的观感就没了。pushState 不会离开独立应用容器。
window.addEventListener(AUTH_EXPIRED_EVENT, () => {
  // 必须同时清掉 Pinia 里的内存态：路由守卫只看 store.token，
  // 只清 localStorage 的话守卫会认为「仍处于登录态」而把 /login 又弹回首页，
  // 于是页面停在首页假装还登录着。
  useAuthStore().logout()
  if (router.currentRoute.value.name !== 'login') {
    void router.push({ name: 'login' })
  }
})
