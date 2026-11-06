<script setup lang="ts">
import { computed, ref } from 'vue'
import type { HeroStats, NodeItem, DetailPayload, UpdateBadge } from '../api/bridge'
import AppModal from './AppModal.vue'
import ImportModal from './ImportModal.vue'
import SettingsModal from './SettingsModal.vue'
import VersionUpdateModal from './VersionUpdateModal.vue'

const props = defineProps<{
  hero: HeroStats | null
  nodes: NodeItem[]
  username: string
  isAdmin: boolean
  selectedId: string
  detail: DetailPayload | null
  busyLabel: string
  connectionError: string
  /** 动作失败提示（启动 / 停止 / 移除 …）；空串表示没有错误。 */
  actionError: string
  /** 版本更新徽标：后端发布版本 + 本机待更新实例数。 */
  updateBadge?: UpdateBadge
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'action', action: string, id: string): void
  (e: 'batch', action: string): void
  (e: 'refresh'): void
  (e: 'logout'): void
}>()

const clock = computed(() =>
  new Date().toLocaleTimeString('zh-CN', { hour12: false }),
)

/** 分布条的各段百分比。总数为 0 时全部为 0。 */
const dist = computed(() => {
  const h = props.hero
  if (!h || h.total <= 0) return []
  const seg = (n: number) => `${(n / h.total) * 100}%`
  return [
    { cls: 'online', width: seg(h.online) },
    { cls: 'starting', width: seg(h.starting) },
    { cls: 'paused', width: seg(h.pending) },
    { cls: 'offline', width: seg(h.stopped) },
  ].filter((s) => s.width !== '0%')
})

const chips = computed(() => {
  const h = props.hero
  if (!h) return []
  return [
    { cls: 'online', text: `在线 ${h.online}` },
    { cls: 'starting', text: `启动中 ${h.starting}` },
    { cls: 'paused', text: `待审核 ${h.pending}` },
    { cls: 'offline', text: `已停止 ${h.stopped}` },
  ]
})

function initials(name: string): string {
  const trimmed = (name || '').trim()
  if (!trimmed) return 'U'
  return trimmed.slice(0, 1).toUpperCase()
}

/**
 * 平均延迟文案：没有样本时显示 --，不要显示 0（0 会被误读成「零延迟」）。
 * 小于 1ms 时保留一位小数，否则四舍五入到整毫秒。
 */
const latencyText = computed(() => {
  const ms = props.hero?.avg_latency_ms
  if (ms === null || ms === undefined) return '--'
  if (ms < 1) return ms.toFixed(1)
  return String(Math.round(ms))
})

const importOpen = ref(false)
const versionOpen = ref(false)
const settingsOpen = ref(false)

/** 顶栏「版本更新」按钮：有待更新实例时高亮并显示数量。 */
const pendingUpdates = computed(() => props.updateBadge?.pending ?? 0)
const publishedVersion = computed(() => props.updateBadge?.published ?? '')

/** 待移除的节点（垃圾桶按钮点开后置入，确认后才真正移除）。 */
const removeTarget = ref<NodeItem | null>(null)

function askRemove(node: NodeItem): void {
  removeTarget.value = node
}

function confirmRemove(): void {
  const target = removeTarget.value
  removeTarget.value = null
  if (!target) return
  emit('action', 'remove', target.id)
}
</script>

