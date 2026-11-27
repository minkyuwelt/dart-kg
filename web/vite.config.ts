import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },   // 노트북에서 3000·8080은 다른 수업이 쓴다 (CLAUDE.md)
});
