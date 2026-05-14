import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("monaco-editor")) return "monaco";
          if (id.includes("node_modules/react") || id.includes("node_modules/react-dom")) return "react-vendor";
          if (id.includes("node_modules/lucide-react")) return "icons";
          if (id.includes("node_modules/sql-formatter")) return "sql-formatter";
          if (id.includes("/src/Dashboard") || id.includes("/src/WidgetEditor")) return "dashboards";
          if (id.includes("/src/DAGPanel") || id.includes("/src/SentinelPanel")) return "dag";
          if (id.includes("/src/NotebookPanel") || id.includes("/src/ModelNotebookView")) return "notebooks";
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:3000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:3000",
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test-setup.js",
    include: ["src/**/*.{test,spec}.{js,jsx,ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
