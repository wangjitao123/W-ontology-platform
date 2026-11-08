import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { templateCompilerOptions } from '@tresjs/core'
import { fileURLToPath, URL } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue({ ...templateCompilerOptions })],
  optimizeDeps: { include: ['dagre', 'marked'] },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: Number(process.env.FRONTEND_PORT || 5177),
    allowedHosts: ['ontology.ojlab.com'],
    watch: {
      // 原子保存（编辑器/tool 写入临时目录再 rename）时，chokidar 会尝试 watch 临时文件，
      // Windows 下常报 EBUSY 并导致 dev server 直接退出 —— 忽略这些临时产物
      ignored: ['**/.git/**', '**/node_modules/**', '**/.*.tmpdir/**', '**/.*.tmp'],
    },
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  // 生产预览：与 dev 用同一套 /api 代理，页面加载不再按需编译
  preview: {
    host: '0.0.0.0',
    port: Number(process.env.FRONTEND_PREVIEW_PORT || 5179),
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 大屏/图谱组件体积较大，放宽警告阈值减少噪音
    chunkSizeWarningLimit: 2000,
  },
})
