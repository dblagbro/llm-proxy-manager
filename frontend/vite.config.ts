import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  // v3.0.16: use relative asset paths so a single built bundle can be
  // served from any URL prefix (e.g. /llm-proxy2/ for prod and
  // /llm-proxy2-smoke/ for the pre-prod canary). Companion runtime
  // detection lives in src/lib/basePath.ts; consumers are App.tsx
  // (BrowserRouter basename) and api/client.ts (request prefix).
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:3000',
      '/v1': 'http://localhost:3000',
      '/health': 'http://localhost:3000',
      '/cluster': 'http://localhost:3000',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
