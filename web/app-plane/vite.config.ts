import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The chat SPA logs in via Cognito and calls the app-plane Chat API. Different
// dev port (5174) so it can run alongside the control-plane SPA (5173).
export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js references `global`, which doesn't exist in the
  // browser; alias it to `globalThis` so the SPA loads.
  define: {
    global: "globalThis",
  },
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: process.env.CONTROL_PLANE_API ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
