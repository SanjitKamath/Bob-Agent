import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The renderer is one page; each window picks its view from `?view=`.
export default defineConfig({
  root: "renderer",
  base: "./",
  plugins: [react()],
  build: { outDir: "../dist/renderer", emptyOutDir: true },
});
