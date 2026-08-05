import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri needs a fixed dev-server port and to know its own host so the
// webview can reach it; see https://v2.tauri.app/start/frontend/vite/.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
});
