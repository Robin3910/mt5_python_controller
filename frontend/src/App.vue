<script setup lang="ts">
// 根组件：顶部导航 + 路由出口；登录后建立后台实时 WS，退出时断开。
// 导航按当前用户的菜单渲染；权限变更后当前页若已无权访问，自动跳到第一个有权限的菜单
import { computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { connectAdminWs, disconnectAdminWs } from '@/services/ws'
import { copyToClipboard } from '@/utils/clipboard'
import { firstAllowedPath } from '@/constants/menus'

const auth = useAuthStore()
const hub = useHubStore()
const router = useRouter()
const route = useRoute()
const authed = computed(() => auth.isAuthed)
const userLabel = computed(() => auth.me?.display_name || auth.me?.username || '')

const PAGE_TITLES: Record<string, string> = {
  home: '首页',
  trading: '交易',
  'ai-trade': 'AI交易',
  follow: '带单',
  me: '我的',
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
function closeTrendConfig(): void {
  router.push({ name: 'home' })
}

watch(
  () => auth.me,
  (me) => {
    const menu = route.meta.menu
    if (me && menu && !auth.hasMenu(menu)) router.replace(firstAllowedPath(auth.hasMenu))
  },
)

/** 当前站点同源的 webhook 请求地址，供 TradingView 等外部系统 POST。 */
const webhookUrl = computed(() => {
  if (typeof window === 'undefined') return '/webhook'
  return new URL('/webhook', window.location.origin).href
})

async function copyWebhookUrl(): Promise<void> {
  const ok = await copyToClipboard(webhookUrl.value)
  if (ok) ElMessage.success('复制成功')
  else ElMessage.error('复制失败')
}

function setupWs(): void {
  if (auth.token) connectAdminWs(auth.token)
  else disconnectAdminWs()
}

onMounted(setupWs)
watch(() => auth.token, setupWs)
</script>

<template>
  <div class="app-shell" :class="{ 'is-customer': authed && !auth.isAdmin }">
    <header v-if="authed && !auth.isAdmin" class="topbar topbar-customer">
      <div class="topbar-customer-inner">
        <div class="brand brand-customer">
          <button v-if="trendConfigOpen" type="button" class="brand-back" @click="closeTrendConfig">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
            <span>参数配置</span>
          </button>
          <template v-else>
            <span class="brand-mark" aria-hidden="true">T</span>
            <p v-if="pageTitle" class="brand-page">{{ pageTitle }}</p>
          </template>
        </div>
        <nav class="nav nav-customer" aria-label="顶部导航">
          <RouterLink v-if="auth.hasMenu('home')" to="/home">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
            <span>首页</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('trading')" to="/trading">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6.99 11 3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z"/></svg>
            <span>交易</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('ai_trade')" to="/ai-trade">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zM7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5zM16 17H8v-2h8v2zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13z"/></svg>
            <span>AI交易</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('follow')" to="/follow">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
            <span>带单</span>
          </RouterLink>
          <RouterLink to="/me">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <span>我的</span>
          </RouterLink>
        </nav>
      </div>
    </header>

    <header v-else-if="authed" class="topbar glass-bar">
      <div class="brand">
        <span class="brand-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke-linejoin="round" />
          </svg>
        </span>
        <span class="brand-text">亿点点<span class="brand-sub"></span></span>
      </div>

      <nav class="nav">
        <!-- 管理员：仅显示总览、节点、分组管理、策略管理、带单、用户权限、我的 -->
        <template v-if="auth.isAdmin">
          <RouterLink v-if="auth.hasMenu('dashboard')" to="/">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
            <span>总览</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('nodes')" to="/nodes">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
            <span>节点</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('groups')" to="/groups">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            <span>分组管理</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('strategies')" to="/strategies">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>
            <span>策略管理</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('follow')" to="/follow">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            <span>带单</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('ai_trade')" to="/ai-trade">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4"/><circle cx="12" cy="3" r="1.5"/><path d="M9 14h.01M15 14h.01M9.5 17h5"/></svg>
            <span>AI 交易</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('permissions')" to="/permissions">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><circle cx="12" cy="10" r="2.5"/><path d="M8.5 16a3.5 3.5 0 0 1 7 0"/></svg>
            <span>用户权限</span>
          </RouterLink>
          <RouterLink to="/me">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            <span>我的</span>
          </RouterLink>
        </template>
      </nav>

      <div class="spacer"></div>

      <div class="topbar-status">
        <button
          v-if="auth.isAdmin"
          type="button"
          class="secure-badge webhook-copy"
          :title="`点击复制 ${webhookUrl}`"
          :aria-label="`复制 webhook 请求地址 ${webhookUrl}`"
          @click="copyWebhookUrl"
        >
          <span class="secure-dot" aria-hidden="true"></span>
          <span class="webhook-copy-text">webhook地址</span>
        </button>
        <span class="pill pill-live">
          <span class="dot online"></span>
          在线 {{ hub.onlineCount }}/{{ hub.nodes.length }}
        </span>
      </div>

      <span v-if="userLabel" class="muted topbar-user" :title="auth.isAdmin ? '超级管理员' : '普通用户'">
        {{ userLabel }}
      </span>
    </header>

    <main :class="authed ? 'content' : 'content-full'">
      <RouterView />
    </main>
  </div>
</template>
