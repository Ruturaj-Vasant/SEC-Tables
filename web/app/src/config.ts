/**
 * Where the API lives — the one place that knows, and it is decided at build time.
 *
 * Two deployments, and they are genuinely different shapes rather than one shape
 * with a different hostname:
 *
 * * **Local development.** `tools/serve.mjs` forwards `/api/*` to the Python
 *   proxy on another port, so the page and the API share an origin. Nothing is
 *   configured, nothing is cross-origin, and no preflight happens.
 * * **GitHub Pages.** Static hosting cannot run `web/proxy/`, so the API is on
 *   another host entirely and every call is cross-origin. The origin is baked in
 *   at build time by `SEC_TABLES_API_BASE`.
 *
 * The third case is the one worth designing for: **Pages built without that
 * variable set.** The naive behaviour — fall back to same-origin — makes the
 * deployed app request `https://user.github.io/SEC-Tables/api/filings`, get
 * Pages' 404 HTML page, and report "unexpected token '<'". That is a deployment
 * mistake dressed up as a parser bug, so it is named as its own state instead.
 */

/**
 * Replaced at build time by esbuild's `define`. Written as a `declare` rather
 * than read off `process.env` at runtime because there is no `process` in a
 * browser — the substitution has to have already happened.
 */
declare const __SEC_TABLES_API_BASE__: string;

export type ApiBase =
  /** A hosted proxy on another origin. Every call is cross-origin. */
  | { kind: "configured"; base: string }
  /** The dev server is forwarding `/api/*`; relative paths are correct. */
  | { kind: "same-origin" }
  /** Static hosting with no proxy configured. Nothing can work; say so. */
  | { kind: "unconfigured" };

const LOOPBACK = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$/;

/**
 * Pure so it can be tested without a page.
 *
 * `configured` wins wherever it is set, including on localhost: a contributor
 * pointing a local build at the deployed proxy is a thing worth being able to do,
 * and having the loopback check override it would make that silently impossible.
 */
export function resolveApiBase(configured: string | undefined, pageOrigin: string): ApiBase {
  const base = (configured ?? "").trim().replace(/\/+$/, "");
  if (base) return { kind: "configured", base };
  if (LOOPBACK.test(pageOrigin)) return { kind: "same-origin" };
  return { kind: "unconfigured" };
}

/** `about:` and `blob:` pages have the string "null" as an origin, not a URL. */
const pageOrigin = (): string =>
  typeof location === "undefined" ? "" : location.origin;

/** The build-time value, or the empty string when the define was not applied. */
export const CONFIGURED_API_BASE: string =
  typeof __SEC_TABLES_API_BASE__ === "string" ? __SEC_TABLES_API_BASE__ : "";

export const API_BASE: ApiBase = resolveApiBase(CONFIGURED_API_BASE, pageOrigin());

/**
 * Turn an API path into the URL to call.
 *
 * Throws on `unconfigured` rather than returning a relative path that would 404
 * against the static host: the failure belongs at the call site with a message
 * about deployment, not three layers later as malformed JSON.
 */
export function apiUrl(path: string, base: ApiBase = API_BASE): string {
  switch (base.kind) {
    case "configured":
      return base.base + path;
    case "same-origin":
      return path;
    case "unconfigured":
      throw new Error(UNCONFIGURED_MESSAGE);
  }
}

export const UNCONFIGURED_MESSAGE =
  "This build has no filing server configured, so it cannot fetch from SEC. " +
  "The interface and browser extraction still work; fetching needs the proxy " +
  "described in web/README.md.";

/** True when the app can reach an API at all. Drives what the page offers. */
export const apiAvailable = (base: ApiBase = API_BASE): boolean =>
  base.kind !== "unconfigured";
