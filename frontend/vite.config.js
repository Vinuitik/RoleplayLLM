import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In production nginx serves the bundle and proxies /api to the engine, so the
// app always talks to a same-origin /api. This proxy makes `npm run dev` behave
// identically against the engine's published loopback port.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
