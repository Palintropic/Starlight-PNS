// 从 vitest/config 取 defineConfig：它就是 vite 的那一个，只是类型上多认
// 一个 test 段。构建行为一模一样。
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:7860',
      '/ws': { target: 'ws://localhost:7860', ws: true },
    },
  },
  // 前端测试。目前只覆盖一件 lint / typecheck / build 都证明不了的事：
  // 持久世界控制面上并发请求的时序（见 PersistentWorlds.timing.test.tsx）。
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
