import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles.css'

// 应用入口：Pinia 必须在 router 之前装载（路由守卫里会用到 store）
createApp(App).use(createPinia()).use(router).mount('#app')
