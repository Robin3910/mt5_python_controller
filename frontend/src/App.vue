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

function logout(): void {
  disconnectAdminWs()
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <div class="bg-mesh" aria-hidden="true">
      <div class="bg-orb bg-orb-1"></div>
      <div class="bg-orb bg-orb-2"></div>
      <div class="bg-orb bg-orb-3"></div>
      <div class="bg-grid"></div>
    </div>

    <header v-if="authed" class="topbar glass-bar">
      <div class="brand">
        <span class="brand-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke-linejoin="round" />
          </svg>
        </span>
        <span class="brand-text">MT5<span class="brand-sub">&nbsp;云端集控中枢</span></span>
      </div>

      <nav class="nav">
        <RouterLink v-if="!auth.isAdmin && auth.hasMenu('home')" to="/home">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 11l9-8 9 8"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/></svg>
          <span>首页</span>
        </RouterLink>
        <RouterLink v-if="!auth.isAdmin && auth.hasMenu('trading')" to="/trading">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>
          <span>交易</span>
        </RouterLink>
        <!-- 普通用户：其他菜单全收进「我的」枢纽页，仅保留首页/交易/我的三个入口 -->
        <template v-if="!auth.isAdmin">
          <RouterLink to="/me">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            <span>我的</span>
          </RouterLink>
        </template>
        <!-- 管理员：仅显示总览、节点、用户权限、我的 -->
        <template v-else>
          <RouterLink v-if="auth.hasMenu('dashboard')" to="/">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
            <span>总览</span>
          </RouterLink>
          <RouterLink v-if="auth.hasMenu('nodes')" to="/nodes">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
            <span>节点</span>
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
      <button class="btn-ghost btn-sm btn-logout" @click="logout">退出</button>
    </header>

    <main :class="authed ? 'content' : 'content-full'">
      <RouterView />
    </main>
  </div>
</template>
