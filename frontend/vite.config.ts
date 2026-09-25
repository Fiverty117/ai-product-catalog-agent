import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    proxy: {
      "/api": loadEnv(mode, ".", "VITE_").VITE_API_TARGET ?? "http://127.0.0.1:8001",
    },
  },
}));
