import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiProxy = {
  "/api": {
    target: "http://localhost:8000",
    changeOrigin: true,
  },
};

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  worker: { format: "es" },
  // Prebundle the worker-only CommonJS import before opening the first graph.
  optimizeDeps: { include: ["elkjs/lib/elk.bundled.js"] },
  server: {
    // Forwards /api/* to the FastAPI backend during local dev, so the
    // browser sees everything as same-origin -- no CORS headers needed
    // for this to work. (main.py's CORS middleware exists only for the
    // separate case of a built frontend served from a different origin.)
    proxy: apiProxy,
  },
  preview: { proxy: apiProxy },
});
