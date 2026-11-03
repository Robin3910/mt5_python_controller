<script setup lang="ts">
// 「我的」枢纽页：账号卡片 + 模块入口网格（样式 1:1 复刻 UI/H5.html 的「我的」tab）
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { copyToClipboard } from '@/utils/clipboard'
import { disconnectAdminWs } from '@/services/ws'
import type { ProfileHubModule } from '@/api/types'

const auth = useAuthStore()
const hub = useHubStore()
const router = useRouter()
const userLabel = computed(() => auth.me?.display_name || auth.me?.username || '')
const roleNames = computed(() => (auth.me?.roles || []).map((r) => r.name).join('、'))

const WEBHOOK_URL = (() => {
  if (typeof window === 'undefined') return '/webhook'
  return new URL('/webhook', window.location.origin).href
})()

onMounted(() => {
  if (!hub.nodes.length) hub.fetchNodes().catch(() => {})
})

// ── 枢纽模块定义 ────────────────────────────────────────────────
// icon: inline SVG path，accent: 图标色 / 底色（15% 透明度）
const ALL_MODULES: ProfileHubModule[] = [
  {
    code: 'dashboard',
    label: '总览',
    description: '实时净值、持仓与事件流',
    path: '/',
    icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    accent: '#3B82F6',
  },
  {
    code: 'nodes',
    label: '节点',
    description: '节点状态管理与账户信息',
    path: '/nodes',
    icon: '<circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>',
    accent: '#06B6D4',
  },
  {
    code: 'trend',
    label: '趋势面板',
    description: 'EMA + RSI 多周期趋势评分',
    path: '/trend',
    icon: '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
    accent: '#22C55E',
  },
  {
    code: 'strategies',
    label: '策略管理',
    description: '策略模版配置与启用控制',
    path: auth.isAdmin ? '/strategies' : '/user-strategies',
    icon: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    accent: '#FBBF24',
  },
  {
    code: 'follow',
    label: '带单',
    description: '把节点加入带单员的分组，接收他的信号',
    path: '/follow',
    icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    accent: '#A78BFA',
  },
  {
    code: 'ai_trade',
    label: 'AI 交易',
    description: '跟随 AI 加仓机器人的加仓节奏（手数不缩放）',
    path: '/ai-trade',
    icon: '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4"/><circle cx="12" cy="3" r="1.5"/><path d="M9 14h.01M15 14h.01M9.5 17h5"/>',
    accent: '#22C55E',
  },
  {
    code: 'console',
    label: '中控台',
    description: '远程执行命令与信号下发',
    path: '/console',
    icon: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    accent: '#F87171',
  },
  {
    code: 'client_versions',
    label: '客户端版本',
    description: '安装包上传与版本发布',
    path: '/client-versions',
    icon: '<path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z"/><path d="M3.27 6.96L12 12l8.73-5.04M12 22V12"/>',
    accent: '#A78BFA',
  },
  {
    code: 'events',
    label: '事件',
    description: '实时事件流与告警通知',
    path: '/events',
    icon: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    accent: '#F59E0B',
  },
  {
    code: 'audits',
    label: '操作审计',
    description: '操作记录与变更历史',
    path: '/audits',
    icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    accent: '#38BDF8',
  },
  {
    code: 'permissions',
    label: '用户权限',
    description: '用户管理、角色配置与节点分配',
    path: '/permissions',
    icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><circle cx="12" cy="10" r="2.5"/><path d="M8.5 16a3.5 3.5 0 0 1 7 0"/>',
    accent: '#EC4899',
  },
]

// 管理员 / 普通用户：都按菜单权限过滤；管理员额外加「首页」「分组管理」入口
const visibleModules = computed<ProfileHubModule[]>(() => {
  let base = auth.isAdmin ? ALL_MODULES : ALL_MODULES.filter((m) => auth.hasMenu(m.code))
  if (auth.isAdmin) {
    base = [
      {
        code: 'home',
        label: '首页',
        description: '全部节点的净值、持仓与实时状态',
        path: '/home',
        icon: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/>',
        accent: '#0EA5E9',
      },
      ...base,
      {
        code: 'groups',
        label: '分组管理',
        description: '分组创建、成员分配与限价监听',
        path: '/groups',
        icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
        accent: '#10B981',
      },
    ]
  }
  return base
})

function go(path: string): void {
  router.push(path)
}

async function copyWebhook(): Promise<void> {
  const ok = await copyToClipboard(WEBHOOK_URL)
  ElMessage.success(ok ? 'webhook 地址已复制' : '复制失败')
}

