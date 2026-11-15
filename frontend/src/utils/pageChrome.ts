import { ref } from 'vue'

/** 客户顶栏的临时标题。页内二级界面替换路由名，离开时清空。 */
export const pageChrome = ref<{ title: string; back: () => void } | null>(null)
