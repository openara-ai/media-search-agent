import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

function toWsOrigin(httpOrigin: string): string {
  const url = new URL(httpOrigin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString().replace(/\/$/, '')
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiOrigin = (env.VITE_API_ORIGIN || 'http://localhost:8000').replace(/\/$/, '')
  const wsOrigin = (env.VITE_WS_ORIGIN || toWsOrigin(apiOrigin)).replace(/\/$/, '')
  const devPort = Number(env.VITE_DEV_PORT || 5173)

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
    server: {
      port: devPort,
      proxy: {
        '/health':          apiOrigin,
        '/search':          apiOrigin,
        '/media':           apiOrigin,
        '/images':          apiOrigin,
        '/videos':          apiOrigin,
        '/faces':           apiOrigin,
        '/people':          apiOrigin,
        '/indexer':         apiOrigin,
        '/config':          apiOrigin,
        '/thumbnails':      apiOrigin,
        '/face_thumbnails': apiOrigin,
        '/ws': { target: wsOrigin, ws: true },
      },
    },
  }
})
