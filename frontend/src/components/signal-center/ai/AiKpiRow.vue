<script setup lang="ts">
// 四个指标卡：今日触发信号 / 覆盖周期 / 高可靠信号 / 平均信号质量。
//
// **口径提醒**（都是拍板定的，别改错）：
// - 今日触发信号 → **UTC+8 自然日**，不是服务器本地日；
// - 覆盖周期 → **只给个数，不写分母**（不显示 "8/8"）；
// - 高可靠信号 → 等级为「高」的**信号条数**（不是占比）；
// - 平均信号质量 → **有可用先验**的信号的质量分均值
//   （没先验的信号不参与，也不把均值拉低）。
import type { AiKpi } from '@/api/types'

defineProps<{ kpi: AiKpi | null }>()
</script>

<template>
  <div class="ai-kpi-grid">
    <div class="pf-card ai-kpi">
      <p class="ai-kpi-k">今日触发信号</p>
      <p class="ai-kpi-v">{{ kpi?.today_signals ?? 0 }}</p>
      <p class="ai-kpi-sub">UTC+8 自然日</p>
    </div>
    <div class="pf-card ai-kpi">
      <p class="ai-kpi-k">覆盖周期</p>
      <p class="ai-kpi-v">{{ kpi?.timeframes_covered ?? 0 }}</p>
      <p class="ai-kpi-sub">窗口内出现过的周期</p>
    </div>
    <div class="pf-card ai-kpi">
      <p class="ai-kpi-k">高可靠信号</p>
      <p class="ai-kpi-v ai-good">{{ kpi?.high_reliability ?? 0 }}</p>
      <p class="ai-kpi-sub">质量分 60 分以上</p>
    </div>
    <div class="pf-card ai-kpi">
      <p class="ai-kpi-k">平均信号质量</p>
      <p class="ai-kpi-v">{{ (kpi?.avg_quality ?? 0).toFixed(1) }}</p>
      <p class="ai-kpi-sub">
        {{ kpi?.signals_scored ?? 0 }} / {{ kpi?.signals_total ?? 0 }} 条有历史样本
      </p>
    </div>
  </div>
</template>
