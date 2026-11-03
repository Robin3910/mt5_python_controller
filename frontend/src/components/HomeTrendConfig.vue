<script setup lang="ts">
// 首页「15分钟」进入的参数页。改的是已有趋势配置：权重、阈值、刷新间隔、K 线周期。
// 均线开关只影响首页图表，不参与下单，也不写入后端。
import type { TrendConfig, TrendTimeframe } from '@/api/types'

const props = defineProps<{
  form: TrendConfig
  refreshMs: number
  ma5: boolean
  ma10: boolean
  ma20: boolean
  saving: boolean
  isAdmin: boolean
  error: string
}>()

const emit = defineEmits<{
  'update:refreshMs': [value: number]
  'update:ma5': [value: boolean]
  'update:ma10': [value: boolean]
  'update:ma20': [value: boolean]
  save: []
  reset: []
  back: []
}>()

const PERIODS: { value: TrendTimeframe; label: string }[] = [
  { value: 'M1', label: '1分钟' },
  { value: 'M5', label: '5分钟' },
  { value: 'M15', label: '15分钟' },
  { value: 'M30', label: '30分钟' },
  { value: 'H1', label: '1小时' },
  { value: 'H4', label: '4小时' },
  { value: 'D1', label: '日线' },
  { value: 'W1', label: '周线' },
  { value: 'MN', label: '月线' },
]

const REFRESHES: { value: number; label: string }[] = [
  { value: 3000, label: '3秒' },
  { value: 5000, label: '5秒' },
  { value: 10000, label: '10秒' },
  { value: 30000, label: '30秒' },
  { value: 60000, label: '1分钟' },
  { value: 300000, label: '5分钟' },
  { value: 600000, label: '10分钟' },
  { value: 1800000, label: '30分钟' },
]

const emaPct = () => Math.round(props.form.ema_weight * 100)
const rsiPct = () => Math.round(props.form.rsi_weight * 100)

function setEmaPct(next: number): void {
  const pct = Math.min(100, Math.max(0, next))
  props.form.ema_weight = pct / 100
  props.form.rsi_weight = Number(((100 - pct) / 100).toFixed(4))
}

function bumpThreshold(field: 'bullish' | 'bearish', delta: number, min: number, max: number): void {
  const current = Math.round(Number(props.form[field]) || 0)
  props.form[field] = Math.min(max, Math.max(min, current + delta))
}
</script>

<template>
  <section class="cfg-card">
    <button v-if="isAdmin" type="button" class="cfg-back" @click="emit('back')">返回首页</button>
    <p class="cfg-intro">权重、阈值与周期在这里调整。首页只展示算出来的多空值，不直接印这些参数。</p>

    <h2 class="cfg-title">多空权重</h2>
    <div class="cfg-pair">
      <label class="cfg-field">
        <span>多头权重（%）</span>
        <span class="cfg-step">
          <button type="button" aria-label="减少多头权重" @click="setEmaPct(emaPct() - 1)">−</button>
          <strong>{{ emaPct() }}</strong>
          <button type="button" aria-label="增加多头权重" @click="setEmaPct(emaPct() + 1)">+</button>
        </span>
      </label>
      <label class="cfg-field">
        <span>空头权重（%）</span>
        <span class="cfg-step">
          <button type="button" aria-label="减少空头权重" @click="setEmaPct(emaPct() + 1)">−</button>
          <strong>{{ rsiPct() }}</strong>
          <button type="button" aria-label="增加空头权重" @click="setEmaPct(emaPct() - 1)">+</button>
        </span>
      </label>
    </div>
    <p class="cfg-note">当前权重合计 {{ emaPct() + rsiPct() }}%。两项是趋势得分里的 EMA / RSI 权重，合计固定为 100%。</p>

    <h2 class="cfg-title">多空阈值</h2>
    <div class="cfg-pair">
      <label class="cfg-field">
        <span>多头阈值</span>
        <span class="cfg-step">
          <button type="button" aria-label="降低多头阈值" @click="bumpThreshold('bullish', -1, 0, 99)">−</button>
          <strong>{{ form.bullish }}</strong>
          <button type="button" aria-label="提高多头阈值" @click="bumpThreshold('bullish', 1, 0, 99)">+</button>
        </span>
      </label>
      <label class="cfg-field">
        <span>空头阈值</span>
        <span class="cfg-step">
          <button type="button" aria-label="降低空头阈值" @click="bumpThreshold('bearish', -1, -99, 0)">−</button>
          <strong>{{ form.bearish }}</strong>
          <button type="button" aria-label="提高空头阈值" @click="bumpThreshold('bearish', 1, -99, 0)">+</button>
        </span>
      </label>
    </div>
    <p class="cfg-note">得分高于多头阈值视为多头，低于空头阈值视为空头。</p>

    <h2 class="cfg-title">更新时间</h2>
    <div class="cfg-chips">
      <button
        v-for="item in REFRESHES"
        :key="item.value"
        type="button"
        class="cfg-chip"
        :class="{ active: refreshMs === item.value }"
        @click="emit('update:refreshMs', item.value)"
      >{{ item.label }}</button>
    </div>

    <h2 class="cfg-title">K线周期</h2>
    <div class="cfg-chips">
      <button
        v-for="item in PERIODS"
        :key="item.value"
        type="button"
        class="cfg-chip"
        :class="{ active: form.timeframe === item.value }"
        @click="form.timeframe = item.value"
      >{{ item.label }}</button>
    </div>

    <h2 class="cfg-title">K线指标</h2>
    <div class="cfg-chips">
      <button type="button" class="cfg-chip cfg-ma ma5" :class="{ active: ma5 }" @click="emit('update:ma5', !ma5)">MA5</button>
      <button type="button" class="cfg-chip cfg-ma ma10" :class="{ active: ma10 }" @click="emit('update:ma10', !ma10)">MA10</button>
      <button type="button" class="cfg-chip cfg-ma ma20" :class="{ active: ma20 }" @click="emit('update:ma20', !ma20)">MA20</button>
    </div>

    <div class="cfg-actions">
      <button type="button" class="cfg-save" :disabled="saving" @click="emit('save')">
        {{ saving ? '保存中…' : '保存设置' }}
      </button>
      <button type="button" class="cfg-reset" @click="emit('reset')">恢复默认</button>
    </div>
    <p v-if="error" class="cfg-error">{{ error }}</p>
    <p class="cfg-foot">
      <template v-if="isAdmin">保存后写入全局配置，所有节点共用。</template>
      <template v-else>改动只作用于本次查看；全局参数由管理员维护。</template>
    </p>
  </section>
