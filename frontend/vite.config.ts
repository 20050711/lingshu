import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// WSL ext4 原生文件系统，inotify 正常，无需 usePolling
// M4：代理目标走环境变量（VITE_API_PROXY_TARGET），默认本地后端（dev-only，不参与生产构建）
// 默认后端 :8001（可用 VITE_API_PROXY_TARGET 覆盖）
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [react()],
  // 2026-09-08 开发机专用开关（免费档快捷切换）：define 为布尔字面量——部署构建不带变量时
  // 折叠为 false，esbuild 死代码删除（bundle 内不含该功能代码，非仅运行时隐藏）。
  // 开发机构建：VITE_DEV_FREE_TOGGLE=1 npm run build；部署机：VITE_DEV_FREE_TOGGLE=0 npm run build。
  define: {
    __DEV_FREE_TOGGLE__: JSON.stringify(process.env.VITE_DEV_FREE_TOGGLE === '1'),
  },
  server: {
    port: 5174,
    host: '127.0.0.1',  // SEC-21：dev server 仅本机监听（原 host:true 局域网暴露）；e2e 从 WSL 本机访问不受影响
    proxy: {
      '/api': { target: apiProxyTarget, changeOrigin: true },
    },
  },
  build: {
    // 大依赖分包：echarts/antd/react 独立 chunk，浏览器缓存复用，首屏更快
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          echarts: ['echarts', 'echarts-for-react'],
          antd: ['antd'],
          markdown: ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
})
