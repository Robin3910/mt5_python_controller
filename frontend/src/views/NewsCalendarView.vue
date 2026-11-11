<script setup lang="ts">
// 财经日历（新闻黑名单 + 停止做单开关）独立页。
//
// 从「个人中心」独立出来的原因：这是一个**独立的功能模块**（有设置、有事件列表、
// 有顶部提示条），挤在个人中心的第 3 个卡片里既难找也不好看。
//
// 顶部滚动提示条挂在 App.vue 上（全局可见），本页与它共享 news store，
// 因此这里改了设置，顶栏的状态会同步更新。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useNewsStore } from '@/stores/news'
import type { NewsEvent } from '@/api/news'

const news = useNewsStore()

/** 倒计时用的本地时钟（每秒跳一次；实际剩余时间按服务端时钟算） */
const nowTick = ref(Date.now())
let tickTimer: number | undefined

/** 当前剩余秒数：按服务端时钟算，避免本地时钟不准导致倒计时跳变。

    `nowTick` 每秒跳一次，作为依赖让这个 computed 重新求值 —— 否则
    剩余时间只在 blocked / until 变化时才更新，倒计时会卡住不动。
*/
const remainingSec = computed(() => {
  void nowTick.value
  if (!news.blocked || !news.until) return 0
  const serverNow = Date.now() + news.serverSkew
  return Math.max(0, Math.floor((news.until * 1000 - serverNow) / 1000))
})

/** 倒计时文案：超过 1 小时显示「X 小时 Y 分」，否则显示「X 分 Y 秒」 */
const remainingText = computed(() => {
  const total = remainingSec.value
  if (total <= 0) return ''
  if (total >= 3600) {
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    return `${h} 小时 ${m} 分`
  }
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
})

/** 可选币种清单（UI 多选用的全集） */
const CURRENCY_OPTIONS = ['USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD']

/** 事件按日期分组（北京时间） */
const newsEventGroups = computed(() => {
  const groups = new Map<string, NewsEvent[]>()
  for (const ev of news.events) {
    const day = ev.event_time_text.slice(0, 10)
    if (!groups.has(day)) groups.set(day, [])
    groups.get(day)!.push(ev)
  }
  return Array.from(groups.entries()).map(([day, list]) => ({ day, list }))
})

/** 只看我的黑名单内事件 */
const onlyMyBlackout = ref(false)
const visibleNewsGroups = computed(() => {
  if (!onlyMyBlackout.value) return newsEventGroups.value
  return newsEventGroups.value
    .map((g) => ({ day: g.day, list: g.list.filter((e) => e.in_blackout) }))
    .filter((g) => g.list.length > 0)
})

const IMPACT_LABELS: Record<string, string> = {
  high: '高影响',
  medium: '中影响',
  low: '低影响',
  holiday: '休市',
}

function impactLabel(impact: string): string {
  return IMPACT_LABELS[impact] || impact
}

/** 切换关注币种（至少保留一个） */
function toggleCurrency(code: string): void {
  const list = news.settings.currencies
  const next = list.includes(code) ? list.filter((c) => c !== code) : [...list, code]
  if (!next.length) {
    news.error = '至少要关注一个币种'
    return
  }
  void news.save({ currencies: next })
}

async function refreshCalendar(): Promise<void> {
  const msg = await news.refresh()
  ElMessage.success(msg)
}

onMounted(() => {
  void news.fetch()
  tickTimer = window.setInterval(() => (nowTick.value = Date.now()), 1000)
})

onBeforeUnmount(() => {
  if (tickTimer) window.clearInterval(tickTimer)
})
</script>

