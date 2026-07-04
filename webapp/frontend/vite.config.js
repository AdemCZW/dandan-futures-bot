import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 開發時把 /api 代理到 FastAPI 後端（uvicorn :8000），避免跨網域問題。
// base：GitHub Pages 是 repo 專案頁（https://<user>.github.io/<repo>/），資源路徑需帶 repo 名前綴；
// 本機開發／Railway dashboard（自訂網域根路徑）則維持 '/'。GH Actions 建置時注入 VITE_BASE_PATH。
export default defineConfig({
  base: process.env.VITE_BASE_PATH || '/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
