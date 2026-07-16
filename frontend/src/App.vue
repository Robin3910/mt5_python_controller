<script setup lang="ts">
// 根组件：顶部导航 + 路由出口；登录后建立后台实时 WS，退出时断开
import { computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { connectAdminWs, disconnectAdminWs } from '@/services/ws'

const auth = useAuthStore()
const hub = useHubStore()
const router = useRouter()
const authed = computed(() => auth.isAuthed)

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
        <RouterLink to="/">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
          <span>总览</span>
        </RouterLink>
        <RouterLink to="/nodes">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
          <span>节点</span>
        </RouterLink>
        <RouterLink to="/console">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
          <span>中控台</span>
        </RouterLink>
        <RouterLink to="/config">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
          <span>配置</span>
        </RouterLink>
        <RouterLink to="/events">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          <span>事件</span>
        </RouterLink>
      </nav>

      <div class="spacer"></div>

      <div class="topbar-status">
        <span class="secure-badge">
          <span class="secure-dot"></span>
          安全连接
        </span>
        <span class="pill pill-live">
          <span class="dot online"></span>
          在线 {{ hub.onlineCount }}/{{ hub.nodes.length }}
        </span>
      </div>

      <button class="btn-ghost btn-sm btn-logout" @click="logout">退出</button>
    </header>

    <main :class="authed ? 'content' : 'content-full'">
      <RouterView />
    </main>
  </div>
</template>
