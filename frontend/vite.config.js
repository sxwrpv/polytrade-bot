import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// One entry: index.html, the Telegram Mini App and the public landing page.
//
// There was a second entry, screener.html, holding a React Wallet Screener.
// The canonical trader research surface is now the dedicated trader-screener
// service (see trader-screener/ and the Caddyfile), so that page was removed
// rather than left building into dist/ where it stayed reachable and served a
// snapshot frozen at image-build time.
//
// Dev: proxy /api to the FastAPI backend. Prod: built into dist/ and served
// same-origin by FastAPI's StaticFiles mount, so relative /api works there too.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
      },
    },
  },
  server: { proxy: { '/api': 'http://localhost:8123' } },
})