<template>
  <div class="news-page">
    <!-- 状态卡 -->
    <section class="pf-card">
      <div class="pf-section-head">
        <p class="pf-section-title"><span class="pf-bar"></span>财经日历</p>
        <span class="pf-count" :class="news.state === 'ok' ? 'ok' : 'warn'">
          {{ news.state === 'ok' ? '开单正常' : news.state === 'paused' ? '已停止做单' : '黑名单中' }}
        </span>
      </div>

      <!-- 状态条 -->
      <div v-if="news.state !== 'ok'" class="news-banner" :class="news.state">
        <span class="news-banner-dot"></span>
        <div class="news-banner-body">
          <p class="news-banner-title">
            <template v-if="news.state === 'paused'">已手动停止做单</template>
            <template v-else>数据公布时段，已停止新开仓</template>
          </p>
          <p v-if="news.status?.reason" class="news-banner-sub">{{ news.status.reason }}</p>
          <p v-if="news.state === 'blocked' && remainingText" class="news-banner-sub">
            预计还有 {{ remainingText }} 恢复
          </p>
        </div>
      </div>
      <div v-else class="news-banner ok">
        <span class="news-banner-dot"></span>
        <div class="news-banner-body">
          <p class="news-banner-title">开单正常</p>
          <p class="news-banner-sub">
            当前没有命中黑名单的高影响数据
          </p>
        </div>
      </div>

      <!-- 说明：只影响新开仓 -->
      <p class="pf-hint news-hint">
        只影响<strong>新开仓</strong>：已在运行的任务与已有持仓的止损止盈<strong>照常执行</strong>，不会被打断。
      </p>

      <div class="profile-form">
        <!-- 总开关 + 手动急停 -->
        <div class="news-switch-row">
          <div class="news-switch-main">
            <label class="pf-label" for="news-enabled">新闻黑名单</label>
            <p class="pf-hint">高影响经济数据公布前后自动停止新开仓</p>
          </div>
          <button
            id="news-enabled"
            type="button"
            class="news-toggle"
            :class="{ on: news.settings.enabled }"
            :aria-pressed="news.settings.enabled"
            :disabled="news.saving"
            @click="news.save({ enabled: !news.settings.enabled })"
          >
            <span class="news-toggle-knob"></span>
          </button>
        </div>

        <div class="news-switch-row">
          <div class="news-switch-main">
            <label class="pf-label" for="news-manual">停止做单</label>
            <p class="pf-hint">手动急停：随时停止所有新开仓，不受日历数据影响</p>
          </div>
          <button
            id="news-manual"
            type="button"
            class="news-toggle"
            :class="{ on: news.settings.manual_paused, danger: news.settings.manual_paused }"
            :aria-pressed="news.settings.manual_paused"
            :disabled="news.saving"
            @click="news.save({ manual_paused: !news.settings.manual_paused })"
          >
            <span class="news-toggle-knob"></span>
          </button>
        </div>

        <!-- 窗口与币种 -->
        <div class="pf-grid-3">
          <div>
            <label class="pf-label" for="news-before">事件前（分钟）</label>
            <input
              id="news-before"
              class="pf-input"
              type="number"
              min="0"
              max="120"
              :value="news.settings.before_min"
              @change="news.save({ before_min: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
          <div>
            <label class="pf-label" for="news-after">事件后（分钟）</label>
            <input
              id="news-after"
              class="pf-input"
              type="number"
              min="0"
              max="120"
              :value="news.settings.after_min"
              @change="news.save({ after_min: Number(($event.target as HTMLInputElement).value) })"
            />
          </div>
          <div>
            <label class="pf-label" for="news-impact">最低影响等级</label>
            <select
              id="news-impact"
              class="pf-input"
              :value="news.settings.min_impact"
              @change="news.save({ min_impact: ($event.target as HTMLSelectElement).value })"
            >
              <option value="high">高影响</option>
              <option value="medium">高 + 中影响</option>
              <option value="low">全部等级</option>
            </select>
          </div>
        </div>

        <div>
          <label class="pf-label">关注币种</label>
          <div class="news-currencies">
            <button
              v-for="code in CURRENCY_OPTIONS"
              :key="code"
              type="button"
              class="news-chip"
              :class="{ on: news.settings.currencies.includes(code) }"
              :disabled="news.saving"
              @click="toggleCurrency(code)"
            >
              {{ code }}
            </button>
          </div>
          <p class="pf-hint">黄金（XAUUSD）按美元 USD 归类；默认只关注 USD。</p>
        </div>
      </div>

      <p v-if="news.error" class="pf-error">{{ news.error }}</p>
    </section>

    <!-- 事件列表 -->
    <section class="pf-card">
      <div class="pf-section-head news-list-head">
        <p class="pf-section-title">近期事件<span class="news-tz">（{{ news.calendar.timezone }}）</span></p>
        <div class="news-list-actions">
          <button
            type="button"
            class="pf-btn pf-btn-mini"
            :class="{ 'pf-btn-danger': onlyMyBlackout }"
            @click="onlyMyBlackout = !onlyMyBlackout"
          >
            {{ onlyMyBlackout ? '看全部' : '只看我的黑名单' }}
          </button>
          <button type="button" class="pf-btn pf-btn-mini" :disabled="news.saving" @click="refreshCalendar">
            刷新
          </button>
        </div>
      </div>

      <p v-if="news.loading" class="pf-hint">加载中…</p>
      <p v-else-if="!visibleNewsGroups.length" class="pf-hint">
        {{ onlyMyBlackout ? '近期没有落入你黑名单的事件' : '近期暂无事件' }}
      </p>

      <div v-for="group in visibleNewsGroups" :key="group.day" class="news-day">
        <p class="news-day-title">{{ group.day }}</p>
        <ul class="pf-list">
          <li v-for="ev in group.list" :key="ev.event_id" class="news-event">
            <div class="news-event-main">
              <p class="news-event-title">
                {{ ev.title }}
                <span class="news-impact" :class="ev.impact">{{ impactLabel(ev.impact) }}</span>
                <span v-if="ev.in_blackout" class="news-flag">黑名单</span>
              </p>
              <p class="news-event-meta">
                {{ ev.event_time_text.slice(11) }} · {{ ev.country }}
                <template v-if="ev.forecast"> · 预期 {{ ev.forecast }}</template>
                <template v-if="ev.previous"> · 前值 {{ ev.previous }}</template>
              </p>
              <p v-if="ev.block_reason" class="news-event-why">{{ ev.block_reason }}</p>
            </div>
          </li>
        </ul>
      </div>

      <!-- 数据新鲜度 -->
      <p class="pf-hint news-fresh">
        <template v-if="news.calendar.fetched_at">
          日历数据更新于 {{ news.calendar.fetched_at_text }}
        </template>
        <template v-else>尚未获取到日历数据</template>
        <template v-if="news.calendar.stale">
          ，更新可能已失败，当前展示的是上次数据（黑名单可能不完整）
        </template>
      </p>
    </section>
  </div>
