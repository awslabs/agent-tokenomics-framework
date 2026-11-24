import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The control-plane API runs at :8000 locally. Proxy /api -> that origin so the
// SPA calls a same-origin path in dev (and CORS is already enabled server-side).
export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js references `global`; alias to globalThis so it
  // loads in the browser.
  define: { global: "globalThis" },
  server: {
    host: true, // listen on all addresses so both localhost and 127.0.0.1 work
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.CONTROL_PLANE_API ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
