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
    // 注册页是公开路由（不加 meta.menu）；已登录访问它会被守卫弹回首页
    { path: '/register', name: 'register', component: () => import('@/views/RegisterView.vue') },
    {
      path: '/home',
      name: 'home',
      component: () => import('@/views/HomeView.vue'),
      meta: { menu: 'home' },
    },
    {
      path: '/home/nodes/:id',
      name: 'home-node',
      component: () => import('@/views/HomeNodeView.vue'),
      meta: { menu: 'home' },
    },
    {
      path: '/trading',
      name: 'trading',
      component: () => import('@/views/TradingView.vue'),
      meta: { menu: 'trading' },
    },
    {
      path: '/follow',
      name: 'follow',
      component: () => import('@/views/FollowView.vue'),
      meta: { menu: 'follow' },
    },
    {
      path: '/ai-trade',
      name: 'ai-trade',
      component: () => import('@/views/AiTradeView.vue'),
      meta: { menu: 'ai_trade' },
    },
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
      component: () => import('@/views/SignalCenterView.vue'),
      meta: { menu: 'events' },
    },
    {
      // 「信号中心」别名路径：老链接 /events 继续有效，新入口用更语义化的路径
      path: '/signal-center',
      name: 'signal-center',
      component: () => import('@/views/SignalCenterView.vue'),
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
    // 「我的」枢纽页：账号卡片 + 模块入口
    { path: '/me', name: 'me', component: () => import('@/views/MineView.vue') },
    // 「个人中心」：个人信息 / 带单员 / 修改密码 / 2FA（登录即可进，不限菜单）
    { path: '/profile', name: 'profile', component: () => import('@/views/ProfileView.vue') },
    {
      path: '/user-strategies',
      name: 'user-strategies',
      component: () => import('@/views/UserStrategiesView.vue'),
      meta: { menu: 'strategies' },
    },
  ],
})

// 全局守卫：未登录只能进 /login / register；已登录先拉当前用户，再按菜单权限放行，
// 无权限时跳到第一个有权限的菜单（都没有则到配置页）
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const isPublic = to.name === 'login' || to.name === 'register'
  if (!isPublic && !auth.isAuthed) return { name: 'login' }
  // 已登录不该再看到登录 / 注册页
  if (isPublic && auth.isAuthed) return { name: 'home' }
  if (to.name === 'login' || to.name === 'register') return true
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
