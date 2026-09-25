import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The console is a separate build from the Python package. `npm run dev`
// proxies /api to the loopback console server (python -m atlas_erp.web) so the
// browser sees one origin; nothing is written, and the API stays the only
// data source.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:4311",
        changeOrigin: true,
      },
    },
  },
  build: {
    // Keep the dependency surface reviewable; every shadcn component here is
    // copied source, not a runtime component package.
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
