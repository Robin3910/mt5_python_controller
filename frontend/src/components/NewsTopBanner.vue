<script setup lang="ts">
// 顶部滚动提示条：黑名单时段当天，在页面最上方滚动展示一条警示。
//
// 展示条件由后端 `banner_state` 判定（今天有黑名单时段 + 用户今天没关闭过），
// 前端只负责渲染与「关闭」动作。
//
// 关闭语义：**当天**不再显示，次日自动重新提示 —— 后端记的是日期不是布尔值。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useNewsStore } from '@/stores/news'

const news = useNewsStore()
const router = useRouter()

/** 关闭按钮：点 X 后立即隐藏（不等接口往返） */
const closed = ref(false)

/** 滚动条文案：提示内容 + 一点引导 */
const marqueeText = computed(() => {
  const reason = news.banner?.reason?.trim()
  return reason ? `${reason} · 点击查看财经日历` : '今日有高影响经济数据公布，已停止新开仓 · 点击查看财经日历'
})

/** 顶栏出现时把页面往下推一段，避免遮住首屏内容 */
const visible = computed(() => news.showBanner && !closed.value)

/** 点击提示条正文 → 跳到财经日历页 */
function openCalendar(): void {
  router.push({ name: 'news-calendar' })
}

/** 点 X 关闭：记「当天已关闭」，当天不再显示 */
async function dismiss(): Promise<void> {
  closed.value = true
  await news.dismissBanner()
}

onMounted(() => {
  // 顶栏是全局的：进页面时如果还没拉过数据，先拉一次
  if (!news.banner?.server_time) void news.fetch()
})

onBeforeUnmount(() => {
  // 重置本地关闭标记：下次进页面时若还是同一天、用户没在后端关过，
  // 提示条应重新出现（本次会话内的临时隐藏不应跨页面生效）。
  closed.value = false
})
</script>

<template>
  <div v-if="visible" class="news-topbar" role="status" aria-live="polite">
    <span class="news-topbar-dot" aria-hidden="true"></span>
    <div class="news-topbar-viewport">
      <div class="news-topbar-track">
        <span class="news-topbar-text">{{ marqueeText }}</span>
        <span class="news-topbar-text" aria-hidden="true">{{ marqueeText }}</span>
      </div>
    </div>
    <button type="button" class="news-topbar-more" @click="openCalendar">查看</button>
    <button
      type="button"
      class="news-topbar-close"
      aria-label="关闭提示（今天不再显示）"
      title="关闭（今天不再显示）"
      @click="dismiss"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <path d="M18 6 6 18M6 6l12 12" />
      </svg>
    </button>
  </div>
</template>

<style scoped>
/* 顶部滚动条：固定在页面最上方，横向无限滚动。
   高度固定 34px，避免内容出现/消失时页面大幅跳动。 */
.news-topbar {
  position: sticky;
  top: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 10px;
  border-bottom: 1px solid rgba(239, 68, 68, 0.35);
  background: linear-gradient(90deg, rgba(127, 29, 29, 0.96), rgba(153, 27, 27, 0.96));
  color: #fee2e2;
  font-size: 12px;
  line-height: 1;
  overflow: hidden;
}

.news-topbar-dot {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
  background: #f87171;
  box-shadow: 0 0 0 3px rgba(248, 113, 113, 0.2);
  animation: news-topbar-pulse 1.6s ease-in-out infinite;
}

@keyframes news-topbar-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

/* 视口裁剪，轨道在其内横向滚动 */
.news-topbar-viewport {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

/* 轨道里放两份同样的文案，滚动到一半时无缝衔接 */
.news-topbar-track {
  display: inline-flex;
  gap: 48px;
  white-space: nowrap;
  animation: news-topbar-scroll 26s linear infinite;
}

.news-topbar:hover .news-topbar-track {
  animation-play-state: paused;
}

@keyframes news-topbar-scroll {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}

.news-topbar-text {
  font-weight: 600;
  letter-spacing: 0.2px;
}

.news-topbar-more {
  flex-shrink: 0;
  padding: 3px 9px;
  border: 1px solid rgba(254, 226, 226, 0.4);
  border-radius: 999px;
  background: rgba(254, 226, 226, 0.12);
  color: #fee2e2;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}

.news-topbar-more:hover {
  background: rgba(254, 226, 226, 0.24);
}

.news-topbar-close {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: #fecaca;
  cursor: pointer;
  transition: background 0.15s;
}

.news-topbar-close:hover {
  background: rgba(254, 226, 226, 0.2);
}

.news-topbar-close svg {
  width: 13px;
  height: 13px;
}

@media (prefers-reduced-motion: reduce) {
  .news-topbar-track { animation: none; }
  .news-topbar-dot { animation: none; }
}
</style>
