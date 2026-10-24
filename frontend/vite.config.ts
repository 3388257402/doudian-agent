import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev proxy：前端只请求 /api/*（CONTRACT §11 前端自检第 2 条），
// 由 Vite 把 /api 转发到 FastAPI，避免前端出现任何绝对后端地址/CORS 配置。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
