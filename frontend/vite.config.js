import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// Two entries, deliberately separate bundles:
//
//   index.html    — the Telegram Mini App and the public landing page.
//   screener.html — the standalone Wallet Screener.
//
// The screener is built as its own page so it can be served from
// screener.polytradebot.live as a static site later without any code change:
// point VITE_API_BASE at https://polytradebot.live/api and deploy the same
// dist. Keeping it out of the app bundle also keeps the authenticated session
// bootstrap off an anonymous public page.
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
        screener: resolve(__dirname, 'screener.html'),
      },
    },
  },
  server: { proxy: { '/api': 'http://localhost:8123' } },
})
