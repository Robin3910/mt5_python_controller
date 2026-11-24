<script setup lang="ts">
// 根组件：顶部导航 + 路由出口；登录后建立后台实时 WS，退出时断开。
// 导航按当前用户的菜单渲染；权限变更后当前页若已无权访问，自动跳到第一个有权限的菜单
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { connectAdminWs, disconnectAdminWs } from '@/services/ws'
import { firstAllowedPath } from '@/constants/menus'
import NewsTopBanner from '@/components/NewsTopBanner.vue'
import { pageChrome } from '@/utils/pageChrome'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const authed = computed(() => auth.isAuthed)

const PAGE_TITLES: Record<string, string> = {
  home: '首页',
  'home-node': '节点主页',
  trading: '交易',
  'ai-trade': 'AI 交易',
  'my-ai-channel': 'AI 频道',
  'my-follow-channel': '我的带单频道',
  'ai-follow-setup': '跟单设置',
  'ai-robot-detail': '机器人详情',
  follow: '带单',
  me: '我的',
  'news-calendar': '财经日历',
  config: '配置',
  nodes: '节点',
  'node-detail': '节点',
  trend: '趋势',
  groups: '分组',
  'group-signals': '分组信号',
  strategies: '策略',
  'user-strategies': '策略',
  events: '事件',
  audits: '审计',
  console: '控制台',
  permissions: '用户权限',
  dashboard: '总览',
  'client-versions': '客户端版本',
}
const pageTitle = computed(() => PAGE_TITLES[String(route.name || '')] || '')
const trendConfigOpen = computed(() => route.name === 'home' && route.query.config === 'trend')
const nodeHomeOpen = computed(() => route.name === 'home-node')
const homeNavActive = computed(() => route.name === 'home' || route.name === 'home-node')
const customerBackTitle = computed(() => {
  if (pageChrome.value?.title) return pageChrome.value.title
  if (trendConfigOpen.value) return '参数配置'
  if (route.name === 'home') return ''
  if (nodeHomeOpen.value) return '节点主页'
  return pageTitle.value
})
function closePageChrome(): void {
  pageChrome.value?.back()
}
function onCustomerBack(): void {
  if (pageChrome.value?.back) {
    pageChrome.value.back()
    return
  }
  router.push({ name: route.name === 'ai-robot-detail' ? 'ai-trade' : ['my-ai-channel', 'my-follow-channel'].includes(String(route.name)) ? 'me' : 'home' })
}

watch(
  () => auth.me,
  (me) => {
    if (me && route.name === 'my-follow-channel' && !auth.canBeFollowLeader) router.replace({ name: 'me' })
    const menu = route.meta.menu
    if (me && menu && !auth.hasMenu(menu)) router.replace(firstAllowedPath(auth.hasMenu))
    // 仅管理员页面（如系统级配置）：普通用户直接跳走
    if (me && route.meta.adminOnly && !auth.isAdmin) router.replace(firstAllowedPath(auth.hasMenu))
  },
)

function setupWs(): void {
  if (auth.token) connectAdminWs(auth.token)
  else disconnectAdminWs()
}

onMounted(setupWs)
watch(() => auth.token, setupWs)
</script>

<template>
  <div class="app-shell" :class="{ 'is-customer': authed && (!auth.isAdmin || ['trading', 'ai_trade', 'follow'].includes(String(route.meta.menu))) }">
    <header v-if="authed" class="topbar topbar-customer">
      <div class="topbar-customer-inner">
        <div class="brand brand-customer">
          <template v-if="route.name === 'ai-trade' || route.name === 'ai-robot-detail' || (route.name === 'follow' && pageChrome)">
            <button type="button" class="brand-back brand-back-ai" :aria-label="route.name === 'follow' ? (pageChrome?.title === '我的跟单' ? '返回带单广场' : '返回上一页') : pageChrome || route.name === 'ai-robot-detail' ? '返回 AI 交易' : '返回首页'" @click="onCustomerBack">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
            </button>
            <p class="brand-page">{{ customerBackTitle }}</p>
          </template>
          <button v-else-if="customerBackTitle" type="button" class="brand-back" :aria-label="customerBackTitle" @click="onCustomerBack">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
            <span>{{ customerBackTitle }}</span>
          </button>
          <template v-else>
            <span class="brand-mark" aria-hidden="true">T</span>
            <p v-if="pageTitle" class="brand-page">{{ pageTitle }}</p>
          </template>
        </div>
        <nav class="nav nav-customer" aria-label="顶部导航">
          <RouterLink v-if="auth.hasMenu('home')" to="/home" :class="{ 'router-link-active': homeNavActive }">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
            <span>首页</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('trading')" to="/trading">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>
            <span>交易</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('ai_trade')" to="/ai-trade" :class="{ 'router-link-active': route.name === 'ai-robot-detail' || route.name === 'ai-follow-setup' }">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zM7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5zM16 17H8v-2h8v2zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13z"/></svg>
            <span>AI交易</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('follow')" to="/follow" @click="closePageChrome">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
            <span>带单</span>
          </RouterLink>
          <RouterLink to="/me" :class="{ 'router-link-active': ['my-ai-channel', 'my-follow-channel'].includes(String(route.name)) }">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <span>我的</span>
          </RouterLink>
        </nav>
      </div>
    </header>

    <!-- 财经日历黑名单提示条：当天有高影响数据公布时在最上方滚动警示。
         放在 header 之后、main 之前 —— 它是全站横幅，不属于任何一个页面。 -->
    <NewsTopBanner v-if="authed" />

    <main :class="authed ? 'content' : 'content-full'">
      <RouterView />
    </main>
  </div>
</template>
