import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The frontend always calls the API through the relative `/api` prefix. In dev
// Vite proxies that to the backend; the proxy target is env-driven so the same
// config works locally (http://localhost:8000) and inside Docker, where the
// backend is reachable by its compose service name (http://backend:8000).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      // Required so the dev server reloads on file changes from a bind mount.
      watch: { usePolling: true },
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
    preview: { host: '0.0.0.0', port: 5173 },
  }
})
