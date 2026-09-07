import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Tauri 固定前端端口
export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: "es2021",
    minify: "esbuild",
    sourcemap: false,
  },
});
