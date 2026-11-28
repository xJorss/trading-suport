import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies, so the app can use same-origin paths (/api, /ws) in both
// development and the container: Vite forwards them here, nginx forwards them
// to `backend:8000` in Docker. Nothing in the bundle hardcodes a port.
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
