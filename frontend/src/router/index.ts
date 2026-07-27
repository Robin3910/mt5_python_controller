import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    { path: '/', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
    { path: '/nodes', name: 'nodes', component: () => import('@/views/NodesView.vue') },
    { path: '/nodes/:id', name: 'node-detail', component: () => import('@/views/NodeDetailView.vue') },
    { path: '/events', name: 'events', component: () => import('@/views/EventsView.vue') },
    { path: '/audits', name: 'audits', component: () => import('@/views/AuditView.vue') },
    { path: '/console', name: 'console', component: () => import('@/views/ConsoleView.vue') },
    { path: '/config', name: 'config', component: () => import('@/views/ConfigView.vue') },
  ],
})

// 全局守卫：未登录只能进 /login；已登录访问 /login 时跳回总览
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isAuthed) return { name: 'login' }
  if (to.name === 'login' && auth.isAuthed) return { name: 'dashboard' }
  return true
})

export default router
