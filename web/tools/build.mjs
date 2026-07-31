/**
 * Bundle the worker, the client and the harness entry into `dist/`.
 *
 * esbuild rather than a framework toolchain: this is a library boundary, not an
 * application, and the only non-obvious requirement is that `bridge.py` be
 * inlined as text so the Python source ships inside the worker bundle instead
 * of arriving as a separate, separately-cacheable fetch.
 */
import { build } from "esbuild";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");

await build({
  entryPoints: {
    worker: join(WEB, "src", "worker.ts"),
    harness: join(WEB, "test", "harness.ts"),
    main: join(WEB, "app", "src", "main.tsx"),
    app: join(WEB, "app", "src", "app.css"),
  },
  outdir: join(WEB, "dist"),
  bundle: true,
  format: "esm",
  target: "es2022",
  platform: "browser",
  jsx: "automatic",
  sourcemap: true,
  // React ships a development build unless told otherwise, and it is both
  // several times larger and noisier in the console — which matters here,
  // because the browser suite fails on a dirty console.
  define: { "process.env.NODE_ENV": '"production"' },
  minify: true,
  loader: { ".py": "text" },
  // `pyodide.mjs` is imported at runtime from a URL the worker computes, so it
  // must stay out of the bundle: it resolves its own wasm relative to where it
  // is served from.
  logLevel: "info",
});
