import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// One entry, index.html: the Telegram Mini App and the public landing page.
// The Wallet Screener at /screener is the separate trader-screener service.
//
// Dev: proxy /api to the FastAPI backend. Prod: built into dist/ and served
// same-origin by FastAPI's StaticFiles mount, so relative /api works there too.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist' },
  server: { proxy: { '/api': 'http://localhost:8080' } },   // backend default port (README)
})
