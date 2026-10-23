<script setup lang="ts">
// 用户头像：无图片上传能力时，用「显示名首字符」生成一枚稳定的彩色字母头像。
//
// 为什么不用 `name[0]` 直接取：中文名的首字是「张/李/王」，英文名是首字母，
// 两者都要大写；emoji / 生僻字是代理对，`[0]` 会截出半个乱码，
// 所以统一走 Array.from 按**码点**取第一个可见字符。
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 显示名 / 用户名；为空时退化成问号 */
    name?: string | null
    /** 直径（px）。列表行用 28–34，详情头部用 40+ */
    size?: number
  }>(),
  { name: '', size: 32 },
)

/** 首字符：按码点取，避免 emoji 代理对被截断 */
const initial = computed(() => {
  const s = (props.name ?? '').trim()
  if (!s) return '?'
  return Array.from(s)[0].toUpperCase()
})

/**
 * 头像配色：由名字散列出一个稳定的索引，同一个人永远同一套颜色，
 * 且与服务端数据无关 —— 刷新、换设备、换人都不会变。
 *
 * 主色系对齐「带单广场」参考设计（紫 #753EE4 为主，蓝 #3762E4 为辅），
 * 其余色相在深色玻璃背景上同样保证足够对比度。
 */
const PALETTE: readonly [string, string][] = [
  ['#753EE4', '#5B21B6'],
  ['#3762E4', '#1E40AF'],
  ['#8B5CF6', '#6D28D9'],
  ['#06B6D4', '#0E7490'],
  ['#EC4899', '#BE185D'],
  ['#10B981', '#047857'],
  ['#F59E0B', '#B45309'],
  ['#3B82F6', '#1E3A8A'],
]

const gradient = computed(() => {
  const s = (props.name ?? '').trim() || '?'
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  const [a, b] = PALETTE[h % PALETTE.length]
  return `linear-gradient(135deg, ${a} 0%, ${b} 100%)`
})

/** 直径变化时字号跟着走，保证「张」这类高字面字符在 28px 圆里也居中不溢出 */
const fontSize = computed(() => `${Math.round(props.size * 0.4)}px`)
</script>

<template>
  <span
    class="user-avatar"
    :style="{ width: `${size}px`, height: `${size}px`, background: gradient, fontSize }"
    aria-hidden="true"
    >{{ initial }}</span
  >
</template>

<style scoped>
.user-avatar {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: #fff;
  font-weight: 700;
  line-height: 1;
  letter-spacing: 0;
  user-select: none;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
}
</style>