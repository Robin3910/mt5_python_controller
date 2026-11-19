<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { ElDialog } from 'element-plus'
import 'element-plus/es/components/dialog/style/css'
import RobotIcon from '@/components/RobotIcon.vue'
import type { AiRobotSummary } from '@/api/types'
import { trackAiUi } from '@/utils/aiAudit'

const props = defineProps<{
  items: { robot: AiRobotSummary; title: string; followers: number }[]
  tab: 'profit' | 'heat'
  loading: boolean
  error: string
  updatedAt: number | null
  openingGroup: string | null
  showBack: boolean
}>()
const emit = defineEmits<{
  switch: [tab: 'profit' | 'heat']
  follow: [robot: AiRobotSummary]
  retry: []
  back: []
}>()

const percent = new Intl.NumberFormat('zh-CN', {
  style: 'percent', signDisplay: 'exceptZero', minimumFractionDigits: 1, maximumFractionDigits: 1,
})
const clock = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' })
const byProfit = computed(() => props.tab === 'profit')
const ranking = computed(() => props.items.map((item) => {
  const perf = item.robot.performance
  const rate = perf?.data_available && perf.profit_rate_valid && Number.isFinite(perf.profit_rate)
    ? perf.profit_rate : null
  const started = item.robot.started_at
  const days = started ? Math.max(0, Math.floor((Date.now() / 1000 - started) / 86400)) : null
  return { ...item, rate, days, score: byProfit.value ? rate : item.followers }
}).filter((item) => item.score != null).sort((a, b) =>
  (b.score ?? 0) - (a.score ?? 0) || a.robot.group_id.localeCompare(b.robot.group_id),
))
const selected = shallowRef<(typeof ranking.value)[number] | null>(null)

function openDetail(row: (typeof ranking.value)[number]): void {
  selected.value = row
  trackAiUi('page_view', { group_id: row.robot.group_id, value: `ai-rank-${props.tab}-detail` })
}

function closeDetail(): void {
  if (!selected.value) return
  trackAiUi('cancel_dialog', { group_id: selected.value.robot.group_id, value: 'ai-rank-detail' })
  selected.value = null
}
</script>

<template>
  <div class="ai-leaderboard">
    <div v-if="showBack" class="rank-heading">
      <button type="button" aria-label="返回 AI 交易" @click="emit('back')">‹</button>
      <h1>机器人榜单</h1>
    </div>
    <section class="rank-panel" aria-label="机器人榜单" :aria-busy="loading">
      <div class="rank-tabs" role="group" aria-label="榜单类型">
        <button type="button" :aria-pressed="byProfit" @click="emit('switch', 'profit')">收益率榜</button>
        <button type="button" :aria-pressed="!byProfit" @click="emit('switch', 'heat')">机器人热度</button>
        <span v-if="updatedAt != null && !error">更新时间 {{ clock.format(updatedAt) }}</span>
      </div>

      <p v-if="loading && !items.length" class="rank-state" role="status">正在加载榜单…</p>
      <div v-else-if="error" class="rank-state" role="alert">
        <p>{{ error }}</p>
        <button type="button" class="rank-action" @click="emit('retry')">重试</button>
      </div>
      <p v-else-if="!ranking.length" class="rank-state" role="status">
        {{ byProfit && items.length ? '暂无可统计的收益率' : '暂无开放的 AI 机器人' }}
      </p>
      <ol v-else class="rank-list" role="list">
        <li v-for="(row, index) in ranking" :key="row.robot.group_id" class="rank-entry">
          <button type="button" class="rank-robot" aria-haspopup="dialog" @click="openDetail(row)">
            <span class="rank-number">{{ index + 1 }}</span>
            <span class="rank-icon" :class="{ heat: !byProfit }"><RobotIcon :size="15" variant="material" /></span>
            <span class="rank-copy">
              <strong :title="row.title">{{ row.title }}</strong>
              <small v-if="byProfit">{{ row.robot.symbol || '—' }} · 运行 {{ row.days ?? '—' }} 天</small>
              <small v-else>{{ row.robot.strategy_name || 'AI 智能交易' }} 类机器人</small>
            </span>
            <span v-if="byProfit" class="rank-rate" :class="{ loss: (row.rate ?? 0) < 0 }">
              {{ percent.format((row.rate ?? 0) / 100) }}
            </span>
            <span v-else class="rank-heat"><strong>{{ row.followers.toLocaleString('zh-CN') }}</strong><small>跟随节点</small></span>
          </button>
          <button
            v-if="byProfit" type="button" class="rank-action rank-follow"
            :disabled="!!openingGroup || !row.robot.marketable || row.robot.ai_follow_paused"
            :aria-label="`跟单申请 ${row.title}`" @click="emit('follow', row.robot)"
          >{{ openingGroup === row.robot.group_id ? '加载中…' : '跟单申请' }}</button>
        </li>
      </ol>
    </section>
    <p class="rank-note">{{ byProfit ? '按近 30 天已实现收益率排名' : '按跟随节点数排名' }}</p>

    <ElDialog
      :model-value="selected != null" :title="byProfit ? '策略详情' : '热度详情'"
      width="min(420px, calc(100% - 24px))" append-to-body align-center
      @update:model-value="(open) => { if (!open) closeDetail() }"
    >
      <div v-if="selected" class="rank-detail">
        <h2>{{ selected.title }}</h2>
        <p v-if="selected.robot.description">{{ selected.robot.description }}</p>
        <dl>
          <div><dt>交易品种</dt><dd>{{ selected.robot.symbol || '暂无数据' }}</dd></div>
          <div><dt>运行天数</dt><dd>{{ selected.days == null ? '暂无数据' : `${selected.days} 天` }}</dd></div>
          <div><dt>近 30 天收益率</dt><dd>{{ selected.rate == null ? '暂无数据' : percent.format(selected.rate / 100) }}</dd></div>
          <div><dt>跟随节点</dt><dd>{{ selected.followers.toLocaleString('zh-CN') }} 个</dd></div>
          <div v-if="selected.robot.max_allow_num != null"><dt>最多加仓次数</dt><dd>{{ selected.robot.max_allow_num }}</dd></div>
          <div v-if="selected.robot.total_lot_limit != null"><dt>总手数限制</dt><dd>{{ selected.robot.total_lot_limit }}</dd></div>
        </dl>
      </div>
      <template #footer>
        <button type="button" class="rank-detail-back" @click="closeDetail">返回榜单</button>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.ai-leaderboard { animation: rank-enter .28s ease-out; }
