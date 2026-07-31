/**
 * Which server the frontend calls, decided at build time.
 *
 * Worth its own file because the failure this prevents is a deployment mistake
 * that presents as an application bug: a Pages build with no proxy configured,
 * falling back to same-origin, requests `/api/filings` from a static host, gets
 * Pages' 404 HTML, and reports `unexpected token '<'`. Every case below is one
 * step of that chain being refused instead.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  apiAvailable,
  apiUrl,
  resolveApiBase,
  UNCONFIGURED_MESSAGE,
} from "../src/config.js";

const PAGES = "https://ruturaj-vasant.github.io";

test("a configured proxy origin is used verbatim", () => {
  const base = resolveApiBase("https://sec-tables-proxy.onrender.com", PAGES);
  assert.deepEqual(base, { kind: "configured", base: "https://sec-tables-proxy.onrender.com" });
  assert.equal(
    apiUrl("/api/filings", base),
    "https://sec-tables-proxy.onrender.com/api/filings",
  );
});

test("a trailing slash does not produce a doubled path", () => {
  const base = resolveApiBase("https://proxy.example.com/", PAGES);
  assert.equal(apiUrl("/api/filing", base), "https://proxy.example.com/api/filing");
});

test("localhost with nothing configured stays same-origin", () => {
  // `tools/serve.mjs` forwards /api/* to the Python proxy, so a relative path is
  // correct and no CORS or preflight is involved at all.
  for (const origin of ["http://127.0.0.1:5199", "http://localhost:3000", "http://[::1]:8080"]) {
    const base = resolveApiBase(undefined, origin);
    assert.deepEqual(base, { kind: "same-origin" }, origin);
    assert.equal(apiUrl("/api/filings", base), "/api/filings");
  }
});

test("a configured origin wins even on localhost", () => {
  // Pointing a local build at the deployed proxy has to remain possible; a
  // loopback check that overrode the configured value would make it silently not.
  const base = resolveApiBase("https://proxy.example.com", "http://127.0.0.1:5199");
  assert.deepEqual(base, { kind: "configured", base: "https://proxy.example.com" });
});

test("a static host with no proxy is its own state, not a same-origin guess", () => {
  const base = resolveApiBase("", PAGES);
  assert.deepEqual(base, { kind: "unconfigured" });
  assert.equal(apiAvailable(base), false);
});

test("calling an unconfigured build fails at the call site, with a deployment message", () => {
  const base = resolveApiBase(undefined, PAGES);
  assert.throws(() => apiUrl("/api/filings", base), /no filing server configured/i);
  assert.match(UNCONFIGURED_MESSAGE, /web\/README\.md/);
});

test("whitespace is not configuration", () => {
  assert.deepEqual(resolveApiBase("   ", PAGES), { kind: "unconfigured" });
  assert.deepEqual(resolveApiBase("  ", "http://localhost:5199"), { kind: "same-origin" });
});

test("a host that merely contains 'localhost' is not localhost", () => {
  // `https://localhost.evil.example.com` must not be read as a dev origin.
  assert.deepEqual(resolveApiBase(undefined, "https://localhost.evil.example.com"), {
    kind: "unconfigured",
  });
  assert.deepEqual(resolveApiBase(undefined, "https://not-127.0.0.1.example.com"), {
    kind: "unconfigured",
  });
});

test("apiAvailable is true for both working shapes", () => {
  assert.equal(apiAvailable(resolveApiBase("https://p.example.com", PAGES)), true);
  assert.equal(apiAvailable(resolveApiBase(undefined, "http://127.0.0.1:5199")), true);
});