<template>
  <div class="console-root">
    <header class="console-header">
      <div class="header-brand">
        <div class="header-logo">
          <div class="header-logo-inner">亿</div>
        </div>
        <span class="header-name">亿点点交易</span>
        <span class="header-divider" />
        <span class="header-module">节点控制台</span>
      </div>

      <div class="header-right">
        <span class="header-clock mono">{{ clock }}</span>
        <span class="header-user" title="账号">
          <span class="avatar">{{ initials(username) }}</span>
          {{ username || '未登录' }}
        </span>
        <button
          class="header-icon-btn"
          :class="{ on: busyLabel !== '' }"
          title="刷新"
          @click="emit('refresh')"
        >
          ↻
        </button>
        <button
          class="header-btn version-btn"
          :class="{ on: pendingUpdates > 0 }"
          :title="
            pendingUpdates > 0
              ? `有 ${pendingUpdates} 个实例可更新到 v${publishedVersion}`
              : '客户端版本更新'
          "
          @click="versionOpen = true"
        >
          <span class="version-btn-icon">↥</span>
          <span class="version-btn-text">版本更新</span>
          <span v-if="pendingUpdates > 0" class="version-badge">
            {{ pendingUpdates }}
          </span>
        </button>
        <button
          class="header-icon-btn"
          title="系统设置"
          @click="settingsOpen = true"
        >
          ⚙
        </button>
        <button class="btn-sm logout" @click="emit('logout')">退出登录</button>
      </div>
    </header>

    <main class="console-main">
      <div class="panel-stack">
        <p v-if="connectionError" class="console-error">{{ connectionError }}</p>
        <p v-if="actionError" class="console-error">{{ actionError }}</p>

        <!-- ── Hero 统计 ── -->
        <section v-if="hero" class="stat-hero">
          <div class="stat-hero-bg" />
          <div class="stat-hero-main">
            <div class="stat-hero-head">
              <div class="stat-hero-title">
                <div class="stat-icon hero gold">▤</div>
                <div>
                  <p class="stat-value hero">{{ hero.total }}</p>
                  <p class="stat-label hero">节点总数</p>
                </div>
              </div>
              <div class="stat-status-chips">
                <span
                  v-for="chip in chips"
                  :key="chip.cls"
                  class="stat-chip"
                  :class="chip.cls"
                >
                  <i />{{ chip.text }}
                </span>
              </div>
            </div>

            <div class="stat-dist-bar">
              <span
                v-for="seg in dist"
                :key="seg.cls"
                class="dist"
                :class="seg.cls"
                :style="{ width: seg.width }"
              />
            </div>

            <div class="stat-hero-metrics">
              <div class="stat-metric">
                <span class="stat-metric-icon violet">
                  <svg
                    xmlns="http://www.w3.org/2000/svg" width="14" height="14"
                    viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <ellipse cx="12" cy="5" rx="9" ry="3" />
                    <path d="M3 5V19A9 3 0 0 0 21 19V5" />
                    <path d="M3 12A9 3 0 0 0 21 12" />
                  </svg>
                </span>
                <div>
                  <p class="stat-metric-value">{{ hero.today_signals }}</p>
                  <p class="stat-metric-label">今日信号</p>
                </div>
              </div>
              <div class="stat-metric">
                <span class="stat-metric-icon blue">
                  <svg
                    xmlns="http://www.w3.org/2000/svg" width="14" height="14"
                    viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="m12 14 4-4" />
                    <path d="M3.34 19a10 10 0 1 1 17.32 0" />
                  </svg>
                </span>
                <div>
                  <p class="stat-metric-value">
                    {{ latencyText }}<small v-if="hero.avg_latency_ms !== null && hero.avg_latency_ms !== undefined">ms</small>
                  </p>
                  <p class="stat-metric-label">平均延迟</p>
                </div>
              </div>
              <div class="stat-metric">
                <span class="stat-metric-icon red">
                  <svg
                    xmlns="http://www.w3.org/2000/svg" width="14" height="14"
                    viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
                    <path d="M12 8v4" />
                    <path d="M12 16h.01" />
                  </svg>
                </span>
                <div>
                  <p class="stat-metric-value">{{ hero.alerts }}</p>
                  <p class="stat-metric-label">异常提醒</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- ── 节点区 ── -->
        <div class="section-head">
          <h2>MT5 节点</h2>
          <div class="section-actions">
            <button
              class="btn-sm primary"
              title="启动所有未运行的节点"
              :disabled="busyLabel !== ''"
              @click="emit('batch', 'start_all')"
            >
              ▶ 全部启动
            </button>
            <button
              class="btn-sm amber"
              title="停止所有在线节点"
              :disabled="busyLabel !== ''"
              @click="emit('batch', 'stop_all')"
            >
              ■ 全部停止
            </button>
            <button class="btn-sm primary" title="导入新节点" @click="importOpen = true">
              ＋ 添加节点
            </button>
          </div>
        </div>

        <!-- ── 节点卡片网格 ── -->
        <div class="node-grid">
          <div
            v-for="node in nodes"
            :key="node.id"
            class="node-card"
            :class="node.badge_tone"
            :data-selected="node.id === selectedId"
            @click="emit('select', node.id)"
          >
            <div class="node-card-head">
              <div class="node-title">
                <span
                  class="status-dot"
                  :class="node.alive ? 'online' : 'offline'"
                />
                <span class="node-name">{{ node.title }}</span>
              </div>
              <div class="node-head-right">
                <span class="badge" :class="node.badge_tone">{{ node.badge_text }}</span>
              </div>
            </div>

            <div class="node-card-meta">
              <span class="meta-server">
                <svg
                  xmlns="http://www.w3.org/2000/svg" width="13" height="13"
                  viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <rect width="20" height="8" x="2" y="2" rx="2" ry="2" />
                  <rect width="20" height="8" x="2" y="14" rx="2" ry="2" />
                  <line x1="6" x2="6.01" y1="6" y2="6" />
                  <line x1="6" x2="6.01" y1="18" y2="18" />
                </svg>
                <span class="meta-text">{{ node.server_label || '服务器 -' }}</span>
              </span>
              <span class="mt5-id-inline mono">
                <svg
                  xmlns="http://www.w3.org/2000/svg" width="12" height="12"
                  viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4" />
                  <path d="M14 13.12c0 2.38 0 6.38-1 8.88" />
                  <path d="M17.29 21.02c.12-.6.43-2.3.5-3.02" />
                  <path d="M2 12a10 10 0 0 1 18-6" />
                  <path d="M2 16h.01" />
                  <path d="M21.8 16c.2-2 .131-5.354 0-6" />
                  <path d="M5 19.5C5.5 18 6 15 6 12a6 6 0 0 1 .34-2" />
                  <path d="M8.65 22c.21-.66.45-1.32.57-2" />
                  <path d="M9 6.8a6 6 0 0 1 9 5.2v2" />
                </svg>
                <span class="mt5-id-inline-label">ID</span>{{ node.account_label || '-' }}
              </span>
            </div>

            <div class="node-info-grid">
              <div class="info-item">
                <span class="info-label-row">
                  <span class="status-ico" :class="node.mt5_connected ? 'ok' : 'no'">
                    {{ node.mt5_connected ? '✓' : '✕' }}
                  </span>
                  <span class="info-label">连接MT5</span>
                </span>
                <span class="info-value" :class="node.mt5_connected ? 'ok' : 'no'">
                  {{ node.mt5_connected ? '已连接' : '未连接' }}
                </span>
              </div>
              <div class="info-item">
                <span class="info-label-row">
                  <span class="status-ico" :class="node.mt5_authenticated ? 'ok' : 'no'">
                    {{ node.mt5_authenticated ? '✓' : '✕' }}
                  </span>
                  <span class="info-label">是否鉴权</span>
                </span>
                <span class="info-value" :class="node.mt5_authenticated ? 'ok' : 'no'">
                  {{ node.mt5_authenticated ? '已鉴权' : '未鉴权' }}
                </span>
              </div>
              <div class="info-item">
                <span class="info-label-row">
                  <span class="status-ico" :class="node.algo_trading_ok ? 'ok' : 'no'">
                    {{ node.algo_trading_ok ? '✓' : '✕' }}
                  </span>
                  <span class="info-label">算法交易</span>
                </span>
                <span class="info-value" :class="node.algo_trading_ok ? 'ok' : 'no'">
                  {{ node.algo_trading_known ? (node.algo_trading_ok ? '已关闭' : '已开启') : '未知' }}
                </span>
              </div>
            </div>

            <div class="node-card-foot">
              <span class="node-uptime">◷ {{ node.uptime_text }}</span>
              <div class="row-actions">
                <button
                  class="btn-icon danger"
                  :title="`删除节点 ${node.title}`"
                  :aria-label="`删除节点 ${node.title}`"
                  @click.stop="askRemove(node)"
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg" width="13" height="13"
                    viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M10 11v6" />
                    <path d="M14 11v6" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                    <path d="M3 6h18" />
                    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
                <button
                  v-if="node.badge_tone === 'starting'"
                  class="starting-tag"
                >
                  启动中…
                </button>
                <template v-else>
                  <button
                    v-if="node.alive"
                    class="btn-sm amber"
                    @click.stop="emit('action', 'stop', node.id)"
                  >
                    停止
                  </button>
                  <button
                    v-else
                    class="btn-sm primary"
                    @click.stop="emit('action', 'start', node.id)"
                  >
                    启动
                  </button>
                </template>
              </div>
            </div>
          </div>

          <p v-if="nodes.length === 0" class="console-empty">
            暂无节点实例。确认后端已登录并完成实例导入。
          </p>
        </div>

        <!-- ── 选中实例详情 ──
             按需求不展示运行日志与策略任务日志，节点信息里也不显示状态地址。 -->
        <section v-if="detail && !detail.empty" class="panel-card console-detail">
          <div class="section-head">
            <h2>{{ detail.title }}</h2>
            <span class="section-note">{{ detail.subtitle }}</span>
          </div>
          <pre class="console-summary">{{ detail.summary }}</pre>
        </section>
      </div>
    </main>

    <ImportModal
      :open="importOpen"
      @close="importOpen = false"
      @done="emit('refresh')"
    />

    <VersionUpdateModal
      :open="versionOpen"
      @close="versionOpen = false"
      @done="emit('refresh')"
    />

    <SettingsModal
      :open="settingsOpen"
      :selected-id="selectedId"
      @close="settingsOpen = false"
    />

    <AppModal
      v-if="removeTarget"
      title="移除节点"
      :subtitle="removeTarget.title"
      @close="removeTarget = null"
    >
      <p class="remove-confirm">
        确定要移除节点「{{ removeTarget.title }}」吗？
        <br />
        若该节点正在运行会先停止，然后从本机面板摘除（不会删除已部署的客户端文件）。
      </p>
      <template #footer>
        <button class="btn-sm" @click="removeTarget = null">取消</button>
        <button class="btn-sm danger" @click="confirmRemove">确认移除</button>
      </template>
    </AppModal>
  </div>
