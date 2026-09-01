import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The MCP App cannot rely on a CDN or on the host permitting a nested HTTP
// frame. Build the read-only XYFlow surface as one IIFE plus one stylesheet;
// Python embeds both into the MCP resource at import time. The generated files
// live under src/ so wheels and the Codex plugin ship them without Node.js.
export default defineConfig({
  plugins: [react()],
  // The card runs directly in a sandboxed browser iframe where Node's
  // `process` global does not exist. Pin React's compile-time branch even
  // when the parent shell exports NODE_ENV=development while packaging.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: fileURLToPath(
      new URL("../../src/orbit/static/mcp-app", import.meta.url),
    ),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    lib: {
      entry: fileURLToPath(
        new URL("./src/mcp-workflow-graph.jsx", import.meta.url),
      ),
      name: "OrbitWorkflowGraph",
      formats: ["iife"],
      fileName: () => "workflow-detail.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (asset) => (
          asset.name?.endsWith(".css") ? "workflow-detail.css" : "[name][extname]"
        ),
      },
    },
  },
});
