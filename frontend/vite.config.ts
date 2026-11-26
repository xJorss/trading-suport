import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The frontend talks to the backend over a direct websocket
// (ws://127.0.0.1:8000/ws), so no proxy is strictly required.
// The /api proxy is here so `fetch("/api/signals")` works in dev without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
