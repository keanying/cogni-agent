import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发环境把 /api 原样转发给后端(8000 端口)。main.py 把全部 API 路由都挂在 /api 前缀下,
// 生产环境由后端直接托管 dist/ 时前端请求的路径与这里完全一致,不需要重写路径。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
