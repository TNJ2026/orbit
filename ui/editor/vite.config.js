import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands inside the Python package, and the result is committed.
// Someone installing orbit with pip has no node and no way to run this, so the
// wheel has to ship the editor already built; hatchling includes every
// non-ignored file under src/orbit, which is what puts it there.
export default defineConfig({
  plugins: [react()],
  base: "/editor/",
  build: {
    outDir: fileURLToPath(
      new URL("../../src/orbit/static/workflow-editor", import.meta.url),
    ),
    emptyOutDir: true,
    // No sourcemaps and no hashed chunk names: the output is committed, so a
    // rebuild that changes only a hash would be noise in every diff.
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name].[ext]",
      },
    },
  },
});
