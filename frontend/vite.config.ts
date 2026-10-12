import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// For local development against a deployed stack:
//   VITE_API_PROXY=https://dxxxx.cloudfront.net npm run dev
// and put that stack's /config.json into public/config.json.
const apiProxy = process.env.VITE_API_PROXY;

export default defineConfig({
  plugins: [react()],
  build: { sourcemap: false, target: "es2022" },
  server: apiProxy ? { proxy: { "/api": { target: apiProxy, changeOrigin: true } } } : undefined,
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"] },
});
