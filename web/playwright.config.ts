import { defineConfig, devices } from "@playwright/test";

/**
 * One project, one real browser, no mocks.
 *
 * `workers: 1` is not a performance concession: each test that prepares a
 * runtime allocates a wasm heap and holds a filing in it, and parallel workers
 * would make the timing measurements meaningless as well as the memory ceiling
 * unpredictable.
 */
export default defineConfig({
  testDir: "./test/browser",
  fullyParallel: false,
  workers: 1,
  // A cold start compiles ~9 MB of wasm; the first test in a file pays for it.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${process.env.PORT ?? 5199}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "node tools/serve.mjs",
    url: `http://127.0.0.1:${process.env.PORT ?? 5199}/index.html`,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
  },
});
