import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  base: '/emby-manager/assets/',
  plugins: [react()],
  server: {
    proxy: {
      '/emby-manager/auth': 'http://127.0.0.1:8084',
      '/emby-manager/v1': 'http://127.0.0.1:8084',
    },
  },
});