</template>

<style scoped>
.cfg-back {
  display: inline-flex;
  margin: 0 0 10px;
  padding: 0;
  border: none;
  background: transparent;
  color: #8B96A8;
  font-size: 12px;
  backdrop-filter: none;
}
.cfg-back:hover { background: transparent; color: #E6EBF4; }
.cfg-card {
  border: 1px solid #1C2740;
  background: #101828;
  border-radius: 16px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  padding: 22px 22px 16px;
}
.cfg-intro,
.cfg-note,
.cfg-foot {
  margin: 0;
  color: #5B6B85;
  font-size: 12px;
  line-height: 1.5;
}
.cfg-intro { text-align: center; margin-bottom: 18px; }
.cfg-note { margin-top: 8px; }
.cfg-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 18px 0 10px;
  color: #E6EBF4;
  font-size: 13px;
  font-weight: 600;
}
.cfg-title::before {
  content: '';
  width: 3px;
  height: 12px;
  border-radius: 2px;
  background: #2563EB;
}
.cfg-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.cfg-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #8B96A8;
  font-size: 12px;
}
.cfg-step {
  display: flex;
  align-items: center;
  height: 40px;
  border: 1px solid #22304A;
  border-radius: 8px;
  background: #0C1220;
  overflow: hidden;
}
.cfg-step strong {
  flex: 1;
  text-align: center;
  color: #E6EBF4;
  font-size: 18px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.cfg-step button,
.cfg-chip,
.cfg-save,
.cfg-reset {
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
}
.cfg-step button {
  width: 36px;
  height: 100%;
  padding: 0;
  border: none;
  border-radius: 0;
  background: transparent;
  color: #8B96A8;
  font-size: 18px;
}
.cfg-step button:hover { background: #16213A; color: #E6EBF4; }
.cfg-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.cfg-chip {
  height: 30px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #8B96A8;
  font-size: 12px;
}
.cfg-chip:hover { color: #E6EBF4; background: #16213A; }
.cfg-chip.active {
  background: #2563EB;
  border-color: transparent;
  color: #fff;
}
.cfg-ma { display: inline-flex; align-items: center; gap: 6px; }
.cfg-ma::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #5B6B85;
}
.cfg-ma.ma5.active::before { background: #eab308; }
.cfg-ma.ma10.active::before { background: #60a5fa; }
.cfg-ma.ma20.active::before { background: #c084fc; }
.cfg-actions {
  display: flex;
  gap: 10px;
  margin-top: 22px;
}
.cfg-save {
  flex: 1;
  height: 40px;
  border: none;
  border-radius: 8px;
  background: #2563EB;
  color: #fff;
  font-size: 14px;
  font-weight: 650;
}
.cfg-save:hover:not(:disabled) { background: #1d4ed8; }
.cfg-reset {
  height: 40px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid #22304A;
  background: #0C1220;
  color: #E6EBF4;
  font-size: 14px;
}
.cfg-reset:hover { background: #16213A; }
.cfg-error { margin: 10px 0 0; color: #f87171; font-size: 12px; }
.cfg-foot { margin-top: 12px; text-align: center; }
@media (max-width: 640px) {
  .cfg-card { padding: 16px 14px 12px; }
  .cfg-pair { grid-template-columns: 1fr; }
}
</style>
