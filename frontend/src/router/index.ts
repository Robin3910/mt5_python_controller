import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { firstAllowedPath } from '@/constants/menus'

declare module 'vue-router' {
  interface RouteMeta {
    /** 进入该页需要的菜单 code（与后端菜单注册表一致）；不填表示登录即可 */
    menu?: string
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    {
      path: '/',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { menu: 'dashboard' },
    },
    {
      path: '/nodes',
      name: 'nodes',
      component: () => import('@/views/NodesView.vue'),
      meta: { menu: 'nodes' },
    },
    {
      path: '/nodes/:id',
      name: 'node-detail',
      component: () => import('@/views/NodeDetailView.vue'),
      meta: { menu: 'nodes' },
    },
    {
      path: '/trend',
      name: 'trend',
      component: () => import('@/views/TrendView.vue'),
      meta: { menu: 'trend' },
    },
    {
      path: '/groups',
      name: 'groups',
      component: () => import('@/views/GroupsView.vue'),
      meta: { menu: 'groups' },
    },
    {
      path: '/groups/:id/signals',
      name: 'group-signals',
      component: () => import('@/views/GroupSignalsView.vue'),
      meta: { menu: 'groups' },
    },
    {
      path: '/strategies',
      name: 'strategies',
      component: () => import('@/views/StrategiesView.vue'),
      meta: { menu: 'strategies' },
    },
    {
      path: '/client-versions',
      name: 'client-versions',
      component: () => import('@/views/ClientVersionsView.vue'),
      meta: { menu: 'client_versions' },
    },
    {
      path: '/events',
      name: 'events',
      component: () => import('@/views/EventsView.vue'),
      meta: { menu: 'events' },
    },
    {
      path: '/audits',
      name: 'audits',
      component: () => import('@/views/AuditView.vue'),
      meta: { menu: 'audits' },
    },
    {
      path: '/console',
      name: 'console',
      component: () => import('@/views/ConsoleView.vue'),
      meta: { menu: 'console' },
    },
    {
      path: '/permissions',
      name: 'permissions',
      component: () => import('@/views/PermissionView.vue'),
      meta: { menu: 'permissions' },
    },
    { path: '/config', name: 'config', component: () => import('@/views/ConfigView.vue') },
  ],
})

// 全局守卫：未登录只能进 /login；已登录先拉当前用户，再按菜单权限放行，
// 无权限时跳到第一个有权限的菜单（都没有则到配置页）
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isAuthed) return { name: 'login' }
  if (to.name === 'login') return auth.isAuthed ? { name: 'dashboard' } : true
  if (!auth.me) {
    try {
      await auth.fetchMe()
    } catch {
      auth.logout()
      return { name: 'login' }
    }
  }
  if (to.meta.menu && !auth.hasMenu(to.meta.menu)) {
    const fallback = firstAllowedPath(auth.hasMenu)
    return fallback === to.path ? true : fallback
  }
  return true
})

export default router
