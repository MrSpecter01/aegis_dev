import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: '0.0.0.0',
    proxy: {
      '/v1': {
        target: process.env.AEGIS_API_URL || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
});