/** 退出登录：断开后台 WS 后清空登录态并回到登录页 */
function logout(): void {
  disconnectAdminWs()
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="mine-page">
    <!-- 账号卡片 -->
    <section class="pf-card mine-card">
      <div class="mine-card-head">
        <div class="mine-user">
          <span class="mine-avatar" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
            </svg>
          </span>
          <div class="mine-user-info">
            <div class="mine-user-row">
              <p class="mine-user-name">{{ userLabel }}</p>
              <span class="mine-online">
                <span class="mine-online-dot"></span>
                在线
              </span>
            </div>
            <p class="mine-user-sub">
              @{{ auth.me?.username }}<template v-if="roleNames"> · {{ roleNames }}</template>
            </p>
          </div>
        </div>
        <div class="mine-actions">
          <RouterLink to="/profile" class="pf-btn">个人中心</RouterLink>
          <button type="button" class="pf-btn" @click="copyWebhook">复制 webhook</button>
          <button type="button" class="pf-btn pf-btn-danger" @click="logout">退出登录</button>
        </div>
      </div>

      <div class="mine-stats">
        <div class="mine-stat">
          <p class="mine-stat-k">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="2" y="2" width="20" height="8" rx="2" />
              <rect x="2" y="14" width="20" height="8" rx="2" />
              <path d="M6 6h.01M6 18h.01" />
            </svg>
            在线节点
          </p>
          <p class="mine-stat-v">{{ hub.onlineCount }} / {{ hub.nodes.length }}</p>
        </div>
        <div class="mine-stat">
          <p class="mine-stat-k">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="4" y="4" width="16" height="16" rx="2" />
              <rect x="9" y="9" width="6" height="6" />
              <path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3" />
            </svg>
            节点总数
          </p>
          <p class="mine-stat-v">{{ hub.nodes.length }}</p>
        </div>
      </div>
    </section>

    <!-- 个人中心入口（原型 mine-profile-entry：账号卡片与「我的模块」之间） -->
    <RouterLink to="/profile" class="mine-profile-entry">
      <span class="mine-profile-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1" />
        </svg>
      </span>
      <div class="mine-profile-body">
        <p class="mine-profile-title">个人中心</p>
        <p class="mine-profile-desc">个人信息 · 修改密码</p>
      </div>
      <span class="mine-chevron" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 18l6-6-6-6" />
        </svg>
      </span>
    </RouterLink>

    <!-- 我的模块 -->
    <section class="mine-section">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>我的模块</p>
        <span class="pf-count">{{ visibleModules.length }} 项</span>
      </div>
      <div class="mine-modules">
        <button
          v-for="m in visibleModules"
          :key="m.code"
          type="button"
          class="mine-module"
          :style="{ '--accent': m.accent }"
          @click="go(m.path)"
        >
          <span class="mine-module-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" v-html="m.icon"></svg>
          </span>
          <div class="mine-module-body">
            <p class="mine-module-title">{{ m.label }}</p>
            <p class="mine-module-desc">{{ m.description }}</p>
          </div>
          <span class="mine-chevron" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 18l6-6-6-6" />
            </svg>
          </span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* ── 1:1 复刻 UI/H5.html「我的」tab 的配色与尺寸 ──
   卡片 / 区块标题 / 按钮等共用外观见 styles.css 的 pf- 前缀类，
   这里只放本页独有的部分。 */
.mine-page {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 账号卡片 ── */
.mine-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.mine-user {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1 1 260px;
  min-width: 0;
}

.mine-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  flex-shrink: 0;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
  box-shadow: 0 2px 10px rgba(99, 102, 241, 0.4);
  color: #fff;
}
.mine-avatar svg { width: 22px; height: 22px; }

.mine-user-info { min-width: 0; flex: 1; }

.mine-user-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.mine-user-name {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--pf-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mine-online {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
  font-size: 10px;
  font-weight: 600;
}
.mine-online-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  animation: mine-pulse 2s ease-in-out infinite;
}
@keyframes mine-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.mine-user-sub {
  margin: 2px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mine-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

/* ── 指标条 ── */
.mine-stats {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.mine-stat {
  border-radius: 8px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  padding: 8px;
}

.mine-stat-k {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  font-size: 9px;
  color: var(--pf-dim);
}
.mine-stat-k svg { width: 12px; height: 12px; }

.mine-stat-v {
  margin: 2px 0 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--pf-text);
  font-family: var(--mono);
}

/* ── 个人中心入口 ── */
.mine-profile-entry {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
  padding: 12px;
  text-align: left;
  text-decoration: none;
  border-radius: 12px;
  border: 1px solid var(--pf-border);
  background: linear-gradient(90deg, #101828 0%, #131c33 100%);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  transition: border-color 0.15s;
}
.mine-profile-entry:hover { border-color: rgba(37, 99, 235, 0.5); }
.mine-profile-entry:hover .mine-chevron { color: #60a5fa; }

.mine-profile-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: 12px;
  background: rgba(37, 99, 235, 0.15);
  color: #3b82f6;
}
.mine-profile-icon svg { width: 20px; height: 20px; }

.mine-profile-body { flex: 1; min-width: 0; }

.mine-profile-title {
  margin: 0;
  font-size: 12px;
  font-weight: 700;
  color: var(--pf-text);
}

.mine-profile-desc {
  margin: 2px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 我的模块 ── */
.mine-section { margin-top: 0; }

.mine-modules {
  margin-top: 8px;
  display: grid;
  gap: 10px;
}
@media (min-width: 640px) {
  .mine-modules { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

.mine-module {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
  padding: 12px;
  text-align: left;
  border-radius: 12px;
  border: 1px solid var(--pf-border);
  background: var(--pf-card);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  transition: border-color 0.15s;
}
.mine-module:hover { border-color: rgba(37, 99, 235, 0.5); }
.mine-module:hover .mine-chevron { color: #60a5fa; }

.mine-module-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: 12px;
  color: var(--accent, var(--pf-blue));
  background: color-mix(in srgb, var(--accent, var(--pf-blue)) 15%, transparent);
}
.mine-module-icon svg { width: 20px; height: 20px; }

.mine-module-body {
  flex: 1;
  min-width: 0;
}

.mine-module-title {
  margin: 0;
  font-size: 12px;
  font-weight: 700;
  color: var(--pf-text);
}

.mine-module-desc {
  margin: 2px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mine-chevron {
  flex-shrink: 0;
  color: var(--pf-faint);
  transition: color 0.15s;
}
.mine-chevron svg { width: 16px; height: 16px; }

/* ── 移动端 ── */
@media (max-width: 640px) {
  .mine-actions {
    width: 100%;
  }
  .mine-actions .pf-btn { flex: 1 1 0; }
}
</style>
