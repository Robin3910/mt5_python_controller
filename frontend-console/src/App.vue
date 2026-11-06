<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { api, subscribe, type Snapshot, type DetailPayload, type ConfigStatus } from './api/bridge'
import LoginView from './components/LoginView.vue'
import ConsoleShell from './components/ConsoleShell.vue'

const snapshot = ref<Snapshot | null>(null)
const detail = ref<DetailPayload | null>(null)
const selectedId = ref<string>('')
const lockReason = ref<string>('')
const connectionError = ref<string>('')
/**
 * 动作失败提示（启动 / 停止 / 移除 …）。
 *
 * 单独一个 ref，不能复用 connectionError：SSE 每次推快照都会走 applySnapshot，
 * 而 applySnapshot 会清掉 connectionError —— 动作失败后紧接着的 refresh()
 * 会把刚写进去的错误消息抹掉，用户只看到「点了没反应」。
 */
const actionError = ref<string>('')
const busyLabel = ref<string>('')
const config = ref<ConfigStatus | null>(null)

let unsubscribe: (() => void) | null = null

const authenticated = computed(() => snapshot.value?.authenticated === true)
const nodes = computed(() => snapshot.value?.nodes ?? [])
const hero = computed(() => snapshot.value?.hero ?? null)

function applySnapshot(next: Snapshot): void {
  snapshot.value = next
  if (next.type === 'locked' || !next.authenticated) {
    lockReason.value = next.reason ?? ''
    detail.value = null
    selectedId.value = ''
    return
  }
  lockReason.value = ''
  connectionError.value = ''

  // 选中的实例被移除时清掉选中态，避免详情面板指向不存在的实例。
  if (selectedId.value && !nodes.value.some((n) => n.id === selectedId.value)) {
    selectedId.value = ''
    detail.value = null
  }
}

async function loadDetail(id: string): Promise<void> {
  if (!id) {
    detail.value = null
    return
  }
  try {
    detail.value = await api.detail(id)
  } catch (exc) {
    detail.value = null
    console.error('[detail]', exc)
  }
}

async function select(id: string): Promise<void> {
  selectedId.value = id
  await loadDetail(id)
}

async function refresh(): Promise<void> {
  try {
    const next = await api.state()
    applySnapshot(next)
  } catch (exc) {
    connectionError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

async function login(username: string, password: string, code: string): Promise<string> {
  const result = await api.login(username, password, code)
  if (result.requires_2fa) {
    return result.error || '请输入 2FA 验证码'
  }
  await refresh()
  return ''
}

async function logout(): Promise<void> {
  await api.logout()
  await refresh()
}

/** 执行一个实例动作，成功后刷新状态。 */
async function runAction(action: string, id: string): Promise<void> {
  busyLabel.value = action
  actionError.value = ''
  try {
    await api.action(action, id)
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyLabel.value = ''
    await refresh()
    if (selectedId.value) await loadDetail(selectedId.value)
  }
}

async function runBatch(action: string): Promise<void> {
  busyLabel.value = action
  actionError.value = ''
  try {
    await api.batch(action)
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyLabel.value = ''
    await refresh()
  }
}

onMounted(async () => {
  await refresh()
  try {
    config.value = await api.config()
  } catch (exc) {
    console.error('[config]', exc)
  }
  unsubscribe = subscribe(applySnapshot)
})

onUnmounted(() => {
  unsubscribe?.()
})
</script>

<template>
  <LoginView
    v-if="!authenticated"
    :reason="lockReason"
    :error="connectionError"
    :on-login="login"
    :config="config"
  />

  <ConsoleShell
    v-else
    :hero="hero"
    :nodes="nodes"
    :username="snapshot?.username ?? ''"
    :is-admin="snapshot?.is_admin ?? false"
    :selected-id="selectedId"
    :detail="detail"
    :busy-label="busyLabel"
    :connection-error="connectionError"
    :action-error="actionError"
    :update-badge="snapshot?.update"
    @select="select"
    @action="runAction"
    @batch="runBatch"
    @refresh="refresh"
    @logout="logout"
  />
</template>
