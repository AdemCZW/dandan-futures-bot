import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 開發時把 /api 代理到 FastAPI 後端（uvicorn :8000），避免跨網域問題。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
