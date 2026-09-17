import * as esbuild from "esbuild";
import { mkdirSync, copyFileSync } from "node:fs";

const outdir = new URL("../maestro/web_dist/", import.meta.url).pathname;
mkdirSync(outdir, { recursive: true });
await esbuild.build({
  entryPoints: ["src/main.jsx"],
  bundle: true,
  minify: true,
  sourcemap: false,
  format: "iife",
  target: ["es2020"],
  outfile: `${outdir}console.js`,
  logLevel: "warning",
});
copyFileSync("index.html", `${outdir}index.html`);
console.log("built maestro/web_dist/");
