<script setup lang="ts">
// 「我的」枢纽页：模块卡片入口 + 用户信息 + 安全快捷操作
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useAuthStore } from '@/stores/auth'
import { copyToClipboard } from '@/utils/clipboard'
import type { ProfileHubModule } from '@/api/types'

const auth = useAuthStore()
const router = useRouter()
const userLabel = computed(() => auth.me?.display_name || auth.me?.username || '')
const initial = computed(() => (auth.me?.display_name || auth.me?.username || 'U')[0].toUpperCase())

const WEBHOOK_URL = (() => {
  if (typeof window === 'undefined') return '/webhook'
  return new URL('/webhook', window.location.origin).href
})()

// ── 枢纽模块定义 ────────────────────────────────────────────────
// icon: inline SVG path，accent: 卡片左上角装饰色
const ALL_MODULES: ProfileHubModule[] = [
  {
    code: 'dashboard',
    label: '总览',
    description: '实时净值、持仓与事件流',
    path: '/',
    icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    accent: '#3b82f6',
  },
  {
    code: 'nodes',
    label: '节点',
    description: '节点状态管理与账户信息',
    path: '/nodes',
    icon: '<circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>',
    accent: '#8b5cf6',
  },
  {
    code: 'trend',
    label: '趋势面板',
    description: 'EMA + RSI 多周期趋势评分',
    path: '/trend',
    icon: '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
    accent: '#06b6d4',
  },
  // {
  //   code: 'groups',
  //   label: '分组管理',
  //   description: '分组创建、成员分配与限价监听',
  //   path: '/groups',
  //   icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  //   accent: '#10b981',
  // },
  {
    code: 'strategies',
    label: '策略管理',
    description: '策略模版配置与启用控制',
    path: auth.isAdmin ? '/strategies' : '/user-strategies',
    icon: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    accent: '#f59e0b',
  },
  {
    code: 'console',
    label: '中控台',
    description: '远程执行命令与信号下发',
    path: '/console',
    icon: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    accent: '#ef4444',
  },
  {
    code: 'client_versions',
    label: '客户端版本',
    description: '安装包上传与版本发布',
    path: '/client-versions',
    icon: '<path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z"/><path d="M3.27 6.96L12 12l8.73-5.04M12 22V12"/>',
    accent: '#64748b',
  },
  {
    code: 'events',
    label: '事件',
    description: '实时事件流与告警通知',
    path: '/events',
    icon: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    accent: '#f97316',
  },
  {
    code: 'audits',
    label: '操作审计',
    description: '操作记录与变更历史',
    path: '/audits',
    icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    accent: '#84cc16',
  },
  {
    code: 'permissions',
    label: '用户权限',
    description: '用户管理、角色配置与节点分配',
    path: '/permissions',
    icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><circle cx="12" cy="10" r="2.5"/><path d="M8.5 16a3.5 3.5 0 0 1 7 0"/>',
    accent: '#ec4899',
  },
]

// 管理员 / 普通用户：都按菜单权限过滤；管理员额外加分组管理入口
const visibleModules = computed<ProfileHubModule[]>(() => {
  let base = auth.isAdmin ? ALL_MODULES : ALL_MODULES.filter((m) => auth.hasMenu(m.code))
  if (auth.isAdmin) {
    base = [
      ...base,
      {
        code: 'groups',
        label: '分组管理',
        description: '分组创建、成员分配与限价监听',
        path: '/groups',
        icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
        accent: '#10b981',
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
</script>

<template>
  <div class="profile-page">
    <!-- 顶部 Hero -->
    <div class="profile-hero card card-pad">
      <div class="hero-left">
        <div class="avatar" :aria-label="`${userLabel} 的头像`">
          <span>{{ initial }}</span>
        </div>
        <div class="hero-info">
          <div class="hero-name">
            {{ userLabel }}
            <span v-if="auth.isAdmin" class="tag amber">超管</span>
          </div>
          <div class="hero-sub muted">
            @{{ auth.me?.username }}
            <span v-if="auth.me?.roles?.length" class="muted" style="margin-left: 6px">
              · {{ auth.me!.roles.map((r) => r.name).join('、') }}
            </span>
          </div>
        </div>
      </div>
      <div class="hero-right">
        <RouterLink to="/config" class="btn-ghost btn-sm">个人设置</RouterLink>
        <button class="btn-ghost btn-sm" @click="copyWebhook">复制 webhook</button>
      </div>
    </div>

    <!-- 模块卡片区 -->
    <div class="hub-grid">
      <button
        v-for="m in visibleModules"
        :key="m.code"
        type="button"
        class="hub-card"
        :style="{ '--accent': m.accent }"
        @click="go(m.path)"
      >
        <div class="hub-card-accent" aria-hidden="true"></div>
        <div class="hub-card-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" v-html="m.icon" aria-hidden="true"></svg>
        </div>
        <div class="hub-card-body">
          <strong class="hub-card-label">{{ m.label }}</strong>
          <p class="hub-card-desc muted">{{ m.description }}</p>
        </div>
        <div class="hub-card-arrow" aria-hidden="true">→</div>
      </button>
    </div>
  </div>
</template>

<style scoped>
.profile-page {
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ── Hero ── */
.profile-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.hero-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.avatar {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary) 0%, #6366f1 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 0 24px rgba(59, 130, 246, 0.4);
}

.hero-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.hero-name {
  font-size: 20px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.hero-sub {
  font-size: 13px;
}

.hero-right {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
}

/* ── Hub Grid ── */
.hub-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}

.hub-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: 0;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  cursor: pointer;
  text-align: left;
  overflow: hidden;
  transition: border-color var(--transition), box-shadow var(--transition), transform var(--transition);
}

.hub-card:hover {
  border-color: var(--accent, var(--primary));
  box-shadow: 0 0 0 1px var(--accent, var(--primary)), 0 8px 24px rgba(0, 0, 0, 0.2);
  transform: translateY(-2px);
}

.hub-card:focus-visible {
  outline: 2px solid var(--accent, var(--primary));
  outline-offset: 2px;
}

/* 左上角装饰色条 */
.hub-card-accent {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--accent, var(--primary));
  opacity: 0.7;
}

.hub-card-icon {
  padding: 20px 16px 8px;
  color: var(--accent, var(--primary));
}

.hub-card-icon svg {
  width: 28px;
  height: 28px;
  opacity: 0.9;
}

.hub-card-body {
  padding: 4px 16px 16px;
  flex: 1;
}

.hub-card-label {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  display: block;
  margin-bottom: 4px;
}

.hub-card-desc {
  font-size: 12px;
  line-height: 1.4;
  margin: 0;
}

.hub-card-arrow {
  position: absolute;
  bottom: 14px;
  right: 14px;
  font-size: 16px;
  color: var(--muted);
  opacity: 0;
  transform: translateX(-4px);
  transition: opacity var(--transition), transform var(--transition);
}

.hub-card:hover .hub-card-arrow {
  opacity: 1;
  transform: translateX(0);
}

/* ── Mobile ── */
@media (max-width: 768px) {
  .profile-page {
    padding: 16px;
    gap: 14px;
  }

  .hero-right {
    width: 100%;
  }

  .hub-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }

  .hub-card-icon {
    padding: 16px 12px 6px;
  }

  .hub-card-icon svg {
    width: 22px;
    height: 22px;
  }

  .hub-card-body {
    padding: 2px 12px 14px;
  }

  .hub-card-label {
    font-size: 13px;
  }

  .hub-card-desc {
    font-size: 11px;
  }
}
</style>
