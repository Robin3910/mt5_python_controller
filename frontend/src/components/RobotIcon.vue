<script setup lang="ts">
// AI 机器人图标（自绘 SVG）。
//
// 为什么不用图标库：项目里没有 @element-plus/icons-vue 之类的依赖，
// 引入一整套图标库只为了一个图标不值当 —— 而且自绘 SVG 能跟着
// 主色（--primary 蓝）走，不需要另做一套彩色资源。
//
// 三种造型，用 variant 切：
//   - 'antenna'（默认）：两根天线分别向外斜出 + 端点小圆球（标题用，一眼看出是机器人）
//   - 'dome'：单根竖天线 + 底部散热栅格
//   - 'iconfont'：iconfont 风格的实心机器人（列表用，填充块面在密集卡片里更醒目）
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 直径（px）。列表用 30–40，页面标题用 26 */
    size?: number
    /** 带底色的圆角方块：列表里代替头像的位置 */
    boxed?: boolean
    /** 造型：'antenna' 双天线（默认）/ 'dome' 单天线 / 'iconfont' 实心 */
    variant?: 'antenna' | 'dome' | 'iconfont'
  }>(),
  { size: 32, boxed: false, variant: 'antenna' },
)

/** 圆角随尺寸缩放：固定 12px 在 30px 的方块上显得过圆，在 44px 上又偏方 */
const radius = computed(() => Math.round(props.size * 0.3))
</script>

<template>
  <span
    class="robot-icon"
    :class="[`v-${variant}`, { boxed }]"
    :style="{
      width: `${size}px`,
      height: `${size}px`,
      borderRadius: boxed ? `${radius}px` : undefined,
    }"
    aria-hidden="true"
  >
    <!-- 造型 A：两根天线向外斜出，端点带小圆球 -->
    <svg
      v-if="variant === 'antenna'"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.9"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <!-- 左天线 -->
      <path d="M7.6 8.1 5.2 4.4" />
      <circle cx="4.5" cy="3.3" r="1.35" fill="currentColor" stroke="none" />
      <!-- 右天线 -->
      <path d="M16.4 8.1 18.8 4.4" />
      <circle cx="19.5" cy="3.3" r="1.35" fill="currentColor" stroke="none" />
      <!-- 头 -->
      <rect x="3.6" y="8" width="16.8" height="12.2" rx="4" />
      <!-- 两只眼睛 -->
      <path d="M8.9 13.4h.01M15.1 13.4h.01" stroke-width="2.3" />
      <!-- 嘴（横条） -->
      <path d="M9.6 17.1h4.8" />
    </svg>

    <!-- 造型 B：单根竖天线 + 底部散热栅格 -->
    <svg
      v-else-if="variant === 'dome'"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <path d="M12 4.2v1.3" />
      <circle cx="12" cy="2.8" r="1.1" fill="currentColor" stroke="none" />
      <rect x="4" y="5.5" width="16" height="12" rx="3.2" />
      <path d="M9 11h.01M15 11h.01" stroke-width="2.2" />
      <path d="M9.5 14.5h5" />
      <path d="M7 20.5h10" stroke-width="1.6" opacity="0.55" />
    </svg>

    <!-- 造型 C：iconfont 实心机器人（原始 1024 画布，填充 currentColor） -->
    <svg
      v-else
      viewBox="0 0 1024 1024"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path d="M683.7 922.7h-345c-73.5 0-133.3-59.8-133.3-133.3V459.8c0-73.5 59.8-133.3 133.3-133.3h345c73.5 0 133.3 59.8 133.3 133.3v329.6c0 73.5-59.8 133.3-133.3 133.3z m-345-506.9c-24.3 0-44.1 19.8-44.1 44.1v329.6c0 24.3 19.8 44.1 44.1 44.1h345c24.3 0 44.1-19.8 44.1-44.1V459.8c0-24.3-19.8-44.1-44.1-44.1h-345zM914.3 759.6c-24.6 0-44.6-20-44.6-44.6V534.3c0-24.6 20-44.6 44.6-44.6s44.6 20 44.6 44.6V715c0 24.7-20 44.6-44.6 44.6zM111.7 759.6c-24.6 0-44.6-20-44.6-44.6V534.3c0-24.6 20-44.6 44.6-44.6s44.6 20 44.6 44.6V715c0 24.7-19.9 44.6-44.6 44.6z" />
      <path d="M511.2 415.8c-24.6 0-44.6-20-44.6-44.6V239.3c0-24.6 20-44.6 44.6-44.6s44.6 20 44.6 44.6v131.9c0 24.6-20 44.6-44.6 44.6z" />
      <path d="M511.2 276.6c-49.2 0-89.2-40-89.2-89.2s40-89.2 89.2-89.2 89.2 40 89.2 89.2-40 89.2-89.2 89.2z m0-89.2h0.2-0.2z m0 0h0.2-0.2z m0 0h0.2-0.2z m0 0h0.2-0.2z m0 0z m0 0h0.2-0.2z m0 0h0.2-0.2z m0-0.1h0.2-0.2zM399 675.5c-28.1 0-50.9-22.8-50.9-50.9 0-28.1 22.8-50.9 50.9-50.9s50.9 22.8 50.9 50.9c0 28.1-22.8 50.9-50.9 50.9zM622.9 675.5c-28.1 0-50.9-22.8-50.9-50.9 0-28.1 22.8-50.9 50.9-50.9 28.1 0 50.9 22.8 50.9 50.9 0 28.1-22.8 50.9-50.9 50.9z" />
    </svg>
  </span>
</template>

<style scoped>
.robot-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--primary);
}
.robot-icon svg {
  width: 100%;
  height: 100%;
}
/* iconfont 造型是实心块面，图形几乎铺满画布 —— 缩一点，
   否则在方块底色里会顶到边框上。 */
.robot-icon.v-iconfont svg {
  width: 78%;
  height: 78%;
}
/* 带底色的方块版本：列表里代替头像的位置 */
.robot-icon.boxed {
  background: rgba(37, 99, 234, 0.14);
  border: 1px solid rgba(37, 99, 234, 0.3);
}
</style>