import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

function buildInfoPlugin() {
  return {
    name: 'chatds-build-info',
    generateBundle(_options, bundle) {
      const entry = Object.values(bundle).find((item) => (
        item.type === 'chunk' && item.isEntry
      ))
      if (!entry) this.error('Unable to identify the frontend entry asset')
      this.emitFile({
        type: 'asset',
        fileName: 'build-info.json',
        source: JSON.stringify({ entry: `/${entry.fileName}` }),
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), buildInfoPlugin()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8010'
    }
  }
})
