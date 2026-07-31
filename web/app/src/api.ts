/**
 * The only way this page talks to SEC: through its own server.
 *
 * There is no direct-to-SEC path here and there cannot be one. Measured, not
 * assumed — see DECISIONS D38. `www.sec.gov` sends no `Access-Control-Allow-
 * Origin` at all; `data.sec.gov` does send `*`, but SEC's edge answers a
 * browser's own User-Agent with 403 and that 403 carries no CORS header either;
 * and a page cannot fix that, because `fetch()` drops a `User-Agent` it is given
 * without raising.
 *
 * Which server it is, is `config.ts`'s decision — same-origin in development,
 * a hosted proxy on GitHub Pages.
 */
import { apiUrl } from "./config.js";
import {
  parseFilingList,
  parseFilingMeta,
  type FilingListResponse,
  type FilingMeta,
  type ProxyErrorKind,
  type ProxyErrorShape,
} from "./domain.js";

export class ProxyError extends Error implements ProxyErrorShape {
  readonly kind: ProxyErrorKind;
  constructor(kind: ProxyErrorKind, message: string) {
    super(message);
    this.name = "ProxyError";
    this.kind = kind;
  }
}

export interface FilingQuery {
  email: string;
  ticker: string;
  year: number;
  form: string;
  filingId?: string | null;
}

export interface FetchedFiling {
  bytes: ArrayBuffer;
  meta: FilingMeta;
  cached: boolean;
}

async function readError(response: Response): Promise<ProxyError> {
  let kind: ProxyErrorKind = "upstream_failure";
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (body?.error?.kind) {
      kind = body.error.kind;
      message = body.error.message ?? message;
    }
  } catch {
    /* a non-JSON error body is itself the message */
  }
  return new ProxyError(kind, message);
}

/**
 * POST, for a read.
 *
 * The contact email is in the body deliberately: a query string lands in access
 * logs, `Referer` headers, browser history and intermediary caches, and the one
 * promise this app makes about that address is that it is not kept anywhere.
 */
async function post(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  let url: string;
  try {
    url = apiUrl(path);
  } catch (err) {
    // A build with no proxy configured. Its own kind, because the fix is a
    // deployment change and no amount of retrying or re-typing helps.
    throw new ProxyError("not_configured", (err as Error).message);
  }
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
      // No cookies, no `Authorization`, nothing ambient — and saying so keeps
      // the proxy free to answer with a specific `Access-Control-Allow-Origin`
      // instead of having to satisfy the credentialed-request rules.
      credentials: "omit",
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    // A cross-origin refusal arrives here as an opaque `TypeError: Failed to
    // fetch` — the browser does not tell the page why, deliberately. So this
    // cannot distinguish "the proxy is down" from "the proxy refused this
    // origin", and the message says both rather than guessing one.
    throw new ProxyError("offline", (err as Error).message);
  }
  if (!response.ok) throw await readError(response);
  return response;
}

/**
 * Wake the proxy, and find out whether it is there.
 *
 * On a host that scales to zero the first real request pays the whole cold
 * start — up to about a minute on Render's free tier — and it pays it at the
 * worst moment, after someone has filled in a form and pressed a button. This
 * runs on page load instead, in parallel with Pyodide's 1.3 s preparation, so
 * the wait overlaps work the visitor was already doing.
 *
 * Failure is not an error: it returns null and the app carries on. A warm-up
 * that could break the page would be worse than no warm-up.
 */
export async function pingProxy(signal?: AbortSignal): Promise<Record<string, unknown> | null> {
  try {
    const response = await fetch(apiUrl("/api/health"), { signal, credentials: "omit" });
    if (!response.ok) return null;
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export async function listFilings(
  query: FilingQuery,
  signal?: AbortSignal,
): Promise<FilingListResponse> {
  const response = await post(
    "/api/filings",
    { email: query.email, ticker: query.ticker, year: query.year, form: query.form },
    signal,
  );
  return parseFilingList(await response.json());
}

export async function fetchFiling(
  query: FilingQuery,
  signal?: AbortSignal,
): Promise<FetchedFiling> {
  const response = await post(
    "/api/filing",
    {
      email: query.email,
      ticker: query.ticker,
      year: query.year,
      form: query.form,
      filingId: query.filingId ?? null,
    },
    signal,
  );
  // `arrayBuffer()`, not `text()`: the bytes go straight to the Worker, and a
  // pre-2001 filing is latin-1 that a text decode would quietly mangle.
  const bytes = await response.arrayBuffer();
  return {
    bytes,
    meta: parseFilingMeta(response.headers.get("X-Filing-Meta")),
    cached: response.headers.get("X-Filing-Cache") === "hit",
  };
}
