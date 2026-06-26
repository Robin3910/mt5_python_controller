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
    <header v-if="authed" class="topbar">
      <div class="brand">⚡ MT5<span>&nbsp;多节点管理</span></div>
      <nav class="nav">
        <RouterLink to="/">总览</RouterLink>
        <RouterLink to="/nodes">节点</RouterLink>
        <RouterLink to="/config">配置</RouterLink>
      </nav>
      <div class="spacer"></div>
      <span class="pill">在线 {{ hub.onlineCount }}/{{ hub.nodes.length }}</span>
      <button class="btn-ghost btn-sm" @click="logout">退出</button>
    </header>
    <main :class="authed ? 'content' : ''">
      <RouterView />
    </main>
  </div>
</template>
