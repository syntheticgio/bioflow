import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    // The browser talks to the API through this proxy, so the frontend needs no
    // knowledge of the API origin and there is no CORS surface in dev.
    proxy: {
      "/api": { target: "http://api:8000", changeOrigin: true },
      "/healthz": { target: "http://api:8000", changeOrigin: true },
      "/readyz": { target: "http://api:8000", changeOrigin: true },
    },
  },
});
