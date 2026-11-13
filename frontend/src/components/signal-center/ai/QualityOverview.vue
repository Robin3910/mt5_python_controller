<script setup lang="ts">
// 信号质量总览：可靠性等级分布 + 质量分区间分布。
//
// **两个分布是两回事**，别混读：
// - 等级分布：**按绝对阈值**分的高/中/低三档（60 / 50 分界）；
// - 区间分布：**按分数高低**分的四档（90+ / 80-89 / 70-79 / <70）。
import type { AiReliabilityItem, AiScoreBucket } from '@/api/types'
import { LEVEL_ADVICE, LEVEL_LABELS, LEVEL_TONE } from '@/constants/signalCenter'

defineProps<{
  reliability: AiReliabilityItem[]
  scoreBuckets: AiScoreBucket[]
  loading?: boolean
}>()
</script>

<template>
  <div class="pf-card">
    <div class="pf-section-head" style="margin-bottom: 12px">
      <h3 class="pf-section-title"><span class="pf-bar" />信号质量总览</h3>
      <span class="pf-count">质量分 = 同类历史信号胜率</span>
    </div>

    <div class="pf-grid-2">
      <!-- 可靠性等级分布 -->
      <div>
        <p class="ai-sub-title">可靠性等级分布</p>
        <div v-if="reliability.length" class="ai-dist">
          <div v-for="r in reliability" :key="r.level" class="ai-dist-row">
            <span class="ai-dist-label">
              <span class="pf-pill" :class="LEVEL_TONE[r.level]">{{ r.label || LEVEL_LABELS[r.level] }}</span>
            </span>
            <span class="ai-dist-bar-wrap">
              <span
                class="ai-dist-bar"
                :class="LEVEL_TONE[r.level]"
                :style="{ width: Math.min(100, r.pct) + '%' }"
              />
            </span>
            <span class="ai-dist-val">
              {{ r.count }} <em>{{ r.pct.toFixed(1) }}%</em>
            </span>
            <span class="ai-dist-advice">{{ r.advice || LEVEL_ADVICE[r.level] }}</span>
          </div>
        </div>
        <p v-else class="pf-empty">暂无数据</p>
      </div>

      <!-- 质量分区间分布 -->
      <div>
        <p class="ai-sub-title">质量分区间分布</p>
        <div v-if="scoreBuckets.length" class="ai-dist">
          <div v-for="b in scoreBuckets" :key="b.key" class="ai-dist-row">
            <span class="ai-dist-label ai-dist-label-wide">{{ b.label }}</span>
            <span class="ai-dist-bar-wrap">
              <span class="ai-dist-bar blue" :style="{ width: Math.min(100, b.pct) + '%' }" />
            </span>
            <span class="ai-dist-val">
              {{ b.count }} <em>{{ b.pct.toFixed(1) }}%</em>
            </span>
            <span class="ai-dist-advice" />
          </div>
        </div>
        <p v-else class="pf-empty">暂无数据</p>
      </div>
    </div>
  </div>
</template>
