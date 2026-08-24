import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The launcher's own version, read from package.json -- the file ops/release.sh
// bumps on every cut, and the only one of the three version declarations that
// keeps the `-alpha`/`-beta` suffix (#808). src/version.ts consumes it.
const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf8"),
) as { version: string };

// Tauri needs a fixed dev-server port and to know its own host so the
// webview can reach it; see https://v2.tauri.app/start/frontend/vite/.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  define: {
    __LAUNCHER_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    port: 1420,
    strictPort: true,
  },
});
