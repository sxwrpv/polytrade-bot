import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: proxy /api to the FastAPI backend. Prod: built into dist/ and served
// same-origin by FastAPI's StaticFiles mount, so relative /api works there too.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist' },
  server: { proxy: { '/api': 'http://localhost:8123' } },
})