@keyframes rank-enter { from { opacity: 0; } to { opacity: 1; } }
.rank-heading { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.rank-heading h1 { margin: 0; font-size: 14px; }
.rank-heading button { padding: 0 8px; }
.rank-panel { padding: 12px; border: 1px solid #1c2740; border-radius: 12px; background: #101828; box-shadow: 0 2px 12px #00000059; }
.rank-panel button { border: 0; padding: 0; background: none; color: inherit; backdrop-filter: none; transition: color .15s, background-color .15s; }
.rank-panel button:active { transform: none; }
.rank-panel button:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
.rank-tabs { display: flex; align-items: center; gap: 4px; border-bottom: 1px solid #1c2740; }
.rank-tabs button { position: relative; padding: 10px 12px; border-radius: 0; color: #5b6b85; font-size: 12px; font-weight: 600; line-height: 16px; white-space: nowrap; }
.rank-tabs button:hover { color: #8b96a8; }
.rank-tabs button[aria-pressed="true"] { color: #e6ebf4; }
.rank-tabs button[aria-pressed="true"]::after { content: ''; position: absolute; inset: auto 8px -1px; height: 2px; border-radius: 2px; background: #2563eb; }
.rank-tabs > span { display: none; margin-left: auto; color: #4a5568; font-size: 10px; white-space: nowrap; }
.rank-list { display: grid; gap: 8px; list-style: none; padding: 0; margin: 10px 0 0; }
.rank-entry { position: relative; display: flex; align-items: center; padding: 10px; gap: 12px; border: 1px solid #22304a; border-radius: 12px; background: #0c1220; transition: border-color .15s; }
.rank-entry:hover { border-color: #2563eb80; }
.rank-robot { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; text-align: left; }
.rank-robot::after { position: absolute; inset: 0; content: ''; }
.rank-number { flex: 0 0 24px; text-align: center; color: #5b6b85; font-size: 14px; font-weight: 700; line-height: 20px; }
.rank-entry:nth-child(1) .rank-number { color: #f59e0b; }
.rank-entry:nth-child(2) .rank-number { color: #94a3b8; }
.rank-entry:nth-child(3) .rank-number { color: #b45309; }
.rank-icon { flex: 0 0 32px; height: 32px; display: grid; place-items: center; border-radius: 8px; background: #2563eb26; }
.rank-icon :deep(.robot-icon) { color: #3b82f6; }
.rank-icon.heat { background: #7c3aed26; }
.rank-icon.heat :deep(.robot-icon) { color: #a78bfa; }
.rank-copy { display: grid; flex: 1; min-width: 0; gap: 2px; }
.rank-copy > * { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-copy strong, .rank-rate { font-size: 12px; font-weight: 700; line-height: 16px; }
.rank-copy small { color: #5b6b85; font-size: 10px; line-height: 15px; }
.rank-rate, .rank-heat { flex-shrink: 0; text-align: right; font-variant-numeric: tabular-nums; }
.rank-rate { color: #22c55e; }
.rank-rate.loss { color: #f6465d; }
.rank-heat strong { display: block; font-size: 14px; font-weight: 700; line-height: 20px; }
.rank-heat small { display: block; color: #5b6b85; font-size: 9px; line-height: 13.5px; }
.rank-panel .rank-action { flex-shrink: 0; padding: 6px 10px; border-radius: 8px; background: #2563eb; color: #fff; font-size: 10px; font-weight: 600; line-height: 15px; white-space: nowrap; }
.rank-panel .rank-action:hover:not(:disabled) { background: #1d4ed8; }
.rank-follow { position: relative; }
.rank-note { margin: 12px 0 0; color: #4a5568; font-size: 10px; line-height: 15px; text-align: center; }
.rank-state { margin: 10px 0 0; padding: 18px 8px; text-align: center; color: #8b96a8; font-size: 12px; }
.rank-state p { margin: 0 0 8px; }
.rank-detail h2 { margin: 0; font-size: 14px; overflow-wrap: anywhere; }
.rank-detail p { overflow-wrap: anywhere; }
.rank-detail dt { color: #8b96a8; }
.rank-detail dl { margin-bottom: 0; }
.rank-detail dl > div { display: flex; justify-content: space-between; gap: 8px; padding: 6px 0; border-bottom: 1px solid #22304a; }
.rank-detail dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
.rank-detail-back { padding: 6px 14px; }
@media (min-width: 640px) { .rank-tabs > span { display: block; } }
@media (min-width: 1024px) { .rank-panel { padding: 16px; } }
@media (max-width: 360px) { .rank-entry, .rank-robot { gap: 6px; } }
@media (prefers-reduced-motion: reduce) { .ai-leaderboard { animation: none; } }
</style>
