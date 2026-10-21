import { createApp } from 'vue'
import App from './App.vue'
import ViewerApp from './ViewerApp.vue'
import './style.css'

const params = new URLSearchParams(window.location.search)
const imagePath = params.get('image')

// /view?image=<逻辑路径+文件名>: 独立图片查看页 (用于"在新窗口查看")
if (window.location.pathname.startsWith('/view') && imagePath) {
  createApp(ViewerApp, { imagePath }).mount('#app')
} else {
  createApp(App).mount('#app')
}
