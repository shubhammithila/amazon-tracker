import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The API and WebSocket are PROXIED rather than called cross-origin.
 *
 * Without this the browser would need CORS on every route and an absolute URL in the client, so the
 * dev and production builds would talk to different addresses — which is how a page works locally and
 * 404s once deployed. Proxying keeps one relative path in the code.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8001", changeOrigin: true },
      "/ws": { target: "ws://localhost:8001", ws: true },
      "/health": { target: "http://localhost:8001", changeOrigin: true },
    },
  },
});
