// 临时脚本：复现「points 初始为空、稍后才拿到数据」的时序（跟真实详情接口一致）。
import { createApp, h, ref, onMounted } from 'vue'
import './src/styles.css'
import ProfitBarChart from './src/components/ProfitBarChart.vue'

const day = 86400
const now = Math.floor(Date.now() / 1000)
const later = [120, -80, 340, 0, -220, 410, 95].map((v, i) => ({
  date: new Date((now - (6 - i) * day) * 1000).toISOString().slice(5, 10),
  ts: now - (6 - i) * day,
  profit: v,
  deals: 3,
}))

createApp({
  setup() {
    const points = ref<typeof later>([])
    onMounted(() => {
      // 模拟接口返回：首屏空，600ms 后才有数据
      setTimeout(() => { points.value = later }, 600)
    })
    return () => h(ProfitBarChart, { points: points.value, height: 150 })
  },
}).mount('#app')