</template>

<style scoped>
.news-page {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 表单容器 ── */
.profile-form {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 状态条：正常 / 手动暂停 / 新闻黑名单 ── */
.news-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid rgba(34, 197, 94, 0.35);
  background: rgba(34, 197, 94, 0.1);
}
.news-banner.paused {
  border-color: rgba(245, 158, 11, 0.4);
  background: rgba(245, 158, 11, 0.1);
}
.news-banner.blocked {
  border-color: rgba(239, 68, 68, 0.45);
  background: rgba(239, 68, 68, 0.12);
}
.news-banner-dot {
  width: 7px;
  height: 7px;
  margin-top: 5px;
  flex-shrink: 0;
  border-radius: 50%;
  background: #22c55e;
}
.news-banner.paused .news-banner-dot { background: #f59e0b; }
.news-banner.blocked .news-banner-dot { background: #ef4444; }
.news-banner-body { min-width: 0; }
.news-banner-title {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--pf-text);
}
.news-banner-sub {
  margin: 2px 0 0;
  font-size: 10px;
  line-height: 1.6;
  color: var(--pf-dim);
  word-break: break-word;
}

/* 「只影响新开仓」的说明 —— 必须显眼 */
.news-hint {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(37, 99, 235, 0.08);
  border: 1px solid rgba(37, 99, 235, 0.28);
}
.news-hint strong { color: var(--pf-text); }

/* 开关行 */
.news-switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid #1a2436;
}
.news-switch-main { min-width: 0; }
.news-switch-main .pf-label { margin-bottom: 2px; }

/* 开关按钮（自绘，风格与全局 .pf-btn 体系一致） */
.news-toggle {
  position: relative;
  width: 42px;
  height: 24px;
  flex-shrink: 0;
  padding: 0;
  border-radius: 999px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.news-toggle:disabled { opacity: 0.5; cursor: not-allowed; }
.news-toggle.on {
  border-color: transparent;
  background: var(--pf-blue);
}
.news-toggle.on.danger {
  background: #ef4444;
}
.news-toggle-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.15s;
}
.news-toggle.on .news-toggle-knob { transform: translateX(18px); }

/* 币种筹码 */
.news-currencies {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.news-chip {
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--pf-border-2);
  background: var(--pf-soft);
  color: var(--pf-dim);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  transition: all 0.15s;
}
.news-chip.on {
  border-color: rgba(37, 99, 235, 0.5);
  background: rgba(37, 99, 235, 0.18);
  color: #93c5fd;
  font-weight: 600;
}
.news-chip:disabled { opacity: 0.5; cursor: not-allowed; }

/* 事件列表 */
.news-list-head { margin-top: 4px; }
.news-tz {
  font-weight: 400;
  color: var(--pf-dim);
  font-size: 10px;
}
.news-list-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.news-day { margin-top: 10px; }
.news-day-title {
  margin: 0 0 2px;
  font-size: 10px;
  color: var(--pf-dim);
}
.news-event {
  display: block;
}
.news-event-main { min-width: 0; }
.news-event-title {
  margin: 0;
  font-size: 12px;
  color: var(--pf-text);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.news-event-meta {
  margin: 3px 0 0;
  font-size: 10px;
  color: var(--pf-dim);
}
.news-event-why {
  margin: 3px 0 0;
  font-size: 10px;
  color: var(--pf-faint);
}

/* 影响等级标签 */
.news-impact {
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 9px;
  font-weight: 600;
  border: 1px solid transparent;
}
.news-impact.high {
  background: rgba(239, 68, 68, 0.16);
  border-color: rgba(239, 68, 68, 0.35);
  color: #f87171;
}
.news-impact.medium {
  background: rgba(245, 158, 11, 0.16);
  border-color: rgba(245, 158, 11, 0.35);
  color: #fbbf24;
}
.news-impact.low {
  background: rgba(100, 116, 139, 0.16);
  border-color: rgba(100, 116, 139, 0.3);
  color: var(--pf-dim);
}
.news-impact.holiday {
  background: rgba(124, 58, 237, 0.16);
  border-color: rgba(124, 58, 237, 0.3);
  color: #a78bfa;
}

/* 「落入我的黑名单」标记 */
.news-flag {
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 9px;
  font-weight: 600;
  background: rgba(239, 68, 68, 0.16);
  border: 1px solid rgba(239, 68, 68, 0.35);
  color: #f87171;
}

/* 数据新鲜度 */
.news-fresh { margin-top: 12px; }

@media (max-width: 640px) {
  .news-page { gap: 10px; }
}
</style>
