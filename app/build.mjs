// Bundles the main process and the preload script. The renderer is built by Vite.
import { build } from "esbuild";

const common = {
  bundle: true,
  platform: "node",
  target: "node22",
  format: "cjs",
  external: ["electron"],
  sourcemap: true,
  logLevel: "info",
};

await build({ ...common, entryPoints: ["main/index.ts"], outfile: "dist/main/index.js" });
await build({ ...common, entryPoints: ["main/preload.ts"], outfile: "dist/main/preload.js" });
