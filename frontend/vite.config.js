import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { cpSync, createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const MONACO_VS = resolve(HERE, "node_modules/monaco-editor/min/vs");
const MONACO_TYPES = { ".js": "application/javascript", ".css": "text/css", ".ttf": "font/ttf", ".json": "application/json" };

// Monaco is served by havn, not fetched from cdn.jsdelivr.net (see
// src/monacoSetup.js): the build copies its AMD files to dist/monaco/vs, and
// the dev server serves them straight from node_modules. Two plugins, because
// Vite also calls closeBundle when a dev or test server shuts down: one
// plugin with both hooks copied 16 MB on every vitest run.
function monacoServe() {
  return {
    name: "havn-monaco-serve",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/monaco/vs", (req, res, next) => {
        const rel = normalize(decodeURIComponent((req.url || "/").split("?")[0])).replace(/^[/\\]+/, "");
        const file = join(MONACO_VS, rel);
        if (!file.startsWith(MONACO_VS + sep) || !existsSync(file) || !statSync(file).isFile()) return next();
        res.setHeader("Content-Type", MONACO_TYPES[extname(file)] || "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
  };
}

function monacoCopy() {
  let outDir = resolve(HERE, "dist");
  return {
    name: "havn-monaco-copy",
    apply: "build",
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      cpSync(MONACO_VS, join(outDir, "monaco", "vs"), { recursive: true });
    },
  };
}

export default defineConfig({
  plugins: [react(), monacoServe(), monacoCopy()],
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
