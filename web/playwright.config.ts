import { defineConfig, devices } from "@playwright/test";

/**
 * One real browser, no mocks in the browser.
 *
 * Two servers are started: the static server (which also forwards `/api/*`) and
 * the proxy. In the default suite the proxy is the **real** `sec_proxy` with
 * SEC's network seam faked, so the app exercises genuine routing, validation,
 * caching and error mapping while the bytes stay the committed fixtures. Point
 * `PROXY_CMD` at the live proxy to run against EDGAR instead.
 *
 * `workers: 1` is not a performance concession: each browser test builds a
 * Pyodide runtime with its own wasm heap, and running several at once makes the
 * timing measurements meaningless and the memory ceiling unpredictable.
 */
const PORT = Number(process.env.PORT ?? 5199);
const PROXY_PORT = Number(process.env.PROXY_PORT ?? 5310);

export default defineConfig({
  testDir: "./test/browser",
  fullyParallel: false,
  workers: 1,
  // A cold start compiles ~9 MB of wasm; the first test in a file pays for it.
  timeout: 180_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "node tools/serve.mjs",
      url: `http://127.0.0.1:${PORT}/index.html`,
      reuseExistingServer: !process.env.CI,
      stdout: "ignore",
      stderr: "pipe",
      env: { PORT: String(PORT), PROXY_TARGET: `http://127.0.0.1:${PROXY_PORT}` },
    },
    {
      command: process.env.PROXY_CMD ?? "python3 tools/fake_sec.py",
      url: `http://127.0.0.1:${PROXY_PORT}/api/health`,
      // Never reused, unlike the static server. A proxy left running from an
      // earlier session serves whatever fixture set it started with, and the
      // failure looks like an application bug rather than a stale process.
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
      env: { PROXY_PORT: String(PROXY_PORT) },
    },
  ],
});
