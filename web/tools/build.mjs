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

/**
 * Where the deployed frontend finds its proxy.
 *
 * Baked in here rather than fetched from a config file at runtime, because a
 * config fetch is an extra round trip before the app can do anything and a
 * failed one is a blank page. Empty is a legitimate value and the default: it
 * means "same origin", which is what local development wants.
 *
 * This is a public URL, not a secret. It ships inside a bundle any visitor can
 * read, so putting it in a GitHub *secret* would suggest a confidentiality this
 * value does not have — the Pages workflow reads it from a repository variable.
 */
const API_BASE = (process.env.SEC_TABLES_API_BASE ?? "").trim().replace(/\/+$/, "");
// A page served over https cannot call an http origin — the browser blocks it as
// mixed content, and as far as the app is concerned the request simply fails.
// Failing the build is the only place that is cheap to notice. Loopback is the
// exception, because the page is http there too: that is how the browser suite
// runs the whole workflow cross-origin against the local proxy.
const LOOPBACK_HTTP = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$/;
if (API_BASE && !/^https:\/\//.test(API_BASE) && !LOOPBACK_HTTP.test(API_BASE)) {
  throw new Error(
    `SEC_TABLES_API_BASE must be an https:// origin (or http loopback), got: ${API_BASE}`,
  );
}
console.log(`  api base: ${API_BASE || "(same origin)"}`);

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
  define: {
    "process.env.NODE_ENV": '"production"',
    __SEC_TABLES_API_BASE__: JSON.stringify(API_BASE),
  },
  minify: true,
  loader: { ".py": "text" },
  // `pyodide.mjs` is imported at runtime from a URL the worker computes, so it
  // must stay out of the bundle: it resolves its own wasm relative to where it
  // is served from.
  logLevel: "info",
});