</template>

<style scoped>
.header-logo-inner {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0b0f1a;
  color: var(--gold);
  font-weight: 700;
  font-size: 13px;
}

.console-error {
  position: sticky;
  top: 0;
  z-index: 20;
  margin: 0 0 14px;
  padding: 9px 11px;
  font-size: 12.5px;
  color: var(--red);
  background: #f2645f1a;
  border: 1px solid rgba(242, 100, 95, 0.28);
  border-radius: 6px;
}

/* 顶栏带文字的按钮（当前只有「版本更新」）。 */
.header-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--dim);
  font-family: inherit;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}

.header-btn:hover {
  color: var(--text);
  border-color: #94a3b866;
}

/* 版本更新按钮：有待更新实例时点亮，并在右上角挂一个数量角标。 */
.version-btn {
  position: relative;
  width: auto;
  height: 30px;
  padding: 0 10px;
  gap: 6px;
}

.version-btn-icon {
  font-size: 14px;
  line-height: 1;
}

.version-btn-text {
  font-size: 13px;
  line-height: 1;
  white-space: nowrap;
}

.version-btn.on {
  color: var(--gold);
  border-color: rgba(217, 179, 108, 0.45);
  background: #d9b36c1a;
}

.version-badge {
  position: absolute;
  top: -5px;
  right: -5px;
  min-width: 15px;
  height: 15px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--amber);
  color: #0f172a;
  font-size: 10px;
  font-weight: 700;
  line-height: 15px;
  text-align: center;
}

.console-empty {
  grid-column: 1 / -1;
  margin: 0;
  padding: 34px 16px;
  text-align: center;
  color: var(--mute);
  font-size: 13px;
}

.remove-confirm {
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
  color: var(--dim);
}

.node-card[data-selected='true'] {
  border-color: #d9b36c66;
}

.console-detail {
  margin-top: 20px;
  padding: 16px 18px;
}

.console-summary,
.console-log {
  margin: 12px 0 0;
  padding: 12px 14px;
  border-radius: 6px;
  background: #0a0e1780;
  border: 1px solid var(--line-soft);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.7;
  color: var(--dim);
  white-space: pre-wrap;
  word-break: break-all;
}

.console-log {
  max-height: 260px;
  overflow: auto;
}
</style>
